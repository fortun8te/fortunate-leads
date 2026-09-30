"""Read path of the map: /api/map/view, /api/map/edges, /api/map/search (docs/MAP_VIEW_API.md).

A view is answered from the precomputed layout of one mode:

* the top ``budget`` people inside the rectangle come from an R*Tree over (x, y, rank): a rank cut is
  found by counting with a hard LIMIT, then one ordered fetch returns exactly the top-ranked people;
* the number of everyone else, per grid cell and cluster, comes from the count pyramid;
* only then are handle and name read from the main database, for the people that are shown.

Every request reads one snapshot of one layout file, so totals and nodes always agree.
"""
import contextlib
import hashlib
import heapq
from itertools import islice
import json
import math
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict

import db
import map_layout as ML

BUDGET_DEFAULT, BUDGET_MIN, BUDGET_MAX = 600, 50, 1500
BUBBLE_MAX = 160          # most (cell, cluster) bubbles in one response
BUBBLE_GRID = 8           # cells across the longer side of the viewport
POOL_FRACTION = 0.5       # extra ranked people fetched to name each bubble's top ids
Q_CAP = 2000
SEARCH_MIN = 2
SEARCH_POOL_TOP = 65536
SEARCH_POOL_KNOWN = 200_000
MAX_EDGE_IDS = 200


class Raw:
    """An already encoded response for the HTTP layer (body may be empty for a 304)."""

    def __init__(self, body, etag=None, status=200, headers=None):
        self.body, self.etag, self.status = body, etag, status
        self.headers = dict(headers or {})
        if etag:
            self.headers['ETag'] = etag
        self.headers.setdefault('Cache-Control', 'no-cache')


# ---------- layout readers ----------

_POOL = {}
_POOL_LOCK = threading.Lock()
_POOL_KEEP = 2
_POOL_MAX = 8
_POINTER = {}


def _current(db_path, mode):
    """Active layout file of a mode (None when unbuilt). The pointer is a tiny JSON file; stat-cache it."""
    pointer = ML._pointer(db_path)
    try:
        st = os.stat(pointer)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    cached = _POINTER.get(str(pointer))
    if cached is None or cached[0] != key:
        cached = (key, ML._read_json(pointer, {}) or {})
        _POINTER[str(pointer)] = cached
    name = cached[1].get(mode)
    if not name:
        return None
    path = ML.map_dir(db_path) / name
    return path if path.exists() else None


def _take(path):
    with _POOL_LOCK:
        idle = _POOL.get(str(path))
        if idle:
            conn = idle.pop()
            if not idle:
                _POOL.pop(str(path), None)
            return conn
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=15)
    conn.execute('PRAGMA query_only=1')
    conn.execute('PRAGMA busy_timeout=15000')
    conn.execute('PRAGMA mmap_size=268435456')
    conn.execute('PRAGMA cache_size=-16384')
    conn.execute('PRAGMA temp_store=FILE')
    return conn


def _give(path, conn):
    with _POOL_LOCK:
        if sum(len(items) for items in _POOL.values()) >= _POOL_MAX:
            for key, items in list(_POOL.items()):
                if items:
                    items.pop(0).close()
                    if not items:
                        _POOL.pop(key, None)
                    break
        idle = _POOL.setdefault(str(path), [])
        if len(idle) < _POOL_KEEP and os.path.exists(path):
            idle.append(conn)
            return
    conn.close()


def close_pool():
    with _POOL_LOCK:
        for idle in _POOL.values():
            for conn in idle:
                conn.close()
        _POOL.clear()
        _POINTER.clear()


class Store:
    def __init__(self, conn, path, mode):
        self.conn, self.path, self.mode = conn, path, mode
        self.rev = ML.meta_get(conn, 'rev', 0)
        self.build_id = ML.meta_get(conn, 'build_id', '0')
        self._groups = None

    def meta(self, key, default=None):
        return ML.meta_get(self.conn, key, default)

    def groups(self):
        if self._groups is None:
            self._groups = self.meta('groups', [])
        return self._groups


@contextlib.contextmanager
def reader(db_path, mode):
    """One read snapshot of the active layout file for ``mode`` (yields None when it is not built)."""
    path = _current(db_path, mode)
    if path is None:
        yield None
        return
    conn = _take(path)
    try:
        conn.execute('BEGIN')
        try:
            yield Store(conn, path, mode) if ML.meta_get(conn, 'schema') == ML.SCHEMA_VERSION else None
        finally:
            conn.execute('ROLLBACK')
    except BaseException:
        conn.close()
        raise
    else:
        _give(path, conn)


# ---------- parameters ----------

def _float(q, key, default):
    raw = q.get(key, [''])[0].strip()
    if raw == '':
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f'{key} must be a number') from None
    if value != value or value in (float('inf'), float('-inf')):
        raise ValueError(f'{key} must be a number')
    return value


def parse_status(text, mode):
    """Set of status codes for the ``status`` filter (None when absent)."""
    if not text.strip():
        return None
    codes = set()
    for token in (t.strip().lower() for t in text.split(',') if t.strip()):
        if token == 'all':
            codes |= set(range(7))
        elif token == 'none':
            codes.add(0)
        elif token in ML.STATUS_CODE and token:
            codes.add(ML.STATUS_CODE[token])
        else:
            raise ValueError('bad status')
    return codes


FOLLOW_FILTERS = {'all': (0, 1, 2, 3, 4, 6), 'following': (1, 3), 'followers': (2, 3, 6),
                  'mutual': (3,), 'not_following': (4, 6), 'unknown': (0, 2)}


def class_mask(mode, scope, min_fit, codes, follow='all'):
    if codes is None:
        codes = set(range(7)) if mode == 'status' else set(range(6))
    minband = 0 if min_fit is None else ML.BAND_EDGES.index(min_fit) + 1
    mask = 0
    for direction in FOLLOW_FILTERS[follow]:
        for code in codes:
            for band in range(7):
                cls = direction * 64 + code * 8 + band
                if band < minband or (scope == 'leads' and not ML.is_lead(cls)):
                    continue
                mask |= 1 << cls
    return mask


class Query:
    def __init__(self, q):
        self.mode = q.get('mode', ['closeness'])[0].strip() or 'closeness'
        if self.mode not in ML.MODES:
            raise ValueError('mode must be closeness, fit, seeds or status')
        x0, y0 = _float(q, 'x0', 0.0), _float(q, 'y0', 0.0)
        x1, y1 = _float(q, 'x1', 1.0), _float(q, 'y1', 1.0)
        x0, y0, x1, y1 = (min(1.0, max(0.0, v)) for v in (x0, y0, x1, y1))
        if not (x0 < x1 and y0 < y1):
            raise ValueError('x0<x1 and y0<y1 are required')
        self.rect = (x0, y0, x1, y1)
        raw = q.get('budget', [''])[0].strip()
        try:
            budget = int(raw) if raw else BUDGET_DEFAULT
        except ValueError:
            raise ValueError('budget must be a whole number') from None
        self.budget = min(BUDGET_MAX, max(BUDGET_MIN, budget))
        scope = q.get('scope', [''])[0].strip().lower()
        if scope not in ('', 'leads', 'all'):
            raise ValueError('scope must be leads or all')
        self.scope = scope or ('all' if self.mode == 'status' else 'leads')
        raw = q.get('min_fit', [''])[0].strip()
        if raw:
            try:
                value = float(raw)
            except ValueError:
                raise ValueError('min_fit must be a number') from None
            if not 0 <= value <= 100:
                raise ValueError('min_fit must be between 0 and 100')
            self.min_fit = ML.snap_min_fit(value)
        else:
            self.min_fit = None
        status = q.get('status', [''])[0]
        self.codes = parse_status(status, self.mode)
        self.status_text = ','.join(sorted(t.strip().lower() for t in status.split(',') if t.strip())) or None
        self.text = q.get('q', [''])[0].strip()
        if self.text and len(self.text.lstrip('@')) < SEARCH_MIN:
            raise ValueError('q needs at least 2 characters')
        self.follow = q.get('follow', ['all'])[0].strip().lower() or 'all'
        if self.follow not in FOLLOW_FILTERS:
            raise ValueError('follow must be all, following, followers, mutual, not_following or unknown')
        overview = q.get('overview', ['0'])[0].strip()
        if overview not in ('0', '1', ''):
            raise ValueError('overview must be 0 or 1')
        self.overview = overview == '1'
        self.mask = class_mask(self.mode, self.scope, self.min_fit, self.codes, self.follow)

    def key(self):
        return json.dumps([self.mode, list(self.rect), self.budget, self.scope, self.min_fit,
                           self.status_text, self.text.lower(), self.follow, self.overview], separators=(',', ':'))


def _etag(store, query):
    digest = hashlib.sha1(query.key().encode()).hexdigest()[:10]
    return f'W/"{store.build_id}-{store.rev}-{digest}"'


# ---------- geometry ----------

def choose_depth(x0, y0, x1, y1):
    span = max(x1 - x0, y1 - y0)
    return max(1, min(ML.DEPTHS[-1], round(math.log2(BUBBLE_GRID / span))))


def snap(rect, depth):
    """Cell index range covering ``rect`` at ``depth`` and the exact rectangle they span."""
    n = 1 << depth
    x0, y0, x1, y1 = rect
    cx0, cy0 = min(n - 1, int(x0 * n)), min(n - 1, int(y0 * n))
    cx1, cy1 = min(n - 1, max(cx0, math.ceil(x1 * n) - 1)), min(n - 1, max(cy0, math.ceil(y1 * n) - 1))
    return (cx0, cy0, cx1, cy1), (cx0 / n, cy0 / n, (cx1 + 1) / n, (cy1 + 1) / n)


def read_cells(store, depth, cells, mask):
    """{(cy, cx, cluster): [n, sx, sy]} for the allowed classes inside the cell range, and their total."""
    cx0, cy0, cx1, cy1 = cells
    groups, total = {}, 0
    execute = store.conn.execute
    for cy in range(cy0, cy1 + 1):
        for cx, cls, cluster, n, sx, sy in execute(
                'SELECT cx,cls,cluster,n,sx,sy FROM agg WHERE depth=? AND cy=? AND cx BETWEEN ? AND ?',
                (depth, cy, cx0, cx1)):
            if (mask >> cls) & 1:
                g = groups.get((cy, cx, cluster))
                if g is None:
                    groups[(cy, cx, cluster)] = [n, sx, sy]
                else:
                    g[0] += n
                    g[1] += sx
                    g[2] += sy
                total += n
    return groups, total


def world_total(store, mask):
    return sum(n for cls, n in store.conn.execute('SELECT cls,sum(n) FROM agg WHERE depth=1 GROUP BY cls')
               if (mask >> cls) & 1)


# ---------- top people by rank ----------

_CNT_B = ('SELECT count(*) FROM (SELECT 1 FROM rtb WHERE maxx>=? AND minx<=? AND maxy>=? AND miny<=? '
          'AND minr>=? LIMIT ?)')
_CNT_K = ('SELECT count(*) FROM (SELECT 1 FROM rtk WHERE maxx>=? AND minx<=? AND maxy>=? AND miny<=? '
          'AND minr>=? AND {classes} LIMIT ?)')
_TOP_B = 'SELECT id,minr,0 FROM rtb WHERE maxx>=? AND minx<=? AND maxy>=? AND miny<=? AND minr>=?'
_TOP_K = ('SELECT id,minr,cls FROM rtk WHERE maxx>=? AND minx<=? AND maxy>=? AND miny<=? AND minr>=? '
          'AND {classes}')


def _class_sql(mask):
    # SQLite integers cannot hold the direction/status/fit bitset. The class
    # universe is bounded, and every literal here comes from an integer bit index.
    values = [str(cls) for cls in range(7 * 64) if (mask >> cls) & 1]
    return 'cls IN (' + ','.join(values) + ')' if values else '0'


def _quantile_cut(quantiles, want, fraction):
    """A first guess of the rank cut: the global rank whose count is ``want / fraction`` from the top."""
    goal = want / max(fraction, 1e-9)
    prev = None
    for count, rank in quantiles:
        if count >= goal:
            return rank if prev is None else (rank + prev) / 2
        prev = rank
    return quantiles[-1][1] if quantiles else 0.0


def top_ranked(store, rect, mask, want, fraction):
    """The ``want`` highest-ranked allowed people in ``rect`` as [(id, rank, cls)], best first.

    Ranks are searched with capped counts (at most ~2*want rows are ever touched per probe), then
    a single ordered fetch returns exactly the top of the set (ties broken by id).
    """
    x0, y0, x1, y1 = rect
    conn = store.conn
    if rect == (0.0, 0.0, 1.0, 1.0) and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='mp_class_rank'").fetchone():
        classes = [r[0] for r in conn.execute('SELECT DISTINCT cls FROM agg WHERE depth=1') if (mask >> r[0]) & 1]
        cursors = [conn.execute('SELECT person_id,rk,cls FROM mp WHERE cls=? ORDER BY rk DESC,person_id LIMIT ?',
                                (cls, want)) for cls in classes]
        return list(islice(heapq.merge(*cursors, key=lambda row: (-row[1], row[0])), want))
    bulk = mask & 1
    class_sql = _class_sql(mask)

    def count(tau, cap):
        n = conn.execute(_CNT_K.format(classes=class_sql), (x0, x1, y0, y1, tau, cap)).fetchone()[0]
        if bulk and n < cap:
            n += conn.execute(_CNT_B, (x0, x1, y0, y1, tau, cap - n)).fetchone()[0]
        return n

    cap = 2 * want + 8
    if count(-1.0, cap) < cap:
        tau = -1.0                                # fewer people than the cap: take them all
    else:
        lo, hi = -1.0, 1.001                      # count(lo) >= cap, count(hi) == 0
        guess = _quantile_cut(store.meta('quantiles', []), want, fraction)
        tau = None
        for step in range(48):
            mid = guess if step == 0 else (lo + hi) / 2
            if not lo < mid < hi:
                mid = (lo + hi) / 2
            n = count(mid, cap)
            if n >= cap:
                lo = mid
            elif n >= want:
                tau = mid
                break
            else:
                hi = mid
        if tau is None:
            tau = lo                              # a huge tie: exact but heavy
    sql = _TOP_K.format(classes=class_sql) + (' UNION ALL ' + _TOP_B if bulk else '') + ' ORDER BY 2 DESC,1 LIMIT ?'
    args = [x0, x1, y0, y1, tau] + ([x0, x1, y0, y1, tau] if bulk else []) + [want]
    return [(r[0], r[1], r[2]) for r in conn.execute(sql, args)]


# ---------- view ----------

_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 96


def clear_cache():
    with _CACHE_LOCK:
        _CACHE.clear()


def _encode(body):
    return json.dumps(body, separators=(',', ':')).encode()


def not_ready(db_path, query, main=None):
    progress = None
    building = ML.is_building(db_path)
    if building:
        info = ML._read_json(ML.map_dir(db_path) / f'progress_{query.mode}.json')
        progress = info.get('progress') if info else 0.0
    return {'rev': 0, 'mode': query.mode, 'ready': False, 'world': {'w': 1, 'h': 1},
            'viewport': dict(zip(('x0', 'y0', 'x1', 'y1'), query.rect)), 'nodes': [], 'clusters': [], 'groups': [],
            'total': 0, 'shown': 0, 'hidden': 0, 'world_total': 0,
            'filters': _filters(query, False),
            'layout': {'ready': False, 'rev': 0, 'pending': ML.pending(main) if main is not None else 0,
                       'built_at': None, 'building': building, 'progress': progress}}


def _filters(query, capped):
    return {'scope': query.scope, 'min_fit': query.min_fit, 'status': query.status_text,
            'q': query.text or None, 'q_capped': capped, 'budget': query.budget,
            'follow': query.follow, 'overview': query.overview,
            'follow_note': 'Not following requires explicit absence at the last complete check; missing evidence stays unknown.'}


def _details(store, ids):
    out = {}
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        for r in store.conn.execute('SELECT person_id,mx,my,rk,cluster,cls,fit,cl,src FROM mp WHERE person_id IN (%s)'
                                    % ','.join('?' * len(chunk)), chunk):
            out[r[0]] = r
    return out


def _people(conn, ids):
    out = {}
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        for r in conn.execute('SELECT id,handle,name,pic_file FROM people WHERE id IN (%s)' % ','.join('?' * len(chunk)), chunk):
            out[r[0]] = (r[1], r[2], f'/img/{r[0]}' if r[3] else None)
    return out


def _bubbles(groups, shown, pool, label_of, depth, per_cell):
    """Turn cell/cluster counts into bubbles: subtract the people already shown, name the next-ranked ones."""
    def key(mx, my, cluster):
        cy, cx = ML.cell_of(mx, my, depth)
        return (cy, cx, -1 if per_cell else cluster)
    dominant = {}
    if per_cell:
        merged = {}
        for (cy, cx, cluster), (n, sx, sy) in groups.items():
            g = merged.setdefault((cy, cx, -1), [0, 0, 0])
            g[0] += n
            g[1] += sx
            g[2] += sy
            best = dominant.get((cy, cx))
            if best is None or n > best[1]:
                dominant[(cy, cx)] = (cluster, n)
        groups = merged
    for row in shown:
        g = groups[key(row[1], row[2], row[4])]
        g[0] -= 1
        g[1] -= row[1]
        g[2] -= row[2]
    tops = {}
    for row in pool:
        k = key(row[1], row[2], row[4])
        lst = tops.setdefault(k, [])
        if len(lst) < 3:
            lst.append(row[0])
    out = []
    for (cy, cx, cluster), (n, sx, sy) in groups.items():
        if n <= 0:
            continue
        cid = dominant[(cy, cx)][0] if per_cell else cluster
        out.append({'id': ((depth * 2048 + cy) * 2048 + cx) * 1024 + (cid if not per_cell else 1023),
                    'x': round(ML.coord(0) + (sx / n) / ML.MICRO, 7), 'y': round(ML.coord(0) + (sy / n) / ML.MICRO, 7),
                    'count': n, 'label': label_of.get(cid, 'People'), 'top_ids': tops.get((cy, cx, cluster), [])})
    out.sort(key=lambda b: (-b['count'], b['id']))
    return out


def _node(row, people, mode):
    pid, mx, my, rk, cluster, cls, fit, cl, src = row
    handle, name, pic = people.get(pid, ('', None, None))
    status = ML.CODE_STATUS.get((cls % 64) >> 3)
    follow = cls // 64
    node = {'id': pid, 'handle': handle, 'name': name, 'x': round(ML.coord(mx), 7), 'y': round(ML.coord(my), 7),
            'rank': round(rk, 6), 'fit': fit, 'status': status, 'cluster': cluster, 'closeness': cl / 1000.0,
            'lead': ML.is_lead(cls), 'pic': pic, 'followed': bool(follow & 1), 'follows_me': bool(follow & 2),
            'following_evidence': 'observed' if follow & 1 else 'absent' if follow & 4 else 'unknown'}
    if src:
        node['source'] = True
    return node


def view(conn, db_path, q, if_none_match=None, cache=True):
    """Answer /api/map/view. ``conn`` is the main database, only used for handles and names."""
    query = Query(q)
    with reader(db_path, query.mode) as store:
        if store is None:
            return Raw(_encode(not_ready(db_path, query, conn)), None, 200, {'Cache-Control': 'no-store'})
        main_rev = db.get_setting(conn, 'lead_data_rev', 0)
        etag = _etag(store, query)[:-1] + f'-{main_rev}\"'
        changing = ML.has_pending(conn) or ML.is_building(db_path)
        cache = cache and not changing
        if cache and if_none_match and etag in [t.strip() for t in if_none_match.split(',')]:
            return Raw(b'', etag, 304)
        if cache:
            with _CACHE_LOCK:
                hit = _CACHE.get(etag)
                if hit is not None:
                    _CACHE.move_to_end(etag)
                    return Raw(hit, etag)
        result = _view(conn, db_path, store, query)
        owner_id = (store.meta('plan', {}) or {}).get('owner_id')
        if owner_id:
            own = _details(store, [owner_id]).get(owner_id)
            if own is not None and conn.execute('SELECT 1 FROM people WHERE id=?', (owner_id,)).fetchone():
                result['world']['me'] = _node(own, _people(conn, [owner_id]), query.mode)
        result['layout']['pending'] = ML.pending(conn)
        result['layout']['building'] = ML.is_building(db_path)
        body = _encode(result)
    if cache:
        with _CACHE_LOCK:
            _CACHE[etag] = body
            while len(_CACHE) > _CACHE_MAX:
                _CACHE.popitem(last=False)
    return Raw(body, None, headers={'Cache-Control': 'no-store'}) if changing else Raw(body, etag)


def _view(conn, db_path, store, query):
    mode, mask, budget = store.mode, query.mask, query.budget
    depth = choose_depth(*query.rect)
    capped = False
    wt = world_total(store, mask)
    label_of = {g['id']: g['label'] for g in store.groups()}
    if query.text:
        found, capped = _search_pool(conn, db_path, query.text, store)
        return _view_candidates(conn, store, query, found, capped, depth, label_of, wt)
    per_cell = False
    while True:
        cells, rect = snap(query.rect, depth)
        groups, total = read_cells(store, depth, cells, mask)
        if len(groups) <= BUBBLE_MAX or depth == 1:
            break
        depth -= 1
    if len(groups) > BUBBLE_MAX:
        per_cell = True
    want = min(total, budget + int(budget * POOL_FRACTION))
    fraction = total / wt if wt else 1.0
    ranked = top_ranked(store, rect, mask, want, fraction) if want else []
    details = _details(store, [r[0] for r in ranked])
    rows = [details[r[0]] for r in ranked if r[0] in details]
    shown, pool = rows[:budget], rows[budget:]
    if query.overview and mode == 'closeness' and query.rect == (0., 0., 1., 1.) and rows:
        shown, pool = _overview_rows(store, rows, groups, depth, mask, budget)
    people = _people(conn, [r[0] for r in shown])
    nodes = [_node(r, people, mode) for r in shown]
    bubbles = _bubbles(groups, shown, pool, label_of, depth, per_cell)
    x0, y0, x1, y1 = rect
    inside = [g for g in store.groups() if x0 <= g['x'] <= x1 and y0 <= g['y'] <= y1]
    return _response(store, query, rect, nodes, bubbles, inside, total, wt, capped)


def _overview_rows(store, ranked, groups, depth, mask, budget):
    """Reserve a quarter of an explicit overview for occupied spatial sectors.

    At most twelve existing count cells are probed, with the same bounded rank
    fetch. All returned people remain actual layout rows and count subtraction
    uses their exact cells. Ordinary ranked views keep their original ordering.
    """
    reserve = min(budget // 4, 120)
    shown = list(ranked[:max(1, budget - reserve)])
    ids = {r[0] for r in shown}
    cells = {}
    for (cy, cx, _), (n, sx, sy) in groups.items():
        previous = cells.setdefault((cy, cx), [0, 0, 0])
        previous[0] += n; previous[1] += sx; previous[2] += sy
    sectors = {}
    for (cy, cx), (n, sx, sy) in cells.items():
        x, y = sx / n / ML.MICRO, sy / n / ML.MICRO
        radius = math.hypot(x - .5, y - .5)
        if radius < .18:
            continue
        sector = int(((math.atan2(y - .5, x - .5) + math.pi) / ML.TWO_PI) * 12) % 12
        previous = sectors.get(sector)
        if previous is None or n > previous[0]:
            sectors[sector] = (n, cy, cx)
    extra = []
    per_cell = max(1, math.ceil(reserve / max(1, len(sectors))))
    grid = 1 << depth
    for _, cy, cx in sectors.values():
        rect = (cx / grid, cy / grid, (cx + 1) / grid, (cy + 1) / grid)
        candidates = top_ranked(store, rect, mask, per_cell + 2, 1 / (grid * grid))
        found = _details(store, [r[0] for r in candidates])
        extra.extend(found[r[0]] for r in candidates if r[0] in found)
    for row in extra + ranked:
        if row[0] not in ids and len(shown) < budget:
            shown.append(row); ids.add(row[0])
    pool = [r for r in ranked if r[0] not in ids]
    return shown, pool


def _response(store, query, rect, nodes, bubbles, groups, total, wt, capped):
    shown = len(nodes)
    counts = store.meta('counts', {})
    world = {'w': 1, 'h': 1}
    if store.mode == 'closeness' and store.meta('plan', {}).get('network_disk'):
        world.update(layout='network_disk', center={'x': .5, 'y': .5}, radius=.47,
                     distance_note='Broad bands reflect recorded connection evidence; position within an audience spreads people for readability.')
    return {'rev': store.rev, 'mode': store.mode, 'ready': True, 'world': world,
            'viewport': dict(zip(('x0', 'y0', 'x1', 'y1'), rect)), 'nodes': nodes, 'clusters': bubbles,
            'groups': groups, 'total': total, 'shown': shown, 'hidden': total - shown, 'world_total': wt,
            'filters': _filters(query, capped),
            'layout': {'ready': True, 'rev': store.rev, 'pending': 0, 'built_at': store.meta('built_at'),
                       'building': False, 'progress': None, 'people': counts.get('bulk', 0) + counts.get('known', 0)}}


def _view_candidates(conn, store, query, found, capped, depth, label_of, wt):
    """A text search restricts the population to a short candidate list; same shape of answer."""
    mask, budget = query.mask, query.budget
    details = _details(store, found)
    cands = [details[i] for i in found if i in details and (mask >> details[i][5]) & 1]
    while True:
        cells, rect = snap(query.rect, depth)
        x0, y0, x1, y1 = rect
        inside = [r for r in cands if x0 <= ML.coord(r[1]) < x1 and y0 <= ML.coord(r[2]) < y1]
        groups = {}
        for r in inside:
            cy, cx = ML.cell_of(r[1], r[2], depth)
            g = groups.setdefault((cy, cx, r[4]), [0, 0, 0])
            g[0] += 1
            g[1] += r[1]
            g[2] += r[2]
        if len(groups) <= BUBBLE_MAX or depth == 1:
            break
        depth -= 1
    inside.sort(key=lambda r: (-r[3], r[0]))
    shown = inside[:budget]
    pool = inside[budget:budget + int(budget * POOL_FRACTION)]
    people = _people(conn, [r[0] for r in shown])
    nodes = [_node(r, people, store.mode) for r in shown]
    bubbles = _bubbles(groups, shown, pool, label_of, depth, len(groups) > BUBBLE_MAX)
    x0, y0, x1, y1 = rect
    gs = [g for g in store.groups() if x0 <= g['x'] <= x1 and y0 <= g['y'] <= y1]
    return _response(store, query, rect, nodes, bubbles, gs, len(inside), wt, capped)


# ---------- search ----------

_SEARCH_INDEX = {}
_SEARCH_LOCK = threading.Lock()


def _normalize(text):
    text = str(text or '').strip()
    if len(text) > 512:
        raise ValueError('Search for a name or Instagram handle.')
    from urllib.parse import urlsplit, unquote
    candidate = text if '://' in text else 'https://' + text
    parsed = urlsplit(candidate)
    if parsed.hostname and parsed.hostname.lower() in ('instagram.com', 'www.instagram.com', 'm.instagram.com'):
        parts = [unquote(p) for p in parsed.path.split('/') if p]
        if len(parts) != 1 or not re.fullmatch(r'[A-Za-z0-9_.]{1,30}', parts[0]):
            raise ValueError('Use an Instagram profile link, not a post or reel.')
        text = parts[0]
    return text.lstrip('@').strip().casefold()


def _prefix_upper(text):
    return text[:-1] + chr(ord(text[-1]) + 1)


def _index_for(conn, db_path):
    """Handles and names of the engaged people and the top of the closeness layout (cached)."""
    path = _current(db_path, 'closeness')
    with reader(db_path, 'closeness') as active:
        revision = (active.build_id, active.rev) if active else None
    key = (str(path), revision, db.get_setting(conn, 'lead_data_rev', 0))
    with _SEARCH_LOCK:
        hit = _SEARCH_INDEX.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
    ids = []
    with reader(db_path, 'closeness') as store:
        if store is None:
            return []
        engaged_mask = class_mask('status', 'all', None, None) & ~sum(1 << (direction * 64) for direction in FOLLOW_FILTERS['all'])
        ids = [r[0] for r in store.conn.execute('SELECT person_id FROM mp WHERE ' + _class_sql(engaged_mask) +
                                              ' LIMIT ?', (SEARCH_POOL_KNOWN,))]
        total = sum((store.meta('counts', {}) or {}).values())
        top = top_ranked(store, (0.0, 0.0, 1.0, 1.0), class_mask('status', 'all', None, None), min(SEARCH_POOL_TOP, total), 1.0)
        ids.extend(r[0] for r in top)
    ids = list(dict.fromkeys(ids))
    entries = []
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        for r in conn.execute('SELECT id,handle,name FROM people WHERE id IN (%s)' % ','.join('?' * len(chunk)), chunk):
            entries.append((r[0], (r[1] or '').casefold(), (r[2] or '').casefold()))
    with _SEARCH_LOCK:
        _SEARCH_INDEX.clear()
        _SEARCH_INDEX[key] = (time.time(), entries)
    return entries


def _present_ids(conn, ids, db_path=None):
    present = set()
    if db_path is not None:
        with reader(db_path, 'closeness') as store:
            if store is not None:
                for start in range(0, len(ids), 900):
                    chunk = ids[start:start + 900]
                    present.update(row[0] for row in store.conn.execute(
                        'SELECT person_id FROM mp WHERE person_id IN (%s)'
                        % ','.join('?' * len(chunk)), chunk))
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        present.update(r[0] for r in conn.execute(
            'SELECT person_id FROM map_person_degree WHERE person_id IN (%s)' % ','.join('?' * len(chunk)), chunk))
        present.update(r[0] for r in conn.execute(
            'SELECT p.id FROM people p JOIN map_source_handles h ON h.handle=p.handle WHERE p.id IN (%s)'
            % ','.join('?' * len(chunk)), chunk))
    return present


def search_ids(conn, db_path, text, limit=Q_CAP):
    """People matching ``text`` (in the map), best match first, and whether the list was cut."""
    text = _normalize(text)
    if len(text) < SEARCH_MIN:
        raise ValueError('q needs at least 2 characters')
    quality = {}
    row = conn.execute('SELECT id FROM people WHERE handle=?', (text,)).fetchone()
    if row:
        quality[row[0]] = 0
    prefix_capped = False
    prefix_rows = conn.execute('SELECT id FROM people WHERE handle>=? AND handle<? ORDER BY handle LIMIT ?',
                               (text, _prefix_upper(text), limit + 1)).fetchall()
    prefix_capped = len(prefix_rows) > limit
    for (pid,) in prefix_rows:
        quality.setdefault(pid, 1)
    if prefix_capped:
        prefix_present = _present_ids(conn, list(quality), db_path)
        if len(prefix_present) >= limit:
            # Exact and prefix handles outrank every name/substring match.
            # A full prefix page needs no cold 265k-person name-search pool.
            ids = sorted(prefix_present, key=lambda pid: (quality[pid], pid))
            return ids[:limit], True
    for pid, handle, name in _index_for(conn, db_path):
        if pid in quality:
            continue
        if name == text:
            quality[pid] = 2
        elif name.startswith(text):
            quality[pid] = 3
        elif text in handle or text in name:
            quality[pid] = 4
    ids = list(quality)
    present = _present_ids(conn, ids, db_path)
    ids = [i for i in ids if i in present]
    ids.sort(key=lambda i: (quality[i], i))
    return ids[:limit], prefix_capped or len(ids) > limit


def _search_pool(conn, db_path, text, store):
    ids, capped = search_ids(conn, db_path, text, Q_CAP)
    return ids, capped


def search(conn, db_path, q):
    text = q.get('q', [''])[0]
    ids, _ = search_ids(conn, db_path, text, 60)
    positions = {}
    revs = []
    for mode in ML.MODES:
        with reader(db_path, mode) as store:
            if store is None:
                continue
            revs.append(store.rev)
            for pid, r in _details(store, ids).items():
                positions.setdefault(pid, {})[mode] = {'x': round(ML.coord(r[1]), 7), 'y': round(ML.coord(r[2]), 7),
                                                       'rank': round(r[3], 6), 'cluster': r[4]}
    people = {}
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        for r in conn.execute('SELECT id,handle,name,pic_file FROM people WHERE id IN (%s)' % ','.join('?' * len(chunk)), chunk):
            people[r[0]] = r
    results = []
    with reader(db_path, 'closeness') as store:
        details = _details(store, ids) if store else {}
    for pid in ids:
        if pid not in people:
            continue
        d = details.get(pid)
        cls = d[5] if d else 0
        follow = cls // 64
        results.append({'id': pid, 'handle': people[pid][1], 'name': people[pid][2],
                        'pic': f'/img/{pid}' if people[pid][3] else None,
                        'followed': bool(follow & 1), 'follows_me': bool(follow & 2),
                        'following_evidence': 'observed' if follow & 1 else 'absent' if follow & 4 else 'unknown',
                        'fit': d[6] if d else None, 'status': ML.CODE_STATUS.get((cls % 64) >> 3),
                        'lead': ML.is_lead(cls), 'closeness': d[7] / 1000.0 if d else None,
                        'pending': pid not in positions, 'positions': positions.get(pid, {})})
        if len(results) == 20:
            break
    return {'rev': max(revs) if revs else 0, 'q': _normalize(text), 'results': results}


# ---------- edges ----------

def edges(conn, q, db_path=None):
    raw = q.get('ids', [''])[0]
    try:
        ids = list(dict.fromkeys(int(t) for t in raw.split(',') if t.strip()))
    except ValueError:
        raise ValueError('ids must be a comma list of person ids') from None
    if not ids:
        raise ValueError('ids is required')
    if len(ids) > MAX_EDGE_IDS:
        raise ValueError(f'at most {MAX_EDGE_IDS} ids')
    slots = ','.join('?' * len(ids))
    handle = {r[0]: r[1] for r in conn.execute(f'SELECT id,handle FROM people WHERE id IN ({slots})', ids)}
    truncated = False
    out, seen = [], set()
    id_of = {}

    def person_id(h):
        key = h.lower()
        if key not in id_of:
            row = conn.execute('SELECT id FROM people WHERE handle=?', (h,)).fetchone()
            id_of[key] = row[0] if row else None
        return id_of[key]

    def follow(seed, pid, direction, observed):
        if pid not in handle:
            handle[pid] = (conn.execute('SELECT handle FROM people WHERE id=?', (pid,)).fetchone() or [None])[0]
        key = (seed.lower(), pid, direction)
        if key in seen:
            return
        seen.add(key)
        me, src = handle.get(pid), seed
        if direction == 'followers':          # the person is in the source's followers list
            frm, frm_h, to, to_h = pid, me, person_id(src), src
        else:
            frm, frm_h, to, to_h = person_id(src), src, pid, me
        out.append({'kind': 'follow', 'from': frm, 'from_handle': frm_h, 'to': to, 'to_handle': to_h,
                    'seed': seed, 'direction': direction, 'observed_at': observed})
    for pid in ids:
        rows = conn.execute('SELECT seed,direction,observed_at FROM current_edges WHERE person_id=? '
                            'ORDER BY observed_at DESC LIMIT 51', (pid,)).fetchall()
        if len(rows) > 50:
            truncated = True
        for r in rows[:50]:
            follow(r[0], pid, r[1], r[2])
        h = handle.get(pid)
        if h and conn.execute('SELECT 1 FROM map_source_handles WHERE handle=?', (h,)).fetchone():
            remaining = max(0, 50 - len(rows[:50]))
            rows = conn.execute('SELECT person_id,direction,observed_at FROM current_edges WHERE seed=? '
                                'ORDER BY observed_at DESC LIMIT ?', (h, remaining + 1)).fetchall()
            if len(rows) > remaining:
                truncated = True
            for r in rows[:remaining]:
                handle.setdefault(r[0], (conn.execute('SELECT handle FROM people WHERE id=?', (r[0],)).fetchone() or [None])[0])
                follow(h, r[0], r[1], r[2])
    members = {}
    for pid, seed in conn.execute(f'SELECT person_id,seed FROM map_seed_member WHERE person_id IN ({slots})', ids):
        members.setdefault(seed.lower(), []).append(pid)
    pairs = {}
    for seed, people in members.items():
        for i, a in enumerate(people):
            for b in people[i + 1:]:
                pair = pairs.setdefault((min(a, b), max(a, b)), [])
                pair.append(seed)
    overlap = [{'kind': 'overlap', 'a': a, 'b': b, 'seeds': sorted(s)[:5], 'shared': len(s)}
               for (a, b), s in sorted(pairs.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    if len(overlap) > 500:
        overlap, truncated = overlap[:500], True
    out = out + overlap
    if len(out) > 2000:
        out, truncated = out[:2000], True
    response = {'rev': db.get_setting(conn, 'lead_data_rev', 0), 'ids': ids, 'truncated': truncated, 'edges': out, 'nodes': []}
    if db_path:
        mode = q.get('mode', ['closeness'])[0]
        if mode not in ML.MODES:
            raise ValueError('mode must be closeness, fit, seeds or status')
        endpoints = set(ids)
        for edge in out:
            endpoints.update(v for v in (edge.get('from'), edge.get('to'), edge.get('a'), edge.get('b')) if v is not None)
        with reader(db_path, mode) as store:
            if store:
                details = _details(store, list(endpoints))
                people = _people(conn, list(details))
                response['nodes'] = [_node(r, people, mode) for r in details.values()]
    return response
