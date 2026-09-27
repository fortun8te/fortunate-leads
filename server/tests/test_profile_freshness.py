"""Profile freshness and request states, using temporary databases only."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
import server


OLD_AT = '2026-01-01T10:00:00.000000+00:00'
NEW_AT = '2026-09-26T10:00:00.000000+00:00'


class ProfileFreshness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.init(str(Path(self.tmp.name) / 'profile-tests.sqlite'))
        self.addCleanup(self.conn.close)
        self.pid = db.upsert_person(self.conn, {
            'handle': 'alice', 'ig_id': '101', 'name': 'Alice',
            'bio': 'Last known bio', 'website': 'https://alice.example',
            'followers': 12, 'bio_at': OLD_AT, 'bio_src': 'meta_bd',
        }, OLD_AT)
        self.conn.commit()

    def detail(self, pid=None):
        return server.api_person(self.conn, {}, {}, self.pid if pid is None else pid)

    def insert_job(self, state='queued', handle='alice', kind='profile', attempts=0):
        jid = self.conn.execute(
            'INSERT INTO jobs(kind,handle,state,attempts,created_at,leased_until) VALUES(?,?,?,?,?,?)',
            (kind, handle, state, attempts, OLD_AT,
             '2099-01-01T00:00:00+00:00' if state == 'leased' else None),
        ).lastrowid
        self.conn.commit()
        return jid

    def job(self, jid):
        return self.conn.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()

    def request(self, pid=None):
        server.api_read(self.conn, {}, {}, self.pid if pid is None else pid)

    def lease(self):
        return server.ext_next(self.conn, {'kinds': ['profile']}, {'version': '3.9.17'})['job']

    def token(self, jid):
        # The extension echoes the lease token it was handed with the job.
        row = self.conn.execute('SELECT lease_token FROM jobs WHERE id=?', (jid,)).fetchone()
        return row[0] if row else None

    def ingest(self, profile, job_id=None):
        with patch.object(db, 'now', return_value=NEW_AT):
            return server.ext_profile(self.conn, {}, {'job_id': job_id, 'lease_token': self.token(job_id), 'profile': profile})

    def assert_old_profile(self, detail=None):
        p = self.detail() if detail is None else detail
        self.assertEqual(
            (p['bio'], p['website'], p['followers'], p['bio_at'], p['bio_src']),
            ('Last known bio', 'https://alice.example', 12, OLD_AT, 'meta_bd'),
        )

    def assert_read_state(self, expected, pending):
        p = self.detail()
        self.assertEqual(p['profile_read'], None if expected is None else {'state': expected})
        self.assertIs(p['profile_read_pending'], pending)
        return p

    def test_no_request_has_no_invented_request_state(self):
        self.assert_old_profile(self.assert_read_state(None, False))

    def test_extension_replaces_stale_source_and_ignores_supplied_source_or_timestamp(self):
        self.request()
        jid = self.lease()['id']
        result = self.ingest({
            'handle': '@Alice', 'ig_id': '101', 'bio': 'Current bio',
            'bio_src': 'meta_bd', 'bio_at': OLD_AT,
        }, jid)
        self.assertEqual(result, {'id': self.pid})
        p = self.assert_read_state('done', False)
        self.assertEqual((p['bio'], p['bio_at'], p['bio_src']), ('Current bio', NEW_AT, 'extension'))
        self.assertEqual((self.job(jid)['state'], self.job(jid)['leased_until']), ('done', None))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM people').fetchone()[0], 1)

    def test_successful_empty_bio_is_a_fresh_read(self):
        self.request()
        jid = self.lease()['id']
        self.ingest({'handle': 'alice', 'ig_id': '101', 'bio': ''}, jid)
        p = self.assert_read_state('done', False)
        self.assertEqual((p['bio'], p['bio_at'], p['bio_src']), ('', NEW_AT, 'extension'))
        self.assertEqual(p['website'], 'https://alice.example')

    def test_queued_and_reading_follow_the_actual_job_without_clearing_profile(self):
        self.request()
        self.assert_old_profile(self.assert_read_state('queued', True))
        jid = self.conn.execute("SELECT id FROM jobs WHERE handle='alice'").fetchone()[0]
        self.assertEqual(self.job(jid)['state'], 'queued')
        self.assertEqual(self.lease()['id'], jid)
        self.assertEqual(self.job(jid)['state'], 'leased')
        self.assert_old_profile(self.assert_read_state('reading', True))

    def test_terminal_failure_matches_job_and_keeps_last_successful_profile(self):
        jid = self.insert_job('leased', attempts=server.PROFILE_MAX_ATTEMPTS)
        server.ext_error(self.conn, {}, {'job_id': jid, 'lease_token': self.token(jid), 'code': 'other', 'message': 'Profile timed out'})
        self.assertEqual((self.job(jid)['state'], self.job(jid)['leased_until']), ('error', None))
        self.assert_old_profile(self.assert_read_state('failed', False))

    def test_retryable_error_returns_to_queue_without_claiming_freshness(self):
        self.request()
        jid = self.lease()['id']
        server.ext_error(self.conn, {}, {'job_id': jid, 'lease_token': self.token(jid), 'code': 'other', 'message': 'Try later'})
        self.assertEqual(self.job(jid)['state'], 'queued')
        self.assert_old_profile(self.assert_read_state('queued', True))

    def test_expired_final_attempt_reports_failed_instead_of_reading_forever(self):
        jid = self.insert_job('leased', attempts=server.PROFILE_MAX_ATTEMPTS)
        self.conn.execute('UPDATE jobs SET leased_until=? WHERE id=?', (OLD_AT, jid))
        self.conn.commit()
        self.assertIsNone(self.lease())
        self.assertEqual(self.job(jid)['state'], 'error')
        self.assert_old_profile(self.assert_read_state('failed', False))

    def test_active_work_takes_precedence_over_newer_terminal_history(self):
        for state, shown in (('queued', 'queued'), ('leased', 'reading')):
            with self.subTest(state=state):
                self.conn.execute('DELETE FROM jobs')
                active = self.insert_job(state)
                self.insert_job('done')
                self.insert_job('error')
                self.assert_old_profile(self.assert_read_state(shown, True))
                self.assertEqual(self.job(active)['state'], state)

    def test_newest_request_wins_within_active_and_terminal_groups(self):
        self.insert_job('leased')
        queued = self.insert_job('queued')
        self.assert_read_state('queued', True)
        self.conn.execute("UPDATE jobs SET state='done'")
        self.conn.commit()
        self.insert_job('error')
        self.assert_read_state('failed', False)
        done = self.insert_job('done')
        self.assertGreater(done, queued)
        self.assert_read_state('done', False)

    def test_metadata_uses_matching_handle_and_profile_kind(self):
        self.insert_job('queued', handle='bob')
        self.insert_job('leased', handle='alice', kind='list')
        self.assert_read_state(None, False)
        jid = self.insert_job('error')
        self.insert_job('queued', handle='alice_other')
        self.assert_read_state('failed', False)
        self.assertEqual(self.job(jid)['handle'], 'alice')

    def test_renamed_account_uses_its_current_handle(self):
        self.insert_job('leased')
        pid = db.upsert_person(self.conn, {'handle': 'alice_new', 'ig_id': '101'})
        self.conn.commit()
        self.assertEqual(pid, self.pid)
        self.assert_read_state('queued', True)   # the open request followed the account and was re-queued
        self.request()
        self.assert_read_state('queued', True)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM jobs WHERE handle='alice_new' AND state='queued'",
        ).fetchone()[0], 1)

    def test_parked_old_owner_does_not_inherit_current_handles_request(self):
        self.insert_job('queued')
        new_pid = db.upsert_person(self.conn, {'handle': 'alice', 'ig_id': '202'})
        self.conn.commit()
        self.assertNotEqual(new_pid, self.pid)
        self.assertEqual(self.detail()['handle'], f'alice~{self.pid}')
        self.assert_old_profile(self.assert_read_state(None, False))
        # The request belonged to the old account; parking it cancelled the request for both.
        self.assertIsNone(self.detail(new_pid)['profile_read'])
        with self.assertRaises(server.Bad):
            self.request(self.pid)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)

    def test_repeated_requests_reuse_queued_and_leased_work(self):
        for _ in range(3):
            self.request()
        jid = self.lease()['id']
        for _ in range(3):
            self.request()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
        self.assertEqual((self.job(jid)['state'], self.job(jid)['priority']), ('leased', server.READ_PRIORITY))
        self.assert_old_profile(self.assert_read_state('reading', True))

    def test_retry_after_failure_creates_one_new_request_and_keeps_old_profile(self):
        old_jid = self.insert_job('error')
        for _ in range(3):
            self.request()
        jobs = self.conn.execute('SELECT id,state FROM jobs ORDER BY id').fetchall()
        self.assertEqual([tuple(j) for j in jobs], [(old_jid, 'error'), (old_jid + 1, 'queued')])
        self.assert_old_profile(self.assert_read_state('queued', True))

    def test_private_and_not_found_finish_requests_without_fabricating_success(self):
        for code in ('private', 'not_found'):
            with self.subTest(code=code):
                unread = db.upsert_person(self.conn, {'handle': 'unread_' + code})
                self.conn.commit()
                self.request(unread)
                jid = self.lease()['id']
                server.ext_error(self.conn, {}, {'job_id': jid, 'lease_token': self.token(jid), 'code': code, 'message': code})
                p = self.detail(unread)
                self.assertEqual(self.job(jid)['state'], 'done')
                self.assertEqual(p['profile_read'], {'state': 'done'})
                self.assertIs(p['profile_read_pending'], False)
                self.assertIsNone(p['bio_at'])
                self.assertIsNone(p['bio_src'])
                self.assertIsNone(p['bio'])

    def test_passive_extension_read_finishes_only_matching_handle_requests(self):
        alice = self.insert_job('queued')
        bob = self.insert_job('queued', handle='bob')
        self.ingest({'handle': 'alice', 'bio': 'A passive read'})
        self.assertEqual(self.job(alice)['state'], 'done')
        self.assertEqual(self.job(bob)['state'], 'queued')
        self.assert_read_state('done', False)


if __name__ == '__main__':
    unittest.main()
