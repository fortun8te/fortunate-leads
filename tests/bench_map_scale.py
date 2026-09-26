"""Synthetic map read benchmark. No application database is opened.

Usage: python3 tests/bench_map_scale.py --edges 100000 --db /tmp/fl-map-100k.sqlite
The fixture is deterministic: 12 source accounts, one or two directed current
observations per person, scores, and a few historical edges. Reuse --db for
before/after runs; --build creates or replaces that *explicit* synthetic file.
"""
import argparse
import json
import math
import os
import resource
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import db  # noqa: E402
import server  # noqa: E402


def build(path, edge_count):
    path = Path(path)
    path.unlink(missing_ok=True)
    conn = db.init(str(path))
    # Fixture loading bypasses application write accounting; reads still use
    # the exact migrated schema and indexes.
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND (name LIKE 'lead_rev_%' OR name LIKE 'map_%')"):
        conn.execute(f'DROP TRIGGER {row[0]}')
    conn.execute("DELETE FROM settings WHERE key LIKE 'map_%_v1'")
    conn.execute('PRAGMA journal_mode=OFF')
    conn.execute('PRAGMA synchronous=OFF')
    stamp = '2026-01-01T00:00:00+00:00'
    conn.executemany('INSERT INTO seeds(handle,added_at) VALUES(?,?)',
                     ((f'seed{i:02}', stamp) for i in range(12)))
    people_count = (edge_count * 4 + 4) // 5
    extras = edge_count - people_count
    started = time.perf_counter()
    for start in range(1, people_count + 1, 10000):
        end = min(start + 10000, people_count + 1)
        ids = range(start, end)
        conn.executemany('INSERT INTO people(id,handle,name,followers,first_seen,updated_at) VALUES(?,?,?,?,?,?)',
                         ((i, f'user{i}', f'User {i}', i % 100000, stamp, stamp) for i in ids))
        conn.executemany('INSERT INTO verdicts(person_id,score,tier,content_fit) VALUES(?,?,?,?)',
                         ((i, i % 101, 'maybe', i % 101) for i in ids))
        edges = []
        for i in ids:
            edges.append((f'seed{i % 12:02}', i, 'followers' if i % 2 else 'following', stamp))
            if i <= extras:
                edges.append((f'seed{(i + 1) % 12:02}', i, 'following', stamp))
        conn.executemany('INSERT INTO edges VALUES(?,?,?,?)', edges)
        conn.executemany('INSERT INTO edge_evidence VALUES(?,?,?,?,?,?)',
                         ((seed, pid, direction, 1, stamp, stamp) for seed, pid, direction, _ in edges))
        if start % 100000 == 1:
            conn.commit()
            print(json.dumps({'built_people': end - 1, 'elapsed_s': round(time.perf_counter() - started, 2)}), flush=True)
    conn.commit()
    conn.execute('ANALYZE')
    conn.close()
    return people_count


def timed(fn, runs):
    values = []
    result = None
    for _ in range(runs):
        t = time.perf_counter()
        result = fn()
        values.append(round((time.perf_counter() - t) * 1000, 2))
    return values, result


def measure(path, runs):
    server.CFG['db'] = path
    conn = db.connect(path)
    npeople = conn.execute('SELECT count(*) FROM people').fetchone()[0]
    nedges = conn.execute('SELECT count(*) FROM edges').fetchone()[0]
    plan = [list(row) for row in conn.execute('EXPLAIN QUERY PLAN SELECT p.id, count(DISTINCT e.seed) AS degree '
             'FROM people p JOIN current_edges e ON e.person_id=p.id LEFT JOIN verdicts v ON v.person_id=p.id '
             "LEFT JOIN marks m ON m.person_id=p.id WHERE p.handle NOT IN (SELECT handle FROM seeds UNION SELECT seed FROM edges) GROUP BY p.id")]
    report = {'people': npeople, 'edges': nedges, 'db_bytes': Path(path).stat().st_size, 'query_plan': plan}
    for name, query in [('overview_400', {'limit': ['400']}),
                        ('all_3000', {'scope': ['all'], 'limit': ['3000']}),
                        ('seed_filtered_400', {'seed': ['seed01'], 'limit': ['400']})]:
        values, result = timed(lambda: server.map_graph(conn, query), runs)
        report[name] = {'ms': values, 'median_ms': statistics.median(values), 'total': result['total'],
                        'shown_people': sum(n['kind'] == 'lead' for n in result['nodes']),
                        'links': len(result['links']),
                        'json_bytes': len(json.dumps(result, separators=(',', ':')).encode())}
    server.clear_caches()
    values, result = timed(lambda: server.seed_links(conn), runs)
    report['seed_links'] = {'ms': values, 'median_ms': statistics.median(values), 'pairs': len(result)}
    report['peak_rss_bytes'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    conn.close()
    return report


def measure_api(path, runs):
    """Cold exact-revision refreshes and warm polls through the public handler."""
    conn = db.connect(path)
    server.CFG['db'] = path
    query = {'limit': ['400']}
    report = {'people': conn.execute('SELECT count(*) FROM people').fetchone()[0],
              'edges': conn.execute('SELECT count(*) FROM edges').fetchone()[0],
              'db_bytes': Path(path).stat().st_size,
              'summary_rows': {
                  'people': conn.execute('SELECT count(*) FROM map_person_degree').fetchone()[0],
                  'members': conn.execute('SELECT count(*) FROM map_seed_member').fetchone()[0]}}
    for name, cold in (('cold', True), ('warm', False)):
        values = []
        for _ in range(runs):
            if cold:
                server.clear_caches()
            t = time.perf_counter()
            result = server.api_map(conn, query, None)
            values.append(round((time.perf_counter() - t) * 1000, 2))
        ordered = sorted(values)
        report[name] = {'runs_ms': values, 'p50_ms': statistics.median(values),
                        'p95_nearest_rank_ms': ordered[math.ceil(.95 * runs) - 1]}
    plan_sql = ('SELECT d.person_id FROM map_person_degree d JOIN people p ON p.id=d.person_id '
                'WHERE d.hidden=0 AND p.handle NOT IN (SELECT handle FROM map_source_handles) '
                'ORDER BY d.score IS NULL,d.score DESC,d.degree DESC,d.person_id LIMIT 400')
    report['rank_plan'] = [r[3] for r in conn.execute('EXPLAIN QUERY PLAN ' + plan_sql)]
    report['response_bytes_compact'] = len(json.dumps(result, separators=(',', ':')).encode())
    report['peak_rss_bytes'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    conn.close()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--edges', type=int, required=True)
    parser.add_argument('--db', required=True)
    parser.add_argument('--runs', type=int, default=1)
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--migrate', action='store_true', help='apply summary backfill after a bulk synthetic build')
    parser.add_argument('--api-only', action='store_true', help='measure cold/warm complete map responses')
    args = parser.parse_args()
    if args.edges < 10 or args.runs < 1:
        parser.error('edges must be >= 10 and runs >= 1')
    if args.build:
        n = build(args.db, args.edges)
        print(json.dumps({'built': args.edges, 'people': n}), flush=True)
    else:
        if not Path(args.db).is_file():
            parser.error('database absent; pass --build for a synthetic fixture')
        conn = db.connect(args.db)
        actual = conn.execute('SELECT count(*) FROM edges').fetchone()[0]
        conn.close()
        if actual != args.edges:
            parser.error(f'fixture has {actual} edges, requested {args.edges}')
    if args.migrate:
        started = time.perf_counter()
        db.init(args.db).close()
        print(json.dumps({'migration_s': round(time.perf_counter() - started, 2)}), flush=True)
    print(json.dumps(measure_api(args.db, args.runs) if args.api_only else measure(args.db, args.runs), indent=2), flush=True)
