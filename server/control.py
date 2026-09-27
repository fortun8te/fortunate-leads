"""One control model for the whole workspace: three stages that run or pause on their own, plus per-account pause.

Stages and the settings they sit on:
  lists  "Collect lists"  settings `paused` (workspace pause, legacy) or `paused_lists`  -> /api/ext/next hands out no list jobs
  bios   "Read bios"      settings `paused` or `paused_bios`                              -> /api/ext/next hands out no profile jobs
  ai     "External AI scoring" setting `qualify` (off also switches `qualify_auto` off)
Local Laya can keep running with external AI off when `local_laya` is enabled.
Resuming lists or bios clears the legacy `paused` and keeps the other stage where it was, so the three stay independent.
"""
from datetime import datetime, timedelta, timezone

import accounts
import db

STAGES = ('lists', 'bios', 'ai')
LABEL = {'lists': 'Collect lists', 'bios': 'Read bios', 'ai': 'AI scoring'}
HELP = {
    'lists': 'Visits the follower and following lists of your seed accounts on Instagram and saves the people in them. '
             'Pause stops new list pages; nothing already saved is lost.',
    'bios': 'Opens the profiles of the most promising people one by one and saves their bio. '
            'Pause stops new profile visits; lists keep going unless you pause them too.',
    'ai': 'Asks the AI model to read each saved bio and score how good a lead the person is. '
          'Pause stops new model calls; no Instagram requests are involved.',
}
KIND = {'lists': 'list', 'bios': 'profile'}
def utc(s):
    try:
        d = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def iso(d):
    return d.isoformat(timespec='microseconds')


def stage_paused(conn, stage):
    if stage == 'ai':
        return not db.get_setting(conn, 'qualify')
    return bool(db.get_setting(conn, 'paused')) or bool(db.get_setting(conn, 'paused_' + stage))


def paused_kinds(conn):
    """Job kinds /api/ext/next must not hand out right now."""
    return {KIND[s] for s in ('lists', 'bios') if stage_paused(conn, s)}


def set_stage(conn, stage, pause):
    if stage == 'ai':
        db.set_setting(conn, 'qualify', not pause)
        if pause:
            db.set_setting(conn, 'qualify_auto', False)
        else:
            # Explicitly resuming external AI leaves local-only mode.
            db.set_setting(conn, 'local_laya', False)
        return
    if not pause and db.get_setting(conn, 'paused'):
        # the legacy pause covered both Instagram stages: keep the other one paused so only this one resumes
        other = 'bios' if stage == 'lists' else 'lists'
        db.set_setting(conn, 'paused_' + other, True)
        db.set_setting(conn, 'paused', False)
    db.set_setting(conn, 'paused_' + stage, bool(pause))


def stop_all(conn):
    db.set_setting(conn, 'local_laya', False)
    for s in STAGES:
        set_stage(conn, s, True)


def resume_all(conn):
    db.set_setting(conn, 'paused', False)
    # Collection controls never opt into model calls. AI has its own explicit switch.
    for s in ('lists', 'bios'):
        set_stage(conn, s, False)


def start_all(conn):
    """Start collection and accounts without changing the user's external AI choice."""
    db.set_setting(conn, 'paused', False)
    for stage in ('lists', 'bios'):
        set_stage(conn, stage, False)
    conn.execute('UPDATE accounts SET paused=0 WHERE paused=1')


def mins(sec):
    sec = max(0, int(sec))
    return f'{sec} s' if sec < 60 else f'{round(sec / 60)} min' if sec < 3600 else f'{sec / 3600:.1f} h'


def lane_wait(conn, row, kind, now):
    """(why, seconds) when this account can't do `kind` right now, else None."""
    if row['hold']:
        return ('Instagram asks this account to log in again' if row['hold'] == 'login'
                else 'Instagram wants a security check on this account'), None
    budget = accounts.budget_of(conn, row).get(kind, 0)
    today = accounts.jload(row['today'], {}) or {}
    used = today.get(kind, 0) if (row['last_seen'] or '').startswith(iso(now)[:10]) else 0
    if budget and used >= budget:   # 0 = no daily limit
        return 'Daily request budget reached', int(((now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) - now).total_seconds())
    cool = row['list_cool_until'] if kind == 'list' else row['profile_cool_until']
    u = utc(cool) if cool else None
    if u and u > now:
        return 'Instagram asked us to slow down, resting', int((u - now).total_seconds())
    ready = (db.get_setting(conn, 'ext_ready') or {}).get(row['lane_id']) or {}
    u = utc(ready.get(kind)) if ready.get(kind) else None
    if u and u > now:
        sec = int((u - now).total_seconds())
        return 'Waiting between requests', sec
    return None


def lane_blocked(row, kind, now):
    """A hard Instagram wait, even if an old job lease still appears active."""
    if row['hold']:
        return True
    fields = ('list_cool_until',) if kind == 'list' else ('profile_cool_until',)
    return any((until := utc(row[field])) is not None and until > now for field in fields)


def counts(conn, now):
    hour, day = iso(now - timedelta(hours=1)), iso(now)[:10]

    def two(sql):
        return {'hour': conn.execute(sql, (hour,)).fetchone()[0], 'today': conn.execute(sql, (day,)).fetchone()[0]}
    ai = two('SELECT count(*) FROM ai_scoring_events WHERE scored_at>=?')
    ai['minute'] = conn.execute('SELECT count(*) FROM ai_scoring_events WHERE scored_at>=?',
                                (iso(now - timedelta(minutes=1)),)).fetchone()[0]
    return {'lists': two('SELECT count(*) FROM edges WHERE first_seen>=?'),
            'bios': two('SELECT count(*) FROM people WHERE bio_at>=?'), 'ai': ai}


UNIT = {'lists': 'people', 'bios': 'bios', 'ai': 'scores'}


def stage_out(conn, stage, accts, rows, c, now, queue, ai_rate=None):
    paused = stage_paused(conn, stage)
    out = {'id': stage, 'label': LABEL[stage], 'help': HELP[stage], 'paused': paused, 'unit': UNIT[stage],
           'hour': c['hour'], 'today': c['today'], 'wait': None, 'queue': queue}
    if stage == 'ai':
        out['minute'] = c['minute']
    if paused:
        why = 'Paused by you.' if stage == 'ai' or db.get_setting(conn, 'paused_' + stage) else 'Paused in the workspace.'
        if stage == 'ai' and db.get_setting(conn, 'local_laya'):
            return dict(out, label='External AI scoring', state='paused', now='External AI is off; local Laya remains enabled.')
        return dict(out, state='paused', now=why)
    if stage == 'ai':
        if not queue:
            return dict(out, state='idle', now='Running, nothing waiting to be scored.')
        return dict(out, state='running', now=f'Scoring bios with the AI model, {queue:,} waiting.')
    kind = KIND[stage]
    # accounts that could do this stage: online, not paused, role allows it
    role_ok = {'list': ('lists', 'both'), 'profile': ('bios', 'both')}[kind]
    able = [a for a in accts if a['online'] and not a['paused'] and (a['role'] or 'both') in role_ok]
    working = [a for a in able if a['job'] and a['job']['kind'] == kind
               and not lane_blocked(rows[a['lane_id']], kind, now)]
    if not accts or not any(a['online'] for a in accts):
        return dict(out, state='waiting' if queue else 'idle', now=f'No Instagram account is online. {queue:,} jobs waiting. Open Chrome with the extension.')
    if not able:
        return dict(out, state='waiting' if queue else 'idle', now=f'Every account that does this is paused or offline. {queue:,} jobs waiting.')
    if working:
        a = working[0]
        j = a['job']
        what = (f"@{j['seed']}'s {j['direction']}" if kind == 'list' else '@' + (j['handle'] or '?'))
        more = f' (+{len(working) - 1} more accounts)' if len(working) > 1 else ''
        return dict(out, state='running', now=f"{a['name']} is reading {what}{more}.")
    if not queue:
        return dict(out, state='idle', now='Running, nothing left to do right now.')
    waits = [(w, a) for a in able for w in [lane_wait(conn, rows[a['lane_id']], kind, now)] if w]
    if waits and len(waits) == len(able):
        (why, sec), a = min(waits, key=lambda x: x[0][1] if x[0][1] is not None else 1e9)
        return dict(out, state='waiting', wait={'why': why, 'seconds': sec},
                    now=f"{why}{', back in ' + mins(sec) if sec is not None else ''}.")
    return dict(out, state='waiting', now=f'Waiting for an account to pick up work, {queue:,} jobs queued.')


def account_out(conn, a, row, now, all_paused):
    base = {'lane_id': a['lane_id'], 'name': a['name'], 'role': a['role'], 'paused': a['paused'], 'online': a['online'],
            'status': a['status'], 'hour': a['hour'], 'today': a['today'], 'wait': None,
            'budget': a['budget'], 'hold': a['hold'], 'job': a['job']}
    if a['paused']:
        return dict(base, state='paused', now='Paused by you.')
    if not a['online']:
        return dict(base, state='offline', now='Offline: its Chrome window is closed or asleep.')
    if a['hold']:
        why, sec = lane_wait(conn, row, 'list', now)
        return dict(base, state='waiting', wait={'why': why, 'seconds': sec}, now=why + '.')
    if a['job']:
        j = a['job']
        if lane_blocked(row, j['kind'], now):
            why, sec = lane_wait(conn, row, j['kind'], now)
            return dict(base, state='waiting', wait={'why': why, 'seconds': sec},
                        now=f"{why}{', back in ' + mins(sec) if sec is not None else ''}.")
        what = f"@{j['seed']}'s {j['direction']}" if j['kind'] == 'list' else f"the bio of @{j['handle']}"
        return dict(base, state='running', now=f'Reading {what}.')
    if all_paused:
        return dict(base, state='paused', now='Nothing to do: lists and bios are both paused.')
    kinds = [KIND[s] for s in ('lists', 'bios') if not stage_paused(conn, s)
             and (a['role'] or 'both') in (s, 'both')]
    if not kinds:
        return dict(base, state='paused', now='The stages assigned to this account are paused.')
    waits = [lane_wait(conn, row, kind, now) for kind in kinds]
    if all(waits):
        why, sec = min(waits, key=lambda w: w[1] if w[1] is not None else float('inf'))
        return dict(base, state='waiting', wait={'why': why, 'seconds': sec},
                    now=f"{why}{', back in ' + mins(sec) if sec is not None else ''}.")
    return dict(base, state='idle', now='Online, waiting for work.')


def snapshot(conn, ai_left=None):
    now = datetime.now(timezone.utc)
    rows = {r['lane_id']: r for r in accounts.rows(conn)}
    accts = [accounts.out(conn, r, now) for r in rows.values()]
    c = counts(conn, now)
    queue = dict.fromkeys(('list', 'profile'), 0) | dict(conn.execute(
        "SELECT kind, count(*) FROM jobs WHERE state IN ('queued','leased') GROUP BY kind").fetchall())
    stages = [stage_out(conn, 'lists', accts, rows, c['lists'], now, queue['list']),
              stage_out(conn, 'bios', accts, rows, c['bios'], now, queue['profile']),
              stage_out(conn, 'ai', accts, rows, c['ai'], now, ai_left or 0)]
    both = all(s['paused'] for s in stages[:2])
    return {'stages': stages, 'accounts': [account_out(conn, a, rows[a['lane_id']], now, both) for a in accts],
            'all_paused': all(s['paused'] for s in stages), 'local_laya': bool(db.get_setting(conn, 'local_laya')),
            'at': iso(now)}


def apply(conn, b):
    """Stage/account pause or resume, or explicit {"action": "start_all"}."""
    action = b.get('action')
    if action == 'start_all':
        if b.get('stage') is not None or isinstance(b.get('account'), str):
            raise ValueError('start_all applies to the whole workspace')
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        try:
            start_all(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return
    if action not in ('pause', 'resume'):
        raise ValueError('action must be pause, resume or start_all')
    pause = action == 'pause'
    # the extension tags every POST with its own `account` object, so only a lane id string means "this account"
    if b.get('stage') is None and isinstance(b.get('account'), str):
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        try:
            accounts.edit(conn, b['account'], {'paused': pause})
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return
    stage = b.get('stage')
    if stage == 'all':
        stop_all(conn) if pause else resume_all(conn)
    elif stage in STAGES:
        set_stage(conn, stage, pause)
    else:
        raise ValueError('stage must be lists, bios, ai or all (or give an account)')
    conn.commit()
