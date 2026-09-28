import importlib.util
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import requests
except ImportError:
    raise unittest.SkipTest('Optional requests dependency is installed in work/mobile-login-env')
import edge_mobile_transport as mobile
from mobile_collector import Stopped, LocalAPI

spec = importlib.util.spec_from_file_location('edge_mobile_run', Path(__file__).resolve().parents[2] / 'ops/edge_mobile_run.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def client():
    return SimpleNamespace(cookie_dict={'ds_user_id': '99'}, authorization_data={'ds_user_id': '99'}, user_id=99,
        rank_token='stable-rank', base_headers={'User-Agent': 'saved-device'}, authorization='secret',
        private=requests.Session(), phone_id='phone', uuid='uuid', android_device_id='android', advertising_id='ad')


def task(**kw):
    return dict(dict(task_id='task', route='mobile_rest', page_size=50, target_id='123', direction='following',
                     viewer_id='99', cursor=None, lane='alt', transport='mobile'), **kw)


def response(payload=None, status=200):
    value = requests.Response()
    value.status_code = status
    value.raw = io.BytesIO(json.dumps(payload or {'status': 'ok', 'users': [{'pk': '1234', 'username': 'alice'}],
                                                'next_max_id': 'next'}).encode())
    return value


class TransportTests(unittest.TestCase):
    def test_one_send_no_redirect_no_retries_and_fingerprint(self):
        c = client()
        c.private.hooks['response'].append(lambda *args: self.fail('hook invoked'))
        with patch.object(requests.Session, 'send', return_value=response()) as send:
            value = mobile.one_request(c, task(), '99')
        self.assertEqual(send.call_count, 1)
        self.assertFalse(send.call_args.kwargs['allow_redirects'])
        self.assertEqual(send.call_args.args[1].hooks, {'response': []})
        self.assertEqual(c.private.get_adapter('https://i.instagram.com').max_retries.total, 0)
        self.assertEqual(value['actual_http_requests'], 1)
        self.assertEqual(value['status'], 'ok')
        self.assertTrue(value['transport_completed'])
        self.assertEqual(value['device_fingerprint'], mobile.device_fingerprint(c))
        self.assertNotIn('secret', json.dumps(value))
        self.assertGreaterEqual(value['duration_ms'], 0)

    def test_real_session_send_does_not_follow_redirects_or_retry_429(self):
        from requests.adapters import HTTPAdapter
        original_send = requests.Session.send
        for status in (302, 429):
            reply = response(status=status)
            reply.headers['Location'] = 'https://i.instagram.com/other'
            with patch.object(requests.Session, 'send', autospec=True, side_effect=original_send) as counted, patch.object(HTTPAdapter, 'send', return_value=reply) as wire:
                result = mobile.one_request(client(), task(), '99')
            self.assertEqual(counted.call_count, 1)
            self.assertEqual(wire.call_count, 1)
            self.assertTrue(result['terminal_warning'])

    @unittest.skipUnless(importlib.util.find_spec('instagrapi'), 'Pinned instagrapi environment required')
    def test_native_graphql_builder_both_directions(self):
        from urllib.parse import parse_qs
        for direction, name, doc in [('following', 'FollowingList', '16104639286363954576550227636'),
                                     ('followers', 'FollowersList', '284797047911918316998205836755')]:
            t = task(route='mobile_graphql', page_size=None, direction=direction)
            payload = {'data': {'xdt_api__v1__friendships__' + direction: {'users': [{'pk': 1234, 'username': 'alice'}], 'next_max_id': 'next'}}}
            with patch.object(requests.Session, 'send', return_value=response(payload)) as send:
                value = mobile.one_request(client(), t, '99')
            request = send.call_args.args[1]
            body = parse_qs(request.body)
            self.assertEqual(request.url, 'https://i.instagram.com/graphql/query')
            self.assertEqual(body['fb_api_req_friendly_name'], [name])
            self.assertEqual(body['client_doc_id'], [doc])
            variables = json.loads(body['variables'][0])
            self.assertNotIn('count', variables)
            self.assertNotIn('count', variables['request_data'])
            self.assertEqual(value['status'], 'ok')
            self.assertIsNone(value['requested_count'])
            self.assertEqual(len(value['rows']), 1)
            self.assertEqual(send.call_count, 1)

    def test_unsupported_graphql_size_never_sends(self):
        with patch.object(requests.Session, 'send') as send, self.assertRaises(Stopped):
            mobile.one_request(client(), task(route='mobile_graphql'), '99')
        send.assert_not_called()

    def test_warning_statuses_and_uncertain_transport_count_once(self):
        for code, expected in [(302, 'soft_block'), (429, 'rate_limit'), (403, 'login'), (500, 'http_error')]:
            with patch.object(requests.Session, 'send', return_value=response(status=code)) as send:
                value = mobile.one_request(client(), task(), '99')
            self.assertEqual(value['status'], expected)
            self.assertTrue(value['terminal_warning'])
            self.assertTrue(value['transport_completed'])
            self.assertEqual(send.call_count, 1)
        with patch.object(requests.Session, 'send', side_effect=requests.Timeout('secret')) as send:
            value = mobile.one_request(client(), task(), '99')
        self.assertIsNone(value['actual_http_requests'])
        self.assertFalse(value['transport_completed'])
        self.assertEqual(send.call_count, 1)
        self.assertNotIn('secret', json.dumps(value))

    def test_corrupt_cursor_and_wrong_graphql_root_stop(self):
        for payload in [dict(status='ok', users=[], has_more=True),
                        dict(status='ok', users=[{'pk': '1', 'username': 'a'}], next_max_id='same'),
                        dict(status='ok', users=[], next_max_id='next')]:
            with self.assertRaises(mobile.PageWarning):
                mobile.parse_page(payload, task(cursor='same'))
        with self.assertRaises(mobile.PageWarning):
            mobile.parse_page({'data': {'xdt_api__v1__friendships__followers': {'users': []}}},
                              task(route='mobile_graphql', page_size=None))
        with self.assertRaises(mobile.PageWarning) as caught:
            mobile.parse_page({'errors': [{'message': 'feedback_required'}]}, task())
        self.assertEqual(caught.exception.code, 'soft_block')

    def test_origin_header(self):
        api = LocalAPI('alt', '99')
        class Opener:
            def open(self, req, **kw):
                self.req = req
                return io.BytesIO(b'{}')
        api.opener = Opener()
        api.call('/api/benchmark/next?transport=mobile')
        self.assertEqual(api.opener.req.get_header('Origin'), 'http://127.0.0.1:8777')

    def test_expired_mobile_permit_records_zero_sends(self):
        class API:
            lane, viewer_id = 'alt', '99'
            def call(self, path, body=None):
                if '/next' in path:
                    return {'enabled': True, 'task': task()}
                if '/permit' in path:
                    return {'granted': True, 'token': 'permit', 'request_id': body['request_id'],
                            'expires_at': (datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat()}
                self.body = body
                return {'ack': True, 'stopped': True}
        with tempfile.TemporaryDirectory() as directory:
            api = API()
            with patch.object(requests.Session, 'send') as send:
                result = runner.run_once(api, client(), Path(directory)/'outbox.json')
            send.assert_not_called()
            self.assertEqual(result['state'], 'stopped')
            self.assertEqual(api.body['actual_http_requests'], 0)
            self.assertTrue(api.body['transport_completed'])
            self.assertEqual(api.body['status'], 'permit_expired')

    def test_outbox_replays_without_second_send_and_stops_inflight(self):
        class API:
            lane, viewer_id = 'alt', '99'
            fail = True
            calls = []
            def call(self, path, body=None):
                self.calls.append((path, body))
                if '/next' in path:
                    return {'enabled': True, 'task': task()}
                if '/permit' in path:
                    return {'granted': True, 'token': 'permit', 'request_id': body['request_id'],
                            'expires_at': (datetime.now(timezone.utc)+timedelta(seconds=90)).isoformat()}
                if self.fail:
                    raise ConnectionError('local only')
                return {'ack': True}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'outbox.json'
            api, c = API(), client()
            with patch.object(requests.Session, 'send', return_value=response()) as send:
                with self.assertRaises(ConnectionError):
                    runner.run_once(api, c, path)
                self.assertEqual(json.loads(path.read_text())['phase'], 'result')
                api.fail = False
                result = runner.run_once(api, c, path)
                self.assertEqual(send.call_count, 1)
                self.assertEqual(result['upstream_requests'], 0)
                self.assertFalse(path.exists())
                runner.save(path, {'phase': 'inflight', 'lane': 'alt', 'viewer': '99'})
                with self.assertRaises(Stopped):
                    runner.run_once(api, c, path)
                self.assertEqual(send.call_count, 1)


if __name__ == '__main__':
    unittest.main()
