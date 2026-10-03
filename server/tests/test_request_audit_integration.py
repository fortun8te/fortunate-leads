"""Real local HTTP permit API, temporary database; no Instagram operations."""
from datetime import datetime, timedelta, timezone
from contextlib import closing
import hashlib
import json
from unittest.mock import patch

import test_server_accounts as lanes
from test_server import db, server


class RequestAuditIntegration(lanes.Base):
    post = lanes.LaneTest.post
    nxt = lanes.LaneTest.nxt
    seeds = lanes.LaneTest.seeds

    def setUp(self):
        super().setUp()
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.31', 'state': 'idle'})
        self.seeds('seed', direction='following')
        self.job = self.nxt('a', 'list')['job']
        self.assertIsNotNone(self.job)

    def request(self, **extra):
        return self.post('a', '/api/ext/request', dict(action='acquire', kind='list',
            job_id=self.job['id'], lease_token=self.job['lease_token'], **extra))

    def release(self, token, **extra):
        return self.post('a', '/api/ext/request', dict(action='release', token=token, **extra))

    def rows(self):
        return [dict(row) for row in self.conn.execute('SELECT * FROM request_audit')]

    def test_grant_and_release_metadata_commit_with_the_gate(self):
        actual_grant = server.request_audit.record_grant
        actual_release = server.request_audit.record_release
        boundaries = []
        def record_grant(conn, *args, **kwargs):
            with closing(db.connect(server.CFG['db'])) as observer:
                boundaries.append(('grant', conn.in_transaction,
                                   db.get_setting(observer, 'instagram_request_gate')))
            return actual_grant(conn, *args, **kwargs)
        def record_release(conn, *args, **kwargs):
            with closing(db.connect(server.CFG['db'])) as observer:
                boundaries.append(('release', conn.in_transaction,
                                   db.get_setting(observer, 'instagram_request_gate')['active'] is not None))
            return actual_release(conn, *args, **kwargs)
        with patch.object(server.request_audit, 'record_grant', side_effect=record_grant):
            code, granted = self.request(purpose='seed_lookup')
        self.assertEqual(code, 200)
        self.assertTrue(granted['granted'])
        row = self.rows()[0]
        self.assertEqual((row['lane'], row['viewer_ig_id'], row['job_id'], row['purpose']),
                         ('lane-a', '101', self.job['id'], 'seed_lookup'))
        self.assertEqual(row['token_hash'], hashlib.sha256(granted['token'].encode()).hexdigest())
        self.assertNotIn(granted['token'], json.dumps(row))
        with patch.object(server.request_audit, 'record_release', side_effect=record_release):
            code, released = self.release(granted['token'], sent=True, http_status=200)
        self.assertEqual((code, released['released']), (200, True))
        self.assertEqual(boundaries, [('grant', True, None), ('release', True, True)])
        row = self.rows()[0]
        self.assertEqual((row['sent'], row['http_status']), (1, 200))
        self.assertIsNotNone(row['released_at'])
        self.assertIsNone(db.get_setting(self.conn, 'instagram_request_gate')['active'])

    def test_denied_and_stale_jobs_create_no_audit(self):
        db.set_setting(self.conn, 'paused_lists', True)
        self.conn.commit()
        self.assertFalse(self.request(purpose='list_page')[1]['granted'])
        self.assertEqual(self.rows(), [])
        db.set_setting(self.conn, 'paused_lists', False)
        self.conn.execute("UPDATE jobs SET leased_until='2000-01-01T00:00:00Z' WHERE id=?", (self.job['id'],))
        self.conn.commit()
        result = self.request(purpose='list_page')[1]
        self.assertFalse(result['granted'])
        self.assertTrue(result['stale'])
        self.assertEqual(self.rows(), [])

    def test_missing_old_client_metadata_preserves_grant_and_unknown_outcome(self):
        grant = self.request()[1]
        self.assertTrue(grant['granted'])
        self.assertEqual(self.rows()[0]['purpose'], 'unknown')
        self.assertTrue(self.release(grant['token'])[1]['released'])
        row = self.rows()[0]
        self.assertIsNotNone(row['released_at'])
        self.assertIsNone(row['sent'])
        self.assertIsNone(row['http_status'])

    def test_invalid_optional_metadata_does_not_change_permission_decision(self):
        granted = self.request(purpose={'invalid': 'SECRET'})[1]
        self.assertTrue(granted['granted'])
        self.assertEqual(db.get_setting(self.conn, 'instagram_request_gate')['active']['token'], granted['token'])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['purpose'], 'unknown')
        self.assertNotIn('SECRET', json.dumps(self.rows()))
        self.assertTrue(self.release(granted['token'], sent='invalid', http_status=-10)[1]['released'])
        self.assertIsNone(db.get_setting(self.conn, 'instagram_request_gate')['active'])
        row = self.rows()[0]
        self.assertIsNotNone(row['released_at'])
        self.assertIsNone(row['sent'])
        self.assertIsNone(row['http_status'])

    def test_other_lane_and_duplicate_release_cannot_change_existing_audit(self):
        granted = self.request(purpose='list_page')[1]
        self.post('b', '/api/ext/heartbeat', {'version': '3.9.31', 'state': 'idle'})
        forged = self.post('b', '/api/ext/request', {'action': 'release', 'token': granted['token'],
                                                   'sent': True, 'http_status': 200})[1]
        self.assertFalse(forged['released'])
        self.assertIsNone(self.rows()[0]['released_at'])
        self.assertTrue(self.release(granted['token'], sent=False)[1]['released'])
        row = self.rows()[0]
        self.assertFalse(self.release(granted['token'], sent=True, http_status=200)[1]['released'])
        self.assertEqual(self.rows()[0], row)

    def test_audit_insert_failure_rolls_back_permit_and_lease_renewal(self):
        before = tuple(self.conn.execute('SELECT * FROM jobs WHERE id=?', (self.job['id'],)).fetchone())
        self.conn.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON request_audit BEGIN SELECT RAISE(ABORT,'fixture audit failure'); END")
        self.conn.commit()
        with patch('traceback.print_exc'):
            code, _ = self.request(purpose='list_page')
        self.assertEqual(code, 500)
        self.assertIsNone(db.get_setting(self.conn, 'instagram_request_gate'))
        self.assertEqual(self.rows(), [])
        self.assertEqual(tuple(self.conn.execute('SELECT * FROM jobs WHERE id=?', (self.job['id'],)).fetchone()), before)

    def test_audit_release_failure_keeps_gate_held(self):
        grant = self.request(purpose='list_page')[1]
        self.conn.execute("CREATE TRIGGER reject_release BEFORE UPDATE ON request_audit BEGIN SELECT RAISE(ABORT,'fixture release failure'); END")
        self.conn.commit()
        with patch('traceback.print_exc'):
            code, _ = self.release(grant['token'], sent=True, http_status=429)
        self.assertEqual(code, 500)
        self.assertEqual(db.get_setting(self.conn, 'instagram_request_gate')['active']['token'], grant['token'])
        self.assertIsNone(self.rows()[0]['released_at'])

    def test_mobile_budget_charge_and_clock_remain_atomic_and_cannot_be_reset(self):
        # Set up an explicitly mobile, identity-bound job using its real API.
        self.conn.execute("UPDATE jobs SET state='done' WHERE id=?", (self.job['id'],))
        self.conn.execute("UPDATE accounts SET collection_backend='mobile',version='mobile-1',budget=? WHERE lane_id='lane-a'",
                          ('{"list":1}',))
        db.set_setting(self.conn, 'mobile_backend_enabled', True)
        db.upsert_person(self.conn, {'ig_id': '909', 'handle': 'fresh', 'is_private': 0, 'following': 2})
        self.conn.commit()
        def mobile(path, body):
            return self.call(path, dict(body, backend='mobile', lane_id='lane-a',
                                       account={'ig_id': '101', 'handle': 'acct.a'}))
        self.assertEqual(mobile('/api/mobile/queue', {'seed': 'fresh', 'direction': 'following', 'count': 200})[0], 200)
        code, out = self.call('/api/ext/next?backend=mobile&lane=lane-a&ig_id=101&kinds=list&version=mobile-1')
        self.assertEqual(code, 200, out)
        job = out['job']
        body = {'action': 'acquire', 'kind': 'list', 'job_id': job['id'], 'lease_token': job['lease_token'], 'purpose': 'list_page'}
        grant = mobile('/api/ext/request', body)[1]
        self.assertTrue(grant['granted'])
        self.assertEqual(self.rows()[0]['purpose'], 'list_page')
        account = self.conn.execute("SELECT * FROM accounts WHERE lane_id='lane-a'").fetchone()
        self.assertEqual(json.loads(account['today'])['list'], 1)
        self.assertIsNotNone(db.get_setting(self.conn, 'mobile_request_after:101'))
        self.assertFalse(mobile('/api/ext/request', body)[1]['granted'])
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(mobile('/api/ext/request', {'action': 'release', 'token': grant['token'], 'sent': True, 'http_status': 200})[1]['released'])
        mobile('/api/ext/heartbeat', {'version': 'mobile-1', 'today': {'list': 0}})
        self.assertEqual(json.loads(self.conn.execute("SELECT today FROM accounts WHERE lane_id='lane-a'").fetchone()[0])['list'], 1)
        self.assertFalse(mobile('/api/ext/request', body)[1]['granted'])
