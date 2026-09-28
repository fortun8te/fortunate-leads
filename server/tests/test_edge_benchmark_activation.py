import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_server import Base
import db
import server
import edge_benchmark as ledger

spec=importlib.util.spec_from_file_location('benchmark_ops',Path(__file__).resolve().parents[2]/'ops'/'edge_benchmark.py')
ops=importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


class ActivationTests(Base):
    def test_normal_next_and_acquire_park_before_side_effects(self):
        db.set_setting(self.conn,'raw_edge_benchmark',{'enabled':True})
        self.conn.commit()
        with mock.patch.object(server,'collector_request',side_effect=AssertionError('must not touch collector')):
            next_result=server.ext_next(self.conn,{}, {})
            permit=server.ext_request(self.conn,{}, {'action':'acquire','kind':'list'})
        self.assertIsNone(next_result['job'])
        self.assertFalse(permit['granted'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM accounts').fetchone()[0],0)
    def test_existing_normal_release_allowed(self):
        db.set_setting(self.conn,'raw_edge_benchmark',{'enabled':True})
        self.conn.commit()
        with mock.patch.object(server,'collector_request'),mock.patch.object(server.accounts,'request_permit',return_value={'released':True}) as release:
            result=server.ext_request(self.conn,{}, {'action':'release','token':'old-token'})
        self.assertTrue(result['released'])
        release.assert_called_once()
    def test_benchmark_routes_are_registered_and_translate_errors(self):
        routes={(method,path) for method,path,_ in server.ROUTES}
        for pair in [('GET','/api/benchmark/next'),('POST','/api/benchmark/permit'),('POST','/api/benchmark/result')]:
            self.assertIn(pair,routes)
        with mock.patch.object(server.edge_benchmark_api,'next_task',side_effect=ValueError('bad identity')):
            with self.assertRaises(server.Bad):
                server.benchmark_next(self.conn,{}, {})


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.live=Path(self.tmp.name)/'live.db'
        self.bench=Path(self.tmp.name)/'bench.db'
        db.init(self.live)
        self.conn=db.connect(self.live)
        for i in range(12):
            self.conn.execute('INSERT INTO people(ig_id,handle,following,is_private,is_verified,first_seen,updated_at) VALUES(?,?,?,0,?,?,?)',(str(i+100),'p'+str(i),[100,1000,3000][i//4],i%2,db.now(),db.now()))
        self.conn.execute("INSERT INTO accounts(lane_id,ig_id,is_main,first_seen,last_seen) VALUES('alt','999',0,?,?)",(db.now(),db.now()))
        self.conn.commit()
        ledger.create_plan(self.live,self.bench,'999')
    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
    def test_activation_requires_drained_permits_and_correct_alternate(self):
        db.set_setting(self.conn,'instagram_request_gate',{'active':{'lane':'alt','token':'old'}})
        self.conn.commit()
        with self.assertRaisesRegex(ValueError,'drain'):
            ops.activate(self.live,self.bench,'alt')
        db.set_setting(self.conn,'instagram_request_gate',{})
        self.conn.commit()
        with self.assertRaisesRegex(ValueError,'alternate'):
            ops.activate(self.live,self.bench,'wrong')
        cfg=ops.activate(self.live,self.bench,'alt')
        self.assertTrue(cfg['enabled'])
        self.assertEqual(cfg['phase'],'warmup')
        with self.assertRaisesRegex(ValueError,'warmup'):
            ops.advance(self.live)
    def test_advance_requires_confirmed_warmups_and_opens_measured_phase(self):
        ops.activate(self.live,self.bench,'alt')
        for i in range(7):
            task=ledger.state(self.bench)['current_task']
            task=ledger.next_task(self.bench,'alt','999',transport=task['transport'])
            request_id='warmup-'+str(i)
            ledger.begin_request(self.bench,request_id,task['task_id'],'999')
            ledger.finish_request(self.bench,request_id,rows=['800'])
        cfg=ops.advance(self.live)
        self.assertEqual(cfg['phase'],'measured')
        self.assertEqual(ledger.state(self.bench)['phase'],'measured')

    def test_disarm_preserves_warning_pause_and_cooldown(self):
        ops.activate(self.live,self.bench,'alt')
        for key,value in [('instagram_request_attention',{'message':'warning'}),('paused_lists',True),('cooldown','2099-01-01T00:00:00+00:00')]:
            db.set_setting(self.conn,key,value)
        self.conn.commit()
        self.assertFalse(ops.deactivate(self.live)['enabled'])
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))
        self.assertEqual(db.get_setting(self.conn,'instagram_request_attention'),{'message':'warning'})
        self.assertEqual(db.get_setting(self.conn,'cooldown'),'2099-01-01T00:00:00+00:00')

if __name__=='__main__':
    unittest.main()
