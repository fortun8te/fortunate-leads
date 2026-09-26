"""Bounded compare benchmark on synthetic edge databases, never application data.

    PYTHONPATH=server python3 tests/bench_connections.py --db /private/tmp/fl-map-scale-100k.sqlite \
      --db /private/tmp/fl-map-scale-1m.sqlite --db /private/tmp/fl-map-scale-5m.sqlite --runs 5

Each run uses a fresh process so peak RSS is meaningful. The supplied databases
must contain seeds seed02 and seed03. A separate full-overlap two-hub fixture
is generated with --dense-people (default 20000).
"""
import argparse
import hashlib
import json
import os
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import db
from connection_graph import compare


def one_run(path, source, target):
    conn = sqlite3.connect('file:' + str(Path(path).resolve()) + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    # Match the production connection policy; SQLite's default uses file temp store.
    conn.execute('PRAGMA temp_store=MEMORY')
    try:
        start = time.perf_counter()
        result = compare(conn, source, target, 20)
        elapsed_ms = (time.perf_counter() - start) * 1000
        digest = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
        return dict(ms=round(elapsed_ms, 3), peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    candidates=result['total_candidates'], returned=result['returned_count'], digest=digest)
    finally:
        conn.close()


def dense_fixture(path, people):
    conn = db.init(path)
    stamp = '2026-01-01T00:00:00+00:00'
    conn.executemany('INSERT INTO seeds(handle) VALUES(?)', [('hub_a',), ('hub_b',)])
    for start in range(1, people + 1, 10000):
        ids = range(start, min(start + 10000, people + 1))
        conn.executemany('INSERT INTO people(id,handle,first_seen,updated_at) VALUES(?,?,?,?)',
                         ((pid, f'user{pid:08}', stamp, stamp) for pid in ids))
        conn.executemany('INSERT INTO edges VALUES(?,?,?,?)',
                         ((seed, pid, 'following', stamp) for seed in ('hub_a', 'hub_b') for pid in ids))
    conn.commit()
    conn.close()


def percentile(values, pct):
    values = sorted(values)
    position = (len(values) - 1) * pct
    low = int(position)
    return round(values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (position - low), 3)


def measure(path, source, target, runs):
    samples = []
    for _ in range(runs):
        output = subprocess.check_output([sys.executable, __file__, '--worker', path, source, target], text=True)
        samples.append(json.loads(output))
    if len({sample['digest'] for sample in samples}) != 1:
        raise RuntimeError('compare output changed between identical benchmark runs')
    ms = [sample['ms'] for sample in samples]
    conn = sqlite3.connect('file:' + str(Path(path).resolve()) + '?mode=ro', uri=True)
    try:
        edge_count = conn.execute('SELECT COUNT(*) FROM edges').fetchone()[0]
    finally:
        conn.close()
    return dict(db=str(path), sources=[source, target], edges=edge_count,
        candidates=samples[0]['candidates'], returned=samples[0]['returned'],
        p50_ms=percentile(ms, .5), p95_ms=percentile(ms, .95),
        peak_rss_bytes=max(sample['peak_rss_bytes'] for sample in samples),
        runs_ms=ms, result_sha256=samples[0]['digest'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', action='append', default=[], help='synthetic fixture with seed02 and seed03')
    parser.add_argument('--runs', type=int, default=5)
    parser.add_argument('--dense-people', type=int, default=20000)
    parser.add_argument('--worker', nargs=3, metavar=('DB', 'SOURCE', 'TARGET'))
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(one_run(*args.worker)))
        return
    if args.runs < 1 or args.dense_people < 1:
        parser.error('runs and dense-people must be positive')
    report = [measure(path, 'seed02', 'seed03', args.runs) for path in args.db]
    with tempfile.TemporaryDirectory(prefix='fl-compare-dense-') as folder:
        path = str(Path(folder) / 'dense.sqlite')
        dense_fixture(path, args.dense_people)
        report.append(measure(path, 'hub_a', 'hub_b', args.runs))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
