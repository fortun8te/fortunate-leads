"""Matched HTTP measurements on synthetic fixtures, without starting workers.

Run this file in separate processes for the base and candidate source trees::

    python3 tests/bench_backend_v2.py run --source /tmp/fortunate-backend-v2/base \
        --fixture /tmp/fortunate-backend-v2/baseline.sqlite \
        --db /tmp/fortunate-backend-v2/http-base.sqlite \
        --report /tmp/fortunate-backend-v2/http-base.json
    python3 tests/bench_backend_v2.py run --source . --fixture ... --db ... --report ...
    python3 tests/bench_backend_v2.py compare --baseline ... --candidate ... --report ...

Cold means cleared application response caches, not cold filesystem/disk caches.
Only a fixture marked by tests/bench.py is accepted. All writes target its copy.
Reports contain generated profile data only and belong outside the repository.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import statistics
import sys
import threading
import time


CASES = {
    'leads_score': '/api/leads?limit=50',
    'leads_connected': '/api/leads?sort=connected&limit=50',
    'leads_min_lists': '/api/leads?min_lists=2&limit=50',
    'leads_strong_fit': '/api/leads?fit=strong&limit=50',
    'leads_search': '/api/leads?q=user12&limit=50',
    'counts': '/api/counts',
    'counts_filtered': '/api/counts?tags=Coffee',
    'tags': '/api/tags',
    'tags_filtered': '/api/tags?tags=Coffee',
    'control': '/api/control',
    'map': '/api/map?limit=400',
    'map_view': '/api/map/view?cohort=1&budget=500',
    'map_search': '/api/map/search?q=user12',
}

# Capture every field, especially saved observation/profile dates. Version 2
# prevents earlier reports with globally stripped provenance from passing.
RAW_RESPONSE_VERSION = 2
RESPONSE_COMPARISON_VERSION = 3
COMPARISON_EXCLUSIONS = [{
    'endpoint': 'control', 'path': '$.body.at',
    'reason': 'control.snapshot sets at=iso(datetime.now(timezone.utc)) for the response snapshot; it is not a saved observation date',
}]


def stable(value):
    if isinstance(value, dict):
        return {key: stable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [stable(item) for item in value]
    return value


def comparison_response(name, response):
    """Remove only the proven control-response clock; raw captures stay intact."""
    out = stable(response)
    if name == 'control':
        body = out.get('body')
        if not isinstance(body, dict) or not isinstance(body.get('at'), str):
            raise ValueError('control $.body.at must exist and be an ISO timestamp before its value can be excluded')
        try:
            clock = datetime.fromisoformat(body['at'].replace('Z', '+00:00'))
        except ValueError as exc:
            raise ValueError('control $.body.at must be a valid ISO timestamp') from exc
        if clock.tzinfo is None or clock.utcoffset() != timedelta(0):
            raise ValueError('control $.body.at must retain its UTC timezone')
        del body['at']
    return out


def summary(samples):
    ordered = sorted(samples)
    return {'n': len(samples), 'p50_ms': round(statistics.median(samples), 3),
            'p95_ms': round(ordered[max(0, math.ceil(.95 * len(ordered)) - 1)], 3),
            'max_ms': round(max(samples), 3)}


def clone_fixture(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or target.suffix != '.sqlite' or '/tmp/' not in str(target):
        raise ValueError('The working DB must be a separate /tmp/*.sqlite file')
    conn = sqlite3.connect(f'file:{source}?mode=ro', uri=True)
    try:
        setting = conn.execute("SELECT value FROM settings WHERE key='bench_fixture_v2'").fetchone()
        if not setting or json.loads(setting[0]) is not True:
            raise ValueError('Only the generated tests/bench.py fixture is accepted')
        target.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ('', '-wal', '-shm'):
            Path(str(target) + suffix).unlink(missing_ok=True)
        dest = sqlite3.connect(target)
        try:
            conn.backup(dest)
        finally:
            dest.close()
        count = conn.execute('SELECT count(*) FROM people').fetchone()[0]
        digest = hashlib.sha256()
        for row in conn.execute('SELECT id,handle,followers FROM people ORDER BY id'):
            digest.update(json.dumps(row, separators=(',', ':')).encode())
    finally:
        conn.close()
    map_source, map_target = Path(str(source) + '.map'), Path(str(target) + '.map')
    if map_target.exists():
        shutil.rmtree(map_target)
    if map_source.exists():
        shutil.copytree(map_source, map_target)
    snapshot_digest = hashlib.sha256()
    with target.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            snapshot_digest.update(block)
    return {'people': count, 'identity_digest': digest.hexdigest(),
            'snapshot_sha256_before_migration': snapshot_digest.hexdigest(),
            'same_sqlite_backup_source': str(source), 'prepared_map_copied': map_source.exists()}


def request(port, path, method='GET', body=None, timeout=30):
    started = time.perf_counter()
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    headers = {}
    if method == 'POST':
        headers = {'Origin': f'http://127.0.0.1:{port}', 'Content-Type': 'application/json'}
    try:
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            value = {'raw_hex': raw.hex()}
        return {'status': response.status, 'elapsed_ms': (time.perf_counter() - started) * 1000,
                'body': value, 'headers': dict(response.getheaders())}
    except Exception as exc:
        return {'status': None, 'elapsed_ms': (time.perf_counter() - started) * 1000,
                'error': f'{type(exc).__name__}: {exc}'}
    finally:
        conn.close()


def run(args):
    os.environ['FL_NO_ORSLOT'] = '1'
    source = Path(args.source).resolve()
    fixture = clone_fixture(args.fixture, args.db)
    sys.path.insert(0, str(source / 'server'))
    import db
    import server
    import map_view

    migration_started = time.perf_counter()
    db.init(args.db).close()
    migration_ms = (time.perf_counter() - migration_started) * 1000
    preparation_ms = None
    if args.prepare_ranking:
        import lead_rank
        preparation_started = time.perf_counter()
        conn = db.connect(args.db)
        try:
            lead_rank.prepare(conn)
            conn.commit()
        finally:
            conn.close()
        preparation_ms = (time.perf_counter() - preparation_started) * 1000
    server.CFG['db'] = str(Path(args.db).resolve())
    # Instantiate only the HTTP listener. Never main(), start_workers or runtime startup.
    httpd = server.Server(('127.0.0.1', 0), server.Handler)
    port = httpd.server_address[1]
    server.CFG['port'] = port
    serving = threading.Thread(target=httpd.serve_forever, name='bench-http', daemon=True)
    serving.start()
    stop = threading.Event()
    peaks = {'process_threads': 0, 'nonclient_threads': 0}

    def monitor():
        while not stop.wait(.002):
            threads = threading.enumerate()
            peaks['process_threads'] = max(peaks['process_threads'], len(threads))
            peaks['nonclient_threads'] = max(peaks['nonclient_threads'], sum(
                not thread.name.startswith(('bench-client', 'bench-monitor')) for thread in threads))

    sampler = threading.Thread(target=monitor, name='bench-monitor', daemon=True)
    sampler.start()

    def clear():
        server.clear_caches()
        with map_view._CACHE_LOCK:
            map_view._CACHE.clear()
        map_view.close_pool()

    report = {'source': str(source), 'fixture': fixture, 'runs': args.runs,
              'response_comparison_version': RAW_RESPONSE_VERSION,
              'excluded_response_paths': [],
              'python_executable': sys.executable, 'python_version': sys.version,
              'migration_ms': round(migration_ms, 3),
              'explicit_ranking_prepare_ms': round(preparation_ms, 3) if preparation_ms is not None else None,
              'cold_definition': 'Application response caches and layout reader pool cleared; OS cache retained',
              'responses': {}, 'latency': {}, 'concurrency': {}, 'errors': []}
    try:
        connect_times = []
        for _ in range(args.connections):
            started = time.perf_counter()
            conn = db.connect(args.db)
            conn.close()
            connect_times.append((time.perf_counter() - started) * 1000)
        report['sqlite_connect_close'] = summary(connect_times)
        for name, path in CASES.items():
            report['latency'][name] = {}
            for mode in ('cold', 'warm'):
                if mode == 'warm':
                    request(port, path)
                samples = []
                for index in range(args.runs):
                    if mode == 'cold':
                        clear()
                    result = request(port, path)
                    samples.append(result['elapsed_ms'])
                    if mode == 'cold' and index == 0:
                        report['responses'][name] = {'status': result['status'], 'body': stable(result.get('body'))}
                    if result['status'] != 200:
                        report['errors'].append({'case': name, 'mode': mode, **result})
                report['latency'][name][mode] = summary(samples)

        for workload in ('warm_mixed', 'cold_same_map', 'warm_same_control'):
            clear()
            if workload.startswith('warm'):
                for path in CASES.values():
                    request(port, path)
            barrier = threading.Barrier(args.clients)
            peaks.update(process_threads=0, nonclient_threads=0)

            def client(index):
                barrier.wait(timeout=30)
                if workload == 'cold_same_map':
                    path = CASES['map']
                elif workload == 'warm_same_control':
                    path = CASES['control']
                else:
                    path = list(CASES.values())[index % len(CASES)]
                return request(port, path)

            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=args.clients, thread_name_prefix='bench-client') as pool:
                results = list(pool.map(client, range(args.clients)))
            statuses = {}
            for result in results:
                status = str(result['status'])
                statuses[status] = statuses.get(status, 0) + 1
            report['concurrency'][workload] = {**summary([r['elapsed_ms'] for r in results]),
                'wall_ms': round((time.perf_counter() - started) * 1000, 3), 'status_counts': statuses,
                'errors': sum(result['status'] != 200 for result in results),
                'retryable_rejections': sum(result['status'] == 503 for result in results), **peaks,
                'thread_measurement': 'Python thread enumeration sampled every 2ms; nonclient includes main and HTTP listener'}

        conn = sqlite3.connect(args.db)
        pid = conn.execute('SELECT min(id) FROM people').fetchone()[0]
        mark_before = conn.execute('SELECT status,note FROM marks WHERE person_id=?', (pid,)).fetchone()
        conn.execute('BEGIN IMMEDIATE')
        holder_started = time.perf_counter()
        # The holder ends after one second even if the request is still waiting.
        releaser = threading.Timer(1.0, lambda: None)
        releaser.start()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='bench-client') as pool:
            future = pool.submit(request, port, f'/api/person/{pid}/mark', 'POST', {'status': 'interested'})
            releaser.join()
            conn.rollback()
            held_ms = (time.perf_counter() - holder_started) * 1000
            conn.close()
            busy = future.result(timeout=30)
        check = sqlite3.connect(args.db)
        try:
            mark_after = check.execute('SELECT status,note FROM marks WHERE person_id=?', (pid,)).fetchone()
        finally:
            check.close()
        report['busy_writer'] = {'holder_requested_ms': 1000, 'holder_actual_ms': round(held_ms, 3),
            'status': busy['status'],
            'elapsed_ms': round(busy['elapsed_ms'], 3), 'body': stable(busy.get('body')),
            'retry_after': busy.get('headers', {}).get('Retry-After'), 'error': busy.get('error'),
            'mark_before': mark_before, 'mark_after': mark_after,
            'rejected_write_preserved_mark': mark_before == mark_after if busy['status'] == 503 else None}
        report['map_view_prepared'] = bool(report['responses']['map_view']['body'].get('ready', False))
    finally:
        stop.set()
        sampler.join(timeout=2)
        httpd.shutdown()
        httpd.server_close()
        serving.join(timeout=2)
        map_view.close_pool()
    Path(args.report).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: report[key] for key in ('fixture', 'sqlite_connect_close', 'concurrency', 'busy_writer')}, indent=2))
    print(f'Report: {args.report}')


def differences(a, b, path='$', cap=20):
    if type(a) is not type(b):
        return [{'path': path, 'baseline': a, 'candidate': b}]
    if isinstance(a, dict):
        result = []
        for key in sorted(a.keys() | b.keys()):
            if key not in a or key not in b:
                result.append({'path': f'{path}.{key}', 'baseline': a.get(key), 'candidate': b.get(key)})
            else:
                result.extend(differences(a[key], b[key], f'{path}.{key}', cap))
            if len(result) >= cap:
                return result[:cap]
        return result
    if isinstance(a, list):
        if len(a) != len(b):
            return [{'path': path + '.length', 'baseline': len(a), 'candidate': len(b)}]
        result = []
        for index, pair in enumerate(zip(a, b)):
            result.extend(differences(*pair, f'{path}[{index}]', cap))
            if len(result) >= cap:
                return result[:cap]
        return result
    return [] if a == b else [{'path': path, 'baseline': a, 'candidate': b}]


def compare(args):
    base, candidate = [json.loads(Path(path).read_text()) for path in (args.baseline, args.candidate)]
    if any(report.get('response_comparison_version') != RAW_RESPONSE_VERSION
           or report.get('excluded_response_paths') != [] for report in (base, candidate)):
        raise ValueError('Rerun both reports: comparison requires intact provenance and no excluded response fields')
    if base['fixture'] != candidate['fixture']:
        raise ValueError('Fixture metadata differs; timings are not matched')
    for field in ('python_executable', 'python_version', 'runs', 'cold_definition'):
        if base[field] != candidate[field]:
            raise ValueError(f'{field} differs; timings are not matched')
    report = {'fixture': base['fixture'], 'response_differences': {}, 'latency': {},
              'response_comparison_version': RESPONSE_COMPARISON_VERSION,
              'excluded_response_paths': COMPARISON_EXCLUSIONS,
              'response_cases': list(CASES),
              'runs_per_condition': {'baseline': base['runs'], 'candidate': candidate['runs']},
              'python_executable': {'baseline': base['python_executable'], 'candidate': candidate['python_executable']},
              'python_version': {'baseline': base['python_version'], 'candidate': candidate['python_version']},
              'cold_definition': base['cold_definition'],
              'map_view_prepared': {'baseline': base['map_view_prepared'], 'candidate': candidate['map_view_prepared']},
              'baseline_errors': base['errors'], 'candidate_errors': candidate['errors'],
              'candidate_migration_ms': candidate.get('migration_ms'),
              'candidate_explicit_ranking_prepare_ms': candidate.get('explicit_ranking_prepare_ms')}
    for name in CASES:
        diff = differences(comparison_response(name, base['responses'][name]),
                           comparison_response(name, candidate['responses'][name]))
        if diff:
            report['response_differences'][name] = diff
        report['latency'][name] = {}
        for mode in ('cold', 'warm'):
            old, new = base['latency'][name][mode], candidate['latency'][name][mode]
            report['latency'][name][mode] = {'baseline': old, 'candidate': new,
                'p50_ratio': round(new['p50_ms'] / old['p50_ms'], 3) if old['p50_ms'] else None}
    for key in ('sqlite_connect_close', 'concurrency', 'busy_writer'):
        report[key] = {'baseline': base[key], 'candidate': candidate[key]}
    report['all_stable_responses_equal'] = not report['response_differences']
    Path(args.report).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'all_stable_responses_equal': report['all_stable_responses_equal'],
                      'response_differences': report['response_differences']}, indent=2))
    if not report['all_stable_responses_equal'] or report['baseline_errors'] or report['candidate_errors']:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    running = commands.add_parser('run')
    running.add_argument('--source', required=True)
    running.add_argument('--fixture', required=True)
    running.add_argument('--db', required=True)
    running.add_argument('--report', required=True)
    running.add_argument('--runs', type=int, default=5)
    running.add_argument('--clients', type=int, default=24)
    running.add_argument('--connections', type=int, default=100)
    running.add_argument('--prepare-ranking', action='store_true',
                         help='Measure explicit lead_rank.prepare on the temp copy before serving')
    comparing = commands.add_parser('compare')
    comparing.add_argument('--baseline', required=True)
    comparing.add_argument('--candidate', required=True)
    comparing.add_argument('--report', required=True)
    args = parser.parse_args()
    if args.command == 'run':
        if min(args.runs, args.clients, args.connections) < 1:
            parser.error('runs, clients and connections must be positive')
        run(args)
    else:
        compare(args)


if __name__ == '__main__':
    main()
