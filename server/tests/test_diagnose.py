"""ops/diagnose against a fixture log: one afternoon with a restart, a SIGKILL, a crash, a 429 and a slow map."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIAGNOSE = ROOT / 'ops' / 'diagnose'
NOW = datetime(2026, 10, 4, 13, 30, 0, tzinfo=timezone.utc)
HIST = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]


def stamp(minutes_before_now, seconds=0):
    moment = NOW - timedelta(minutes=minutes_before_now) + timedelta(seconds=seconds)
    return moment.strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % (moment.microsecond // 1000)


def ev(minutes_before_now, level, component, event, seconds=0, **fields):
    return dict(ts=stamp(minutes_before_now, seconds), level=level, component=component, event=event, **fields)


def hist(**buckets):
    out = list(HIST)
    for index, count in buckets.items():
        out[int(index[1:])] = count
    return out


def build_fixture(data_dir, pid=None, write_run_marker=True):
    """Return the event list written to <data_dir>/logs/events.jsonl (and its rotated copy)."""
    logs, crash = Path(data_dir) / 'logs', Path(data_dir) / 'crash'
    logs.mkdir(parents=True)
    crash.mkdir(parents=True)
    events = []
    add = events.append
    # Three hours ago: run A, stopped by launchd with SIGTERM.
    add(ev(200, 'info', 'proc', 'start', run_id='A', pid=100, sha='aaaaaaaaaa'))
    add(ev(200, 'info', 'proc', 'previous_run', outcome='first'))
    add(ev(150, 'warn', 'proc', 'signal', signal='SIGTERM'))
    add(ev(150, 'info', 'proc', 'exit', reason='SIGTERM', clean=True))
    # Run B starts after a clean stop, then dies without a trace (SIGKILL / OOM).
    add(ev(149, 'info', 'proc', 'start', run_id='B', pid=101, sha='bbbbbbbbbb'))
    add(ev(149, 'info', 'proc', 'previous_run', outcome='clean', exit_reason='SIGTERM'))
    for minute in range(148, 100, -5):
        add(ev(minute, 'info', 'proc', 'heartbeat', up_s=(149 - minute) * 60, rss_peak_mb=300, threads=30, fds=100, load1=2.1,
               queue=0, dropped=0, errors=0, warns=0, http_active=0, http_oldest_s=0, lanes={'trashtheturtles': 12}))
    add(ev(98, 'info', 'proc', 'start', run_id='C', pid=102, sha='bbbbbbbbbb'))
    add(ev(98, 'warn', 'proc', 'previous_run', outcome='killed', exit_reason='no shutdown record (SIGKILL, OOM, power loss or reboot)',
           prev_last_alive=stamp(101), last_events=['10:50:01.000 info collect.page seed=glowbrand.co returned=50',
                                                    '10:50:30.100 info collect.permit granted=True kind=list']))
    # Run D crashes with an uncaught exception.
    add(ev(25, 'info', 'proc', 'start', run_id='D', pid=103, sha='bbbbbbbbbb'))
    add(ev(25, 'info', 'proc', 'previous_run', outcome='killed'))
    add(ev(24, 'error', 'proc', 'crash', exc_type='RuntimeError', error='boom', sig='RuntimeError@collection:ext_next|boom', thread='main'))
    (crash / '20261004T130600-000.json').write_text(json.dumps({
        'kind': 'uncaught_exception', 'ts': stamp(24), 'run_id': 'D', 'pid': 103,
        'exception': {'type': 'RuntimeError', 'sig': 'RuntimeError@collection:ext_next|boom', 'message': 'boom', 'stack': []},
        'last_events': [{'event': 'x'}] * 5}))
    # Run E: the current run.
    add(ev(23, 'info', 'proc', 'start', run_id='E', pid=pid or os.getpid(), sha='cccccccccc'))
    add(ev(23, 'warn', 'proc', 'previous_run', outcome='crashed', exit_reason='crash',
           crash_dump=str(crash / '20261004T130600-000.json'), prev_last_alive=stamp(24)))

    # Errors: the same lock error many times, one other.
    for index in range(14):
        add(ev(22 - index, 'error', 'worker', 'step_error', exc_type='OperationalError', error='database is locked',
               sig='OperationalError@collection:ext_next|database is locked', loop='rules',
               stack=['server.py:10 serve', 'collection.py:210 ext_next', 'connection.py:44 execute'], request_id='ab%02x' % index,
               route='/api/ext/next', account='trashtheturtles'))
    add(ev(5, 'error', 'http', 'exception', exc_type='KeyError', error="'x'", sig='KeyError@maps:api_map|N',
           stack=['maps.py:50 api_map'], request_id='cd01', route='/api/map/view'))

    # HTTP: the map is slow, leads are fine, one 503.
    add(ev(20, 'info', 'http', 'agg', agg=True, key='GET /api/map/view', n=30, sum_ms=9000, max_ms=900, p95_ms=500,
           hist=hist(b5=10, b6=14, b7=6), bytes=3000000, labels={'200': 29, '304': 1}))
    add(ev(10, 'info', 'http', 'agg', agg=True, key='GET /api/leads', n=60, sum_ms=1200, max_ms=60, p95_ms=25,
           hist=hist(b2=40, b3=15, b4=5), labels={'200': 60}))
    add(ev(9, 'warn', 'http', 'req', method='GET', route='/api/map/view', status=200, dur_ms=1500.0, request_id='ef01'))
    add(ev(8, 'warn', 'http', 'req', method='POST', route='/api/tags/rename', status=503, dur_ms=250.0, request_id='ef02'))
    add(ev(8, 'info', 'http', 'agg', agg=True, key='client_gone', n=4, sum_ms=0, max_ms=0, hist=HIST, labels={'BrokenPipeError': 4}))
    # DB.
    add(ev(15, 'warn', 'db', 'lock_wait', dur_ms=1200, sql='BEGIN IMMEDIATE'))
    add(ev(14, 'warn', 'db', 'busy', dur_ms=15000, sql='UPDATE jobs SET state=?', error='database is locked'))
    add(ev(12, 'info', 'db', 'q', agg=True, key='SELECT * FROM people WHERE id=?', n=500, sum_ms=3000, max_ms=120, hist=HIST))
    add(ev(7, 'warn', 'watchdog', 'stuck_request', route='/api/map/view', age_s=41.0, stack=['maps.py:77 build']))
    add(ev(6, 'info', 'proc', 'slept', slept_s=300))

    # Collection: one healthy account, one held after a scraping warning.
    for minute in range(60, 11, -1):
        add(ev(minute, 'info', 'collect', 'permit', lane='lane-ttt', account='trashtheturtles', granted=True, kind='list', job_id=7))
        add(ev(minute, 'info', 'collect', 'page', seconds=2, lane='lane-ttt', account='trashtheturtles', outcome='ok', seed='glowbrand.co',
               direction='following', returned=50, requested=50, http_status=200, cursor_in='aa%02d' % minute, cursor_out='bb%02d' % minute))
    add(ev(18, 'info', 'collect', 'lane_state', lane='lane-ttt', account='trashtheturtles', state='running', tab='ok', paused=False,
           version='3.9.31'))
    add(ev(18, 'info', 'ext', 'ig.req', seconds=1, lane='lane-ttt', account='trashtheturtles', status=200, ms=640, kind='list', via='ext-log', lag_s=4.0, ext_v='3.9.31'))
    add(ev(17, 'warn', 'ext', 'heartbeat_gap', lane='lane-ttt', account='trashtheturtles', gap_s=130))
    add(ev(16, 'info', 'ext', 'sw_start', lane='lane-ttt', account='trashtheturtles', n=3, via='ext-log', lag_s=6.0, ext_v='3.9.31'))
    add(ev(11, 'info', 'ext', 'ig.req', lane='lane-ttt', account='trashtheturtles', status=429, ms=900, kind='list', via='ext-log', lag_s=2.0, level_hint='x'))
    add(ev(11, 'warn', 'ig', 'adverse', seconds=1, lane='lane-ttt', account='trashtheturtles', code='rate_limit', reason='please_wait',
           http_status=429, kind='list', direction='following'))
    add(ev(11, 'warn', 'state', 'setting', seconds=1, key='cooldown', value=stamp(-4), request_id='rq9'))
    add(ev(11, 'info', 'collect', 'lane_state', seconds=2, lane='lane-ttt', account='trashtheturtles', state='waiting', tab='ok', paused=False,
           cooldown_until=stamp(-4), version='3.9.31'))
    add(ev(4, 'info', 'collect', 'permit', lane='lane-ttt', account='trashtheturtles', granted=False, kind='list', why='blocked',
           hint='cooldown_until=x', first=True))
    add(ev(90, 'warn', 'ig', 'adverse', lane='lane-dih', account='dihfluencer', code='challenge', reason='scraping_warning', http_status=200))
    add(ev(90, 'error', 'ig', 'adverse', seconds=1, lane='lane-dih', account='dihfluencer', code='challenge', reason='scraping_warning', http_status=200))
    add(ev(30, 'info', 'collect', 'lane_state', lane='lane-dih', account='dihfluencer', state='tab_challenge', tab='tab_challenge', hold='challenge', paused=True, version='3.9.31'))
    add(ev(3, 'info', 'collect', 'permit', lane='lane-dih', account='dihfluencer', granted=False, kind='list', why='blocked', hint='hold:challenge'))
    add(ev(2, 'info', 'proc', 'heartbeat', up_s=1320, rss_peak_mb=412, threads=38, fds=210, load1=3.5, queue=0, dropped=0, errors=15, warns=9,
           http_active=1, http_oldest_s=0, lanes={'trashtheturtles': 8, 'dihfluencer': 20}))
    add(ev(1, 'info', 'proc', 'heartbeat', up_s=1380, rss_peak_mb=412, threads=38, fds=210, load1=3.5, queue=0, dropped=0, errors=15, warns=9,
           http_active=0, http_oldest_s=0, lanes={'trashtheturtles': 8, 'dihfluencer': 50},
           settings={'paused_lists': True, 'cooldown': stamp(-4), 'instagram_scraping_warning': {'lane': 'lane-dih', 'pending': ['lane-x']}}))

    events.sort(key=lambda e: e['ts'])
    older = [e for e in events if e['ts'] < stamp(120)]
    newer = [e for e in events if e['ts'] >= stamp(120)]
    (logs / 'events.jsonl.1').write_text(''.join(json.dumps(e) + '\n' for e in older))
    (logs / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in newer))
    if write_run_marker:
        (logs / 'run.json').write_text(json.dumps({'run_id': 'E', 'pid': pid or os.getpid(), 'status': 'running', 'started': stamp(23),
                                                   'last_alive': stamp(1), 'sha': 'cccccccccc'}))
    return events


def run(data_dir, *args, stderr_file=None):
    command = [sys.executable, str(DIAGNOSE), '--data-dir', str(data_dir), '--now', NOW.strftime('%Y-%m-%dT%H:%M:%S.000Z')]
    command += list(args) if stderr_file else list(args) + ['--no-stderr']
    if stderr_file:
        command += ['--stderr', str(stderr_file)]
    return subprocess.run(command, capture_output=True, text=True, timeout=60)


class DiagnoseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.events = build_fixture(self.dir)

    def report(self, *args):
        result = run(self.dir, '--json', *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def text(self, *args):
        result = run(self.dir, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_default_report_fits_about_eighty_lines_and_names_every_section(self):
        out = self.text('--since', '4h')
        lines = out.splitlines()
        self.assertLessEqual(len(lines), 80, out)
        for heading in ('SERVER', 'RESTARTS in window: 4', 'ERRORS', 'SLOW ENDPOINTS', 'COLLECTION', 'LAST INSTAGRAM ADVERSE EVENT',
                        'EXTENSION LANES'):
            self.assertIn(heading, out)
        self.assertIn('STATE (last heartbeat): paused lists, cooldown until 13:34Z, scraping warning on lane-dih +1 more', out)
        self.assertIn('KILLED', out)
        self.assertIn('CRASHED', out)
        self.assertIn('no shutdown record', out)

    def test_restarts_are_classified_with_their_exit_reasons(self):
        report = self.report('--since', '4h')
        outcomes = [(r['after'], r['reason']) for r in report['server']['restarts']]
        self.assertEqual([o[0] for o in outcomes], ['first', 'clean', 'killed', 'killed', 'crashed'])   # JSON keeps the first run too
        self.assertEqual(outcomes[1][1], 'SIGTERM')
        self.assertTrue(report['server']['alive'])
        self.assertEqual(report['server']['crash_dumps'][0]['kind'], 'uncaught_exception')
        self.assertEqual(report['server']['watchdog'], {'stuck_request': 1})
        self.assertEqual(report['server']['slept'], [300])

    def test_errors_are_grouped_by_signature_with_counts_and_one_sample(self):
        errors = self.report('--since', '4h')['errors']
        top = errors[0]
        self.assertEqual((top['sig'], top['n']), ('OperationalError@collection:ext_next|database is locked', 14))
        self.assertLess(top['first'], top['last'])
        self.assertEqual(top['sample']['stack'][-1], 'connection.py:44 execute')
        self.assertTrue(any(g['sig'].startswith('KeyError@') for g in errors))
        self.assertIn('x14', self.text('--since', '4h'))

    def test_slow_endpoints_rank_by_p95_and_merge_aggregates_with_single_requests(self):
        http = self.report('--since', '4h')['http']
        routes = {r['route']: r for r in http['routes']}
        self.assertEqual(http['routes'][0]['route'], 'GET /api/map/view')
        self.assertEqual(routes['GET /api/map/view']['n'], 31)           # 30 folded + 1 slow single request
        self.assertGreaterEqual(routes['GET /api/map/view']['max_ms'], 1500)
        self.assertLess(routes['GET /api/leads']['p95_ms'], 100)
        self.assertEqual(http['client_gone'], 4)
        self.assertEqual(http['slow_requests'], 1)

    def test_collection_timeline_per_account_with_the_adverse_context(self):
        collection = self.report('--since', '4h')['collection']
        by_account = {a['account']: a for a in collection['accounts']}
        turtle = by_account['trashtheturtles']
        self.assertEqual((turtle['permits'], turtle['pages']), (49, 49))
        self.assertEqual(turtle['permits_last_hour'], 49)
        self.assertEqual(turtle['permits_by_hour_utc'], {'12:00': 30, '13:00': 19})
        self.assertEqual(turtle['last_adverse']['code'], 'rate_limit')
        self.assertEqual(turtle['denial']['hint'], 'cooldown_until=x')
        self.assertEqual(turtle['state']['state'], 'waiting')
        self.assertEqual(turtle['last_success']['event'], 'page')
        held = by_account['dihfluencer']
        self.assertEqual(held['state']['hold'], 'challenge')
        self.assertEqual(held['denial']['hint'], 'hold:challenge')
        context = collection['adverse_context']
        self.assertEqual(context['event']['code'], 'rate_limit')                   # the newest adverse event overall
        self.assertEqual(len(context['before']), 20)
        self.assertTrue(all(e['ts'] <= context['event']['ts'] for e in context['before']))
        self.assertTrue(any(e['event'] == 'page' for e in context['before']))
        self.assertNotIn('dihfluencer', json.dumps([e.get('account') for e in context['before']]))   # the other account's noise is left out

    def test_account_filter_and_window(self):
        report = self.report('--since', '4h', '--account', '@dihfluencer')
        self.assertEqual([a['account'] for a in report['collection']['accounts']], ['dihfluencer'])
        self.assertEqual(report['collection']['adverse_context']['event']['reason'], 'scraping_warning')
        narrow = self.report('--since', '5m')
        self.assertEqual(len(narrow['server']['restarts']), 0)
        self.assertEqual([g['sig'].split('|')[0] for g in narrow['errors']], ['KeyError@maps:api_map'])
        prefix = self.report('--since', '4h', '--account', 'lane-t')
        self.assertEqual([a['account'] for a in prefix['collection']['accounts']], ['trashtheturtles'])

    def test_extension_lane_health(self):
        lanes = {l['account']: l for l in self.report('--since', '4h')['extension']}
        turtle = lanes['trashtheturtles']
        self.assertEqual((turtle['gaps'], turtle['sw_starts'], turtle['last_age_s']), (1, 1, 8))
        self.assertEqual(lanes['dihfluencer']['last_age_s'], 50)

    def test_tail_prints_the_last_events_one_per_line(self):
        out = self.text('--since', '4h', '--tail', '5')
        self.assertEqual(len(out.splitlines()), 5)
        self.assertIn('proc.heartbeat', out.splitlines()[-1])
        filtered = self.text('--since', '4h', '--tail', '3', '--account', 'dihfluencer')
        self.assertTrue(all('dihfluencer' in line for line in filtered.splitlines()))

    def test_it_reads_files_only_and_says_so_when_the_server_is_not_running(self):
        gone = subprocess.Popen([sys.executable, '-c', 'pass'])
        gone.wait()
        for path in (self.dir / 'logs').iterdir():
            path.unlink()
        for path in (self.dir / 'crash').iterdir():
            path.unlink()
        build_fixture_again = tempfile.TemporaryDirectory()
        self.addCleanup(build_fixture_again.cleanup)
        build_fixture(Path(build_fixture_again.name) / 'd', pid=gone.pid)
        out = run(Path(build_fixture_again.name) / 'd', '--since', '4h').stdout
        self.assertIn('NOT RUNNING', out)

    def test_a_silent_but_alive_server_is_flagged_as_a_possible_hang(self):
        out = run(self.dir, '--since', '4h', '--now', (NOW + timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%S.000Z')).stdout
        self.assertIn('ALIVE BUT SILENT', out)
        self.assertIn('kill -USR1', out)

    def test_missing_logs_still_report_and_fall_back_to_the_stderr_file(self):
        empty = self.dir / 'empty'
        empty.mkdir()
        stderr = self.dir / 'launchd.log'
        stderr.write_text('Fortunate Leads on http://127.0.0.1:8777  db=x\n' + (
            'Traceback (most recent call last):\n  File "/x/server.py", line 3115, in route\n    boom()\n'
            'sqlite3.OperationalError: database is locked\n') * 3 + 'BrokenPipeError: [Errno 32] Broken pipe\n')
        out = run(empty, '--since', '1h', stderr_file=stderr).stdout
        self.assertIn('NO STRUCTURED LOG', out)
        self.assertIn('x3', out)
        self.assertIn('database is locked', out)
        self.assertEqual(run(empty, '--since', '1h', '--json').returncode, 0)

    def test_torn_and_garbage_lines_are_skipped_not_fatal(self):
        with open(self.dir / 'logs' / 'events.jsonl', 'a') as handle:
            handle.write('not json at all\n{"ts":"2026-10-04T13:29:00.000Z","level":"info","comp')
        report = self.report('--since', '4h')
        self.assertGreaterEqual(report['bad_lines'], 1)
        self.assertGreater(report['events'], 100)

    def test_bad_arguments_exit_with_a_message(self):
        result = run(self.dir, '--since', 'tomorrow')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--since expects', result.stderr)


if __name__ == '__main__':
    unittest.main()
