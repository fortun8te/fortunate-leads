import sqlite3
import json
import subprocess
from unittest import mock
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import edge_benchmark as b


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.live=Path(self.tmp.name)/'live.db'
        self.bench=Path(self.tmp.name)/'bench.db'
        with sqlite3.connect(self.live) as db:
            db.executescript('CREATE TABLE people(id INTEGER PRIMARY KEY,ig_id TEXT,handle TEXT,following INT,is_private INT,is_verified INT); CREATE TABLE seeds(handle TEXT,ig_id TEXT); CREATE TABLE edges(seed TEXT,person_id INT,direction TEXT);')
            for i in range(12):
                db.execute('INSERT INTO people VALUES(?,?,?,?,0,?)',(i+1,str(i+100),'p'+str(i),[100,1000,3000][i//4],i%2))
            db.execute("INSERT INTO seeds VALUES('p0','100')")
            db.execute("INSERT INTO seeds VALUES('p1','101')")
            db.execute("INSERT INTO edges VALUES('p0',2,'following')")
            db.execute("INSERT INTO edges VALUES('p1',1,'followers')")
        b.create_plan(self.live,self.bench,'999')
    def tearDown(self):
        self.tmp.cleanup()
    def next(self,now=1):
        task=b.state(self.bench)['current_task']
        return b.next_task(self.bench,'lane', '999',transport=task['transport'],now=now)
    def warmups(self):
        for i in range(7):
            t=self.next()
            b.begin_request(self.bench,'w'+str(i),t['task_id'],'999',started_at=2)
            b.finish_request(self.bench,'w'+str(i),rows=['800'],finished_at=3)
        self.assertIsNone(self.next())
        b.advance_phase(self.bench,now=4)
    def test_baseline_dedup_orientations_and_isolation(self):
        self.assertEqual(b.state(self.bench)['baseline_count'],1)
        with sqlite3.connect(self.live) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM edges').fetchone()[0],2)
    def test_warmup_and_idempotent_and_wait_denominator(self):
        self.warmups()
        self.assertEqual(sum(x['novel_pairs'] for x in b.report(self.bench)['arms'].values()),0)
        t=self.next(now=5)
        b.begin_request(self.bench,'a',t['task_id'],'999',started_at=6)
        self.assertFalse(b.begin_request(self.bench,'a',t['task_id'],'999')['send_allowed'])
        r=b.finish_request(self.bench,'a',rows=['800','800'],next_cursor='c1',has_more=True,finished_at=10,waits={'pacing':4})
        self.assertEqual(r['novel_pairs'],1)
        self.assertEqual(b.finish_request(self.bench,'a',rows=['801'],finished_at=11),r)
        rep=b.report(self.bench,now=20)
        self.assertEqual(rep['wall_seconds_including_all_waits'],19)
        self.assertEqual(rep['arms'][t['arm']]['waits']['pacing'],4)
        t2=self.next(now=21)
        self.assertEqual(t2['cursor'],'c1')
        b.begin_request(self.bench,'b',t2['task_id'],'999',started_at=22)
        r=b.finish_request(self.bench,'b',rows=['800'],next_cursor='c1',has_more=True,finished_at=23)
        self.assertEqual(r['terminal_warning'],'repeated_cursor')
        self.assertIsNone(self.next(now=24))
    def test_crash_stops_no_replay(self):
        t=self.next()
        b.begin_request(self.bench,'a',t['task_id'],'999',started_at=2)
        self.assertIsNone(self.next(now=3))
        self.assertEqual(b.recover_outstanding(self.bench,now=4),['a'])
        self.assertEqual(b.state(self.bench)['state'],'stopped')
        self.assertIsNone(self.next(now=5))
    def test_corpus_rotation_is_deterministic(self):
        other=Path(self.tmp.name)/'other.db'
        b.create_plan(self.live,other,'999')
        self.assertEqual(b.state(other)['corpus'],b.state(self.bench)['corpus'])
        with b._connect(self.bench) as db:
            starts=[r[0] for r in db.execute('SELECT arm FROM tasks WHERE warmup=0 GROUP BY target_id ORDER BY min(task_id)')]
        self.assertEqual(len(set(starts)),7)
    def test_novelty_dedup_across_orientations_and_independent_arms(self):
        self.warmups()
        with b._connect(self.bench) as db:
            ids=[r[0] for r in db.execute('SELECT task_id FROM tasks WHERE warmup=0 ORDER BY task_id LIMIT 3')]
            db.execute("UPDATE tasks SET arm='web_rest50',target_id='100',direction='following' WHERE task_id=?",(ids[0],))
            db.execute("UPDATE tasks SET arm='web_rest50',target_id='800',direction='followers' WHERE task_id=?",(ids[1],))
            db.execute("UPDATE tasks SET arm='web_rest100',target_id='100',direction='following' WHERE task_id=?",(ids[2],))
        counts=[]
        for i,rows in enumerate((['101','800'],['100'],['800'])):
            t=self.next(now=5+i*2)
            b.begin_request(self.bench,'m'+str(i),t['task_id'],'999',started_at=5+i*2)
            counts.append(b.finish_request(self.bench,'m'+str(i),rows=rows,finished_at=6+i*2)['novel_pairs'])
        self.assertEqual(counts,[1,0,1])

    def test_zero_actual_send_stops_and_is_separate_from_attempts(self):
        t=self.next()
        b.begin_request(self.bench,'zero',t['task_id'],'999',started_at=2)
        b.finish_request(self.bench,'zero',rows=['800'],actual_http_requests=0,finished_at=3)
        rep=b.report(self.bench,now=4)
        self.assertEqual(rep['plan']['state'],'stopped')
        self.assertEqual(rep['transport_totals']['registered_attempts'],1)
        self.assertEqual(rep['transport_totals']['confirmed_http_requests'],0)
        self.assertEqual(rep['transport_totals']['local_no_send'],1)

    def test_invalid_ids_stop_and_zero_send_not_counted(self):
        t=self.next()
        b.begin_request(self.bench,'a',t['task_id'],'999',started_at=2)
        r=b.finish_request(self.bench,'a',rows=['not_id'],finished_at=3)
        self.assertEqual(r['terminal_warning'],'malformed_numeric_id')
        self.assertEqual(b.state(self.bench)['state'],'stopped')

    def test_report_uses_actual_http_and_excludes_warmup(self):
        self.warmups()
        t=self.next(now=10)
        b.begin_request(self.bench,'measured',t['task_id'],'999',started_at=10)
        b.finish_request(self.bench,'measured',rows=['800','800','801'],finished_at=12,
                         duration_ms=250,waits={'pacing':6})
        r=b.report(self.bench,now=12)['arms'][t['arm']]
        self.assertEqual(r['confirmed_http_requests'],1)
        self.assertEqual(r['novel_pairs'],2)
        self.assertEqual(r['rows_per_http_request'],3)
        self.assertAlmostEqual(r['duplicate_edge_percent'],100/3)
        self.assertEqual(r['allocated_wall_seconds'],8)
        self.assertEqual(r['observed_unique_edges_per_allocated_hour'],900)
        self.assertEqual(r['latency_p50_seconds'],.25)
        self.assertEqual(r['latency_p95_seconds'],.25)
        self.assertEqual(r['pagination_completed'],1)
        self.assertEqual(r['waits']['pacing'],6)

    def test_large_chrome_preset_is_bounded_and_frozen(self):
        before=self.bench.read_bytes()
        other=Path(self.tmp.name)/'large.db'
        rep=b.create_plan(self.live,other,'999',preset='chrome-large-following',target_ids=['108','109'])
        self.assertEqual(set(rep['arms']),{'web_rest200','web_rest300','web_rest500','web_rest1500'})
        self.assertEqual(rep['plan']['max_requests'],20)
        self.assertEqual(rep['plan']['window_seconds'],900)
        self.assertEqual(rep['plan']['corpus_mode'],'explicit_ids')
        with b._connect(other) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],12)
            self.assertEqual({r[0] for r in db.execute('SELECT DISTINCT direction FROM tasks')},{'following'})
            self.assertEqual({r[0] for r in db.execute('SELECT DISTINCT transport FROM tasks')},{'chrome'})
        self.assertEqual(self.bench.read_bytes(),before)
        with self.assertRaisesRegex(ValueError,'already exists'):
            b.create_plan(self.live,other,'999',preset='followers-feasibility')

    def test_follower_preset_and_custom_arms(self):
        other=Path(self.tmp.name)/'followers.db'
        rep=b.create_plan(self.live,other,'999',preset='followers-feasibility')
        self.assertEqual(rep['plan']['max_requests'],35)
        self.assertEqual(rep['plan']['directions'],['followers'])
        self.assertEqual(len(rep['plan']['corpus']),2)
        native=next(arm for arm in rep['plan']['arms'] if arm[0]=='mobile_graphql')
        self.assertIsNone(native[2])
        custom=Path(self.tmp.name)/'custom.db'
        rep=b.create_plan(self.live,custom,'999',arms=['web_rest200','web_rest500'],target_ids=['108'])
        self.assertEqual(rep['plan']['max_requests'],6)
        self.assertEqual(set(rep['arms']),{'web_rest200','web_rest500'})

    def test_feasibility_rejects_ineligible_targets_and_unapproved_sizes(self):
        for kwargs in [dict(target_ids=['999']),dict(target_ids=['123456']),dict(arms=['mobile_rest500']),
                       dict(arms=['web_rest1500'],directions=('followers',)),dict(target_count=4),
                       dict(preset='chrome-large-following',max_requests=100)]:
            with self.assertRaises(ValueError):
                b.create_plan(self.live,Path(self.tmp.name)/'bad.db','999',**kwargs)
        with sqlite3.connect(self.live) as db:
            db.execute("UPDATE people SET is_private=1 WHERE ig_id='108'")
        with self.assertRaisesRegex(ValueError,'ineligible'):
            b.create_plan(self.live,Path(self.tmp.name)/'private.db','999',target_ids=['108'])

    def test_cli_creates_ready_opt_in_plan_without_live_writes(self):
        live_before=self.live.read_bytes()
        destination=Path(self.tmp.name)/'cli.db'
        script=Path(__file__).resolve().parents[2]/'ops'/'edge_benchmark.py'
        result=subprocess.run([sys.executable,str(script),'create','--live-db',str(self.live),
                               '--bench-db',str(destination),'--viewer-id','999',
                               '--arms','web_rest200,web_rest300,web_rest500,web_rest1500',
                               '--direction','following','--target-ids','108,109'],
                              check=True,text=True,capture_output=True)
        plan=json.loads(result.stdout)['plan']
        self.assertEqual(plan['state'],'ready')
        self.assertEqual(plan['max_requests'],20)
        self.assertEqual(plan['directions'],['following'])
        self.assertEqual(self.live.read_bytes(),live_before)

    def test_report_backwards_compatible_with_frozen_legacy_metadata(self):
        with b._connect(self.bench) as db:
            meta=b._meta(db)
            meta.pop('arms')
            meta['version']=1
            b._save(db,meta)
        before=self.bench.read_bytes()
        self.assertEqual(set(b.report(self.bench)['arms']),{a[0] for a in b.ARMS})
        self.assertEqual(self.bench.read_bytes(),before)

    def protocol(self):
        self.bench=Path(self.tmp.name)/'protocol.db'
        return b.create_plan(self.live,self.bench,'999',preset='followers-protocol',target_ids=['108','109','110'])

    def test_protocol_fixed_arms_conditional_search_and_incomplete_comparison(self):
        rep=self.protocol()
        self.assertEqual(set(rep['arms']),{'web_rest50','web_modal','mobile_rest50','mobile_graphql','web_search50'})
        self.assertEqual(rep['plan']['directions'],['followers'])
        self.assertEqual(rep['plan']['arm_request_budgets']['web_search50'],7)
        with b._connect(self.bench) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks WHERE arm='web_search50' AND state='deferred'").fetchone()[0],4)
        self.assertTrue(all(v['status']=='inconclusive' for v in rep['protocol_comparison']['arms'].values()))
        with self.assertRaisesRegex(ValueError,'evidence'):
            b.enable_fallback(self.bench,'try search',query='a')
        with self.assertRaises(ValueError):
            b.create_plan(self.live,Path(self.tmp.name)/'fourth.db','999',preset='followers-protocol',cohort_index=4)

    def test_protocol_target_cap_preserves_rows_and_does_not_hold_viewer(self):
        self.protocol()
        t=self.next()
        b.begin_request(self.bench,'cap',t['task_id'],'999',started_at=2)
        r=b.finish_request(self.bench,'cap',rows=['800'],status='target_cap',target_limited=True,raw_returned_count=5,finished_at=3)
        self.assertEqual((r['stop_scope'],r['observed_pairs'],r['returned_rows']),('target',1,5))
        self.assertFalse(r['viewer_safety_hold'])
        self.assertEqual(b.state(self.bench)['state'],'running')
        self.assertIsNotNone(self.next(now=4))
        b.enable_fallback(self.bench,'recorded target limit',query='a')
        self.assertEqual(b.state(self.bench)['enabled_conditional_arms'],['web_search50'])

    def test_protocol_429_stops_arm_and_holds_all_requests(self):
        self.protocol()
        t=self.next()
        b.begin_request(self.bench,'rate',t['task_id'],'999',started_at=2)
        r=b.finish_request(self.bench,'rate',status='rate_limit',http_status=429,finished_at=3)
        self.assertEqual(r['stop_scope'],'arm')
        self.assertTrue(r['viewer_safety_hold'])
        self.assertIsNone(self.next(now=4))
        self.assertEqual(b.state(self.bench)['stopped_arms'][t['arm']]['request_index'],1)
        b.resolve_safety_hold(self.bench,'provider hold independently reviewed')
        following=self.next(now=5)
        self.assertIsNotNone(following)
        self.assertNotEqual(following['arm'],t['arm'])

    def test_protocol_auth_is_global_and_not_resumable(self):
        self.protocol()
        t=self.next()
        b.begin_request(self.bench,'auth',t['task_id'],'999',started_at=2)
        r=b.finish_request(self.bench,'auth',status='challenge',finished_at=3)
        self.assertEqual(r['stop_scope'],'cohort')
        self.assertEqual(b.state(self.bench)['state'],'stopped')
        with self.assertRaises(ValueError):
            b.resolve_safety_hold(self.bench,'cannot bypass challenge')

    def test_protocol_comparison_waits_for_full_corpus_and_both_twenty_percent_gains(self):
        self.protocol()
        with b._connect(self.bench) as db:
            meta=b._meta(db)
            arms={name:{'unique_edges_per_http_request':12,'observed_unique_edges_per_allocated_hour':120} for name,_,_ in meta['arms']}
            arms['web_rest50']={'unique_edges_per_http_request':10,'observed_unique_edges_per_allocated_hour':100}
            db.execute("UPDATE tasks SET state='done',pages=1 WHERE warmup=0 AND arm IN ('web_rest50','mobile_rest50')")
            result=b._protocol_comparison(db,meta,arms)
            self.assertEqual(result['arms']['mobile_rest50']['status'],'bounded_improvement')
            self.assertEqual(result['arms']['web_modal']['status'],'inconclusive')
            arms['mobile_rest50']['observed_unique_edges_per_allocated_hour']=110
            self.assertEqual(b._protocol_comparison(db,meta,arms)['arms']['mobile_rest50']['status'],'threshold_not_met')

    def test_protocol_dispatch_enforces_complete_threshold_verdict_only(self):
        self.protocol()
        t=b.state(self.bench)['current_task']
        failed=t['arm']
        verdict={'protocol_comparison':{'arms':{failed:{'status':'threshold_not_met','full_corpus':True}}}}
        with mock.patch.object(b,'report',return_value=verdict):
            next_row=self.next()
        meta=b.state(self.bench)
        self.assertEqual(meta['stopped_arms'][failed]['reason'],'full_corpus_threshold_not_met')
        if next_row:
            self.assertNotEqual(next_row['arm'],failed)
        next_arm=meta['current_task']['arm']
        with mock.patch.object(b,'report',return_value={'protocol_comparison':{'arms':{next_arm:{'status':'inconclusive'}}}}):
            self.next(now=2)
        self.assertNotIn(next_arm,b.state(self.bench)['stopped_arms'])

    def test_protocol_per_arm_budget_prevents_additional_send(self):
        self.bench=Path(self.tmp.name)/'budget.db'
        names=b.PRESETS['followers-protocol']['arms']
        b.create_plan(self.live,self.bench,'999',preset='followers-protocol',arm_request_budgets={name:1 for name in names})
        for i in range(4):
            task=self.next()
            b.begin_request(self.bench,'warm-'+str(i),task['task_id'],'999',started_at=2)
            b.finish_request(self.bench,'warm-'+str(i),finished_at=3)
        b.advance_phase(self.bench,now=4)
        self.assertIsNone(self.next(now=5))
        self.assertTrue(any(x['reason']=='arm_request_budget' for x in b.state(self.bench)['stopped_arms'].values()))
        self.assertEqual(b.report(self.bench,now=5)['transport_totals']['registered_attempts'],4)

if __name__=='__main__':
    unittest.main()
