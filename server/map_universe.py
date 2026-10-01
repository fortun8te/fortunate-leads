"""Immutable offline follow graph + spatial tiles. Requests never build or scan the graph.

CSR ordinals are compact uint32; external node IDs remain the existing people.id.
Hop distance is over recorded evidence, never proof of a personal relationship.
"""
import argparse
from array import array
import base64
from contextlib import closing
import hashlib
import json
import itertools
import math
import os
from pathlib import Path
import re
import sqlite3
import struct
import sys
import tempfile
import time
import uuid

RECORD = struct.Struct('<IfffIHHII')
HEADER = struct.Struct('<4sIII')
UNKNOWN = 65535
MAX_TILE = 2048
MAX_BUDGET = 65536
VERSION_RE = re.compile(r'^[a-f0-9]{32}$')


def _readonly(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    return c


def _tables(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def _source_stamp(conn, database):
    """Cheap conservative change hint, including WAL writes and untracked import tables."""
    tables = _tables(conn)
    revision = None
    if 'settings' in tables:
        row = conn.execute("SELECT value FROM settings WHERE key='lead_data_rev'").fetchone()
        revision = row[0] if row else None
    latest_import = None
    if 'follow_export_imports' in tables:
        row = conn.execute('SELECT fingerprint FROM follow_export_imports ORDER BY rowid DESC LIMIT 1').fetchone()
        latest_import = row[0] if row else None
    files = {}
    for suffix in ('', '-wal'):
        try:
            stat = Path(str(database) + suffix).stat()
            files[suffix or 'database'] = [stat.st_size, stat.st_mtime_ns]
        except OSError:
            files[suffix or 'database'] = None
    return dict(lead_data_rev=revision, latest_import=latest_import, files=files)


def _uniform(pid, salt=0):
    x = (pid + salt * 0x9e3779b9) & 0xffffffffffffffff
    x = ((x ^ (x >> 30)) * 0xbf58476d1ce4e5b9) & 0xffffffffffffffff
    x = ((x ^ (x >> 27)) * 0x94d049bb133111eb) & 0xffffffffffffffff
    return ((x ^ (x >> 31)) & 0xffffffff) / 4294967296


# Worst-case portrait area occupies at most 85% of each annulus. Growing
# the world rather than shrinking all portraits keeps the owner neighborhood legible.
PORTRAIT_FILL = .85
GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))


def _bands(counts):
    bands = [(0.0, 0.0)]
    inner = 24.0
    for distance in range(1, len(counts)):
        peak_radius = 12.0 / (1 + distance * .10) * 1.15
        outer = math.sqrt(inner * inner + max(1, counts[distance]) * peak_radius ** 2 / PORTRAIT_FILL)
        bands.append((inner, outer))
        inner = outer
    return bands


def _portrait_radius(distance, radial_distance, followers, log_max):
    follower_boost = .85 + .30 * math.log1p(max(0, followers)) / log_max
    # Keep a small world-space floor so far-away people can still be inspected
    # at high zoom, without remaining visible as portraits in the initial view.
    taper = max(.02, 1.0 / (1 + (radial_distance / 360.0) ** 3))
    return 12.0 / (1 + distance * .10) * taper * follower_boost


def _morton(x, y, span):
    a = max(0, min(4095, int((x / span + 1) * 2047.5)))
    b = max(0, min(4095, int((y / span + 1) * 2047.5)))
    v = 0
    for bit in range(12):
        v |= ((a >> bit) & 1) << (2 * bit)
        v |= ((b >> bit) & 1) << (2 * bit + 1)
    return v


def _write_array(path, values):
    if sys.byteorder != 'little':
        values.byteswap()
    with open(path, 'wb') as f:
        values.tofile(f)
    if sys.byteorder != 'little':
        values.byteswap()


def build(database, anchor, traversal='outgoing', edge_policy='captured', tile_size=MAX_TILE, output=None):
    """Build a snapshot in a new directory, atomically publish only on success.

    Memory is O(N + E) typed arrays, not Python node/edge objects; sorting/dedup is SQLite.
    This is an offline operation. Caller must never invoke it inside an API request.
    """
    if traversal not in ('outgoing', 'incoming', 'undirected'):
        raise ValueError('invalid traversal')
    if edge_policy not in ('captured', 'current_handle', 'exports', 'all'):
        raise ValueError('invalid edge policy')
    tile_size = max(1, min(MAX_TILE, int(tile_size)))
    root = Path(output) if output else Path(str(database) + '.universe')
    root.mkdir(parents=True, exist_ok=True)
    version = uuid.uuid4().hex
    start = time.monotonic()
    temp = Path(tempfile.mkdtemp(prefix='.building-', dir=root))
    source = _readonly(database)
    index = sqlite3.connect(temp / 'index.sqlite')
    index.row_factory = sqlite3.Row
    try:
        source.execute('BEGIN')
        source_stamp = _source_stamp(source, database)
        source_snapshot_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        index.executescript('''PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE;
        CREATE TABLE nodes(id INTEGER PRIMARY KEY, ordinal INTEGER UNIQUE, handle TEXT, name TEXT,
          followers INTEGER, hop INTEGER, community INTEGER, degree INTEGER, x REAL,y REAL,r REAL,morton INTEGER);
        CREATE INDEX node_handle ON nodes(handle COLLATE NOCASE);
        CREATE TABLE evidence(source_id INTEGER,target_id INTEGER,observed_at TEXT,provenance TEXT,
          PRIMARY KEY(source_id,target_id,provenance,observed_at)) WITHOUT ROWID;
        CREATE INDEX evidence_target ON evidence(target_id,source_id);
        CREATE TABLE adjacency(source INTEGER,target INTEGER,PRIMARY KEY(source,target)) WITHOUT ROWID;
        CREATE TABLE tiles(id INTEGER PRIMARY KEY,count INTEGER,x0 REAL,y0 REAL,x1 REAL,y1 REAL,hop_min INTEGER,hop_max INTEGER);
        CREATE VIRTUAL TABLE tile_bounds USING rtree(id,x0,x1,y0,y1);
        ''')
        ids = array('I')
        anchor_ordinal = None
        max_followers = 0
        for row in source.execute('SELECT id,handle,name,followers FROM people ORDER BY id'):
            pid = int(row['id'])
            if not 0 < pid <= 0xffffffff:
                raise ValueError('people.id must fit uint32; explicit remapping required')
            max_followers = max(max_followers, max(0,int(row['followers'] or 0)))
            ordinal = len(ids)
            ids.append(pid)
            index.execute('INSERT INTO nodes(id,ordinal,handle,name,followers) VALUES(?,?,?,?,?)',
                          (pid, ordinal, row['handle'], row['name'], row['followers']))
            if str(anchor).lower().lstrip('@') == row['handle'].lower() or str(anchor) == str(pid):
                anchor_ordinal = ordinal
        if anchor_ordinal is None:
            raise ValueError('anchor must be an existing person ID or handle')
        n = len(ids)
        tables = _tables(source)
        evidence_queries = []
        if 'universe_edges' in tables:
            evidence_queries.append("SELECT source_id,target_id,observed_at,provenance FROM universe_edges")
        if {'edge_observations', 'jobs', 'edge_evidence'} <= tables and 'target_ig_id' in {r[1] for r in source.execute('PRAGMA table_info(jobs)')}:
            evidence_queries.append('''SELECT p.id,o.person_id,o.observed_at,'job_target_at_build'
              FROM edge_observations o JOIN jobs j ON j.id=o.job_id
              JOIN people p ON p.ig_id=j.target_ig_id
              JOIN edge_evidence e ON e.seed=o.seed AND e.person_id=o.person_id AND e.direction=o.direction
              WHERE e.active=1 AND o.direction='following' AND j.target_ig_id IS NOT NULL''')
            evidence_queries.append('''SELECT o.person_id,p.id,o.observed_at,'job_target_at_build'
              FROM edge_observations o JOIN jobs j ON j.id=o.job_id
              JOIN people p ON p.ig_id=j.target_ig_id
              JOIN edge_evidence e ON e.seed=o.seed AND e.person_id=o.person_id AND e.direction=o.direction
              WHERE e.active=1 AND o.direction='followers' AND j.target_ig_id IS NOT NULL''')
        if edge_policy in ('current_handle', 'all') and 'current_edges' in tables:
            evidence_queries.append("SELECT p.id,e.person_id,e.observed_at,'current_handle_at_build' FROM current_edges e JOIN people p ON p.handle=e.seed WHERE e.direction='following'")
            evidence_queries.append("SELECT e.person_id,p.id,e.observed_at,'current_handle_at_build' FROM current_edges e JOIN people p ON p.handle=e.seed WHERE e.direction='followers'")
        if edge_policy in ('exports', 'all') and {'follow_export_members', 'follow_export_imports'} <= tables:
            evidence_queries.append("SELECT f.owner_person_id,m.person_id,m.source_timestamp,'historical_export_handle' FROM follow_export_members m JOIN follow_export_imports f ON f.fingerprint=m.fingerprint WHERE m.direction='following' AND f.owner_person_id IS NOT NULL AND m.person_id IS NOT NULL")
            evidence_queries.append("SELECT m.person_id,f.owner_person_id,m.source_timestamp,'historical_export_handle' FROM follow_export_members m JOIN follow_export_imports f ON f.fingerprint=m.fingerprint WHERE m.direction='followers' AND f.owner_person_id IS NOT NULL AND m.person_id IS NOT NULL")
        for sql in evidence_queries:
            batch = []
            for row in source.execute(sql):
                if row[0] != row[1]:
                    batch.append((row[0],row[1],str(row[2] or ''),str(row[3] or 'unspecified')))
                if len(batch) == 4096:
                    index.executemany('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?)', batch)
                    batch.clear()
            index.executemany('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?)', batch)
        # Unknown/deleted IDs are not bridges. Keep evidence separately from compact adjacency.
        pairs = 'SELECT a.ordinal,b.ordinal FROM evidence e JOIN nodes a ON a.id=e.source_id JOIN nodes b ON b.id=e.target_id'
        index.execute('INSERT OR IGNORE INTO adjacency ' + (pairs if traversal != 'incoming' else pairs.replace('a.ordinal,b.ordinal', 'b.ordinal,a.ordinal')))
        if traversal == 'undirected':
            index.execute('INSERT OR IGNORE INTO adjacency ' + pairs.replace('a.ordinal,b.ordinal', 'b.ordinal,a.ordinal'))
        offsets = array('Q', [0])
        targets = array('I')
        degrees = array('I', [0]) * n
        parents = array('I', range(n))

        def find(a):
            while parents[a] != a:
                parents[a] = parents[parents[a]]
                a = parents[a]
            return a

        current = 0
        for a, b in index.execute('SELECT source,target FROM adjacency ORDER BY source,target'):
            while current < a:
                offsets.append(len(targets))
                current += 1
            targets.append(b)
            degrees[a] += 1
            pa, pb = find(a), find(b)
            if pa != pb:
                parents[max(pa, pb)] = min(pa, pb)
        while len(offsets) <= n:
            offsets.append(len(targets))
        hops = array('H', [UNKNOWN]) * n
        hops[anchor_ordinal] = 0
        queue = array('I', [anchor_ordinal])
        head = 0
        while head < len(queue):
            a = queue[head]
            head += 1
            for k in range(offsets[a], offsets[a + 1]):
                b = targets[k]
                if hops[b] == UNKNOWN:
                    if hops[a] >= UNKNOWN - 1:
                        raise ValueError('hop depth exceeds binary contract')
                    hops[b] = hops[a] + 1
                    queue.append(b)
        max_hop = max((h for h in hops if h != UNKNOWN), default=0)
        ring_counts = array('I', [0]) * (max_hop + 3)
        for h in hops:
            ring_counts[h if h != UNKNOWN else max_hop + 2] += 1
        bands = _bands(ring_counts)
        span = max(600.0, bands[-1][1] + 12.0)
        ring_ranks = array('I', [0]) * len(ring_counts)
        log_max = max(1.0, math.log1p(max_followers))
        for row in index.execute('SELECT id,ordinal,followers FROM nodes ORDER BY id'):
            i, pid = row['ordinal'], row['id']
            hop = hops[i]
            distance = hop if hop != UNKNOWN else max_hop + 2
            if hop == 0:
                x = y = 0.0
            else:
                # Equal-area radial strata and a golden angle avoid random clumps.
                # Small ID-derived jitter keeps the packing irregular and deterministic.
                inner, outer = bands[distance]
                rank = ring_ranks[distance]
                ring_ranks[distance] += 1
                fraction = (rank + .35 + .30 * _uniform(pid, 1)) / ring_counts[distance]
                radius = math.sqrt(inner * inner + fraction * (outer * outer - inner * inner))
                spacing = math.sqrt((outer * outer - inner * inner) / ring_counts[distance])
                angle = rank * GOLDEN_ANGLE + distance * .71 + (_uniform(pid, 2) - .5) * .10 * spacing / radius
                x, y = math.cos(angle) * radius, math.sin(angle) * radius
            followers = max(0, int(row['followers'] or 0))
            r = _portrait_radius(distance, math.hypot(x, y), followers, log_max)
            if hop == 0:
                r = 12.0
            index.execute('UPDATE nodes SET hop=?,community=?,degree=?,x=?,y=?,r=?,morton=? WHERE id=?',
                          (hop, ids[find(i)], degrees[i], x, y, r, _morton(x, y, span), pid))
        _write_array(temp / 'node_ids.u32', ids)
        _write_array(temp / 'offsets.u64', offsets)
        _write_array(temp / 'targets.u32', targets)
        index.execute('CREATE INDEX node_spatial ON nodes(hop,morton,id)')
        (temp / 'tiles').mkdir()
        cursor = index.execute('SELECT * FROM nodes ORDER BY hop,morton,id')
        tid = 0
        bounds = [0.0, 0.0, 0.0, 0.0]
        for _, group in itertools.groupby(cursor, key=lambda row: row['hop']):
            while True:
                rows = list(itertools.islice(group, tile_size))
                if not rows:
                    break
                payload = bytearray(HEADER.pack(b'MUV1', len(rows), RECORD.size, 0))
                x0 = min(r['x'] - r['r'] for r in rows)
                y0 = min(r['y'] - r['r'] for r in rows)
                x1 = max(r['x'] + r['r'] for r in rows)
                y1 = max(r['y'] + r['r'] for r in rows)
                for row in rows:
                    flags = (1 if row['hop'] == 0 else 0) | (2 if row['hop'] != UNKNOWN else 0) | (4 if row['followers'] is not None else 0)
                    payload.extend(RECORD.pack(row['id'], row['x'], row['y'], row['r'], min(0xffffffff, max(0, row['followers'] or 0)), row['hop'], flags, row['community'], row['degree']))
                (temp / 'tiles' / (str(tid) + '.bin')).write_bytes(payload)
                index.execute('INSERT INTO tiles VALUES(?,?,?,?,?,?,?,?)', (tid, len(rows), x0, y0, x1, y1, min(r['hop'] for r in rows), max(r['hop'] for r in rows)))
                index.execute('INSERT INTO tile_bounds VALUES(?,?,?,?,?)', (tid, x0, x1, y0, y1))
                bounds = [min(bounds[0], x0), min(bounds[1], y0), max(bounds[2], x1), max(bounds[3], y1)]
                tid += 1
        index.execute('CREATE INDEX tile_priority ON tiles(hop_min,id)')
        anchor_row = index.execute('SELECT id,handle FROM nodes WHERE ordinal=?', (anchor_ordinal,)).fetchone()
        provenance = {r[0]: r[1] for r in index.execute('SELECT provenance,count(*) FROM evidence GROUP BY provenance')}
        manifest = dict(schema=1, version=version, available=True, node_count=n, edge_count=index.execute('SELECT count(*) FROM adjacency').fetchone()[0], tile_count=tid,
                        anchor=dict(person_id=anchor_row['id'], handle=anchor_row['handle'], x=0, y=0), bounds=bounds, span=max(bounds[2]-bounds[0],bounds[3]-bounds[1]),
                        traversal=traversal, edge_policy=edge_policy, provenance=provenance,
                        distance_semantics='Shortest recorded follow chain; no recorded path does not prove no connection',
                        identity_semantics='Frozen local IDs; job targets are read at build time, current handles require explicit opt-in, export IDs were frozen at import',
                        identity_limit='Job target identity is not an immutable capture-time identity record',
                        unresolved_evidence_count=index.execute('SELECT count(*) FROM evidence e LEFT JOIN nodes a ON a.id=e.source_id LEFT JOIN nodes b ON b.id=e.target_id WHERE a.id IS NULL OR b.id IS NULL').fetchone()[0],
                        unknown_hop=UNKNOWN, reachable_count=len(queue), community_semantics='Weakly connected recorded component',
                        metric_semantics='Distinct adjacency degree under selected traversal', record_stride=32, header_bytes=16,
                        byte_order='little', ring_counts=list(ring_counts), ring_bounds=bands,
                        layout_semantics='Density-expanding disjoint hop annuli; deterministic irregular equal-area radial packing',
                        size_semantics='Follower multiplier 0.85 to 1.15; radius decreases with hop and radial distance',
                        portrait_fill_ceiling=PORTRAIT_FILL,
                        max_tile_records=tile_size, max_view_records=MAX_BUDGET,
                        source_snapshot_at=source_snapshot_at, source_stamp=source_stamp,
                        rebuild_required_after_source_changes=True, auto_rebuild=False,
                        created_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), build_seconds=round(time.monotonic()-start, 3))
        # Radius is the vertical half-extent at a reference landscape aspect of 1.6.
        # Approximate 700 nearest people rather than fitting the entire universe.
        first_distance = next((d for d in range(1, max_hop + 1) if ring_counts[d]), None)
        if first_distance is None:
            camera_radius = 100.0
        else:
            inner, outer = bands[first_distance]
            fraction = min(1.0, 700.0 / 1.6 / ring_counts[first_distance])
            camera_radius = max(100.0, .85 * math.sqrt(inner * inner + fraction * (outer * outer - inner * inner)))
        manifest['initial_camera'] = dict(x=0.0, y=0.0, radius=camera_radius,
                                        target_nodes=700, reference_aspect=1.6)
        manifest['owner'] = dict(manifest['anchor'], id=anchor_row['id'])
        manifest['total'] = n
        (temp / 'manifest.json').write_text(json.dumps(manifest, separators=(',', ':')))
        index.commit()
        index.close()
        source.rollback()
        source.close()
        os.rename(temp, root / version)
        pointer = root / ('.current-' + version)
        pointer.write_text(json.dumps({'version': version}))
        os.replace(pointer, root / 'current.json')
        return manifest
    except Exception:
        index.close()
        source.close()
        import shutil
        shutil.rmtree(temp, ignore_errors=True)
        raise


def _snapshot(database, version=None):
    root = Path(str(database) + '.universe')
    if version is None:
        try:
            version = json.loads((root / 'current.json').read_text())['version']
        except (OSError, ValueError, KeyError):
            return None
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ValueError('invalid universe version')
    path = root / version
    return path if (path / 'manifest.json').is_file() else None


def _value(q, key, default=None):
    v = q.get(key, default)
    return v[0] if isinstance(v, list) else v


def _tile(row, version):
    return dict(id=row['id'], record_count=row['count'], bounds=[row['x0'], row['y0'], row['x1'], row['y1']],
                hop_min=row['hop_min'], hop_max=row['hop_max'], priority=row['hop_min'],
                url='/api/map/universe/tile?version=' + version + '&tile=' + str(row['id']))


def view(database, q):
    path = _snapshot(database, _value(q, 'version'))
    if path is None:
        return {'available': False, 'tiles': [], 'next_cursor': None}
    manifest = json.loads((path / 'manifest.json').read_text())
    version = manifest['version']
    budget = max(manifest['max_tile_records'], min(MAX_BUDGET, int(_value(q, 'budget', 8192))))
    bounds = [float(_value(q, k, manifest['bounds'][i])) for i, k in enumerate(('x0', 'y0', 'x1', 'y1'))]
    if not all(math.isfinite(v) for v in bounds) or bounds[0] > bounds[2] or bounds[1] > bounds[3]:
        raise ValueError('invalid camera bounds')
    query_hash = hashlib.sha256(json.dumps([version,bounds,budget],separators=(',', ':')).encode()).hexdigest()[:16]
    last = (-1, -1)
    token = _value(q, 'cursor')
    if token:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)))
            if decoded['q'] != query_hash:
                raise ValueError()
            last = tuple(decoded['last'])
            if len(last) != 2 or not all(isinstance(v,int) and v >= 0 for v in last):
                raise ValueError()
        except Exception:
            raise ValueError('cursor does not match this universe view')
    with closing(_readonly(path / 'index.sqlite')) as c:
        rows = c.execute('''SELECT t.* FROM tile_bounds b JOIN tiles t ON t.id=b.id
          WHERE b.x1>=? AND b.x0<=? AND b.y1>=? AND b.y0<=?
            AND (t.hop_min>? OR (t.hop_min=? AND t.id>?)) ORDER BY t.hop_min,t.id LIMIT 65''',
          (bounds[0],bounds[2],bounds[1],bounds[3],last[0],last[0],last[1])).fetchall()
    chosen = []
    count = 0
    for row in rows:
        if len(chosen) == 32 or count + row['count'] > budget:
            break
        chosen.append(row)
        count += row['count']
    next_cursor = None
    if chosen and len(chosen) < len(rows):
        next_cursor = base64.urlsafe_b64encode(json.dumps({'q':query_hash,'last':[chosen[-1]['hop_min'],chosen[-1]['id']]},separators=(',', ':')).encode()).decode().rstrip('=')
    return dict(available=True, version=version, tiles=[_tile(r,version) for r in chosen], record_count=count,
                next_cursor=next_cursor, complete=next_cursor is None, bounds=bounds)


def manifest(database):
    path = _snapshot(database)
    if path is None:
        return {'available': False, 'reason': 'No offline universe snapshot has been built'}
    result = json.loads((path / 'manifest.json').read_text())
    if 'source_snapshot_at' not in result:
        result['source_snapshot_at'] = result.get('created_at')
        result['source_timestamp_basis'] = 'legacy_build_completion_time'
    else:
        result['source_timestamp_basis'] = 'source_read_snapshot_start'
    page = view(database, {'version': result['version'], 'budget': '8192'})
    result.update(tiles=page['tiles'], next_cursor=page['next_cursor'], initial_record_count=page['record_count'])
    if 'source_stamp' in result:
        try:
            with closing(_readonly(database)) as source:
                current_stamp = _source_stamp(source, database)
            changed = current_stamp != result['source_stamp']
            result.update(stale=changed, source_changed_since_build=changed,
                          freshness='source_changed' if changed else 'no_change_detected')
        except (OSError, sqlite3.Error):
            result.update(stale=None, source_changed_since_build=None, freshness='unknown')
    else:
        result.update(stale=None, source_changed_since_build=None, freshness='unknown')
    result['rebuild_hint'] = 'Rebuild offline after imports or source changes. The change hint is conservative and may include other workspace edits.'
    result['auto_rebuild'] = False
    return result


def tile(database, q):
    from map_view import Raw
    path = _snapshot(database, _value(q, 'version'))
    tile_id = str(_value(q, 'tile', ''))
    if not re.fullmatch(r'[0-9]{1,10}', tile_id):
        raise ValueError('invalid tile ID')
    if path is None or not (path / 'tiles' / (tile_id + '.bin')).is_file():
        return Raw(b'', status=404)
    body = (path / 'tiles' / (tile_id + '.bin')).read_bytes()
    return Raw(body, etag='"' + path.name + '-' + tile_id + '"', headers={'Cache-Control':'private, max-age=31536000, immutable','Content-Type':'application/octet-stream'})


def person(database, q):
    path = _snapshot(database, _value(q,'version'))
    if path is None:
        return {'available': False}
    pid = int(_value(q,'id',0))
    with closing(_readonly(path / 'index.sqlite')) as c:
        row = c.execute('SELECT id,handle,name,followers,hop,community,degree,x,y,r FROM nodes WHERE id=?',(pid,)).fetchone()
        if row is None:
            return {'available':True,'person':None}
        item = _node(row)
        evidence = [dict(r) for r in c.execute('SELECT * FROM evidence WHERE source_id=? LIMIT 51',(pid,))]
    return {'available':True,'version':path.name,'person':item,'outgoing_evidence':evidence[:50],'outgoing_evidence_truncated':len(evidence)>50}


def _node(row):
    item = dict(row)
    item['person_id'] = item['id']
    item['distance_state'] = 'recorded_path' if item['hop'] != UNKNOWN else 'no_recorded_path'
    if item['hop'] == UNKNOWN:
        item['hop'] = None
    return item


def search(database, q):
    path = _snapshot(database, _value(q,'version'))
    text = str(_value(q,'q','')).lower().strip().lstrip('@')
    if path is None or not text:
        return {'available':path is not None,'results':[]}
    if len(text) > 100:
        raise ValueError('search is too long')
    # Indexed handle range; no substring/global scan. Snapshot metadata is independent of edges.
    with closing(_readonly(path/'index.sqlite')) as c:
        rows = c.execute('SELECT id,handle,name,followers,hop,community,degree,x,y,r FROM nodes WHERE handle COLLATE NOCASE>=? AND handle COLLATE NOCASE<? ORDER BY handle COLLATE NOCASE LIMIT 20',(text,text+'\uffff')).fetchall()
    return {'available':True,'version':path.name,'results':[_node(r) for r in rows]}


def _ids(q):
    raw = str(_value(q,'ids',''))
    parts = raw.split(',') if raw else []
    if len(parts)>200 or any(not re.fullmatch(r'[0-9]{1,10}',v) for v in parts):
        raise ValueError('ids must contain at most 200 unsigned integer IDs')
    return list(dict.fromkeys(int(v) for v in parts))


def locate(database, q):
    path = _snapshot(database,_value(q,'version'))
    ids = _ids(q)
    if path is None or not ids:
        return {'available':path is not None,'nodes':[]}
    with closing(_readonly(path/'index.sqlite')) as c:
        rows = c.execute('SELECT id,handle,name,followers,hop,community,degree,x,y,r FROM nodes WHERE id IN ('+','.join('?'*len(ids))+')',ids).fetchall()
    return {'available':True,'version':path.name,'nodes':[_node(r) for r in rows]}


def edges(database, q):
    path = _snapshot(database,_value(q,'version'))
    ids = _ids(q)
    if path is None or not ids:
        return {'available':path is not None,'edges':[],'truncated':False}
    marks = ','.join('?'*len(ids))
    with closing(_readonly(path/'index.sqlite')) as c:
        rows = c.execute('SELECT source_id,target_id,observed_at,provenance FROM evidence WHERE source_id IN ('+marks+') OR target_id IN ('+marks+') LIMIT 1001',ids+ids).fetchall()
    return {'available':True,'version':path.name,'edges':[dict(r,source=r['source_id'],target=r['target_id']) for r in rows[:1000]],'truncated':len(rows)>1000}


def routes(server):
    def database(conn):
        return server._map_database(conn)
    return [('GET',r'/api/map/universe/manifest',lambda c,q,b:manifest(database(c))),
            ('GET',r'/api/map/universe/view',lambda c,q,b:view(database(c),q)),
            ('GET',r'/api/map/universe/tile',lambda c,q,b:tile(database(c),q)),
            ('GET',r'/api/map/universe/person',lambda c,q,b:person(database(c),q)),
            ('GET',r'/api/map/universe/search',lambda c,q,b:search(database(c),q)),
            ('GET',r'/api/map/universe/locate',lambda c,q,b:locate(database(c),q)),
            ('GET',r'/api/map/universe/edges',lambda c,q,b:edges(database(c),q))]


def main():
    p = argparse.ArgumentParser(description='Offline immutable map universe builder. Does not modify the source database.')
    p.add_argument('--db',required=True)
    p.add_argument('--anchor',required=True)
    p.add_argument('--traversal',choices=('outgoing','incoming','undirected'),default='outgoing')
    p.add_argument('--edge-policy',choices=('captured','current_handle','exports','all'),default='captured')
    p.add_argument('--tile-size',type=int,default=MAX_TILE)
    p.add_argument('--output')
    args = p.parse_args()
    print(json.dumps(build(args.db,args.anchor,args.traversal,args.edge_policy,args.tile_size,args.output),indent=2))


if __name__ == '__main__':
    main()
