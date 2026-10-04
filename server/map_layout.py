"""Precomputed map layouts: importance-ranked, spatially indexed, built outside requests.

One SQLite file per mode lives in ``<db>.map/`` (``closeness.sqlite`` ...):

* ``mp``   one row per person: micro-cell position, rank, cluster, facet class.
* ``rtb``  R*Tree over (x, y, rank) for people with no fit and no status (the bulk).
* ``rtk``  the same for people with a fit or a status ("known"), plus the facet class.
* ``agg``  count pyramid per (depth, cell, class, cluster): counts and coordinate sums.

The layout is a pure function of a person's stored facts (``layout_row``), so a full build and the
incremental path (``apply_dirty``, driven by triggers that fill ``map_layout_dirty``) agree exactly.
Nothing here runs in a request. See docs/MAP_VIEW_API.md.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import sqlite3
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402

MODES = ('closeness', 'fit', 'seeds', 'status')
STATUS_NAMES = ('interested', 'contacted', 'talking', 'spoke_before', 'client', 'no')
STATUS_CODE = {None: 0, '': 0, 'interested': 1, 'good': 1, 'contacted': 2, 'talking': 3,
               'spoke_before': 4, 'client': 5, 'no': 6}
CODE_STATUS = {i + 1: n for i, n in enumerate(STATUS_NAMES)}
BAND_EDGES = (0, 25, 45, 60, 70, 85)          # min_fit snaps down to one of these
COMMUNITIES = 12                              # default view: largest recorded source audiences
MICRO_BITS = 22
MICRO = 1 << MICRO_BITS
DEPTHS = tuple(range(1, 11))                  # count pyramid depths (cell = 2**-depth wide)
SCHEMA_VERSION = 7
M64 = (1 << 64) - 1
TWO_PI = 2 * math.pi
POSITIVE = ('interested', 'talking', 'client')

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE mp(person_id INTEGER PRIMARY KEY, mx INTEGER NOT NULL, my INTEGER NOT NULL, rk REAL NOT NULL,
  cluster INTEGER NOT NULL, cls INTEGER NOT NULL, fit INTEGER, cl INTEGER NOT NULL, src INTEGER NOT NULL DEFAULT 0);
CREATE INDEX mp_class_rank ON mp(cls,rk DESC,person_id);
CREATE VIRTUAL TABLE rtb USING rtree(id,minx,maxx,miny,maxy,minr,maxr);
CREATE VIRTUAL TABLE rtk USING rtree(id,minx,maxx,miny,maxy,minr,maxr,+cls INTEGER);
CREATE TABLE agg(depth INTEGER NOT NULL, cy INTEGER NOT NULL, cx INTEGER NOT NULL, cls INTEGER NOT NULL,
  cluster INTEGER NOT NULL, n INTEGER NOT NULL, sx INTEGER NOT NULL, sy INTEGER NOT NULL,
  PRIMARY KEY(depth,cy,cx,cls,cluster)) WITHOUT ROWID;
"""


class BuildBusy(RuntimeError):
    pass


# ---------- small pure helpers ----------

def _h64(x):
    x = (x + 0x9E3779B97F4A7C15) & M64
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & M64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & M64
    return x ^ (x >> 31)


def uniforms(pid, salt=0):
    """Three stable uniforms in [0,1) for a person."""
    h = _h64(pid * 8 + salt)
    return (h & 0x1FFFFF) / 2097152.0, ((h >> 21) & 0x1FFFFF) / 2097152.0, (h >> 42) / 4194304.0


_F32 = struct.Struct('f')


def f32(v):
    return _F32.unpack(_F32.pack(v))[0]


def fit_band(fit):
    """0 for unread, else 1..6 by BAND_EDGES."""
    if fit is None:
        return 0
    band = 1
    for i, edge in enumerate(BAND_EDGES):
        if fit >= edge:
            band = i + 1
    return band


def snap_min_fit(value):
    """Largest band edge at or below value (or 0)."""
    edge = 0
    for e in BAND_EDGES:
        if value >= e:
            edge = e
    return edge


def cls_of(status_code, fit, follow=0):
    return follow * 64 + status_code * 8 + fit_band(fit)


def is_lead(cls):
    status_code, band = divmod(cls % 64, 8)
    return status_code != 6 and (band == 0 or band >= 3)


def cell_of(mx, my, depth):
    shift = MICRO_BITS - depth
    return my >> shift, mx >> shift


def coord(micro):
    return (micro + 0.5) / MICRO


def to_micro(v):
    return min(MICRO - 1, max(0, int(v * MICRO)))


# ---------- paths and stores ----------

def map_dir(db_path):
    return Path(str(db_path) + '.map')


def _pointer(db_path):
    return map_dir(db_path) / 'current.json'


def mode_path(db_path, mode):
    """The active file of a mode, or the path it would get when unbuilt.

    A finished build gets a fresh name and ``current.json`` is repointed atomically, so a WAL
    database is never renamed underneath a reader that still has it open.
    """
    name = (_read_json(_pointer(db_path), None) or {}).get(mode)
    return map_dir(db_path) / (name or f'{mode}.sqlite')


def mode_exists(db_path, mode):
    name = (_read_json(_pointer(db_path), None) or {}).get(mode)
    return bool(name) and (map_dir(db_path) / name).exists()


def point_to(db_path, mode, name):
    pointer = _read_json(_pointer(db_path), None) or {}
    pointer[mode] = name
    _write_json(_pointer(db_path), pointer)


def gc(db_path, grace=180):
    """Delete files no pointer refers to any more (after a grace period for in-flight readers)."""
    pointer = _read_json(_pointer(db_path), None) or {}
    keep = set(pointer.values())
    now = time.time()
    for path in map_dir(db_path).glob('*.sqlite*'):
        base = path.name.split('.sqlite')[0] + '.sqlite'
        if base in keep or '.building' in path.name:
            continue
        try:
            if now - path.stat().st_mtime > grace:
                path.unlink()
        except OSError:
            pass


def open_store(path, create=False, readonly=False):
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.execute('PRAGMA busy_timeout=30000')
    if not readonly:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA mmap_size=268435456')
    conn.execute('PRAGMA temp_store=FILE')
    conn.execute('PRAGMA cache_size=-32768')
    if create:
        conn.executescript(SCHEMA)
    return conn


def meta_get(conn, key, default=None):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def meta_set(conn, key, value):
    conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (key, json.dumps(value, separators=(',', ':'))))


# ---------- the plan: small global facts every layout needs ----------

def _source_class(status, relationships):
    """1 = a good or client account of Michael's, 2 = someone he knows, 0 = neither."""
    try:
        rels = json.loads(relationships or '[]')
    except (ValueError, TypeError):
        rels = []
    if status in POSITIVE or 'client' in rels:
        return 1
    return 2 if rels else 0


def source_classes(conn, handles):
    out = {}
    for handle in handles:
        row = conn.execute('SELECT m.status,oc.relationships FROM people p LEFT JOIN marks m ON m.person_id=p.id '
                           'LEFT JOIN owner_context oc ON oc.person_id=p.id WHERE p.handle=?', (handle,)).fetchone()
        out[handle.lower()] = _source_class(row[0], row[1]) if row else 0
    return out


def _owner_handle(conn):
    return conn.execute('SELECT ' + "coalesce((SELECT handle FROM seeds WHERE is_me=1 ORDER BY "
                        "(lower(handle)='fortun8te') DESC,handle LIMIT 1),'fortun8te')").fetchone()[0]


NETWORK_LABELS = ('Direct connections', 'Known sources', 'Shared audiences', 'Other collected', 'You')
NETWORK_RADII = ((.055, .205), (.235, .29), (.325, .385), (.42, .47))
OWNER_RADII = {3: (.055, .085), 2: (.095, .125), 1: (.135, .165), 0: (.175, .205)}


def network_group(F, ctx):
    pid, deg, score, fit, code, me, seeds, src, fam, rel = F
    if pid == ctx.owner_id:
        return 4
    if me & 3 or fam or rel:
        return 0
    others = [s for s in seeds if s != ctx.owner]
    if any(s in ctx.known_sources for s in others):
        return 1
    if len(others) >= 2:
        return 2
    return 3


def known_source_handles(conn, handles):
    known = []
    for handle in handles:
        row = conn.execute('SELECT m.status,oc.relationships FROM people p LEFT JOIN marks m ON m.person_id=p.id '
                           'LEFT JOIN owner_context oc ON oc.person_id=p.id WHERE p.handle=?', (handle,)).fetchone()
        if row:
            try:
                relationships = json.loads(row[1] or '[]')
            except (ValueError, TypeError):
                relationships = []
            if row[0] == 'client' or relationships:
                known.append(handle)
    return known


def make_plan(conn):
    """Everything global the four layouts share. Small: a few hundred sources at most."""
    owner = _owner_handle(conn).lower()
    rows = conn.execute('SELECT seed,degree FROM map_seed_degree ORDER BY degree DESC,seed').fetchall()
    sources = [(r[0].lower(), r[1]) for r in rows if r[0].lower() != owner]
    classes = source_classes(conn, [h for h, _ in sources])
    total = conn.execute('SELECT count(*) FROM people').fetchone()[0]
    comm = [h for h, _ in sources[:COMMUNITIES]]
    # Allocate angular space by collected audience size, softened so a large
    # audience has room without swallowing every smaller neighborhood.
    counts = [n for _, n in sources[:COMMUNITIES]]
    counts.append(max(1, sum(n for _, n in sources[COMMUNITIES:])))
    weights = [max(1, n) ** .65 for n in counts]
    angle, sectors = -math.pi / 2, []
    for weight in weights:
        width = TWO_PI * weight / sum(weights)
        sectors.append([angle, width]); angle += width
    known_sources = known_source_handles(conn, [h for h, _ in sources])
    src = {}
    kmap = {h: k for k, h in enumerate(comm)}
    for h, n in sources:
        src[h] = [n, classes[h], kmap.get(h, -1)]
    source_ids = [r[0] for r in conn.execute('SELECT p.id FROM map_source_handles h '
                                             'JOIN people p ON p.handle=h.handle')]
    row = conn.execute('SELECT id FROM people WHERE handle=?', (owner,)).fetchone()
    owner_id = row[0] if row else 0
    plan = {'id': hashlib.sha1(f'{time.time()}-{os.getpid()}'.encode()).hexdigest()[:10],
            'made_at': db.now(), 'owner': owner, 'owner_id': owner_id, 'people': total, 'src': src,
            'comm': comm, 'network_disk': True, 'network_sectors': sectors, 'known_sources': known_sources,
            'source_ids': source_ids}
    return plan


class Ctx:
    """A plan unpacked for fast per-person placement."""

    def __init__(self, plan):
        self.plan = plan
        self.owner = plan['owner']
        self.owner_id = plan.get('owner_id', 0)
        self.src = {h: (v[1], v[2]) for h, v in plan['src'].items()}
        self.k_other = len(plan['comm'])
        self.network_disk = plan.get('network_disk', False)
        self.network_sectors = plan.get('network_sectors', [])
        self.known_sources = set(plan.get('known_sources', []))

    def groups(self, mode):
        """The same source/evidence anchors in every mode; mode only changes rank."""
        out = []
        for audience in range(self.k_other + 1):
            label = ('Audience of @' + self.plan['comm'][audience] if audience < self.k_other else 'Other collected audiences')
            start, width = self.network_sectors[audience]
            theta = start + .5 * width
            for category, (low, high) in enumerate(NETWORK_RADII):
                radius = math.sqrt((low * low + high * high) / 2)
                out.append({'id': audience * 4 + category, 'label': label,
                            'category': NETWORK_LABELS[category],
                            'x': .5 + radius * math.cos(theta), 'y': .5 + radius * math.sin(theta),
                            'r': min(.045, radius * width * .22)})
        out.append({'id': (self.k_other + 1) * 4, 'label': 'You', 'x': .5, 'y': .5, 'r': .035})
        return out


# ---------- per-person facts ----------

# feature tuple: (pid, deg, score, fit, status_code, me, seeds, src, fam, rel)
# me: 0 none, 1 followed (Michael follows them), 2 follows (they follow Michael), 3 mutual.
FAMILIARITY = {'briefly': 1, 'know_them': 2, 'close': 3}


def _chunked(ids, n=900):
    ids = list(ids)
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def _clause(ids, lo, hi, column='person_id'):
    if ids is not None:
        return f'{column} IN ({",".join("?" * len(ids))})', list(ids)
    return f'{column}>=? AND {column}<?', [lo, hi]


def load_features(conn, ctx, ids=None, lo=None, hi=None):
    """{pid: feature tuple} for the people of the map among ``ids`` (or ids in [lo, hi))."""
    owner = ctx.owner
    where, args = _clause(ids, lo, hi)
    degree = {r[0]: r[1] for r in conn.execute(f'SELECT person_id,degree FROM map_person_degree WHERE {where}', args)}
    if ids is not None:
        sources = {r[0] for r in conn.execute('SELECT p.id FROM people p JOIN map_source_handles h ON h.handle=p.handle '
                                              f'WHERE p.id IN ({",".join("?" * len(ids))})', args)}
    else:
        sources = {i for i in ctx.plan['source_ids'] if lo <= i < hi}
    # Every saved person belongs in the map, even without collected edges.
    if ids is not None:
        slots = ','.join('?' * len(ids))
        present = {r[0] for r in conn.execute(f'SELECT id FROM people WHERE id IN ({slots})', list(ids))}
    else:
        present = {r[0] for r in conn.execute('SELECT id FROM people WHERE id>=? AND id<?', (lo, hi))}
    if not present:
        return {}
    seeds = {}
    for pid, seed in conn.execute(f'SELECT person_id,seed FROM map_seed_member WHERE {where}', args):
        seeds.setdefault(pid, []).append(seed.lower())
    score, fit, status, own = {}, {}, {}, {}
    for pid, s, f in conn.execute(f'SELECT person_id,score,content_fit FROM verdicts WHERE {where}', args):
        score[pid] = s
        fit[pid] = f
    for pid, s in conn.execute(f'SELECT person_id,status FROM marks WHERE {where}', args):
        status[pid] = s
    for pid, rel, fam in conn.execute(f'SELECT person_id,relationships,familiarity FROM owner_context WHERE {where}', args):
        own[pid] = (rel, fam)
    me = {}
    for pid, direction in conn.execute(f'SELECT person_id,direction FROM current_edges WHERE seed=? AND {where}',
                                       [owner, *args]):
        me[pid] = me.get(pid, 0) | (2 if direction == 'followers' else 1)
    # The same direct follow can be observed in either person's list. A source's
    # following list containing the owner proves source -> owner, not owner -> source.
    owner_where, owner_args = _clause(ids, lo, hi, column='p.id')
    for pid, direction in conn.execute(
            f'SELECT p.id,e.direction FROM current_edges e JOIN people p ON p.handle=e.seed '
            f'WHERE e.person_id=? AND {owner_where}', [ctx.owner_id, *owner_args]):
        me[pid] = me.get(pid, 0) | (2 if direction == 'following' else 1)
    # Absence is recorded only when a complete tracked following check disproves
    # a previously observed edge. No edge/partial coverage remains unknown.
    for (pid,) in conn.execute(f"SELECT person_id FROM edge_evidence WHERE seed=? AND direction='following' "
                              f'AND active=0 AND {where} AND EXISTS(SELECT 1 FROM edges e '
                              'WHERE e.seed=edge_evidence.seed AND e.person_id=edge_evidence.person_id '
                              'AND e.direction=edge_evidence.direction)', [owner, *args]):
        if not me.get(pid, 0) & 1:
            me[pid] = me.get(pid, 0) | 4
    out = {}
    for pid in present:
        rel, fam = own.get(pid, (None, None))
        try:
            rels = json.loads(rel) if rel else []
        except (ValueError, TypeError):
            rels = []
        code = STATUS_CODE.get(status.get(pid), 0)
        if code == 0 and 'client' in rels:
            code = 5
        f = fit.get(pid)
        out[pid] = (pid, degree.get(pid, 0), score.get(pid), None if f is None else int(round(f)), code,
                    me.get(pid, 0), tuple(seeds.get(pid, ())), 1 if pid in sources else 0,
                    FAMILIARITY.get(fam, 0), 1 if rels else 0)
    return out


def features_for(conn, ctx, ids):
    """Like load_features for an arbitrary id list; people outside the map map to None."""
    out = {pid: None for pid in ids}
    for chunk in _chunked(ids):
        out.update(load_features(conn, ctx, ids=chunk))
    return out


# ---------- placement ----------

STATUS_CLOSE = (0.0, 0.10, 0.06, 0.22, 0.24, 0.30, 0.0)
ENGAGED = (0.0, 0.14, 0.08, 0.20, 0.20, 0.28, -0.35)
STATUS_BASE = (0.05, 0.45, 0.35, 0.55, 0.50, 0.60, 0.0)
ME_TERM = (0.0, 0.22, 0.27, 0.35)


def closeness(F, ctx):
    """Evidence ordering for recorded follows; never an interpersonal probability."""
    pid, deg, score, fit, code, me, seeds, src, fam, rel = F
    if pid == ctx.owner_id:
        return 1.0
    others = [s for s in seeds if s != ctx.owner]
    # Small shared-source tie breaks cannot outrank any direct owner evidence.
    support = 0.04 * (1 - 0.55 ** len(others))
    base = {3: .90, 2: .75, 1: .60}.get(me & 3)
    if base is not None:
        return base + support
    if fam or rel:
        return .45 + support
    known = any(s in ctx.known_sources for s in others)
    return (.20 if known else .10 if len(others) >= 2 else .03) + support


def _rank(mode, F, c, n_others, owner_id=0):
    pid, deg, score, fit, code, me, seeds, src, fam, rel = F
    if pid == owner_id:
        return 1.0
    s = (score or 0) / 100.0
    if mode == 'closeness':
        raw = c
    elif mode == 'fit':
        raw = (0.62 * (fit / 100.0) + 0.05 if fit is not None else 0.0) + 0.24 * c + 0.14 * s + ENGAGED[code]
        if src:
            raw += 0.15
    elif mode == 'seeds':
        raw = 0.50 * min(1.0, max(0, n_others - 1) / 7.0) + 0.30 * c + 0.20 * s + ENGAGED[code]
        if src:
            raw += 0.60
    else:
        raw = STATUS_BASE[code] + 0.25 * (0.6 * c + 0.4 * s)
        if src:
            raw += 0.15
    return max(0.0, min(1.0, raw / 1.3))


def layout_row(mode, F, ctx, c=None):
    """(pid, mx, my, rk, cluster, cls, fit, cl, src) for one person in one mode."""
    pid, deg, score, fit, code, me, seeds, src, fam, rel = F
    if c is None:
        c = closeness(F, ctx)
    owner = ctx.owner
    others = [s for s in seeds if s != owner]
    # A person's position and community belong to the network, not the selected
    # view. Mode changes ranking only, preserving the user's spatial context.
    u1, u2, u3 = uniforms(pid, 0)
    if pid == ctx.owner_id:
        cluster, x, y = (ctx.k_other + 1) * 4, .5, .5
    else:
        category = network_group(F, ctx)
        recorded = [s for s in others if s in ctx.src and ctx.src[s][1] >= 0]
        chosen = min(recorded, key=lambda s: (ctx.plan['src'][s][0], s)) if recorded else None
        audience = ctx.src[chosen][1] if chosen else ctx.k_other
        start, width = ctx.network_sectors[audience]
        theta = start + (.035 + .93 * u1) * width
        low, high = OWNER_RADII[me & 3] if category == 0 else NETWORK_RADII[category]
        radius = math.sqrt(low * low + u2 * (high * high - low * low))
        x, y = .5 + radius * math.cos(theta), .5 + radius * math.sin(theta)
        cluster = audience * 4 + category
    raw = _rank(mode, F, c, len(others), ctx.owner_id)
    rk = f32(min(1.0, max(0.0, raw)) * 0.9999 + 1e-4 * u3)
    return (pid, to_micro(x), to_micro(y), rk, cluster, cls_of(code, fit, me), fit, int(round(c * 1000)), src)


def rows_for(mode, F, ctx):
    return layout_row(mode, F, ctx)


# ---------- writing rows ----------

def _agg_cells(mx, my):
    for d in DEPTHS:
        shift = MICRO_BITS - d
        yield d, my >> shift, mx >> shift


def _tree(cls):
    return 'rtb' if cls == 0 else 'rtk'


def insert_rows(conn, rows):
    """Add mp + tree rows (no agg; the caller owns that)."""
    conn.executemany('INSERT INTO mp VALUES(?,?,?,?,?,?,?,?,?)', rows)
    bulk, known = [], []
    for pid, mx, my, rk, cluster, cls, fit, cl, src in rows:
        x, y = coord(mx), coord(my)
        if cls == 0:
            bulk.append((pid, x, x, y, y, rk, rk))
        else:
            known.append((pid, x, x, y, y, rk, rk, cls))
    if bulk:
        conn.executemany('INSERT INTO rtb VALUES(?,?,?,?,?,?,?)', bulk)
    if known:
        conn.executemany('INSERT INTO rtk VALUES(?,?,?,?,?,?,?,?)', known)


def build_agg(conn):
    conn.execute('DELETE FROM agg')
    shift = MICRO_BITS - DEPTHS[-1]
    conn.execute('INSERT INTO agg SELECT ?, my>>?, mx>>?, cls, cluster, count(*), sum(mx), sum(my) FROM mp '
                 'GROUP BY 2,3,4,5 ORDER BY 2,3,4,5', (DEPTHS[-1], shift, shift))
    for d in reversed(DEPTHS[:-1]):
        conn.execute('INSERT INTO agg SELECT ?, cy>>1, cx>>1, cls, cluster, sum(n), sum(sx), sum(sy) FROM agg '
                     'WHERE depth=? GROUP BY 2,3,4,5 ORDER BY 2,3,4,5', (d, d + 1))


def rank_quantiles(conn):
    """Global rank value at counts 2^k from the top (a coarse table used to seed the search for a rank cut)."""
    buckets = conn.execute('SELECT CAST(rk*65536 AS INTEGER) b,count(*) FROM mp GROUP BY b ORDER BY b DESC').fetchall()
    out, seen, target = [], 0, 1
    for b, n in buckets:
        seen += n
        while seen >= target:
            out.append([target, round((b + 0.5) / 65536, 6)])
            target *= 2
    return out


# ---------- full build ----------

def _write_json(path, value):
    tmp = str(path) + '.tmp'
    Path(tmp).write_text(json.dumps(value, separators=(',', ':')))
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


@contextlib.contextmanager
def _lock(path, blocking=False):
    handle = open(path, 'a+')
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError:
            raise BuildBusy(str(path)) from None
        yield
    finally:
        handle.close()


def is_building(db_path):
    path = map_dir(db_path) / 'build.lock'
    if not path.exists():
        return False
    handle = open(path, 'a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        handle.close()


def build_mode(db_path, mode, plan, chunk=100_000, progress=None):
    """Build one mode into ``<mode>.building.sqlite`` and atomically swap it in. Resumable."""
    directory = map_dir(db_path)
    new = directory / f'{mode}.building.sqlite'
    conn = None
    resume = False
    if new.exists():
        try:
            conn = open_store(new)
            resume = meta_get(conn, 'plan_id') == plan['id'] and meta_get(conn, 'schema') == SCHEMA_VERSION
        except sqlite3.DatabaseError:
            resume = False
        if not resume:
            if conn:
                conn.close()
            for suffix in ('', '-wal', '-shm'):
                Path(str(new) + suffix).unlink(missing_ok=True)
            conn = None
    if conn is None:
        conn = open_store(new, create=True)
        meta_set(conn, 'plan_id', plan['id'])
        meta_set(conn, 'schema', SCHEMA_VERSION)
        meta_set(conn, 'mode', mode)
        meta_set(conn, 'phase', 'load')
        meta_set(conn, 'cursor', 0)
    src = db.connect(db_path)
    src.execute('PRAGMA query_only=1')
    ctx = Ctx(plan)
    started = time.time()
    try:
        phase = meta_get(conn, 'phase')
        if phase == 'load':
            hi_id = src.execute('SELECT coalesce(max(id),0) FROM people').fetchone()[0]
            lo = meta_get(conn, 'cursor', 0)
            done = conn.execute('SELECT count(*) FROM mp').fetchone()[0]
            while lo <= hi_id:
                hi = lo + chunk
                feats = load_features(src, ctx, lo=lo, hi=hi)
                rows = [layout_row(mode, F, ctx) for F in feats.values()]
                conn.execute('BEGIN IMMEDIATE')
                if rows:
                    insert_rows(conn, rows)
                meta_set(conn, 'cursor', hi)
                conn.execute('COMMIT')
                done += len(rows)
                lo = hi
                if progress:
                    progress(mode, 'load', min(1.0, lo / max(1, hi_id + 1)), done, time.time() - started)
            meta_set(conn, 'phase', 'agg')
            phase = 'agg'
        if phase == 'agg':
            conn.execute('BEGIN IMMEDIATE')
            build_agg(conn)
            meta_set(conn, 'phase', 'final')
            conn.execute('COMMIT')
            if progress:
                progress(mode, 'agg', 1.0, 0, time.time() - started)
        n_bulk = conn.execute('SELECT count(*) FROM rtb').fetchone()[0]
        n_known = conn.execute('SELECT count(*) FROM rtk').fetchone()[0]
        old_rev = 0
        active = mode_path(db_path, mode)
        if mode_exists(db_path, mode):
            try:
                old = open_store(active, readonly=True)
                old_rev = meta_get(old, 'rev', 0)
                old.close()
            except sqlite3.DatabaseError:
                old_rev = 0
        conn.execute('BEGIN IMMEDIATE')
        meta_set(conn, 'quantiles', rank_quantiles(conn))
        meta_set(conn, 'plan', plan)
        meta_set(conn, 'groups', ctx.groups(mode))
        meta_set(conn, 'counts', {'bulk': n_bulk, 'known': n_known})
        build_id = hashlib.sha1(f'{plan["id"]}-{mode}-{time.time()}'.encode()).hexdigest()[:8]
        meta_set(conn, 'build_id', build_id)
        meta_set(conn, 'rev', old_rev + 1)
        meta_set(conn, 'built_at', db.now())
        meta_set(conn, 'phase', 'done')
        conn.execute('COMMIT')
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.close()
        conn = None
        for suffix in ('-wal', '-shm'):
            Path(str(new) + suffix).unlink(missing_ok=True)
        final = directory / f'{mode}.{build_id}.sqlite'
        os.replace(new, final)
        point_to(db_path, mode, final.name)
        if progress:
            progress(mode, 'done', 1.0, n_bulk + n_known, time.time() - started)
        return {'mode': mode, 'people': n_bulk + n_known, 'seconds': round(time.time() - started, 2)}
    finally:
        src.close()
        if conn:
            conn.close()


def _build_worker(args):
    db_path, mode, plan, chunk = args

    def progress(m, phase, frac, done, elapsed):
        _write_json(map_dir(db_path) / f'progress_{m}.json',
                    {'mode': m, 'phase': phase, 'progress': round(frac, 4), 'done': done,
                     'elapsed_s': round(elapsed, 1), 'at': time.time()})
    return build_mode(db_path, mode, plan, chunk, progress)


def build(db_path, modes=MODES, chunk=100_000, workers=1, fresh=False, log=None):
    """Full, resumable build of the requested modes. Reads the database, never blocks requests.

    ``fresh`` discards a half-finished build. Returns per-mode timings.
    """
    directory = map_dir(db_path)
    directory.mkdir(parents=True, exist_ok=True)
    with _lock(directory / 'build.lock'), _lock(directory / 'apply.lock'):
        src = db.connect(db_path)
        try:
            state = _read_json(directory / 'plan.json')
            if fresh or not state or state.get('state') != 'building' or state.get('modes') != list(modes) or state.get('schema') != SCHEMA_VERSION:
                plan = None
                # A partial-mode preparation must retain the geometry used by
                # the other current modes. Rebuild all modes to refresh anchors.
                if set(modes) != set(MODES):
                    for existing in MODES:
                        if not mode_exists(db_path, existing):
                            continue
                        active = open_store(mode_path(db_path, existing), readonly=True)
                        try:
                            if meta_get(active, 'schema') == SCHEMA_VERSION:
                                plan = meta_get(active, 'plan')
                        finally:
                            active.close()
                        if plan:
                            break
                plan = plan or make_plan(src)
                for mode in modes:
                    for suffix in ('', '-wal', '-shm'):
                        (directory / f'{mode}.building.sqlite{suffix}').unlink(missing_ok=True)
                # Retain queued edits: partial-mode builds and resumed builds must
                # not discard updates owed to other active layouts.
                _write_json(directory / 'plan.json', {'state': 'building', 'schema': SCHEMA_VERSION, 'modes': list(modes), 'plan': plan,
                                                      'started': time.time()})
            else:
                plan = state['plan']
        finally:
            src.close()
        started = time.time()
        jobs = [(str(db_path), mode, plan, chunk) for mode in modes]
        if workers > 1 and len(jobs) > 1:
            import multiprocessing
            with multiprocessing.get_context('spawn').Pool(min(workers, len(jobs))) as pool:
                results = pool.map(_build_worker, jobs, chunksize=1)
        else:
            results = [_build_worker(job) for job in jobs]
        _write_json(directory / 'plan.json', {'state': 'done', 'schema': SCHEMA_VERSION, 'modes': list(modes), 'plan': plan,
                                              'started': started, 'finished': time.time()})
        for mode in modes:
            (directory / f'progress_{mode}.json').unlink(missing_ok=True)
        if log:
            log(results)
        return {'seconds': round(time.time() - started, 2), 'modes': results}


# ---------- incremental apply ----------

def load_ctx(store):
    plan = meta_get(store, 'plan')
    return Ctx(plan) if plan else None


def _apply_to_store(store, mode, ctx, feats):
    """Bring the rows of ``feats`` (pid -> feature tuple or None) in line with the data. Returns changed count."""
    ids = list(feats)
    old = {}
    for chunk in _chunked(ids):
        for r in store.execute(f'SELECT * FROM mp WHERE person_id IN ({",".join("?" * len(chunk))})', chunk):
            old[r[0]] = tuple(r)
    delta = {}

    def bump(mx, my, cls, cluster, sign):
        for d, cy, cx in _agg_cells(mx, my):
            key = (d, cy, cx, cls, cluster)
            cell = delta.get(key)
            if cell is None:
                delta[key] = [sign, sign * mx, sign * my]
            else:
                cell[0] += sign
                cell[1] += sign * mx
                cell[2] += sign * my
    changed = 0
    store.execute('BEGIN IMMEDIATE')
    try:
        adds, removes = [], []
        for pid, F in feats.items():
            new = layout_row(mode, F, ctx) if F is not None else None
            prev = old.get(pid)
            if prev is not None and new is not None and tuple(prev) == tuple(new):
                continue
            if prev is None and new is None:
                continue
            changed += 1
            if prev is not None:
                removes.append(prev)
                bump(prev[1], prev[2], prev[5], prev[4], -1)
            if new is not None:
                adds.append(new)
                bump(new[1], new[2], new[5], new[4], 1)
        if changed:
            for chunk in _chunked([r[0] for r in removes]):
                slots = ','.join('?' * len(chunk))
                store.execute(f'DELETE FROM rtb WHERE id IN ({slots})', chunk)
                store.execute(f'DELETE FROM rtk WHERE id IN ({slots})', chunk)
                store.execute(f'DELETE FROM mp WHERE person_id IN ({slots})', chunk)
            if adds:
                insert_rows(store, adds)
            store.executemany('INSERT INTO agg VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(depth,cy,cx,cls,cluster) '
                              'DO UPDATE SET n=n+excluded.n,sx=sx+excluded.sx,sy=sy+excluded.sy',
                              ((*k, v[0], v[1], v[2]) for k, v in delta.items()))
            store.executemany('DELETE FROM agg WHERE depth=? AND cy=? AND cx=? AND cls=? AND cluster=? AND n<=0',
                              (k for k in delta))
            meta_set(store, 'rev', meta_get(store, 'rev', 0) + 1)
        store.execute('COMMIT')
    except BaseException:
        store.execute('ROLLBACK')
        raise
    return changed


def apply_ids(db_path, ids, main=None):
    """Recompute the layout rows of ``ids`` in every built mode. Returns {mode: changed}."""
    own = main is None
    main = main or db.connect(db_path)
    out = {}
    try:
        feats = None
        ids = list(dict.fromkeys(ids))
        for mode in MODES:
            if not mode_exists(db_path, mode):
                continue
            store = open_store(mode_path(db_path, mode))
            try:
                if meta_get(store, 'schema') != SCHEMA_VERSION:
                    continue
                ctx = load_ctx(store)
                if ctx is None:
                    continue
                if feats is None:
                    if ctx.owner_id in ids:
                        # Reverse source-list changes queue the owner member. Refresh
                        # the source endpoints too, including former follows on deletion.
                        related = [r[0] for r in main.execute(
                            'SELECT DISTINCT p.id FROM current_edges e JOIN people p ON p.handle=e.seed '
                            'WHERE e.person_id=?', (ctx.owner_id,))]
                        related.extend(r[0] for r in store.execute('SELECT person_id FROM mp WHERE cls>=64'))
                        ids = list(dict.fromkeys([*ids, *related]))
                    feats = features_for(main, ctx, ids)
                out[mode] = _apply_to_store(store, mode, ctx, feats)
            finally:
                store.close()
    finally:
        if own:
            main.close()
    return out


def pending(conn, cap=100_000):
    return conn.execute('SELECT count(*) FROM (SELECT 1 FROM map_layout_dirty LIMIT ?)', (cap,)).fetchone()[0]


def has_pending(conn):
    return conn.execute('SELECT 1 FROM map_layout_dirty LIMIT 1').fetchone() is not None


def apply_dirty(db_path, limit=5000, main=None):
    """Drain up to ``limit`` queued people into the layouts. Safe to call from a worker or the CLI."""
    directory = map_dir(db_path)
    if not any(mode_exists(db_path, m) for m in MODES) or is_building(db_path):
        return {'applied': 0, 'skipped': 'no layout' if not directory.exists() else 'building'}
    own = main is None
    main = main or db.connect(db_path)
    try:
        try:
            with _lock(directory / 'apply.lock'):
                # A crash between claiming and acknowledging leaves claimed rows: take them back.
                main.execute('UPDATE map_layout_dirty SET claimed=0 WHERE claimed=1')
                main.commit()
                ids = [r[0] for r in main.execute('SELECT person_id FROM map_layout_dirty ORDER BY person_id LIMIT ?',
                                                  (limit,))]
                if not ids:
                    return {'applied': 0, 'pending': 0}
                for chunk in _chunked(ids):
                    main.execute(f'UPDATE map_layout_dirty SET claimed=1 WHERE person_id IN ({",".join("?" * len(chunk))})', chunk)
                main.commit()
                changed = apply_ids(db_path, ids, main=main)
                for chunk in _chunked(ids):
                    main.execute('DELETE FROM map_layout_dirty WHERE claimed=1 AND '
                                 f'person_id IN ({",".join("?" * len(chunk))})', chunk)
                main.commit()
                return {'applied': len(ids), 'changed': changed, 'pending': pending(main)}
        except BuildBusy:
            return {'applied': 0, 'skipped': 'busy'}
    finally:
        if own:
            main.close()


# ---------- plan drift (sources whose weight changed) ----------

def check_plan(db_path, main=None):
    """Compare the sources' weight classes with the plan. Requeue the members of a source whose class
    changed (closeness depends on it) and report new sources, which only a rebuild gives a cluster."""
    own = main is None
    main = main or db.connect(db_path)
    try:
        if not mode_exists(db_path, 'closeness'):
            return {'stale': False}
        store = open_store(mode_path(db_path, 'closeness'), readonly=True)
        try:
            plan = meta_get(store, 'plan')
        finally:
            store.close()
        if not plan:
            return {'stale': False}
        owner = _owner_handle(main).lower()
        seeds = [r[0].lower() for r in main.execute('SELECT seed FROM map_seed_degree')]
        classes = source_classes(main, [s for s in seeds if s != owner])
        known = known_source_handles(main, classes)
        membership_changed = set(known) ^ set(plan.get('known_sources', []))
        changed = sorted({h for h, c in classes.items() if h in plan['src'] and plan['src'][h][1] != c} |
                         {h for h in membership_changed if h in plan['src']})
        added = [h for h in classes if h not in plan['src']]
        requeued = 0
        for handle in changed:
            main.execute('INSERT INTO map_layout_dirty(person_id,claimed) SELECT person_id,0 FROM map_seed_member '
                         'WHERE seed=? ON CONFLICT(person_id) DO UPDATE SET claimed=0', (handle,))
            requeued += main.execute('SELECT changes()').fetchone()[0]
        if changed:
            main.commit()
            for mode in MODES:
                if mode_exists(db_path, mode):
                    s = open_store(mode_path(db_path, mode))
                    try:
                        stored = meta_get(s, 'plan')
                        for handle in changed:
                            stored['src'][handle][1] = classes[handle]
                        stored['known_sources'] = sorted(known)
                        meta_set(s, 'plan', stored)
                    finally:
                        s.close()
        return {'stale': bool(added), 'new_sources': added[:20], 'requeued': requeued,
                'changed_sources': changed[:20]}
    finally:
        if own:
            main.close()


# ---------- status ----------

def status(db_path, main=None):
    directory = map_dir(db_path)
    out = {'dir': str(directory), 'modes': {}, 'building': is_building(db_path)}
    for mode in MODES:
        path = mode_path(db_path, mode)
        info = {'built': mode_exists(db_path, mode)}
        if info['built']:
            store = open_store(path, readonly=True)
            try:
                info.update(rev=meta_get(store, 'rev'), built_at=meta_get(store, 'built_at'),
                            build_id=meta_get(store, 'build_id'), counts=meta_get(store, 'counts'),
                            bytes=path.stat().st_size)
                info['schema'] = meta_get(store, 'schema')
                info['ready'] = info['schema'] == SCHEMA_VERSION
            finally:
                store.close()
        progress = _read_json(directory / f'progress_{mode}.json')
        if progress and out['building']:
            info['progress'] = progress
        out['modes'][mode] = info
    if main is not None:
        out['pending'] = pending(main)
    return out


def step(db_path, main):
    """Worker tick: drain the queue in small batches, look at plan drift now and then."""
    out = {}
    if has_pending(main):
        out['apply'] = apply_dirty(db_path, 4000, main=main)
    now = time.time()
    if now - step.last_plan_check > 60:
        step.last_plan_check = now
        out['plan'] = check_plan(db_path, main=main)
    return out


step.last_plan_check = 0.0


def main():
    ap = argparse.ArgumentParser(description='Build and maintain the map layouts (docs/MAP_VIEW_API.md).')
    ap.add_argument('--db', required=True)
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build', help='full build; resumes a half-finished one')
    b.add_argument('--modes', default=','.join(MODES))
    b.add_argument('--chunk', type=int, default=100_000)
    b.add_argument('--workers', type=int, default=4)
    b.add_argument('--fresh', action='store_true', help='discard a half-finished build')
    a = sub.add_parser('apply', help='apply queued changes')
    a.add_argument('--limit', type=int, default=5000)
    sub.add_parser('status')
    args = ap.parse_args()
    if args.cmd == 'build':
        modes = [m for m in args.modes.split(',') if m]
        bad = [m for m in modes if m not in MODES]
        if bad:
            ap.error('unknown mode ' + ','.join(bad))
        print(json.dumps(build(args.db, modes, args.chunk, args.workers, args.fresh)))
    elif args.cmd == 'apply':
        conn = db.connect(args.db)
        while True:
            out = apply_dirty(args.db, args.limit, main=conn)
            print(json.dumps(out))
            if not out.get('applied'):
                break
        conn.close()
    else:
        conn = db.connect(args.db)
        print(json.dumps(status(args.db, conn), indent=2))
        conn.close()


if __name__ == '__main__':
    main()
