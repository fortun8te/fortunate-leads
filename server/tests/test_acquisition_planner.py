import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from acquisition_planner import choose, observe

spec = importlib.util.spec_from_file_location('acquisition_replay', Path(__file__).resolve().parents[2] / 'ops/acquisition_replay.py')
replay_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay_module)
replay = replay_module.replay


def page(job, target, members, at, **kwargs):
    return dict(job_id=job, target_id=target, members=members, at=at,
                request_count=kwargs.pop('request_count', 1), direction=kwargs.pop('direction', 'following'), **kwargs)


class AcquisitionPlannerTests(unittest.TestCase):
    def test_manual_priority_preserved(self):
        state = observe({}, 'auto', requests=1, new_edges=500, new_people=500)
        selected = choose([{'job_id':'auto'}, {'job_id':'manual', 'manual_priority':5}], state)
        self.assertEqual(selected['job_id'], 'manual')
        self.assertIsNone(selected['score'])

    def test_new_people_have_explicit_weight(self):
        state = observe({}, 'edges', requests=1, new_edges=100, new_people=0)
        state = observe(state, 'people', requests=1, new_edges=80, new_people=80)
        self.assertEqual(choose([{'job_id':'edges'}, {'job_id':'people'}], state)['job_id'], 'people')

    def test_explores_after_three_events(self):
        state = {}
        for _ in range(3):
            state = observe(state, 'productive', requests=1, new_edges=200, new_people=200)
        self.assertEqual(choose([{'job_id':'productive'}, {'job_id':'unseen'}], state)['job_id'], 'unseen')

    def test_bounded_and_pure(self):
        with self.assertRaises(ValueError):
            choose([{'job_id':i} for i in range(65)])
        original = observe({}, 'a', requests=1, new_edges=2, new_people=1)
        saved = copy.deepcopy(original)
        observe(original, 'a', requests=1, new_edges=4, new_people=2)
        choose([{'job_id':'a'}], original)
        self.assertEqual(original, saved)

    def test_no_future_payload_leakage(self):
        records = [page('a','10',['20'],1), page('a','10',['30'],2), page('b','40',['50'],3)]
        changed = copy.deepcopy(records)
        changed[1]['members'] = [str(i) for i in range(1000, 9000)]
        first = replay({'cutoff':0}, records, 1)['results']['adaptive']
        second = replay({'cutoff':0}, changed, 1)['results']['adaptive']
        self.assertEqual(first, second)

    def test_future_page_times_do_not_set_adaptive_candidate_order(self):
        records = [page('a','10',['20'],1),page('b','30',['40'],2)]
        changed = copy.deepcopy(records)
        changed[1]['at'] = 0.5
        self.assertEqual(replay({'cutoff':0},records,1)['results']['adaptive'],
                         replay({'cutoff':0},changed,1)['results']['adaptive'])

    def test_errors_and_lookups_cost_real_requests(self):
        records = [page('a','10',[],1,role='lookup'), page('a','10',[],2,role='error',status='429'), page('a','10',['20'],3)]
        for result in replay({'cutoff':0}, records, 2)['results'].values():
            self.assertEqual(result['confirmed_requests'], 2)
            self.assertEqual(result['canonical_new_edges'], 0)
            self.assertEqual(result['lookup_events'], 1)
            self.assertEqual(result['error_events'], 1)

    def test_canonical_cross_direction_dedup(self):
        records = [page('a','10',['20'],1), page('b','20',['10'],2,direction='followers')]
        for result in replay({'cutoff':0}, records, 10)['results'].values():
            self.assertEqual(result['canonical_new_edges'], 1)
            self.assertEqual(result['unique_new_people'], 0)  # Both targets already in candidate pool.
            self.assertIsNone(result['completed_lists'])
            self.assertEqual(result['exhausted_recorded_histories'], 2)

    def test_independent_shared_baselines(self):
        baseline = {'cutoff':0, 'edges':[['10','20']], 'people':['30']}
        records = [page('a','10',['20','30','40'],1), page('b','50',['40'],2)]
        old = copy.deepcopy((baseline, records))
        result = replay(baseline, records, 10)
        for policy in result['results'].values():
            self.assertEqual(policy['canonical_new_edges'], 3)
            self.assertEqual(policy['unique_new_people'], 1)
        self.assertEqual((baseline, records), old)
        self.assertEqual(result, replay(baseline, records, 10))

    def test_unknown_transport_not_free(self):
        records = [page('a','10',[],1,request_count=None), page('a','10',['20'],2)]
        for result in replay({'cutoff':0}, records, 10)['results'].values():
            self.assertTrue(result['stopped_on_unknown_cost'])
            self.assertEqual(result['confirmed_requests'], 0)
            self.assertEqual(result['canonical_new_edges'], 0)

    def test_cutoff_and_malformed_identifiers_rejected(self):
        with self.assertRaises(ValueError):
            replay({'cutoff':2}, [page('a','10',['20'],1)])
        with self.assertRaises(ValueError):
            replay({'cutoff':0}, [page('a','handle',['20'],1)])

    def test_zero_budget_consumes_nothing(self):
        for result in replay({'cutoff':0}, [page('a','10',['20'],1)], 0)['results'].values():
            self.assertEqual(result['recorded_events_consumed'], 0)

    def test_benchmark_loader_keeps_errors_warmups_and_cost_without_final_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'saved.sqlite'
            with sqlite3.connect(path) as conn:
                conn.executescript('''
                CREATE TABLE metadata(value TEXT);
                CREATE TABLE baseline(src TEXT,dst TEXT);
                CREATE TABLE tasks(task_id INT,target_id TEXT,direction TEXT,arm TEXT,warmup INT);
                CREATE TABLE requests(request_id TEXT,task_id INT,role TEXT,started_at REAL,result TEXT);
                CREATE TABLE observations(request_id TEXT,src TEXT,dst TEXT);
                ''')
                conn.execute('INSERT INTO metadata VALUES(?)', (json.dumps({'created_at':0}),))
                conn.execute("INSERT INTO tasks VALUES(1,'10','following','web_rest200',1)")
                conn.execute('INSERT INTO requests VALUES(?,?,?,?,?)', ('first',1,'lookup',1,json.dumps({'actual_http_requests':1,'status':'ok'})))
                conn.execute('INSERT INTO requests VALUES(?,?,?,?,?)', ('second',1,'page',2,json.dumps({'actual_http_requests':1,'status':'ok'})))
                conn.execute("INSERT INTO observations VALUES('second','10','20')")
            before = path.read_bytes()
            baseline, records, provenance = replay_module.load_benchmark(path)
            result = replay(baseline, records, 2)['results']['adaptive']
            self.assertEqual(result['confirmed_requests'], 2)
            self.assertEqual(result['canonical_new_edges'], 1)
            self.assertEqual(result['lookup_events'], 1)
            self.assertTrue(records[0]['warmup'])
            self.assertFalse(provenance['chain_proof'])
            self.assertEqual(path.read_bytes(), before)


class ProductionReplayTests(unittest.TestCase):
    def make_capture(self, path):
        with sqlite3.connect(path) as conn:
            conn.executescript("""
                CREATE TABLE collector_events(id INTEGER,event_id TEXT,at TEXT,job_id INT,kind TEXT,direction TEXT,http_status INT,returned_count INT);
                CREATE TABLE edge_observations(seed TEXT,person_id INT,direction TEXT,page_key TEXT,job_id INT,observed_at TEXT);
                CREATE TABLE people(id INT,handle TEXT,ig_id TEXT,first_seen TEXT);
                CREATE TABLE seeds(handle TEXT,ig_id TEXT);
                CREATE TABLE jobs(id INT,seed TEXT,target_ig_id TEXT,created_at TEXT);
                INSERT INTO people VALUES(1,'target','10','2026-09-01T00:00:00+00:00');
                INSERT INTO people VALUES(2,'known','20','2026-09-01T00:00:00+00:00');
                INSERT INTO people VALUES(3,'new','30','2026-10-03T01:00:00+00:00');
                INSERT INTO seeds VALUES('target','10');
                INSERT INTO jobs VALUES(1,'target',NULL,'2026-10-01T00:00:00+00:00');
                INSERT INTO jobs VALUES(2,'target',NULL,'2026-10-03T02:00:00+00:00');
                INSERT INTO collector_events VALUES(1,NULL,'2026-10-03T01:00:00+00:00',1,'list','following',200,2);
                INSERT INTO collector_events VALUES(2,NULL,'2026-10-03T01:01:00+00:00',1,'list','following',0,0);
                INSERT INTO collector_events VALUES(3,NULL,'2026-10-03T01:02:00+00:00',1,'profile',NULL,429,0);
                INSERT INTO collector_events VALUES(4,NULL,'2026-10-03T02:01:00+00:00',2,'list','following',200,1);
                INSERT INTO edge_observations VALUES('target',2,'following','old',1,'2026-10-01 12:00:00');
                INSERT INTO edge_observations VALUES('target',2,'following','page-a',1,'2026-10-03T01:00:00+00:00');
                INSERT INTO edge_observations VALUES('target',3,'following','page-a',1,'2026-10-03T01:00:00+00:00');
                INSERT INTO edge_observations VALUES('target',3,'following','page-b',2,'2026-10-03T02:01:00+00:00');
            """)

    def test_production_baseline_attribution_future_candidates_and_unknown_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.sqlite'
            self.make_capture(path)
            before = path.read_bytes()
            baseline, records, provenance = replay_module.load_production(path,'2026-10-03T00:00:00+00:00')
            self.assertEqual(len(records), 2)
            self.assertEqual(provenance['later_or_undated_candidate_events_excluded'], 1)
            self.assertEqual(provenance['confirmed_profile_responses_excluded'], 1)
            self.assertEqual(provenance['unique_job_timestamp_matches'], 1)
            self.assertEqual(set(baseline['people']), {'10','20'})
            for result in replay(baseline,records,10)['results'].values():
                self.assertEqual(result['canonical_new_edges'], 1)
                self.assertEqual(result['unique_new_people'], 1)
                self.assertEqual(result['confirmed_requests'], 1)
                self.assertTrue(result['stopped_on_unknown_cost'])
            self.assertEqual(path.read_bytes(),before)

    def test_production_bounds_and_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.sqlite'
            self.make_capture(path)
            _, records, provenance = replay_module.load_production(path,'2026-10-03T00:00:00+00:00',max_events=1)
            self.assertTrue(provenance['truncated'])
            self.assertEqual(len(records),1)
            with self.assertRaises(ValueError):
                replay_module.load_production(path,'2026-10-03T00:00:00+00:00',max_events=10001)
            with self.assertRaises(ValueError):
                replay_module.load_production(path,'2026-10-03T00:00:00')
            empty = Path(directory) / 'empty.sqlite'
            sqlite3.connect(empty).close()
            with self.assertRaises(ValueError):
                replay_module.load_production(empty,'2026-10-03T00:00:00+00:00')

    def test_ambiguous_timestamp_not_assigned_members(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.sqlite'
            self.make_capture(path)
            with sqlite3.connect(path) as conn:
                conn.execute("INSERT INTO collector_events VALUES(5,NULL,'2026-10-03T01:00:00+00:00',1,'list','following',200,2)")
            _,records,provenance = replay_module.load_production(path,'2026-10-03T00:00:00+00:00',max_events=1)
            self.assertEqual(records[0]['members'],[])
            self.assertEqual(provenance['positive_pages_without_attributed_members'],1)


if __name__ == '__main__':
    unittest.main()
