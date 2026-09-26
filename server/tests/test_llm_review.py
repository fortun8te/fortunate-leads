"""Free-only provider contract and local quota regression checks; no live network."""
import json
import threading
import io
import urllib.error
from concurrent.futures import ThreadPoolExecutor
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import llm


KEY = 'sk-or-review-secret-12345678'
MODEL_A = 'vendor/one:free'
MODEL_B = 'vendor/two:free'


def reply(model=MODEL_A):
    return ('{"ok":true}', model)


class ProviderSafetyTest(unittest.TestCase):
    def pool(self, limit=2, state_path=None):
        return llm.Providers([KEY], (MODEL_A, MODEL_B), proxy='http://127.0.0.1:9/down',
                             daily_limit=limit, state_path=state_path)

    def test_parallel_requests_share_one_durable_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'quota.json'
            p = self.pool(limit=3, state_path=path)
            p.cool[('proxy', '*')] = (llm.time.time() + 60, 1)
            def call(_):
                try:
                    p.chat([])
                    return True
                except llm.Unavailable:
                    return False
            with patch.object(llm, '_post', return_value='ok') as post:
                with ThreadPoolExecutor(max_workers=16) as executor:
                    results = list(executor.map(call, range(32)))
            self.assertEqual(sum(results), 3)
            self.assertEqual(post.call_count, 3)
            restarted = self.pool(limit=3, state_path=path)
            self.assertEqual(restarted.status()['providers'][1]['state'], 'spent')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_removing_and_readding_key_does_not_reset_quota(self):
        p = self.pool(limit=1)
        with patch.object(llm, '_post', return_value='ok'):
            p.test(llm.key_id(KEY))
        p.configure([], [MODEL_A], 1)
        p.configure([KEY], [MODEL_A], 1)
        with patch.object(llm, '_post') as post:
            self.assertEqual(p.test(llm.key_id(KEY))['state'], 'spent')
        post.assert_not_called()

    def test_provider_reported_spent_blocks_manual_retest(self):
        p = self.pool()
        p.spent[llm.key_id(KEY)] = llm._next_midnight()
        with patch.object(llm, '_post') as post:
            self.assertEqual(p.test(llm.key_id(KEY))['state'], 'spent')
        post.assert_not_called()

    def test_manual_test_backoff_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'quota.json'
            p = self.pool(limit=20, state_path=path)
            with patch.object(llm, '_post', side_effect=llm._Http(429, 120)) as post:
                self.assertFalse(p.test(llm.key_id(KEY))['passed'])
            self.assertEqual(post.call_count, 2)
            restarted = self.pool(limit=20, state_path=path)
            with patch.object(llm, '_post') as post:
                self.assertFalse(restarted.test(llm.key_id(KEY))['passed'])
            post.assert_not_called()

    def test_inflight_success_cannot_clear_concurrent_failure_cooldown(self):
        p = self.pool(limit=20)
        p.cool[('proxy', '*')] = (llm.time.time() + 60, 1)
        started, release = threading.Event(), threading.Event()
        def post(*args):
            if threading.current_thread().name == 'successful-request':
                started.set()
                self.assertTrue(release.wait(5))
                return 'ok'
            raise llm._Http(429, 120)
        with patch.object(llm, '_post', side_effect=post):
            thread = threading.Thread(target=lambda: p.chat([], models=[MODEL_A]), name='successful-request')
            thread.start()
            self.assertTrue(started.wait(5))
            try:
                with self.assertRaises(llm.Unavailable):
                    p.chat([], models=[MODEL_A])
            finally:
                release.set()
                thread.join(5)
        self.assertGreater(p.cool[(llm.key_id(KEY), MODEL_A)][0], llm.time.time())

    def test_lazy_pool_is_created_once_under_concurrency(self):
        pool = self.pool()
        with patch.object(llm, 'PROVIDERS', [None]), patch.object(llm.Providers, 'load', return_value=pool) as load:
            with ThreadPoolExecutor(max_workers=16) as executor:
                instances = list(executor.map(lambda _: llm.get(), range(32)))
        self.assertTrue(all(instance is pool for instance in instances))
        load.assert_called_once()

    def test_paid_override_rejected_before_spending_quota(self):
        p = self.pool()
        with patch.object(llm, '_post') as post:
            with self.assertRaises(ValueError):
                p.chat([], models=['vendor/paid'])
        post.assert_not_called()
        self.assertEqual(p.count, {})

    def test_key_limit_is_shared_across_models_and_test_calls(self):
        p = llm.Providers([KEY], (MODEL_A, MODEL_B, 'vendor/three:free'),
                          proxy='http://127.0.0.1:9/down', daily_limit=3)
        with patch.object(llm, '_post', side_effect=[llm._Http(503, None), llm._Http(503, None),
                                                     '{"ok":true}']) as post:
            result = p.test(llm.key_id(KEY))
        self.assertTrue(result['passed'])
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sum(n for (pid, _), (_, n) in p.count.items() if pid == llm.key_id(KEY)), 3)
        self.assertEqual(p.status()['providers'][1]['state'], 'spent')
        p.cool[('proxy', '*')] = (llm.time.time() + 60, 1)
        with patch.object(llm, '_post') as blocked:
            with self.assertRaises(llm.Unavailable):
                p.chat([], models=(MODEL_B, MODEL_A))
            blocked.assert_not_called()

    def test_key_limit_counts_chat_attempts_across_models(self):
        p = self.pool()
        p.cool[('proxy', '*')] = (llm.time.time() + 60, 1)
        with patch.object(llm, '_post', side_effect=[llm._Http(503, None), '{"ok":true}']) as post:
            self.assertEqual(p.chat([], models=(MODEL_A, MODEL_B)), reply(MODEL_B))
        self.assertEqual(post.call_count, 2)
        with patch.object(llm, '_post') as blocked:
            with self.assertRaises(llm.Unavailable):
                p.chat([])
            blocked.assert_not_called()

    def test_401_on_test_disables_key_and_status_is_broken(self):
        p = self.pool()
        with patch.object(llm, '_post', side_effect=llm._Http(401, None, KEY)):
            result = p.test(llm.key_id(KEY))
        self.assertEqual(result['state'], 'broken')
        status = p.status()['providers'][1]
        self.assertTrue(status['disabled'])
        self.assertEqual(status['state'], 'broken')
        self.assertNotIn(KEY, json.dumps(status))

    def test_usage_and_spent_survive_restart_without_saving_key(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'openrouter.json'
            config.write_text(json.dumps({'keys': [KEY], 'models': [MODEL_A, MODEL_B], 'daily_limit': 2}))
            sidecar = config.with_name('openrouter.state.json')
            with patch.object(llm, '_post', return_value='{"ok":true}'):
                p = llm.Providers.load(config, proxy='http://127.0.0.1:9/down')
                p.test(llm.key_id(KEY))
            self.assertTrue(sidecar.exists())
            self.assertNotIn(KEY, sidecar.read_text())
            q = llm.Providers.load(config, proxy='http://127.0.0.1:9/down')
            self.assertEqual(sum(n for (_, _), (_, n) in q.count.items()), 1)
            with q.lock:
                q.spent[llm.key_id(KEY)] = llm._next_midnight()
                q._save_state()
            restarted = llm.Providers.load(config, proxy='http://127.0.0.1:9/down')
            self.assertEqual(restarted.status()['providers'][1]['state'], 'spent')
            self.assertNotIn(KEY, sidecar.read_text())

    def test_provider_error_never_echoes_key(self):
        p = self.pool()
        with patch.object(llm, '_post', side_effect=llm._Http(429, None, 'key=' + KEY)):
            with self.assertRaises(llm.Unavailable) as caught:
                p.chat([], models=(MODEL_A,))
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn(KEY, json.dumps(p.status()))

    def test_missing_or_substituted_response_model_is_rejected(self):
        class Response:
            def __init__(self, body):
                self.body = body
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, _):
                return json.dumps(self.body).encode()
        class Opener:
            def __init__(self, body):
                self.body = body
            def open(self, *_args, **_kwargs):
                return Response(self.body)
        for body in ({'choices': [{'message': {'content': 'ok'}}]},
                     {'model': MODEL_B, 'choices': [{'message': {'content': 'ok'}}]},
                     {'model': 'vendor/paid', 'choices': [{'message': {'content': 'ok'}}]}):
            with self.subTest(body=body), patch.object(llm, '_opener', return_value=Opener(body)):
                with self.assertRaisesRegex(ValueError, 'did not confirm'):
                    llm._post('http://local.test', None, MODEL_A, [], 1, 10, True)

    def test_http_error_body_redacts_credentials(self):
        error = urllib.error.HTTPError('http://local.test', 429, 'rate limited', {},
                                      io.BytesIO(json.dumps({'error': {'message': 'daily limit key=' + KEY}}).encode()))
        with patch.object(llm, '_opener') as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(llm._Http) as caught:
                llm._post('http://local.test', KEY, MODEL_A, [], 1, 10, True)
        self.assertNotIn(KEY, caught.exception.text)
        self.assertIn('daily limit', caught.exception.text)
        self.assertTrue(llm._is_spent(caught.exception.code, caught.exception.text))

    def test_atomic_write_failure_preserves_prior_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'quota.json'
            llm.write_config({'day': 'before'}, path)
            with patch.object(llm.os, 'replace', side_effect=OSError('mock disk error')):
                with self.assertRaises(OSError):
                    llm.write_config({'day': 'after'}, path)
            self.assertEqual(json.loads(path.read_text()), {'day': 'before'})
            self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_retry_after_http_date(self):
        date = (datetime.now(timezone.utc) + timedelta(seconds=120)).strftime('%a, %d %b %Y %H:%M:%S GMT')
        self.assertGreater(llm._retry_after(date), 100)
        self.assertLess(llm._retry_after(date), 130)

    def test_config_cannot_remove_the_local_free_request_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'openrouter.json'
            with self.assertRaises(ValueError):
                llm.set_models(daily_limit=0, path=config)
            with self.assertRaises(ValueError):
                llm.set_models(daily_limit=llm.FREE_DAILY + 1, path=config)
            config.write_text(json.dumps({'daily_limit': 100_000}))
            self.assertEqual(llm.settings(config)['daily_limit'], llm.FREE_DAILY)


if __name__ == '__main__':
    unittest.main()
