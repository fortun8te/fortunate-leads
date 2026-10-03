"""Bounded local HTTP transport with explicit application dependencies.

This module never imports the business API. Production owns one HttpApplication;
the compatibility entry point may bind an application factory for older callers.
"""

import io
import json
import mimetypes
import re
import selectors
import socket
import sqlite3
import threading
import time
import traceback
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


SECURITY_HEADERS = {
    'X-Frame-Options': 'DENY',
    'Content-Security-Policy': "frame-ancestors 'none'",
    'X-Content-Type-Options': 'nosniff',
}


class _RequestBusy(Exception):
    pass


class CompiledRouter:
    """Compile the established route expressions once per application."""

    def __init__(self, routes):
        by_method = {}
        for method, expression, function in routes:
            by_method.setdefault(method, []).append((re.compile(expression), function))
        self.by_method = {method: tuple(rows) for method, rows in by_method.items()}

    def resolve(self, method, path):
        for expression, function in self.by_method.get(method, ()):
            match = expression.fullmatch(path)
            if match:
                named = set(expression.groupindex.values())
                arguments = [value if index in named else int(value)
                             for index, value in enumerate(match.groups(), 1)]
                return function, arguments
        return None


class HttpApplication:
    """Dependencies used by every request, supplied by the composition layer.

    connect accepts the configured database path and returns a new connection.
    sqlite_busy_seconds applies to every request connection before its first
    endpoint query. Supply a short-timeout connector too, so opening a database
    cannot itself wait behind another writer for the legacy fifteen seconds.
    """

    def __init__(self, *, config, connect, routes, bad, not_found, conflict,
                 raw_type, web_root, pictures_dir, valid_picture,
                 extension_origin, font_path=None, log_error=None,
                 read_timeout=5.0, body_timeout=10.0, write_timeout=5.0,
                 sqlite_busy_seconds=0.25, max_body_bytes=64 * 1024 * 1024,
                 retryable_errors=(), body_budget_bytes=128 * 1024 * 1024):
        if min(read_timeout, body_timeout, write_timeout) <= 0:
            raise ValueError('HTTP timeouts must be positive')
        if sqlite_busy_seconds < 0 or max_body_bytes < 0 or body_budget_bytes < 0:
            raise ValueError('HTTP database/body limits must be nonnegative')
        self.config = config
        self.connect = connect
        self.router = CompiledRouter(routes)
        self.bad = bad
        self.not_found = not_found
        self.conflict = conflict
        self.raw_type = raw_type
        self.web_root = Path(web_root).resolve()
        self.pictures_dir = pictures_dir
        self.valid_picture = valid_picture
        self.extension_origin = extension_origin
        self.font_path = Path(font_path) if font_path is not None else (
            Path.home() / 'Library' / 'Fonts' / 'ABCArealSuperfamilyVariable.ttf')
        self.log_error = log_error if log_error is not None else traceback.print_exc
        self.read_timeout = float(read_timeout)
        self.body_timeout = float(body_timeout)
        self.write_timeout = float(write_timeout)
        self.sqlite_busy_seconds = float(sqlite_busy_seconds)
        self.max_body_bytes = int(max_body_bytes)
        self.retryable_errors = tuple(retryable_errors)
        self.body_budget_bytes = int(body_budget_bytes)
        self._body_lock = threading.Lock()
        self._body_bytes = 0

    @contextmanager
    def body_allocation(self, length):
        with self._body_lock:
            if self._body_bytes + length > self.body_budget_bytes:
                raise _RequestBusy('Request body capacity is full')
            self._body_bytes += length
        try:
            yield
        finally:
            with self._body_lock:
                self._body_bytes -= length

    @contextmanager
    def connection(self):
        connection = self.connect(self.config['db'])
        try:
            connection.execute('PRAGMA busy_timeout=' + str(int(self.sqlite_busy_seconds * 1000)))
            yield connection
        finally:
            # Connection.close rolls back unfinished work. Domain functions
            # retain the established responsibility for explicit commits.
            connection.close()


class _DeadlineReader(io.RawIOBase):
    """Refresh the remaining total read budget before every socket read.

    Socket inactivity timeouts alone let a trickling peer occupy a worker
    forever. BufferedReader calls readinto repeatedly, so the deadline also
    bounds a header line or body received one byte at a time.
    """

    def __init__(self, connection):
        super().__init__()
        self.connection = connection
        self.stream = connection.makefile('rb', buffering=0)
        self.deadline = None

    def readable(self):
        return True

    def set_deadline(self, seconds):
        self.deadline = time.monotonic() + seconds

    def readinto(self, buffer):
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Request read timed out')
            self.connection.settimeout(remaining)
        return self.stream.readinto(buffer)

    def close(self):
        if not self.closed:
            self.stream.close()
        super().close()


def _sqlite_busy(error):
    code = getattr(error, 'sqlite_errorcode', None)
    if code is not None:
        return code & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    message = str(error).lower()
    return message in ('database is locked', 'database is busy') or message.startswith(
        ('database table is locked', 'database schema is locked'))


class Handler(BaseHTTPRequestHandler):
    """Request handler using only its injected HttpApplication."""

    @property
    def application(self):
        return self.server.application

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.rfile.close()
        self._reader = _DeadlineReader(self.connection)
        self.rfile = io.BufferedReader(self._reader)
        self._reader.set_deadline(self.application.read_timeout)

    def handle_one_request(self):
        self._reader.set_deadline(self.application.read_timeout)
        try:
            return super().handle_one_request()
        except (ConnectionError, OSError):
            self.close_connection = True

    def do_GET(self):
        self.route('GET')

    def do_POST(self):
        self.route('POST')

    def do_OPTIONS(self):
        self._drained = False
        if self.headers.get('Origin') != self.application.extension_origin:
            return self.send(403, b'', 'text/plain')
        self.send(204, b'', 'text/plain', {
            'Access-Control-Allow-Methods': 'GET, POST',
            'Access-Control-Allow-Headers': 'Content-Type, X-FL',
        })

    def _content_length(self):
        values = self.headers.get_all('Content-Length', [])
        if len(values) > 1 and len(set(values)) != 1:
            raise self.application.bad('bad Content-Length')
        if self.headers.get('Transfer-Encoding'):
            raise self.application.bad('unsupported Transfer-Encoding')
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            raise self.application.bad('bad Content-Length') from None
        if length < 0 or length > self.application.max_body_bytes:
            raise self.application.bad('bad Content-Length')
        return length

    def drain(self):
        """Drain early-response bodies with a finite total read deadline."""
        if getattr(self, '_drained', False):
            return
        self._drained = True
        try:
            length = self._content_length()
        except self.application.bad:
            self.close_connection = True
            return
        if length:
            self._reader.set_deadline(self.application.body_timeout)
            try:
                # Drain in fixed chunks instead of allocating a second large
                # JSON buffer for requests rejected before dispatch.
                while length:
                    chunk = self.rfile.read(min(length, 64 * 1024))
                    if not chunk:
                        self.close_connection = True
                        break
                    length -= len(chunk)
            except (TimeoutError, OSError):
                self.close_connection = True

    def send(self, code, body, ctype='application/json', headers=None):
        self.drain()
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.connection.settimeout(self.application.write_timeout)
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        if self.headers.get('Origin') == self.application.extension_origin:
            self.send_header('Access-Control-Allow-Origin', self.application.extension_origin)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def route(self, method):
        self._drained = False
        app = self.application
        url = urlparse(self.path)
        port = app.config['port'] or self.server.server_address[1]
        origin = self.headers.get('Origin')
        if (len(self.headers.get_all('Host', [])) != 1 or
                self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}')):
            return self.send(403, {'ok': False, 'error': 'host'})
        if len(self.headers.get_all('Origin', [])) > 1:
            return self.send(403, {'ok': False, 'error': 'origin'})
        if url.path.startswith('/api/ext/') or (url.path.startswith('/api/benchmark/') and origin == app.extension_origin):
            if origin != app.extension_origin and not (origin is None and self.headers.get('X-FL') == '1'):
                return self.send(403, {'ok': False, 'error': 'origin'})
        elif origin and origin not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            return self.send(403, {'ok': False, 'error': 'origin'})
        elif method == 'POST' and not origin:
            return self.send(403, {'ok': False, 'error': 'origin required'})
        try:
            found = app.router.resolve(method, url.path)
            if found:
                function, arguments = found
                return self.api(function, parse_qs(url.query), arguments, method == 'POST' or '/ext/' in url.path)
            if method == 'GET' and url.path.startswith('/img/'):
                return self.image(url.path[5:])
            if method == 'GET' and url.path == '/api/local-font/areal':
                return self.local_font()
            if method == 'GET' and not url.path.startswith('/api/'):
                return self.static(url.path)
            return self.send(404, {'ok': False, 'error': 'not found'})
        except Exception as error:
            return self.send_error_response(error)

    def send_error_response(self, error):
        app = self.application
        if isinstance(error, app.conflict):
            return self.send(409, dict(error.current, ok=False, error=str(error), current=error.current))
        if isinstance(error, app.not_found):
            return self.send(404, {'ok': False, 'error': str(error)})
        if isinstance(error, (app.bad, ValueError, OverflowError)):
            return self.send(400, {'ok': False, 'error': str(error)})
        if isinstance(error, (TimeoutError, socket.timeout)):
            self._drained = True
            self.close_connection = True
            return self.send(408, {'ok': False, 'error': 'Request read timed out'})
        if isinstance(error, sqlite3.OperationalError) and _sqlite_busy(error):
            return self.send(503, {'ok': False, 'error': 'Database busy; retry shortly'}, headers={'Retry-After': '1'})
        if isinstance(error, (_RequestBusy, *app.retryable_errors)):
            return self.send(503, {'ok': False, 'error': 'Server busy; retry shortly'}, headers={'Retry-After': '1'})
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            self.close_connection = True
            return
        app.log_error()
        return self.send(500, {'ok': False, 'error': 'Internal server error'})

    def api(self, function, query, arguments, with_ok):
        app = self.application
        length = self._content_length()
        with app.body_allocation(length):
            self._drained = True
            self._reader.set_deadline(app.body_timeout)
            data = self.rfile.read(length) if length else b''
            if len(data) != length:
                raise app.bad('Incomplete request body')
            body = json.loads(data or b'{}')
            del data
            if not isinstance(body, dict):
                raise app.bad('body must be a JSON object')
            with app.connection() as connection:
                output = function(connection, query, body, *arguments)
            if isinstance(output, app.raw_type):
                if output.etag and output.etag in [value.strip() for value in self.headers.get('If-None-Match', '').split(',')]:
                    return self.send(304, b'', headers=output.headers)
                return self.send(output.status, output.body, ctype=output.headers.get('Content-Type', 'application/json'),
                                 headers={name: value for name, value in output.headers.items() if name.lower() != 'content-type'})
            self.send(200, dict(output, ok=True) if with_ok else output)

    def image(self, person_id):
        app = self.application
        if not re.fullmatch(r'[0-9]+', person_id):
            return self.send(404, {'ok': False, 'error': 'no image'})
        with app.connection() as connection:
            owned = connection.execute('SELECT 1 FROM people WHERE id=? AND pic_file=?',
                                       (person_id, f'{person_id}.jpg')).fetchone()
        if not owned:
            return self.send(404, {'ok': False, 'error': 'no image'})
        directory = app.pictures_dir() if callable(app.pictures_dir) else app.pictures_dir
        picture = Path(directory) / f'{person_id}.jpg'
        if not picture.is_file():
            return self.send(404, {'ok': False, 'error': 'no image'})
        data = picture.read_bytes()
        if not app.valid_picture(data):
            return self.send(404, {'ok': False, 'error': 'no image'})
        content_type = 'image/png' if data[:4] == b'\x89PNG' else 'image/webp' if data[8:12] == b'WEBP' else 'image/jpeg'
        self.send(200, data, content_type, {'Cache-Control': 'no-cache'})

    def local_font(self):
        font = self.application.font_path
        if not font.is_file():
            return self.send(404, b'Font not installed', 'text/plain')
        self.send(200, font.read_bytes(), 'font/ttf', {'Cache-Control': 'private, max-age=86400'})

    def static(self, path):
        root = self.application.web_root
        asset = (root / (path.lstrip('/') or 'index.html')).resolve()
        if asset.is_dir():
            asset = (asset / 'index.html').resolve()
        if not asset.is_relative_to(root) or not asset.is_file():
            return self.send(404, b'not found', 'text/plain')
        self.send(200, asset.read_bytes(), mimetypes.guess_type(asset.name)[0] or 'application/octet-stream',
                  {'Cache-Control': 'no-cache'})


def bind_handler(application_factory):
    """Adapt old HTTPServer callers without making transport depend on globals.

    The factory belongs to the compatibility composition layer. Production
    Handler receives the already constructed server.application instead.
    """
    class BoundHandler(Handler):
        @property
        def application(self):
            provided = getattr(self.server, 'application', None)
            if provided is not None:
                return provided
            if not hasattr(self, '_application'):
                self._application = application_factory()
            return self._application

    return BoundHandler


class Server(HTTPServer):
    """A finite number of handlers, with immediate overload rejection.

    No unbounded ThreadingMixIn or executor queue is used. Admission happens
    before creating a thread. Closing interrupts socket reads and waits only
    for the configured shutdown budget; worker threads are daemon threads.
    """

    request_queue_size = 256
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass=Handler, bind_and_activate=True,
                 *, application=None, max_workers=32, shutdown_timeout=2.0,
                 max_rejection_workers=4, rejection_timeout=0.25):
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError('max_workers must be a positive integer')
        if shutdown_timeout < 0:
            raise ValueError('shutdown_timeout must be nonnegative')
        if (isinstance(max_rejection_workers, bool) or not isinstance(max_rejection_workers, int) or
                max_rejection_workers < 1 or rejection_timeout <= 0):
            raise ValueError('Rejection worker count/timeout must be positive')
        self.application = application
        self.max_workers = max_workers
        self.shutdown_timeout = float(shutdown_timeout)
        self._slots = threading.BoundedSemaphore(max_workers)
        self._worker_lock = threading.Lock()
        self._workers = {}
        self._rejections = {}
        self._rejection_slots = threading.BoundedSemaphore(max_rejection_workers)
        self.rejection_timeout = float(rejection_timeout)
        self._closing = False
        self._serve_lock = threading.Lock()
        self._serve_stop = threading.Event()
        self._serve_done = threading.Event()
        self._serve_done.set()
        self._serve_thread = None
        self.peak_active_requests = 0
        self.rejected_requests = 0
        super().__init__(server_address, RequestHandlerClass, bind_and_activate)

    def serve_forever(self, poll_interval=0.5):
        """Own the serve lifecycle so stopping before startup cannot deadlock.

        socketserver.shutdown waits forever when its serve loop never began.
        Our stop event is permanent: shutdown before the loop is a completed
        stop, and a future startup observes that stop instead of reopening.
        """
        with self._serve_lock:
            if self._serve_stop.is_set():
                return
            if self._serve_thread is not None:
                raise RuntimeError('HTTP server is already serving')
            self._serve_thread = threading.get_ident()
            self._serve_done.clear()
        try:
            with selectors.DefaultSelector() as selector:
                with self._serve_lock:
                    if self._serve_stop.is_set():
                        return
                    selector.register(self, selectors.EVENT_READ)
                while not self._serve_stop.is_set():
                    try:
                        ready = selector.select(max(0.001, min(float(poll_interval), 0.1)))
                    except (OSError, ValueError):
                        if self._serve_stop.is_set():
                            break
                        raise
                    if self._serve_stop.is_set():
                        break
                    if ready:
                        self._handle_request_noblock()
                    self.service_actions()
        finally:
            with self._serve_lock:
                self._serve_thread = None
                self._serve_done.set()

    def shutdown(self):
        self._serve_stop.set()
        with self._serve_lock:
            serving_thread = self._serve_thread
        if serving_thread is not None and serving_thread != threading.get_ident():
            self._serve_done.wait()

    @property
    def active_requests(self):
        with self._worker_lock:
            return len(self._workers)

    @property
    def active_rejections(self):
        with self._worker_lock:
            return len(self._rejections)

    def process_request(self, request, client_address):
        if self._closing or not self._slots.acquire(blocking=False):
            self._reject(request)
            return
        thread = threading.Thread(target=self._process_request, args=(request, client_address),
                                  name='fortunate-http', daemon=True)
        with self._worker_lock:
            closing = self._closing
            if not closing:
                self._workers[thread] = request
                self.peak_active_requests = max(self.peak_active_requests, len(self._workers))
        if closing:
            self._slots.release()
            self._reject(request)
            return
        try:
            thread.start()
        except Exception:
            with self._worker_lock:
                self._workers.pop(thread, None)
            self._slots.release()
            self.shutdown_request(request)
            raise

    def _process_request(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            with self._worker_lock:
                self._workers.pop(threading.current_thread(), None)
            self._slots.release()

    def _reject(self, request):
        with self._worker_lock:
            self.rejected_requests += 1
        if self._closing or not self._rejection_slots.acquire(blocking=False):
            return self._reject_now(request)
        thread = threading.Thread(target=self._drain_rejected_request, args=(request,),
                                  name='fortunate-http-rejection', daemon=True)
        with self._worker_lock:
            self._rejections[thread] = request
        try:
            thread.start()
        except Exception:
            with self._worker_lock:
                self._rejections.pop(thread, None)
            self._rejection_slots.release()
            self._reject_now(request)

    def _drain_rejected_request(self, request):
        """Drain an ordinary upload before closing, outside the accept loop.

        Closing with unread POST bytes causes a TCP reset on macOS, hiding the
        503 from clients. At most four rejection workers spend 250ms draining
        finite uploads; further overload receives a best-effort immediate reply.
        """
        deadline = time.monotonic() + self.rejection_timeout
        try:
            data = bytearray()
            while b'\r\n\r\n' not in data and len(data) < 64 * 1024:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                request.settimeout(remaining)
                chunk = request.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
            if b'\r\n\r\n' in data:
                header, body = bytes(data).split(b'\r\n\r\n', 1)
                lengths = [line.partition(b':')[2].strip() for line in header.split(b'\r\n')[1:]
                           if line.partition(b':')[0].lower() == b'content-length']
                try:
                    length = int(lengths[0]) if lengths and len(set(lengths)) == 1 else 0
                except ValueError:
                    length = 0
                maximum = self.application.max_body_bytes if self.application is not None else 64 * 1024 * 1024
                if 0 < length <= maximum:
                    unread = max(0, length - len(body))
                    while unread:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        request.settimeout(remaining)
                        chunk = request.recv(min(unread, 64 * 1024))
                        if not chunk:
                            break
                        unread -= len(chunk)
        except OSError:
            pass
        finally:
            self._reject_now(request)
            with self._worker_lock:
                self._rejections.pop(threading.current_thread(), None)
            self._rejection_slots.release()

    def _reject_now(self, request):
        body = b'{"ok": false, "error": "Server busy; retry shortly"}'
        fields = {'Content-Type': 'application/json', 'Content-Length': str(len(body)),
                  'Retry-After': '1', 'Connection': 'close', **SECURITY_HEADERS}
        if self.application is not None:
            fields['Access-Control-Allow-Origin'] = self.application.extension_origin
        response = ('HTTP/1.0 503 Service Unavailable\r\n' + ''.join(
            f'{name}: {value}\r\n' for name, value in fields.items()) + '\r\n').encode() + body
        try:
            # A small local response usually fits in one send. If the peer is
            # not accepting data, release it without blocking the accept loop.
            request.setblocking(False)
            request.send(response)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)

    def server_close(self):
        self._closing = True
        self._serve_stop.set()
        with self._serve_lock:
            super().server_close()
        with self._worker_lock:
            workers = tuple(self._workers.items()) + tuple(self._rejections.items())
        for _, request in workers:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        deadline = time.monotonic() + self.shutdown_timeout
        for thread, _ in workers:
            if thread is not threading.current_thread() and thread.ident is not None:
                thread.join(max(0, deadline - time.monotonic()))
