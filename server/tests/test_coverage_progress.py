"""Current-run coverage shown to the UI must match saved page and lease evidence."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('FL_NO_ORSLOT', '1')

import accounts
import control
import db
import laya
import server


class CoverageProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.tmp.name) / 'coverage.sqlite')
        db.queue_list(self.conn, 'target', 'followers')
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def next(self, lane):
        return server.ext_next(self.conn, {'lane': [lane], 'kinds': ['list'], 'version': ['3.9.17']}, {})['job']

    def page(self, lane, job, index, n=25, end=False, total=None):
        users = [{'ig_id': str(900000000 + index * 25 + j), 'handle': f'member_{index}_{j}'} for j in range(n)]
        return server.ext_list_page(self.conn, {'lane': [lane]}, {
            'job_id': job['id'], 'lease_token': job['lease_token'], 'seed': 'target',
            'direction': 'followers', 'requested_cursor': job['cursor'],
            'next_cursor': None if end else f'page_{index + 1}',
            'has_more': not end, 'done': end, 'users': users,
            'total': total, 'total_source': 'current_run' if total is not None else 'cached',
            'requested_count': 25,
        })

    def test_stopped_trial_needs_review_and_is_not_counted_in_the_pending_queue(self):
        self.conn.execute("UPDATE lists SET state='paused',error='Following page trial stopped' WHERE seed='target'")
        self.conn.commit()
        rows, coverage = server.list_coverage(self.conn)
        self.assertEqual(rows[0]['completion'], 'blocked')
        self.assertEqual((coverage['active_lists'], coverage['blocked_lists']), (0, 1))
        summary = server.collection_progress.summary(self.conn, rows, [])
        self.assertEqual((summary['pending'], summary['needs_review']), (0, 1))

    def test_2500_followers_complete_after_all_pages_and_midrun_handoff(self):
        lane = 'lane-a'
        first_job = None
        for i in range(100):
            if i == 50:
                # The old lane gives back ownership; the next lane must carry
                # the same job and saved cursor rather than restarting at page 1.
                accounts.release_all(self.conn, lane, why='offline')
                self.conn.commit()
                lane = 'lane-b'
            job = self.next(lane)
            self.assertIsNotNone(job)
            first_job = first_job or job['id']
            self.assertEqual(job['id'], first_job)
            self.assertEqual(job['cursor'], None if i == 0 else f'page_{i}')
            self.page(lane, job, i, end=i == 99, total=2500 if i == 0 else None)
            if i < 99:
                self.assertNotEqual(self.conn.execute("SELECT state FROM lists WHERE seed='target'").fetchone()[0], 'done')
        row = self.conn.execute("SELECT state,received,total FROM lists WHERE seed='target'").fetchone()
        self.assertEqual(tuple(row), ('done', 2500, 2500))
        self.assertTrue(db.list_run_complete(self.conn, first_job))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM edges WHERE seed='target'").fetchone()[0], 2500)
        lists, summary = server.list_coverage(self.conn)
        self.assertEqual((lists[0]['saved_current_run'], lists[0]['expected'], lists[0]['expected_source'],
                          lists[0]['completion'], lists[0]['missing']), (2500, 2500, 'current_run', 'complete', 0))
        self.assertEqual((summary['saved_entries'], summary['expected_entries'], summary['complete_lists']), (2500, 2500, 1))

    def test_short_terminal_response_stays_partial_with_saved_members(self):
        lane = 'lane-a'
        self.page(lane, self.next(lane), 0, total=2500)
        self.page(lane, self.next(lane), 1, n=24, end=True)
        row = self.conn.execute("SELECT state,received,total FROM lists WHERE seed='target'").fetchone()
        self.assertEqual(tuple(row), ('partial', 49, 2500))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM edges WHERE seed='target'").fetchone()[0], 49)
        lists, summary = server.list_coverage(self.conn)
        self.assertEqual((lists[0]['saved_current_run'], lists[0]['expected'], lists[0]['missing'],
                          lists[0]['completion']), (49, 2500, 2451, 'partial'))
        self.assertEqual((summary['complete_lists'], summary['partial_lists'], summary['missing_entries']), (0, 1, 2451))

    def test_unknown_target_never_enters_known_denominator(self):
        job = self.next('lane-a')
        self.page('lane-a', job, 0)
        lists, summary = server.list_coverage(self.conn)
        self.assertIsNone(lists[0]['expected'])
        self.assertEqual((summary['saved_entries'], summary['saved_entries_unknown_targets'],
                          summary['expected_entries'], summary['unknown_targets']), (25, 25, 0, 1))

    def test_done_with_orphan_cursor_is_unverified(self):
        job = self.next('lane-a')
        self.page('lane-a', job, 0, end=True, total=25)
        self.assertTrue(db.list_run_complete(self.conn, job['id']))
        self.conn.execute('INSERT INTO list_page_requests(job_id,requested_cursor,next_cursor) VALUES(?,?,?)',
                          (job['id'], 'orphan', None))
        self.conn.commit()
        self.assertFalse(db.list_run_complete(self.conn, job['id']))
        lists, summary = server.list_coverage(self.conn)
        self.assertEqual((lists[0]['completion'], lists[0]['saved_entries'], lists[0]['expected']),
                         ('unverified', 25, 25))
        self.assertEqual((summary['complete_lists'], summary['partial_lists']), (0, 1))

    def test_scraper_status_uses_control_stage_state(self):
        control.set_stage(self.conn, 'lists', pause=True)
        self.conn.commit()
        status = server.api_scraper_status(self.conn, {}, {})
        lists = next(s for s in status['stages'] if s['id'] == 'lists')
        self.assertEqual((lists['state'], lists['paused']), ('paused', True))

    def test_local_progress_requires_current_queue_signature(self):
        db.set_setting(self.conn, 'local_laya', True)
        db.upsert_person(self.conn, {'handle': 'review_me'})
        self.conn.commit()
        stale = server.local_coverage(self.conn)
        self.assertEqual((stale['state'], stale['processed_profiles'], stale['pending_profiles']),
                         ('rebuilding', None, None))
        self.conn.create_function('laya_hash', 6, server.laya_hash, deterministic=True)
        self.assertTrue(server.rebuild_laya_queue(self.conn, laya.cache_signature()))
        pending = server.local_coverage(self.conn)
        self.assertEqual((pending['eligible_profiles'], pending['processed_profiles'], pending['pending_profiles']),
                         (1, 0, 1))
        person = self.conn.execute("SELECT * FROM people WHERE handle='review_me'").fetchone()
        digest = server.laya_hash(*(person[k] for k in ('handle', 'name', 'bio', 'category', 'website', 'followers')))
        self.conn.execute('INSERT INTO laya(person_id,input_hash,answers,fit,updated_at) VALUES(?,?,?,?,?)',
                          (person['id'], digest, '{}', 0, db.now()))
        self.conn.execute('DELETE FROM laya_queue WHERE person_id=?', (person['id'],))
        self.conn.commit()
        server._local_coverage_cache.clear()
        reviewed = server.local_coverage(self.conn)
        self.assertEqual((reviewed['eligible_profiles'], reviewed['processed_profiles'], reviewed['pending_profiles'],
                          reviewed['state']), (1, 1, 0, 'complete'))


if __name__ == '__main__':
    unittest.main()
