"""Loopback tests for the independent transport, with synthetic temporary data."""

import http.client
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend_http import CompiledRouter, Handler, HttpApplication, SECURITY_HEADERS, Server, bind_handler
from map_view import Raw


class Bad(Exception):
    pass


class NotFound(Bad):
    pass


class Conflict(Exception):
    def __init__(self, current):
        self.current = current
        super().__init__('This record changed. Review the latest note before retrying.')


class Retryable(Exception):
    pass


class TrackedConnection(sqlite3.Connection):
    closed = 0

    def close(self):
        type(self).closed += 1
        super().close()


class HttpTransportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / 'synthetic.sqlite'
        self.web = self.root / 'web'
        self.web.mkdir()
        (self.web / 'index.html').write_text('Temporary test page')
        self.pictures = self.root / 'pfp'
        self.pictures.mkdir()
        connection = sqlite3.connect(self.database)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE people(id INTEGER PRIMARY KEY, pic_file TEXT)')
        connection.execute('CREATE TABLE writes(value TEXT)')
        connection.execute("INSERT INTO people VALUES(1,'1.jpg')")
        connection.commit()
        connection.close()
        self.extension = 'chrome-extension://test-only'
        self.logged = mock.Mock()
        self.app_options = {}
        self.routes = [
            ('GET', '/api/counts', lambda connection, query, body: {
                'people': connection.execute('SELECT COUNT(*) FROM people').fetchone()[0]}),
            ('GET', '/api/ext/next', lambda connection, query, body: {'job': None}),
            ('POST', '/api/ext/profile', lambda connection, query, body: {'received': body}),
            ('GET', '/api/benchmark/next', lambda connection, query, body: {'active': False}),
            ('POST', '/api/benchmark/permit', lambda connection, query, body: {'allowed': False}),
            ('POST', '/api/write', self.write),
        ]
        self.servers = []
        self.addCleanup(self.close_servers)

    def connect(self, path):
        connection = sqlite3.connect(path, timeout=0.05, factory=TrackedConnection)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def write(connection, query, body):
        connection.execute('INSERT INTO writes VALUES(?)', (body.get('value'),))
        connection.commit()
        return {'saved': True}

    def application(self, **options):
        return HttpApplication(config={'db': str(self.database), 'port': 0}, connect=self.connect,
                               routes=self.routes, bad=Bad, not_found=NotFound, conflict=Conflict,
                               raw_type=Raw, web_root=self.web, pictures_dir=self.pictures,
                               valid_picture=lambda data: data.startswith(b'\xff\xd8\xff'),
                               extension_origin=self.extension, font_path=self.root / 'missing.ttf',
                               log_error=self.logged, retryable_errors=(Retryable,),
                               **dict(self.app_options, **options))

    def start(self, *, workers=16, handler=Handler, application=None, **options):
        server = Server(('127.0.0.1', 0), handler, application=application or self.application(**options),
                        max_workers=workers)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.servers.append((server, thread))
        thread.start()
        return server

    def close_servers(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def request(self, server, path, body=None, *, method=None, headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        try:
            data = json.dumps(body).encode() if body is not None else None
            fields = dict(headers or {})
            method = method or ('POST' if body is not None else 'GET')
            connection.request(method, path, data, fields)
            response = connection.getresponse()
            return response.status, dict(response.headers), response.read()
        finally:
            connection.close()

    @staticmethod
    def eventually(predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_happy_route_and_committed_write_use_a_closed_temporary_connection(self):
        server = self.start()
        before = TrackedConnection.closed
        status, headers, data = self.request(server, '/api/counts')
        self.assertEqual((status, json.loads(data)), (200, {'people': 1}))
        self.assertEqual(TrackedConnection.closed, before + 1)
        for key, value in SECURITY_HEADERS.items():
            self.assertEqual(headers[key], value)
        port = server.server_address[1]
        status, _, data = self.request(server, '/api/write', {'value': 'preserved'},
                                       headers={'Origin': f'http://localhost:{port}'})
        self.assertEqual((status, json.loads(data)), (200, {'saved': True, 'ok': True}))
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute('SELECT value FROM writes').fetchone()[0], 'preserved')

    def test_route_regex_is_compiled_once_and_captures_keep_legacy_types(self):
        function = lambda *arguments: None
        with mock.patch('backend_http.re.compile', wraps=__import__('re').compile) as compile_pattern:
            router = CompiledRouter([('POST', r'/api/person/(\d+)/(?P<lane>[a-z0-9_-]+)', function)])
            for _ in range(3):
                self.assertEqual(router.resolve('POST', '/api/person/123/lane_a'), (function, [123, 'lane_a']))
            compile_pattern.assert_called_once()
        self.assertIsNone(router.resolve('GET', '/api/person/123/lane_a'))
        self.assertIsNone(router.resolve('POST', '/api/person/123/lane_a/more'))

    def test_origin_host_and_extension_header_contracts(self):
        server = self.start()
        port = server.server_address[1]
        cases = [
            ('/api/counts', None, {}, 200),
            ('/api/counts', None, {'Host': 'evil.example'}, 403),
            ('/api/counts', None, {'Origin': self.extension}, 403),
            ('/api/counts', None, {'Origin': f'http://localhost:{port}'}, 200),
            ('/api/counts', None, {'Origin': 'https://evil.example'}, 403),
            ('/api/ext/next', None, {}, 403),
            ('/api/ext/next', None, {'X-FL': '1'}, 200),
            ('/api/ext/next', None, {'X-FL': '0'}, 403),
            ('/api/ext/next', None, {'Origin': self.extension}, 200),
            ('/api/ext/next', None, {'Origin': f'http://localhost:{port}'}, 403),
            ('/api/ext/profile', {}, {'X-FL': '1'}, 200),
            ('/api/write', {}, {}, 403),
            ('/api/write', {}, {'Origin': 'null'}, 403),
            ('/api/benchmark/next', None, {}, 200),
            ('/api/benchmark/next', None, {'Origin': self.extension}, 200),
            ('/api/benchmark/permit', {}, {'Origin': self.extension}, 200),
            ('/api/benchmark/permit', {}, {'X-FL': '1'}, 403),
        ]
        for path, body, fields, expected in cases:
            with self.subTest(path=path, fields=fields):
                status, headers, _ = self.request(server, path, body, headers=fields)
                self.assertEqual(status, expected)
                self.assertEqual(headers.get('Access-Control-Allow-Origin'),
                                 self.extension if fields.get('Origin') == self.extension else None)
                for name, value in SECURITY_HEADERS.items():
                    self.assertEqual(headers.get(name), value)

    def test_preflight_retains_exact_extension_policy(self):
        server = self.start()
        status, headers, data = self.request(server, '/api/ext/profile', method='OPTIONS',
                                            headers={'Origin': self.extension})
        self.assertEqual((status, data), (204, b''))
        self.assertEqual(headers['Access-Control-Allow-Methods'], 'GET, POST')
        self.assertEqual(headers['Access-Control-Allow-Headers'], 'Content-Type, X-FL')
        self.assertEqual(self.request(server, '/api/ext/profile', method='OPTIONS')[0], 403)

    def test_raw_binary_and_conditional_map_responses_keep_headers_and_bytes(self):
        binary = b'\x00\xff\x01binary-map'
        self.routes += [('GET', '/api/map/universe/tile', lambda connection, query, body: Raw(
            binary, '"snapshot-1"', headers={'Content-Type': 'application/octet-stream',
                                            'Cache-Control': 'private, max-age=31536000, immutable'}))]
        server = self.start()
        status, headers, data = self.request(server, '/api/map/universe/tile')
        self.assertEqual((status, data), (200, binary))
        self.assertEqual(headers['Content-Type'], 'application/octet-stream')
        self.assertEqual(headers['ETag'], '"snapshot-1"')
        status, headers, data = self.request(server, '/api/map/universe/tile',
                                            headers={'If-None-Match': '"different", "snapshot-1"'})
        self.assertEqual((status, data), (304, b''))
        self.assertEqual(headers['ETag'], '"snapshot-1"')
        self.assertEqual(headers['Cache-Control'], 'private, max-age=31536000, immutable')

    def test_static_pictures_and_missing_font_remain_supported_and_confined(self):
        image = b'\xff\xd8\xffTemporary JPEG bytes'
        (self.pictures / '1.jpg').write_bytes(image)
        (self.pictures / '2.jpg').write_bytes(image)
        outside = self.root / 'private'
        outside.mkdir()
        (outside / 'index.html').write_text('private information')
        (self.web / 'escape').symlink_to(outside, target_is_directory=True)
        server = self.start()
        self.assertEqual(self.request(server, '/')[2], b'Temporary test page')
        status, headers, data = self.request(server, '/img/1')
        self.assertEqual((status, data), (200, image))
        self.assertEqual(headers['Content-Type'], 'image/jpeg')
        self.assertEqual(headers['Cache-Control'], 'no-cache')
        for path in ('/img/2', '/img/../private', '/../private/index.html', '/escape', '/api/local-font/areal'):
            with self.subTest(path=path):
                self.assertEqual(self.request(server, path)[0], 404)
        (self.pictures / '1.jpg').write_bytes(b'not a picture')
        self.assertEqual(self.request(server, '/img/1')[0], 404)

    def test_client_errors_conflict_and_unexpected_errors_are_distinct(self):
        errors = [(Bad('bad input'), 400), (ValueError('value'), 400), (OverflowError('overflow'), 400),
                  (NotFound('missing'), 404), (Conflict({'note': 'latest'}), 409),
                  (KeyError('internal key'), 500), (TypeError('internal type'), 500),
                  (sqlite3.OperationalError('no such table: broken'), 500), (Retryable('busy cache'), 503)]
        for index, (error, _) in enumerate(errors):
            def broken(connection, query, body, error=error):
                connection.execute("INSERT INTO writes VALUES('uncommitted')")
                raise error
            self.routes.append(('GET', '/api/failure/' + str(index), broken))
        server = self.start()
        for index, (_, expected) in enumerate(errors):
            status, headers, body = self.request(server, '/api/failure/' + str(index))
            with self.subTest(index=index):
                self.assertEqual(status, expected)
                self.assertFalse(json.loads(body)['ok'])
                if expected == 409:
                    self.assertEqual(json.loads(body)['current'], {'note': 'latest'})
                if expected == 500:
                    self.assertEqual(json.loads(body)['error'], 'Internal server error')
                if expected == 503:
                    self.assertEqual(headers['Retry-After'], '1')
        self.assertEqual(self.logged.call_count, 3)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM writes').fetchone()[0], 0)

    def test_locked_database_returns_short_retryable_response_and_rolls_back(self):
        server = self.start(sqlite_busy_seconds=0.08)
        writer = sqlite3.connect(self.database)
        self.addCleanup(writer.close)
        writer.execute('BEGIN IMMEDIATE')
        before = time.monotonic()
        status, headers, body = self.request(server, '/api/write', {'value': 'not written'},
                                            headers={'Origin': f'http://127.0.0.1:{server.server_address[1]}'})
        elapsed = time.monotonic() - before
        self.assertEqual(status, 503)
        self.assertEqual(headers['Retry-After'], '1')
        self.assertFalse(json.loads(body)['ok'])
        self.assertLess(elapsed, 0.8)
        writer.rollback()
        self.assertEqual(writer.execute('SELECT COUNT(*) FROM writes').fetchone()[0], 0)
        self.assertEqual(self.request(server, '/api/counts')[0], 200)

    def test_body_deadline_returns_408_releases_slot_and_does_not_write(self):
        server = self.start(workers=1, body_timeout=0.12)
        port = server.server_address[1]
        slow = socket.create_connection(server.server_address, timeout=2)
        self.addCleanup(slow.close)
        slow.sendall((f'POST /api/write HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
                      f'Origin: http://127.0.0.1:{port}\r\nContent-Length: 40\r\n\r\n{{').encode())
        before = time.monotonic()
        response = slow.recv(4096)
        self.assertIn(b' 408 ', response.splitlines()[0])
        self.assertLess(time.monotonic() - before, 0.8)
        self.assertTrue(self.eventually(lambda: server.active_requests == 0))
        self.assertEqual(self.request(server, '/api/counts')[0], 200)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM writes').fetchone()[0], 0)

    def test_trickling_headers_have_a_total_deadline_and_release_capacity(self):
        server = self.start(workers=1, read_timeout=0.16)
        slow = socket.create_connection(server.server_address, timeout=2)
        self.addCleanup(slow.close)
        slow.sendall(b'GET /api/counts HTTP/1.1\r\nHost: ')
        self.assertTrue(self.eventually(lambda: server.active_requests == 1))
        before = time.monotonic()
        for _ in range(6):
            time.sleep(0.045)
            try:
                slow.sendall(b'x')
            except OSError:
                break
        self.assertTrue(self.eventually(lambda: server.active_requests == 0, timeout=0.35))
        self.assertLess(time.monotonic() - before, 0.65)
        self.assertEqual(self.request(server, '/api/counts')[0], 200)

    def test_overload_rejects_without_new_threads_or_queued_work_and_recovers(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        entered = threading.Event()
        count_lock = threading.Lock()
        arrived = 0

        def hold(connection, query, body):
            nonlocal arrived
            with count_lock:
                arrived += 1
                if arrived == 2:
                    entered.set()
            gate.wait(timeout=3)
            return {'ready': True}

        self.routes.append(('GET', '/api/hold', hold))
        server = self.start(workers=2)
        held = []
        for _ in range(2):
            connection = http.client.HTTPConnection(*server.server_address, timeout=3)
            self.addCleanup(connection.close)
            connection.request('GET', '/api/hold')
            held.append(connection)
        self.assertTrue(entered.wait(timeout=1))
        before = time.monotonic()
        for _ in range(24):
            status, headers, data = self.request(server, '/api/counts')
            self.assertEqual(status, 503)
            self.assertEqual(headers['Retry-After'], '1')
            self.assertFalse(json.loads(data)['ok'])
            self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertLess(time.monotonic() - before, 1.0)
        self.assertEqual(server.active_requests, 2)
        self.assertEqual(server.peak_active_requests, 2)
        self.assertEqual(server.rejected_requests, 24)
        gate.set()
        for connection in held:
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
        self.assertTrue(self.eventually(lambda: server.active_requests == 0))
        self.assertEqual(self.request(server, '/api/counts')[0], 200)

    def test_overload_post_body_observes_503_without_connection_reset(self):
        entered = threading.Event()
        gate = threading.Event()
        self.addCleanup(gate.set)

        def hold(connection, query, body):
            entered.set()
            gate.wait(timeout=3)
            return {'ready': True}

        self.routes.append(('GET', '/api/hold', hold))
        server = self.start(workers=1)
        held = http.client.HTTPConnection(*server.server_address, timeout=3)
        self.addCleanup(held.close)
        held.request('GET', '/api/hold')
        self.assertTrue(entered.wait(timeout=1))
        for payload in ('small', 'x' * 32_768, 'x' * 1024 * 1024, 'x' * 8 * 1024 * 1024):
            with self.subTest(body_length=len(payload)):
                status, headers, body = self.request(server, '/api/write', {'value': payload},
                    headers={'Origin': f'http://127.0.0.1:{server.server_address[1]}'})
                self.assertEqual(status, 503)
                self.assertEqual(headers['Retry-After'], '1')
                self.assertFalse(json.loads(body)['ok'])
        gate.set()
        response = held.getresponse()
        response.read()

    def test_aggregate_body_budget_is_finite_and_released_after_timeout(self):
        server = self.start(workers=3, body_budget_bytes=8, body_timeout=0.15)
        port = server.server_address[1]
        slow = socket.create_connection(server.server_address, timeout=2)
        self.addCleanup(slow.close)
        slow.sendall((f'POST /api/write HTTP/1.1\r\nHost: localhost:{port}\r\n'
                      f'Origin: http://localhost:{port}\r\nContent-Length: 7\r\n\r\n{{').encode())
        self.assertTrue(self.eventually(lambda: server.application._body_bytes == 7))
        status, headers, data = self.request(server, '/api/write', {},
                                             headers={'Origin': f'http://localhost:{port}'})
        self.assertEqual(status, 503)
        self.assertEqual(headers['Retry-After'], '1')
        self.assertFalse(json.loads(data)['ok'])
        self.assertIn(b' 408 ', slow.recv(4096).splitlines()[0])
        self.assertTrue(self.eventually(lambda: server.application._body_bytes == 0))
        self.assertEqual(self.request(server, '/api/write', {},
                                    headers={'Origin': f'http://localhost:{port}'})[0], 200)

    def test_rejection_lane_itself_is_bounded_and_never_blocks_acceptance(self):
        entered = threading.Event()
        gate = threading.Event()
        self.addCleanup(gate.set)

        def hold(connection, query, body):
            entered.set()
            gate.wait(timeout=3)
            return {}

        self.routes.append(('GET', '/api/hold', hold))
        server = self.start(workers=1)
        held = http.client.HTTPConnection(*server.server_address, timeout=3)
        self.addCleanup(held.close)
        held.request('GET', '/api/hold')
        self.assertTrue(entered.wait(timeout=1))
        slow_rejections = []
        for index in range(4):
            peer = socket.create_connection(server.server_address, timeout=2)
            self.addCleanup(peer.close)
            slow_rejections.append(peer)
            self.assertTrue(self.eventually(lambda: server.active_rejections == index + 1))
        before = time.monotonic()
        self.assertEqual(self.request(server, '/api/counts')[0], 503)
        self.assertLess(time.monotonic() - before, 0.2)
        self.assertLessEqual(server.active_rejections, 4)
        self.assertEqual(server.active_requests, 1)
        self.assertTrue(self.eventually(lambda: server.active_rejections == 0))
        gate.set()
        response = held.getresponse()
        response.read()

    def test_invalid_body_length_and_non_object_json_do_not_execute_endpoint(self):
        called = mock.Mock(return_value={'done': True})
        self.routes.append(('POST', '/api/probe', called))
        server = self.start(max_body_bytes=32)
        port = server.server_address[1]
        for length, body in [('-1', b''), ('33', b''), ('bad', b''), ('2', b'[]'), ('3', b'"x"')]:
            with self.subTest(length=length, body=body):
                connection = socket.create_connection(server.server_address, timeout=2)
                try:
                    connection.sendall((f'POST /api/probe HTTP/1.1\r\nHost: localhost:{port}\r\n'
                                        f'Origin: http://localhost:{port}\r\nContent-Length: {length}\r\n\r\n').encode() + body)
                    response = connection.recv(4096)
                    self.assertIn(b' 400 ', response.splitlines()[0])
                finally:
                    connection.close()
        called.assert_not_called()

    def test_server_close_interrupts_partial_socket_reads(self):
        server = self.start(read_timeout=20)
        slow = socket.create_connection(server.server_address, timeout=2)
        self.addCleanup(slow.close)
        slow.sendall(b'GET /api/counts HTTP/1.1\r\nHost:')
        self.assertTrue(self.eventually(lambda: server.active_requests == 1))
        server.shutdown()
        before = time.monotonic()
        server.server_close()
        self.assertLess(time.monotonic() - before, 0.5)
        self.assertEqual(server.active_requests, 0)

    def test_shutdown_before_serving_returns_and_prevents_future_startup(self):
        server = Server(('127.0.0.1', 0), Handler, application=self.application())
        self.addCleanup(server.server_close)
        finished = threading.Event()
        thread = threading.Thread(target=lambda: (server.shutdown(), finished.set()), daemon=True)
        thread.start()
        self.assertTrue(finished.wait(timeout=0.3))
        thread.join(timeout=0.3)
        before = time.monotonic()
        server.serve_forever()
        self.assertLess(time.monotonic() - before, 0.2)
        self.assertTrue(server._serve_done.is_set())

    def test_close_before_serving_and_repeated_shutdown_are_safe(self):
        server = Server(('127.0.0.1', 0), Handler, application=self.application())
        server.server_close()
        server.shutdown()
        server.server_close()
        server.serve_forever()
        self.assertEqual(server.active_requests, 0)

    def test_shutdown_from_serve_loop_does_not_wait_on_itself(self):
        server = Server(('127.0.0.1', 0), Handler, application=self.application())
        self.addCleanup(server.server_close)
        server.service_actions = server.shutdown
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        thread.join(timeout=0.5)
        self.assertFalse(thread.is_alive())

    def test_close_between_marking_serve_running_and_selector_registration_is_clean(self):
        server = Server(('127.0.0.1', 0), Handler, application=self.application())
        self.addCleanup(server.server_close)
        ready = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        failures = []
        selector_type = __import__('selectors').DefaultSelector

        def delayed_selector():
            ready.set()
            release.wait(timeout=1)
            return selector_type()

        def run():
            try:
                server.serve_forever()
            except Exception as error:
                failures.append(error)

        with mock.patch('backend_http.selectors.DefaultSelector', side_effect=delayed_selector):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            self.assertTrue(ready.wait(timeout=0.5))
            self.assertIsNotNone(server._serve_thread)
            server.server_close()
            release.set()
            thread.join(timeout=0.5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(server._serve_done.is_set())
        server.shutdown()

    def test_bound_legacy_handler_constructs_dependencies_once_per_request(self):
        factory = mock.Mock(side_effect=self.application)
        server = ThreadingHTTPServer(('127.0.0.1', 0), bind_handler(factory))
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.servers.append((server, thread))
        thread.start()
        self.assertEqual(self.request(server, '/api/counts')[0], 200)
        self.assertEqual(factory.call_count, 1)
        self.routes.append(('GET', '/api/new', lambda connection, query, body: {'new': True}))
        self.assertEqual(self.request(server, '/api/new')[0], 200)
        self.assertEqual(factory.call_count, 2)

    def test_bound_handler_uses_shared_server_application_body_budget(self):
        factory = mock.Mock(side_effect=AssertionError('Factory must not replace the supplied application'))
        shared = self.application(body_budget_bytes=8, body_timeout=0.15)
        server = self.start(handler=bind_handler(factory), application=shared, workers=3)
        port = server.server_address[1]
        slow = socket.create_connection(server.server_address, timeout=2)
        self.addCleanup(slow.close)
        slow.sendall((f'POST /api/write HTTP/1.1\r\nHost: localhost:{port}\r\n'
                      f'Origin: http://localhost:{port}\r\nContent-Length: 7\r\n\r\n{{').encode())
        self.assertTrue(self.eventually(lambda: shared._body_bytes == 7))
        status, headers, body = self.request(server, '/api/write', {},
                                             headers={'Origin': f'http://localhost:{port}'})
        self.assertEqual(status, 503)
        self.assertEqual(headers['Retry-After'], '1')
        self.assertFalse(json.loads(body)['ok'])
        self.assertIn(b' 408 ', slow.recv(4096).splitlines()[0])
        self.assertTrue(self.eventually(lambda: shared._body_bytes == 0))
        factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
