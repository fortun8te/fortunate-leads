"""Broad stage sidecar on 127.0.0.1:18742 (loopback only). Stdlib HTTP around sidecar/broad.py.

GET  /health  -> {"ok", "model": "broad", "deployment_version": head version, "device", "labels"}
POST /decide  {"items": [{"id", "person": {handle,name,category,bio,followers}, "rules": 0-100}]}
           -> {"model": "broad", "deployment_version", "results": [{"id", "p": 0-1}]}
The head file is re-read when it changes (after train_broad.py), so retraining needs no restart.
usage: sidecar/.venv/bin/python sidecar/broad_server.py [--port 18742]"""
import argparse
import json
import sys
import threading
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

warnings.filterwarnings('ignore')
sys.path.insert(0, str(Path(__file__).resolve().parent))
import broad  # noqa: E402

MAX_ITEMS = 512


def make_handler(model):
    lock = threading.Lock()   # one forward pass at a time on MPS
    state = {'mtime': broad.HEAD.stat().st_mtime if broad.HEAD.exists() else None}

    def fresh():
        m = broad.HEAD.stat().st_mtime if broad.HEAD.exists() else None
        if m != state['mtime']:
            model.reload()
            state['mtime'] = m

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _local(self):
            if self.client_address[0] not in ('127.0.0.1', '::1'):
                self._send(403, {'error': 'loopback only'})
                return False
            return True

        def do_GET(self):
            if not self._local():
                return
            if self.path.split('?')[0] != '/health':
                return self._send(404, {'error': 'not found'})
            with lock:
                fresh()
            h = model.head or {}
            self._send(200, {'ok': model.head is not None, 'model': 'broad', 'deployment_version': model.version,
                             'device': str(model.agent.device), 'labels': h.get('n'), 'top30': h.get('top30')})

        def do_POST(self):
            if not self._local():
                return
            if self.path.split('?')[0] != '/decide':
                return self._send(404, {'error': 'not found'})
            try:
                n = int(self.headers.get('Content-Length') or 0)
                items = json.loads(self.rfile.read(min(n, 20_000_000)))['items']
                if not isinstance(items, list) or not 0 < len(items) <= MAX_ITEMS:
                    raise ValueError('1-%d items' % MAX_ITEMS)
                people = [dict(it['person']) for it in items]
                rules = [float(it.get('rules') or 0) for it in items]
            except (ValueError, KeyError, TypeError) as e:
                return self._send(400, {'error': str(e)[:200]})
            with lock:
                fresh()
                if model.head is None:
                    return self._send(503, {'error': 'no trained head yet (run train_broad.py)'})
                ps = model.score(people, rules)
                version = model.version
            self._send(200, {'model': 'broad', 'deployment_version': version,
                             'results': [{'id': it['id'], 'p': round(p, 4)} for it, p in zip(items, ps)]})

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=18742)
    a = ap.parse_args()
    model = broad.Model()
    srv = ThreadingHTTPServer(('127.0.0.1', a.port), make_handler(model))
    print(f'broad sidecar on 127.0.0.1:{a.port} device={model.agent.device} head={model.version}', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
