"""List safety regressions; uses isolated temporary databases only."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ['FL_NO_ORSLOT'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db
import server


class ListProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = db.init(Path(self.tmp.name) / 'test.sqlite')
        db.queue_list(self.c, 'seed', 'following')
        self.c.commit()
        self.q = {'lane': ['lane-a']}

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def page(self, people, cursor=None, done=False, total=None, source='unknown'):
        job = server.ext_next(self.c, self.q, {})['job']
        self.assertIsNotNone(job)
        body = dict(job_id=job['id'], seed=job['seed'], direction=job['direction'],
                    lease_token=job['lease_token'], requested_cursor=job['cursor'],
                    users=[{'handle': h} for h in people], next_cursor=cursor,
                    done=done, total=total, total_source=source)
        return server.ext_list_page(self.c, self.q, body), body

    def old_edge(self):
        pid = db.upsert_person(self.c, {'handle': 'old'})
        db.add_edge(self.c, 'seed', pid, 'following')
        self.c.commit()
        return pid

    def active(self, pid):
        return bool(self.c.execute('SELECT active FROM edge_evidence WHERE person_id=?', (pid,)).fetchone()[0])

    def test_cycle_saves_new_people_and_preserves_resume_cursor(self):
        self.page(['alice'], 'a', total=10, source='current_run')
        self.page(['bob'], 'b', total=10, source='current_run')
        result, _ = self.page(['carol'], 'a', total=10, source='current_run')
        self.assertEqual(result, {'received': 3, 'stalled': True, 'partial': True})
        self.assertEqual(tuple(self.c.execute('SELECT state,cursor,received FROM lists').fetchone()), ('partial', 'b', 3))
        self.assertEqual(self.c.execute('SELECT count(*) FROM edges').fetchone()[0], 3)
        self.assertEqual(self.c.execute('SELECT state FROM jobs').fetchone()[0], 'error')
        self.assertTrue(db.queue_list(self.c, 'seed', 'following'))
        self.c.commit()
        self.assertIsNone(server.ext_next(self.c, self.q, {})['job']['cursor'])

    def test_old_extension_cannot_lease_jobs(self):
        result = server.ext_next(self.c, dict(self.q, version=['3.7.9']), {})
        self.assertTrue(result['upgrade_required'])
        self.assertIsNone(result['job'])
        self.assertEqual(self.c.execute('SELECT state FROM jobs').fetchone()[0], 'queued')
        self.assertIsNotNone(server.ext_next(self.c, dict(self.q, version=['3.8.0']), {})['job'])

    def test_replayed_request_is_idempotent(self):
        _, body = self.page(['alice'], 'a')
        result = server.ext_list_page(self.c, self.q, body)
        self.assertTrue(result['duplicate'])
        self.assertEqual(self.c.execute('SELECT count(*) FROM pages').fetchone()[0], 1)
        self.assertEqual(self.c.execute('SELECT count(*) FROM list_members').fetchone()[0], 1)

    def test_cached_or_missing_total_never_disproves_prior_member(self):
        old = self.old_edge()
        db.upsert_person(self.c, {'handle': 'seed', 'following': 1})
        self.c.execute('UPDATE lists SET total=1')
        self.c.commit()
        self.page(['alice'], done=True)
        self.assertTrue(self.active(old))
        db.queue_list(self.c, 'seed', 'following', refresh=True)
        self.c.commit()
        self.page(['alice'], done=True, total=1, source='cached')
        self.assertTrue(self.active(old))

    def test_fresh_complete_run_can_disprove_and_keeps_raw_history(self):
        old = self.old_edge()
        self.page(['alice'], done=True, total=1, source='current_run')
        self.assertFalse(self.active(old))
        self.assertEqual(self.c.execute('SELECT count(*) FROM edges').fetchone()[0], 2)

    def test_new_refresh_does_not_reuse_previous_trusted_total(self):
        self.page(['alice'], done=True, total=1, source='current_run')
        old = self.old_edge()
        db.queue_list(self.c, 'seed', 'following', refresh=True)
        self.c.commit()
        self.page(['alice'], done=True)
        self.assertTrue(self.active(old))

    def test_legacy_prefix_stays_unverified_after_transient_error(self):
        old = self.old_edge()
        self.c.execute("UPDATE lists SET cursor='legacy',run_job_id=NULL,received=10")
        self.c.execute('DELETE FROM list_runs')
        self.c.commit()
        self.page(['alice'], 'next', total=2, source='current_run')
        job = server.ext_next(self.c, self.q, {})['job']
        server.ext_error(self.c, self.q, dict(job_id=job['id'], lease_token=job['lease_token'], code='other', message='temporary failure'))
        self.page(['bob'], done=True, total=2, source='current_run')
        self.assertTrue(self.active(old))
        self.assertEqual(self.c.execute('SELECT state FROM lists').fetchone()[0], 'partial')
        self.assertEqual(self.c.execute('SELECT first_page_seen FROM list_runs').fetchone()[0], 0)

    def test_repair_preserves_tracked_prefix_and_total_provenance(self):
        old = self.old_edge()
        self.page(['alice'], 'next', total=2, source='current_run')
        self.c.execute("UPDATE jobs SET state='error'")
        self.c.execute("UPDATE lists SET state='error'")
        db.repair_lists(self.c)
        # repair also queues the opposite direction; lease only this continuation.
        self.c.execute("UPDATE jobs SET priority=100 WHERE direction='following' AND state='queued'")
        self.c.commit()
        self.page(['bob'], done=True, total=None)
        self.assertFalse(self.active(old))
        row = self.c.execute("SELECT state,received FROM lists WHERE direction='following'").fetchone()
        self.assertEqual(tuple(row), ('done', 2))


if __name__ == '__main__':
    unittest.main()
