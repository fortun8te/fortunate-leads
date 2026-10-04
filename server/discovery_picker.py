"""Autonomous next-seed picker: scores people we already know and keeps the list queue topped up.

Nothing here talks to Instagram. It only decides which already-allowed list request is queued next, using
evidence we already hold: who follows whom in the lists we have collected, how many new people each
finished list actually returned, and the size of each candidate's own following list.

Two layers:
  * the scoring core (`Candidate`, `Context`, `score`, `choose`) is pure and has no database access, so the
    replay in ops/discovery_picker_replay.py evaluates exactly the code that runs live;
  * the database layer (`load_candidates`, `measure_runs`, `top_up`) reads saved evidence, queues the
    chosen lists with the normal queue and tags each one in `collection_discovery` as automatic.

What the evidence says (see docs/AUTO_SEEDS.md for the numbers):
  * the first page of any list is worth about the same as any other, so yield per request is the share of a
    list that is new to us; size only matters for the one-request lookup and for lists Instagram may cut short;
  * accounts that many collected lists already contain sit in a well-covered area, so their own lists return
    more people we already have (more overlap, fewer new people);
  * a neighbourhood that recently returned mostly known people is saturated, so its remaining candidates are
    moved down until something there proves otherwise.
Manual seeds always come first: automatic lists are queued at a lower priority than anything typed in.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import ceil, log10, log1p, sqrt
import json

import db

# ---- settings (stored in the normal settings table) ----
ENABLED_KEY = 'auto_discover'            # existing on/off switch, default on
DEPTH_KEY = 'auto_pick_depth'            # lists kept pending per active collector
DAILY_CAP_KEY = 'auto_pick_daily_cap'    # most automatic seeds added per local day
POLICY = 'adaptive_yield'                # value of auto_discover_policy that selects this picker
DEFAULT_DEPTH = 2
DEFAULT_DAILY_CAP = 12
MAX_DEPTH = 6
MAX_DAILY_CAP = 60
AUTO_PRIORITY = -10                      # manual lists are queued at 0 and above, so they always win

ROWS_PER_PAGE = 45                       # same planning figure as discovery_policy
MIN_FOLLOWING = 100                      # smaller lists cost a lookup and return almost nothing (6.6 new people per request under 100)
MAX_FOLLOWING = 6000                     # larger lists are slow to finish and Instagram may cut them short
UNKNOWN_FOLLOWING = 500                  # planning size when the profile has not been read yet
MIN_MEASURED_MEMBERS = 30                # a finished list this small says nothing about its neighbourhood

# model weights, tuned on the replay and checked on a held-out half (ops/discovery_picker_replay.py)
PRIOR_RATIO = 0.75                       # share of a list that is new when we know nothing
OVERLAP_WEIGHT = 0.1                     # ratio lost per log(1 + collected lists that already contain the account)
VERIFIED_WEIGHT = 0.08
SIZE_WEIGHT = 0.2                        # ratio gained per tenfold increase in how many accounts it follows
BUSINESS_WEIGHT = 0.0                    # business signals do not predict freshness; kept for lead relevance only
NEIGHBOUR_SHRINK = 3.0                   # lists of evidence before a neighbourhood outweighs the global rate
EXPLORE_WEIGHT = 0.05                    # bonus for neighbourhoods we know little about
RECENT_ALPHA = 0.15                      # weight of each newly finished list in the running global rate
DIVERSITY_WEIGHT = 0.5                   # score share lost when a candidate shares all its parents with a pick
OWNER_BOOST = {'client': 1.6, 'talking': 1.5, 'interested': 1.3}
TAPER_FROM = 0.75                        # share of the size cap where the size taper starts
MAX_FROM_ONE_NEIGHBOURHOOD = 2           # picks per top-up that may share a dominant parent


@dataclass
class Candidate:
    handle: str
    person_id: int = 0
    following: int = None
    followers: int = None
    verified: bool = False
    business: bool = None
    owner_status: str = None             # client / talking / interested, set by the owner
    relationship: bool = False
    parents: frozenset = frozenset()     # collected seeds whose following list contains this account
    overlap: int = 0                     # how many collected lists contain it (== len(parents) unless followers lists count)
    name: str = None
    ig_id: str = None
    pic: bool = False


@dataclass
class Context:
    """What we have learned so far. Built from measured lists, never from guesses."""
    recent_ratio: float = PRIOR_RATIO
    # parent seed -> [lists measured among its members, sum of their new-people ratios]
    parent_stats: dict = field(default_factory=dict)
    measured: int = 0


def cost(size):
    """Requests for one list: the lookup plus the pages."""
    return 1 + max(1, ceil(size / ROWS_PER_PAGE))


def planning_size(c):
    return c.following if c.following is not None else UNKNOWN_FOLLOWING


def eligible_size(c, cfg=None):
    lo = (cfg or {}).get('min_following', MIN_FOLLOWING)
    hi = (cfg or {}).get('max_following', MAX_FOLLOWING)
    return c.following is None or lo <= c.following <= hi


def neighbourhood(c, ctx):
    """(average new-people ratio of the lists measured around this account's parents, lists behind it)."""
    total = weight = 0.0
    for parent in c.parents:
        stat = ctx.parent_stats.get(parent)
        if stat and stat[0]:
            total += stat[1]
            weight += stat[0]
    return (total / weight, weight) if weight else (None, 0.0)


def expected_ratio(c, ctx):
    base = ctx.recent_ratio
    avg, evidence = neighbourhood(c, ctx)
    adjust = 0.0
    if avg is not None:
        adjust = (evidence / (evidence + NEIGHBOUR_SHRINK)) * (avg - base)
    ratio = base + adjust - OVERLAP_WEIGHT * log1p(max(0, c.overlap))
    ratio -= VERIFIED_WEIGHT if c.verified else 0.0
    if c.following:
        ratio += SIZE_WEIGHT * log10(max(c.following, 10) / 700.0)
    ratio -= BUSINESS_WEIGHT if c.business else 0.0
    return min(0.98, max(0.05, ratio)), evidence


def score(c, ctx, cfg=None):
    """Expected new people per request, nudged by exploration and owner evidence. Higher is better."""
    ratio, evidence = expected_ratio(c, ctx)
    size = planning_size(c)
    per_request = ratio * size / cost(size)
    # very long lists tie up a collector for days and Instagram may cut them short: taper near the cap
    hi = (cfg or {}).get('max_following', MAX_FOLLOWING)
    if size > hi * TAPER_FROM:
        per_request *= max(0.6, 1.0 - 0.4 * (size - hi * TAPER_FROM) / (hi * (1 - TAPER_FROM)))
    if c.following is None:
        per_request *= 0.9
    explore = EXPLORE_WEIGHT * ROWS_PER_PAGE / sqrt(1.0 + evidence)
    value = per_request + explore
    boost = OWNER_BOOST.get(c.owner_status, 1.0) * (1.15 if c.relationship else 1.0)
    return {'score': value * boost, 'ratio': ratio, 'per_request': per_request, 'size': size, 'evidence': evidence,
            'boost': boost}


def similarity(a, b):
    """Share of parents two candidates have in common (0 to 1)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def choose(candidates, ctx, count, *, pending_parents=(), cfg=None, scorer=None):
    """Greedy pick of `count` candidates: best score first, then each next one is discounted by how much its
    neighbourhood overlaps what is already chosen or waiting in the queue. Returns [(candidate, detail)]."""
    scorer = scorer or score
    scored = [(c, scorer(c, ctx, cfg)) for c in candidates if eligible_size(c, cfg)]
    scored.sort(key=lambda item: (-item[1]['score'], item[0].handle))
    taken, picked_parents = [], [frozenset(p) for p in pending_parents]
    per_parent = defaultdict(int)
    while scored and len(taken) < count:
        best_index, best_value = None, None
        for index, (c, detail) in enumerate(scored):
            nearest = max((similarity(c.parents, other) for other in picked_parents), default=0.0)
            value = detail['score'] * (1.0 - DIVERSITY_WEIGHT * nearest)
            if best_value is None or value > best_value + 1e-12:
                best_index, best_value = index, value
        c, detail = scored.pop(best_index)
        lead = max(c.parents, key=lambda p: (ctx.parent_stats.get(p, (0, 0))[0], p), default=None)
        if lead is not None and per_parent[lead] >= MAX_FROM_ONE_NEIGHBOURHOOD:
            continue
        if lead is not None:
            per_parent[lead] += 1
        taken.append((c, dict(detail, diversity=best_value)))
        picked_parents.append(c.parents)
    return taken


def update_context(ctx, seed_parents, ratio, members):
    """Fold one finished list into what we know. `seed_parents` are the lists that contained the seed."""
    ctx.measured += 1
    ctx.recent_ratio = (1 - RECENT_ALPHA) * ctx.recent_ratio + RECENT_ALPHA * ratio
    for parent in seed_parents:
        stat = ctx.parent_stats.setdefault(parent, [0, 0.0])
        stat[0] += 1
        stat[1] += ratio
    return ctx


def build_context(history):
    """history: [{'seed','ratio','members','parents'}] oldest first."""
    ctx = Context()
    for item in history:
        update_context(ctx, item['parents'], item['ratio'], item['members'])
    return ctx


# ---- plain-language reasons (docs/COPY.md) ----

def plural(n, word):
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"


def reason(c, detail, ctx):
    """One short sentence on why this account was added. No codes, no scores."""
    parts = []
    if c.owner_status:
        parts.append({'client': 'Your client', 'talking': 'Someone you are talking to',
                      'interested': 'Marked as interested'}[c.owner_status])
    elif c.relationship:
        parts.append('A personal connection you recorded')
    n = len(c.parents)
    avg, evidence = neighbourhood(c, ctx)
    if n:
        who = sorted(c.parents)[:2]
        parts.append('Followed by ' + (f"@{who[0]}" if n == 1 else f"@{who[0]} and {plural(n - 1, 'other collected profile')}"
                                        if n > 1 else ''))
    if avg is not None and evidence >= 2:
        if avg >= 0.7:
            parts.append(f"similar lists returned mostly new people ({round(avg * 100)}%)")
        elif avg < 0.45:
            parts.append(f"similar lists returned few new people ({round(avg * 100)}%), so this is lower priority")
    elif not evidence and n:
        parts.append('an area we have not tried yet')
    if c.following:
        parts.append(f"follows {c.following:,}")
    text = '. '.join(p[0].upper() + p[1:] if i == 0 else p for i, p in enumerate(parts)) if parts else 'Followed by people we have collected'
    return text if text.endswith('.') else text + '.'


# ---- database layer ----

def _today():
    return datetime.now().astimezone().date().isoformat()


def settings(conn):
    depth = db.get_setting(conn, DEPTH_KEY, DEFAULT_DEPTH)
    cap = db.get_setting(conn, DAILY_CAP_KEY, DEFAULT_DAILY_CAP)
    return {'enabled': db.get_setting(conn, ENABLED_KEY, True) is True,
            'depth': depth if type(depth) is int and 1 <= depth <= MAX_DEPTH else DEFAULT_DEPTH,
            'daily_cap': cap if type(cap) is int and 0 <= cap <= MAX_DAILY_CAP else DEFAULT_DAILY_CAP}


def set_settings(conn, *, enabled=None, depth=None, daily_cap=None):
    """Validated writes; the caller owns the transaction."""
    if enabled is not None:
        if type(enabled) is not bool:
            raise ValueError('Auto-pick must be on or off.')
        db.set_setting(conn, ENABLED_KEY, enabled)
    if depth is not None:
        if type(depth) is not int or not 1 <= depth <= MAX_DEPTH:
            raise ValueError(f'Lists kept ready must be a whole number from 1 to {MAX_DEPTH}.')
        db.set_setting(conn, DEPTH_KEY, depth)
    if daily_cap is not None:
        if type(daily_cap) is not int or not 0 <= daily_cap <= MAX_DAILY_CAP:
            raise ValueError(f'Daily limit must be a whole number from 0 to {MAX_DAILY_CAP}.')
        db.set_setting(conn, DAILY_CAP_KEY, daily_cap)


def ensure(conn):
    """Idempotent schema upkeep, called from collection_suggestions.ensure at database initialisation."""
    have = {r[1] for r in conn.execute('PRAGMA table_info(collection_discovery)')}
    for column, ddl in (('score', 'REAL'), ('pinned', 'INTEGER NOT NULL DEFAULT 0'), ('policy', 'TEXT'),
                        ('members', 'INTEGER'), ('fresh', 'INTEGER'), ('measured_at', 'TEXT')):
        if have and column not in have:
            conn.execute(f'ALTER TABLE collection_discovery ADD COLUMN {column} {ddl}')
    conn.execute('CREATE TABLE IF NOT EXISTS discovery_yield(job_id INTEGER PRIMARY KEY, seed TEXT NOT NULL COLLATE NOCASE,'
                 'members INTEGER NOT NULL, fresh INTEGER NOT NULL, started_at TEXT NOT NULL, measured_at TEXT NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS discovery_yield_seed ON discovery_yield(seed)')


def measure_runs(conn):
    """Count, for every finished following list, how many members were new to us when its run began.

    A member is new when its saved profile first appeared at or after the run's first page. Cached per run in
    `discovery_yield`; only runs that stopped (done or partial) are measured, and only once.
    """
    known = {r[0] for r in conn.execute('SELECT job_id FROM discovery_yield')}
    runs = conn.execute(
        "SELECT j.id,j.seed,r.member_count,(SELECT min(at) FROM pages g WHERE g.job_id=j.id) AS started "
        "FROM jobs j JOIN list_runs r ON r.job_id=j.id WHERE j.kind='list' AND j.direction='following' "
        "AND j.state IN ('done','partial') AND r.member_count>=?", (MIN_MEASURED_MEMBERS,)).fetchall()
    stamp = db.now()
    for run in runs:
        if run['id'] in known or not run['started']:
            continue
        fresh = conn.execute('SELECT count(*) FROM list_members m JOIN people p ON p.id=m.person_id '
                             'WHERE m.job_id=? AND p.first_seen>=?', (run['id'], run['started'])).fetchone()[0]
        conn.execute('INSERT OR IGNORE INTO discovery_yield(job_id,seed,members,fresh,started_at,measured_at) VALUES(?,?,?,?,?,?)',
                     (run['id'], run['seed'], run['member_count'], fresh, run['started'], stamp))


def parent_map(conn):
    """person_id -> set of lowercase seeds whose following list contains that person."""
    parents = defaultdict(set)
    for row in conn.execute("SELECT e.person_id,e.seed FROM edges e WHERE e.direction='following'"):
        parents[row[0]].add(row[1].lower())
    return parents


def load_context(conn, parents=None):
    """Context from every measured list, oldest first. A seed measured twice counts its latest run."""
    parents = parents if parents is not None else parent_map(conn)
    latest = {}
    for row in conn.execute('SELECT y.seed,y.members,y.fresh,y.started_at,p.id AS pid FROM discovery_yield y '
                            'LEFT JOIN people p ON p.handle=y.seed ORDER BY y.started_at'):
        latest[row['seed'].lower()] = row
    history = []
    for seed, row in sorted(latest.items(), key=lambda kv: kv[1]['started_at']):
        history.append({'seed': seed, 'ratio': row['fresh'] / row['members'], 'members': row['members'],
                        'parents': parents.get(row['pid'], set()) - {seed} if row['pid'] else set()})
    return build_context(history)


EXCLUDE_TAGS = ('blocked', 'not a fit', 'not fit', 'do not scrape')


def load_candidates(conn, parents=None, *, limit=4000):
    """Everyone who appears in a collected following list and could still become a seed. Read only."""
    parents = parents if parents is not None else parent_map(conn)
    taken = {r[0].lower() for r in conn.execute('SELECT handle FROM seeds')}
    taken |= {r[0].lower() for r in conn.execute('SELECT seed FROM lists')}
    taken |= {r[0].lower() for r in conn.execute("SELECT seed FROM jobs WHERE kind='list' AND seed IS NOT NULL")}
    taken |= {r[0].lower() for r in conn.execute('SELECT handle FROM collection_discovery')}
    taken |= {r[0].lower() for r in conn.execute('SELECT seed FROM list_private_denials')}
    try:
        taken |= {r[0].lower() for r in conn.execute('SELECT old_handle FROM seed_aliases')}
        taken |= {r[0].lower() for r in conn.execute('SELECT new_handle FROM seed_aliases')}
    except Exception:
        pass
    blocked_ids = {r[0] for r in conn.execute(
        "SELECT person_id FROM tags WHERE source='manual' AND lower(trim(tag)) IN (%s)" % ','.join('?' * len(EXCLUDE_TAGS)),
        EXCLUDE_TAGS)}
    rejected = {r[0] for r in conn.execute("SELECT person_id FROM marks WHERE status='no'")}
    own_ig = {r[0] for r in conn.execute('SELECT ig_id FROM accounts WHERE ig_id IS NOT NULL')}
    own_ig |= {r[0] for r in conn.execute('SELECT ig_id FROM account_identity_state')}
    own_handles = {r[0].lower() for r in conn.execute('SELECT handle FROM accounts WHERE handle IS NOT NULL')}
    owner = {}
    for row in conn.execute('SELECT m.person_id,m.status FROM marks m WHERE m.status IN (\'client\',\'talking\',\'interested\')'):
        owner[row[0]] = row[1]
    relationships = {r[0] for r in conn.execute("SELECT person_id FROM owner_context WHERE relationships!='[]'")}
    ids = list(parents)
    out = []
    for start in range(0, len(ids), 800):
        chunk = ids[start:start + 800]
        marks = ','.join('?' * len(chunk))
        for r in conn.execute(
                'SELECT id,ig_id,handle,name,pic_file,followers,following,is_private,is_verified,is_business,category,website '
                f'FROM people WHERE id IN ({marks})', chunk):
            handle = r['handle'].lower()
            if (handle in taken or handle in own_handles or '~' in handle or handle == 'fortun8te' or r['is_private']
                    or r['id'] in blocked_ids or r['id'] in rejected or (r['ig_id'] and r['ig_id'] in own_ig)):
                continue
            business = None if r['is_business'] is None and not r['category'] else bool(r['is_business'] or r['category'])
            out.append(Candidate(handle=r['handle'], person_id=r['id'], following=r['following'], followers=r['followers'],
                                 verified=bool(r['is_verified']), business=business, owner_status=owner.get(r['id']),
                                 relationship=r['id'] in relationships, parents=frozenset(parents[r['id']]),
                                 overlap=len(parents[r['id']]), name=r['name'], ig_id=r['ig_id'], pic=bool(r['pic_file'])))
    return out


def pending_parents(conn, parents):
    """Neighbourhoods of lists already waiting, so a top-up does not queue lookalikes of what is queued."""
    rows = conn.execute("SELECT p.id FROM jobs j JOIN people p ON p.handle=j.seed COLLATE NOCASE "
                        "WHERE j.kind='list' AND j.direction='following' AND j.state IN ('queued','leased')").fetchall()
    return [frozenset(parents.get(r[0], ())) for r in rows]


def pending_count(conn):
    """Every list waiting or running, manual or automatic, in the direction automatic seeds use."""
    return conn.execute("SELECT count(*) FROM jobs WHERE kind='list' AND direction='following' "
                        "AND state IN ('queued','leased')").fetchone()[0]


def added_today(conn):
    day = _today()
    return conn.execute("SELECT count(*) FROM collection_discovery WHERE state IN ('queued','removed') AND policy=? "
                        "AND substr(at,1,10)>=? AND at>=?", (POLICY, day, _day_start())).fetchone()[0]


def _day_start():
    local = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc).isoformat()


def top_up(conn, active_collectors, now=None, *, queue_list=None):
    """Queue automatic seeds until `depth * active_collectors` lists are pending. Returns the picks added.

    Caller owns the transaction and has already checked every collector guard (pause, warning hold, cooldown,
    protected main account). Respects the daily limit, never re-adds anything seen before, and queues at
    AUTO_PRIORITY so manual lists keep their place at the front.
    """
    cfg = settings(conn)
    if not cfg['enabled'] or active_collectors < 1:
        return []
    want = cfg['depth'] * active_collectors - pending_count(conn)
    room = cfg['daily_cap'] - added_today(conn)
    count = min(want, room)
    if count <= 0:
        return []
    queue_list = queue_list or db.queue_list
    measure_runs(conn)
    parents = parent_map(conn)
    ctx = load_context(conn, parents)
    picks = choose(load_candidates(conn, parents), ctx, count, pending_parents=pending_parents(conn, parents))
    added = []
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    for c, detail in picks:
        if not queue_list(conn, c.handle, 'following', priority=AUTO_PRIORITY):
            continue
        text = reason(c, detail, ctx)
        conn.execute('INSERT INTO collection_discovery(handle,person_id,ig_id,state,at,reason,directions,score,policy) '
                     "VALUES(?,?,?,'queued',?,?,?,?,?)",
                     (c.handle, c.person_id, c.ig_id, stamp, text, json.dumps(['following']), detail['score'], POLICY))
        added.append({'handle': c.handle, 'person_id': c.person_id, 'ig_id': c.ig_id, 'reason': text,
                      'directions': ['following'], 'score': detail['score'], 'display_name': c.name})
    return added


# ---- the user's side: list, pin, remove ----

def _state(conn, handle):
    """queued | collecting | done for one automatic seed."""
    row = conn.execute("SELECT state FROM lists WHERE seed=? AND direction='following'", (handle,)).fetchone()
    if row is None:
        return 'queued'
    return {'running': 'collecting', 'queued': 'queued', 'done': 'done', 'partial': 'done'}.get(row['state'], 'queued')


def listing(conn, limit=100):
    rows = conn.execute(
        "SELECT d.handle,d.person_id,d.reason,d.at,d.pinned,d.score,d.state,p.name,p.pic_file,p.following,"
        "(SELECT y.members FROM discovery_yield y WHERE y.seed=d.handle ORDER BY y.started_at DESC LIMIT 1) AS members,"
        "(SELECT y.fresh FROM discovery_yield y WHERE y.seed=d.handle ORDER BY y.started_at DESC LIMIT 1) AS fresh "
        "FROM collection_discovery d LEFT JOIN people p ON p.id=d.person_id "
        "WHERE d.policy=? AND d.state IN ('queued','removed') ORDER BY d.at DESC,d.handle LIMIT ?", (POLICY, limit)).fetchall()
    items = []
    for r in rows:
        if r['state'] == 'removed':
            continue
        items.append({'handle': r['handle'], 'name': r['name'], 'pic': f"/img/{r['person_id']}" if r['pic_file'] else None,
                      'reason': r['reason'], 'added_at': r['at'], 'pinned': bool(r['pinned']), 'status': _state(conn, r['handle']),
                      'following': r['following'], 'saved': r['members'],
                      'new_share': round(r['fresh'] / r['members'], 2) if r['members'] else None})
    cfg = settings(conn)
    return {'enabled': cfg['enabled'], 'depth': cfg['depth'], 'daily_cap': cfg['daily_cap'], 'added_today': added_today(conn),
            'items': items}


def pin(conn, handle, value=True):
    """Keep an automatic seed and give it manual priority. Caller commits."""
    row = conn.execute("SELECT handle FROM collection_discovery WHERE handle=? AND state='queued'", (db.norm_handle(handle),)).fetchone()
    if not row:
        return 'missing'
    conn.execute('UPDATE collection_discovery SET pinned=? WHERE handle=?', (1 if value else 0, row['handle']))
    conn.execute("UPDATE jobs SET priority=? WHERE kind='list' AND state='queued' AND lower(seed)=lower(?) AND priority<=0",
                 (0 if value else AUTO_PRIORITY, row['handle']))
    return 'ok'


def remove(conn, handle):
    """Stop an automatic seed before or while it collects, and never add it again. Saved people stay. Caller commits."""
    handle = db.norm_handle(handle)
    row = conn.execute("SELECT handle,pinned FROM collection_discovery WHERE handle=? AND state='queued'", (handle,)).fetchone()
    if not row:
        return 'missing'
    conn.execute("UPDATE collection_discovery SET state='removed' WHERE handle=?", (row['handle'],))
    removed = False
    try:
        import seeds as seed_workflow           # the Accounts seed workflow, when present, does the tombstone and the hold
        if hasattr(seed_workflow, 'remove_seed'):
            seed_workflow.remove_seed(conn, row['handle'])
            removed = True
    except ImportError:
        pass
    if not removed:
        conn.execute("UPDATE jobs SET state='cancelled' WHERE kind='list' AND state='queued' AND lower(seed)=lower(?)", (row['handle'],))
    return 'ok'
