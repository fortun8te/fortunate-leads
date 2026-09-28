"""Offline trials must stay fresh, pinned, paced, and count real completed runs."""
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ops'))
import db
import server
import accounts
import assign_page_experiment
import collection_benchmark

class FollowingPageExperimentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.init(Path(self.tmp.name) / 'trial.sqlite')
        self.addCleanup(self.conn.close)
        accounts.touch(self.conn, 'alt', {'ig_id':'101','handle':'alt'}, version='3.9.21')
        for i,target in enumerate(['sizefifty','sizehundred','sizetwohundred']):
            db.upsert_person(self.conn, {'handle':target,'ig_id':str(900+i),'is_private':False,'following':300})
        self.targets=['sizefifty','sizehundred','sizetwohundred']
        self.conn.commit()
    def assign(self):
        assign_page_experiment.following_trial(self.conn,self.targets,'101',apply=True)
        self.conn.commit()
    def next(self):
        return server.ext_next(self.conn,{'lane':['alt'],'kinds':['list']},{'version':'3.9.21','account':{'ig_id':'101','handle':'alt'}})['job']
    def body(self,job,final=False):
        return {'job_id':job['id'],'lease_token':job['lease_token'],'requested_cursor':job['cursor'],
                'seed':job['seed'],'direction':'following','ig_id':job['ig_id'],
                'account':{'ig_id':'101','handle':'alt'},'requested_count':job['page_size'],'http_status':200,
                'users':[{'handle':'person'+str(i),'ig_id':str(i)} for i in ([11,12] if final else [10,11])],
                'done':final,'has_more':not final,'next_cursor':None if final else 'opaque',
                'total':3,'total_source':'current_run'}
    def test_dry_run_is_read_only_and_apply_creates_three_fresh_immutable_arms(self):
        plan=assign_page_experiment.following_trial(self.conn,self.targets,'101')
        self.assertEqual([p['requested_count'] for p in plan],[50,100,200])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
        self.assign()
        rows=self.conn.execute('SELECT page_size,experiment_viewer_ig_id,cursor FROM jobs JOIN lists USING(seed,direction) ORDER BY jobs.id').fetchall()
        self.assertEqual([tuple(r) for r in rows],[(50,'101',None),(100,'101',None),(200,'101',None)])
        with self.assertRaises(ValueError): assign_page_experiment.following_trial(self.conn,self.targets,'101',apply=True)
    def test_three_complete_runs_count_duplicates_and_200_request_correctly(self):
        self.assign()
        for size in [50,100,200]:
            job=self.next(); self.assertEqual(job['page_size'],size)
            server.ext_list_page(self.conn,{'lane':['alt']},self.body(job))
            job=self.next(); self.assertEqual(job['cursor'],'opaque')
            server.ext_list_page(self.conn,{'lane':['alt']},self.body(job,True))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM lists WHERE state='done'").fetchone()[0],3)
        rows=self.conn.execute('SELECT requested_count,sum(returned_count),sum(new_links) FROM collector_events GROUP BY requested_count ORDER BY requested_count').fetchall()
        self.assertEqual([tuple(r) for r in rows],[(50,4,3),(100,4,3),(200,4,3)])
        self.assertEqual(self.conn.execute('SELECT count(DISTINCT person_id) FROM list_members').fetchone()[0],3)
        now=datetime.now(timezone.utc)
        benchmark=collection_benchmark.report(self.conn,now-timedelta(hours=1),now)
        self.assertEqual(benchmark['experiment_direction'],'following')
        self.assertEqual(sum(a['current_done_jobs'] for a in benchmark['experiment_cohort_current']),3)
        self.assertEqual([a['page_size'] for a in benchmark['experiment_cohort_current']],[50,100,200])
        self.assertEqual([a['new_links'] for a in benchmark['experiment_in_window']],[3,3,3])
        self.assertEqual(benchmark['experiment_people_in_window']['unique_people'],3)
        self.assertEqual([a['rows_per_accepted_page'] for a in benchmark['experiment_in_window']],[2,2,2])
    def test_page_size_or_viewer_change_cannot_import(self):
        self.assign();job=self.next()
        for field,value in [('requested_count',100),('account',{'ig_id':'202'})]:
            body=self.body(job);body[field]=value
            with self.assertRaises(server.Bad):server.ext_list_page(self.conn,{'lane':['alt']},body)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0],0)
    def test_failed_trial_never_restarts_as_normal_list_or_other_direction(self):
        self.assign()
        self.conn.execute("UPDATE jobs SET state='error'")
        self.conn.execute("UPDATE lists SET state='error'")
        db.repair_lists(self.conn)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0],3)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM lists WHERE direction='followers'").fetchone()[0],0)
    def test_pinned_permit_denies_changed_viewer_and_old_client(self):
        self.assign();job=self.next()
        body=dict(action='acquire',kind='list',job_id=job['id'],lease_token=job['lease_token'],account={'ig_id':'202'})
        self.assertFalse(server.ext_request(self.conn,{'lane':['alt']},body)['granted'])
        body['account']={'ig_id':'101'}
        self.conn.execute("UPDATE accounts SET version='3.9.19' WHERE lane_id='alt'");self.conn.commit()
        self.assertFalse(server.ext_request(self.conn,{'lane':['alt']},body)['granted'])
    def test_existing_workspace_hold_still_blocks_permit(self):
        self.assign();job=self.next()
        until=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        self.conn.execute('UPDATE accounts SET list_cool_until=? WHERE lane_id=?',(until,'alt'));self.conn.commit()
        body=dict(action='acquire',kind='list',job_id=job['id'],lease_token=job['lease_token'],account={'ig_id':'101'})
        self.assertFalse(server.ext_request(self.conn,{'lane':['alt']},body)['granted'])

    def page(self, job, number):
        body = self.body(job)
        body.update(next_cursor='cursor' + str(number), total=10000,
                    users=[{'handle':f"member{job['id']}_{number}_{i}",
                            'ig_id':str(10000 + job['id'] * 100 + number * 2 + i)} for i in range(2)])
        return body

    def error(self, job, code='other', **changes):
        body = {'job_id':job['id'],'lease_token':job['lease_token'],'code':code,
                'account':{'ig_id':'101','handle':'alt'}, 'message':'trial warning',
                'event_id':'warning-' + str(job['id'])}
        body.update(changes)
        return server.ext_error(self.conn, {'lane':['alt']}, body)

    def assert_parked(self, saved_seed=None):
        jobs = self.conn.execute('SELECT state,leased_until,lease_token,retry_not_before FROM jobs '
                                 'WHERE experiment_viewer_ig_id IS NOT NULL').fetchall()
        self.assertEqual([tuple(row) for row in jobs], [('error',None,None,None)] * 3)
        for row in self.conn.execute('SELECT * FROM lists'):
            self.assertEqual(row['state'], 'partial' if row['seed'] == saved_seed else 'paused')
            self.assertEqual(row['released_why'], 'trial_stopped')
            self.assertIn('Following page trial stopped:', row['error'])

    def test_each_arm_stops_after_four_saved_pages_without_stopping_other_arms(self):
        self.assign()
        for size in [50,100,200]:
            for number in range(1,5):
                job = self.next()
                self.assertEqual(job['page_size'], size)
                body = self.page(job, number)
                result = server.ext_list_page(self.conn, {'lane':['alt']}, body)
                self.assertEqual(bool(result.get('trial_stopped')), number == 4)
            parked = self.conn.execute('SELECT state,cursor,received FROM lists WHERE seed=?', (job['seed'],)).fetchone()
            self.assertEqual(tuple(parked), ('partial','cursor4',8))
            self.assertFalse(db.list_run_complete(self.conn, job['id']))
            self.assertEqual(server.ext_list_page(self.conn, {'lane':['alt']}, body)['duplicate'], True)
            body.update(requested_cursor='cursor4',next_cursor='cursor5')
            self.assertTrue(server.ext_list_page(self.conn, {'lane':['alt']}, body)['stale'])
        self.assertIsNone(self.next())
        self.assertEqual(self.conn.execute("SELECT count(*) FROM collector_events WHERE outcome='page'").fetchone()[0],12)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0],24)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM list_members').fetchone()[0],24)
        self.conn.execute("UPDATE lists SET updated_at='2000-01-01T00:00:00+00:00'")
        db.repair_lists(self.conn)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0],3)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','leased')").fetchone()[0],0)

    def test_ordinary_trial_error_parks_cohort_and_preserves_collected_prefix(self):
        self.assign()
        job = self.next()
        server.ext_list_page(self.conn, {'lane':['alt']}, self.page(job,1))
        job = self.next()
        self.error(job)
        self.assert_parked(job['seed'])
        saved = self.conn.execute('SELECT cursor,received,run_job_id FROM lists WHERE seed=?', (job['seed'],)).fetchone()
        self.assertEqual(tuple(saved), ('cursor1',2,job['id']))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM current_edges').fetchone()[0],2)
        self.assertIsNone(self.next())

    def test_rate_limit_stops_trial_and_keeps_longest_real_cooldown(self):
        self.assign(); job = self.next()
        until = (datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()
        db.set_setting(self.conn, 'cooldown', until); self.conn.commit()
        self.error(job, 'rate_limit', retry_after=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
        self.assert_parked()
        self.assertEqual(db.get_setting(self.conn,'cooldown'), until)
        row = self.conn.execute("SELECT list_cool_until FROM accounts WHERE lane_id='alt'").fetchone()
        self.assertEqual(row['list_cool_until'], until)

    def test_security_warning_stops_trial_and_retains_manual_pause(self):
        self.assign(); job = self.next()
        self.error(job, 'challenge')
        self.assert_parked()
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))
        self.assertTrue(db.get_setting(self.conn,'paused_bios'))
        self.assertEqual(self.conn.execute("SELECT hold FROM accounts WHERE lane_id='alt'").fetchone()[0], 'challenge')

    def test_warning_from_ordinary_job_also_stops_latest_trial(self):
        self.assign()
        db.queue_list(self.conn,'ordinaryseed','following',priority=10000)
        self.conn.commit()
        job = self.next(); self.assertEqual(job['seed'],'ordinaryseed')
        self.error(job, 'soft_block')
        states = self.conn.execute('SELECT state FROM jobs WHERE experiment_viewer_ig_id IS NOT NULL').fetchall()
        self.assertEqual([row[0] for row in states], ['error']*3)
        ordinary = self.conn.execute('SELECT state,retry_not_before FROM jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual(ordinary['state'],'queued')
        self.assertIsNotNone(ordinary['retry_not_before'])
        self.assertIsNotNone(db.get_setting(self.conn,'cooldown'))

    def test_ordinary_limited_page_also_stops_latest_trial(self):
        self.assign()
        db.queue_list(self.conn,'ordinaryseed','following',priority=10000)
        self.conn.commit()
        job = self.next(); self.assertEqual(job['seed'],'ordinaryseed')
        body = self.page(job,1)
        body.update(limited=True,done=True)
        result = server.ext_list_page(self.conn,{'lane':['alt']},body)
        self.assertTrue(result['trial_stopped'])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE experiment_viewer_ig_id IS NOT NULL AND state='error'").fetchone()[0],3)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0],2)
        self.assertEqual(self.conn.execute("SELECT state FROM lists WHERE seed='ordinaryseed'").fetchone()[0],'partial')

    def test_stale_error_and_mismatched_viewer_do_not_stop_cohort(self):
        self.assign(); job = self.next()
        self.assertTrue(self.error(job,lease_token='old-token')['stale'])
        self.error(job,event_id='mismatched',account={'ig_id':'202'})
        states = self.conn.execute('SELECT state FROM jobs WHERE experiment_viewer_ig_id IS NOT NULL').fetchall()
        self.assertTrue(all(row[0] in ('queued','leased') for row in states))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM lists WHERE released_why='trial_stopped'").fetchone()[0],0)

    def test_old_warning_replay_cannot_stop_a_new_cohort(self):
        self.assign(); old_job = self.next()
        self.error(old_job)
        targets = ['newfifty','newhundred','newtwohundred']
        for i, target in enumerate(targets):
            db.upsert_person(self.conn, {'handle':target,'ig_id':str(800+i),'is_private':False,'following':300})
        assign_page_experiment.following_trial(self.conn,targets,'101',apply=True)
        self.conn.commit()
        self.assertTrue(self.error(old_job)['duplicate'])
        self.assertTrue(self.error(old_job,event_id='different-old-event')['stale'])
        latest = db.get_setting(self.conn,'page_experiment_latest')['job_ids']
        for job_id in latest:
            self.assertEqual(self.conn.execute('SELECT state FROM jobs WHERE id=?',(job_id,)).fetchone()[0],'queued')

    def test_reported_network_error_is_terminal_for_trial(self):
        self.assign(); job = self.next()
        self.error(job,'network')
        self.assert_parked()
        self.assertIsNone(self.next())

    def test_repeated_cursor_saves_returned_people_then_stops_all_arms(self):
        self.assign(); job = self.next()
        server.ext_list_page(self.conn, {'lane':['alt']}, self.page(job,1))
        job = self.next()
        body = self.page(job,2)
        body['next_cursor'] = 'cursor1'
        result = server.ext_list_page(self.conn, {'lane':['alt']}, body)
        self.assertTrue(result['stalled'])
        self.assertTrue(result['trial_stopped'])
        self.assert_parked(job['seed'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0],4)
        self.assertEqual(self.conn.execute('SELECT cursor FROM lists WHERE seed=?',(job['seed'],)).fetchone()[0],'cursor1')

    def test_limited_page_saves_people_then_stops_cohort_without_claiming_completion(self):
        self.assign(); job = self.next()
        body = self.page(job,1)
        body.update(limited=True,done=True)
        result = server.ext_list_page(self.conn, {'lane':['alt']}, body)
        self.assertTrue(result['trial_stopped'])
        self.assert_parked(job['seed'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0],2)
        self.assertFalse(db.list_run_complete(self.conn,job['id']))

    def test_cohort_stop_preserves_verified_complete_arm(self):
        self.assign(); job = self.next()
        server.ext_list_page(self.conn,{'lane':['alt']},self.body(job))
        job = self.next()
        server.ext_list_page(self.conn,{'lane':['alt']},self.body(job,True))
        completed_id = job['id']
        self.error(self.next())
        row = self.conn.execute('SELECT state FROM jobs WHERE id=?',(completed_id,)).fetchone()
        self.assertEqual(row['state'],'done')
        self.assertTrue(db.list_run_complete(self.conn,completed_id))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE state='error'").fetchone()[0],2)
