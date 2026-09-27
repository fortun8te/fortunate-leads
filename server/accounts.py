"""Lanes: one Chrome profile = one extension install = one Instagram account, all feeding this server.

A lane is identified by the `lane_id` its extension keeps in chrome.storage (old builds send none and are the
`default` lane). Pacing, budgets and cooldowns stay inside each extension; the server decides who gets which job:
  - a leased job is never handed to another lane before its lease ends;
  - a list sticks to the lane that started it (`lists.lane`) while that lane is healthy: seen within 10 min, not
    logged out / challenged, not paused and not in a list cooldown. Otherwise the list is released (`prev_lane` kept)
    and the next lane resumes it from the saved cursor;
  - role lists|bios|both filters the job kinds; a main account (is_main) gets bios only (or lists up to
    `main_list_share` of the last hour's pages) while alternates are configured for lists; with no alternates, it takes lists too.
"""
import json
import secrets
import re
from datetime import date, datetime, timedelta, timezone

import db

DEFAULT_LANE = 'default'
LANE_RX = re.compile(r'[A-Za-z0-9_-]{1,64}')
ROLES = ('lists', 'bios', 'both')
HOLDS = ('login', 'challenge')
RELEASE_AFTER = timedelta(minutes=10)   # offline this long: its list moves on
ONLINE_FOR = timedelta(seconds=90)      # heartbeats come every <= 30 s
BUDGET_MAX = {'list': 3000, 'profile': 5000}   # per day; 0 = no daily limit
HANDOFFS_KEEP = 30
IDENTITY_WAITS = ('cooldown_until', 'list_cool_until', 'profile_cool_until', 'list_endpoint_until')


def utc(s):
    d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def iso(d):
    return d.isoformat(timespec='microseconds')


def later(s, now):
    try:
        return bool(s) and utc(s) > now
    except ValueError:
        return False


def jload(s, default=None):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


# ---------- identity from a request ----------

def lane_of(q, b):
    v = b.get('lane_id') if isinstance(b, dict) else None
    if v is None:
        v = (q.get('lane') or [None])[0]
    v = str(v) if isinstance(v, (str, int)) and not isinstance(v, bool) else ''
    return v if LANE_RX.fullmatch(v) else DEFAULT_LANE


def clean_account(a):
    """{'ig_id','handle'} as sent by the extension. A present-but-empty ig_id means: no Instagram session."""
    if not isinstance(a, dict):
        return None
    out = {}
    if 'ig_id' in a:
        v = a['ig_id']
        v = str(v) if isinstance(v, (int, str)) and not isinstance(v, bool) else ''
        out['ig_id'] = v if re.fullmatch(r'\d{1,30}', v) else None
    if 'handle' in a:
        h = db.norm_handle(a['handle']) if isinstance(a['handle'], str) else ''
        out['handle'] = h if re.fullmatch(r'[a-z0-9._]{1,30}', h) else None
    return out or None


def account_from(q, b):
    if isinstance(b, dict) and 'account' in b:
        return clean_account(b['account'])
    if 'ig_id' in q or 'handle' in q:
        return clean_account({k: q[k][0] for k in ('ig_id', 'handle') if k in q})
    return None


def is_me(conn, handle):
    return bool(handle) and bool(conn.execute('SELECT 1 FROM seeds WHERE is_me=1 AND handle=?', (handle,)).fetchone())


def local_day(value=None):
    return (value or datetime.now(timezone.utc)).astimezone().date().isoformat()


def client_day(value):
    """Extension dayKey is local YYYY-M-D; store an unambiguous ISO date."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', value)
    if not match:
        return None
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None


def merged_counts(a, b):
    left, right = jload(a, {}) or {}, jload(b, {}) or {}
    return json.dumps({kind: max(left.get(kind, 0) if type(left.get(kind)) is int else 0,
                                 right.get(kind, 0) if type(right.get(kind)) is int else 0)
                       for kind in ('list', 'profile')})


def active_wait(a, b, now):
    active = [value for value in (a, b) if later(value, now)]
    return iso(max(map(utc, active))) if active else None


def remember_identity(conn, row, now, day=None):
    """Persist the maximum reported usage and active waits for this lane/IG ID."""
    if not row['ig_id']:
        return None
    saved = conn.execute('SELECT * FROM account_identity_state WHERE lane_id=? AND ig_id=?',
                         (row['lane_id'], row['ig_id'])).fetchone()
    row_day = day or (saved['day'] if saved else local_day(utc(row['last_seen'])) if row['last_seen'] else local_day(now))
    old_day = (saved['day'] or '') if saved else ''
    day = max(row_day, old_day)
    today = (merged_counts(saved['today'], row['today']) if saved and old_day == row_day else
             saved['today'] if old_day > row_day else row['today'])
    waits = [active_wait(saved[k] if saved else None, row[k], now) for k in IDENTITY_WAITS]
    conn.execute('INSERT INTO account_identity_state VALUES(?,?,?,?,?,?,?,?) '
                 'ON CONFLICT(lane_id,ig_id) DO UPDATE SET day=excluded.day,today=excluded.today,'
                 'cooldown_until=excluded.cooldown_until,list_cool_until=excluded.list_cool_until,'
                 'profile_cool_until=excluded.profile_cool_until,list_endpoint_until=excluded.list_endpoint_until',
                 (row['lane_id'], row['ig_id'], day, today, *waits))
    return conn.execute('SELECT * FROM account_identity_state WHERE lane_id=? AND ig_id=?',
                        (row['lane_id'], row['ig_id'])).fetchone()


def touch(conn, lane, acct=None, **fields):
    """Upsert the lane's row as seen now; `fields` are column values to set. Returns the row."""
    ts = db.now()
    day = client_day(fields.pop('identity_day', None)) or local_day(utc(ts))
    row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()
    if not row:
        conn.execute('INSERT INTO accounts(lane_id, first_seen, last_seen) VALUES(?,?,?)', (lane, ts, ts))
        row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()
    now = utc(ts)
    previous = remember_identity(conn, row, now)
    sets = dict(fields, last_seen=ts)
    if acct and 'ig_id' in acct and not acct['ig_id']:
        sets['hold'] = 'login'   # the profile has no Instagram session at all
    elif acct:
        if acct.get('ig_id'):
            if row['ig_id'] and row['ig_id'] != acct['ig_id']:
                # These counters and waits belong to the previous Instagram identity,
                # even if this Chrome lane and its extension storage are unchanged.
                release_all(conn, lane, why='identity_changed')
                saved = conn.execute('SELECT * FROM account_identity_state WHERE lane_id=? AND ig_id=?',
                                     (lane, acct['ig_id'])).fetchone()
                prior_today = saved['today'] if saved and saved['day'] == day else None
                incoming_today = sets.get('today') if 'today' in fields else None
                sets.update(handle=None, is_main=0,
                            today=merged_counts(prior_today, incoming_today) if prior_today or incoming_today else None,
                            rate=None, last_error=None, activity=None, hold=None)
                sets.update({key: active_wait(saved[key] if saved else None, fields.get(key), now)
                             for key in IDENTITY_WAITS})
            sets['ig_id'] = acct['ig_id']
        if acct.get('handle'):
            sets['handle'] = acct['handle']
            if (row['ig_id'] != acct.get('ig_id') or not row['handle']) and is_me(conn, acct['handle']):
                sets['is_main'] = 1   # Michael's own account: protected by default
    if previous and sets.get('ig_id', row['ig_id']) == row['ig_id']:
        # A stale heartbeat must not lower today's count or shorten an active
        # cooldown that was already observed for this identity.
        if previous['day'] == day:
            sets['today'] = merged_counts(previous['today'], sets.get('today', row['today']))
        else:
            sets['today'] = sets.get('today') if 'today' in fields else None
        for key in IDENTITY_WAITS:
            sets[key] = active_wait(previous[key], sets.get(key, row[key]), now)
    conn.execute(f"UPDATE accounts SET {', '.join(k + '=?' for k in sets)} WHERE lane_id=?", (*sets.values(), lane))
    updated = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()
    remember_identity(conn, updated, now, day)
    return updated


def budget_of(conn, row):
    b = db.get_setting(conn, 'budget')
    own = jload(row['budget']) if row else None
    if isinstance(own, dict):
        b.update({k: v for k, v in own.items() if k in BUDGET_MAX and isinstance(v, int)})
    return b


def paused_for(conn, row):
    return bool(db.get_setting(conn, 'paused')) or bool(row and row['paused'])


# ---------- health, release, handoff ----------

def account_available(row, now):
    """Can an in-flight profile lease stay with this signed-in lane?"""
    return bool(row['last_seen']) and now - utc(row['last_seen']) <= RELEASE_AFTER and not row['hold'] \
        and not row['paused']


def healthy(row, now):
    """May this lane keep (or take) a list?"""
    return account_available(row, now) and not later(row['list_cool_until'], now)


def role_allows(row, kind):
    role = row['role'] if row['role'] in ROLES else 'both'
    return role == 'both' or (kind == 'list' and role == 'lists') or (kind == 'profile' and role == 'bios')


def identity_owner(conn, row, now):
    """One active Chrome lane per Instagram identity, regardless of job kind.

    Prefer an available `both` lane so one profile can serve both queues.
    Otherwise the oldest available lane wins until it pauses, hits a login
    wall, or misses the normal 10-minute handoff window.
    """
    if not row['ig_id']:
        return True
    peers = conn.execute('SELECT * FROM accounts WHERE ig_id=?', (row['ig_id'],)).fetchall()
    available = [peer for peer in peers if account_available(peer, now)]
    both = [peer for peer in available if peer['role'] == 'both']
    if both:
        available = both
    return not available or min(available, key=lambda peer: (peer['first_seen'] or '', peer['lane_id']))['lane_id'] == row['lane_id']


def identity_handoff_pending(conn, row, now):
    """Wait for a previous lane's live request before the new owner starts."""
    if not row['ig_id']:
        return False
    return bool(conn.execute(
        "SELECT 1 FROM jobs j JOIN accounts a ON a.lane_id=j.lane "
        "WHERE a.ig_id=? AND j.lane!=? AND j.state='leased' AND j.leased_until>? LIMIT 1",
        (row['ig_id'], row['lane_id'], iso(now))).fetchone())


def identity_cooling(conn, row, kind, now):
    """A second browser profile cannot evade this Instagram identity's cooldown."""
    field = 'list_cool_until' if kind == 'list' else 'profile_cool_until'
    if not row['ig_id']:
        return later(row[field], now)
    return any(later(peer[0], now) for peer in conn.execute(
        f'SELECT {field} FROM accounts WHERE ig_id=? UNION ALL '
        f'SELECT {field} FROM account_identity_state WHERE ig_id=?', (row['ig_id'], row['ig_id'])))


def list_budget_left(conn, row, now):
    return request_budget_left(conn, row, 'list', now)


def request_budget_left(conn, row, kind, now):
    peers = conn.execute('SELECT * FROM accounts WHERE ig_id=?', (row['ig_id'],)).fetchall() if row['ig_id'] else [row]
    # Respect the strictest configured budget when one identity moves between profiles.
    limits = [budget_of(conn, peer).get(kind, 0) for peer in peers]
    limits = [limit for limit in limits if limit > 0]  # zero means unlimited
    if not limits:
        return True
    limit = min(limits)
    day = local_day(now)
    by_lane = {saved['lane_id']: (jload(saved['today'], {}) or {}).get(kind, 0)
               for saved in conn.execute('SELECT lane_id,today FROM account_identity_state WHERE ig_id=? AND day=?',
                                         (row['ig_id'], day))} if row['ig_id'] else {}
    for peer in peers:
        if peer['last_seen'] and local_day(utc(peer['last_seen'])) == day:
            by_lane[peer['lane_id']] = max(by_lane.get(peer['lane_id'], 0), (jload(peer['today'], {}) or {}).get(kind, 0))
    used = sum(by_lane.values())
    return used < limit


def main_bios_reserved(conn, row):
    """Ordinary bios belong to configured alternates, including while they rest."""
    return bool(row['is_main'] and conn.execute(
        "SELECT 1 FROM accounts WHERE is_main=0 AND role IN ('bios','both') LIMIT 1").fetchone())


def follower_route_wait(conn, row, now):
    """Keep failed follower routes out of the next recovery window.

    Recent recorded failures also cover jobs queued by versions that only saved
    a shared wait. No current shared hold or saved cursor is modified.
    """
    deadlines = [row['list_endpoint_until']] if later(row['list_endpoint_until'], now) else []
    events = conn.execute(
        "SELECT e.at FROM collector_events e JOIN jobs j ON j.id=e.job_id "
        "WHERE e.lane=? AND e.at>=? AND e.kind='list' AND e.direction='followers' "
        "AND e.reason='list_html_home_redirect' AND j.viewer_ig_id IS ? "
        "ORDER BY e.at DESC LIMIT 2",
        (row['lane_id'], iso(now - timedelta(hours=6)), row['ig_id'])).fetchall()
    if events:
        latest = utc(events[0]['at'])
        repeated = len(events) > 1 and latest - utc(events[1]['at']) <= timedelta(hours=2)
        deadline = latest + timedelta(hours=4 if repeated else 2)
        if deadline > now:
            deadlines.append(iso(deadline))
    return max(deadlines, key=utc) if deadlines else None


def list_share(conn, rows):
    """Zero share reserves the main even when configured alternates cannot work.

    A pool-wide outage, pause or budget exhaustion is not target-specific access
    evidence. Only an installation without any alternate list lane falls back
    to the main automatically; deliberate positive shares remain supported.
    """
    share = float(db.get_setting(conn, 'main_list_share') or 0)
    if share > 0:
        return share
    others = [r for r in rows if not r['is_main']
              and (r['role'] or 'both') in ('lists', 'both')]
    return 0.0 if others else 1.0


def keeps_lists(conn, row, now, share=0.0):
    """Healthy and allowed to work lists (role, main-account protection)."""
    return healthy(row, now) and identity_owner(conn, row, now) and not identity_cooling(conn, row, 'list', now) \
        and list_budget_left(conn, row, now) and (row['role'] or 'both') in ('lists', 'both') \
        and (not row['is_main'] or share > 0)


def viewer_may_access_list(conn, row, seed, direction):
    """Access is unknown until this viewer is denied, even while offline or cooling."""
    if (row['role'] or 'both') not in ('lists', 'both'):
        return False
    if not row['ig_id']:
        return not conn.execute('SELECT 1 FROM list_private_denials WHERE seed=? AND direction=? LIMIT 1',
                                (seed, direction)).fetchone()
    return not conn.execute(
        'SELECT 1 FROM list_private_denials WHERE seed=? AND direction=? AND viewer_ig_id=?',
        (seed, direction, row['ig_id'])).fetchone()


def eligible_for_list(conn, row, seed, direction, now):
    """The viewer can take this list right now."""
    return healthy(row, now) and identity_owner(conn, row, now) and role_allows(row, 'list') \
        and not identity_cooling(conn, row, 'list', now) \
        and not (direction == 'followers' and follower_route_wait(conn, row, now)) \
        and list_budget_left(conn, row, now) and viewer_may_access_list(
        conn, row, seed, direction)


def reopen_private_for_viewer(conn, row, now):
    """A newly usable identity reopens only lists stopped after access denials; keep the saved prefix."""
    if not row['ig_id'] or not healthy(row, now) or not identity_owner(conn, row, now) \
            or (row['role'] or 'both') not in ('lists', 'both'):
        return
    for lst in conn.execute("SELECT DISTINCT l.seed,l.direction FROM list_private_denials seen "
                            "JOIN lists l ON l.seed=seen.seed AND l.direction=seen.direction WHERE "
                            "(l.state='private' OR (l.state='partial' AND l.released_why='private')) "
                            "AND NOT EXISTS(SELECT 1 FROM list_private_denials d WHERE d.seed=l.seed AND d.direction=l.direction AND d.viewer_ig_id=?)",
                            (row['ig_id'],)).fetchall():
        if conn.execute("SELECT 1 FROM jobs WHERE kind='list' AND seed=? AND direction=? AND state IN ('queued','leased')",
                        (lst['seed'], lst['direction'])).fetchone():
            continue
        job_id = conn.execute('INSERT INTO jobs(kind,seed,direction,priority,created_at) VALUES(?,?,?,?,?)',
                              ('list', lst['seed'], lst['direction'], 0, db.now())).lastrowid
        db.start_list_run(conn, job_id, lst['seed'], lst['direction'])
        conn.execute("UPDATE lists SET state='queued',error=NULL,updated_at=? WHERE seed=? AND direction=?",
                     (db.now(), lst['seed'], lst['direction']))


def why_released(row, now):
    if row is None:
        return 'removed'
    if row['hold']:
        return row['hold']
    if row['paused']:
        return 'paused'
    if row['role'] == 'bios' or row['is_main']:
        return 'role'
    if later(row['list_cool_until'], now):
        return 'cooldown'
    return 'offline'


def release(conn, now, only=None):
    """Give back the leases and list ownership of lanes that are not healthy. Caller commits.
    A healthy lane changing role (or yielding lists to a new account) and a paused lane may still be
    finishing a request. Keep that lease until its callback or expiry; login and list cooldowns hand off now."""
    # Most polls have no ownership to clean up. Only lanes holding a live lease
    # or an unfinished list can affect this release; checking every account's
    # identity, cooldown and budget on every poll grows with the whole pool.
    lane_filter = ' AND lane=?' if only is not None else ''
    lane_args = (only,) if only is not None else ()
    leased = list(conn.execute(
        "SELECT id, kind, lane, leased_until FROM jobs WHERE state='leased' AND lane IS NOT NULL" + lane_filter,
        lane_args))
    owned = list(conn.execute(
        "SELECT seed, direction, lane FROM lists WHERE lane IS NOT NULL "
        "AND state NOT IN ('done','private','error','partial')" + lane_filter, lane_args))
    list_lanes = {j['lane'] for j in leased if j['kind'] == 'list'} | {x['lane'] for x in owned}
    profile_lanes = {j['lane'] for j in leased if j['kind'] == 'profile'}
    if not list_lanes and not profile_lanes:
        return 0
    rows = {r['lane_id']: r for r in conn.execute('SELECT * FROM accounts')}
    share = list_share(conn, rows.values()) if list_lanes else 0.0
    ok = {k for k in list_lanes if k in rows and keeps_lists(conn, rows[k], now, share)}
    fine = {k for k in profile_lanes if k in rows and account_available(rows[k], now)
            and identity_owner(conn, rows[k], now)}
    ts = iso(now)
    jobs, held = [], set()
    for j in leased:
        if j['lane'] in (ok if j['kind'] == 'list' else fine):
            continue
        row = rows.get(j['lane'])
        # A role/share/pause change cannot undo a request already sent to Instagram. A login wall,
        # list limit, offline lane, or expired lease can be handed off immediately.
        finishing = (row and (j['leased_until'] or '') > ts and not row['hold']
                     and (j['kind'] != 'list' or not later(row['list_cool_until'], now))
                     and (row['paused'] or (healthy(row, now) if j['kind'] == 'list' else account_available(row, now))))
        if finishing:
            if j['kind'] == 'list':
                held.add(j['lane'])
        else:
            jobs.append(j)
    lists = [x for x in owned if x['lane'] not in ok and x['lane'] not in held]
    for j in jobs:
        conn.execute("UPDATE jobs SET state='queued', leased_until=NULL, lane=NULL, lease_token=NULL, attempts=max(attempts-1, 0) WHERE id=?", (j['id'],))
    for x in lists:
        conn.execute("UPDATE lists SET lane=NULL, prev_lane=?, released_at=?, released_why=?, "
                     "state=CASE WHEN state='running' THEN 'queued' ELSE state END WHERE seed=? AND direction=?",
                     (x['lane'], ts, why_released(rows.get(x['lane']), now), x['seed'], x['direction']))
    return len(jobs) + len(lists)


def release_all(conn, lane, why=None):
    """Everything this lane holds, now (login wall, challenge, removed)."""
    ts = db.now()
    n = conn.execute("UPDATE jobs SET state='queued', leased_until=NULL, lane=NULL, lease_token=NULL, attempts=max(attempts-1, 0) "
                     "WHERE state='leased' AND lane=?", (lane,)).rowcount
    row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()
    why = why or why_released(row, db.utc_now())
    return n + conn.execute("UPDATE lists SET lane=NULL, prev_lane=?, released_at=?, released_why=?, "
                            "state=CASE WHEN state='running' THEN 'queued' ELSE state END "
                            "WHERE lane=? AND state NOT IN ('done','private','error','partial')", (lane, ts, why, lane)).rowcount


def note_handoff(conn, seed, direction, frm, to, why):
    log = db.get_setting(conn, 'handoffs') or []
    log.append({'seed': seed, 'direction': direction, 'from': frm, 'to': to, 'why': why, 'at': db.now()})
    db.set_setting(conn, 'handoffs', log[-HANDOFFS_KEEP:])


# ---------- leasing ----------

def kinds_for(conn, row, kinds, now):
    if identity_handoff_pending(conn, row, now):
        return []
    role = row['role'] if row['role'] in ROLES else 'both'
    allowed = {'lists': ['list'], 'bios': ['profile'], 'both': ['list', 'profile']}[role]
    # The main lane may still take a particular list that every alt cannot view.
    # Its normal share is checked in pick_job, after that list is known.
    return [k for k in kinds if k in allowed and identity_owner(conn, row, now)
            and request_budget_left(conn, row, k, now)
            and not identity_cooling(conn, row, k, now)
            and not (k == 'profile' and main_bios_reserved(conn, row))]


def pending_profile_jobs(conn, job):
    """Read only one indexed target group, never the full profile backlog."""
    handle = db.norm_handle(job['handle'])
    target = job['target_ig_id'] if 'target_ig_id' in job.keys() else None
    person = conn.execute('SELECT ig_id,handle FROM people WHERE ' + ('ig_id=?' if target else 'handle=?'),
                          (target or handle,)).fetchone()
    target = target or (person['ig_id'] if person else None)
    canonical = person['handle'] if person else handle
    aliases = tuple(dict.fromkeys((job['handle'], handle, canonical, '@' + canonical)))
    slots = ','.join('?' * len(aliases))
    rows = conn.execute("SELECT j.*,p.ig_id AS known_ig_id,p.handle AS known_handle,"
                        "bound.handle AS bound_handle FROM jobs j "
                        "LEFT JOIN people p ON p.handle=j.handle COLLATE NOCASE "
                        "LEFT JOIN people bound ON bound.ig_id=j.target_ig_id "
                        "WHERE j.id IN (SELECT id FROM jobs WHERE kind='profile' AND state IN ('queued','leased') "
                        "AND target_ig_id=? UNION SELECT id FROM jobs WHERE kind='profile' "
                        f"AND state IN ('queued','leased') AND handle COLLATE NOCASE IN ({slots})) ORDER BY j.id",
                        (target, *aliases)).fetchall()
    result = []
    for raw in rows:
        row = dict(raw)
        handle = db.norm_handle(row['handle'])
        # Legacy callers occasionally saved an @handle or profile URL.
        if not row['known_ig_id'] and handle != row['handle']:
            known = conn.execute('SELECT ig_id,handle FROM people WHERE handle=?', (handle,)).fetchone()
            if known:
                row['known_ig_id'], row['known_handle'] = known['ig_id'], known['handle']
        row_target = row['target_ig_id'] or row['known_ig_id']
        if target and row_target and row_target != target:
            continue  # a reused handle must not inherit an older identity's request
        row_canonical = row['bound_handle'] or (row['known_handle'] if not row['target_ig_id'] else None) or handle
        if row_target != row['target_ig_id'] or row_canonical != row['handle']:
            renamed = row_canonical != handle and row['state'] == 'leased'
            conn.execute("UPDATE jobs SET target_ig_id=?,handle=?,"
                         "state=CASE WHEN ? THEN 'queued' ELSE state END,"
                         "lane=CASE WHEN ? THEN NULL ELSE lane END,"
                         "lease_token=CASE WHEN ? THEN NULL ELSE lease_token END,"
                         "leased_until=CASE WHEN ? THEN NULL ELSE leased_until END WHERE id=?",
                         (row_target, row_canonical, renamed, renamed, renamed, renamed, row['id']))
            row.update(target_ig_id=row_target, handle=row_canonical)
            if renamed:
                row.update(state='queued', lane=None, lease_token=None, leased_until=None)
        row['target_key'] = ('id', row_target) if row_target else ('handle', row_canonical)
        result.append(row)
    return result


def profile_job_siblings(conn, job):
    rows = pending_profile_jobs(conn, job)
    match = next((r for r in rows if r['id'] == job['id']), None)
    if match:
        return [r for r in rows if r['target_key'] == match['target_key']]
    target = job['target_ig_id']
    handle = db.norm_handle(job['handle'])
    return [r for r in rows if (target and r['target_ig_id'] == target) or r['handle'] == handle]


def coalesce_profile_jobs(conn, job, now=None):
    """Keep one pending request per target, retaining cancelled rows as history.

    Caller owns the write transaction. Newest requested time, longest delay and
    highest manual priority survive; a coalesced row is never reported as read.
    """
    now = now or datetime.now(timezone.utc)
    groups = {}
    for row in pending_profile_jobs(conn, job):
        groups.setdefault(row['target_key'], []).append(row)
    active = (db.get_setting(conn, 'instagram_request_gate') or {}).get('active') or {}
    history = []
    for rows in groups.values():
        if len(rows) < 2:
            continue
        leased = [r for r in rows if r['state'] == 'leased' and later(r['leased_until'], now)]
        winner = next((r for r in leased if active.get('kind') == 'profile' and r['lane'] == active.get('lane')),
                      leased[0] if leased else rows[0])
        retries = [r['retry_not_before'] for r in rows if r['retry_not_before']]
        retry = max(retries, key=utc) if retries else None
        created = [r['created_at'] for r in rows if r['created_at']]
        created_at = max(created, key=utc) if created else None
        delayed = later(retry, now)
        conn.execute("UPDATE jobs SET priority=?,attempts=?,limit_hits=?,retry_not_before=?,created_at=?,"
                     "state=CASE WHEN ? THEN 'queued' ELSE state END,"
                     "lane=CASE WHEN ? THEN NULL ELSE lane END,"
                     "lease_token=CASE WHEN ? THEN NULL ELSE lease_token END,"
                     "leased_until=CASE WHEN ? THEN NULL ELSE leased_until END WHERE id=?",
                     (max(r['priority'] or 0 for r in rows), max(r['attempts'] or 0 for r in rows),
                      max(r['limit_hits'] or 0 for r in rows), retry, created_at,
                      delayed, delayed, delayed, delayed, winner['id']))
        for row in rows:
            if row['id'] != winner['id']:
                conn.execute("UPDATE jobs SET state='cancelled',lane=NULL,lease_token=NULL,leased_until=NULL WHERE id=?",
                             (row['id'],))
                history.append({'id': row['id'], 'kept': winner['id'], 'reason': 'duplicate_profile_request', 'at': iso(now)})
    if history:
        previous = db.get_setting(conn, 'profile_jobs_coalesced') or []
        db.set_setting(conn, 'profile_jobs_coalesced', (previous + history)[-50:])
    # Return the surviving pending row in the shape used by the lease planner.
    survivor = next((row for rows in groups.values() for row in rows if row['id'] not in {h['id'] for h in history}), None)
    return conn.execute('SELECT j.*,NULL AS owner,NULL AS prev_lane,NULL AS released_why FROM jobs j WHERE j.id=?',
                        (survivor['id'],)).fetchone() if survivor else None


def pick_job(conn, lane, kinds, now, allow_page_size=True):
    """The next job for this lane (inside the caller's write transaction), or None. Lists first: its own list,
    then lists another lane left mid-way (they have a cursor), then by priority."""
    ts = iso(now)
    accts = conn.execute('SELECT * FROM accounts').fetchall()
    # Eligibility is stable inside this one write transaction. The main-account
    # fallbacks inspect the same lanes several times; keep their answers local
    # to this pick so the next request always sees fresh cooldowns and identity.
    owners, list_eligible = {}, {}

    def owner(r):
        key = r['lane_id']
        if key not in owners:
            owners[key] = identity_owner(conn, r, now)
        return owners[key]

    def eligible(r):
        key = r['lane_id']
        if key not in list_eligible:
            list_eligible[key] = (healthy(r, now) and owner(r)
                                  and not identity_cooling(conn, r, 'list', now)
                                  and list_budget_left(conn, r, now)
                                  and (r['role'] or 'both') in ('lists', 'both'))
        return list_eligible[key]

    share = list_share(conn, accts)
    ok = [r['lane_id'] for r in accts if eligible(r) and (not r['is_main'] or share > 0)]
    okm = ','.join('?' * len(ok)) or "''"
    row = next((r for r in accts if r['lane_id'] == lane), None)
    if not row or identity_handoff_pending(conn, row, now):
        return None
    kinds = [kind for kind in kinds if role_allows(row, kind) and owner(row)
             and not (kind == 'profile' and main_bios_reserved(conn, row))]
    if not kinds:
        return None
    marks = ','.join('?' * len(kinds))
    # This viewer's follower endpoint may redirect while following still works.
    # Keep the other direction eligible and let a healthy viewer take its queued followers.
    route_waiting = [r['lane_id'] for r in accts if follower_route_wait(conn, r, now)]
    follower_filter = " AND (j.kind!='list' OR j.direction!='followers')" if lane in route_waiting else ''
    page_size_filter = '' if allow_page_size else ' AND j.page_size IS NULL'
    # The same recovered route wait must govern eligibility and sticky ownership.
    # Heartbeats can omit list_endpoint_until while recorded redirects still block it.
    owner_filter = (" OR (j.direction='followers' AND l.lane IN ("
                    + ','.join('?' for _ in route_waiting) + '))') if route_waiting else ''
    regular_main = True
    if row['is_main'] and 'list' in kinds and share < 1:
        since = iso(now - timedelta(hours=1))
        own, total = conn.execute('SELECT count(CASE WHEN lane=? THEN 1 END), count(*) FROM pages WHERE at>=?',
                                  (lane, since)).fetchone()
        regular_main = share > 0 and (not total or own < share * total)
    # The denial condition belongs in SQL, before LIMIT, so a long backlog of
    # inaccessible lists cannot starve later work or create a Python full scan.
    viewer_filter = ("NOT EXISTS(SELECT 1 FROM list_private_denials d "
                     "WHERE d.seed=j.seed AND d.direction=j.direction AND d.viewer_ig_id=?)" if row['ig_id']
                     else "NOT EXISTS(SELECT 1 FROM list_private_denials d "
                          "WHERE d.seed=j.seed AND d.direction=j.direction)")
    viewer_args = (row['ig_id'],) if row['ig_id'] else ()
    # High-priority follower jobs can monopolize the queue while Instagram
    # redirects their list requests. Give the other direction one fair probe.
    # A failed following probe restores normal priority; a saved following page
    # keeps that direction eligible while follower redirects are fresh.
    prefer_following = False
    if 'list' in kinds:
        recent = iso(now - timedelta(hours=1))
        follower_redirects = conn.execute(
            "SELECT count(*) FROM lists WHERE direction='followers' AND updated_at>=? "
            "AND error LIKE '%list_html_home_redirect%'", (recent,)).fetchone()[0]
        if follower_redirects >= 3:
            following_errors = conn.execute(
                "SELECT 1 FROM lists WHERE direction='following' AND updated_at>=? "
                "AND error IS NOT NULL LIMIT 1", (recent,)).fetchone()
            following_pages = conn.execute(
                "SELECT 1 FROM pages p JOIN jobs j ON j.id=p.job_id WHERE j.direction='following' "
                "AND p.at>=? LIMIT 1", (recent,)).fetchone()
            prefer_following = bool(following_pages or not following_errors)
    if row['is_main'] and not regular_main:
        # Drive this exceptional lookup from the small set of denied lists,
        # rather than scanning every ordinary queued list on each poll.
        alt_ids = sorted({r['ig_id'] for r in accts if not r['is_main'] and r['ig_id']
                          and (r['role'] or 'both') in ('lists', 'both')})
        denied_alts = ''.join(" AND EXISTS(SELECT 1 FROM list_private_denials a "
                              "WHERE a.seed=j.seed AND a.direction=j.direction AND a.viewer_ig_id=?)"
                              for _ in alt_ids)
        if any(not r['is_main'] and not r['ig_id']
               and (r['role'] or 'both') in ('lists', 'both') for r in accts):
            denied_alts += ' AND 0'  # An unidentified alternate has not proven lack of access.
        fallback = conn.execute(
            f"""SELECT j.*, l.lane AS owner, l.prev_lane, l.released_why FROM
            (SELECT DISTINCT seed,direction FROM list_private_denials) d
            JOIN jobs j ON j.kind='list' AND j.seed=d.seed AND j.direction=d.direction
            LEFT JOIN lists l ON l.seed=j.seed AND l.direction=j.direction
            WHERE (j.state='queued' OR (j.state='leased' AND j.leased_until<?))
              AND (j.retry_not_before IS NULL OR j.retry_not_before<=?)
              AND (l.lane IS NULL OR l.lane=? OR l.lane NOT IN ({okm}){owner_filter})
              AND {viewer_filter}{denied_alts}{follower_filter}{page_size_filter}
            ORDER BY coalesce(l.lane=?, 0) DESC, j.priority DESC,
              l.cursor IS NOT NULL DESC, coalesce(l.state='running', 0) DESC,
              coalesce(j.direction='following', 0) DESC, j.id LIMIT 1""",
            (ts, ts, lane, *ok, *route_waiting, *viewer_args, *alt_ids, lane)).fetchone()
        if fallback:
            return fallback
        if 'profile' not in kinds:
            return None
        kinds = ['profile']
        marks = '?'
    query = f"""SELECT j.*, l.lane AS owner, l.prev_lane, l.released_why FROM jobs j
        LEFT JOIN lists l ON j.kind='list' AND l.seed=j.seed AND l.direction=j.direction
        WHERE j.kind IN ({marks}) AND (j.state='queued' OR (j.state='leased' AND j.leased_until<?))
          AND (j.retry_not_before IS NULL OR j.retry_not_before<=?)
          AND (j.kind!='profile' OR NOT EXISTS (
            SELECT 1 FROM jobs busy WHERE busy.kind='profile' AND busy.state='leased'
              AND busy.id!=j.id AND busy.leased_until>=?
              AND ((j.target_ig_id IS NOT NULL AND busy.target_ig_id=j.target_ig_id)
                   OR busy.handle=j.handle COLLATE NOCASE)))
          AND (j.kind='profile' OR l.lane IS NULL OR l.lane=? OR l.lane NOT IN ({okm}){owner_filter})
          AND (j.kind='profile' OR {viewer_filter}){follower_filter}{page_size_filter}
        ORDER BY j.kind='list' DESC,
          CASE WHEN ? AND j.kind='list' AND j.direction='following' THEN 1 ELSE 0 END DESC,
          coalesce(l.lane=?, 0) DESC, j.priority DESC, l.cursor IS NOT NULL DESC,
          coalesce(l.state='running', 0) DESC, coalesce(j.direction='following', 0) DESC, j.id LIMIT 1"""
    args = (*kinds, ts, ts, ts, lane, *ok, *route_waiting, *viewer_args, prefer_following, lane)
    while True:
        candidate = conn.execute(query, args).fetchone()
        if not candidate or candidate['kind'] != 'profile':
            return candidate
        survivor = coalesce_profile_jobs(conn, candidate, now)
        if survivor and not later(survivor['retry_not_before'], now) and (
                survivor['state'] == 'queued' or not later(survivor['leased_until'], now)):
            return survivor
        # Coalescing can reveal a longer target delay or an existing live lease.
        # That indexed group is now excluded; select the next eligible target.


def took(conn, lane, job):
    """Bookkeeping after `job` was leased to `lane`."""
    if job['kind'] != 'list':
        return
    frm = job['owner'] or job['prev_lane']
    if frm and frm != lane:
        note_handoff(conn, job['seed'], job['direction'], frm, lane, job['released_why'] or 'offline')
    conn.execute('UPDATE lists SET lane=?, prev_lane=NULL, released_at=NULL, released_why=NULL WHERE seed=? AND direction=?',
                 (lane, job['seed'], job['direction']))


# ---------- views ----------

def name_of(row):
    if not row:
        return 'removed account'
    return '@' + row['handle'] if row['handle'] else (row['label'] or 'lane ' + row['lane_id'][:8])


def list_wait_until(row, now):
    waits = [v for v in (row['list_cool_until'],) if later(v, now)]
    return max(waits, key=utc) if waits else None


def full_cooldown(row, now):
    """An account is resting only when every request type in its role is cooling."""
    role = row['role'] if row['role'] in ROLES else 'both'
    kinds = {'lists': ('list',), 'bios': ('profile',), 'both': ('list', 'profile')}[role]
    untils = [list_wait_until(row, now) if k == 'list' else row['profile_cool_until'] for k in kinds]
    return min(untils, key=utc) if untils and all(later(x, now) for x in untils) else None


def status_of(row, now, conn=None):
    seen = row['last_seen'] and now - utc(row['last_seen']) < ONLINE_FOR
    if row['hold']:
        return 'needs_login' if row['hold'] == 'login' else 'challenge'
    if not seen:
        return 'offline'
    if row['paused'] or (conn is not None and db.get_setting(conn, 'paused')):
        return 'paused'
    if full_cooldown(row, now):
        return 'cooldown'
    if conn is not None and conn.execute(
            "SELECT 1 FROM jobs WHERE state='leased' AND lane=? AND leased_until>? LIMIT 1",
            (row['lane_id'], iso(now))).fetchone():
        return 'running'
    return 'online'


def out(conn, row, now, include_lists=True):
    since = iso(now - timedelta(hours=1))
    pages, people = conn.execute('SELECT count(*), coalesce(sum(users), 0) FROM pages WHERE lane=? AND at>=?',
                                 (row['lane_id'], since)).fetchone()
    job = conn.execute("SELECT kind, seed, direction, handle FROM jobs WHERE state='leased' AND lane=? AND leased_until>? "
                       'ORDER BY id DESC LIMIT 1', (row['lane_id'], iso(now))).fetchone()
    owns = [dict(r) for r in conn.execute("SELECT seed, direction, received, total FROM lists WHERE lane=? "
                                          "AND state NOT IN ('done','private','error','partial') ORDER BY updated_at DESC", (row['lane_id'],))] \
        if include_lists else []
    rate = jload(row['rate'])
    today = jload(row['today']) or {}
    saved = conn.execute('SELECT day FROM account_identity_state WHERE lane_id=? AND ig_id=?',
                         (row['lane_id'], row['ig_id'])).fetchone() if row['ig_id'] else None
    if (saved['day'] if saved else local_day(utc(row['last_seen'])) if row['last_seen'] else None) != local_day(now):
        today = {}
    own_budget = jload(row['budget'])
    return {'lane_id': row['lane_id'], 'ig_id': row['ig_id'], 'handle': row['handle'], 'label': row['label'], 'name': name_of(row),
            'role': row['role'], 'budget': budget_of(conn, row), 'budget_custom': isinstance(own_budget, dict),
            'paused': bool(row['paused']), 'is_main': bool(row['is_main']), 'first_seen': row['first_seen'],
            'last_seen': row['last_seen'], 'version': row['version'], 'state': row['state'], 'hold': row['hold'],
            'status': status_of(row, now, conn), 'online': bool(row['last_seen']) and now - utc(row['last_seen']) < ONLINE_FOR,
            'healthy': healthy(row, now), 'cooldown_until': full_cooldown(row, now),
            'list_endpoint_until': follower_route_wait(conn, row, now),
            'cool': {'list': list_wait_until(row, now),
                     'profile': row['profile_cool_until'] if later(row['profile_cool_until'], now) else None},
            'rate': rate, 'last_limit': (rate or {}).get('last_hit_at'), 'last_error': row['last_error'],
            'today': {'list': today.get('list', 0), 'profile': today.get('profile', 0)},
            'hour': {'pages': pages, 'people': people}, 'activity': row['activity'], 'text': row['text'],
            'job': dict(job) if job else None, 'lists': owns}


def rows(conn):
    return conn.execute('SELECT * FROM accounts ORDER BY is_main DESC, first_seen, lane_id').fetchall()


def listing(conn, now=None, include_lists=True):
    now = now or db.utc_now()
    return [out(conn, r, now, include_lists=include_lists) for r in rows(conn)]


def alerts(conn, now=None, accts=None):
    """Plain sentences for the UI, most urgent first."""
    now = now or db.utc_now()
    accts = accts if accts is not None else listing(conn, now)
    by = {a['lane_id']: a for a in accts}
    recent = iso(now - timedelta(hours=6))
    moves = [h for h in db.get_setting(conn, 'handoffs') or [] if h.get('at', '') >= recent]
    waiting = {r[0] for r in conn.execute(
        "SELECT DISTINCT prev_lane FROM lists WHERE lane IS NULL AND prev_lane IS NOT NULL AND state IN ('queued','running')")}

    def moved(lane):
        to = [h for h in moves if h['from'] == lane]
        if to:
            h = to[-1]
            return f"; its list moved to {by[h['to']]['name'] if h['to'] in by else 'another account'}"
        if lane in waiting:
            return '; its list waits for another account'
        return ''

    out_ = []
    for a in accts:
        if a['list_endpoint_until']:
            out_.append({'level': 'warn', 'code': 'list_endpoint_wait', 'lane_id': a['lane_id'],
                         'text': f"{a['name']} is getting Instagram home pages instead of public lists. Its list requests will retry later."})
        if a['hold'] == 'login':
            out_.append({'level': 'error', 'lane_id': a['lane_id'],
                         'text': f"{a['name']} logged out — open its Chrome profile and log in{moved(a['lane_id'])}"})
        elif a['hold'] == 'challenge':
            out_.append({'level': 'error', 'lane_id': a['lane_id'],
                         'text': f"{a['name']} hit a security check — open its Chrome profile and complete it{moved(a['lane_id'])}"})
        elif a['last_seen'] and now - utc(a['last_seen']) > RELEASE_AFTER:
            mins = int((now - utc(a['last_seen'])).total_seconds() // 60)
            span = f'{mins // 60}h {mins % 60}m' if mins >= 60 else f'{mins}m'
            out_.append({'level': 'warn', 'lane_id': a['lane_id'],
                         'text': f"{a['name']} offline for {span} — open its Chrome profile{moved(a['lane_id'])}"})
    seen = {}
    for a in accts:
        if a['ig_id']:
            seen.setdefault(a['ig_id'], []).append(a)
    for same in seen.values():
        if len(same) > 1:
            out_.append({'level': 'warn', 'lane_id': same[1]['lane_id'],
                         'text': f"{same[0]['name']} is logged in on {len(same)} Chrome profiles — log the extra ones into other accounts"})
            available = [a for a in same if a['last_seen'] and now - utc(a['last_seen']) <= RELEASE_AFTER
                         and not a['paused'] and not a['hold']]
            roles = {a['role'] for a in available}
            if 'both' not in roles and {'lists', 'bios'} <= roles:
                out_.append({'level': 'warn', 'lane_id': available[0]['lane_id'],
                             'text': f"{same[0]['name']} has lists and bios split across Chrome profiles. "
                                     'Set one profile to both so this Instagram account can do both stages safely.'})
    queued = conn.execute("SELECT count(*) FROM jobs WHERE kind='list' AND state IN ('queued','leased')").fetchone()[0]
    live = [a for a in accts if a['online'] and not a['paused'] and not a['hold']]
    if queued and live and not any(a['role'] in ('lists', 'both') for a in live):
        out_.append({'level': 'warn', 'lane_id': None, 'text': 'No online account takes lists — set one to lists or both'})
    elif queued and live and not any(a['role'] in ('lists', 'both') and not a['is_main'] for a in live) \
            and not float(db.get_setting(conn, 'main_list_share') or 0):
        out_.append({'level': 'info', 'lane_id': None,
                     'text': ('Your main account is reserved. Reconnect an alternate to continue lists.'
                              if any(not a['is_main'] and a['role'] in ('lists', 'both') for a in accts)
                              else 'Your main account is the only one online, so it reads lists too — add a second account to protect it')})
    return out_


def aggregate_rate(accts):
    live = [a for a in accts if a['online']]

    def total(k):
        vals = [(a['rate'] or {}).get(k) for a in live]
        vals = [v for v in vals if isinstance(v, (int, float))]
        return round(sum(vals), 1) if vals else None
    hits = [a['last_limit'] for a in accts if a['last_limit']]
    return {'pages_hour': total('pages_hour'), 'people_hour': total('people_hour'),
            'last_hit_at': max(hits) if hits else None, 'online': len(live), 'accounts': len(accts),
            'pages_last_hour': sum(a['hour']['pages'] for a in accts), 'people_last_hour': sum(a['hour']['people'] for a in accts)}


# ---------- edits ----------

def edit(conn, lane, b):
    row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()
    if not row:
        raise LookupError('no such account')
    sets = {}
    if 'role' in b:
        if b['role'] not in ROLES:
            raise ValueError('role must be lists, bios or both')
        sets['role'] = b['role']
    if 'budget' in b:
        v = b['budget']
        if v is None:
            sets['budget'] = None
        elif isinstance(v, dict):
            cur = jload(row['budget']) or {}
            for k, cap in BUDGET_MAX.items():
                if k in v:
                    x = v[k]
                    if x is None:
                        cur.pop(k, None)
                    elif isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= cap:
                        cur[k] = x
                    else:
                        raise ValueError(f'budget.{k} must be an integer from 0 to {cap}')
            sets['budget'] = json.dumps(cur) if cur else None
        else:
            raise ValueError('budget must be {list, profile} or null')
    for k in ('paused', 'is_main'):
        if k in b:
            if not isinstance(b[k], bool):
                raise ValueError(f'{k} must be true or false')
            sets[k] = int(b[k])
    if 'label' in b:
        v = b['label']
        if v is not None and (not isinstance(v, str) or len(v.strip()) > 40):
            raise ValueError('label must be up to 40 characters')
        sets['label'] = (v or '').strip() or None
    if sets:
        conn.execute(f"UPDATE accounts SET {', '.join(k + '=?' for k in sets)} WHERE lane_id=?", (*sets.values(), lane))
    if sets.get('paused'):
        release(conn, db.utc_now(), only=lane)
    return conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()


def remove(conn, lane):
    if not conn.execute('SELECT 1 FROM accounts WHERE lane_id=?', (lane,)).fetchone():
        return 0
    conn.execute('DELETE FROM accounts WHERE lane_id=?', (lane,))
    release_all(conn, lane)
    return 1


# Reuse the extension's minimum spacing across accounts; this is not a safe-rate claim.
REQUEST_SPACING_SECONDS = 2
REQUEST_LEASE_SECONDS = 90
REQUEST_QUEUE_MAX = 64


def request_permit(conn, lane, kind=None, token=None, now=None):
    """FIFO, persisted single-request lease; caller checks workspace controls first."""
    now = now or datetime.now(timezone.utc)
    stamp = now.timestamp()
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    try:
        state = db.get_setting(conn, 'instagram_request_gate') or {}
        active = state.get('active')
        matching_release = bool(token is not None and active and active['lane'] == lane and active['token'] == token)
        if active and active['until'] <= stamp and not matching_release:
            # Expiry proves the worker stopped reporting, not that its browser stopped.
            # Pause new collection until the owner checks the tab and explicitly resumes.
            message = 'Collection paused: an Instagram request did not confirm completion. Check the account tab before resuming collection.'
            db.set_setting(conn, 'paused_lists', True)
            db.set_setting(conn, 'paused_bios', True)
            db.set_setting(conn, 'instagram_request_attention', {'lane': active['lane'], 'at': iso(now), 'message': message})
            conn.execute('UPDATE accounts SET last_error=? WHERE lane_id=?', (message, active['lane']))
            active = None
            state['queue'] = []
        queue = [item for item in state.get('queue', []) if item['seen'] > stamp - REQUEST_LEASE_SECONDS]
        # A paused/disconnected waiting account must not block healthy waiters.
        live = {row['lane_id']: row for row in conn.execute('SELECT * FROM accounts')
                if not row['paused'] and not row['hold'] and row['last_seen']
                and utc(row['last_seen']).timestamp() > stamp - REQUEST_LEASE_SECONDS}
        allowed = {kind for kind, stage in (('list', 'lists'), ('profile', 'bios'))
                   if not db.get_setting(conn, 'paused') and not db.get_setting(conn, 'paused_' + stage)}
        eligibility = {}

        def may_request(candidate, request_kind):
            key = (candidate, request_kind)
            if key not in eligibility:
                row = live.get(candidate)
                eligibility[key] = bool(row is not None and request_kind in allowed
                                        and role_allows(row, request_kind)
                                        and not (request_kind == 'profile' and main_bios_reserved(conn, row))
                                        and identity_owner(conn, row, now)
                                        and not identity_handoff_pending(conn, row, now)
                                        and not identity_cooling(conn, row, request_kind, now)
                                        and request_budget_left(conn, row, request_kind, now))
            return eligibility[key]

        # Controls can change while a cached job waits for its turn. Remove
        # ineligible waiters as well as refusing their own acquire, otherwise
        # they would remain at the front and stall every healthy account.
        queue = [item for item in queue if may_request(item['lane'], item['kind'])]
        next_at = state.get('next_at', 0)
        if token is not None:
            released = bool(active and active['lane'] == lane and active['token'] == token)
            if released:
                active = None
                next_at = max(next_at, stamp + REQUEST_SPACING_SECONDS)
            result = {'released': released}
        else:
            if kind not in ('list', 'profile'):
                raise ValueError('kind must be list or profile')
            cooling = db.get_setting(conn, 'cooldown')
            try:
                cooldown = utc(cooling) if cooling else None
            except (ValueError, TypeError, AttributeError):
                cooldown = None
            if not may_request(lane, kind) or (cooling and (cooldown is None or cooldown > now)):
                result = {'granted': False, 'wait_ms': 15000}
            elif active and active['lane'] == lane:
                result = {'granted': False, 'wait_ms': 2000}
            else:
                waiting = next((item for item in queue if item['lane'] == lane), None)
                if waiting:
                    waiting['seen'] = stamp
                    waiting['kind'] = kind
                elif len(queue) < REQUEST_QUEUE_MAX:
                    queue.append({'lane': lane, 'kind': kind, 'seen': stamp})
                if not active and stamp >= next_at and queue and queue[0]['lane'] == lane:
                    queue.pop(0)
                    db.set_setting(conn, 'instagram_request_attention', None)
                    active = {'lane': lane, 'kind': kind, 'token': secrets.token_hex(24),
                              'until': stamp + REQUEST_LEASE_SECONDS}
                    next_at = stamp + REQUEST_SPACING_SECONDS
                    result = {'granted': True, 'token': active['token'], 'expires_at': iso(now + timedelta(seconds=REQUEST_LEASE_SECONDS))}
                else:
                    wait = REQUEST_SPACING_SECONDS if active else next_at - stamp
                    result = {'granted': False, 'wait_ms': max(1000, min(15000, int(wait * 1000)))}
        db.set_setting(conn, 'instagram_request_gate', {'active': active, 'queue': queue, 'next_at': next_at})
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
