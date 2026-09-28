import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounts
import db
import mobile_collector as mobile


class Response:
    def __init__(self, body=None, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.body = body if body is not None else {'status': 'ok', 'users': [{'pk': 1, 'username': 'alice'}], 'next_max_id': 'mobile-cursor', 'has_more': True}
        self.closed = False

    def iter_content(self, **kwargs):
        yield json.dumps(self.body).encode()

    def close(self):
        self.closed = True


def client(response=None):
    return SimpleNamespace(cookie_dict={'ds_user_id': '99'}, authorization_data={'ds_user_id': '99'}, user_id=99,
        rank_token='99_stable-device-uuid', base_headers={'User-Agent': 'test'}, authorization='secret-auth',
        private=SimpleNamespace(get=Mock(return_value=response or Response())),
        private_request=Mock(side_effect=AssertionError('must not use retrying wrapper')),
        user_followers_v1_chunk=Mock(side_effect=AssertionError('must not use pagination helper')))


def job(**extra):
    return dict({'id': 7, 'seed': 'target', 'ig_id': '123', 'kind': 'list', 'direction': 'followers', 'page_size': 200,
                 'cursor': None, 'collection_backend': 'mobile', 'backend_lane': 'alt', 'backend_viewer_ig_id': '99',
                 'lease_token': 'lease'}, **extra)


class API:
    lane, viewer_id = 'alt', '99'
    def __init__(self, state=None, granted=True, task=None, fail_save=False):
        self.state = state or {'backend': 'mobile', 'backend_cursor_isolated': True, 'paused': False, 'stages': {'list': True}}
        self.granted, self.task, self.fail_save = granted, task or job(), fail_save
        self.calls = []
    def call(self, path, body=None):
        self.calls.append((path, body))
        if path == '/api/mobile/state':
            return self.state
        if path.startswith('/api/ext/next'):
            return dict(self.state, job=self.task)
        if path == '/api/ext/request':
            return {'granted': self.granted, 'token': 'permit'} if body['action'] == 'acquire' else {'released': True}
        if self.fail_save:
            raise RuntimeError('local server offline')
        return {'received': 1} if path.endswith('list-page') else {}


class MobileTransportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.outbox = Path(self.temp.name) / 'outbox.json'

    def test_loader_resets_saved_transport_and_retries_after_load(self):
        saved = {'uuids': dict.fromkeys(('phone_id', 'uuid', 'client_session_id', 'advertising_id', 'android_device_id', 'request_id', 'tray_session_id'), 'saved-value'),
                 'device_settings': dict.fromkeys(('app_version', 'android_version', 'android_release', 'dpi', 'resolution', 'manufacturer', 'device', 'model', 'cpu', 'version_code', 'bloks_versioning_id'), 'saved-value'),
                 'cookies': {'ds_user_id': '99', 'sessionid': 'private-value'},
                 'private_transport': 'curl', 'session_retry_total': 3}
        settings = Path(self.temp.name) / 'settings.json'
        settings.write_text(json.dumps(saved)); settings.chmod(0o600)
        c, events = client(), []
        session = SimpleNamespace(trust_env=True, verify=False, proxies={'https': 'unused'}, mount=Mock())
        def load(path):
            events.append('load')
            c.private = SimpleNamespace()  # saved curl transport has no mount()
        def reset(**kwargs):
            events.append('reset')
            c.private = session
        c.load_settings = Mock(side_effect=load)
        c.set_retry_config = Mock(side_effect=reset)
        factory, adapter = Mock(return_value=c), Mock()
        with patch('importlib.metadata.version', return_value='3.0.14'), patch.dict(sys.modules, {
                'instagrapi': SimpleNamespace(Client=factory),
                'requests': SimpleNamespace(), 'requests.adapters': SimpleNamespace(HTTPAdapter=adapter)}):
            self.assertIs(mobile.load_client(settings), c)
        self.assertEqual(events, ['load', 'reset'])
        c.set_retry_config.assert_called_once_with(private_transport='requests', session_retry_total=0)
        self.assertEqual(session.mount.call_count, 2)
        self.assertTrue(all(call.kwargs == {'max_retries': 0} for call in adapter.call_args_list))
        self.assertFalse(session.trust_env)
        self.assertTrue(session.verify)
        self.assertEqual(session.proxies, {})

    def test_saved_device_and_authentication_required_without_cookie_cloning(self):
        saved = {'uuids': dict.fromkeys(('phone_id', 'uuid', 'client_session_id', 'advertising_id', 'android_device_id', 'request_id', 'tray_session_id'), 'saved-value'),
                 'device_settings': dict.fromkeys(('app_version', 'android_version', 'android_release', 'dpi', 'resolution', 'manufacturer', 'device', 'model', 'cpu', 'version_code', 'bloks_versioning_id'), 'saved-value'),
                 'cookies': {'ds_user_id': '99', 'sessionid': 'private-value'}}
        mobile.validate_saved_session(saved)
        saved['authorization_data'] = {'ds_user_id': '99', 'sessionid': 'private-value'}
        mobile.validate_saved_session(saved)
        saved['authorization_data']['ds_user_id'] = '42'
        with self.assertRaises(mobile.Stopped):
            mobile.validate_saved_session(saved)
        with self.assertRaises(mobile.Stopped):
            mobile.validate_saved_session({'cookies': {'ds_user_id': '99', 'sessionid': 'private-value'}})

    def test_default_outbox_isolated_per_lane_and_rejects_paths(self):
        self.assertNotEqual(mobile.default_outbox('alt_1'), mobile.default_outbox('alt_2'))
        self.assertEqual(mobile.default_outbox('alt_1').parent.name, 'alt_1')
        for lane in ('../escape', '/absolute', 'a/b', '', 'x' * 101):
            with self.assertRaises(ValueError):
                mobile.default_outbox(lane)

    def test_denied_permit_retains_lease_without_refetch(self):
        api, c = API(granted=False), client()
        self.assertEqual(mobile.run_once(api, c, self.outbox)['state'], 'waiting')
        api.granted = True
        result = mobile.run_once(api, c, self.outbox)
        self.assertEqual(sum(p.startswith('/api/ext/next') for p, _ in api.calls), 1)
        self.assertEqual(result['job_id'], 7)
        self.assertEqual(result['page_size'], 200)
        self.assertEqual(result['distinct_rows'], 1)
        self.assertGreaterEqual(result['elapsed_ms'], 0)
        self.assertEqual(result['upstream_requests'], 1)
        self.assertFalse(self.outbox.with_suffix('.job.json').exists())

    def test_warning_replay_stops_and_retry_after_preserves_deadline(self):
        for value in ('172800', 'Wed, 01 Jan 2031 00:00:00 GMT'):
            c, api = client(Response(status=429, headers={'Retry-After': value})), API(fail_save=True)
            with self.assertRaises(RuntimeError):
                mobile.run_once(api, c, self.outbox)
            pending = json.loads(self.outbox.read_text())
            deadline = mobile.datetime.fromisoformat(pending['body']['retry_after'])
            if value.isdigit():
                self.assertGreater((deadline - mobile.datetime.now(mobile.timezone.utc)).total_seconds(), 172790)
            else:
                self.assertEqual(deadline.year, 2031)
            api.fail_save = False
            self.assertEqual(mobile.run_once(api, c, self.outbox)['state'], 'stopped')
            c.private.get.assert_called_once()

    def test_typed_warnings_and_malformed_pagination_stop(self):
        for extra, code in (({'error_type': 'feedback_required'}, 'soft_block'),
                ({'require_login': True}, 'login'), ({'two_factor_required': True}, 'challenge'),
                ({'spam': True}, 'soft_block'), ({'has_more': True}, 'other'),
                ({'should_limit_list_of_followers': 'false'}, 'other')):
            c = client(Response(dict(status='ok', users=[], **extra)))
            with self.assertRaises(mobile.ResponseWarning) as caught:
                mobile.one_page(c, job(), '99')
            self.assertEqual(caught.exception.code, code)
            c.private.get.assert_called_once()

    def test_one_get_requests200_but_records_actual_rows_and_cursor(self):
        c = client()
        page = mobile.one_page(c, job(), '99')
        c.private.get.assert_called_once()
        args, kw = c.private.get.call_args
        self.assertEqual(kw['params']['count'], 200)
        self.assertEqual(kw['params']['rank_token'], '99_stable-device-uuid')
        self.assertFalse(kw['allow_redirects'])
        self.assertEqual(kw['timeout'], (5, 20))
        self.assertEqual(len(page['users']), 1)
        self.assertEqual(page['next_cursor'], 'mobile-cursor')
        self.assertFalse(page['done'])
        self.assertEqual(page['total_source'], 'unknown')
        c.private_request.assert_not_called()
        c.user_followers_v1_chunk.assert_not_called()

    def test_mobile_cursor_forwarded_only_for_bound_job(self):
        c = client()
        mobile.one_page(c, job(cursor='opaque-mobile'), '99')
        self.assertEqual(c.private.get.call_args.kwargs['params']['max_id'], 'opaque-mobile')
        for changed in ({'collection_backend': 'chrome'}, {'backend_viewer_ig_id': '42'}):
            c.private.get.reset_mock()
            with self.assertRaises(mobile.Stopped):
                mobile.one_page(c, job(**changed), '99')
            c.private.get.assert_not_called()

    def test_missing_or_inconsistent_session_identity_never_requests(self):
        c = client(); c.authorization_data['ds_user_id'] = '42'
        with self.assertRaises(mobile.Stopped):
            mobile.one_page(c, job(), '99')
        c.private.get.assert_not_called()

    def test_hold_denied_permit_and_wrong_backend_do_not_request(self):
        for api in (API(state={'backend': 'mobile', 'backend_cursor_isolated': True, 'paused': True}),
                    API(granted=False), API(task=job(collection_backend='chrome'))):
            self.outbox.with_suffix('.job.json').unlink(missing_ok=True)
            c = client()
            try:
                mobile.run_once(api, c, self.outbox)
            except mobile.Stopped:
                pass
            c.private.get.assert_not_called()

    def test_429_challenge_and_redirect_stop_without_retry_and_report_before_release(self):
        for response, code in ((Response(status=429, headers={'Retry-After': '120'}), 'rate_limit'),
                (Response({'message': 'challenge_required', 'status': 'fail'}), 'challenge'),
                (Response(status=302), 'soft_block')):
            c, api = client(response), API()
            result = mobile.run_once(api, c, self.outbox)
            self.assertEqual(result['state'], 'stopped')
            c.private.get.assert_called_once()
            error_index = next(i for i, (p, b) in enumerate(api.calls) if p == '/api/ext/error')
            release_index = next(i for i, (p, b) in enumerate(api.calls) if b and b.get('action') == 'release')
            self.assertLess(error_index, release_index)
            self.assertEqual(api.calls[error_index][1]['code'], code)
            self.assertNotIn('secret-auth', json.dumps(api.calls))

    def test_unknown_transport_keeps_permit_for_existing_expiry_attention_hold(self):
        c, api = client(), API()
        c.private.get.side_effect = TimeoutError('secret-response')
        result = mobile.run_once(api, c, self.outbox)
        self.assertEqual(result['state'], 'stopped')
        c.private.get.assert_called_once()
        self.assertFalse(any(b and b.get('action') == 'release' for _, b in api.calls))
        self.assertNotIn('secret-response', json.dumps(api.calls))

    def test_failed_local_save_replays_outbox_without_new_upstream_request(self):
        c, api = client(), API(fail_save=True)
        with self.assertRaises(RuntimeError):
            mobile.run_once(api, c, self.outbox)
        self.assertTrue(self.outbox.exists())
        self.assertEqual(self.outbox.stat().st_mode & 0o777, 0o600)
        api.fail_save = False
        result = mobile.run_once(api, c, self.outbox)
        self.assertEqual(result['upstream_requests'], 0)
        self.assertEqual(result['returned_count'], 1)
        c.private.get.assert_called_once()
        self.assertFalse(self.outbox.exists())

    def test_outbox_different_viewer_cannot_be_replayed(self):
        self.outbox.write_text(json.dumps({'lane': 'alt', 'viewer': '42'}))
        api, c = API(), client()
        with self.assertRaises(mobile.Stopped):
            mobile.run_once(api, c, self.outbox)
        self.assertEqual(api.calls, [])
        c.private.get.assert_not_called()

    def test_absent_end_flag_is_not_completion(self):
        page = mobile.one_page(client(Response({'status': 'ok', 'users': []})), job(), '99')
        self.assertFalse(page['done'])
        self.assertIsNone(page['has_more'])


class MobileQueueTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.temp.name) / 'test.sqlite'))
        self.conn.execute("INSERT INTO accounts(lane_id,ig_id,is_main,role,last_seen,version) VALUES('alt','99',0,'both',?,'3.9.20')", (db.now(),))
        self.conn.execute("INSERT INTO accounts(lane_id,ig_id,is_main,role,last_seen,version) VALUES('main','77',1,'both',?,'3.9.20')", (db.now(),))
        db.upsert_person(self.conn, {'handle': 'target', 'ig_id': '123'})
        db.set_setting(self.conn, 'paused_lists', True)
        self.conn.commit()
    def tearDown(self):
        self.conn.close(); self.temp.cleanup()
    def enable(self):
        mobile.configure_lane(self.conn, 'alt', '99', enabled=True)
    def test_disable_preserves_mobile_jobs_and_cursors(self):
        self.enable()
        mobile.queue_mobile_list(self.conn, 'alt', '99', 'target', 'followers')
        self.conn.execute("UPDATE lists SET cursor='mobile-only' WHERE seed='target'")
        result = mobile.configure_lane(self.conn, 'alt', '99', disable=True)
        self.assertEqual(result['backend'], 'chrome')
        self.assertFalse(db.get_setting(self.conn, mobile.FLAG))
        self.assertEqual(self.conn.execute('SELECT collection_backend FROM jobs').fetchone()[0], 'mobile')
        self.assertEqual(self.conn.execute('SELECT cursor FROM lists').fetchone()[0], 'mobile-only')
        with self.assertRaises(ValueError):
            db.queue_list(self.conn, 'target', 'followers', refresh=True)

    def test_mobile_private_list_never_reopens_as_chrome(self):
        self.enable()
        mobile.queue_mobile_list(self.conn, 'alt', '99', 'target', 'followers')
        self.conn.execute("UPDATE jobs SET state='done'")
        self.conn.execute("UPDATE lists SET state='private',cursor='mobile-only'")
        self.conn.execute("INSERT INTO list_private_denials(seed,direction,viewer_ig_id,denied_at) VALUES('target','followers','99',?)", (db.now(),))
        row = self.conn.execute("SELECT * FROM accounts WHERE lane_id='main'").fetchone()
        accounts.reopen_private_for_viewer(self.conn, row, mobile.datetime.now(mobile.timezone.utc))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)

    def test_disabled_by_default_and_main_never_assignable(self):
        with self.assertRaises(ValueError):
            mobile.validate_client(self.conn, 'alt', 'mobile', '99')
        with self.assertRaises(ValueError):
            mobile.configure_lane(self.conn, 'main', '77', enabled=True)
        self.assertFalse(db.get_setting(self.conn, mobile.FLAG, False))
    def test_explicit_configuration_preserves_pause_and_existing_warning(self):
        db.set_setting(self.conn, 'cooldown', '2099-01-01T00:00:00+00:00')
        self.enable()
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
        self.assertEqual(db.get_setting(self.conn, 'cooldown'), '2099-01-01T00:00:00+00:00')
        with self.assertRaises(ValueError):
            mobile.validate_client(self.conn, 'alt', 'chrome', '99')
    def test_only_fresh_target_queued_and_repair_never_creates_chrome_counterpart(self):
        self.enable()
        queued = mobile.queue_mobile_list(self.conn, 'alt', '99', 'target', 'followers')
        self.assertEqual(queued['count'], 200)
        row = self.conn.execute('SELECT * FROM jobs WHERE id=?', (queued['id'],)).fetchone()
        self.assertEqual(row['collection_backend'], 'mobile')
        self.assertEqual(row['backend_lane'], 'alt')
        with self.assertRaises(ValueError):
            db.queue_list(self.conn, 'target', 'followers', refresh=True)
        db.repair_lists(self.conn)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
        with self.assertRaises(ValueError):
            mobile.queue_mobile_list(self.conn, 'alt', '99', 'target', 'following')
    def test_existing_chrome_history_never_relabelled(self):
        db.queue_list(self.conn, 'target', 'followers')
        self.enable()
        with self.assertRaises(ValueError):
            mobile.queue_mobile_list(self.conn, 'alt', '99', 'target', 'followers')
        self.assertEqual(self.conn.execute('SELECT collection_backend FROM jobs').fetchone()[0], 'chrome')
    def test_mobile_and_chrome_dispatch_are_isolated(self):
        self.enable()
        mobile.queue_mobile_list(self.conn, 'alt', '99', 'target', 'followers')
        db.queue_list(self.conn, 'chrome_target', 'following')
        now = mobile.datetime.now(mobile.timezone.utc)
        picked = accounts.pick_job(self.conn, 'alt', ['list'], now)
        self.assertEqual(picked['collection_backend'], 'mobile')
        main = accounts.pick_job(self.conn, 'main', ['list'], now)
        self.assertTrue(main is None or main['collection_backend'] == 'chrome')

if __name__ == '__main__':
    unittest.main()
