"""Reproducible connection-query timings on synthetic records, never a live database.

python3 tests/bench_connections.py --people 100000 --runs 5
"""
import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import connections
import db
from bench import build


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--people', type=int, default=100000)
    parser.add_argument('--runs', type=int, default=5)
    args = parser.parse_args()
    if args.people < 50 or args.runs < 1:
        parser.error('people must be at least 50; runs must be positive')
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / 'synthetic.sqlite')
        edge_count, _ = build(path, args.people)
        conn = db.connect(path)
        conn.execute("UPDATE seeds SET ig_id=CAST(10000000 + CAST(substr(handle,5) AS INTEGER) AS TEXT)")
        # These are generated identities, not an inference about any existing imported dataset.
        conn.execute('INSERT INTO edge_observations(seed,person_id,direction,seed_ig_id,person_ig_id,first_seen,last_seen) '
                     'SELECT e.seed,e.person_id,e.direction,s.ig_id,p.ig_id,e.first_seen,e.first_seen '
                     'FROM edges e JOIN seeds s ON s.handle=e.seed JOIN people p ON p.id=e.person_id')
        conn.commit()
        conn.execute('ANALYZE')
        results = []
        for name, fn in [('source suggestions', lambda: connections.search(conn, '')),
                         ('prefix search', lambda: connections.search(conn, 'user99')),
                         ('source to person', lambda: connections.explore(conn, 'seed0', 'user42')),
                         ('person to person', lambda: connections.explore(conn, 'user1', 'user42'))]:
            durations = []
            for _ in range(args.runs):
                start = time.perf_counter()
                result = fn()
                durations.append((time.perf_counter() - start) * 1000)
            results.append({'case': name, 'median_ms': round(statistics.median(durations), 2),
                            'max_ms': round(max(durations), 2), 'search': result.get('search')})
        print(json.dumps({'people': args.people, 'observations': edge_count, 'runs': args.runs, 'results': results}, indent=2))
        conn.close()


if __name__ == '__main__':
    main()
