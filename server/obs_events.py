"""What the server's events mean: HTTP request policy, collection and Instagram signals, extension log intake.

obs.py is the pipeline; this file decides what is worth saying. It only observes: nothing here changes a
response, a permit or a pace. Every public function is a no-op while logging is off and never raises.

Volume rules: machine polls (heartbeats, lease checks, permit retries) are folded into per-route summaries and
state transitions; every Instagram request, every list page, every adverse response and every operator action
is its own line.
"""
import functools
import hashlib
import itertools
import json
import re
import time

import obs

SLOW_MS = 500
_ids = itertools.count(1)
_ID = re.compile(r'/\d+(?=/|$)')
_KEY = re.compile(r'/[0-9a-f]{10}(?=/|$)')
_LANE = re.compile(r'^/api/accounts/[A-Za-z0-9_-]+')
_EVENT_NAME = re.compile(r'^[a-z][a-z0-9_.:-]{0,59}$')
_FIELD_NAME = re.compile(r'^[a-z][a-z0-9_]{0,31}$')
_POLL_POSTS = frozenset(('/api/ext/heartbeat', '/api/ext-log', '/api/benchmark/permit', '/api/benchmark/result'))
_ADVERSE = frozenset(('rate_limit', 'soft_block', 'login', 'challenge'))
HEARTBEAT_GAP_S = 90
SILENT_S = 120


def route_key(path):
    """A path with ids removed, so '/api/person/12/mark' and '/api/person/13/mark' are one route."""
    if path.startswith('/img/'):
        return '/img/:id'
    if not path.startswith('/api/'):
        return '/static'
    if path.startswith('/api/accounts/'):
        path = _LANE.sub('/api/accounts/:lane', path)
    return _KEY.sub('/:key', _ID.sub('/:id', path))[:80]


def _first(values, key):
    got = values.get(key) if isinstance(values, dict) else None
    return got[0] if isinstance(got, list) and got else None


def _text(value, limit=64):
    return value[:limit] if isinstance(value, str) and value else None


def _fp(cursor):
    """Short fingerprint of an Instagram pagination cursor: enough to see a repeat, useless as a credential."""
    return hashlib.sha1(cursor.encode()).hexdigest()[:8] if isinstance(cursor, str) and cursor else None


# ---------------------------------------------------------------- HTTP requests
class Ticket:
    __slots__ = ('start', 'method', 'route', 'rid', 'lane', 'account', 'observer', 'warned', 'q', 'body')


def request_begin(method, path):
    """Called first thing in Handler.route. Returns a ticket or None while logging is off."""
    if not obs.S.on:
        return None
    try:
        route = route_key(path.partition('?')[0])
        ticket = Ticket()
        ticket.start = time.monotonic()
        ticket.method, ticket.route = method, route
        ticket.rid = '%s.%x' % (obs.S.run_id[-4:], next(_ids))
        ticket.lane = ticket.account = ticket.q = ticket.body = None
        ticket.observer = _OBSERVERS.get((method, route))
        ticket.warned = False
        obs.unbind()
        obs.bind(request_id=ticket.rid, route=route)
        obs.request_enter(ticket)
        return ticket
    except Exception:
        obs.S.internal_errors += 1
        return None


def request_end(ticket, status, nbytes, exc=None):
    """Called when the request is finished: one summary line when it is worth one, else a folded count."""
    if ticket is None:
        return
    try:
        obs.request_exit()
        dur = (time.monotonic() - ticket.start) * 1000
        route, method = ticket.route, ticket.method
        disconnect = not status and isinstance(exc, (BrokenPipeError, ConnectionResetError))
        label = str(status) if status else 'client_gone' if exc is not None else '0'
        failed = bool(status and status >= 400) and not (status == 404 and route == '/static')
        slow = dur >= SLOW_MS
        individual = (failed or slow or (exc is not None and not status and not disconnect) or
                      (method != 'GET' and ticket.observer is None and route not in _POLL_POSTS))
        repeats = None
        if individual and failed and not slow and status < 500:
            # A client hammering one bad URL becomes a count, not a flood of lines.
            repeats = obs.throttle(('req', method, route, status), 5)
            individual = repeats is not None
        if individual:
            level = ('error' if status and status >= 500 and status != 503 else   # 503 = busy, retry: a warning
                     'warn' if slow or (failed and status != 404) else 'info')
            fields = {'err': type(exc).__name__, 'err_msg': str(exc)} if exc is not None and (failed or not status) else {}
            obs.event('http', 'req', level, method=method, route=route, status=status or None, dur_ms=round(dur, 1),
                      bytes=nbytes or None, suppressed=repeats or None, **fields)
        else:
            obs.agg('http', 'agg', '%s %s' % (method, route), dur, nbytes or 0, label, every=60)
    except Exception:
        obs.S.internal_errors += 1
    finally:
        obs.unbind()


def _remember_handle(lane, handle):
    if len(_LANE_HANDLE) > 256 and lane not in _LANE_HANDLE:
        _LANE_HANDLE.clear()
    _LANE_HANDLE[lane] = handle


def bind_api(ticket, query, body):
    """Called after the body is parsed, before the endpoint runs: attach lane and account to this request's events."""
    if ticket is None:
        return
    try:
        ticket.q, ticket.body = query, body
        lane = _text(body.get('lane_id')) or _text(_first(query, 'lane'))
        if lane is None:
            return
        account = body.get('account')
        handle = _text(account.get('handle')) if isinstance(account, dict) else None
        handle = handle or _text(_first(query, 'handle'))
        if handle:
            _remember_handle(lane, handle)
        ticket.lane, ticket.account = lane, handle or _LANE_HANDLE.get(lane)
        obs.bind(lane=ticket.lane, account=ticket.account)
    except Exception:
        obs.S.internal_errors += 1


def done_api(ticket, output):
    """Called with the endpoint's result: turns collection traffic into the events an agent reads."""
    if ticket is None or ticket.observer is None or not isinstance(output, dict):
        return
    try:
        ticket.observer(ticket, ticket.q or {}, ticket.body or {}, output)
    except Exception:
        obs.S.internal_errors += 1


# ---------------------------------------------------------------- collection and Instagram signals
_LANE_HANDLE = {}
_LANES = {}      # lane -> last heartbeat facts (in memory only)
_denied = set()  # lanes whose heartbeat went silent and have not come back


def _hint(lane):
    """Why a lane is blocked, from what its last heartbeat said. None when nothing explains it."""
    row = _LANES.get(lane)
    if not row:
        return None
    if row.get('upgrade'):
        return 'extension_upgrade_required'
    if row.get('hold'):
        return 'hold:%s' % row['hold']
    if row.get('paused'):
        return 'paused'
    if row.get('cooldown_until'):
        return 'cooldown_until=%s' % row['cooldown_until']
    stages = row.get('stages')
    if isinstance(stages, dict) and not any(stages.values()):
        return 'stages_off'
    return None


def _permit(t, q, b, out):
    lane, kind = t.lane, b.get('kind')
    if b.get('action') == 'release':
        if out.get('released') is False:
            obs.warn('collect', 'permit_release_unmatched', kind=kind)
        return
    key = ('permit', lane, kind)
    if out.get('granted'):
        streak = obs.transition_end(key) or {}
        extra = {'after_denials': streak.get('repeats'), 'denied_for_s': streak.get('held_s')} if streak else {}
        obs.info('collect', 'permit', granted=True, kind=kind, job_id=b.get('job_id'), **extra)
        return
    wait = out.get('wait_ms')
    why = (str(out['reason']) if out.get('reason') else 'stale_lease' if out.get('stale') else
           'blocked' if wait == 15000 else 'busy' if wait == 2000 else 'queued')
    hint = _hint(lane) if why == 'blocked' else None
    obs.transition(key, (why, hint), 'collect', 'permit', granted=False, kind=kind, why=why, hint=hint, wait_ms=wait,
                   job_id=b.get('job_id'))


def _page(t, q, b, out):
    users = b.get('users')
    outcome = ('duplicate' if out.get('duplicate') else 'stale' if out.get('stale') else
               'stalled' if out.get('stalled') else 'trial_stopped' if out.get('trial_stopped') else 'ok')
    obs.event('collect', 'page', 'info' if outcome in ('ok', 'duplicate') else 'warn', outcome=outcome,
              job_id=b.get('job_id'), seed=_text(b.get('seed')), direction=b.get('direction'),
              returned=len(users) if isinstance(users, list) else None, requested=b.get('requested_count'),
              http_status=b.get('http_status'), has_more=b.get('has_more'), done=b.get('done'),
              limited=b.get('limited') or None, cursor_in=_fp(b.get('requested_cursor')),
              cursor_out=_fp(b.get('next_cursor')), received=out.get('received'), total=b.get('total'))


def _bio(t, q, b, out):
    profile = b.get('profile')
    obs.event('collect', 'bio', 'warn' if out.get('stale') else 'info', job_id=b.get('job_id'), route=b.get('route'),
              handle=_text(profile.get('handle')) if isinstance(profile, dict) else None,
              saved=bool(out.get('id')), stale=out.get('stale') or None)


def _error(t, q, b, out):
    code, reason = b.get('code'), b.get('reason')
    adverse = code in _ADVERSE or reason in ('scraping_warning', 'list_html_home_redirect')
    severe = reason == 'scraping_warning' or code in ('login', 'challenge')
    head = str(b.get('message') or '').split(' | ', 1)[0]
    obs.event('ig', 'adverse' if adverse else 'error', 'error' if severe else 'warn' if adverse or code == 'other' else 'info',
              code=code, reason=reason, http_status=b.get('http_status'), kind=b.get('kind'), direction=b.get('direction'),
              job_id=b.get('job_id'), route=b.get('route'), retry_after=b.get('retry_after'), retry_at=b.get('retry_at'),
              message=head, stale=out.get('stale') or None)


def _state_facts(lane, b, out):
    """Merge what a heartbeat or lease response says about a lane's state into memory; returns its signature."""
    if len(_LANES) > 256 and lane not in _LANES:   # a local client inventing lane ids must not grow memory
        _LANES.clear()
        _LANE_HANDLE.clear()
    row = _LANES.setdefault(lane, {})
    cool = b.get('cool') if isinstance(b.get('cool'), dict) else {}
    stages = out.get('stages') if isinstance(out.get('stages'), dict) else row.get('stages')
    row.update(paused=out.get('paused', row.get('paused')), cooldown_until=out.get('cooldown_until'),
               stages=stages, upgrade=bool(out.get('upgrade_required')))
    if 'hold' in b:
        row['hold'] = b.get('hold')
    if 'state' in b:
        row['state'] = _text(b.get('state'), 20)
    if 'tab' in b:
        row['tab'] = _text(b.get('tab'), 24)
    if _text(b.get('version'), 40):
        row['version'] = b['version']
    if cool or 'cool' in b:
        row['cool'] = (cool.get('list'), cool.get('profile'))
    return (row.get('state'), row.get('tab'), row.get('hold'), row.get('paused'), row.get('cooldown_until'),
            row.get('cool'), tuple(sorted((stages or {}).items())), row.get('upgrade'))


def _heartbeat(t, q, b, out):
    lane = t.lane
    if lane is None:
        return
    now = time.monotonic()
    row = _LANES.get(lane) or {}
    gap = now - row['beat'] if row.get('beat') else None
    if gap is not None and gap > HEARTBEAT_GAP_S:
        obs.warn('ext', 'heartbeat_gap', gap_s=round(gap), version=row.get('version'))
    if lane in _denied:
        _denied.discard(lane)
        obs.info('ext', 'heartbeat_back', silent_s=round(gap) if gap is not None else None)
    signature = _state_facts(lane, b, out)
    _LANES[lane]['beat'] = now
    today = b.get('today') if isinstance(b.get('today'), dict) else {}
    obs.transition(('lane', lane), signature, 'collect', 'lane_state', state=_LANES[lane].get('state'),
                   tab=_LANES[lane].get('tab'), hold=_LANES[lane].get('hold'), paused=out.get('paused'),
                   cooldown_until=out.get('cooldown_until'), cool=_LANES[lane].get('cool'),
                   stages=out.get('stages'), upgrade_required=out.get('upgrade_required') or None,
                   version=_LANES[lane].get('version'), today_list=today.get('list'), today_profile=today.get('profile'))


def _next(t, q, b, out):
    lane = t.lane
    if lane is None:
        return
    signature = _state_facts(lane, {}, out)
    job = out.get('job')
    if isinstance(job, dict):
        obs.transition_end(('lease_none', lane))
        obs.info('collect', 'lease', kind=job.get('kind'), job_id=job.get('id'),
                 seed=_text(job.get('seed')) or _text(job.get('handle')), direction=job.get('direction'),
                 has_cursor=bool(job.get('cursor')), received=job.get('received'), page_size=job.get('page_size'))
    else:
        obs.transition(('lease_none', lane), signature, 'collect', 'lease_none', paused=out.get('paused'),
                       cooldown_until=out.get('cooldown_until'), stages=out.get('stages'),
                       upgrade_required=out.get('upgrade_required') or None, hint=_hint(lane))


def _control(t, q, b, out):
    obs.info('collect', 'control', action=_text(b.get('action')), stage=_text(b.get('stage')),
             target=_text(b.get('account')) if isinstance(b.get('account'), str) else None,
             route=t.route)


_OBSERVERS = {
    ('POST', '/api/ext/request'): _permit,
    ('POST', '/api/ext/list-page'): _page,
    ('POST', '/api/ext/profile'): _bio,
    ('POST', '/api/ext/error'): _error,
    ('POST', '/api/ext/heartbeat'): _heartbeat,
    ('GET', '/api/ext/next'): _next,
    ('POST', '/api/control'): _control,
    ('POST', '/api/ext/control'): _control,
}


def lane_gauge():
    """For the heartbeat: seconds since each extension lane last reported. Flags lanes that went silent."""
    now = time.monotonic()
    ages = {}
    for lane, row in list(_LANES.items()):
        if not row.get('beat'):
            continue
        age = now - row['beat']
        ages[(_LANE_HANDLE.get(lane) or lane)[:40]] = round(age)
        if age > SILENT_S and lane not in _denied:
            _denied.add(lane)
            obs.warn('ext', 'heartbeat_silent', lane=lane, account=_LANE_HANDLE.get(lane), age_s=round(age),
                     last_state=row.get('state'), version=row.get('version'))
    return ages


# ---------------------------------------------------------------- state changes at their source
WATCHED_SETTINGS = frozenset((
    'paused', 'paused_lists', 'paused_bios', 'cooldown', 'instagram_scraping_warning',
    'instagram_collection_isolation', 'followers_route_until', 'instagram_request_attention', 'budget',
    'follower_lists', 'follower_redirect_recovery', 'qualify', 'qualify_auto', 'processing_mode',
    'mobile_backend_enabled'))
_ALARMING = frozenset(('cooldown', 'instagram_scraping_warning', 'instagram_request_attention'))
_last_setting = {}


def _summary(value):
    """A short, scalar view of a setting value: dicts keep scalar leaves and name their nested keys."""
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:10]:
            out[str(key)] = (sorted(map(str, item))[:8] if isinstance(item, (dict, list)) and not isinstance(item, str) else item)
        return out
    return value


def setting_changed(key, value):
    """Hook for storage.settings.set_setting: record watched control settings when their value really changes."""
    if not obs.S.on or key not in WATCHED_SETTINGS:
        return
    try:
        summary = _summary(value)
        previous = _last_setting.get(key)
        if key in _last_setting and previous == summary:
            return
        _last_setting[key] = summary
        obs.event('state', 'setting', 'warn' if key in _ALARMING and value else 'info', key=key, value=summary,
                  prev=previous)
    except Exception:
        obs.S.internal_errors += 1


def baseline(conn):
    """At start-up: record the control settings as they are, so the log says what state the run began in."""
    if not obs.S.on:
        return
    try:
        marks = ','.join('?' * len(WATCHED_SETTINGS))
        values = {key: _summary(json.loads(raw)) for key, raw in conn.execute(
            'SELECT key, value FROM settings WHERE key IN (%s)' % marks, tuple(sorted(WATCHED_SETTINGS)))}
        _last_setting.update(values)
        obs.info('state', 'baseline', **values)
    except Exception:
        obs.S.internal_errors += 1


def settings_gauge():
    """For the heartbeat: the control settings that are on right now (paused, cooling, warned), so the newest
    heartbeat always says what state collection is in, whatever the log window."""
    return {key: value for key, value in _last_setting.items() if value not in (None, False, 0, '', {}, [])
            and key not in ('budget', 'processing_mode', 'qualify', 'qualify_auto', 'follower_lists')}


# ---------------------------------------------------------------- extension log intake
def api_ext_log(conn, q, b):
    """POST /api/ext-log: a batch of the extension's own events, tagged by lane and appended to the server log."""
    events = b.get('events')
    if not isinstance(events, list):
        raise ValueError('events must be a list')
    lane = _text(b.get('lane_id')) or _text(_first(q, 'lane'))
    account = b.get('account')
    handle = (_text(account.get('handle')) if isinstance(account, dict) else None) or _LANE_HANDLE.get(lane)
    if lane and handle:
        _remember_handle(lane, handle)
    accepted = 0
    if obs.S.on:
        now = time.time()
        for item in events[:200]:
            if not isinstance(item, dict) or not _EVENT_NAME.match(str(item.get('ev', ''))):
                continue
            stamp = item.get('t')
            stamp = stamp / 1000 if isinstance(stamp, (int, float)) and abs(stamp / 1000 - now) < 7 * 86400 else now
            fields = {k: v for k, v in item.items()
                      if k not in ('ev', 'lv', 't') and _FIELD_NAME.match(str(k)) and isinstance(v, (str, int, float, bool))}
            obs.event_at(stamp, 'ext', item['ev'], item.get('lv') if item.get('lv') in obs.LEVELS else 'info',
                         lane=lane, account=handle, via='ext-log', lag_s=round(max(0, now - stamp), 1),
                         ext_v=_text(b.get('v'), 20),
                         **dict(list(fields.items())[:14]))
            accepted += 1
        dropped = b.get('dropped')
        if isinstance(dropped, int) and dropped > 0:
            obs.warn('ext', 'log_dropped', lane=lane, account=handle, n=dropped)
    return {'accepted': accepted}


def routes():
    return [('POST', '/api/ext-log', api_ext_log)]


# ---------------------------------------------------------------- outbound calls (models and local services)
def traced_call(provider, op, describe=None, outcome=None):
    """Decorator: one `llm.call` event per call with duration and result class, never any content.

    describe(args, kwargs) -> safe fields known before the call; outcome(args, kwargs, result) -> safe fields after it.
    """
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not obs.S.on:
                return fn(*args, **kwargs)
            started = time.perf_counter()
            fields, status, code, result = {}, 'ok', None, None
            try:
                fields = describe(args, kwargs) if describe else {}
            except Exception:
                fields = {}
            try:
                result = fn(*args, **kwargs)
                return result
            except BaseException as exc:
                status, code = type(exc).__name__, getattr(exc, 'code', None)
                raise
            finally:
                extra = {}
                try:
                    extra = outcome(args, kwargs, result) if outcome and status == 'ok' else {}
                except Exception:
                    extra = {}
                obs.event('llm', 'call', 'info' if status == 'ok' else 'warn', provider=provider, op=op, status=status,
                          http=code if isinstance(code, int) else None,
                          dur_ms=round((time.perf_counter() - started) * 1000), **fields, **extra)
        wrapper.__wrapped__ = fn
        return wrapper
    return decorate


def _arg(args, kwargs, index, name):
    return kwargs[name] if name in kwargs else args[index] if len(args) > index else None


def openrouter_fields(args, kwargs):
    messages = _arg(args, kwargs, 3, 'messages') or []
    return {'model': _arg(args, kwargs, 2, 'model'), 'max_tokens': _arg(args, kwargs, 5, 'max_tokens'),
            'prompt_chars': sum(len(str(m.get('content', ''))) for m in messages if isinstance(m, dict))}


def openrouter_outcome(args, kwargs, result):
    usage = _arg(args, kwargs, 7, 'usage_out')
    fields = {'reply_chars': len(result) if isinstance(result, str) else None}
    if isinstance(usage, dict):
        fields.update(tokens_in=usage.get('input_tokens'), tokens_out=usage.get('output_tokens'))
    return fields
