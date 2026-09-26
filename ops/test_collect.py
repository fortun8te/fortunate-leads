"""CLI contract tests with an isolated HTTP server; no live collection is queued."""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CLI = Path(__file__).with_name('collect.py')
INSTALLED = Path('/Users/michael/.codex/skills/instagram-lead-scraper/scripts/scrape.py')


class FakeWorkspace(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/api/scraper':
            self.respond(self.server.scraper)
        elif self.path == '/api/public-bios':
            self.respond(self.server.bios)
        else:
            self.send_error(404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.posts.append((self.path, self.headers.get('Origin'), body))
        self.respond({'ok': True, 'queued': len(body['handles']) * len(body['directions'])})


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeWorkspace)
        self.server.scraper = {
            'ext': {'online': False, 'state': 'idle', 'cooldown_until': None},
            'paused': False, 'queue': {'list': 1, 'profile': 0},
            'lists': [{'seed': 'nasa', 'direction': 'followers', 'state': 'running', 'received': 30}],
        }
        self.server.bios = {'on': False, 'state': 'off', 'running': False, 'remaining': 12,
                            'etaSeconds': None, 'cooldowns': {}}
        self.server.posts = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def run_cli(self, *args, installed=False):
        return subprocess.run([sys.executable, str(INSTALLED if installed else CLI), '--base-url', self.base, *args],
                              capture_output=True, text=True, timeout=10)

    def test_queue_deduplicates_and_preserves_existing_active_list(self):
        result = self.run_cli('queue', '@NASA', 'HTTPS://M.INSTAGRAM.COM/%4eASA/?hl=en', 'example')
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['profiles'], ['nasa', 'example'])
        self.assertEqual(output['already_present'], 1)
        self.assertEqual(output['queued_lists'], 3)
        self.assertEqual([(body['handles'], body['directions'], body['refresh']) for _, _, body in self.server.posts],
                         [(['example'], ['followers'], False), (['nasa', 'example'], ['following'], False)])
        self.assertTrue(all(path == '/api/scraper/seeds' and origin == self.base for path, origin, _ in self.server.posts))

    def test_dry_run_and_file_do_not_post(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'profiles.txt'
            source.write_text('# batch\n@NASA\ninstagram.com/example\n@example\n')
            result = self.run_cli('queue', '--following', '--file', str(source), '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['to_submit'], {'following': ['nasa', 'example']})
        self.assertEqual(self.server.posts, [])

    def test_refresh_submits_existing_list_with_explicit_flag(self):
        result = self.run_cli('queue', '--followers', '--refresh', '@NASA')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.posts[0][2], {'handles': ['nasa'], 'directions': ['followers'], 'refresh': True})

    def test_unsupported_new_only_and_invalid_input_queue_nothing(self):
        for args in [('queue', '--new-only', '@nasa'), ('queue', 'https://evil.test/?next=instagram.com/nasa')]:
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.server.posts, [])

    def test_status_and_installed_entrypoint(self):
        result = self.run_cli('status', installed=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['list_states'], {'running': 1})
        self.assertEqual(output['lists_total'], 1)
        self.assertNotIn('lists', output)
        self.assertEqual(self.server.posts, [])

    def test_status_compacts_large_response_without_losing_counts_or_eta(self):
        self.server.scraper['lists'] = [
            {'seed': f'brand{i}', 'direction': 'followers', 'state':
             'running' if i < 2 else 'partial' if i < 7 else 'done', 'received': i}
            for i in range(100)]
        self.server.scraper['accounts'] = [
            {'lane_id': 'one', 'online': True, 'status': 'running'},
            {'lane_id': 'two', 'online': True, 'status': 'cooldown'},
            {'lane_id': 'three', 'online': False, 'status': 'offline'},
        ]
        self.server.scraper['progress'] = {'lists': {'left': 450, 'per_hour': 90, 'eta_h': 5}}
        result = self.run_cli('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['list_states'], {'done': 93, 'partial': 5, 'running': 2})
        self.assertEqual(output['accounts'], {'total': 3, 'online': 2,
                                              'states': {'cooldown': 1, 'offline': 1, 'running': 1}})
        self.assertEqual(output['list_progress'], {'left': 450, 'per_hour': 90, 'eta_h': 5})
        self.assertLess(len(result.stdout), 1100)
        self.assertNotIn('brand0', result.stdout)

        detailed = self.run_cli('status', '--details')
        self.assertEqual(detailed.returncode, 0, detailed.stderr)
        full = json.loads(detailed.stdout)
        self.assertEqual(len(full['lists']), 100)
        self.assertEqual(len(full['account_details']), 3)


if __name__ == '__main__':
    unittest.main()
