"""Offline scraper timing against a disposable online-backup copy of a real database.

The source is opened read-only. No Instagram or local HTTP request is made.
python3 tests/bench_page_pipeline.py --db /path/to/leads.sqlite
"""
import argparse
import math
import os
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ['FL_NO_ORSLOT'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import accounts  # noqa: E402
import db  # noqa: E402
import server  # noqa: E402


def timed(call, repeats):
    out = []
    for _ in range(repeats):
        start = time.perf_counter()
        call()
        out.append((time.perf_counter() - start) * 1000)
    return out


def summary(times):
    ordered = sorted(times)
    p95 = ordered[math.ceil(len(ordered) * .95) - 1]
    return f"median={statistics.median(times):.1f}ms p95={p95:.1f}ms max={max(times):.1f}ms"


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', type=Path, required=True)
    ap.add_argument('--pages', type=positive_int, default=12)
    ap.add_argument('--users', type=positive_int, default=50)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix='fl-page-bench-') as tmp:
        source = sqlite3.connect(f'{args.db.resolve().as_uri()}?mode=ro', uri=True)
        dest = sqlite3.connect(str(Path(tmp) / 'copy.sqlite'))
        source.backup(dest)
        source.close()
        dest.close()
        conn = db.connect(str(Path(tmp) / 'copy.sqlite'))
        lane = conn.execute('SELECT lane_id FROM accounts LIMIT 1').fetchone()
        if lane:
            now = datetime.now(timezone.utc)
            queue = timed(lambda: accounts.pick_job(conn, lane[0], ['list', 'profile'], now), 100)
            print('queue_selection', summary(queue))
        existing = conn.execute('SELECT ig_id,handle FROM people WHERE ig_id IS NOT NULL LIMIT ? OFFSET 1000',
                                (args.users,)).fetchall()
        for mode in ('new', 'existing'):
            if mode == 'existing' and len(existing) < args.users:
                continue
            seed = f'benchmark_{mode}'
            job = conn.execute("INSERT INTO jobs(kind,seed,direction,state,created_at) VALUES('list',?, 'following','leased',?)",
                               (seed, db.now())).lastrowid
            conn.commit()
            times = []
            for page in range(args.pages):
                cursor = '' if page == 0 else f'cursor{page}'
                following = f'cursor{page + 1}'
                if mode == 'new':
                    users = [{'ig_id': str(900000000000000 + page * args.users + i),
                              'handle': f'bench_{page}_{i}', 'name': 'Benchmark'} for i in range(args.users)]
                else:
                    users = [{'ig_id': row['ig_id'], 'handle': row['handle']} for row in existing]
                payload = {'job_id': job, 'seed': seed, 'direction': 'following',
                           'requested_cursor': cursor, 'next_cursor': following, 'users': users}
                start = time.perf_counter()
                server.ext_list_page(conn, {}, payload)
                times.append((time.perf_counter() - start) * 1000)
                conn.execute("UPDATE jobs SET state='leased' WHERE id=?", (job,))
                conn.commit()
            print(f'{mode}_page users={args.users} count={args.pages}', summary(times))
        conn.close()


if __name__ == '__main__':
    main()
