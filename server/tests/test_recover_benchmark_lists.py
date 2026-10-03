"""Saved benchmark recovery preserves direction, dates, identity gaps and sources."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('recover_benchmark_lists',
    Path(__file__).resolve().parents[2] / 'ops' / 'recover_benchmark_lists.py')
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'benchmark.sqlite'
        conn = sqlite3.connect(self.source)
        conn.executescript('''
            CREATE TABLE tasks(task_id TEXT,target_id TEXT,target_handle TEXT,direction TEXT,route TEXT,warmup INTEGER);
            CREATE TABLE requests(request_id TEXT,task_id TEXT,started_at REAL,finished_at REAL,result TEXT);
            CREATE TABLE observations(request_id TEXT,src TEXT,dst TEXT);
            CREATE TABLE baseline(src TEXT,dst TEXT);
            INSERT INTO tasks VALUES('t','100','owner','followers','saved_route',1);
            INSERT INTO observations VALUES('r','200','100');
            INSERT INTO observations VALUES('r','300','100');
            INSERT INTO baseline VALUES('400','100');
        ''')
        conn.execute('INSERT INTO requests VALUES(?,?,?,?,?)', ('r','t',1700000000,1700000001,
            json.dumps({'actual_http_requests':1,'transport_completed':True,'status':'server_cap'})))
        conn.commit();conn.close()
        self.identity = self.root / 'identities.sqlite'
        conn = sqlite3.connect(self.identity)
        conn.executescript("CREATE TABLE people(ig_id TEXT,handle TEXT); INSERT INTO people VALUES('200','known');")
        conn.close()

    def test_reverse_direction_unknown_ids_old_date_and_readonly_sources(self):
        before = self.source.read_bytes(), self.identity.read_bytes()
        out = self.root / 'capture.jsonl'
        result = recovery.recover([self.source],out,self.identity)
        page = json.loads(out.read_text())
        self.assertEqual(page['direction'],'followers')
        self.assertEqual(page['source_owner_id'],'100')
        self.assertEqual(page['members'],[{'ig_id':'200','handle':'known'},{'ig_id':'300'}])
        self.assertEqual(page['observed_at'],'2023-11-14T22:13:21+00:00')
        self.assertTrue(page['source_context']['warmup'])
        self.assertEqual(page['source_context']['stop_outcome'],'server_cap')
        self.assertEqual(result['observations'],2)
        self.assertEqual(result['new_instagram_requests'],0)
        self.assertEqual(before,(self.source.read_bytes(),self.identity.read_bytes()))

    def test_unknown_transport_is_not_promoted_to_captured_evidence(self):
        conn = sqlite3.connect(self.source)
        conn.execute("UPDATE requests SET result='{}'");conn.commit();conn.close()
        out = self.root / 'capture.jsonl'
        result = recovery.recover([self.source],out)
        self.assertEqual(result['skipped_unconfirmed'],1)
        self.assertEqual(out.read_text(),'')

    def test_bad_orientation_is_atomic_and_existing_output_preserved(self):
        conn = sqlite3.connect(self.source)
        conn.execute("UPDATE observations SET dst='999'");conn.commit();conn.close()
        out = self.root / 'capture.jsonl'
        with self.assertRaisesRegex(ValueError,'source list'):
            recovery.recover([self.source],out)
        self.assertFalse(out.exists())
        self.assertEqual(list(self.root.glob('.recovered-*')),[])
        out.write_text('already saved')
        with self.assertRaisesRegex(ValueError,'existing captures'):
            recovery.recover([self.source],out)
        self.assertEqual(out.read_text(),'already saved')
