"""CLI contract tests for ops/account_twin.py against an isolated fake workspace; no Instagram, no real server."""
import io
import json
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import account_twin as cli


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, payload, code=200):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.respond(self.server.status)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.posts.append((self.path, self.headers.get('Origin'), body))
        if self.server.refuse:
            return self.respond({'ok': False, 'error': self.server.refuse}, 400)
        self.respond(self.server.status)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Fake)
        self.server.status = {'state': 'off', 'enabled': False, 'limits': {'ceiling_per_hour': 295},
                              'preflight': {'ok': False, 'blockers': [{'code': 'x', 'text': 'Open the profile first.'}], 'lanes': []}}
        self.server.posts, self.server.refuse = [], None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['--base-url', self.base, *args])
        return code, out.getvalue(), err.getvalue()

    def test_status_is_read_only_and_readable(self):
        code, out, _ = self.run_cli('status')
        self.assertEqual(code, 0)
        self.assertIn('state        off', out)
        self.assertIn('Open the profile first.', out)
        self.assertEqual(self.server.posts, [])

    def test_every_change_needs_yes_and_sends_a_reviewed_request(self):
        for args, action in ((('baseline-start', '--first', 'lane-a'), 'start_baseline'), (('baseline-finish',), 'finish_baseline'),
                             (('enable', '--first', 'lane-a', '--second', 'lane-b'), 'enable'), (('advance',), 'advance'),
                             (('disable', '--reason', 'done'), 'disable'), (('reset',), 'reset')):
            with self.subTest(action=action):
                self.server.posts.clear()
                code, out, _ = self.run_cli(*args)
                self.assertEqual(code, 1)
                self.assertIn('--yes', out)
                self.assertEqual(self.server.posts, [], 'nothing is sent without --yes')
                code, _, _ = self.run_cli(*args, '--yes')
                self.assertEqual(code, 0)
                path, origin, body = self.server.posts[-1]
                self.assertEqual((path, origin, body['action'], body['reviewed']), ('/api/account-twin', self.base, action, True))

    def test_enable_names_the_first_profile_first(self):
        self.run_cli('enable', '--first', 'lane-a', '--second', 'lane-b', '--yes')
        self.assertEqual(self.server.posts[-1][2]['lane_ids'], ['lane-a', 'lane-b'])

    def test_a_refusal_is_shown_plainly(self):
        self.server.refuse = 'Keep collecting for 12 more minutes.'
        code, _, err = self.run_cli('baseline-finish', '--yes')
        self.assertEqual(code, 1)
        self.assertIn('Keep collecting for 12 more minutes.', err)

    def test_watch_exits_with_2_when_the_test_stops_itself(self):
        self.server.status = {'state': 'tripped', 'trip': {'code': 'rate_limit', 'lane': 'lane-b', 'after_minutes': 80.5, 'reason': 'x'},
                              'current': {}, 'history': []}
        code, out, _ = self.run_cli('watch', '--every', '10')
        self.assertEqual(code, cli.STOPPED)
        self.assertIn('STOPPED', out)
        self.assertIn('Collection is paused', out)

    def test_report_states_how_to_read_it_and_the_ratio(self):
        self.server.status = {
            'state': 'twin', 'ig_id': '1', 'baseline': {'minutes': 60, 'requests': 160, 'requests_per_hour': 160, 'list_pages_per_hour': 150},
            'history': [{'label': 'twin_same_total', 'cap_per_hour': 160, 'minutes': 61, 'requests': 160, 'requests_per_hour': 158, 'list_pages_per_hour': 148}],
            'current': {'label': 'plus_25_percent_1', 'cap_per_hour': 200, 'minutes': 30, 'requests': 90, 'requests_per_hour': 180, 'list_pages_per_hour': 170}}
        code, out, _ = self.run_cli('report')
        rows = json.loads(out)['rows']
        self.assertEqual([r['x_baseline'] for r in rows], [1.0, 0.99, 1.13])
        self.assertIn('zero adverse events', json.loads(out)['how_to_read'])

    def test_only_local_urls_are_accepted(self):
        for bad in ('https://127.0.0.1:8777', 'http://example.com:8777', 'http://127.0.0.1', 'http://127.0.0.1:8777/x'):
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                cli.main(['--base-url', bad, 'status'])


if __name__ == '__main__':
    unittest.main()
