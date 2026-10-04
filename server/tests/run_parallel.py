#!/usr/bin/env python3
"""Run the backend suite in parallel processes, one group of test modules per worker.

    python3 server/tests/run_parallel.py                 # all modules, auto worker count
    python3 server/tests/run_parallel.py -j 6 -v         # six workers, per-test lines
    python3 server/tests/run_parallel.py test_lead_rank  # only the named modules
    python3 server/tests/run_parallel.py --durations 20  # also list the slowest modules
    python3 server/tests/run_parallel.py --record        # rewrite shard_weights.json from this run

`python3 -m unittest discover -s server/tests` stays the reference command and runs the same
tests in one process. This runner only changes the schedule: modules are dealt to workers,
longest first (weights in shard_weights.json), so total wall time approaches the longest
single module instead of the sum. Tests build their own temporary databases and bind
ephemeral ports, so workers do not share state. A module's tests still run in one process,
in the same order as under discover.

The report gives wall time and CPU time. On a busy machine wall time says little; the busiest
worker's CPU time is what an idle machine would take.

Exit status is 0 only when every worker passes. Failures are printed with the module's
full unittest output; an import error counts as an error, as under discover.
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time
import traceback
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent
WEIGHTS = HERE / 'shard_weights.json'
DEFAULT_WEIGHT = 2.0


def module_names(selected):
    names = sorted(path.stem for path in HERE.glob('test_*.py'))
    if not selected:
        return names
    chosen = []
    for item in selected:
        item = item.removesuffix('.py')
        matches = [name for name in names if name == item] or [name for name in names if item in name]
        if not matches:
            raise SystemExit(f'No test module matches {item!r}')
        chosen.extend(match for match in matches if match not in chosen)
    return chosen


def load_weights():
    try:
        return json.loads(WEIGHTS.read_text())
    except (OSError, ValueError):
        return {}


def deal(names, workers, weights):
    """Longest-processing-time-first: each module goes to the currently lightest worker."""
    shards = [{'load': 0.0, 'names': []} for _ in range(workers)]
    for name in sorted(names, key=lambda n: (-weights.get(n, DEFAULT_WEIGHT), n)):
        shard = min(shards, key=lambda s: s['load'])
        shard['names'].append(name)
        shard['load'] += weights.get(name, DEFAULT_WEIGHT)
    return [shard['names'] for shard in shards if shard['names']]


def worker(names, verbosity):
    """Run the named modules in this process; print one JSON line per module on stdout."""
    os.environ.setdefault('FL_NO_ORSLOT', '1')
    sys.path[:0] = [str(HERE), str(SERVER)]
    loader = unittest.TestLoader()
    for name in names:
        stream = io.StringIO()
        started, cpu_started = time.perf_counter(), time.process_time()
        try:
            suite = loader.loadTestsFromName(name)
        except unittest.SkipTest as reason:   # an optional dependency is missing, as under discover
            print(json.dumps({'module': name, 'run': 0, 'failures': 0, 'errors': 0, 'skipped': 1,
                              'seconds': round(time.perf_counter() - started, 3), 'cpu': 0.0,
                              'output': f'{name}: skipped ({reason})\n'}),
                  flush=True)
            continue
        except Exception:   # an import error fails this module only, not the worker
            print(json.dumps({'module': name, 'run': 1, 'failures': 0, 'errors': 1,
                              'seconds': round(time.perf_counter() - started, 3), 'cpu': 0.0,
                              'output': traceback.format_exc()}),
                  flush=True)
            continue
        result = unittest.TextTestRunner(stream=stream, verbosity=verbosity).run(suite)
        print(json.dumps({'module': name, 'run': result.testsRun, 'failures': len(result.failures),
                          'errors': len(result.errors), 'skipped': len(result.skipped),
                          'seconds': round(time.perf_counter() - started, 3),
                          'cpu': round(time.process_time() - cpu_started, 3),
                          'output': stream.getvalue() if not result.wasSuccessful() or verbosity > 1 else ''}),
              flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('modules', nargs='*', help='module names or substrings (default: all)')
    parser.add_argument('-j', '--jobs', type=int, default=0,
                        help='worker processes (default: half the cores, 2 to 8)')
    parser.add_argument('-v', '--verbose', action='store_true', help='print every test result')
    parser.add_argument('--durations', type=int, default=0, metavar='N', help='list the N slowest modules')
    parser.add_argument('--record', action='store_true', help='rewrite shard_weights.json from this run')
    parser.add_argument('--worker', nargs='+', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return worker(args.worker, 2 if args.verbose else 1)

    names = module_names(args.modules)
    workers = max(1, min(args.jobs or max(2, min(8, (os.cpu_count() or 4) // 2)), len(names)))
    shards = deal(names, workers, load_weights())
    env = dict(os.environ, FL_NO_ORSLOT='1', PYTHONPATH=os.pathsep.join(
        [str(HERE), str(SERVER)] + ([os.environ['PYTHONPATH']] if os.environ.get('PYTHONPATH') else [])))
    started = time.perf_counter()
    procs = [subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker', *shard]
                              + (['-v'] if args.verbose else []),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
             for shard in shards]
    results, crashed = [], []
    for shard, proc in zip(shards, procs):
        out, err = proc.communicate()
        lines = []
        for line in out.splitlines():
            try:
                lines.append(json.loads(line))
            except ValueError:
                err += line + '\n'
        results.extend(lines)
        finished = {line['module'] for line in lines}
        if proc.returncode or set(shard) - finished:
            crashed.append((sorted(set(shard) - finished), proc.returncode, err))
        elif err.strip() and args.verbose:
            sys.stderr.write(err)
    elapsed = time.perf_counter() - started

    bad = [r for r in results if r['failures'] or r['errors']]
    for r in sorted(bad, key=lambda r: r['module']):
        print('=' * 70 + f"\n{r['module']}: {r['failures']} failed, {r['errors']} errors\n" + r['output'])
    if args.verbose:
        for r in sorted(results, key=lambda r: r['module']):
            print(r['output'], end='')
    for modules, code, err in crashed:
        print('=' * 70 + f"\nworker exited with status {code} before finishing: {', '.join(modules)}\n{err[-4000:]}")
    if args.durations:
        print(f'Slowest {args.durations} modules (CPU seconds, wall seconds, tests)')
        for r in sorted(results, key=lambda r: -r['cpu'])[:args.durations]:
            print(f"  {r['cpu']:8.2f}  {r['seconds']:8.2f}  {r['run']:4d}  {r['module']}")
    total = sum(r['run'] for r in results)
    skipped = sum(r['skipped'] for r in results)
    failed = sum(r['failures'] + r['errors'] for r in results)
    print('-' * 70)
    cpu = {tuple(shard): sum(r['cpu'] for r in results if r['module'] in shard) for shard in shards}
    print(f'Ran {total} tests in {len(results)} modules on {len(shards)} workers in {elapsed:.1f}s wall; '
          f'CPU {sum(cpu.values()):.1f}s in total, {max(cpu.values(), default=0):.1f}s on the busiest worker '
          '(the wall time an otherwise idle machine needs)')
    ok = not failed and not crashed
    print('OK' + (f' (skipped={skipped})' if skipped else '') if ok else
          f'FAILED ({failed} failed or errored' + (f', {len(crashed)} workers crashed' if crashed else '') + ')')
    if args.record and ok:
        weights = {r['module']: round(r['cpu'], 1) for r in sorted(results, key=lambda r: r['module'])}
        WEIGHTS.write_text(json.dumps(weights, indent=1, sort_keys=True) + '\n')
        print(f'Recorded {WEIGHTS.relative_to(HERE.parent.parent)}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
