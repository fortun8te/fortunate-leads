"""Lost or double-saved pages are detected; stale leases and duplicate profile jobs clean up safely."""
import os
os.environ.setdefault('FL_NO_ORSLOT', '1')

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import accounts
import collection_audit
import db
import server


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.tmp.name) / 'audit.sqlite')
        self.q = {'lane': ['alt'], 'kinds': ['list'], 'version': ['3.9.30']}
        self.acct = {'lane_id': 'alt', 'account': {'ig_id': '2', 'handle': 'alternate'}}
        accounts.touch(self.conn, 'alt', {'ig_id': '2', 'handle': 'alternate'}, version='3.9.30')
        db.queue_list(self.conn, 'seed', 'following')
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def page(self, job, handles, requested, nxt):
        body = dict(self.acct, job_id=job['id'], lease_token=job['lease_token'], seed='seed', direction='following',
                    requested_cursor=requested, users=[{'handle': h} for h in handles], next_cursor=nxt, done=nxt is None,
                    has_more=nxt is not None, total=4, total_source='current_run')
        return server.ext_list_page(self.conn, self.q, body)

    def crawl(self):
        job = server.ext_next(self.conn, self.q, dict(self.acct))['job']
        self.page(job, ['a', 'b'], None, 'c1')
        return job

    def counts(self):
        return {k: v['count'] for k, v in collection_audit.audit(self.conn).items()}

    def test_a_clean_crawl_with_a_replayed_page_audits_clean(self):
        job = self.crawl()
        replay = self.page(job, ['a', 'b'], None, 'c1')                 # lost acknowledgement: same page again
        self.assertTrue(replay.get('duplicate') or replay.get('stale'))
        job = server.ext_next(self.conn, self.q, dict(self.acct))['job']     # each page is its own lease
        self.assertEqual(job['cursor'], 'c1')
        self.page(job, ['c', 'd'], 'c1', None)
        counts = self.counts()
        for key in ('lost_pages', 'member_count_drift', 'received_drift', 'double_counted_events', 'orphan_requests', 'duplicate_people'):
            self.assertEqual(counts[key], 0, key)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0], 4)

    def test_a_finished_run_that_never_asked_for_its_next_cursor_is_a_lost_page(self):
        job = self.crawl()
        self.conn.execute("UPDATE jobs SET state='done' WHERE id=?", (job['id'],))
        self.assertEqual(self.counts()['lost_pages'], 1)

    def test_count_drift_and_double_counted_events_are_found(self):
        job = self.crawl()
        self.conn.execute('UPDATE list_runs SET member_count=member_count+1 WHERE job_id=?', (job['id'],))
        self.conn.execute("INSERT INTO collector_events(at,lane,job_id,kind,direction,outcome) VALUES(?,?,?,?,?,?)",
                          (db.now(), 'alt', job['id'], 'list', 'following', 'page'))
        counts = self.counts()
        self.assertEqual((counts['member_count_drift'], counts['received_drift'], counts['double_counted_events']), (1, 1, 1))

    def test_expired_lease_is_requeued_with_its_cursor_and_dry_run_changes_nothing(self):
        job = self.crawl()
        job = server.ext_next(self.conn, self.q, dict(self.acct))['job']
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        self.conn.execute("UPDATE jobs SET leased_until=? WHERE id=?", (accounts.iso(old), job['id']))
        before = self.conn.execute('SELECT state,lane FROM jobs WHERE id=?', (job['id'],)).fetchone()
        report = collection_audit.cleanup(self.conn)
        self.assertEqual(len(report['expired_leases']), 1)
        self.assertEqual(tuple(self.conn.execute('SELECT state,lane FROM jobs WHERE id=?', (job['id'],)).fetchone()), tuple(before))
        collection_audit.cleanup(self.conn, apply=True)
        row = self.conn.execute('SELECT state,lane FROM jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual((row['state'], row['lane']), ('queued', None))
        lst = self.conn.execute("SELECT cursor,received FROM lists WHERE seed='seed'").fetchone()
        self.assertEqual((lst['cursor'], lst['received']), ('c1', 2))
        resumed = server.ext_next(self.conn, self.q, dict(self.acct))['job']
        self.assertEqual(resumed['cursor'], 'c1')

    def test_a_live_lease_is_never_touched(self):
        job = self.crawl()
        job = server.ext_next(self.conn, self.q, dict(self.acct))['job']
        self.assertEqual(collection_audit.release_expired_leases(self.conn, apply=True), [])
        self.assertEqual(self.conn.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'leased')

    def test_duplicate_pending_profile_jobs_collapse_to_one_and_keep_history(self):
        for h in ('Person', 'person', 'PERSON'):
            self.conn.execute("INSERT INTO jobs(kind,handle,created_at) VALUES('profile',?,?)", (h, db.now()))
        self.conn.commit()
        self.assertEqual(self.counts()['duplicate_profile_jobs'], 1)
        collection_audit.cleanup(self.conn, apply=True)
        pending = self.conn.execute("SELECT count(*) FROM jobs WHERE kind='profile' AND state IN ('queued','leased')").fetchone()[0]
        total = self.conn.execute("SELECT count(*) FROM jobs WHERE kind='profile'").fetchone()[0]
        self.assertEqual((pending, total), (1, 3))


if __name__ == '__main__':
    unittest.main()
