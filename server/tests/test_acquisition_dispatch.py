"""Exercise experimental selection through the guarded production scheduler."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounts
import acquisition_dispatch as dispatch
import db


class AcquisitionDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.init(str(Path(self.tmp.name) / 'dispatch.sqlite'))
        self.addCleanup(self.conn.close)
        self.now = datetime.now(timezone.utc)
        accounts.touch(self.conn, 'lane', {'ig_id':'101','handle':'alternate'}, version='3.9.31')
        dispatch.set_policy(self.conn, dispatch.ADAPTIVE)
        self.a = self.add_list('first')
        self.b = self.add_list('second')
        self.tick = 0
        self.metrics = {}
        self.metric_mock = patch('acquisition_dispatch.acquisition.recent_job_metrics',
                                 side_effect=lambda conn, job, limit=20: self.metrics.get(job, []))
        self.metric_mock.start()
        self.addCleanup(self.metric_mock.stop)

    def add_list(self, handle, direction='following', priority=0):
        db.queue_list(self.conn, handle, direction, priority=priority)
        return self.conn.execute('SELECT max(id) FROM jobs').fetchone()[0]

    def page(self, job):
        self.tick += 1
        at = accounts.iso(self.now + timedelta(seconds=self.tick))
        self.conn.execute("INSERT INTO collector_events(at,lane,job_id,kind,outcome) VALUES(?,'lane',?,'list','page')", (at,job))
        self.conn.execute("UPDATE lists SET lane='lane',cursor=?,state='running' WHERE seed=(SELECT seed FROM jobs WHERE id=?)", (str(self.tick),job))

    def yield_for(self, job, amount):
        self.metrics[job] = [dict(source_kind='collector_page', new_canonical_edges=amount,
                                  new_nodes=amount, ingested_at=accounts.iso(self.now))]

    def pick(self):
        return accounts.pick_job(self.conn,'lane',['list'],self.now+timedelta(seconds=self.tick+1))

    def test_second_unowned_automatic_target_can_receive_exploration_turn(self):
        self.conn.execute('UPDATE jobs SET priority=-10')
        for job, handle in ((self.a,'first'),(self.b,'second')):
            self.conn.execute("INSERT INTO collection_discovery(handle,state,at,reason,directions,job_id) VALUES(?,'queued',?,'network','[]',?)", (handle,accounts.iso(self.now),job))
        self.page(self.a)
        self.yield_for(self.a,200)
        db.set_setting(self.conn,dispatch.TURN_PREFIX+'lane',3)
        self.assertIsNone(self.conn.execute("SELECT lane FROM lists WHERE seed='second'").fetchone()[0])
        self.assertEqual(self.pick()['id'], self.b)

    def test_saved_four_page_quantum_cannot_be_undone_by_higher_yield(self):
        for _ in range(4):
            self.page(self.a)
        self.yield_for(self.a,500)
        self.yield_for(self.b,1)
        self.assertEqual(self.pick()['id'],self.b)
        self.assertEqual(self.conn.execute("SELECT cursor FROM lists WHERE seed='first'").fetchone()[0],'4')

    def test_quantum_does_not_block_only_eligible_job(self):
        for _ in range(4):
            self.page(self.a)
        self.conn.execute("UPDATE jobs SET retry_not_before=? WHERE id=?",(accounts.iso(self.now+timedelta(hours=1)),self.b))
        self.assertEqual(self.pick()['id'],self.a)

    def test_explicit_positive_priority_keeps_existing_selection(self):
        self.conn.execute('UPDATE jobs SET priority=50 WHERE id=?',(self.a,))
        self.yield_for(self.a,1)
        self.yield_for(self.b,500)
        self.assertEqual(self.pick()['id'],self.a)

    def test_pinned_following_trial_and_unpinned_follower_trial_stay_in_order(self):
        self.yield_for(self.a,1)
        self.yield_for(self.b,500)
        self.conn.execute("UPDATE jobs SET page_size=100,experiment_viewer_ig_id='101' WHERE id=?",(self.a,))
        self.assertEqual(self.pick()['id'],self.a)
        self.conn.execute("UPDATE jobs SET page_size=25,experiment_viewer_ig_id=NULL,direction='followers' WHERE id=?",(self.a,))
        self.conn.execute("UPDATE lists SET direction='followers' WHERE seed='first'")
        self.page(self.a)  # Existing scheduler sticks to this trial before quantum.
        self.assertEqual(self.pick()['id'],self.a)

    def test_later_trial_cannot_jump_ahead_on_yield(self):
        self.conn.execute('UPDATE jobs SET page_size=25,direction=\'followers\' WHERE id=?',(self.b,))
        self.conn.execute("UPDATE lists SET direction='followers' WHERE seed='second'")
        self.yield_for(self.a,1)
        self.yield_for(self.b,500)
        self.assertEqual(self.pick()['id'],self.a)

    def test_following_fallback_priority_survives_follower_yield(self):
        self.conn.execute("UPDATE jobs SET direction='followers' WHERE id=?",(self.b,))
        self.conn.execute("UPDATE lists SET direction='followers' WHERE seed='second'")
        for i in range(3):
            self.conn.execute("INSERT INTO lists(seed,direction,state,error,updated_at) VALUES(?,'followers','partial','list_html_home_redirect',?)",('failed'+str(i),accounts.iso(self.now)))
        self.yield_for(self.a,1)
        self.yield_for(self.b,500)
        self.assertEqual(self.pick()['id'],self.a)

    def test_polls_are_read_only_and_only_real_ordinary_takes_advance_lane_counter(self):
        before = self.conn.total_changes
        picked = self.pick()
        self.pick()
        self.assertEqual(self.conn.total_changes,before)
        self.assertEqual(dispatch.admission_turn(self.conn,'lane'),0)
        accounts.took(self.conn,'lane',picked)
        self.assertEqual(dispatch.admission_turn(self.conn,'lane'),1)
        self.assertEqual(dispatch.admission_turn(self.conn,'other'),0)
        for _ in range(3):
            accounts.took(self.conn,'lane',picked)
        self.assertEqual(dispatch.admission_turn(self.conn,'lane'),0)
        self.conn.execute('UPDATE jobs SET priority=10 WHERE id=?',(picked['id'],))
        accounts.took(self.conn,'lane',self.pick())
        self.assertEqual(dispatch.admission_turn(self.conn,'lane'),0)

    def test_policy_off_keeps_original_selection_and_no_counter(self):
        dispatch.set_policy(self.conn,dispatch.CURRENT)
        self.yield_for(self.a,1)
        self.yield_for(self.b,500)
        picked = self.pick()
        self.assertEqual(picked['id'],self.a)
        accounts.took(self.conn,'lane',picked)
        self.assertEqual(dispatch.admission_turn(self.conn,'lane'),0)

    def test_other_healthy_lane_ownership_is_still_filtered_by_scheduler(self):
        accounts.touch(self.conn,'other',{'ig_id':'102','handle':'otheralternate'},version='3.9.31')
        self.conn.execute("UPDATE lists SET lane='other' WHERE seed='second'")
        self.yield_for(self.a,1)
        self.yield_for(self.b,500)
        self.assertEqual(self.pick()['id'],self.a)


if __name__ == '__main__':
    unittest.main()
