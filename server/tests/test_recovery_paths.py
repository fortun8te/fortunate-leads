"""Transient failures recover without duplicating work or committing half a verdict."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db  # noqa: E402
import server  # noqa: E402


class RecoveryPathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'leads.sqlite'))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_qualification_rolls_back_one_person_then_retries_with_limit(self):
        bad = db.upsert_person(self.conn, {'handle': 'bad', 'bio': 'Shop'})
        good = db.upsert_person(self.conn, {'handle': 'good', 'bio': 'Founder of a shop'})
        self.conn.commit()
        original = server.requalify

        def broken(conn, person, me, net):
            if person['id'] == bad:
                conn.execute("INSERT INTO tags VALUES(?,'half-written','signal','auto')", (bad,))
                raise RuntimeError('temporary rule failure')
            return original(conn, person, me, net)

        with patch.object(server, 'requalify', side_effect=broken), patch.object(server.traceback, 'print_exc'):
            self.assertEqual(server.qualify_batch(self.conn), 2)
            self.assertIsNone(self.conn.execute("SELECT 1 FROM tags WHERE person_id=? AND tag='half-written'", (bad,)).fetchone())
            self.assertEqual(self.conn.execute('SELECT model FROM verdicts WHERE person_id=?', (good,)).fetchone()[0], 'rules')
            first = self.conn.execute('SELECT * FROM verdicts WHERE person_id=?', (bad,)).fetchone()
            self.assertEqual((first['model'], first['reason']), ('error', 'retry 1/5'))
            self.assertEqual(server.qualify_batch(self.conn), 0)  # waits instead of spinning
            for attempt in range(2, 6):
                self.conn.execute("UPDATE verdicts SET updated_at='' WHERE person_id=?", (bad,))
                self.assertEqual(server.qualify_batch(self.conn), 1)
                reason = self.conn.execute('SELECT reason FROM verdicts WHERE person_id=?', (bad,)).fetchone()[0]
                self.assertEqual(reason, f"{'failed' if attempt == 5 else 'retry'} {attempt}/5")
            self.assertEqual(server.qualify_batch(self.conn), 0)
        db.upsert_person(self.conn, {'handle': 'bad', 'bio': 'Updated shop'})
        self.conn.commit()
        self.assertEqual(server.qualify_batch(self.conn), 1)
        self.assertEqual(self.conn.execute('SELECT model FROM verdicts WHERE person_id=?', (bad,)).fetchone()[0], 'rules')

    def test_bulk_rule_failure_rolls_back_then_isolates_bad_person(self):
        bad = db.upsert_person(self.conn, {'handle': 'bad', 'bio': 'Shop'})
        good = db.upsert_person(self.conn, {'handle': 'good', 'bio': 'Shop'})
        self.conn.commit()
        original = server.rules.sync
        calls = []

        def flaky(conn, pids, *args):
            calls.append(len(pids))
            if bad in pids:
                conn.execute("INSERT INTO tags VALUES(?,'half-written','signal','rule')", (bad,))
                raise RuntimeError('one bad rule result')
            return original(conn, pids, *args)

        with patch.object(server.rules, 'sync', side_effect=flaky), patch.object(server.traceback, 'print_exc'):
            self.assertEqual(server.qualify_batch(self.conn), 2)
        self.assertEqual(calls, [2, 1, 1])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM tags WHERE tag='half-written'").fetchone())
        self.assertEqual(self.conn.execute('SELECT model FROM verdicts WHERE person_id=?', (good,)).fetchone()[0], 'rules')
        self.assertEqual(self.conn.execute('SELECT reason FROM verdicts WHERE person_id=?', (bad,)).fetchone()[0], 'retry 1/5')

    def test_normal_rule_sync_runs_once_for_batch(self):
        for handle in ('a', 'b', 'c'):
            db.upsert_person(self.conn, {'handle': handle, 'bio': 'Shop'})
        self.conn.commit()
        original = server.rules.sync
        with patch.object(server.rules, 'sync', wraps=original) as sync:
            self.assertEqual(server.qualify_batch(self.conn), 3)
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(len(sync.call_args.args[1]), 3)

    def test_expired_profile_lease_has_backoff_and_hard_stop(self):
        self.conn.execute("INSERT INTO jobs(kind,handle,created_at) VALUES('profile','slow',?)", (db.now(),))
        self.conn.commit()
        lane = {'lane': ['test-lane']}
        first = server.ext_next(self.conn, lane, {})['job']
        self.assertEqual(first['handle'], 'slow')
        job_id = first['id']
        for attempt in range(1, server.PROFILE_MAX_ATTEMPTS + 1):
            self.conn.execute("UPDATE jobs SET leased_until='2000-01-01',retry_not_before=NULL WHERE id=?", (job_id,))
            self.conn.commit()
            self.assertIsNone(server.ext_next(self.conn, lane, {})['job'])
            row = self.conn.execute('SELECT state,attempts,retry_not_before,lease_token FROM jobs WHERE id=?', (job_id,)).fetchone()
            self.assertEqual(row['attempts'], attempt)
            self.assertIsNone(row['lease_token'])
            self.assertEqual(row['state'], 'error' if attempt == server.PROFILE_MAX_ATTEMPTS else 'queued')
            if attempt < server.PROFILE_MAX_ATTEMPTS:
                self.assertGreater(datetime.fromisoformat(row['retry_not_before']), datetime.now(timezone.utc))
                self.conn.execute("UPDATE jobs SET retry_not_before='2000-01-01' WHERE id=?", (job_id,))
                self.conn.commit()
                self.assertEqual(server.ext_next(self.conn, lane, {})['job']['id'], job_id)
        self.assertIsNone(server.ext_next(self.conn, lane, {})['job'])

    def test_profile_other_error_waits_and_stops_after_eight_attempts(self):
        self.conn.execute("INSERT INTO jobs(kind,handle,created_at) VALUES('profile','broken',?)", (db.now(),))
        self.conn.commit()
        lane = {'lane': ['test-lane']}
        for attempt in range(1, server.PROFILE_MAX_ATTEMPTS + 1):
            job = server.ext_next(self.conn, lane, {})['job']
            self.assertEqual(job['handle'], 'broken')
            server.ext_error(self.conn, lane, {'job_id': job['id'], 'lease_token': job['lease_token'],
                                               'kind': 'profile', 'code': 'other'})
            row = self.conn.execute('SELECT state,attempts,retry_not_before FROM jobs WHERE id=?', (job['id'],)).fetchone()
            self.assertEqual(row['attempts'], attempt)
            if attempt == server.PROFILE_MAX_ATTEMPTS:
                self.assertEqual(row['state'], 'error')
            else:
                self.assertEqual(row['state'], 'queued')
                self.assertGreater(datetime.fromisoformat(row['retry_not_before']), datetime.now(timezone.utc))
                self.assertIsNone(server.ext_next(self.conn, lane, {})['job'])
                self.conn.execute("UPDATE jobs SET retry_not_before='2000-01-01' WHERE id=?", (job['id'],))
                self.conn.commit()
        self.assertIsNone(server.ext_next(self.conn, lane, {})['job'])

    def test_planner_requeues_legacy_five_attempt_error_once(self):
        db.set_setting(self.conn, 'budget', {'list': 0, 'profile': 0})
        self.conn.execute("INSERT INTO jobs(kind,handle,state,attempts,created_at) "
                          "VALUES('profile','legacy','error',5,?)", (db.now(),))
        self.conn.execute("INSERT INTO jobs(kind,handle,state,attempts,created_at) "
                          "VALUES('profile','terminal','error',8,?)", (db.now(),))
        self.conn.commit()
        server.plan_profiles(self.conn)
        rows = {r['handle']: r for r in self.conn.execute("SELECT * FROM jobs WHERE kind='profile'")}
        self.assertEqual(rows['legacy']['state'], 'queued')
        self.assertIsNotNone(rows['legacy']['retry_not_before'])
        self.assertEqual(rows['terminal']['state'], 'error')
        self.assertEqual(len(rows), 2)
        server.plan_profiles(self.conn)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE handle='legacy'").fetchone()[0], 1)

    def test_planner_commits_legacy_requeue_when_queue_is_full(self):
        db.set_setting(self.conn, 'budget', {'list': 0, 'profile': 1})
        self.conn.execute("INSERT INTO jobs(kind,handle,state,created_at) "
                          "VALUES('profile','waiting','queued',?)", (db.now(),))
        self.conn.execute("INSERT INTO jobs(kind,handle,state,attempts,created_at) "
                          "VALUES('profile','legacy','error',5,?)", (db.now(),))
        self.conn.commit()

        self.assertEqual(server.plan_profiles(self.conn), 0)
        self.assertFalse(self.conn.in_transaction)
        other = db.connect(str(Path(self.tmp.name) / 'leads.sqlite'))
        try:
            self.assertEqual(other.execute("SELECT state FROM jobs WHERE handle='legacy'").fetchone()[0], 'queued')
            other.execute('BEGIN IMMEDIATE')
            other.rollback()
        finally:
            other.close()


if __name__ == '__main__':
    unittest.main()
