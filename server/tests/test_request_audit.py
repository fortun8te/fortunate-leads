from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import request_audit as A

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TOKEN = 'ab' * 24


class RequestAudit(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        A.ensure(self.conn)
        self.conn.commit()

    def grant(self, **values):
        args = dict(lane='lane-a', viewer='123', kind='list', jobid=7, purpose='seed_lookup', token=TOKEN, now=NOW)
        args.update(values)
        return A.record_grant(self.conn, **args)

    def release(self, **values):
        args = dict(lane='lane-a', viewer='123', token=TOKEN, now=NOW + timedelta(seconds=5))
        args.update(values)
        return A.record_release(self.conn, **args)

    def test_token_is_only_stored_as_hash_and_fields_are_allowlisted(self):
        self.assertTrue(self.grant())
        row = dict(self.conn.execute('SELECT * FROM request_audit').fetchone())
        self.assertEqual(row['token_hash'], hashlib.sha256(TOKEN.encode()).hexdigest())
        self.assertNotIn(TOKEN, '\n'.join(self.conn.iterdump()))
        self.assertEqual(set(row), {'token_hash','lane','viewer_ig_id','job_id','kind','purpose',
                                    'granted_at','released_at','sent','http_status'})

    def test_grants_and_unknown_navigation_do_not_claim_http_requests(self):
        self.grant()
        self.assertTrue(self.release())
        result = A.rollup(self.conn, NOW)
        self.assertEqual(result['scope'], 'granted_operations_not_total_http_requests')
        row = result['groups'][0]
        self.assertEqual((row['granted_operations'], row['released_operations'], row['operations_with_confirmed_http'],
                          row['operations_with_unknown_http']), (1, 1, 0, 1))

    def test_release_idempotency_immutable_outcome_and_no_duplicate_writes(self):
        self.grant(purpose='list_page')
        self.assertTrue(self.release(sent=True, http_status=429))
        before = self.conn.total_changes
        self.assertTrue(self.release(sent=True, http_status=429, now=NOW + timedelta(seconds=20)))
        self.assertFalse(self.release(sent=False))
        self.assertEqual(self.conn.total_changes, before)
        group = A.rollup(self.conn, NOW)['groups'][0]
        self.assertEqual((group['operations_with_confirmed_http'], group['responses_429']), (1, 1))

    def test_unknown_token_other_lane_and_viewer_cannot_write(self):
        self.grant()
        before = self.conn.total_changes
        for values in ({'token': 'ff' * 24}, {'token': 'bad'}, {'lane': 'other'}, {'viewer': '456'},
                       {'token': None}, {'sent': 'yes'}, {'http_status': 0}, {'sent': False, 'http_status': 200},
                       {'now': NOW - timedelta(seconds=1)}):
            self.assertFalse(self.release(**values), values)
        self.assertEqual(self.conn.total_changes, before)

    def test_grant_validation_and_exact_replays(self):
        for values in ({'kind': 'unknown'}, {'purpose': 'profile_view'}, {'purpose': {}}, {'viewer': True},
                       {'viewer': '0'}, {'lane': 'https://secret.example/path'}, {'jobid': True}, {'jobid': -1},
                       {'token': 'a' * 47}, {'purpose': 'secret response body'}, {'now': '2026-01-01'}):
            self.assertFalse(self.grant(**values), values)
        self.assertTrue(self.grant())
        before = self.conn.total_changes
        self.assertTrue(self.grant())
        self.assertFalse(self.grant(viewer='456'))
        self.assertEqual(self.conn.total_changes, before)

    def test_atomic_with_callers_transaction(self):
        self.conn.execute('CREATE TABLE unrelated(value TEXT)')
        self.conn.execute("INSERT INTO unrelated VALUES('pending')")
        A.ensure(self.conn)
        self.grant()
        self.release(sent=False)
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM request_audit').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM unrelated').fetchone()[0], 0)

    def test_viewer_rollup_index_skips_100k_unrelated_operations(self):
        self.grant()
        self.conn.executemany('''INSERT INTO request_audit SELECT ?,lane,'999',job_id,kind,purpose,
          granted_at,NULL,NULL,NULL FROM request_audit WHERE token_hash=?''',
          ((f'fixture-{i}', hashlib.sha256(TOKEN.encode()).hexdigest()) for i in range(100000)))
        work = [0]
        def progress():
            work[0] += 100
            return int(work[0] > 5000)
        self.conn.set_progress_handler(progress, 100)
        try:
            groups = A.rollup(self.conn, NOW, viewer='123')['groups']
            self.assertEqual(groups[0]['granted_operations'], 1)
            self.assertEqual(groups[0]['unreleased_operations'], 1)
        finally:
            self.conn.set_progress_handler(None, 0)


if __name__ == '__main__':
    unittest.main()
