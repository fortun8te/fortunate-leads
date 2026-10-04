"""Structured logging and crash forensics (server/obs.py, server/obs_events.py). Temporary folders only."""
import http.client
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

import obs
import obs_events


def read_events(directory):
    """Every event in the active log and its rotated copies, oldest first."""
    logs = Path(directory) / 'logs'
    names = sorted((p for p in logs.glob('events.jsonl*')), key=lambda p: -int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0)
    rows = []
    for path in names:
        for line in path.read_text().splitlines():
            rows.append(json.loads(line))
    return rows


class ObsCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = self.temp.name
        self.addCleanup(obs.stop)
        obs_events._LANES.clear()
        obs_events._LANE_HANDLE.clear()
        obs_events._last_setting.clear()
        obs_events._denied.clear()

    def start(self, **options):
        defaults = dict(hooks=False, watchdog=False, poll=0.01)
        defaults.update(options)
        return obs.start(self.dir, **defaults)

    def events(self, **match):
        obs.flush_aggs(force=True)
        self.assertTrue(obs.flush(3))
        rows = read_events(self.dir)
        return [r for r in rows if all(r.get(k) == v for k, v in match.items())]


class RedactionTests(ObsCase):
    SECRETS = ['SESSIONSECRET', 'CSRFSECRET', 'BEARERSECRET', 'sk-or-v1-APIKEYSECRET', 'NOTESECRET', 'DMSECRET',
               'COOKIESECRET', 'QUERYSECRET', 'PASSWORDSECRET']

    def test_credentials_and_typed_text_never_reach_disk_or_ring(self):
        self.start()
        obs.info('test', 'secrets', route='/api/x', status=200,
                 cookie='sessionid=COOKIESECRET; csrftoken=CSRFSECRET', sessionid='SESSIONSECRET',
                 authorization='Bearer BEARERSECRET', headers={'Authorization': 'Bearer BEARERSECRET'},
                 api_key='sk-or-v1-APIKEYSECRET', note='NOTESECRET', dm='DMSECRET', password='PASSWORDSECRET',
                 nested={'cookie': 'COOKIESECRET', 'ok': 1, 'deeper': {'session_id': 'SESSIONSECRET', 'n': 2}},
                 message='failed: sessionid=SESSIONSECRET csrftoken=CSRFSECRET Bearer BEARERSECRET',
                 url='https://www.instagram.com/api/v1/x/?max_id=QUERYSECRET', path='/api/v1/y/?q=QUERYSECRET',
                 text='NOTESECRET again', bio='NOTESECRET', items=['sk-or-v1-APIKEYSECRET', 'fine'])
        rows = self.events(event='secrets')
        self.assertEqual(len(rows), 1)
        disk = (Path(self.dir) / 'logs' / 'events.jsonl').read_text()
        ring = json.dumps(list(obs.S.ring), default=str)
        for secret in self.SECRETS:
            self.assertNotIn(secret, disk)
            self.assertNotIn(secret, ring)
        row = rows[0]
        self.assertEqual((row['route'], row['status']), ('/api/x', 200))
        self.assertEqual(row['cookie'], '[redacted]')
        self.assertEqual(row['nested']['ok'], 1)
        self.assertEqual(row['nested']['cookie'], '[redacted]')
        self.assertEqual(row['items'][1], 'fine')

    def test_scrub_examples(self):
        cases = {
            'sessionid=abc123456; other=1': 'sessionid=[redacted]; other=1',
            'Authorization: Bearer abcdefghijklmnop': 'Authorization=[redacted]',
            'key sk-or-v1-abcdef1234567890 end': 'key [redacted] end',
            'https://x.com/a?max_id=ZZZ&b=1': 'https://x.com/a?[query]',
            'WHERE id=? AND a=?': 'WHERE id=? AND a=?',
            '12:00:03 ok': '12:00:03 ok',
        }
        for given, expected in cases.items():
            self.assertEqual(obs.scrub(given), expected)
        long = 'word ' * 200
        self.assertEqual(len(obs.scrub(long)), obs.MAX_STR + len('...(+700)'))
        self.assertIn('[redacted:48ch]', obs.scrub('token blob ' + 'A1' * 24 + ' end'))

    def test_token_counts_are_not_mistaken_for_tokens(self):
        self.start()
        obs.info('llm', 'call', tokens_in=12, tokens_out=34, lease_token='LEASESECRET')
        row = self.events(event='call')[0]
        self.assertEqual((row['tokens_in'], row['tokens_out'], row['lease_token']), (12, 34, '[redacted]'))

    def test_unprintable_and_odd_values_do_not_raise(self):
        self.start()

        class Bad:
            def __str__(self):
                raise RuntimeError('no')
        obs.info('test', 'odd', bad=Bad(), nan=float('nan'), inf=float('inf'), big=10 ** 30, items=set(range(50)), b=b'bytes')
        row = self.events(event='odd')[0]
        self.assertEqual(row['bad'], '[unprintable]')
        self.assertIsNone(row.get('nan'))
        self.assertEqual(len(row['items']), 24)

    def test_extension_log_intake_redacts_bounds_and_tags_the_lane(self):
        self.start()
        batch = [{'t': int(time.time() * 1000) - 4000, 'lv': 'warn', 'ev': 'ig.req', 'status': 429, 'ms': 812,
                  'cookie': 'sessionid=COOKIESECRET', 'path': '/api/v1/friendships/55/following/?max_id=QUERYSECRET',
                  'err': 'sessionid=SESSIONSECRET boom', 'nested': {'a': 1}},
                 {'ev': 'Bad Name!', 'lv': 'info'}, 'junk', {'ev': 'sw_start', 'n': 3, 'text': 'NOTESECRET'}]
        out = obs_events.api_ext_log(None, {}, {'lane_id': 'lane-1', 'account': {'handle': 'alt1'}, 'events': batch + [{'ev': 'x%d' % i} for i in range(400)],
                                                'dropped': 7, 'v': '3.9.31'})
        self.assertEqual(out, {'accepted': 198})   # at most 200 entries are read; two of those are invalid
        rows = self.events(component='ext')
        disk = (Path(self.dir) / 'logs' / 'events.jsonl').read_text()
        for secret in self.SECRETS:
            self.assertNotIn(secret, disk)
        first = next(r for r in rows if r['event'] == 'ig.req')
        self.assertEqual((first['lane'], first['account'], first['status'], first['level'], first['via']),
                         ('lane-1', 'alt1', 429, 'warn', 'ext-log'))
        self.assertGreaterEqual(first['lag_s'], 3.5)
        self.assertEqual(first['ext_v'], '3.9.31')
        self.assertEqual(first['path'], '/api/v1/friendships/55/following/?[query]')
        self.assertNotIn('nested', first)
        self.assertTrue(any(r['event'] == 'log_dropped' and r['n'] == 7 for r in rows))
        with self.assertRaises(ValueError):
            obs_events.api_ext_log(None, {}, {'events': 'nope'})

    def test_model_call_events_carry_no_prompt_text(self):
        self.start()

        @obs_events.traced_call('openrouter', 'chat', obs_events.openrouter_fields, obs_events.openrouter_outcome)
        def post(url, key, model, messages, timeout, max_tokens, json_mode, usage_out=None):
            usage_out.update(input_tokens=11, output_tokens=5)
            return 'REPLYSECRET'
        usage = {}
        post('u', 'KEYSECRET', 'x-ai/grok:free', [{'role': 'user', 'content': 'PROMPTSECRET'}], 5, 100, True, usage)
        with self.assertRaises(KeyError):
            obs_events.traced_call('k2', 'x')(lambda: (_ for _ in ()).throw(KeyError('x')))()
        rows = self.events(component='llm')
        text = json.dumps(rows)
        for secret in ('KEYSECRET', 'PROMPTSECRET', 'REPLYSECRET'):
            self.assertNotIn(secret, text)
        ok = rows[0]
        self.assertEqual((ok['model'], ok['status'], ok['tokens_in'], ok['tokens_out'], ok['prompt_chars']),
                         ('x-ai/grok:free', 'ok', 11, 5, 12))
        self.assertEqual(rows[1]['status'], 'KeyError')


class RotationTests(ObsCase):
    def test_log_rotates_by_size_and_keeps_a_fixed_number_of_files(self):
        self.start(max_bytes=6000, files=3)
        for i in range(400):
            obs.info('test', 'fill', i=i, pad='x' * 40)
        self.assertTrue(obs.flush(5))
        logs = Path(self.dir) / 'logs'
        names = sorted(p.name for p in logs.glob('events.jsonl*'))
        self.assertEqual(names, ['events.jsonl', 'events.jsonl.1', 'events.jsonl.2'])
        for name in names:
            path = logs / name
            self.assertLessEqual(path.stat().st_size, 6000)
            for line in path.read_text().splitlines():
                json.loads(line)       # no torn lines at a rotation boundary
        rows = [r for r in read_events(self.dir) if r['event'] == 'fill']
        indexes = [r['i'] for r in rows]
        self.assertEqual(indexes, sorted(indexes))
        self.assertEqual(indexes[-1], 399)                       # the newest event is always kept
        self.assertEqual(indexes, list(range(indexes[0], 400)))  # and what is kept is contiguous
        self.assertGreater(indexes[0], 0)                        # the oldest were dropped by rotation
        self.assertGreaterEqual(obs.S.writer.rotations, 2)

    def test_writing_works_when_the_monotonic_clock_is_near_zero(self):
        # Python 3.9 on macOS starts time.monotonic() near zero in each process; a fresh CI container can too.
        import types
        shim = types.SimpleNamespace(**{name: getattr(time, name) for name in dir(time) if not name.startswith('__')})
        shim.monotonic = lambda: 0.5
        with mock.patch.object(obs, 'time', shim):
            self.start()
            obs.info('test', 'early')
            self.assertTrue(obs.flush(3))
        self.assertEqual([r['event'] for r in read_events(self.dir) if r['event'] == 'early'], ['early'])

    def test_files_are_private_and_a_restart_appends(self):
        self.start()
        obs.info('test', 'one')
        self.assertTrue(obs.flush(3))
        path = Path(self.dir) / 'logs' / 'events.jsonl'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        obs.stop()
        self.start()
        obs.info('test', 'two')
        names = [r['event'] for r in self.events() if r['event'] in ('one', 'two')]
        self.assertEqual(names, ['one', 'two'])


class BackpressureTests(ObsCase):
    def test_a_stalled_writer_never_blocks_callers_and_drops_are_counted(self):
        self.start()
        release = threading.Event()
        real_write = obs.S.writer.write

        def stalled(lines):
            release.wait(10)
            real_write(lines)
        obs.S.writer.write = stalled
        obs.info('test', 'wake-the-writer')
        time.sleep(0.1)                      # the writer is now stuck inside write()
        began = time.perf_counter()
        for i in range(60000):
            obs.info('test', 'flood', i=i)
        elapsed = time.perf_counter() - began
        self.assertLess(elapsed, 3.0, 'event() must not wait for the writer')
        self.assertGreater(obs.S.dropped, 0)
        self.assertLessEqual(len(obs.S.q), obs.QUEUE_MAX + 1)
        self.assertEqual(len(obs.S.ring), obs.RING_MAX)           # the crash ring still has the newest events
        self.assertEqual(obs.S.ring[-1]['i'], 59999)
        release.set()
        self.assertTrue(obs.flush(10))
        dropped = [r for r in read_events(self.dir) if r['event'] == 'dropped']
        self.assertTrue(dropped)
        self.assertEqual(dropped[-1]['total'], obs.S.dropped)

    def test_an_unwritable_log_directory_never_raises(self):
        if os.geteuid() == 0:
            self.skipTest('running as root: directory permissions are not enforced')
        self.start()
        logs = Path(self.dir) / 'logs'
        os.chmod(logs, 0o500)
        self.addCleanup(os.chmod, logs, 0o700)
        obs.S.writer.close()
        for i in range(200):
            obs.error('test', 'lost', i=i)
        time.sleep(0.2)
        self.assertGreaterEqual(obs.S.writer.errors, 1)
        os.chmod(logs, 0o700)

    def test_event_is_cheap(self):
        self.start()
        began = time.perf_counter()
        for i in range(20000):
            obs.info('http', 'req', method='GET', route='/api/x', status=200, dur_ms=1.5, bytes=100)
        per_call_us = (time.perf_counter() - began) / 20000 * 1e6
        self.assertLess(per_call_us, 60, 'about 6 us on an idle Mac; the bound allows a loaded one')

    def test_logging_is_a_no_op_until_started(self):
        before = list(obs.S.ring)
        obs.info('test', 'ignored', cookie='x')
        self.assertEqual(list(obs.S.ring), before)
        self.assertEqual(obs.connect_options(), {})


class DatabaseTests(ObsCase):
    def test_slow_statements_lock_waits_and_busy_errors_are_logged_without_literals(self):
        self.start()
        self.assertIs(obs.connect_options()['factory'], obs.TimedConnection)
        path = os.path.join(self.dir, 'db.sqlite')
        with mock.patch.object(obs, '_DB_NOTE', 0.0), mock.patch.object(obs, '_DB_SLOW', 0.0), mock.patch.object(obs, '_DB_LOCK', 0.05):
            a = sqlite3.connect(path, factory=obs.TimedConnection, timeout=0.2, check_same_thread=False)
            a.execute('CREATE TABLE t(x TEXT, y INTEGER)')
            a.execute("INSERT INTO t VALUES('SECRETLITERAL', 5)")
            a.commit()
            a.execute("SELECT * FROM t WHERE x='SECRETLITERAL' AND y=5").fetchall()
            a.execute('BEGIN IMMEDIATE')
            b = sqlite3.connect(path, factory=obs.TimedConnection, timeout=0.3)
            with self.assertRaises(sqlite3.OperationalError):
                b.execute('BEGIN IMMEDIATE')
            threading.Timer(0.15, a.rollback).start()
            c = sqlite3.connect(path, factory=obs.TimedConnection, timeout=3)
            c.execute('BEGIN IMMEDIATE')       # waits for the lock a holds, then succeeds
            c.rollback()
            for conn in (a, b, c):
                conn.close()
            obs.flush_aggs(force=True)
        rows = self.events(component='db')
        text = json.dumps(rows)
        self.assertNotIn('SECRETLITERAL', text)
        slow = [r for r in rows if r['event'] == 'slow' and 'WHERE x=?' in r['sql']]
        self.assertTrue(slow and 'y=?' in slow[0]['sql'])
        busy = [r for r in rows if r['event'] == 'busy']
        self.assertTrue(busy and busy[0]['level'] == 'warn')
        waits = [r for r in rows if r['event'] == 'lock_wait']
        self.assertTrue(waits and waits[0]['dur_ms'] >= 100)

    def test_settings_changes_are_logged_once_per_real_change(self):
        self.start()
        import storage.settings as settings
        conn = sqlite3.connect(os.path.join(self.dir, 's.sqlite'))
        conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)')
        settings.set_setting(conn, 'cooldown', '2026-10-04T12:00:00+00:00')
        settings.set_setting(conn, 'cooldown', '2026-10-04T12:00:00+00:00')
        settings.set_setting(conn, 'paused_lists', True)
        settings.set_setting(conn, 'instagram_scraping_warning', {'lane': 'a', 'ig_id': '1', 'pending': {'b': {'lane': 'b'}}})
        settings.set_setting(conn, 'unrelated_key', 1)
        self.assertEqual(sorted(obs_events.settings_gauge()), ['cooldown', 'instagram_scraping_warning', 'paused_lists'])
        conn.close()
        rows = self.events(component='state')
        self.assertEqual([(r['key'], r['level']) for r in rows],
                         [('cooldown', 'warn'), ('paused_lists', 'info'), ('instagram_scraping_warning', 'warn')])
        self.assertEqual(rows[2]['value']['pending'], ['b'])


class BaselineTests(ObsCase):
    def test_start_up_records_the_control_state_the_run_began_in(self):
        self.start()
        conn = sqlite3.connect(os.path.join(self.dir, 's.sqlite'))
        conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)')
        conn.executemany('INSERT INTO settings VALUES(?,?)', [
            ('paused_lists', 'true'), ('paused_bios', 'false'), ('cooldown', '"2026-10-04T15:00:00+00:00"'),
            ('instagram_scraping_warning', json.dumps({'lane': 'dih', 'pending': {'other': {'lane': 'other'}}})), ('unrelated', '1')])
        obs_events.baseline(conn)
        conn.close()
        row = self.events(event='baseline')[0]
        self.assertEqual((row['paused_lists'], row['paused_bios'], row['cooldown']), (True, False, '2026-10-04T15:00:00+00:00'))
        self.assertEqual(row['instagram_scraping_warning']['pending'], ['other'])
        self.assertNotIn('unrelated', row)
        self.assertEqual(sorted(obs_events.settings_gauge()), ['cooldown', 'instagram_scraping_warning', 'paused_lists'])


class HeartbeatTests(ObsCase):
    def test_heartbeat_lines_carry_vitals_and_loop_counters(self):
        self.start(watchdog=True, tick_s=0.05, hb_s=0.15, hang_s=0)
        step = obs.wrap_step('demo', lambda conn: True)
        for _ in range(3):
            step(None)
        time.sleep(0.6)
        beats = self.events(event='heartbeat')
        self.assertGreaterEqual(len(beats), 3)
        last = beats[-1]
        for key in ('up_s', 'threads', 'rss_peak_mb', 'queue', 'dropped', 'http_active', 'loops'):
            self.assertIn(key, last)
        self.assertEqual(last['loops']['demo'][:3], [3, 3, 0])

    def test_a_request_stuck_past_the_limit_is_reported_with_its_stack(self):
        self.start(watchdog=True, tick_s=0.05, hb_s=60, hang_s=0, stuck_s=0.2)
        gate = threading.Event()

        def very_slow_handler():
            gate.wait(5)
        worker = threading.Thread(target=lambda: (obs_events.request_begin('GET', '/api/slow?q=hidden'), very_slow_handler()))
        worker.start()
        time.sleep(0.6)
        gate.set()
        worker.join()
        rows = self.events(component='watchdog', event='stuck_request')
        self.assertEqual(len(rows), 1, 'reported once, not on every tick')
        self.assertEqual(rows[0]['route'], '/api/slow')
        self.assertTrue(any('very_slow_handler' in line for line in rows[0]['stack']))
        self.assertNotIn('hidden', json.dumps(rows))

    def test_a_stuck_worker_loop_is_reported(self):
        self.start(watchdog=True, tick_s=0.05, hb_s=60, hang_s=0, stuck_s=0.2)
        gate = threading.Event()

        def hung_step(conn):
            gate.wait(5)
        step = obs.wrap_step('lab', hung_step)
        thread = threading.Thread(target=step, args=(None,))
        thread.start()
        time.sleep(0.6)
        gate.set()
        thread.join()
        rows = self.events(component='watchdog', event='stuck_loop')
        self.assertEqual([r['loop'] for r in rows], ['lab'])
        self.assertTrue(any('hung_step' in line for line in rows[0]['stack']))


class ProcessTests(unittest.TestCase):
    """Real child processes: crashes, kills and signals as the next start sees them."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = self.temp.name

    def script(self, body):
        return ('import sys, time, threading, os\nsys.path.insert(0, %r)\nimport obs\n' % str(SERVER) +
                'obs.start(%r, tick_s=0.05, hb_s=0.1, poll=0.01)\n' % self.dir + body)

    def run_child(self, body, timeout=30):
        return subprocess.run([sys.executable, '-c', self.script(body)], capture_output=True, text=True, timeout=timeout)

    def previous(self):
        """What a fresh start says about the run before it."""
        result = self.run_child('obs.stop()')
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [r for r in read_events(self.dir) if r['event'] == 'previous_run']
        return rows[-1]

    def marker(self):
        return json.loads((Path(self.dir) / 'logs' / 'run.json').read_text())

    def test_uncaught_exception_writes_a_crash_dump_and_the_next_start_says_crashed(self):
        result = self.run_child("""
obs.info('test', 'before_crash', n=1)
obs.warn('test', 'about_to_crash', cookie='sessionid=DUMPSECRET')
time.sleep(0.2)
raise RuntimeError('boom sessionid=DUMPSECRET42')
""")
        self.assertEqual(result.returncode, 1)
        self.assertIn('RuntimeError', result.stderr)                   # the original excepthook still prints
        dumps = list((Path(self.dir) / 'crash').glob('*.json'))
        self.assertEqual(len(dumps), 1)
        text = dumps[0].read_text()
        self.assertNotIn('DUMPSECRET', text)
        body = json.loads(text)
        self.assertEqual(body['kind'], 'uncaught_exception')
        self.assertEqual(body['exception']['type'], 'RuntimeError')
        self.assertTrue(body['exception']['sig'].startswith('RuntimeError'))
        names = [e['event'] for e in body['last_events']]
        self.assertIn('about_to_crash', names)
        self.assertIn('MainThread#', ' '.join(body['threads']))
        self.assertLessEqual(len(body['last_events']), obs.RING_MAX)
        self.assertEqual(self.marker()['status'], 'crashed')
        rows = read_events(self.dir)
        self.assertIn('crash', [r['event'] for r in rows])           # flushed before the process died
        verdict = self.previous()
        self.assertEqual(verdict['outcome'], 'crashed')
        self.assertTrue(verdict['crash_dump'].endswith('.json'))
        self.assertIn('about_to_crash', ' '.join(verdict['last_events']))

    def test_sigkill_is_reported_as_killed_with_the_last_events(self):
        child = subprocess.Popen([sys.executable, '-c', self.script("""
obs.info('test', 'last_words', n=7)
time.sleep(0.3)
print('ready', flush=True)
time.sleep(60)
""")], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            child.kill()
            child.wait(10)
        finally:
            if child.poll() is None:
                child.kill()
            child.stdout.close()
        self.assertEqual(self.marker()['status'], 'running')           # nothing recorded a shutdown
        verdict = self.previous()
        self.assertEqual(verdict['outcome'], 'killed')
        self.assertIn('no shutdown record', verdict['exit_reason'])
        self.assertIn('last_words', ' '.join(verdict['last_events']))

    def test_sigterm_is_logged_as_the_shutdown_reason(self):
        child = subprocess.Popen([sys.executable, '-c', self.script("""
print('ready', flush=True)
time.sleep(60)
""")], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            child.send_signal(signal.SIGTERM)
            self.assertEqual(child.wait(10), 0)
        finally:
            if child.poll() is None:
                child.kill()
            child.stdout.close()
        marker = self.marker()
        self.assertEqual((marker['status'], marker['reason']), ('clean', 'SIGTERM'))
        rows = read_events(self.dir)
        self.assertIn('SIGTERM', [r.get('signal') for r in rows if r['event'] == 'signal'])
        self.assertEqual([r['reason'] for r in rows if r['event'] == 'exit'], ['SIGTERM'])
        verdict = self.previous()
        self.assertEqual((verdict['outcome'], verdict['exit_reason']), ('clean', 'SIGTERM'))

    def test_a_thread_that_dies_is_logged_and_dumped_but_the_run_stays_clean(self):
        result = self.run_child("""
t = threading.Thread(target=lambda: 1 / 0, name='fortunate-demo')
t.start(); t.join()
time.sleep(0.3)
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = read_events(self.dir)
        died = [r for r in rows if r['event'] == 'uncaught']
        self.assertEqual(len(died), 1)
        self.assertEqual((died[0]['thread'], died[0]['exc_type']), ('fortunate-demo', 'ZeroDivisionError'))
        dumps = [json.loads(p.read_text()) for p in (Path(self.dir) / 'crash').glob('*.json')]
        self.assertEqual([d['kind'] for d in dumps], ['thread_exception'])
        self.assertEqual(self.marker()['status'], 'clean')

    def test_a_second_start_after_a_clean_exit_says_clean_and_the_first_says_first(self):
        first = self.run_child('obs.info("test", "hello")')
        self.assertEqual(first.returncode, 0, first.stderr)
        verdicts = [r['outcome'] for r in read_events(self.dir) if r['event'] == 'previous_run']
        self.assertEqual(verdicts, ['first'])
        self.assertEqual(self.previous()['outcome'], 'clean')

    def test_sigusr1_dumps_every_thread_stack_without_stopping_the_process(self):
        child = subprocess.Popen([sys.executable, '-c', self.script("""
print('ready', flush=True)
time.sleep(1.5)
""")], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            child.send_signal(signal.SIGUSR1)
            self.assertEqual(child.wait(10), 0)
        finally:
            if child.poll() is None:
                child.kill()
            child.stdout.close()
        dumps = list((Path(self.dir) / 'crash').glob('faulthandler.log')) + list((Path(self.dir) / 'crash').glob('*faulthandler.txt'))
        self.assertTrue(dumps and 'most recent call first' in dumps[0].read_text())


class HangDumpTests(unittest.TestCase):
    def test_a_process_that_stops_heartbeating_dumps_every_stack_and_the_next_start_files_it(self):
        with tempfile.TemporaryDirectory() as directory:
            code = ('import sys, time\nsys.path.insert(0, %r)\nimport obs\n'
                    'obs.start(%r, tick_s=30, hb_s=300, hang_s=0.4, poll=0.01)\n'
                    'def frozen_here():\n    time.sleep(1.2)\nfrozen_here()\n' % (str(SERVER), directory))
            result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            fault = Path(directory) / 'crash' / 'faulthandler.log'
            self.assertIn('Timeout (0:00:00.400000)!', fault.read_text())
            self.assertIn('frozen_here', fault.read_text())
            second = subprocess.run([sys.executable, '-c', 'import sys\nsys.path.insert(0, %r)\nimport obs\nobs.start(%r)\nobs.stop()' % (str(SERVER), directory)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(second.returncode, 0, second.stderr)
            filed = list((Path(directory) / 'crash').glob('*-faulthandler.txt'))
            self.assertEqual(len(filed), 1)
            self.assertEqual(json.loads((Path(directory) / 'logs' / 'run.json').read_text())['status'], 'clean')   # a hang dump is not a crash


class HttpTests(ObsCase):
    """A real Application on a temporary database: what a request leaves in the log."""

    def setUp(self):
        super().setUp()
        os.environ.setdefault('FL_NO_ORSLOT', '1')
        from backend.app import Application
        from backend.common import AppConfig
        self.start()
        self.config = AppConfig(str(Path(self.dir) / 'leads.sqlite'), port=0)
        self.app = Application(self.config)
        self.addCleanup(self.app.close)
        self.thread = threading.Thread(target=lambda: self.app.serve(workers=False), daemon=True)
        self.thread.start()
        deadline = time.time() + 5
        while not (self.app.server is not None and self.app.serving.is_set()) and time.time() < deadline:
            time.sleep(0.01)
        self.port = self.app.server.server_address[1]
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.app.close)

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        data = None if body is None else json.dumps(body)
        sent = {'X-FL': '1', 'Content-Type': 'application/json'}
        sent.update(headers or {})
        conn.request(method, path, body=data, headers=sent)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, json.loads(payload or b'{}')

    def test_routine_reads_are_folded_and_nothing_from_the_query_is_logged(self):
        for _ in range(4):
            self.assertEqual(self.call('GET', '/api/counts?q=QUERYSECRET')[0], 200)
        self.assertEqual(self.call('GET', '/api/nope')[0], 404)
        rows = self.events(component='http')
        self.assertNotIn('QUERYSECRET', json.dumps(rows))
        aggs = [r for r in rows if r['event'] == 'agg' and r['key'] == 'GET /api/counts']
        self.assertEqual(sum(r['n'] for r in aggs), 4)
        self.assertEqual(aggs[0]['labels'], {'200': 4})
        self.assertEqual([r for r in rows if r['event'] == 'req' and r['route'] == '/api/counts'], [])
        missing = [r for r in rows if r['event'] == 'req' and r['route'] == '/api/nope']
        self.assertEqual(missing[0]['status'], 404)
        self.assertRegex(missing[0]['request_id'] if 'request_id' in missing[0] else 'x.1', r'.+')

    def test_heartbeats_log_state_changes_not_every_beat(self):
        beat = {'lane_id': 'lane-a', 'account': {'ig_id': '11', 'handle': 'alt1'}, 'version': '3.9.31', 'state': 'idle', 'tab': 'ok',
                'today': {'list': 1, 'profile': 0}, 'cool': {'list': None, 'profile': None}}
        for _ in range(3):
            self.assertEqual(self.call('POST', '/api/ext/heartbeat', beat)[0], 200)
        states = self.events(event='lane_state')
        self.assertEqual(len(states), 1)
        self.assertEqual((states[0]['lane'], states[0]['account'], states[0]['state'], states[0]['first']), ('lane-a', 'alt1', 'idle', True))
        self.call('POST', '/api/ext/heartbeat', dict(beat, state='running'))
        self.call('POST', '/api/ext/heartbeat', dict(beat, state='running'))
        states = self.events(event='lane_state')
        self.assertEqual([s['state'] for s in states], ['idle', 'running'])
        self.assertEqual(states[1]['prev_repeats'], 2)

    def test_denied_permits_collapse_into_one_line_and_an_adverse_response_links_to_its_state_change(self):
        lane = {'lane_id': 'lane-a', 'account': {'ig_id': '11', 'handle': 'alt1'}}
        self.call('POST', '/api/ext/heartbeat', dict(lane, version='3.9.31', state='idle', tab='ok'))
        for _ in range(5):
            self.call('POST', '/api/ext/request', dict(lane, action='acquire', kind='list', job_id=1))
        permits = self.events(event='permit')
        self.assertEqual(len(permits), 1)
        self.assertEqual((permits[0]['granted'], permits[0]['why'], permits[0]['first']), (False, 'stale_lease', True))
        status, _ = self.call('POST', '/api/ext/error', dict(lane, code='rate_limit', reason='please_wait', http_status=429, kind='list',
                                                           message='rate_limit (please_wait) on @seed following (HTTP 429) | {"sessionid":"BODYSECRET"}',
                                                           event_id='e1'))
        self.assertEqual(status, 200)
        adverse = self.events(event='adverse')[0]
        self.assertEqual((adverse['code'], adverse['http_status'], adverse['account'], adverse['level']), ('rate_limit', 429, 'alt1', 'warn'))
        self.assertNotIn('BODYSECRET', json.dumps(self.events()))
        cooldown = [r for r in self.events(component='state', event='setting') if r['key'] == 'cooldown']
        self.assertEqual(len(cooldown), 1)
        self.assertEqual(cooldown[0]['request_id'], adverse['request_id'])      # same request: cause and effect
        self.assertEqual(cooldown[0]['route'], '/api/ext/error')

    def test_extension_events_posted_over_http_land_in_the_server_log(self):
        status, out = self.call('POST', '/api/ext-log', {'lane_id': 'lane-a', 'account': {'handle': 'alt1'}, 'v': '3.9.31', 'dropped': 0,
                                                         'events': [{'t': int(time.time() * 1000), 'lv': 'error', 'ev': 'step_threw', 'err': 'boom'}]})
        self.assertEqual((status, out['accepted']), (200, 1))
        row = self.events(component='ext')[0]
        self.assertEqual((row['event'], row['level'], row['lane'], row['account'], row['err']), ('step_threw', 'error', 'lane-a', 'alt1', 'boom'))
        browser = self.call('POST', '/api/ext-log', {'events': []}, headers={'Origin': 'http://evil.example', 'X-FL': ''})
        self.assertEqual(browser[0], 403)       # a web page cannot write to the log

    def test_failures_and_slow_requests_are_individual_warnings_and_errors(self):
        from backend_http import Handler, Server
        calls = []

        def boom(conn, q, b):
            raise RuntimeError('kaboom sessionid=ROUTESECRET')

        def locked(conn, q, b):
            raise sqlite3.OperationalError('database is locked')

        def sleepy(conn, q, b):
            time.sleep(0.05)
            return {'done': True}
        routes = self.app.routes + [('GET', '/api/test/boom', boom), ('GET', '/api/test/locked', locked), ('GET', '/api/test/sleepy', sleepy)]
        http_app = self.app.http_application(routes=routes)
        server = Server(('127.0.0.1', 0), Handler, application=http_app)
        http_app.config = self.app.config.with_port(server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        with mock.patch('traceback.print_exc'):          # keep the (unredacted, as before) stderr traceback out of the test output
            self.assertEqual(self.call('GET', '/api/test/boom')[0], 500)
        self.assertEqual(self.call('GET', '/api/test/locked')[0], 503)
        with mock.patch.object(obs_events, 'SLOW_MS', 20):
            self.assertEqual(self.call('GET', '/api/test/sleepy')[0], 200)
        rows = self.events()
        boom_error = next(r for r in rows if r['event'] == 'exception')
        self.assertEqual((boom_error['level'], boom_error['exc_type'], boom_error['component']), ('error', 'RuntimeError', 'http'))
        self.assertTrue(boom_error['sig'].startswith('RuntimeError@'))
        self.assertTrue(any('boom' in frame for frame in boom_error['stack']))
        self.assertNotIn('ROUTESECRET', json.dumps(rows))
        self.assertTrue(boom_error['request_id'])
        reqs = {r['route']: r for r in rows if r['event'] == 'req'}
        self.assertEqual((reqs['/api/test/boom']['status'], reqs['/api/test/boom']['level']), (500, 'error'))
        self.assertEqual((reqs['/api/test/locked']['status'], reqs['/api/test/locked']['level']), (503, 'warn'))
        self.assertEqual((reqs['/api/test/sleepy']['status'], reqs['/api/test/sleepy']['level']), (200, 'warn'))
        self.assertGreaterEqual(reqs['/api/test/sleepy']['dur_ms'], 50)

    def test_repeating_failures_collapse_instead_of_flooding_the_log(self):
        for _ in range(30):
            self.assertEqual(self.call('GET', '/api/nope')[0], 404)
        rows = self.events(component='http')
        lines = [r for r in rows if r['event'] == 'req' and r['route'] == '/api/nope']
        self.assertEqual(len(lines), 1)
        counted = sum(r['n'] for r in rows if r['event'] == 'agg' and r['key'] == 'GET /api/nope')
        self.assertEqual(counted, 29)                        # nothing is lost: the rest are in the per-route count

    def test_a_loop_failing_in_a_tight_cycle_logs_one_line_per_signature(self):
        for i in range(50):
            try:
                raise ValueError('same failure %d' % i)
            except ValueError:
                with mock.patch('traceback.print_exc'):
                    obs.log_exception('worker', 'step_error', loop='rules')
        rows = self.events(event='step_error')
        self.assertEqual(len(rows), 1)

    def test_a_browser_closing_a_connection_is_a_count_not_a_traceback(self):
        from backend_http import Server
        server = self.app.server
        try:
            raise BrokenPipeError(32, 'Broken pipe')
        except BrokenPipeError:
            Server.handle_error(server, None, ('127.0.0.1', 5))
        rows = self.events(component='http', event='agg')
        gone = [r for r in rows if r['key'] == 'client_gone']
        self.assertEqual((gone[0]['n'], gone[0]['labels']), (1, {'BrokenPipeError': 1}))
        self.assertEqual(self.events(event='handler_crash'), [])


class PageEventTests(ObsCase):
    def test_list_page_event_has_cursor_fingerprints_not_cursors(self):
        self.start()
        ticket = obs_events.Ticket()
        ticket.lane, ticket.account = 'lane-a', 'alt1'
        body = {'job_id': 9, 'seed': 'seedaccount', 'direction': 'following', 'users': [{'handle': 'a'}] * 50, 'requested_count': 50,
                'http_status': 200, 'has_more': True, 'done': False, 'requested_cursor': 'CURSORIN123', 'next_cursor': 'CURSOROUT456',
                'total': 1200}
        obs_events._page(ticket, {}, body, {'received': 150})
        stalled = dict(body, requested_cursor='SAMECURSOR', next_cursor='SAMECURSOR')
        obs_events._page(ticket, {}, stalled, {'received': 150, 'stalled': True, 'partial': True})
        first, second = self.events(event='page')
        self.assertEqual((first['returned'], first['requested'], first['outcome'], first['received'], first['seed']), (50, 50, 'ok', 150, 'seedaccount'))
        self.assertNotIn('CURSOR', json.dumps([first, second]))
        self.assertNotEqual(first['cursor_in'], first['cursor_out'])
        self.assertEqual((second['outcome'], second['level'], second['cursor_in'] == second['cursor_out']), ('stalled', 'warn', True))


if __name__ == '__main__':
    unittest.main()
