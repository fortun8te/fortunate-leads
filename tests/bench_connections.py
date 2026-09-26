"""Reproducible connection compare benchmark; never opens application data.

    python3 tests/bench_connections.py --people 100000 --runs 3

Reuses bench.py's seeded synthetic distribution, then adds a 20k-neighbor
source. Also measures tracked history with three page observations per hub edge.
Reports first-call and median timings, SQL statement counts, and result size.
"""
import argparse
import json
import os
import statistics
import tempfile
import time
from pathlib import Path

os.environ.setdefault('FL_NO_ORSLOT', '1')
from bench import build, db
from connection_graph import compare


def measure(conn, source, target, runs):
    durations = []
    statements = []
    for _ in range(runs):
        count = [0]
        conn.set_trace_callback(lambda sql: count.__setitem__(0, count[0] + 1))
        start = time.perf_counter()
        result = compare(conn, source, target)
        durations.append(round((time.perf_counter() - start) * 1000, 2))
        conn.set_trace_callback(None)
        statements.append(count[0])
    return dict(source=source, target=target, first_ms=durations[0], median_ms=statistics.median(durations),
                runs_ms=durations, sql_statements=statements[0], candidates=result['total_candidates'],
                returned=result['returned_count'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--people', type=int, default=100000)
    parser.add_argument('--runs', type=int, default=3)
    args = parser.parse_args()
    if args.people < 2 or args.runs < 1:
        parser.error('people must be at least 2 and runs at least 1')
    with tempfile.TemporaryDirectory(prefix='fortunate-connections-bench-') as folder:
        path = str(Path(folder) / 'synthetic.sqlite')
        build(path, args.people)
        conn = db.connect(path)
        base_edges = conn.execute('SELECT COUNT(*) FROM edges').fetchone()[0]
        report = dict(people=args.people, baseline_edges=base_edges, cases=[])
        report['cases'].append(dict(case='representative', **measure(conn, 'seed0', 'seed1', args.runs)))
        size = min(20000, args.people)
        ts = db.now()
        conn.execute("INSERT INTO seeds(handle,added_at) VALUES('benchhub',?)", (ts,))
        conn.executemany("INSERT INTO edges VALUES('benchhub',?,'following',?)", ((pid, ts) for pid in range(1, size + 1)))
        conn.commit()
        report['hub_edges'] = size
        report['cases'].append(dict(case='20k_hub_legacy', **measure(conn, 'benchhub', 'seed0', args.runs)))
        conn.executemany("INSERT INTO edge_observations(seed,person_id,direction,page_key,job_id,observed_at) VALUES('benchhub',?,'following',?,NULL,?)",
                         ((pid, f'bench:{page}', ts) for pid in range(1, size + 1) for page in range(3)))
        conn.commit()
        report['history_rows'] = size * 3
        report['cases'].append(dict(case='20k_hub_tracked', **measure(conn, 'benchhub', 'seed0', args.runs)))
        conn.close()
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
