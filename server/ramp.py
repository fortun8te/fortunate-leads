"""Automatic, safe speed ramp on top of the pace API (docs/RAMP.md, docs/RECOVERY.md).

Off by default. When on, for each opted-in healthy collector (default: handle trashtheturtles), after every `interval`
of clean, continuous collection it raises the observed list pages per hour by `step_factor`, by narrowing the list gap
through pace.post() (so the extension gets it through the normal pace delivery, no reload). It never passes
`ceiling` pages/hour (default 285, the top of what the pacing evidence measured clean) and never leaves the bounds in
extension/lib/pace-limits.js. On any adverse event it freezes or steps back; the existing safety stops always win and
are never touched here. A person is needed for scraping warning, challenge, login and unconfirmed requests.

All time arguments are explicit (`now`), so the whole controller runs on virtual time in tests.

Storage: one setting, `ramp` (survives restarts):
  {v, enabled, ceiling, interval_s, step_factor, auto_resume, accounts: [handle|lane_id], lanes: {lane_id: state}}
"""
import json
import math
import os
import shutil
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import accounts
import db
import pace
import pace_limits as limits

PROVEN_CEILING = 285          # list pages per hour: the top of the clean envelope in docs/PACING_EVIDENCE.md
DEFAULT_INTERVAL_S = 2 * 3600
DEFAULT_STEP = 1.5
MIN_RUNTIME = 0.90            # collection must run this share of a window for it to count
MIN_PAGES = 30                # fewer saved pages than this cannot be measured
OUTAGE_S = 20 * 60            # no saved page for this long = collection was not running
RECOVERY_OUTAGE_S = 30 * 60   # an outage this long means a recovery: resume one level lower
HOLD_AFTER_LIMIT = 2          # clean windows to hold after a 429 or soft block
RISE_RATIO, RISE_ABS = 1.5, 2.0   # response time proxy "materially up"
SLOWER_THAN_BEFORE = 0.90     # a faster pace that yields under 90% of the earlier rate made things worse
EMPTY_RUN = 5                 # this many empty pages in a row is a bad sign
DISK_LOW_BYTES = 2 << 30
REST_AFTER_LIMIT_S = 2 * 3600     # auto-resume waits this long after a 429/soft block/redirect stop
MAX_AUTO_RESUMES_PER_DAY = 2
HISTORY = 40
DEFAULT_ACCOUNTS = ['trashtheturtles']
RATE = ('rate_limit', 'soft_block')
STOP = ('login', 'challenge')
KEY = 'ramp'


def utcnow():
    return datetime.now(timezone.utc)


def iso(d):
    return d.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def parse(s):
    return accounts.utc(s)


# ---------------------------------------------------------------- logging (obs if present, else a JSONL line)

def _data_dir(conn):
    try:
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        return Path(path).parent if path else None
    except Exception:
        return None


def emit(conn, name, level='info', now=None, **fields):
    """One structured event: component 'ramp'. Uses server/obs.py when it is active, else appends a line in the same
    shape to <data>/logs/events.jsonl, so ops/diagnose reads it either way. Never raises."""
    try:
        try:
            import obs
            if obs.active():
                obs.event('ramp', name, level, **fields)
                return
        except ImportError:
            pass
        folder = _data_dir(conn)
        if folder is None:
            return
        when = (now or utcnow()).astimezone(timezone.utc)
        line = {'ts': when.strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % (when.microsecond // 1000), 'level': level,
                'component': 'ramp', 'event': name}
        line.update({k: v for k, v in fields.items() if v is not None})
        logs = folder / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / 'events.jsonl', 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(line, separators=(',', ':'), default=str) + '\n')
    except Exception:
        pass


# ---------------------------------------------------------------- storage

def config(conn):
    raw = db.get_setting(conn, KEY, None)
    raw = raw if isinstance(raw, dict) and raw.get('v') == 1 else {}
    cfg = {'v': 1, 'enabled': bool(raw.get('enabled', False)),
           'ceiling': raw.get('ceiling') if isinstance(raw.get('ceiling'), (int, float)) and raw.get('ceiling') > 0 else PROVEN_CEILING,
           'interval_s': raw.get('interval_s') if isinstance(raw.get('interval_s'), (int, float)) and raw.get('interval_s') >= 60 else DEFAULT_INTERVAL_S,
           'step_factor': raw.get('step_factor') if isinstance(raw.get('step_factor'), (int, float)) and raw.get('step_factor') > 1 else DEFAULT_STEP,
           'auto_resume': bool(raw.get('auto_resume', True)),
           'accounts': [str(a).lstrip('@').lower() for a in raw.get('accounts') or DEFAULT_ACCOUNTS],
           'lanes': raw.get('lanes') if isinstance(raw.get('lanes'), dict) else {}}
    return cfg


def save(conn, cfg):
    db.set_setting(conn, KEY, cfg)
    conn.commit()


# ---------------------------------------------------------------- the pace model (matches pace.model, unrounded)

def _breaks(values):
    return pace._mean(values['break_len']) / pace._mean(values['break_every'])


def model_cycle(values):
    """Seconds per list page from the numbers alone: the longer of gap and spacing, plus the breaks."""
    return max(pace._mean(values['list_gap']), pace._mean(values['spacing'])) + _breaks(values)


def model_rate(values):
    return 3600 / model_cycle(values)


def plan_step(values, observed, factor, ceiling):
    """New pace values aiming at observed*factor pages/h, clipped to `ceiling`. Returns a dict:
    values, target, predicted, at_ceiling, at_bound, overhead_s. `observed` is measured pages/h (None: use the model).
    The extra time Instagram and the app add per page (overhead) is measured, held constant, and only the list gap changes."""
    cycle = model_cycle(values)
    obs = observed if observed and observed > 0 else 3600 / cycle
    overhead = max(0.0, 3600 / obs - cycle)
    target = min(float(ceiling), obs * factor)
    at_ceiling = target >= ceiling - 1e-9 and obs * factor >= ceiling
    gap_mean = 3600 / target - overhead - _breaks(values)
    gap_mean = max(gap_mean, 0.1)
    lo_now, hi_now = values['list_gap']
    ratio = max(hi_now / lo_now, FIELD_SPREAD())
    lo = 2 * gap_mean / (1 + ratio)
    spec = limits.FIELDS['list_gap']

    def build(lo_):
        hi_ = lo_ * ratio
        return limits.clamp_range(spec, [math.ceil(lo_ / spec['step'] - 1e-9) * spec['step'],
                                         math.ceil(hi_ / spec['step'] - 1e-9) * spec['step']])
    gap = build(lo)
    new = dict(values, list_gap=gap)

    def predicted(v):
        return 3600 / (model_cycle(v) + overhead)
    guard = 0
    while predicted(new) > ceiling * 1.005 and guard < 400:     # rounding must never land above the ceiling
        lo += spec['step']
        gap = build(lo)
        new = dict(values, list_gap=gap)
        guard += 1
    pred = predicted(new)
    at_bound = pred < target * 0.97 and gap[0] <= spec['floor'][0] + 1e-9 and not at_ceiling
    return {'values': new, 'target': round(target, 1), 'predicted': round(pred, 1), 'at_ceiling': at_ceiling,
            'at_bound': at_bound, 'overhead_s': round(overhead, 2), 'observed': round(obs, 1)}


def FIELD_SPREAD():
    return limits.FIELDS['list_gap']['min_spread']


# ---------------------------------------------------------------- evidence

def events_between(conn, lane, t0, t1, kinds=None):
    rows = conn.execute('SELECT at,kind,direction,outcome,reason,http_status,returned_count,job_id FROM collector_events '
                        'WHERE lane=? AND at>=? AND at<=? ORDER BY at, id',
                        (lane, iso(t0 - timedelta(minutes=1))[:19], iso(t1 + timedelta(minutes=1))[:19] + 'z')).fetchall()
    out = []
    for r in rows:
        try:
            at = parse(r['at'])
        except (TypeError, ValueError):
            continue
        if t0 <= at <= t1:
            out.append(dict(r, t=at))
    return out


def evidence(conn, lane, t0, t1, values):
    """Everything a decision needs about [t0, t1]: pages, runtime share, adverse counts, response-time proxy."""
    evs = events_between(conn, lane, t0, t1)
    pages = [e for e in evs if e['kind'] == 'list' and e['outcome'] == 'page']
    total = max(1.0, (t1 - t0).total_seconds())
    marks = [t0] + [p['t'] for p in pages] + [t1]
    active, outages, longest = 0.0, 0, 0.0
    for a, b in zip(marks, marks[1:]):
        dt = (b - a).total_seconds()
        longest = max(longest, dt)
        if dt <= OUTAGE_S:
            active += dt
        else:
            outages += 1
    gaps = [(b['t'] - a['t']).total_seconds() for a, b in zip(pages, pages[1:])]
    gaps = [g for g in gaps if g <= OUTAGE_S]
    median_gap = statistics.median([g for g in gaps if g <= 4 * statistics.median(gaps)]) if gaps else None
    gap_mean = pace._mean(values['list_gap'])
    overhead = max(0.0, median_gap - gap_mean) if median_gap is not None else None
    empty, run, empty_run = 0, 0, 0
    for p in pages:
        if p['returned_count'] == 0:
            run += 1
            empty_run = max(empty_run, run)
        else:
            run = 0
    return {
        'from': iso(t0), 'to': iso(t1), 'pages': len(pages), 'runtime_share': round(min(1.0, active / total), 3),
        'active_s': round(active), 'outages': outages, 'longest_gap_s': round(longest),
        'pages_per_hour': round(len(pages) / (active / 3600), 1) if active > 0 and pages else None,
        'median_gap_s': round(median_gap, 2) if median_gap is not None else None,
        'overhead_s': round(overhead, 2) if overhead is not None else None,
        'rate_limits': sum(1 for e in evs if e['outcome'] in RATE or e['http_status'] == 429),
        'stops': sum(1 for e in evs if e['outcome'] in STOP),
        'redirects': sum(1 for e in evs if e['reason'] == 'list_html_home_redirect'),
        'empty_run': empty_run,
        'last_page': iso(pages[-1]['t']) if pages else None,
    }


def new_adverse(conn, lane, since, now):
    """Adverse events strictly after `since` (an aware datetime), oldest first."""
    out = []
    for e in events_between(conn, lane, since, now):
        if e['t'] <= since:
            continue
        if e['outcome'] in RATE or e['http_status'] == 429:
            out.append(('rate', e))
        elif e['outcome'] in STOP:
            out.append(('stop', e))
        elif e['reason'] == 'list_html_home_redirect':
            out.append(('redirect', e))
    return out


# ---------------------------------------------------------------- state helpers

def fresh_lane(row, values, now):
    return {'ig_id': row['ig_id'], 'handle': row['handle'], 'mode': 'running', 'level': 0,
            'levels': [{'level': 0, 'values': values, 'predicted': round(model_rate(values), 1), 'at': iso(now)}],
            'window_start': iso(now), 'checked_to': iso(now), 'hold_left': 0, 'expected_id': limits.ident(values),
            'prev_overhead': None, 'pre_step_rate': None, 'just_stepped': False, 'consec_rise': 0, 'rate_events': [],
            'auto_resumes': [], 'resume_due': None, 'last_page_at': None, 'last_tick': iso(now), 'last_window': None,
            'stopped_reason': None, 'since': iso(now), 'history': []}


def note(conn, st, now, action, reason, level='info', **numbers):
    """Record a decision in the lane's history ring and the structured log."""
    entry = dict({'at': iso(now), 'action': action, 'reason': reason, 'level': st['level']}, **numbers)
    st['history'] = (st['history'] + [entry])[-HISTORY:]
    emit(conn, action, level, now=now, lane=st.get('lane_id'), account=st.get('handle'), reason=reason,
         ramp_level=st['level'], mode=st['mode'], **numbers)
    return entry


def apply_values(conn, lane_id, values, st):
    """Set the account's pace through the pace API. Returns (ok, after_values, clamped)."""
    out = pace.post(conn, {}, {'scope': 'account', 'lane_id': lane_id, 'values': values})
    st['expected_id'] = out['after']['id']
    return out


def step_down(conn, lane_id, st, now, why, hold, level='warn', **numbers):
    """Back to the previous level's pace (or stay at level 0). Returns True when the pace changed."""
    if st['level'] <= 0:
        st['hold_left'] = max(st['hold_left'], hold)
        st['window_start'] = iso(now)
        note(conn, st, now, 'step_down_none', f'{why}; already at the starting pace, nothing lower', level, **numbers)
        return False
    st['level'] -= 1
    target = st['levels'][st['level']]
    del st['levels'][st['level'] + 1:]
    out = apply_values(conn, lane_id, target['values'], st)
    st['hold_left'] = max(st['hold_left'], hold)
    st['just_stepped'] = False
    st['pre_step_rate'] = None
    st['window_start'] = iso(now)
    note(conn, st, now, 'step_down', why, level, to_id=out['after']['id'], to_pages_per_hour=target.get('predicted'),
         hold_windows=st['hold_left'], **numbers)
    return True


def restart_window(st, now):
    st['window_start'] = iso(now)


# ---------------------------------------------------------------- one account, one tick

def eligible(conn, row, cfg):
    if row is None or not row['ig_id']:
        return 'not signed in to Instagram yet'
    if accounts.collection_protected(conn, row):
        return 'main account, never automated'
    wanted = (row['handle'] or '').lower()
    if wanted not in cfg['accounts'] and row['lane_id'] not in cfg['accounts']:
        return 'not opted in'
    return None


def stage_pauses(conn):
    return bool(db.get_setting(conn, 'paused')) or bool(db.get_setting(conn, 'paused_lists'))


def blockers_now(conn, row, now):
    """Reasons collection cannot run right now (existing safety stops). Plain words, empty when clear."""
    out = []
    if db.get_setting(conn, 'instagram_scraping_warning'):
        out.append(('stop', 'Instagram showed a scraping warning'))
    if row['hold']:
        out.append(('stop', f"Instagram needs a {'login' if row['hold'] == 'login' else 'security check'}"))
    request = db.get_setting(conn, 'instagram_request_attention')
    if isinstance(request, dict) and request.get('message'):
        out.append(('stop', 'a request has no confirmed end'))
    shared = db.get_setting(conn, 'cooldown')
    try:
        if shared and parse(shared) > now:
            out.append(('wait', 'all collection is resting after an Instagram response'))
    except (TypeError, ValueError):
        pass
    if accounts.later(row['cooldown_until'], now) or accounts.later(row['list_cool_until'], now):
        out.append(('wait', 'the account is resting after an Instagram limit'))
    if row['paused'] or stage_pauses(conn):
        out.append(('pause', 'collection is paused'))
    if not row['last_seen'] or now - accounts.utc(row['last_seen']) > accounts.ONLINE_FOR:
        out.append(('offline', 'the extension is offline'))
    return out


def disk_low(conn):
    try:
        folder = _data_dir(conn)
        return folder is not None and shutil.disk_usage(folder).free < DISK_LOW_BYTES
    except Exception:
        return False


def tick_lane(conn, cfg, row, st, now):
    lane = row['lane_id']
    st['lane_id'], st['handle'] = lane, row['handle']
    values_now = pace.resolve(conn, row)
    # --- 0. someone changed the pace by hand: stand down, never fight a person
    if st['mode'] not in ('manual', 'stopped') and values_now['id'] != st['expected_id']:
        st['mode'] = 'manual'
        st['stopped_reason'] = 'the pace was changed by hand'
        note(conn, st, now, 'stand_down', 'the pace was changed by hand; the ramp stops and leaves it alone until you resume it',
             'warn', running_id=values_now['id'], expected_id=st['expected_id'])
        return
    if st['mode'] in ('manual', 'stopped'):
        return
    blocks = blockers_now(conn, row, now)
    # --- 1. adverse events since the last look (each is handled once)
    since = parse(st['checked_to'])
    fresh = new_adverse(conn, lane, since, now)
    st['checked_to'] = iso(now)
    # --- 1a. warning, challenge, login, unconfirmed request: stop, step back, wait for a person
    stop_now = [b for kind, b in blocks if kind == 'stop']
    if stop_now or any(k == 'stop' for k, _ in fresh):
        why = (stop_now[0] if stop_now else 'Instagram showed a login or security page')
        st['mode'] = 'stopped'
        st['stopped_reason'] = why
        st['resume_due'] = None
        step_down(conn, lane, st, now, f'{why}; the ramp stops until a person resumes it', 0, 'error', kind='stop')
        st['mode'] = 'stopped'
        return
    # --- 1b. 429 or soft block: previous level, hold 2 clean windows, maybe auto-resume
    limits_hit = [e for k, e in fresh if k == 'rate']
    if limits_hit:
        hit_at = limits_hit[-1]['t']
        recent = [t for t in st['rate_events'] if now - parse(t) < timedelta(hours=24)] + [iso(hit_at)]
        st['rate_events'] = recent[-10:]
        if len(recent) >= 2:
            step_down(conn, lane, st, now, f'{len(recent)} Instagram limits in 24 hours; the ramp stops until a person resumes it',
                      0, 'error', kind='rate_limit', http_status=limits_hit[-1]['http_status'])
            st['mode'] = 'stopped'
            st['stopped_reason'] = 'second Instagram limit in a day'
            return
        st['mode'] = 'holding'
        step_down(conn, lane, st, now, 'Instagram answered with a rate limit; back to the previous level and hold',
                  HOLD_AFTER_LIMIT, 'warn', kind=limits_hit[-1]['outcome'], http_status=limits_hit[-1]['http_status'])
        st['resume_due'] = {'code': limits_hit[-1]['outcome'], 'at': iso(hit_at), 'rest_s': REST_AFTER_LIMIT_S}
        return
    redirects = [e for k, e in fresh if k == 'redirect']
    if redirects:
        st['resume_due'] = {'code': 'home_redirect', 'at': iso(redirects[-1]['t']), 'rest_s': REST_AFTER_LIMIT_S}
        note(conn, st, now, 'redirect_seen', 'Instagram sent a followers request to the home page; the server rests collection, '
             'the ramp restarts it after the rest and resumes one level lower', 'warn', count=len(redirects))
    # --- 1c. auto-resume after a protective stop the server put on itself (never after warning/challenge/login)
    maybe_auto_resume(conn, cfg, row, st, now, blocks)
    # --- 1d. a long outage (server down, Mac asleep, Chrome closed, extension offline) is a recovery:
    #         resume one level lower than before the outage, never from the top
    latest = conn.execute("SELECT max(at) FROM collector_events WHERE lane=? AND kind='list' AND outcome='page'", (lane,)).fetchone()[0]
    last_page = parse(latest) if latest else None
    seen = parse(st['last_page_at']) if st['last_page_at'] else None
    tick_gap = (now - parse(st['last_tick'])).total_seconds()
    st['last_tick'] = iso(now)
    if last_page and (seen is None or last_page > seen):
        first_new = events_between(conn, lane, seen or parse(st['since']), now)
        first_new = [e for e in first_new if e['kind'] == 'list' and e['outcome'] == 'page' and (seen is None or e['t'] > seen)]
        if seen is not None and first_new and (first_new[0]['t'] - seen).total_seconds() > RECOVERY_OUTAGE_S:
            gap = round((first_new[0]['t'] - seen).total_seconds() / 60)
            restart_window(st, first_new[0]['t'])
            if st['level'] > 0:
                step_down(conn, lane, st, first_new[0]['t'], f'collection came back after {gap} minutes without a page; '
                          'resuming one level lower', 1, 'warn', kind='outage', gap_min=gap)
            else:
                note(conn, st, now, 'recovered', f'collection came back after {gap} minutes without a page (already at the starting pace)',
                     'info', kind='outage', gap_min=gap)
        st['last_page_at'] = iso(last_page)
    elif tick_gap > RECOVERY_OUTAGE_S:
        note(conn, st, now, 'server_gap', f'the server was away for {round(tick_gap / 60)} minutes; the window restarts', 'warn', gap_min=round(tick_gap / 60))
        restart_window(st, now)
    # --- 2. window evaluation
    start = parse(st['window_start'])
    if (now - start).total_seconds() < cfg['interval_s']:
        return
    evaluate_window(conn, cfg, row, st, now, start, blocks)


def maybe_auto_resume(conn, cfg, row, st, now, blocks):
    due = st.get('resume_due')
    if not due or not cfg['auto_resume']:
        return
    lane = row['lane_id']
    at = parse(due['at'])
    if now - at < timedelta(seconds=due['rest_s']):
        return
    if any(k in ('stop', 'wait') for k, _ in blocks):
        return
    if any(r['hold'] for r in accounts.rows(conn)):
        return
    if not (db.get_setting(conn, 'paused_lists') and db.get_setting(conn, 'paused_bios')):
        st['resume_due'] = None            # somebody already restarted collection; nothing for us to do
        return
    if not row['last_seen'] or now - accounts.utc(row['last_seen']) > accounts.ONLINE_FOR or row['paused']:
        return
    recent = [t for t in st['auto_resumes'] if now - parse(t) < timedelta(hours=24)]
    if len(recent) >= MAX_AUTO_RESUMES_PER_DAY:
        st['resume_due'] = None
        st['mode'] = 'stopped'
        st['stopped_reason'] = 'collection stopped again after automatic restarts; a person needs to restart it'
        note(conn, st, now, 'auto_resume_refused', f'already restarted {len(recent)} times in 24 hours; a person must restart collection', 'error')
        return
    db.set_setting(conn, 'paused_lists', False)
    db.set_setting(conn, 'paused_bios', False)
    conn.commit()
    st['auto_resumes'] = recent + [iso(now)]
    st['resume_due'] = None
    st['window_start'] = iso(now)
    note(conn, st, now, 'auto_resume', f"restarted collection {round((now - at).total_seconds() / 60)} minutes after the {due['code'].replace('_', ' ')}; "
         f"ramp continues at level {st['level']}", 'warn', code=due['code'], rested_min=round((now - at).total_seconds() / 60))


def evaluate_window(conn, cfg, row, st, now, start, blocks):
    lane = row['lane_id']
    values = st['levels'][st['level']]['values']
    ev = evidence(conn, lane, start, now, values)
    nums = {k: ev[k] for k in ('pages', 'runtime_share', 'pages_per_hour', 'median_gap_s', 'overhead_s', 'outages', 'longest_gap_s')}
    st['last_window'] = dict(ev, decided=iso(now))

    def hold(reason, level='info', action='hold'):
        restart_window(st, now)
        note(conn, st, now, action, reason, level, **nums)

    if ev['rate_limits'] or ev['stops']:
        return hold('an Instagram limit or security page is in this window; not stepping', 'warn', 'freeze')
    if ev['runtime_share'] < MIN_RUNTIME:
        return hold(f"collection ran {round(ev['runtime_share'] * 100)}% of the window (needs {round(MIN_RUNTIME * 100)}%); the window starts again")
    if ev['pages'] < MIN_PAGES or not ev['pages_per_hour']:
        return hold(f"only {ev['pages']} pages saved; too few to measure")
    for kind, text in blocks:
        return hold(f'{text}; not stepping', 'warn', 'freeze')
    if disk_low(conn):
        return hold('the disk is nearly full; not stepping until there is space', 'warn', 'freeze')
    if ev['redirects'] >= 2:
        st['consec_rise'] = 0
        if st['level'] > 0:
            return step_down(conn, lane, st, now, f"{ev['redirects']} followers home redirects in the window; back one level", 1, 'warn', **nums)
        return hold(f"{ev['redirects']} followers home redirects in the window; not stepping", 'warn', 'freeze')
    if ev['empty_run'] >= EMPTY_RUN:
        return hold(f"{ev['empty_run']} empty pages in a row; not stepping", 'warn', 'freeze')
    obs = ev['pages_per_hour']
    prev_over, over = st['prev_overhead'], ev['overhead_s']
    # a step that made things worse: slower than before the step, or response time rising twice in a row
    if st['just_stepped'] and st['pre_step_rate'] and obs < st['pre_step_rate'] * SLOWER_THAN_BEFORE:
        return step_down(conn, lane, st, now, f"after the step the pace is {obs:.0f} pages/h against {st['pre_step_rate']:.0f} before it; back one level",
                         HOLD_AFTER_LIMIT, 'warn', before=st['pre_step_rate'], **nums)
    if prev_over is not None and over is not None and over > max(RISE_RATIO * prev_over, prev_over + RISE_ABS):
        st['consec_rise'] += 1
        nums_r = dict(nums, previous_overhead_s=prev_over)
        st['prev_overhead'] = over
        if st['consec_rise'] >= 2 and st['level'] > 0:
            return step_down(conn, lane, st, now, f'response time rose in two windows in a row ({prev_over}s to {over}s extra per page); back one level',
                             HOLD_AFTER_LIMIT, 'warn', **nums_r)
        restart_window(st, now)
        note(conn, st, now, 'hold', f'response time is up ({prev_over}s to {over}s extra per page): early warning, not stepping', 'warn', **nums_r)
        return
    st['consec_rise'] = 0
    st['prev_overhead'] = over
    st['just_stepped'] = False
    if st['hold_left'] > 0:
        st['hold_left'] -= 1
        st['mode'] = 'holding' if st['hold_left'] else 'running'
        return hold(f"clean window after a limit; {st['hold_left']} more before the ramp continues")
    # --- clean: step up, clipped to the ceiling and the bounds
    plan = plan_step(values, obs, cfg['step_factor'], cfg['ceiling'])
    if obs >= cfg['ceiling'] * 0.98 or (plan['at_ceiling'] and plan['predicted'] <= obs * 1.02):
        st['mode'] = 'at_ceiling'
        return hold(f"at the ceiling: {obs:.0f} pages/h against {cfg['ceiling']:.0f}", 'info', 'at_ceiling')
    if plan['predicted'] <= obs * 1.02:
        st['mode'] = 'at_bound'
        return hold(f"at the fastest allowed gap ({values['list_gap'][0]} to {values['list_gap'][1]} s); the bounds file limits any further step", 'info', 'at_bound')
    out = apply_values(conn, lane, plan['values'], st)
    st['level'] += 1
    del st['levels'][st['level']:]
    st['levels'].append({'level': st['level'], 'values': out['after']['values'], 'predicted': plan['predicted'], 'at': iso(now), 'from_observed': obs})
    st['mode'] = 'at_ceiling' if plan['at_ceiling'] else 'running'
    st['pre_step_rate'] = obs
    st['just_stepped'] = True
    restart_window(st, now)
    note(conn, st, now, 'step_up', f"clean window: {obs:.0f} pages/h measured, new target {plan['target']:.0f} (expected {plan['predicted']:.0f})" +
         (f" - the ceiling of {cfg['ceiling']:.0f}" if plan['at_ceiling'] else ''), 'info',
         target=plan['target'], expected=plan['predicted'], to_id=out['after']['id'], list_gap=out['after']['values']['list_gap'],
         overhead_model_s=plan['overhead_s'], clamped=[c['field'] for c in out['clamped']] or None, **nums)


# ---------------------------------------------------------------- the worker hook

def tick(conn, now=None):
    """Run every ~30 s from a server worker. Cheap when off. Returns True when it changed anything."""
    now = now or utcnow()
    cfg = config(conn)
    if not cfg['enabled']:
        return False
    changed = False
    for row in accounts.rows(conn):
        why = eligible(conn, row, cfg)
        st = cfg['lanes'].get(row['lane_id'])
        if why:
            if st and st.get('mode') not in ('stopped',) and (accounts.collection_protected(conn, row) or not row['ig_id']):
                st['mode'] = 'stopped'
                st['stopped_reason'] = why
                note(conn, dict(st, lane_id=row['lane_id'], handle=row['handle']), now, 'stopped', why, 'warn')
                changed = True
            continue
        if st is not None and st.get('ig_id') != row['ig_id']:
            note(conn, dict(st, lane_id=row['lane_id'], handle=row['handle']), now, 'identity_changed',
                 'this Chrome profile now signs in as someone else; the ramp state is discarded', 'warn')
            st = None
        if st is None and (row['hold'] or accounts.warning_for(conn, row)):
            continue                      # never start on a held or warned account
        if st is None:
            base = pace.resolve(conn, row)['values']
            st = cfg['lanes'][row['lane_id']] = fresh_lane(row, base, now)
            st['lane_id'], st['handle'] = row['lane_id'], row['handle']
            note(conn, st, now, 'enrolled', f"ramp watches {accounts.name_of(row)}; starting pace is {model_rate(base):.0f} pages/h by the numbers",
                 'info', start_id=limits.ident(base))
            changed = True
        before = json.dumps(st, sort_keys=True)
        tick_lane(conn, cfg, row, st, now)
        changed = changed or json.dumps(st, sort_keys=True) != before
    save(conn, cfg)
    return changed


def step(conn):
    """Worker entry (server.start_workers): never raises into the worker loop's busy path."""
    try:
        tick(conn)
    except Exception as exc:
        emit(conn, 'tick_failed', 'error', error=f'{type(exc).__name__}: {exc}'[:200])
        try:
            conn.rollback()
        except Exception:
            pass
    return False


# ---------------------------------------------------------------- settings and the readout

def _lane_row(conn, ref):
    wanted = str(ref).lstrip('@').lower()
    match = [r for r in accounts.rows(conn) if r['lane_id'] == ref or (r['handle'] or '').lower() == wanted]
    if len(match) != 1:
        raise ValueError(f'No single account matches "{ref}".')
    return match[0]


def post(conn, q, b):
    """{action: on|off|resume|restore|set, ...}. `ceiling` above the proven 285 must be given explicitly."""
    action = b.get('action', 'set')
    if action not in ('on', 'off', 'resume', 'restore', 'set'):
        raise ValueError('Choose on, off, resume, restore or set.')
    now = utcnow()
    cfg = config(conn)
    for key, kind, lo, hi in (('ceiling', (int, float), 20, 5000), ('interval_s', (int, float), 60, 7 * 86400), ('step_factor', (int, float), 1.01, 4)):
        if key in b:
            if isinstance(b[key], bool) or not isinstance(b[key], kind) or not lo <= b[key] <= hi:
                raise ValueError(f'{key} must be a number from {lo} to {hi}.')
            if key == 'ceiling' and b[key] != cfg['ceiling']:
                emit(conn, 'ceiling_set', 'warn', now=now, ceiling=b[key], proven=PROVEN_CEILING,
                     above_proven=b[key] > PROVEN_CEILING)
            cfg[key] = b[key]
    if 'auto_resume' in b:
        cfg['auto_resume'] = bool(b['auto_resume'])
    if 'accounts' in b:
        if not isinstance(b['accounts'], list) or not all(isinstance(a, str) for a in b['accounts']):
            raise ValueError('accounts must be a list of handles.')
        cfg['accounts'] = [a.lstrip('@').lower() for a in b['accounts']]
    if action == 'on':
        cfg['enabled'] = True
        emit(conn, 'ramp_on', now=now, ceiling=cfg['ceiling'], interval_h=round(cfg['interval_s'] / 3600, 2), step_factor=cfg['step_factor'])
    elif action == 'off':
        cfg['enabled'] = False
        emit(conn, 'ramp_off', now=now)
    elif action in ('resume', 'restore'):
        row = _lane_row(conn, b.get('account') or (cfg['accounts'] or [''])[0])
        st = cfg['lanes'].get(row['lane_id'])
        if st is None:
            raise ValueError('The ramp has not started on that account yet.')
        st['lane_id'], st['handle'] = row['lane_id'], row['handle']
        if action == 'restore':
            out = pace.post(conn, {}, {'scope': 'account', 'lane_id': row['lane_id'], 'reset': True})
            cfg['lanes'].pop(row['lane_id'], None)
            emit(conn, 'restore', 'warn', now=now, lane=row['lane_id'], account=row['handle'], to_id=out['after']['id'])
        else:
            # take the pace as it is now as the new starting level; the ramp climbs from here
            cur = pace.resolve(conn, row)
            fresh = fresh_lane(row, cur['values'], now)
            fresh.update(lane_id=row['lane_id'], handle=row['handle'], history=st['history'], auto_resumes=st['auto_resumes'], rate_events=st['rate_events'])
            cfg['lanes'][row['lane_id']] = fresh
            note(conn, fresh, now, 'resume', 'a person resumed the ramp; it starts again from the pace running now', 'warn')
    save(conn, cfg)
    return status(conn)


def status(conn, now=None):
    now = now or utcnow()
    cfg = config(conn)
    over = cfg['ceiling'] > PROVEN_CEILING
    out = {'v': 1, 'enabled': cfg['enabled'], 'ceiling': cfg['ceiling'], 'proven_ceiling': PROVEN_CEILING, 'above_proven': over,
           'interval_h': round(cfg['interval_s'] / 3600, 2), 'step_factor': cfg['step_factor'], 'auto_resume': cfg['auto_resume'],
           'accounts': [], 'lines': []}
    lines = out['lines']
    lines.append(f"Ramp is {'ON' if cfg['enabled'] else 'OFF'}. After every {out['interval_h']:g} h of clean collection it speeds up by "
                 f"{cfg['step_factor']:g}x (pages per hour), up to a ceiling of {cfg['ceiling']:g} pages/h.")
    if over:
        lines.append(f"The ceiling is set to {cfg['ceiling']:g}, above the proven {PROVEN_CEILING}. Nothing above {PROVEN_CEILING} pages/h has been measured clean; "
                     'that part is an experiment you chose.')
    else:
        lines.append(f'Proven clean range: up to {PROVEN_CEILING} pages/h (6,195 pages, no list rate limit). Anything above that is unmeasured, so the ramp stops there.')
    lines.append('Opted in: ' + (', '.join('@' + a for a in cfg['accounts']) or 'nobody') + '. The main account and any held or warned account are never ramped.')
    for row in accounts.rows(conn):
        st = cfg['lanes'].get(row['lane_id'])
        if st is None:
            continue
        cur = st['levels'][min(st['level'], len(st['levels']) - 1)]
        start = parse(st['window_start'])
        left = max(0, cfg['interval_s'] - (now - start).total_seconds())
        word = {'running': 'climbing', 'holding': 'holding after a limit', 'at_ceiling': 'at the ceiling', 'at_bound': 'at the fastest allowed gap',
                'stopped': 'STOPPED, needs a person', 'manual': 'standing down (pace changed by hand)'}.get(st['mode'], st['mode'])
        name = accounts.name_of(row)
        lw = st.get('last_window') or {}
        lines.append('')
        lines.append(f"{name}: {word}. Level {st['level']}, expected about {cur.get('predicted', 0):.0f} pages/h; list gap {cur['values']['list_gap'][0]}-{cur['values']['list_gap'][1]} s.")
        if st['mode'] in ('running', 'holding', 'at_ceiling', 'at_bound'):
            lines.append(f"  Current window started {iso(start)}; the next decision is in {int(left // 60)} min"
                         + (f" ({st['hold_left']} clean window(s) to hold first)" if st['hold_left'] else '') + '.')
        if st['stopped_reason']:
            lines.append(f"  Why: {st['stopped_reason']}. Fix it, then run: ops/set-pace --ramp resume --account @{row['handle']}")
        if lw:
            lines.append(f"  Last window: {lw['pages']} pages, {lw['pages_per_hour']} pages/h, collection ran {round(lw['runtime_share'] * 100)}%, "
                         f"median gap {lw['median_gap_s']} s, extra time per page {lw['overhead_s']} s (earlier {st['prev_overhead']}).")
        for h in st['history'][-6:]:
            lines.append(f"  {h['at'][5:16].replace('T', ' ')}  {h['action'].replace('_', ' ')}: {h['reason']}")
        out['accounts'].append({'lane_id': row['lane_id'], 'name': name, 'mode': st['mode'], 'level': st['level'], 'expected_pages_per_hour': cur.get('predicted'),
                                'values': cur['values'], 'window_start': st['window_start'], 'next_decision_in_s': round(left), 'hold_left': st['hold_left'],
                                'stopped_reason': st['stopped_reason'], 'last_window': st.get('last_window'), 'history': st['history'][-12:],
                                'levels': [{'level': l['level'], 'pages_per_hour': l.get('predicted')} for l in st['levels']]})
    if not out['accounts']:
        lines.append('')
        lines.append('No account is being ramped yet.' + ('' if cfg['enabled'] else ' Turn it on with: ops/set-pace --ramp on'))
    return out


def get(conn, q, b):
    return status(conn)


def routes(_server):
    return [('GET', r'/api/ramp', get), ('POST', r'/api/ramp', post)]
