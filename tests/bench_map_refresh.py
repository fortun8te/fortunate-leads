"""Read-only repeat-map-refresh benchmark against an existing synthetic fixture.

python3 tests/bench_map_refresh.py --db /tmp/fl-map-backend-1m.sqlite --runs 5

Both cases clear only the endpoint response cache, as changed metadata does.
The former behavior also clears source overlap. The optimized case preserves
its warmed, membership-revision cache. This measures bounded 400-person API
responses, not rendering millions of avatars or collection speed.
"""
import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import db
import server


def measure(path, runs):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    if db.get_setting(conn, 'map_membership_rev', None) is None:
        raise ValueError('Fixture needs the membership revision migration first')
    report = {'people': conn.execute('SELECT count(*) FROM people').fetchone()[0],
              'edges': conn.execute('SELECT count(*) FROM edges').fetchone()[0]}
    query = {'scope': ['all'], 'limit': ['400']}
    server.clear_caches()
    server.api_map(conn, query, None)
    for name, old_behavior in [('previous', True), ('optimized', False)]:
        elapsed = []
        for _ in range(runs):
            with server.CACHE_LOCK:
                server.CACHE.clear()
            if old_behavior:
                server.SEED_LINKS[0] = None
            start = time.perf_counter()
            result = server.api_map(conn, query, None)
            elapsed.append(round((time.perf_counter() - start) * 1000, 2))
        report[name] = {'ms': elapsed, 'median_ms': statistics.median(elapsed)}
    report['displayed_people'] = sum(node['kind'] == 'lead' for node in result['nodes'])
    report['response_bytes'] = len(json.dumps(result, separators=(',', ':')).encode())
    conn.close()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--runs', type=int, default=5)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error('--runs must be positive')
    print(json.dumps(measure(args.db, args.runs), indent=2))
