"""One control model for the whole workspace: three stages that run or pause on their own, plus per-account pause.

Stages and the settings they sit on:
  lists  "Collect lists"  settings `paused` (workspace pause, legacy) or `paused_lists`  -> /api/ext/next hands out no list jobs
  bios   "Read bios"      settings `paused` or `paused_bios`                              -> /api/ext/next hands out no profile jobs
  ai     "External AI scoring" setting `qualify` (off also switches `qualify_auto` off)
Processing modes are cumulative: R, RLAI and RLEAI. Pausing external AI retains local AI.
Resuming lists or bios clears the legacy `paused` and keeps the other stage where it was, so the three stay independent.
"""
from datetime import datetime, timedelta, timezone

import accounts
import db
import processing_modes

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
        return not processing_modes.allows(conn, 'external')
    return bool(db.get_setting(conn, 'instagram_scraping_warning') and not db.get_setting(conn, 'instagram_collection_isolation')) or bool(db.get_setting(conn, 'paused')) or bool(db.get_setting(conn, 'paused_' + stage))


def paused_kinds(conn):
    """Job kinds /api/ext/next must not hand out right now."""
    return {KIND[s] for s in ('lists', 'bios') if stage_paused(conn, s)}


def set_stage(conn, stage, pause):
    if stage == 'ai':
        mode = processing_modes.current_mode(conn)
        processing_modes.set_mode(conn, 'RLAI' if pause and mode == 'RLEAI'
                                  else mode if pause else 'RLEAI')
        return
    if not pause and db.get_setting(conn, 'paused'):
        # the legacy pause covered both Instagram stages: keep the other one paused so only this one resumes
        other = 'bios' if stage == 'lists' else 'lists'
        db.set_setting(conn, 'paused_' + other, True)
        db.set_setting(conn, 'paused', False)
    db.set_setting(conn, 'paused_' + stage, bool(pause))


def stop_all(conn):
    processing_modes.set_mode(conn, 'R')
    for s in ('lists', 'bios'):
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


def shared_collection_wait(conn, now):
    """Describe the workspace hold that already blocks every Instagram request."""
    raw = db.get_setting(conn, 'cooldown')
    if not raw:
        return None
    until = utc(raw)
    if until and until <= now:
        return None
    why = 'Protective pause after an Instagram response'
    if until is None:
        return {'state': 'waiting', 'wait': {'why': why, 'seconds': None, 'until': None, 'scope': 'workspace'},
                'now': 'Instagram collection is on hold. Check collection settings before continuing.'}
    seconds = max(0, int((until - now).total_seconds()))
    return {'state': 'waiting', 'wait': {'why': why, 'seconds': seconds, 'until': iso(until), 'scope': 'workspace'},
            'now': f'Scraping is paused until {until.astimezone():%H:%M}. Progress is saved. Resumes automatically.'}


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
    if row['state'] == 'network_wait':
        return 'Instagram connection trouble, retrying safely', max(0, int((u - now).total_seconds())) if u else None
    if u and u > now:
        sec = int((u - now).total_seconds())
        return 'Waiting between requests', sec
    return None


def counts(conn, now):
    hour, day = iso(now - timedelta(hours=1)), iso(now)[:10]

    def two(sql):
        return {'hour': conn.execute(sql, (hour,)).fetchone()[0], 'today': conn.execute(sql, (day,)).fetchone()[0]}
    ai = two('SELECT count(*) FROM ai_scoring_events WHERE scored_at>=?')
    ai['minute'] = conn.execute('SELECT count(*) FROM ai_scoring_events WHERE scored_at>=?',
                                (iso(now - timedelta(minutes=1)),)).fetchone()[0]
    return {'lists': two("SELECT coalesce(sum(saved_entries),0) FROM collector_events WHERE at>=? AND outcome='page'"),
            'new_profiles': two('SELECT count(*) FROM people WHERE first_seen>=?'),
            'bios': two("SELECT count(*) FROM collector_events WHERE at>=? AND kind='profile' AND outcome='profile' AND reason='saved'"), 'ai': ai}


UNIT = {'lists': 'list entries', 'bios': 'bios', 'ai': 'scores'}


def active_request(conn, now):
    request = (db.get_setting(conn, 'instagram_request_gate') or {}).get('active') or {}
    until = request.get('until')
    return request if isinstance(until, (int, float)) and not isinstance(until, bool) and until > now.timestamp() else {}


def reserved(conn, row, kind):
    return accounts.collection_protected(conn, row)


def stage_out(conn, stage, accts, rows, c, now, queue, ai_rate=None):
    paused = stage_paused(conn, stage)
    out = {'id': stage, 'label': LABEL[stage], 'help': HELP[stage], 'paused': paused, 'unit': UNIT[stage],
           'hour': c['hour'], 'today': c['today'], 'wait': None, 'queue': queue}
    if stage == 'ai':
        out['minute'] = c['minute']
    if paused:
        why = 'Paused by you.' if stage == 'ai' or db.get_setting(conn, 'paused_' + stage) else 'Paused in the workspace.'
        if stage == 'ai' and processing_modes.allows(conn, 'laya'):
            return dict(out, label='External AI scoring', state='paused', now='External AI is off; local Laya remains enabled.')
        attention = db.get_setting(conn, 'instagram_scraping_warning') or db.get_setting(conn, 'instagram_request_attention')
        if stage != 'ai' and isinstance(attention, dict) and attention.get('message'):
            return dict(out, state='paused', now=attention['message'], attention=attention)
        return dict(out, state='paused', now=why)
    if stage == 'ai':
        if not queue:
            return dict(out, state='idle', now='Running, nothing waiting to be scored.')
        return dict(out, state='running', now=f'Scoring bios with the AI model, {queue:,} waiting.')
    shared_wait = shared_collection_wait(conn, now)
    if shared_wait:
        return dict(out, **shared_wait)
    kind = KIND[stage]
    # accounts that could do this stage: online, not paused, role allows it
    role_ok = {'list': ('lists', 'both'), 'profile': ('bios', 'both')}[kind]
    able = [a for a in accts if a['online'] and not a['paused'] and (a['role'] or 'both') in role_ok]
    request = active_request(conn, now)
    working = [a for a in able if request.get('lane') == a['lane_id'] and request.get('kind') == kind]
    if not accts or not any(a['online'] for a in accts):
        return dict(out, state='waiting' if queue else 'idle', now=f'No Instagram account is online. {queue:,} jobs waiting. Open Chrome with the extension.')
    if not able:
        return dict(out, state='waiting' if queue else 'idle', now=f'Every account that does this is paused or offline. {queue:,} jobs waiting.')
    if working:
        a = working[0]
        j = a['job'] or {}
        what = (f"@{j['seed']}'s {j['direction']}" if kind == 'list' and j.get('seed') else '@' + j['handle'] if j.get('handle') else 'Instagram')
        more = f' (+{len(working) - 1} more accounts)' if len(working) > 1 else ''
        return dict(out, state='running', now=f"{a['name']} is reading {what}{more}.")
    if not queue:
        return dict(out, state='idle', now='No collection work waiting.')
    able = [a for a in able if not reserved(conn, rows[a['lane_id']], kind)]
    if not able:
        return dict(out, state='waiting', now='Your main account is reserved. Waiting for an alternate account.')
    waits = [(w, a) for a in able for w in [lane_wait(conn, rows[a['lane_id']], kind, now)] if w]
    if waits and len(waits) == len(able):
        (why, sec), a = min(waits, key=lambda x: x[0][1] if x[0][1] is not None else 1e9)
        return dict(out, state='waiting', wait={'why': why, 'seconds': sec},
                    now=f"{why}{', back in ' + mins(sec) if sec is not None else ''}.")
    return dict(out, state='waiting', now=f'Waiting for an account to pick up work, {queue:,} jobs queued.')


def account_out(conn, a, row, now, all_paused):
    request = active_request(conn, now)
    active = request.get('lane') == a['lane_id']
    base = {'collection_protected': accounts.collection_protected(conn, row), 'active': active, 'lane_id': a['lane_id'], 'name': a['name'], 'role': a['role'], 'paused': a['paused'], 'online': a['online'],
            'status': a['status'] if active or a['status'] != 'running' else 'online', 'hour': a['hour'], 'today': a['today'], 'wait': None,
            'budget': a['budget'], 'hold': a['hold'], 'job': a['job']}
    if active:
        stopping = base['collection_protected'] or a['paused'] or stage_paused(conn, 'lists' if request.get('kind') == 'list' else 'bios')
        return dict(base, state='stopping' if stopping else 'running',
                    now='Finishing the current Instagram request.' if stopping else 'Reading an Instagram list.' if request.get('kind') == 'list' else 'Reading an Instagram profile.')
    if base['collection_protected']:
        return dict(base, state='idle', reason_code='main_reserved', now='Personal use only. Automated collection is disabled.')
    if a['paused']:
        return dict(base, state='paused', now='Paused by you.')
    if not a['online']:
        return dict(base, state='offline', now='Offline: its Chrome window is closed or asleep.')
    shared_wait = shared_collection_wait(conn, now) if not all_paused else None
    if shared_wait:
        return dict(base, **shared_wait)
    if a['hold']:
        why, sec = lane_wait(conn, row, 'list', now)
        return dict(base, state='waiting', wait={'why': why, 'seconds': sec}, now=why + '.')
    if a.get('state') == 'network_wait':
        return dict(base, state='waiting', now='Instagram connection trouble. Retrying safely; progress is saved.')
    if all_paused:
        return dict(base, state='paused', now='Lists and bios are paused.')
    if a['job']:
        j = a['job']
        wait = lane_wait(conn, row, j['kind'], now)
        if wait:
            why, sec = wait
            return dict(base, state='waiting', wait={'why': why, 'seconds': sec},
                        now=f"{why}{', back in ' + mins(sec) if sec is not None else ''}.")
        what = f"@{j['seed']}'s {j['direction']}" if j['kind'] == 'list' else f"the bio of @{j['handle']}"
        return dict(base, state='waiting', now=f'Assigned {what}; waiting for the next request.')
    kinds = [KIND[s] for s in ('lists', 'bios') if not stage_paused(conn, s)
             and (a['role'] or 'both') in (s, 'both')]
    if not kinds:
        return dict(base, state='paused', now='The stages assigned to this account are paused.')
    kinds = [kind for kind in kinds if not reserved(conn, row, kind)]
    if not kinds:
        return dict(base, state='idle', reason_code='main_reserved', now='Main account reserved; alternates handle collection.')
    waits = [lane_wait(conn, row, kind, now) for kind in kinds]
    if all(waits):
        why, sec = min(waits, key=lambda w: w[1] if w[1] is not None else float('inf'))
        return dict(base, state='waiting', wait={'why': why, 'seconds': sec},
                    now=f"{why}{', back in ' + mins(sec) if sec is not None else ''}.")
    return dict(base, state='idle', now='Online, waiting for work.')


def queue_counts(conn):
    """Pending operational work, excluding directions disabled by the operator."""
    following = db.get_setting(conn, 'follower_lists') is False
    return dict.fromkeys(('list', 'profile'), 0) | dict(conn.execute(
        "SELECT kind, count(*) FROM jobs WHERE state IN ('queued','leased') "
        "AND (?=0 OR kind!='list' OR direction='following') GROUP BY kind", (following,)).fetchall())


def snapshot(conn, ai_left=None):
    now = datetime.now(timezone.utc)
    rows = {r['lane_id']: r for r in accounts.rows(conn)}
    accts = [accounts.out(conn, r, now) for r in rows.values()]
    c = counts(conn, now)
    queue = queue_counts(conn)
    stages = [stage_out(conn, 'lists', accts, rows, c['lists'], now, queue['list']),
              stage_out(conn, 'bios', accts, rows, c['bios'], now, queue['profile']),
              stage_out(conn, 'ai', accts, rows, c['ai'], now, ai_left or 0)]
    both = all(s['paused'] for s in stages[:2])
    gate = db.get_setting(conn, 'instagram_request_gate') or {}
    request = gate.get('active') or {}
    until = request.get('until')
    active = bool(request and isinstance(until, (int, float)) and not isinstance(until, bool)
                  and until > now.timestamp())
    request_attention = db.get_setting(conn, 'instagram_request_attention')
    attention = db.get_setting(conn, 'instagram_scraping_warning') or request_attention
    unconfirmed = bool(request and not active) or bool(isinstance(request_attention, dict) and request_attention.get('message'))
    if request and not active and not attention:
        # Expiry does not prove that a browser request stopped. GET reports the
        # uncertainty; only the existing request gate changes collection policy.
        attention = {'lane': request.get('lane'), 'message':
                     'An Instagram request did not confirm completion. Check the account tab before continuing.'}
    for stage in stages[:2]:
        stage['active'] = active and request.get('kind') == KIND[stage['id']]
        stage['stop_acknowledged'] = stage['paused'] and not stage['active'] and not unconfirmed
        if stage['paused'] and stage['active']:
            stage.update(state='stopping', now='Finishing the current Instagram request. Your progress is saved.')
        elif stage['paused'] and unconfirmed:
            stage.update(attention=attention, now=attention.get('message') if isinstance(attention, dict) else
                         'Waiting for the account to confirm collection has stopped.')
    stopping = both and active
    acknowledged = both and not (active or unconfirmed)
    return {'progress': {'unique_new_profiles': c['new_profiles'], 'saved_list_entries': c['lists'],
                         'bios_read': c['bios'], 'timezone': 'UTC'}, 'stages': stages, 'accounts': [account_out(conn, a, rows[a['lane_id']], now, both) for a in accts],
            'all_paused': all(s['paused'] for s in stages) and not processing_modes.allows(conn, 'laya'),
            'local_laya': processing_modes.allows(conn, 'laya'), 'processing': processing_modes.snapshot(conn),
            'instagram_request_attention': attention, 'collection_isolation': db.get_setting(conn, 'instagram_collection_isolation'), 'active': active, 'stopping': stopping,
            'stop_acknowledged': acknowledged,
            'collection_scope': 'following' if db.get_setting(conn, 'follower_lists') is False else 'both',
            'collection': {'paused': both, 'active': active, 'stopping': stopping,
                           'stop_acknowledged': acknowledged, 'unconfirmed': unconfirmed},
            'at': iso(now)}


def stop_benchmark(conn, body):
    """Operator review of an expired experiment. Unknown outcomes stay unknown."""
    if body.get('checked_account_tab') is not True:
        raise ValueError('Check the benchmark account tab first, then confirm it has stopped.')
    now = datetime.now(timezone.utc)
    stamp = now.timestamp()
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    try:
        cfg = db.get_setting(conn, 'raw_edge_benchmark') or {}
        if cfg.get('enabled') is not True:
            raise ValueError('No active benchmark to stop.')
        inflight = cfg.get('inflight') or {}
        gate = db.get_setting(conn, 'instagram_request_gate') or {}
        request = gate.get('active') or {}
        attention = db.get_setting(conn, 'instagram_request_attention')
        def expired(value):
            return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= stamp
        if inflight and (not expired(inflight.get('at')) or
                         not expired(inflight['at'] + accounts.REQUEST_LEASE_SECONDS)):
            raise ValueError('Wait for the benchmark request to finish or expire before reviewing it.')
        if request and (not expired(request.get('until')) or request.get('lane') != cfg.get('lane_id') or
                        request.get('token') != inflight.get('token')):
            raise ValueError('A current or unrelated Instagram request remains. Collection stays stopped.')
        if attention:
            try:
                attention_at = utc(attention.get('at')) if isinstance(attention, dict) else None
            except (TypeError, ValueError, AttributeError):
                attention_at = None
            if (not isinstance(attention, dict) or attention.get('lane') != cfg.get('lane_id') or
                    attention.get('message') != 'Benchmark request did not confirm completion.' or
                    not attention_at or not inflight or attention_at.timestamp() < inflight['at']):
                raise ValueError('An unrelated safety warning needs review. Collection stays stopped.')
        review = {'at': iso(now), 'lane': cfg.get('lane_id'), 'outcome': 'abandoned_unconfirmed',
                  'checked_account_tab': True, 'request': request or None, 'inflight': inflight or None,
                  'attention': attention}
        cfg.update(enabled=False, operator_stop=review)
        db.set_setting(conn, 'raw_edge_benchmark', cfg)
        if request:
            db.set_setting(conn, 'instagram_request_gate', dict(gate, active=None, queue=[],
                           next_at=max(gate.get('next_at') or 0, stamp + accounts.REQUEST_SPACING_SECONDS)))
        db.set_setting(conn, 'instagram_request_attention', None)
        for stage in ('lists', 'bios'):
            db.set_setting(conn, 'paused_' + stage, True)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def apply(conn, b):
    """Stage/account pause or resume, or explicit {"action": "start_all"}."""
    action = b.get('action')
    if action in ('acknowledge_scraping_warning', 'resume_selected_accounts') and not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    warning = db.get_setting(conn, 'instagram_scraping_warning')
    if action == 'set_list_scope':
        scope = b.get('scope')
        if scope not in ('following', 'both'):
            raise ValueError('Choose following only or followers and following.')
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        state = snapshot(conn)
        startup = db.get_setting(conn, 'collection_startup') or {}
        if not state['stop_acknowledged'] or startup.get('state') in ('opening', 'waiting'):
            raise ValueError('Stop collection and wait for the current request before changing lists.')
        db.set_setting(conn, 'follower_lists', scope == 'both')
        conn.commit()
        return
    if action == 'resume_selected_accounts':
        selected = b.get('accounts')
        isolation = db.get_setting(conn, 'instagram_collection_isolation')
        if not (warning or isolation) or not isinstance(selected, list) or len(selected) not in (1, 2):
            raise ValueError('Select one or two existing accounts while keeping the warned account blocked.')
        stages = b.get('collection_stages', (isolation or {}).get('collection_stages', ['lists', 'bios']))
        if (not isinstance(stages, list) or not stages or any(stage not in ('lists', 'bios') for stage in stages)
                or len(stages) != len(set(stages))):
            raise ValueError('Choose lists, bios, or both collection stages.')
        now = datetime.now(timezone.utc)
        gate = db.get_setting(conn, 'instagram_request_gate') or {}
        if gate.get('active') or db.get_setting(conn, 'instagram_request_attention') or shared_collection_wait(conn, now):
            raise ValueError('Wait for the current request or protective pause to finish first.')
        allowed = {}
        for selection in selected:
            if not isinstance(selection, dict):
                raise ValueError('Each selected account needs its existing lane and Instagram identity.')
            row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (selection.get('lane_id'),)).fetchone()
            if (not row or not row['ig_id'] or selection.get('ig_id') != row['ig_id'] or row['lane_id'] in allowed
                    or row['ig_id'] in allowed.values() or accounts.collection_protected(conn, row) or accounts.warning_for(conn, row) or row['hold']
                    or not row['last_seen'] or now - utc(row['last_seen']) > accounts.RELEASE_AFTER
                    or row['state'] == 'network_wait' or row['collection_backend'] != 'chrome'
                    or tuple(int(v) for v in str(row['version'] or '0').split('.') if v.isdigit()) < (3, 9, 30)
                    or accounts.later(row['cooldown_until'], now)
                    or any(accounts.identity_cooling(conn, row, kind, now) for kind in ('list', 'profile'))):
                raise ValueError('Only healthy, signed-in accounts can continue.')
            waits = conn.execute(
                'SELECT cooldown_until,list_cool_until,profile_cool_until FROM accounts WHERE ig_id=? UNION ALL '
                'SELECT cooldown_until,list_cool_until,profile_cool_until FROM account_identity_state WHERE ig_id=?',
                (row['ig_id'], row['ig_id'])).fetchall()
            if any(accounts.later(value, now) for wait in waits for value in wait):
                raise ValueError('A selected Instagram identity still has a protective pause.')
            if any(accounts.budget_of(conn, row).get(kind, 0) <= 0 for kind in ('list', 'profile')):
                raise ValueError('Set finite daily limits before continuing selected accounts.')
            allowed[row['lane_id']] = row['ig_id']
        if isolation and not set(allowed.items()).issubset((isolation.get('accounts') or {}).items()):
            raise ValueError('Continue only previously selected Instagram accounts.')
        db.set_setting(conn, 'instagram_collection_isolation', {'accounts': allowed, 'collection_stages': stages, 'at': iso(now)})
        conn.execute('UPDATE accounts SET paused=1')
        for lane in allowed:
            conn.execute('UPDATE accounts SET paused=0 WHERE lane_id=?', (lane,))
        db.set_setting(conn, 'paused', False)
        db.set_setting(conn, 'paused_lists', 'lists' not in stages)
        db.set_setting(conn, 'paused_bios', 'bios' not in stages)
        conn.commit()
        return
    if action == 'acknowledge_scraping_warning':
        if (not all(stage_paused(conn, stage) for stage in ('lists', 'bios'))
                or (db.get_setting(conn, 'instagram_request_gate') or {}).get('active')):
            raise ValueError('Stop collection and wait for the current request before reviewing this warning.')
        if not warning or b.get('account') != warning.get('lane') or b.get('reviewed') is not True:
            raise ValueError('Review the affected Instagram account before acknowledging its warning.')
        row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (warning['lane'],)).fetchone()
        if not warning.get('review_ready') or not row or not row['last_seen'] or datetime.now(timezone.utc) - utc(row['last_seen']) > accounts.RELEASE_AFTER:
            raise ValueError('The updated extension must confirm the Instagram warning page is closed before acknowledgment.')
        reviewed = db.get_setting(conn, 'instagram_scraping_warning_reviewed') or {}
        reviewed[warning['lane']] = datetime.now(timezone.utc).timestamp() * 1000
        db.set_setting(conn, 'instagram_scraping_warning_reviewed', reviewed)
        pending = warning.get('pending', {})
        remaining = pending.pop(next(iter(pending))) if pending else None
        if remaining:
            remaining['pending'] = pending
        db.set_setting(conn, 'instagram_scraping_warning', remaining)
        conn.execute('UPDATE accounts SET hold=NULL WHERE lane_id=?', (warning['lane'],))
        conn.commit()
        return
    if warning and action in ('resume', 'start_all') and b.get('stage') != 'ai':
        raise ValueError(warning['message'])
    if action == 'stop_benchmark':
        return stop_benchmark(conn, b)
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
    if stage == 'collection':
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        try:
            if not pause:
                raw = db.get_setting(conn, 'cooldown')
                until = utc(raw) if raw else None
                if raw and (until is None or until > datetime.now(timezone.utc)):
                    raise ValueError('Instagram is on a shared safety hold. Collection remains paused.')
                db.set_setting(conn, 'paused', False)
            for name in ('lists', 'bios'):
                db.set_setting(conn, 'paused_' + name, pause)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return
    if stage == 'all':
        stop_all(conn) if pause else resume_all(conn)
    elif stage in STAGES:
        set_stage(conn, stage, pause)
    else:
        raise ValueError('stage must be collection, lists, bios, ai or all (or give an account)')
    conn.commit()
