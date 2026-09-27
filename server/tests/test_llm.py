import os; os.environ.setdefault('FL_NO_ORSLOT', '1')  # tests never see the real key pool
"""Provider layer (llm.py) and Laya client (laya.py) against fake HTTP stubs."""
import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import os
import sqlite3
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import laya  # noqa: E402
import llm  # noqa: E402


class Stub(BaseHTTPRequestHandler):
    """Behaviour per Authorization key (or 'proxy' without one): a list of (status, headers, body) consumed in order."""
    plan = {}
    seen = []
    usage = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        who = (self.headers.get('Authorization') or 'proxy').replace('Bearer ', '')
        Stub.seen.append((who, body['model'], self.headers.get('X-Title'), body.get('response_format')))
        steps = Stub.plan.get(who) or [(200, {}, None)]
        status, headers, content = steps.pop(0) if len(steps) > 1 else steps[0]
        data = json.dumps({'model': body['model'], 'choices': [{'message': {'content': content or '{"ok": true}'}}],
                           'usage': Stub.usage}).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}/api/v1/chat/completions'
        self.old = llm.OPENROUTER
        llm.OPENROUTER = self.url   # keys go to the same stub
        Stub.plan, Stub.seen, Stub.usage = {}, [], None

    def tearDown(self):
        llm.OPENROUTER = self.old
        self.httpd.shutdown()
        self.httpd.server_close()

    def prov(self, keys=('sk-or-aaaa1111', 'sk-or-bbbb2222'), proxy='http://127.0.0.1:9/down'):
        return llm.Providers(list(keys), ('m/a:free', 'm/b:free'), proxy)

    def test_proxy_first_then_keys_with_headers_and_json_mode(self):
        p = self.prov(proxy=self.url)
        self.assertEqual(p.chat([{'role': 'user', 'content': 'x'}]), ('{"ok": true}', 'm/a:free'))
        who, model, title, fmt = Stub.seen[0]
        self.assertEqual((who, model, title, fmt), ('proxy', 'm/a:free', 'Fortunate Leads', {'type': 'json_object'}))

    def test_durable_usage_counts_retries_batches_and_only_reported_tokens(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'usage.sqlite'
            key = 'sk-or-aaaa1111'
            Stub.plan = {'proxy': [(503, {}, None)], key: [(200, {}, None)]}
            Stub.usage = {'prompt_tokens': 123, 'completion_tokens': 45}
            p = llm.Providers([key], ('m/a:free',), proxy=self.url, usage_path=path)
            self.assertEqual(p.chat([{'role': 'user', 'content': 'private prompt'}], batch_size=8),
                             ('{"ok": true}', 'm/a:free'))
            report = llm.usage_ledger.summary(path)
            self.assertEqual((report['requests'], report['successes'], report['failed_attempts']), (2, 1, 1))
            self.assertEqual((report['items_attempted'], report['input_tokens_reported'],
                              report['output_tokens_reported'], report['token_reports']), (16, 123, 45, 1))
            self.assertEqual(report['recording_since'][:10], llm._today())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(b'private prompt', path.read_bytes())
            self.assertNotIn(key.encode(), path.read_bytes())
            with sqlite3.connect(path) as db:
                self.assertEqual([r[0] for r in db.execute('SELECT status FROM attempts ORDER BY id')],
                                 ['http_error', 'ok'])
            again = llm.usage_ledger.summary(path)
            self.assertEqual(again['requests'], 2)

    def test_rotation_and_cooldown_on_429(self):
        p = self.prov()   # proxy down: transport error cools the proxy, then the keys take over
        Stub.plan = {'sk-or-aaaa1111': [(429, {'Retry-After': '120'}, None)], 'sk-or-bbbb2222': [(200, {}, None)]}
        for _ in range(3):
            p.chat([{'role': 'user', 'content': 'x'}])
        st = p.status()
        a = next(x for x in st['providers'] if x['key'] == 'sk-…1111')
        self.assertIn('m/a:free', a['cooldowns'])            # key A cooled on model a after its 429
        self.assertEqual(a['last_error'], 'm/a:free: HTTP 429')
        self.assertEqual(sum(1 for w, *_ in Stub.seen if w == 'sk-or-aaaa1111'), 1)  # never asked again while cooling
        self.assertEqual(sum(1 for w, *_ in Stub.seen if w == 'sk-or-bbbb2222'), 3)
        proxy = st['providers'][0]
        self.assertTrue(proxy['cooldowns'])                  # proxy down -> short cooldown for the whole provider
        dump = json.dumps(st)
        self.assertNotIn('aaaa1111', dump.replace('…1111', ''))  # never the key itself
        self.assertNotIn('sk-or-', dump)

    def test_401_disables_key_until_restart(self):
        p = self.prov()
        Stub.plan = {'sk-or-aaaa1111': [(401, {}, None)], 'sk-or-bbbb2222': [(200, {}, None)]}
        for _ in range(4):
            p.chat([{'role': 'user', 'content': 'x'}])
        self.assertEqual(sum(1 for w, *_ in Stub.seen if w == 'sk-or-aaaa1111'), 1)
        self.assertTrue(next(x for x in p.status()['providers'] if x['key'] == 'sk-…1111')['disabled'])

    def test_all_down_raises_unavailable(self):
        p = self.prov()
        Stub.plan = {'sk-or-aaaa1111': [(503, {}, None)], 'sk-or-bbbb2222': [(402, {}, None)]}
        with self.assertRaises(llm.Unavailable):
            p.chat([{'role': 'user', 'content': 'x'}])
        n = len(Stub.seen)
        with self.assertRaises(llm.Unavailable):   # everything cooling: nothing is sent at all
            p.chat([{'role': 'user', 'content': 'x'}])
        self.assertEqual(len(Stub.seen), n)

    def test_daily_limit_shared_by_key_across_models(self):
        p = self.prov(keys=('sk-or-aaaa1111',))
        p.daily_limit = 2
        p.chat([{'role': 'user', 'content': 'x'}], models=('m/a:free',))
        p.chat([{'role': 'user', 'content': 'x'}], models=('m/b:free',))
        self.assertEqual([m for key, m, *_ in Stub.seen if key == 'sk-or-aaaa1111'], ['m/a:free', 'm/b:free'])
        with self.assertRaises(llm.Unavailable):
            p.chat([{'role': 'user', 'content': 'x'}])

    def test_keys_from_env_and_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'openrouter.json'
            f.write_text(json.dumps({'keys': ['sk-file-9999', 'sk-env-1234'], 'models': ['x/y:free']}))
            old = os.environ.get('OPENROUTER_API_KEYS')
            os.environ['OPENROUTER_API_KEYS'] = 'sk-env-1234, ,sk-env-5678'
            try:
                p = llm.Providers.load(f)
            finally:
                if old is None:
                    del os.environ['OPENROUTER_API_KEYS']
                else:
                    os.environ['OPENROUTER_API_KEYS'] = old
        self.assertEqual(p.keys, ['sk-env-1234', 'sk-env-5678', 'sk-file-9999'])
        self.assertEqual(p.models, ('x/y:free',))
        self.assertEqual([x['key'] for x in p.status()['providers']], [None, 'sk-…1234', 'sk-…5678', 'sk-…9999'])

    def test_only_free_models_can_be_configured_or_called(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'openrouter.json'
            with self.assertRaises(ValueError):
                llm.set_models(['vendor/paid'], path=f)
            f.write_text(json.dumps({'models': ['vendor/paid']}))
            self.assertTrue(all(m.endswith(':free') for m in llm.settings(f)['models']))
        self.assertEqual(llm.model_order(['vendor/paid', 'm/a:free'], {}), ('m/a:free',))
        with self.assertRaises(ValueError):
            llm._post(self.url, None, 'vendor/paid', [], 1, 10, True)


class LayaStub(BaseHTTPRequestHandler):
    mode = 'ok'
    calls = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        LayaStub.calls.append('health')
        data = json.dumps({'ok': True, 'model': laya.MODEL,
                           'deployment_version': laya.DEPLOYMENT_VERSION}).encode()
        self.send_response(200 if self.mode != 'down' else 500)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        LayaStub.calls.append(('decide', len(body['items']), [q['key'] for q in body['questions']]))
        if self.mode == 'slow':
            time.sleep(1.5)
        res = [{'id': it['id'], 'answers': {'dtc_founder': {'p': 0.95}, 'brand_account': {'p': 0.92}, 'creator': {'p': 0.05},
                                             'service_provider': {'p': 0.1}, 'netherlands': {'p': 0.1}}} for it in body['items']]
        data = json.dumps({'results': res, 'model': laya.MODEL,
                           'deployment_version': laya.DEPLOYMENT_VERSION}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass


class LayaTest(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), LayaStub)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.old = laya.URL, laya.DECIDE_TIMEOUT
        laya.URL = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        laya.reset()
        LayaStub.mode, LayaStub.calls = 'ok', []

    def tearDown(self):
        laya.URL, laya.DECIDE_TIMEOUT = self.old
        laya.reset()
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_batches_and_five_questions(self):
        # macOS monotonic clocks can begin near zero in a new process.
        with patch.object(laya.time, 'monotonic', return_value=0.05):
            self.assertTrue(laya.available())
        with patch.object(laya.time, 'monotonic', return_value=0.1):
            self.assertTrue(laya.available())
        self.assertEqual(LayaStub.calls.count('health'), 1)   # cached for 60 s
        people = [{'id': i, 'handle': f'h{i}', 'bio': 'x'} for i in range(130)]
        out = laya.decide(people)
        self.assertEqual(len(out), 130)
        self.assertEqual([c[1] for c in LayaStub.calls if c != 'health'], [64, 64, 2])
        self.assertEqual(LayaStub.calls[1][2], ['dtc_founder', 'brand_account', 'creator', 'service_provider', 'netherlands'])
        self.assertEqual(out[0], {'dtc_founder': 0.95, 'brand_account': 0.92, 'creator': 0.05,
                                  'service_provider': 0.1, 'netherlands': 0.1})
        self.assertGreater(laya.fit(out[0]), 70)

    def test_empty_cache_probes_even_near_clock_origin(self):
        self.assertTrue(laya.available(now=1))
        self.assertTrue(laya.available(now=2))
        self.assertEqual(LayaStub.calls, ['health'])

    def test_down_or_slow_is_skipped(self):
        laya.URL = 'http://127.0.0.1:9'
        self.assertFalse(laya.available())
        self.assertEqual(laya.decide([{'id': 1, 'handle': 'x'}]), {})
        laya.URL = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        laya.reset()
        LayaStub.mode, laya.DECIDE_TIMEOUT = 'slow', 0.5
        self.assertEqual(laya.decide([{'id': 1, 'handle': 'x'}]), {})
        self.assertFalse(laya.available())   # a timeout marks it down for the cache period

    def test_tags_only_when_very_sure(self):
        self.assertEqual(laya.tags({'dtc_founder': 0.99, 'creator': 0.85, 'brand_account': 0.93}, set()), [])
        self.assertEqual(laya.tags({'brand_account': 0.93}, {'Brand'}), [])
        self.assertIsNone(laya.fit({}))


if __name__ == '__main__':
    unittest.main()
