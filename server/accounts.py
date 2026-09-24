"""Lanes: one Chrome profile = one extension install = one Instagram account, all feeding this server.

A lane is identified by the `lane_id` its extension keeps in chrome.storage (old builds send none and are the
`default` lane). Pacing, budgets and cooldowns stay inside each extension; the server decides who gets which job:
  - a leased job is never handed to another lane before its lease ends;
  - a list sticks to the lane that started it (`lists.lane`) while that lane is healthy: seen within 10 min, not
    logged out / challenged, not paused and not in a list cooldown. Otherwise the list is released (`prev_lane` kept)
    and the next lane resumes it from the saved cursor;
  - role lists|bios|both filters the job kinds; a main account (is_main) gets bios only (or lists up to
    `main_list_share` of the last hour's pages) while another healthy account takes lists; alone, it takes lists too.
"""
import json
import re
from datetime import datetime, timedelta, timezone

import db

DEFAULT_LANE = 'default'
LANE_RX = re.compile(r'[A-Za-z0-9_-]{1,64}')
ROLES = ('lists', 'bios', 'both')
HOLDS = ('login', 'challenge')
RELEASE_AFTER = timedelta(minutes=10)   # offline this long: its list moves on
ONLINE_FOR = timedelta(seconds=90)      # heartbeats come every <= 30 s
BUDGET_MAX = {'list': 3000, 'profile': 5000}   # per day; profile 0 = no daily number
HANDOFFS_KEEP = 30


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


def touch(conn, lane, acct=None, **fields):
    """Upsert the lane's row as seen now; `fields` are column values to set. Returns the row."""
    ts = db.now()
    row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()
    if not row:
        conn.execute('INSERT INTO accounts(lane_id, first_seen, last_seen) VALUES(?,?,?)', (lane, ts, ts))
        row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()
    sets = dict(fields, last_seen=ts)
    if acct and 'ig_id' in acct and not acct['ig_id']:
        sets['hold'] = 'login'   # the profile has no Instagram session at all
    elif acct:
        if acct.get('ig_id'):
            if row['ig_id'] and row['ig_id'] != acct['ig_id'] and not acct.get('handle'):
                sets['handle'] = None   # another account logged in; its handle follows
            sets['ig_id'] = acct['ig_id']
        if acct.get('handle'):
            sets['handle'] = acct['handle']
            if not row['handle'] and is_me(conn, acct['handle']):
                sets['is_main'] = 1   # Michael's own account: protected by default
    conn.execute(f"UPDATE accounts SET {', '.join(k + '=?' for k in sets)} WHERE lane_id=?", (*sets.values(), lane))
    return conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()


def budget_of(conn, row):
    b = db.get_setting(conn, 'budget')
    own = jload(row['budget']) if row else None
    if isinstance(own, dict):
        b.update({k: v for k, v in own.items() if k in BUDGET_MAX and isinstance(v, int)})
    return b


def paused_for(conn, row):
    return bool(db.get_setting(conn, 'paused')) or bool(row and row['paused'])


# ---------- health, release, handoff ----------

def healthy(row, now):
    """May this lane keep (or take) a list?"""
    return bool(row['last_seen']) and now - utc(row['last_seen']) <= RELEASE_AFTER and not row['hold'] \
        and not row['paused'] and not later(row['list_cool_until'], now)


def list_share(conn, rows, now):
    """The main account's list share: the setting, or 1.0 while no other healthy account takes lists (a lone main
    account used to get no lists at all, so one connected extension sat 'Idle, queue empty' on a full queue)."""
    share = float(db.get_setting(conn, 'main_list_share') or 0)
    if share > 0:
        return share
    others = [r for r in rows if not r['is_main'] and healthy(r, now) and (r['role'] or 'both') in ('lists', 'both')]
    return 0.0 if others else 1.0


def keeps_lists(row, now, share=0.0):
    """Healthy and allowed to work lists (role, main-account protection)."""
    return healthy(row, now) and (row['role'] or 'both') in ('lists', 'both') and (not row['is_main'] or share > 0)


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
    A paused lane may still be finishing the job it holds, so its leases go only once they expire."""
    rows = {r['lane_id']: r for r in conn.execute('SELECT * FROM accounts')}
    share = list_share(conn, rows.values(), now)
    ok = {k for k, r in rows.items() if keeps_lists(r, now, share)}
    ts = iso(now)
    fine = {k for k, r in rows.items() if healthy(r, now)}
    jobs = [j for j in conn.execute("SELECT id, kind, lane, leased_until FROM jobs WHERE state='leased' AND lane IS NOT NULL")
            if j['lane'] not in (ok if j['kind'] == 'list' else fine) and (only is None or j['lane'] == only)
            and not (j['lane'] in rows and rows[j['lane']]['paused'] and not rows[j['lane']]['hold'] and (j['leased_until'] or '') > ts)]
    held = {j['lane'] for j in conn.execute("SELECT lane FROM jobs WHERE state='leased' AND lane IS NOT NULL AND leased_until>?", (ts,))
            if j['lane'] in rows and rows[j['lane']]['paused'] and not rows[j['lane']]['hold']}
    lists = [x for x in conn.execute("SELECT seed, direction, lane FROM lists WHERE lane IS NOT NULL "
                                     "AND state NOT IN ('done','private','error')")
             if x['lane'] not in ok and x['lane'] not in held and (only is None or x['lane'] == only)]
    for j in jobs:
        conn.execute("UPDATE jobs SET state='queued', leased_until=NULL, lane=NULL, attempts=max(attempts-1, 0) WHERE id=?", (j['id'],))
    for x in lists:
        conn.execute("UPDATE lists SET lane=NULL, prev_lane=?, released_at=?, released_why=?, "
                     "state=CASE WHEN state='running' THEN 'queued' ELSE state END WHERE seed=? AND direction=?",
                     (x['lane'], ts, why_released(rows.get(x['lane']), now), x['seed'], x['direction']))
    return len(jobs) + len(lists)


def release_all(conn, lane):
    """Everything this lane holds, now (login wall, challenge, removed)."""
    ts = db.now()
    n = conn.execute("UPDATE jobs SET state='queued', leased_until=NULL, lane=NULL, attempts=max(attempts-1, 0) "
                     "WHERE state='leased' AND lane=?", (lane,)).rowcount
    row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()
    why = why_released(row, db.utc_now())
    return n + conn.execute("UPDATE lists SET lane=NULL, prev_lane=?, released_at=?, released_why=?, "
                            "state=CASE WHEN state='running' THEN 'queued' ELSE state END "
                            "WHERE lane=? AND state NOT IN ('done','private','error')", (lane, ts, why, lane)).rowcount


def note_handoff(conn, seed, direction, frm, to, why):
    log = db.get_setting(conn, 'handoffs') or []
    log.append({'seed': seed, 'direction': direction, 'from': frm, 'to': to, 'why': why, 'at': db.now()})
    db.set_setting(conn, 'handoffs', log[-HANDOFFS_KEEP:])


# ---------- leasing ----------

def kinds_for(conn, row, kinds, now):
    role = row['role'] if row['role'] in ROLES else 'both'
    allowed = {'lists': ['list'], 'bios': ['profile'], 'both': ['list', 'profile']}[role]
    kinds = [k for k in kinds if k in allowed]
    if 'list' in kinds and row['is_main']:
        share = list_share(conn, conn.execute('SELECT * FROM accounts').fetchall(), now)
        if share >= 1.0:
            return kinds
        since = iso(now - timedelta(hours=1))
        own, total = conn.execute('SELECT count(CASE WHEN lane=? THEN 1 END), count(*) FROM pages WHERE at>=?',
                                  (row['lane_id'], since)).fetchone()
        if share <= 0 or (total and own >= share * total):
            kinds.remove('list')
    return kinds


def pick_job(conn, lane, kinds, now):
    """The next job for this lane (inside the caller's write transaction), or None. Lists first: its own list,
    then lists another lane left mid-way (they have a cursor), then by priority."""
    ts = iso(now)
    accts = conn.execute('SELECT * FROM accounts').fetchall()
    share = list_share(conn, accts, now)
    ok = [r['lane_id'] for r in accts if keeps_lists(r, now, share)]
    marks = ','.join('?' * len(kinds))
    okm = ','.join('?' * len(ok)) or "''"
    return conn.execute(
        f"""SELECT j.*, l.lane AS owner, l.prev_lane, l.released_why FROM jobs j
        LEFT JOIN lists l ON j.kind='list' AND l.seed=j.seed AND l.direction=j.direction
        WHERE j.kind IN ({marks}) AND (j.state='queued' OR (j.state='leased' AND j.leased_until<?))
          AND (j.kind='profile' OR l.lane IS NULL OR l.lane=? OR l.lane NOT IN ({okm}))
        ORDER BY j.kind='list' DESC, coalesce(l.lane=?, 0) DESC, j.priority DESC, l.cursor IS NOT NULL DESC,
          coalesce(l.state='running', 0) DESC, coalesce(j.direction='following', 0) DESC, j.id LIMIT 1""",
        (*kinds, ts, lane, *ok, lane)).fetchone()


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


def status_of(row, now, conn=None):
    seen = row['last_seen'] and now - utc(row['last_seen']) < ONLINE_FOR
    if row['hold']:
        return 'needs_login' if row['hold'] == 'login' else 'challenge'
    if not seen:
        return 'offline'
    if row['paused'] or (conn is not None and db.get_setting(conn, 'paused')):
        return 'paused'
    if later(row['cooldown_until'], now):
        return 'cooldown'
    if row['state'] == 'running':
        return 'running'
    return 'online'


def out(conn, row, now):
    since = iso(now - timedelta(hours=1))
    pages, people = conn.execute('SELECT count(*), coalesce(sum(users), 0) FROM pages WHERE lane=? AND at>=?',
                                 (row['lane_id'], since)).fetchone()
    job = conn.execute("SELECT kind, seed, direction, handle FROM jobs WHERE state='leased' AND lane=? AND leased_until>? "
                       'ORDER BY id DESC LIMIT 1', (row['lane_id'], iso(now))).fetchone()
    owns = [dict(r) for r in conn.execute("SELECT seed, direction, received, total FROM lists WHERE lane=? "
                                          "AND state NOT IN ('done','private','error') ORDER BY updated_at DESC", (row['lane_id'],))]
    rate = jload(row['rate'])
    today = jload(row['today']) or {}
    if not (row['last_seen'] or '').startswith(iso(now)[:10]):
        today = {}
    own_budget = jload(row['budget'])
    return {'lane_id': row['lane_id'], 'ig_id': row['ig_id'], 'handle': row['handle'], 'label': row['label'], 'name': name_of(row),
            'role': row['role'], 'budget': budget_of(conn, row), 'budget_custom': isinstance(own_budget, dict),
            'paused': bool(row['paused']), 'is_main': bool(row['is_main']), 'first_seen': row['first_seen'],
            'last_seen': row['last_seen'], 'version': row['version'], 'state': row['state'], 'hold': row['hold'],
            'status': status_of(row, now, conn), 'online': bool(row['last_seen']) and now - utc(row['last_seen']) < ONLINE_FOR,
            'healthy': healthy(row, now), 'cooldown_until': row['cooldown_until'] if later(row['cooldown_until'], now) else None,
            'rate': rate, 'last_limit': (rate or {}).get('last_hit_at'), 'last_error': row['last_error'],
            'today': {'list': today.get('list', 0), 'profile': today.get('profile', 0)},
            'hour': {'pages': pages, 'people': people}, 'activity': row['activity'], 'text': row['text'],
            'job': dict(job) if job else None, 'lists': owns}


def rows(conn):
    return conn.execute('SELECT * FROM accounts ORDER BY is_main DESC, first_seen, lane_id').fetchall()


def listing(conn, now=None):
    now = now or db.utc_now()
    return [out(conn, r, now) for r in rows(conn)]


def alerts(conn, now=None, accts=None):
    """Plain sentences for the UI, most urgent first."""
    now = now or db.utc_now()
    accts = accts if accts is not None else listing(conn, now)
    by = {a['lane_id']: a for a in accts}
    recent = iso(now - timedelta(hours=6))
    moves = [h for h in db.get_setting(conn, 'handoffs') or [] if h.get('at', '') >= recent]
    waiting = {(r['seed'], r['direction']): r for r in conn.execute(
        "SELECT seed, direction, prev_lane FROM lists WHERE lane IS NULL AND prev_lane IS NOT NULL AND state IN ('queued','running')")}

    def moved(lane):
        to = [h for h in moves if h['from'] == lane]
        if to:
            h = to[-1]
            return f"; its list moved to {by[h['to']]['name'] if h['to'] in by else 'another account'}"
        if any(w['prev_lane'] == lane for w in waiting.values()):
            return '; its list waits for another account'
        return ''

    out_ = []
    for a in accts:
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
    queued = conn.execute("SELECT count(*) FROM jobs WHERE kind='list' AND state IN ('queued','leased')").fetchone()[0]
    live = [a for a in accts if a['online'] and not a['paused'] and not a['hold']]
    if queued and live and not any(a['role'] in ('lists', 'both') for a in live):
        out_.append({'level': 'warn', 'lane_id': None, 'text': 'No online account takes lists — set one to lists or both'})
    elif queued and live and not any(a['role'] in ('lists', 'both') and not a['is_main'] for a in live) \
            and not float(db.get_setting(conn, 'main_list_share') or 0):
        out_.append({'level': 'info', 'lane_id': None,
                     'text': 'Your main account is the only one online, so it reads lists too — add a second account to protect it'})
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
                    elif isinstance(x, (int, float)) and not isinstance(x, bool):
                        cur[k] = max(0, min(cap, int(x)))
                    else:
                        raise ValueError(f'budget.{k} must be a number')
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
