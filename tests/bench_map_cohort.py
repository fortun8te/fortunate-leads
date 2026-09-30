"""Physical stable-page benchmark; isolated reduced fixture, never a live database.

Run: python tests/bench_map_cohort.py --directory /isolated/path --people 10000000
Measures map_view.view (including JSON), not HTTP transport, rendering or ingestion.
"""
import argparse
import hashlib
import json
import resource
import shutil
import sqlite3
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import map_layout as ML
import map_view as MV

RESERVE = 8 * 1024**3


def guard(directory):
    if shutil.disk_usage(directory).free < RESERVE:
        raise RuntimeError('Stopped: fewer than 8 GiB free')


def fixture(path, population):
    if path.exists():
        raise RuntimeError('Refusing to overwrite a fixture')
    layout = ML.map_dir(path)
    layout.mkdir()
    store = sqlite3.connect(layout / 'closeness.sqlite')
    store.executescript(ML.SCHEMA)
    store.close()
    conn = sqlite3.connect(path)
    conn.executescript('''
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=FILE;
        PRAGMA cache_size=-32768;
        CREATE TABLE people(id INTEGER PRIMARY KEY,handle TEXT,name TEXT,pic_file TEXT,followers INTEGER);
        CREATE TABLE map_person_degree(person_id INTEGER PRIMARY KEY,degree INTEGER);
        CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE map_layout_dirty(person_id INTEGER PRIMARY KEY);
        CREATE TEMP TABLE batch(id INTEGER PRIMARY KEY);
    ''')
    conn.execute('ATTACH DATABASE ? AS layout', (str(layout / 'closeness.sqlite'),))
    conn.executescript('PRAGMA layout.journal_mode=OFF; PRAGMA layout.synchronous=OFF; PRAGMA layout.cache_size=-32768;')
    for low in range(1, population + 1, 25000):
        guard(path.parent)
        high = min(population, low + 24999)
        conn.execute('DELETE FROM batch')
        conn.execute('WITH RECURSIVE n(i) AS (SELECT ? UNION ALL SELECT i+1 FROM n WHERE i<?) INSERT INTO batch SELECT i FROM n', (low, high))
        conn.execute("INSERT INTO people SELECT id,printf('person%08d',id),printf('Person %d',id),NULL,(id*7919)%200000 FROM batch")
        conn.execute('INSERT INTO map_person_degree SELECT id,id%12 FROM batch')
        # Eight classes, ties containing one million IDs, owner deliberately in a tie.
        conn.execute('INSERT INTO layout.mp SELECT id,(id*71)%4194304,(id*31)%4194304,?-(id-1)/1000000,0,(id%8)/2*64+(id%2)*40+5,70,500,0 FROM batch', (population // 1000000,))
        conn.commit()
        if high % 1000000 == 0:
            print(json.dumps({'stage': 'fixture', 'people': high}), flush=True)
    conn.execute('INSERT INTO layout.agg SELECT 1,0,0,cls,0,count(*),sum(mx),sum(my) FROM layout.mp GROUP BY cls')
    conn.commit()
    conn.close()
    store = sqlite3.connect(layout / 'closeness.sqlite')
    for key, value in {'schema': ML.SCHEMA_VERSION, 'rev': 1, 'build_id': 'physical-cohort', 'plan': {'owner_id': 1}, 'counts': {'bulk': population, 'known': 0}}.items():
        ML.meta_set(store, key, value)
    store.commit()
    store.close()
    ML._pointer(path).write_text(json.dumps({'closeness': 'closeness.sqlite'}))


def measure(path, population):
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA cache_size=-32768')
    samples = []
    digest = hashlib.sha256()
    def request(after='', **extra):
        q = {'scope': ['all'], 'cohort': ['1'], 'budget': ['1000'], 'after': [after]}
        q.update({k: [v] for k, v in extra.items()})
        start = time.perf_counter()
        raw = MV.view(conn, path, q, cache=False)
        elapsed = (time.perf_counter() - start) * 1000
        return json.loads(raw.body), elapsed

    assert conn.execute('SELECT count(*) FROM people').fetchone()[0] == population
    after, expected, pages, maximum_bytes = '', 2, 0, 0
    first_cursor = None
    while True:
        guard(path.parent)
        result, elapsed = request(after)
        samples.append(elapsed)
        assert result['ready'] and result['cohort'] and not result['cohort_reset']
        assert result['clusters'] == []
        assert result['total'] == population - 1
        ids = [node['id'] for node in result['nodes']]
        # Independent arithmetic oracle; no SQL sort, production rank helper or ID set.
        assert ids == list(range(expected, min(expected + 1000, population + 1)))
        for node in result['nodes']:
            pid = node['id']
            assert node['handle'] == f'person{pid:08d}'
            assert node['rank'] == population // 1000000 - (pid - 1) // 1000000
            assert node['followers'] == (pid * 7919) % 200000
            assert node['source_count'] == pid % 12
        digest.update(','.join(map(str, ids)).encode() + b'\n')
        expected += len(ids)
        pages += 1
        maximum_bytes = max(maximum_bytes, len(json.dumps(result)))
        after = result['next_cursor']
        if pages == 1:
            first_cursor = after
        if pages % 1000 == 0:
            print(json.dumps({'stage': 'oracle', 'pages': pages, 'verified_people': expected - 2}), flush=True)
        if after is None:
            break
    assert expected == population + 1
    assert pages == (population - 2) // 1000 + 1
    checks = {}
    for name, cursor_id, filters, predicate in (
        ('late_tie', population - 1500, {}, lambda i: True),
        ('client', population - 10000, {'status': 'client'}, lambda i: i % 2 == 1),
        ('mutual', population - 10000, {'follow': 'mutual'}, lambda i: i % 8 in (6, 7)),
        ('empty_fit', population - 10000, {'min_fit': '85'}, lambda i: False),
    ):
        rank = population // 1000000 - (cursor_id - 1) // 1000000
        cursor = json.dumps([rank, cursor_id, 'physical-cohort:1'])
        result, elapsed = request(cursor, **filters)
        oracle = [i for i in range(cursor_id + 1, population + 1) if predicate(i)][:1000]
        assert [n['id'] for n in result['nodes']] == oracle
        checks[name] = {'milliseconds': round(elapsed, 3), 'returned': len(oracle)}
    stale = json.loads(first_cursor)
    stale[2] = 'previous-build:0'
    result, elapsed = request(json.dumps(stale))
    assert result['cohort_reset'] and [n['id'] for n in result['nodes']] == list(range(2, min(1002, population + 1)))
    checks['revision_reset'] = {'milliseconds': round(elapsed, 3), 'verified': True}
    # One high-ID outlier makes rank ordering distinguishable from plain ID order.
    layout = sqlite3.connect(ML.mode_path(path, 'closeness'))
    original_rank = population // 1000000 - (population - 1) // 1000000
    try:
        layout.execute('UPDATE mp SET rk=1000000 WHERE person_id=?', (population,))
        ML.meta_set(layout, 'rev', 2)
        layout.commit()
        result, elapsed = request(first_cursor)
        assert result['cohort_reset']
        assert [n['id'] for n in result['nodes']] == [population] + list(range(2, 1001))
        following, next_elapsed = request(result['next_cursor'])
        assert not following['cohort_reset']
        assert [n['id'] for n in following['nodes']] == list(range(1001, 2001))
        checks['rank_outlier_and_actual_revision_change'] = {'milliseconds': round(elapsed, 3), 'next_page_ms': round(next_elapsed, 3), 'verified': True}
    finally:
        layout.execute('UPDATE mp SET rk=? WHERE person_id=?', (original_rank, population))
        ML.meta_set(layout, 'rev', 1)
        layout.commit()
        layout.close()
    conn.close()
    MV.close_pool()
    ordered = sorted(samples)
    return {'physical_people': population, 'owner_excluded': 1, 'verified_people': expected - 2, 'pages': pages,
            'every_id_and_order_verified': True, 'page_id_sha256': digest.hexdigest(),
            'api_ms': {'first': samples[0], 'middle': samples[len(samples)//2], 'last': samples[-1], 'median': statistics.median(samples), 'p95': ordered[int(len(ordered)*.95)], 'maximum': max(samples)},
            'maximum_json_bytes': maximum_bytes, 'checks': checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--people', type=int, default=10000000)
    parser.add_argument('--stage', choices=('all', 'requests'), default='all')
    args = parser.parse_args()
    if args.people < 20000:
        parser.error('people must be at least 20000')
    args.directory.mkdir(parents=True, exist_ok=True)
    guard(args.directory)
    path = args.directory / 'cohort.sqlite'
    start = time.perf_counter()
    output = {'stage': args.stage, 'population': args.people, 'fixture': 'Reduced current-read schema; real rows, eight facet classes, million-row rank ties. No edges, photos, ingestion, spatial pyramid build or other view modes.',
              'measurement': 'Uncached map_view.view API function and JSON encoding; no HTTP, browser or cold-disk guarantee.',
              'source_sha256': hashlib.sha256(Path(MV.__file__).read_bytes()).hexdigest()}
    if args.stage == 'all':
        fixture(path, args.people)
        output['fixture_seconds'] = time.perf_counter() - start
    output['requests'] = measure(path, args.people)
    output.update(elapsed_seconds=time.perf_counter() - start,
                  peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
                  disk_bytes=sum(p.stat().st_size for p in args.directory.rglob('*.sqlite')),
                  free_disk_bytes=shutil.disk_usage(args.directory).free)
    (args.directory / 'results.json').write_text(json.dumps(output, indent=2) + '\n')
    print(json.dumps(output, indent=2), flush=True)


if __name__ == '__main__':
    main()
