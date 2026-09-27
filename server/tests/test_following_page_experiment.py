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
        accounts.touch(self.conn, 'alt', {'ig_id':'101','handle':'alt'}, version='3.9.20')
        for i,target in enumerate(['sizefifty','sizehundred','sizetwohundred']):
            db.upsert_person(self.conn, {'handle':target,'ig_id':str(900+i),'is_private':False,'following':300})
        self.targets=['sizefifty','sizehundred','sizetwohundred']
        self.conn.commit()
    def assign(self):
        assign_page_experiment.following_trial(self.conn,self.targets,'101',apply=True)
        self.conn.commit()
    def next(self):
        return server.ext_next(self.conn,{'lane':['alt'],'kinds':['list']},{'version':'3.9.20','account':{'ig_id':'101','handle':'alt'}})['job']
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
