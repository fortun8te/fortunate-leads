import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounts
import collection_progress
import db


class CollectionProgressTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = db.init(str(Path(self.temp.name) / 'test.sqlite'))
        self.addCleanup(self.conn.close)
        self.now = datetime.now(timezone.utc)
        self.conn.execute("INSERT INTO accounts(lane_id,ig_id,role,last_seen) VALUES('lane','123','both',?)", (accounts.iso(self.now),))
        self.conn.commit()
        self.lanes = [{'lane_id': 'lane', 'online': True, 'paused': False, 'hold': None,
                       'role': 'both', 'budget': {'list': 0}, 'today': {'list': 6}}]
        self.lists = [{'completion': 'collecting', 'expected_source': 'current_run',
                       'expected': 1060, 'saved_current_run': 60, 'run_job_id': 1}]
        for minutes in (10, 8, 6, 4, 2, 0.5):
            self.event(minutes)

    def event(self, minutes, added=10, job=1, lane='lane'):
        self.conn.execute("INSERT INTO collector_events(at,lane,job_id,kind,outcome,saved_entries,returned_count) VALUES(?,?,?,'list','page',?,200)",
                          (accounts.iso(self.now - timedelta(minutes=minutes)), lane, job, added))
        self.conn.commit()

    def summary(self):
        return collection_progress.summary(self.conn, self.lists, self.lanes, self.now)

    def test_upgrade_preserves_old_events_without_inventing_distinct_counts(self):
        self.conn.execute('ALTER TABLE collector_events DROP COLUMN saved_entries')
        self.conn.commit()
        upgraded = db.init(str(Path(self.temp.name) / 'test.sqlite'))
        self.addCleanup(upgraded.close)
        rows = upgraded.execute('SELECT returned_count,saved_entries FROM collector_events').fetchall()
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(tuple(row) == (200, None) for row in rows))

    def test_measured_known_queue_has_a_range_and_discovery_does_not_imply_final_completion(self):
        out = self.summary()
        self.assertEqual(out['pending'], 1)
        self.assertEqual(out['known_entries_left'], 1000)
        self.assertTrue(out['open_ended'])
        self.assertEqual(out['eta']['scope'], 'current_queue')
        self.assertLess(out['eta']['low_minutes'], out['eta']['high_minutes'])
        self.assertGreater(out['eta']['low_minutes'], 100)

    def test_unknown_or_old_profile_counts_are_separate_from_known_lists(self):
        self.lists.append({'completion': 'waiting', 'expected_source': 'estimate',
                           'expected': 999999, 'saved_current_run': None, 'run_job_id': 2})
        out = self.summary()
        self.assertEqual(out['unknown_lists'], 1)
        self.assertEqual(out['known_entries_left'], 1000)
        self.assertEqual(out['eta']['scope'], 'known_lists')
        self.lists = self.lists[1:]
        self.assertIsNone(self.summary()['eta'])
        self.assertEqual(self.summary()['status'], 'unknown_totals')

    def test_old_runs_returned_duplicates_and_other_lanes_do_not_inflate_rate(self):
        baseline = self.summary()['eta']
        self.event(1, added=50000, job=99)
        self.event(1, added=50000, lane='unavailable')
        self.assertEqual(self.summary()['eta'], baseline)
        self.conn.execute('UPDATE collector_events SET saved_entries=0 WHERE job_id=1')
        self.conn.commit()
        self.assertIsNone(self.summary()['eta'])
        self.assertEqual(self.summary()['message'], 'No new entries in recent pages')

    def test_new_schema_history_without_distinct_counts_warms_up(self):
        self.conn.execute('UPDATE collector_events SET saved_entries=NULL')
        self.conn.commit()
        self.assertEqual(self.summary()['status'], 'warming_up')
        self.assertIsNone(self.summary()['eta'])

    def test_paused_security_hold_cooldown_and_budget_never_keep_a_running_eta(self):
        db.set_setting(self.conn, 'paused_lists', True)
        self.assertEqual(self.summary()['status'], 'paused')
        db.set_setting(self.conn, 'paused_lists', False)
        self.lanes[0]['hold'] = 'challenge'
        self.assertIsNone(self.summary()['eta'])
        self.lanes[0]['hold'] = None
        db.set_setting(self.conn, 'cooldown', accounts.iso(self.now + timedelta(minutes=30)))
        self.assertEqual(self.summary()['message'], 'Waiting for Instagram')
        db.set_setting(self.conn, 'cooldown', None)
        self.lanes[0]['budget']['list'] = 6
        self.assertIn('Daily limits', self.summary()['message'])
        self.lanes[0]['budget']['list'] = 7
        self.assertIsNone(self.summary()['eta'])

    def test_stale_pages_and_short_samples_do_not_predict(self):
        self.now += timedelta(minutes=6)
        self.assertIsNone(self.summary()['eta'])
        self.now -= timedelta(minutes=6)
        self.conn.execute("DELETE FROM collector_events WHERE at<?", (accounts.iso(self.now - timedelta(minutes=4)),))
        self.conn.commit()
        self.assertIsNone(self.summary()['eta'])

    def test_partial_capped_and_unverified_lists_are_never_finished(self):
        self.lists = [{'completion': 'partial', 'error': 'Instagram limited this list; coverage is partial.'}, {'completion': 'unverified'}, {'completion': 'blocked'}, {'completion': 'complete'}]
        out = self.summary()
        self.assertEqual((out['finished'], out['pending'], out['limited'], out['needs_review']), (1, 0, 1, 2))
        self.assertNotEqual(out['message'], 'Current queue finished')
        self.assertIsNone(out['eta'])

    def test_second_available_lane_without_known_list_work_does_not_block_partial_eta(self):
        self.conn.execute("INSERT INTO accounts(lane_id,ig_id,role,last_seen) VALUES('second','456','both',?)", (accounts.iso(self.now),))
        self.conn.commit()
        self.lanes.append(dict(self.lanes[0], lane_id='second'))
        self.assertIsNotNone(self.summary()['eta'])

    def add_reserved_main(self):
        self.conn.execute("INSERT INTO accounts(lane_id,ig_id,role,is_main,last_seen) VALUES('main','789','both',1,?)", (accounts.iso(self.now),))
        self.conn.commit()
        self.lanes.append(dict(self.lanes[0], lane_id='main', budget={'list': 200}, today={'list': 0}))

    def test_protected_main_does_not_mask_alternate_daily_limit(self):
        self.add_reserved_main()
        self.lanes[0]['budget']['list'] = 6
        out = self.summary()
        self.assertIsNone(out['eta'])
        self.assertIn('Daily limits', out['message'])

    def test_protected_main_does_not_mask_offline_alternate(self):
        self.add_reserved_main()
        self.lanes[0]['online'] = False
        out = self.summary()
        self.assertIsNone(out['eta'])
        self.assertIn('offline', out['message'])

    def test_main_is_reserved_when_explicitly_shared_or_only_list_account(self):
        self.conn.execute("UPDATE accounts SET is_main=1 WHERE lane_id='lane'")
        self.conn.commit()
        self.assertIsNone(self.summary()['eta'])
        self.conn.execute("INSERT INTO accounts(lane_id,ig_id,role) VALUES('offline','789','both')")
        self.conn.commit()
        self.assertIsNone(self.summary()['eta'])
        db.set_setting(self.conn, 'main_list_share', .1)
        self.assertIsNone(self.summary()['eta'])

    def test_duplicate_identity_budget_and_cooldown_do_not_predict_progress(self):
        self.conn.execute("INSERT INTO accounts(lane_id,ig_id,role,paused,last_seen,budget,today) VALUES('duplicate','123','both',1,?,'{\"list\":6}','{\"list\":6}')", (accounts.iso(self.now),))
        self.conn.commit()
        self.assertIn('Daily limits', self.summary()['message'])
        self.conn.execute("UPDATE accounts SET budget=NULL,today='{}',list_cool_until=? WHERE lane_id='duplicate'", (accounts.iso(self.now + timedelta(minutes=30)),))
        self.conn.commit()
        self.assertEqual(self.summary()['message'], 'Waiting for Instagram')
        self.assertIsNone(self.summary()['eta'])

    def test_without_saved_pages_does_not_claim_measured_progress(self):
        self.conn.execute('DELETE FROM collector_events')
        self.conn.commit()
        out = self.summary()
        self.assertIsNone(out['eta'])
        self.assertNotIn('Measuring', out['message'])

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server import Base


class PageProgressMeasurementTest(Base):
    def test_persisted_measurement_counts_distinct_run_members_not_repeated_rows(self):
        self.call('/api/scraper/seeds', {'handles': ['seed'], 'directions': ['following']})
        path = '/api/ext/next?lane=measured&ig_id=123&handle=viewer'
        for index in range(2):
            job = self.call(path)[1]['job']
            code, out = self.call('/api/ext/list-page', {
                'lane_id': 'measured', 'account': {'ig_id': '123', 'handle': 'viewer'},
                'job_id': job['id'], 'seed': 'seed', 'direction': 'following',
                'users': [{'ig_id': '77', 'handle': 'same.person'}, {'ig_id': '77', 'handle': 'same.person'}],
                'next_cursor': 'page' + str(index + 1), 'done': False,
            })
            self.assertEqual(code, 200, out)
        rows = self.conn.execute("SELECT returned_count,saved_entries FROM collector_events WHERE kind='list' ORDER BY id").fetchall()
        self.assertEqual([tuple(row) for row in rows], [(2, 1), (2, 0)])
