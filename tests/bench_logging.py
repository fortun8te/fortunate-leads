"""Overhead of structured logging on the leads and map endpoints (docs/LOGGING.md).

    python3 tests/bench_logging.py --source DIR [--map-dir DIR/leads.sqlite.map] --port 8981 [--rounds 6] [--requests 100]

Part 1, end to end: one server process with logging off (FL_LOG_OFF=1) and one with it on, alternating
(ABBA ...) against APFS clones (`cp -c`) of the same database, so drift on a busy Mac hits both. Reports the
median latency of each endpoint per configuration and the paired per-round difference with a 95% interval.
Part 2, deterministic: the extra work logging does per request and per SQL statement, in microseconds, as a
share of the measured median. Part 2 is the number to trust on a noisy machine.

DIR must hold leads.sqlite (a snapshot or copy; the script only reads it). Servers run with --no-workers, so
no background loop writes while measuring. Everything is removed when the script ends.
"""
import argparse
import http.client
import json
import math
import os
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))


def clone(source, target):
    """APFS copy-on-write clone: instant, and the source is never written."""
    command = ['cp', '-c', '-R', str(source), str(target)] if os.path.isdir(source) else ['cp', '-c', str(source), str(target)]
    subprocess.run(command, check=True)


def start_server(workdir, port, logging_on):
    env = dict(os.environ, FL_NO_ORSLOT='1')
    if not logging_on:
        env['FL_LOG_OFF'] = '1'
    proc = subprocess.Popen([sys.executable, str(ROOT / 'server' / 'server.py'), '--db', str(workdir / 'leads.sqlite'),
                             '--port', str(port), '--no-workers'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            conn.request('GET', '/api/counts')
            if conn.getresponse().status == 200:
                conn.close()
                return proc
        except OSError:
            time.sleep(0.3)
    proc.kill()
    raise SystemExit('server did not start')


def stop_server(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()


def timed_requests(port, path_for, count):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=120)
    samples = []
    for i in range(count):
        began = time.perf_counter()
        conn.request('GET', path_for(i))
        response = conn.getresponse()
        response.read()
        samples.append((time.perf_counter() - began) * 1000)
        if response.status not in (200, 304):
            raise SystemExit('unexpected status %s for %s' % (response.status, path_for(i)))
    conn.close()
    return samples


def concurrent(port, path_for, per_client, clients=4):
    results = []

    def work(n):
        results.extend(timed_requests(port, lambda i: path_for(i + n * 1000), per_client))
    threads = [threading.Thread(target=work, args=(n,)) for n in range(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


ENDPOINTS = {
    'leads (cached)': lambda i: '/api/leads?sort=fit&offset=0&limit=50',
    'leads (uncached)': lambda i: '/api/leads?sort=fit&offset=%d&limit=50' % (i * 50),
    'map view (cached)': lambda i: '/api/map/view?mode=closeness&cohort=1&budget=500&scope=leads',
    'map view (uncached)': lambda i: '/api/map/view?mode=closeness&cohort=1&budget=%d&scope=leads' % (300 + i % 700),
}


def measure(workdir, port, logging_on, requests):
    proc = start_server(workdir, port, logging_on)
    try:
        out = {}
        for name, path_for in ENDPOINTS.items():
            timed_requests(port, path_for, 8)                       # warm up: page cache, SQLite statement cache
            out[name] = statistics.median(timed_requests(port, path_for, requests))
        timed_requests(port, ENDPOINTS['leads (uncached)'], 4)
        out['leads x4 clients (uncached)'] = statistics.median(concurrent(port, ENDPOINTS['leads (uncached)'], max(10, requests // 4)))
        return out
    finally:
        stop_server(proc)


def interval(differences):
    """Mean and a 95% interval of paired relative differences (t-distribution, small samples)."""
    n = len(differences)
    mean = statistics.mean(differences)
    if n < 2:
        return mean, mean, mean
    t = {2: 12.71, 3: 4.30, 4: 3.18, 5: 2.78, 6: 2.57, 7: 2.45, 8: 2.36, 9: 2.31, 10: 2.26}.get(n, 2.0)
    half = t * statistics.stdev(differences) / math.sqrt(n)
    return mean, mean - half, mean + half


def microbench():
    """Added work, in microseconds, measured in-process with logging on (events go to a temp folder)."""
    import obs
    import obs_events
    out = {}
    with tempfile.TemporaryDirectory() as directory:
        obs.start(directory, hooks=False, watchdog=False)
        try:
            n = 40000
            began = time.perf_counter()
            for _ in range(n):
                ticket = obs_events.request_begin('GET', '/api/leads?sort=fit&offset=0&limit=50')
                obs_events.bind_api(ticket, {'sort': ['fit']}, {})
                obs_events.done_api(ticket, {'rows': []})
                obs_events.request_end(ticket, 200, 20000)
            out['per request, folded into a summary (us)'] = round((time.perf_counter() - began) / n * 1e6, 2)
            began = time.perf_counter()
            for _ in range(n):
                ticket = obs_events.request_begin('POST', '/api/person/12/mark')
                obs_events.request_end(ticket, 200, 100)
            out['per request, own log line (us)'] = round((time.perf_counter() - began) / n * 1e6, 2)
            plain = sqlite3.connect(':memory:')
            timed = sqlite3.connect(':memory:', factory=obs.TimedConnection)
            for conn in (plain, timed):
                conn.execute('CREATE TABLE t(a INTEGER PRIMARY KEY, b TEXT)')
                conn.executemany('INSERT INTO t VALUES(?,?)', [(i, 'x') for i in range(1000)])
            times = {}
            for label, conn in (('plain', plain), ('timed', timed)):
                best = []
                for _ in range(5):
                    began = time.perf_counter()
                    for i in range(40000):
                        conn.execute('SELECT b FROM t WHERE a=?', (i % 1000,)).fetchone()
                    best.append((time.perf_counter() - began) / 40000 * 1e6)
                times[label] = min(best)
            out['per SQL statement, extra (us)'] = round(times['timed'] - times['plain'], 3)
            out['_sql_plain_us'] = round(times['plain'], 3)
        finally:
            obs.stop()
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, help='folder with leads.sqlite to clone (read only)')
    parser.add_argument('--map-dir', help='a leads.sqlite.map folder to clone next to the database (needed for a real map view)')
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--rounds', type=int, default=6)
    parser.add_argument('--requests', type=int, default=100)
    parser.add_argument('--tmp', help='parent folder for the clone (default: system temp)')
    parser.add_argument('--out', help='write the result JSON here')
    args = parser.parse_args()
    workdir = Path(tempfile.mkdtemp(prefix='fl-bench-logging-', dir=args.tmp))
    try:
        clone(Path(args.source) / 'leads.sqlite', workdir / 'leads.sqlite')
        if args.map_dir:
            clone(Path(args.map_dir), workdir / 'leads.sqlite.map')
        runs = {'off': [], 'on': []}
        for round_index in range(args.rounds):
            order = ('off', 'on') if round_index % 2 == 0 else ('on', 'off')
            for config in order:
                runs[config].append(measure(workdir, args.port, config == 'on', args.requests))
                print('round %d %-3s %s' % (round_index + 1, config, {k: round(v, 1) for k, v in runs[config][-1].items()}), flush=True)
        report = {'rounds': args.rounds, 'requests_per_endpoint': args.requests, 'endpoints': {}}
        for name in runs['off'][0]:
            off = [r[name] for r in runs['off']]
            on = [r[name] for r in runs['on']]
            diffs = [(b - a) / a for a, b in zip(off, on)]
            mean, low, high = interval(diffs)
            report['endpoints'][name] = {'off_median_ms': round(statistics.median(off), 2), 'on_median_ms': round(statistics.median(on), 2),
                                         'overhead_pct_mean': round(mean * 100, 2), 'ci95_pct': [round(low * 100, 2), round(high * 100, 2)],
                                         'off_rounds_ms': [round(v, 2) for v in off], 'on_rounds_ms': [round(v, 2) for v in on]}
        micro = microbench()
        report['added_work'] = micro
        per_request = micro['per request, folded into a summary (us)'] + micro['per SQL statement, extra (us)'] * 8
        report['bound_pct_of_median'] = {name: round(per_request / 1000 / row['off_median_ms'] * 100, 3)
                                         for name, row in report['endpoints'].items()}
        report['bound_assumes'] = 'one summary-folded request plus 8 SQL statements per request'
        text = json.dumps(report, indent=2)
        print(text)
        if args.out:
            Path(args.out).write_text(text + '\n')
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == '__main__':
    main()
