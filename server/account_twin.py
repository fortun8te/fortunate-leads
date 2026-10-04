"""Shared-account twin: two Chrome profiles signed in to ONE Instagram account, as a controlled experiment.

Default off. Nothing here runs unless the `account_twin` setting exists, and that setting is only
written by an explicit operator action. Normal behaviour stays: one active Chrome profile per Instagram
identity (accounts.identity_owner), a duplicate profile gets no work.

Why it is shaped like this (evidence in docs/SECOND_PROFILE_TEST.md):
  - Cooldowns, daily budgets, warning holds and the 429 circuit are already keyed by Instagram ID in
    accounts.py, so one profile's limit stops its twin. This module adds the missing pieces.
  - A second profile doubles the account's request rate unless something caps it. The cap lives on the
    server, in the same request permit every Instagram request already needs, so it works with the
    unmodified extension.
  - The first adverse event ends the experiment and stops collection. Nothing resumes by itself.

States (stored in settings.account_twin):
  baseline       one profile, measuring requests per hour (twin NOT active)
  baseline_done  baseline accepted, twin can be enabled
  twin           both profiles work; combined permits are capped
  finished       the operator ended it (second profile paused)
  tripped        an adverse event ended it (collection paused, second profile paused)
Only @trashtheturtles can ever be selected; the main account and the warned account are refused in code.
"""
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import accounts
import db

SETTING = 'account_twin'
LOG = 'account_twin_log'
VERSION = 1

TWIN_IG_ID = '64189916997'           # @trashtheturtles, the only healthy collector
TWIN_HANDLE = 'trashtheturtles'
BLOCKED_IG_IDS = frozenset({'55966675376', '25764895364'})   # @fortun8te (main), @dihfluencer (warned)
BLOCKED_HANDLES = frozenset({'fortun8te', 'dihfluencer'})
MIN_VERSION = (3, 9, 30)

CORE_JS = Path(__file__).resolve().parent.parent / 'extension' / 'lib' / 'core.js'
_PACING = {}


def _js_number(text):
    return float(text.strip())


def pacing():
    """The extension's own pacing (lib/core.js PACE), read from that file and never copied here.

    This is the one source of how fast ONE profile may go. The twin has no speed number of its own: its
    ceiling is what one profile could do under this pacing, so changing the pace in the extension (the speed
    control) changes the twin's ceiling too. When the speed control moves PACE into extension/lib/pace-limits.js,
    repoint this one function (tests/test_account_twin.py checks it against the extension).
    """
    stamp = CORE_JS.stat().st_mtime_ns
    if _PACING.get('stamp') != stamp:
        text = CORE_JS.read_text(encoding='utf-8')
        block = re.search(r'const PACE = \{(.*?)\n  \};', text, re.S)
        if not block:
            raise ValueError('The extension pacing could not be read, so the twin cannot be capped.')
        found = {}
        for key in ('listGap', 'breakEvery', 'breakLen', 'profileGap', 'spacing'):
            m = re.search(key + r':\s*\[([^\]]+)\]', block.group(1))
            found[key] = [_js_number(x) for x in m.group(1).split(',')] if m else None
        win = re.search(r'window:\s*(\d+)\s*\*\s*MIN', block.group(1))
        top = re.search(r'windowMax:\s*(\d+)', block.group(1))
        if not win or not top or any(v is None or len(v) != 2 for v in found.values()):
            raise ValueError('The extension pacing could not be read, so the twin cannot be capped.')
        found.update(window=int(win.group(1)) * 60, windowMax=int(top.group(1)))
        _PACING.clear()
        _PACING.update(found, stamp=stamp)
    return {k: v for k, v in _PACING.items() if k != 'stamp'}


def _mean(pair):
    return sum(pair) / 2


def single_profile_ceiling(conn):
    """Requests per hour ONE profile produces at the pace in force, never more than its 11-minute window allows.

    Lists only when bios are off for the run (the usual @trashtheturtles run); lists plus bios otherwise."""
    p = pacing()
    spacing = _mean(p['spacing']) / 1000
    list_cycle = max(_mean(p['listGap']) / 1000, spacing) + (_mean(p['breakLen']) / 1000) / _mean(p['breakEvery'])
    rate = 3600 / list_cycle
    isolation = db.get_setting(conn, 'instagram_collection_isolation') or {}
    bios = ('bios' in isolation.get('collection_stages', ('lists', 'bios')) and not db.get_setting(conn, 'paused')
            and not db.get_setting(conn, 'paused_bios'))
    if bios:
        rate += 3600 / max(_mean(p['profileGap']) / 1000, spacing)
    return int(min(rate, p['windowMax'] * 3600 / p['window']))


GROWTH = 1.25
STEP_MINUTES = 60
BASELINE_MINUTES = 60
BASELINE_MIN_REQUESTS = 60
BASELINE_MAX_AGE = timedelta(hours=24)
BASELINE_MIN_ACTIVE = 0.8            # share of 5-minute slots with at least one request
EXERCISED = 0.8                      # a step counts only if it reached this share of its cap
OTHER_ERRORS_MAX = 3                 # unexplained errors in one step before the experiment stops
BURST = 2                            # grants allowed back to back before spacing applies
ADVERSE = frozenset({'rate_limit', 'soft_block', 'login', 'challenge'})
PROPAGATE = ('cooldown_until', 'list_cool_until', 'profile_cool_until', 'list_endpoint_until', 'hold')
LIVE_STATES = ('baseline', 'baseline_done', 'twin')

ABORT_RULES = (
    'Any rate limit, soft block, login wall, security check or scraping warning on the account.',
    'Any Instagram home-page redirect on a list (the server treats it as a shared safety hold).',
    'Any new cooldown, hold or warning that appears on either profile.',
    'An Instagram request that never confirmed completion.',
    f'{OTHER_ERRORS_MAX} unexplained errors in one step.',
    'More combined requests in an hour than the step cap (a bug, stops the test).',
)


def utc_now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- state

def config(conn):
    """The saved experiment, or None. A damaged value fails closed: no twin."""
    try:
        value = db.get_setting(conn, SETTING)
    except ValueError:
        return None
    return value if isinstance(value, dict) and value.get('v') == VERSION else None


def save(conn, cfg):
    db.set_setting(conn, SETTING, cfg)


def is_member(conn, row):
    """True when this lane may work concurrently with its twin right now."""
    if row is None or not row['ig_id']:
        return False
    cfg = config(conn)
    return bool(cfg and cfg.get('state') == 'twin' and row['ig_id'] == cfg.get('ig_id')
                and row['lane_id'] in cfg.get('lanes', ()))


def peer_lanes(conn, row):
    """The other twin lane(s) while the twin is active, else an empty list."""
    if not is_member(conn, row):
        return []
    return [lane for lane in config(conn)['lanes'] if lane != row['lane_id']]


def _row(conn, lane):
    return conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()


def _ver(value):
    return tuple(int(v) for v in str(value or '0').split('.') if v.isdigit())


def _later_of(a, b):
    live = [v for v in (a, b) if v]
    return max(live, key=accounts.utc) if live else None


# ---------------------------------------------------------------- eligibility

def lane_blockers(conn, row, now):
    """Why this lane can never or cannot yet be part of the twin. Empty list = fine."""
    if row is None:
        return [('missing_lane', 'That Chrome profile has not connected yet.')]
    out = []
    handle = (row['handle'] or '').lower()
    if row['ig_id'] in BLOCKED_IG_IDS or handle in BLOCKED_HANDLES or row['is_main']:
        return [('account_excluded', 'The main account and the warned account can never be used for this test.')]
    if row['ig_id'] != TWIN_IG_ID:
        return [('account_not_allowed', f'Only @{TWIN_HANDLE} can be used for this test.')]
    if not handle:
        return [('handle_unknown', 'This profile has not reported its Instagram handle yet.')]
    if handle != TWIN_HANDLE:
        return [('account_not_allowed', f'Only @{TWIN_HANDLE} can be used for this test.')]
    if accounts.collection_protected(conn, row):
        out.append(('main_protected', 'This account is protected as a personal account.'))
    if accounts.warning_for(conn, row):
        out.append(('warning', 'Instagram showed a scraping warning for this account. Review it first.'))
    if row['hold']:
        out.append(('hold', 'This account needs a login or security check first.'))
    if row['collection_backend'] != 'chrome':
        out.append(('backend', 'This profile does not collect through Chrome.'))
    if _ver(row['version']) < MIN_VERSION:
        out.append(('old_extension', 'Reload the extension in this Chrome profile (3.9.30 or newer).'))
    if not row['last_seen'] or now - accounts.utc(row['last_seen']) > accounts.ONLINE_FOR:
        out.append(('offline', 'This Chrome profile is not online.'))
    if row['state'] == 'network_wait':
        out.append(('connection_error', 'This profile has an Instagram connection problem.'))
    if any(accounts.later(row[k], now) for k in accounts.IDENTITY_WAITS) \
            or any(accounts.identity_cooling(conn, row, kind, now) for kind in ('list', 'profile')):
        out.append(('cooling', 'Instagram asked this account to slow down. Wait for the pause to end.'))
    budget = accounts.budget_of(conn, row)
    if budget.get('list', 0) <= 0 or budget.get('profile', 0) <= 0:
        out.append(('budget_unbounded', 'Set finite daily limits for this account first.'))
    return out


def preflight(conn, now=None, primary=None, twin=None, need_twin=True):
    """Everything that must be true before the next experiment step. {'ok', 'blockers', 'lanes'}."""
    now = now or utc_now()
    blockers = []

    def block(code, text, lane=None):
        blockers.append({'code': code, 'text': text, **({'lane_id': lane} if lane else {})})

    rows = conn.execute('SELECT * FROM accounts WHERE ig_id=? ORDER BY first_seen, lane_id', (TWIN_IG_ID,)).fetchall()
    ids = [r['lane_id'] for r in rows]
    # A lane that belongs to another Instagram account is refused by name, before anything else.
    for lane in (primary, twin):
        row = _row(conn, lane) if lane else None
        if row is not None and row['ig_id'] != TWIN_IG_ID:
            for code, text in lane_blockers(conn, row, now):
                block(code, text, lane)
            return {'ok': False, 'blockers': blockers, 'lanes': []}
    for lane in (primary, twin):
        if lane and lane not in ids:
            block('missing_lane', 'That Chrome profile has not connected yet.', lane)
    primary = primary or (ids[0] if ids else None)
    if not primary:
        block('missing_lane', f'Open @{TWIN_HANDLE} in its Chrome profile first.')
    if len(ids) > 2:
        block('extra_profile', f'@{TWIN_HANDLE} is signed in on more than two Chrome profiles.')
    if need_twin:
        twin = twin or next((i for i in ids if i != primary), None)
        if len(ids) != 2 or not twin or twin == primary:
            block('need_two_profiles', f'Sign in to @{TWIN_HANDLE} in exactly one extra Chrome profile.')
    lanes = [lane for lane in ([primary] + ([twin] if need_twin else [])) if lane and lane in ids]
    for lane in lanes:
        for code, text in lane_blockers(conn, _row(conn, lane), now):
            block(code, text, lane)
    if need_twin and len(lanes) == 2:
        limits = [accounts.budget_of(conn, _row(conn, lane)) for lane in lanes]
        if limits[0] != limits[1]:
            block('budget_mismatch', 'Give both profiles the same daily limits. The account shares the stricter one.')
    cooling = db.get_setting(conn, 'cooldown')
    if cooling and accounts.later(cooling, now):
        block('shared_hold', 'Instagram collection is on a protective pause.')
    active = (db.get_setting(conn, 'instagram_request_gate') or {}).get('active') or {}
    if need_twin and isinstance(active.get('until'), (int, float)) and active['until'] > now.timestamp():
        block('request_active', 'Wait for the current Instagram request to finish.')
    if db.get_setting(conn, 'instagram_request_attention'):
        block('request_unconfirmed', 'An Instagram request did not confirm completion. Check the account tab first.')
    return {'ok': not blockers, 'blockers': blockers, 'lanes': lanes}


# ---------------------------------------------------------------- counters, caps and the request permit hooks

def _acc(now):
    return {'started_at': accounts.iso(now), 'requests': 0, 'by_lane': {}, 'by_kind': {}, 'slots': [], 'other_errors': 0}


def _current(cfg):
    return cfg.get('step') if cfg.get('state') == 'twin' else cfg.get('baseline') if cfg.get('state') == 'baseline' else None


def _rows_of(conn, cfg):
    return [r['lane_id'] for r in conn.execute('SELECT lane_id FROM accounts WHERE ig_id=?', (cfg['ig_id'],))]


def capacity_wait(conn, lane, kind, stamp):
    """Milliseconds a twin lane must wait for the combined cap, 0 when it may ask now. Off = always 0."""
    cfg = config(conn)
    if not cfg or cfg.get('state') != 'twin' or lane not in cfg.get('lanes', ()):
        return 0
    cap = cfg['step']['cap_per_hour']
    state = db.get_setting(conn, LOG) or {}
    log = [e for e in state.get('log', []) if e[0] > stamp - 3600]
    interval = 3600 / cap
    waits = []
    tat = state.get('tat', 0)
    if tat - BURST * interval > stamp:
        waits.append(tat - BURST * interval - stamp)
    if len(log) >= cap:
        waits.append(log[-cap][0] + 3600 - stamp)
    window, window_max = pacing()['window'], pacing()['windowMax']
    recent = [e for e in log if e[0] > stamp - window]
    if len(recent) >= window_max:
        waits.append(recent[-window_max][0] + window - stamp)
    return max(1000, min(15000, math.ceil(max(waits) * 1000))) if waits else 0   # the extension re-asks within 15 s


def note_grant(conn, lane, kind, stamp):
    """Count one granted Instagram request for the account. No-op unless an experiment is live."""
    cfg = config(conn)
    if not cfg or cfg.get('state') not in ('baseline', 'twin') or lane not in cfg.get('lanes', ()):
        return
    twin = cfg['state'] == 'twin'
    state = db.get_setting(conn, LOG) or {}
    log = [e for e in state.get('log', []) if e[0] > stamp - 3600] + [[stamp, lane, kind]]
    tat = max(state.get('tat', 0), stamp) + 3600 / cfg['step']['cap_per_hour'] if twin else 0
    db.set_setting(conn, LOG, {'log': log, 'tat': tat})
    acc = _current(cfg)
    acc['requests'] += 1
    acc['by_lane'][lane] = acc['by_lane'].get(lane, 0) + 1
    acc['by_kind'][kind] = acc['by_kind'].get(kind, 0) + 1
    slot = int(stamp // 300)
    if not acc['slots'] or acc['slots'][-1] != slot:
        acc['slots'] = (acc['slots'] + [slot])[-600:]
    acc['last_at'] = accounts.iso(datetime.fromtimestamp(stamp, timezone.utc))
    if twin and len(log) > cfg['step']['cap_per_hour']:
        save(conn, cfg)
        trip(conn, lane, 'cap_exceeded', 'More requests than the step cap in one hour.', datetime.fromtimestamp(stamp, timezone.utc))
        return
    save(conn, cfg)


# ---------------------------------------------------------------- adverse events

def _account_lane(conn, cfg, lane):
    row = _row(conn, lane)
    return bool(row is not None and row['ig_id'] == cfg.get('ig_id'))


def _spread(conn, cfg, lane, fields):
    """One profile's wait or hold applies to its twin at once; the peer keeps the longer of the two."""
    for peer in (p for p in cfg.get('lanes', ()) if p != lane):
        row = _row(conn, peer)
        if row is None:
            continue
        sets = {}
        for key in PROPAGATE:
            value = fields.get(key)
            if not value:
                continue
            sets[key] = value if key == 'hold' else _later_of(row[key], value)
        if sets:
            conn.execute(f"UPDATE accounts SET {', '.join(k + '=?' for k in sets)} WHERE lane_id=?", (*sets.values(), peer))
        if fields.get('hold') in accounts.HOLDS:
            accounts.release_all(conn, peer)


def trip(conn, lane, code, reason, now=None):
    """End the experiment at the first adverse event. Collection stays paused until the operator resumes it."""
    now = now or utc_now()
    cfg = config(conn)
    if not cfg or cfg.get('state') not in LIVE_STATES:
        return False
    was = cfg['state']
    acc = _current(cfg) or {}
    started = cfg.get('enabled_at') or (cfg.get('baseline') or {}).get('started_at')
    cfg.update(state='tripped', tripped_from=was, trip={
        'at': accounts.iso(now), 'lane': lane, 'code': str(code)[:40], 'reason': str(reason or '')[:200],
        'after_minutes': round((now - accounts.utc(started)).total_seconds() / 60, 1) if started else None,
        'requests_in_step': acc.get('requests')})
    if was == 'twin':
        for peer in cfg['lanes']:
            if peer != cfg['primary']:
                conn.execute('UPDATE accounts SET paused=1 WHERE lane_id=?', (peer,))
        db.set_setting(conn, 'paused_lists', True)
        db.set_setting(conn, 'paused_bios', True)
    save(conn, cfg)
    return True


def after_error(conn, lane, code, reason, home_redirect, fields, now=None):
    """Called from /api/ext/error for every reported error. Account-level, whichever profile reported it."""
    cfg = config(conn)
    if not cfg or cfg.get('state') not in LIVE_STATES or not _account_lane(conn, cfg, lane):
        return
    now = now or utc_now()
    red = code in ADVERSE or bool(home_redirect) or reason == 'scraping_warning'
    if cfg['state'] == 'twin':
        _spread(conn, cfg, lane, fields)
    if red:
        trip(conn, lane, code or 'other', reason or code, now)
    elif code == 'other':
        acc = _current(cfg)
        if acc is not None:
            acc['other_errors'] = acc.get('other_errors', 0) + 1
            save(conn, cfg)
            if acc['other_errors'] >= OTHER_ERRORS_MAX:
                trip(conn, lane, 'other', f'{OTHER_ERRORS_MAX} unexplained errors in one step.', now)


def after_heartbeat(conn, row, now=None):
    """Called from /api/ext/heartbeat. A new hold, wait or warning on the account ends the experiment."""
    cfg = config(conn)
    if not cfg or cfg.get('state') not in LIVE_STATES or row is None or row['ig_id'] != cfg.get('ig_id'):
        return
    now = now or utc_now()
    why = None
    if row['hold']:
        why = ('hold', 'A login or security check appeared.')
    elif accounts.warning_for(conn, row):
        why = ('scraping_warning', 'Instagram showed a scraping warning.')
    elif any(accounts.later(row[k], now) for k in ('cooldown_until',) + accounts.IDENTITY_WAITS):
        why = ('cooldown', 'Instagram asked the account to slow down.')
    else:
        attention = db.get_setting(conn, 'instagram_request_attention')
        if isinstance(attention, dict) and attention.get('lane') in cfg.get('lanes', ()):
            why = ('request_unconfirmed', 'An Instagram request did not confirm completion.')
    if why:
        if cfg['state'] == 'twin':
            _spread(conn, cfg, row['lane_id'], {k: row[k] for k in PROPAGATE})
        trip(conn, row['lane_id'], why[0], why[1], now)


# ---------------------------------------------------------------- metrics

def _metrics(conn, cfg, acc, now, end=None):
    started = accounts.utc(acc['started_at'])
    stop = accounts.utc(end) if end else now
    hours = max((stop - started).total_seconds() / 3600, 1 / 3600)
    lanes = _rows_of(conn, cfg)
    marks = ','.join('?' * len(lanes)) or "''"
    args = (accounts.iso(started), accounts.iso(stop), *lanes)
    pages, people = conn.execute(
        f'SELECT count(*), coalesce(sum(users),0) FROM pages WHERE at>=? AND at<=? AND lane IN ({marks})', args).fetchone()
    bios = conn.execute(
        f"SELECT count(*) FROM collector_events WHERE at>=? AND at<=? AND lane IN ({marks}) "
        "AND kind='profile' AND outcome='profile' AND reason='saved'", args).fetchone()[0]
    slots = int(((stop - started).total_seconds()) // 300) + 1
    return {'started_at': acc['started_at'], 'minutes': round(hours * 60, 1), 'seconds': (stop - started).total_seconds(),
            'requests': acc['requests'],
            'requests_per_hour': round(acc['requests'] / hours, 1), 'by_lane': dict(acc['by_lane']),
            'by_kind': dict(acc['by_kind']), 'list_pages': pages, 'list_pages_per_hour': round(pages / hours, 1),
            'people': people, 'bios': bios, 'active_share': round(min(1.0, len(acc['slots']) / slots), 2),
            'other_errors': acc.get('other_errors', 0)}


def status(conn, now=None):
    now = now or utc_now()
    cfg = config(conn)
    out = {'state': cfg['state'] if cfg else 'off', 'enabled': bool(cfg and cfg.get('state') == 'twin'),
           'allowed': {'handle': TWIN_HANDLE, 'ig_id': TWIN_IG_ID},
           'limits': {'ceiling_per_hour': single_profile_ceiling(conn), 'window_requests': pacing()['windowMax'],
                      'window_seconds': pacing()['window'], 'growth': GROWTH, 'step_minutes': STEP_MINUTES,
                      'source': 'extension/lib/core.js PACE (the speed control); the twin has no speed setting of its own'},
           'abort_rules': list(ABORT_RULES)}
    if not cfg:
        out['preflight'] = preflight(conn, now, need_twin=False)
        return out
    out.update(ig_id=cfg['ig_id'], lanes=cfg.get('lanes', []), primary=cfg.get('primary'),
               trip=cfg.get('trip'), history=cfg.get('history', []), baseline=cfg.get('baseline_result'))
    if cfg['state'] == 'baseline':
        out['current'] = _metrics(conn, cfg, cfg['baseline'], now)
    if cfg['state'] == 'twin':
        step = cfg['step']
        m = _metrics(conn, cfg, step, now)
        cap = step['cap_per_hour']
        out['current'] = dict(m, index=step['index'], label=step['label'], cap_per_hour=cap,
                              utilisation=round(m['requests_per_hour'] / cap, 2),
                              minutes_left=max(0, round(step['min_minutes'] - m['minutes'], 1)))
        base = (cfg.get('baseline_result') or {}).get('list_pages_per_hour')
        out['current']['vs_baseline'] = round(m['list_pages_per_hour'] / base, 2) if base else None
    if cfg['state'] in ('baseline_done', 'twin'):
        out['preflight'] = preflight(conn, now, primary=cfg['primary'], need_twin=True)
    return out


# ---------------------------------------------------------------- operator actions

def _need_review(body):
    if body.get('reviewed') is not True:
        raise ValueError('Confirm that you reviewed the plan first.')


def _require(conn, *states):
    cfg = config(conn)
    if not cfg or cfg.get('state') not in states:
        raise ValueError('That step is not available now. Current state: ' + (cfg['state'] if cfg else 'off') + '.')
    return cfg


def _freeze(conn, cfg, acc, now):
    return dict(_metrics(conn, cfg, acc, now), ended_at=accounts.iso(now))


def start_baseline(conn, body, now):
    _need_review(body)
    cfg = config(conn)
    if cfg and cfg.get('state') in LIVE_STATES:
        raise ValueError('An experiment is already running. Finish or reset it first.')
    pf = preflight(conn, now, primary=body.get('lane_id'), need_twin=False)
    if not pf['ok']:
        raise ValueError(pf['blockers'][0]['text'])
    primary = pf['lanes'][0]
    cfg = {'v': VERSION, 'state': 'baseline', 'ig_id': TWIN_IG_ID, 'primary': primary, 'lanes': [primary],
           'history': [], 'baseline': _acc(now)}
    db.set_setting(conn, LOG, {'log': [], 'tat': 0})
    save(conn, cfg)


def finish_baseline(conn, body, now):
    _need_review(body)
    cfg = _require(conn, 'baseline')
    m = _metrics(conn, cfg, cfg['baseline'], now)
    if m['seconds'] < BASELINE_MINUTES * 60:
        raise ValueError(f'Keep collecting for {math.ceil(BASELINE_MINUTES - m["seconds"] / 60)} more minutes.')
    if m['requests'] < BASELINE_MIN_REQUESTS:
        raise ValueError(f'Only {m["requests"]} requests so far. A baseline needs at least {BASELINE_MIN_REQUESTS}.')
    if m['active_share'] < BASELINE_MIN_ACTIVE:
        raise ValueError('Collection was idle too often. A baseline from that hour would be too low. Keep collecting and try again.')
    cfg.update(state='baseline_done', baseline_result=dict(m, ended_at=accounts.iso(now),
                                                          per_hour=max(1, int(m['requests_per_hour']))))
    cfg.pop('baseline', None)
    save(conn, cfg)


def enable(conn, body, now):
    _need_review(body)
    cfg = _require(conn, 'baseline_done')
    lanes = body.get('lane_ids')
    if (not isinstance(lanes, list) or len(lanes) != 2 or lanes[0] != cfg['primary'] or lanes[1] == lanes[0]
            or any(not isinstance(lane, str) for lane in lanes)):
        raise ValueError('Name the baseline profile first, then the second profile.')
    pf = preflight(conn, now, primary=lanes[0], twin=lanes[1], need_twin=True)
    if not pf['ok']:
        raise ValueError(pf['blockers'][0]['text'])
    if now - accounts.utc(cfg['baseline_result']['ended_at']) > BASELINE_MAX_AGE:
        raise ValueError('The baseline is more than a day old. Measure it again.')
    cap = min(cfg['baseline_result']['per_hour'], single_profile_ceiling(conn))
    cfg.update(state='twin', lanes=lanes, enabled_at=accounts.iso(now),
               step={**_acc(now), 'index': 1, 'label': 'twin_same_total', 'cap_per_hour': cap, 'min_minutes': STEP_MINUTES})
    # The request log is kept: the last hour of baseline requests still counts against the account's hour.
    conn.execute('UPDATE accounts SET paused=0 WHERE lane_id=?', (lanes[1],))
    save(conn, cfg)


def advance(conn, body, now):
    _need_review(body)
    cfg = _require(conn, 'twin')
    step = cfg['step']
    m = _metrics(conn, cfg, step, now)
    if m['seconds'] < step['min_minutes'] * 60:
        raise ValueError(f'Run this step for {math.ceil(step["min_minutes"] - m["seconds"] / 60)} more minutes first.')
    if m['requests_per_hour'] < EXERCISED * step['cap_per_hour']:
        raise ValueError('This step did not reach its cap, so it proved nothing yet. Keep it running or end the test.')
    new_cap = max(step['cap_per_hour'] + 1, math.floor(step['cap_per_hour'] * GROWTH))
    ceiling = single_profile_ceiling(conn)
    if new_cap > ceiling:
        raise ValueError(f'The next step ({new_cap} an hour) is more than one profile may do at the current pace ({ceiling}). '
                         'The twin has no speed setting of its own: change the pace with the speed control, or end the test here.')
    cfg['history'].append(dict(_freeze(conn, cfg, step, now), index=step['index'], label=step['label'],
                               cap_per_hour=step['cap_per_hour']))
    cfg['step'] = {**_acc(now), 'index': step['index'] + 1, 'label': f'plus_25_percent_{step["index"]}',
                   'cap_per_hour': new_cap, 'min_minutes': STEP_MINUTES}
    save(conn, cfg)


def disable(conn, body, now):
    """End the experiment on purpose. The second profile is paused; the first keeps its normal settings."""
    _need_review(body)
    cfg = _require(conn, 'baseline', 'baseline_done', 'twin')
    if cfg['state'] == 'twin':
        cfg['history'].append(dict(_freeze(conn, cfg, cfg['step'], now), index=cfg['step']['index'],
                                   label=cfg['step']['label'], cap_per_hour=cfg['step']['cap_per_hour']))
        for lane in cfg['lanes']:
            if lane != cfg['primary']:
                conn.execute('UPDATE accounts SET paused=1 WHERE lane_id=?', (lane,))
        cfg.pop('step', None)
    cfg.update(state='finished', finished_at=accounts.iso(now), reason=str(body.get('reason') or '')[:200] or None)
    cfg.pop('baseline', None)
    save(conn, cfg)


def reset(conn, body, now):
    """Clear a finished or tripped experiment. It never resumes collection."""
    _need_review(body)
    _require(conn, 'finished', 'tripped')
    conn.execute('DELETE FROM settings WHERE key IN (?,?)', (SETTING, LOG))


ACTIONS = {'start_baseline': start_baseline, 'finish_baseline': finish_baseline, 'enable': enable, 'advance': advance,
           'disable': disable, 'reset': reset}


def apply(conn, body, now=None):
    action = body.get('action')
    if action == 'check':
        return status(conn, now)
    if action not in ACTIONS:
        raise ValueError('action must be check, ' + ', '.join(ACTIONS))
    now = now or utc_now()
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    try:
        ACTIONS[action](conn, body, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return status(conn, now)


def sync_pause(conn):
    """After a Start/Resume of the selected account, the twin follows its primary profile's pause state."""
    cfg = config(conn)
    if not cfg or cfg.get('state') != 'twin':
        return
    primary = _row(conn, cfg['primary'])
    if primary is not None and not primary['paused']:
        for lane in cfg['lanes']:
            if lane != cfg['primary']:
                conn.execute('UPDATE accounts SET paused=0 WHERE lane_id=?', (lane,))


# ---------------------------------------------------------------- HTTP

def routes(server):
    def get_status(conn, q, b):
        return status(conn)

    def post_action(conn, q, b):
        try:
            return apply(conn, b)
        except ValueError as exc:
            raise server.Bad(str(exc)) from None

    return [('GET', r'/api/account-twin', get_status), ('POST', r'/api/account-twin', post_action)]
