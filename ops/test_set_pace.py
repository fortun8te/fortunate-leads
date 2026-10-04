"""ops/set-pace against a real server on a temp database; run: python3 -m unittest ops/test_set_pace.py"""
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'server'))
import db  # noqa: E402
import server  # noqa: E402

loader = importlib.machinery.SourceFileLoader('set_pace', str(ROOT / 'ops' / 'set-pace'))
spec = importlib.util.spec_from_loader('set_pace', loader)
set_pace = importlib.util.module_from_spec(spec)
loader.exec_module(set_pace)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.CFG['db'] = str(Path(self.tmp.name) / 'leads.sqlite')
        db.init(server.CFG['db']).close()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        server.CFG['port'] = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{server.CFG['port']}"
        body = {'lane_id': 'lane1', 'account': {'ig_id': '5501', 'handle': 'turtles'}, 'version': '3.9.31', 'state': 'idle', 'tab': 'ok',
                'hold': None, 'day': '2026-10-4', 'today': {'list': 0, 'profile': 0}}
        request = urllib.request.Request(self.url + '/api/ext/heartbeat', data=json.dumps(body).encode(), method='POST')
        request.add_header('Origin', server.EXT_ORIGIN)
        urllib.request.urlopen(request).read()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def run_cli(self, *argv):
        lines = []
        code = set_pace.run(list(argv) + ['--url', self.url], out=lines.append)
        return code, '\n'.join(lines)

    def test_show_then_slow_then_back(self):
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn('Pace for all accounts: normal', text)
        self.assertIn('@turtles: normal (all accounts)', text)
        code, text = self.run_cli('slow')
        self.assertIn('normal -> slow', text)
        self.assertIn('Expected requests per hour: 364 -> 155', text)
        self.assertIn('list gap', text)
        code, text = self.run_cli('normal')
        self.assertIn('slow -> normal', text)

    def test_explicit_numbers_are_held_at_the_bounds_and_say_so(self):
        _, text = self.run_cli('--list-gap', '3-5')
        self.assertIn('held at the bound', text)
        self.assertIn('nothing changed', text)
        _, text = self.run_cli('--list-gap', '12-20', '--account', '@turtles')
        self.assertIn('Pace for @turtles', text)
        self.assertIn('7-12 -> 12-20', text)

    def test_dry_run_saves_nothing_and_no_daily_cap(self):
        _, text = self.run_cli('slow', '--dry-run')
        self.assertIn('nothing saved', text)
        self.assertIn('normal', self.run_cli()[1].splitlines()[0])
        _, text = self.run_cli('--no-daily-cap')
        self.assertIn('no cap', text)
        self.assertIn('no daily cap on pages', self.run_cli()[1])

    def test_errors_are_plain(self):
        with self.assertRaises(set_pace.Fail) as ctx:
            self.run_cli('slow', '--reset')
        self.assertIn('Choose one', str(ctx.exception))
        with self.assertRaises(set_pace.Fail) as ctx:
            self.run_cli('slow', '--account', '@nobody')
        self.assertIn('No single account', str(ctx.exception))
        with self.assertRaises(set_pace.Fail) as ctx:
            set_pace.run(['--url', 'http://127.0.0.1:9'], out=print)
        self.assertIn("Can't reach the workspace", str(ctx.exception))

    def test_parse(self):
        self.assertEqual(set_pace.parse_value('9-14'), [9.0, 14.0])
        self.assertEqual(set_pace.parse_value('9'), 9.0)
        self.assertEqual(set_pace.parse_value('72', integer=True), 72)
        with self.assertRaises(set_pace.Fail):
            set_pace.parse_value('quick')


if __name__ == '__main__':
    unittest.main()
