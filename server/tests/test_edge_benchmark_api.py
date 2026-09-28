"""Real local SQLite/gate/ledger integration, without any network requests."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounts
import db
import edge_benchmark as ledger
import edge_benchmark_api as api


class BenchmarkAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'live.db'
        self.bench = Path(self.tmp.name) / 'bench.db'
        db.init(str(self.path))
        self.conn = db.connect(str(self.path))
        accounts.touch(self.conn, 'alt', {'ig_id': '999', 'handle': 'alternate'}, is_main=0, role='lists', paused=0)
        for i in range(12):
            db.upsert_person(self.conn, {'ig_id': str(i+100), 'handle': 'p'+str(i), 'following': [100,1000,3000][i//4], 'is_private': False, 'is_verified': bool(i%2)})
        for setting in ('paused','paused_lists','paused_bios'):
            db.set_setting(self.conn, setting, False)
        self.conn.commit()
        ledger.create_plan(self.path, self.bench, '999')
        db.set_setting(self.conn, api.KEY, {'enabled': True, 'path': str(self.bench), 'viewer_id': '999', 'lane_id': 'alt', 'phase': 'warmup'})
        self.conn.commit()
        self.transport = ledger.state(self.bench)['current_task']['transport']

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def identity(self, **extra):
        return dict(lane_id='alt', account={'ig_id':'999'}, transport=self.transport, **extra)

    def grant(self):
        task = api.next_task(self.conn, {}, self.identity())['task']
        body = self.identity(task_id=task['task_id'], request_id='request-123', fingerprints={'device':'a'*64})
        permit = api.permit(self.conn, {}, body)
        self.assertTrue(permit.get('granted'), permit)
        self.assertGreater(accounts.utc(permit['expires_at']).timestamp(), time.time()+85)
        body.update(token=permit['token'])
        return body

    def finish(self, body, **extra):
        result = dict(body, rows=[{'ig_id':'800','handle':'sample'}], next_cursor=None, has_more=False, status='ok',
                      actual_http_requests=1, transport_completed=True, duration_ms=12, returned_count=1)
        result.update(extra)
        return api.result(self.conn, {}, result)

    def test_ack_both_clients_duplicate_and_completed_release(self):
        body = self.grant()
        first = self.finish(body)
        for reply in (first, self.finish(body)):
            self.assertTrue(reply['ok'])
            self.assertTrue(reply['ack'])
            self.assertTrue(reply['acknowledged'])
            self.assertEqual(reply['request_id'], body['request_id'])
        self.assertIsNone(db.get_setting(self.conn, 'instagram_request_gate')['active'])
        with ledger._connect(self.bench) as bench:
            self.assertEqual(bench.execute('SELECT count(*) FROM requests').fetchone()[0], 1)

    def test_incomplete_transport_retains_gate_stops_and_replays_stop(self):
        body = self.grant()
        reply = self.finish(body, transport_completed=False, status='transport_uncertain')
        self.assertTrue(reply['stopped'])
        self.assertFalse(reply['result']['transport_completed'])
        self.assertIsNotNone(db.get_setting(self.conn, 'instagram_request_gate')['active'])
        self.assertEqual(ledger.state(self.bench)['state'], 'stopped')
        self.assertTrue(self.finish(body)['stopped'])
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))

    def test_normalized_waits_are_recorded_without_ledger_error(self):
        cfg = db.get_setting(self.conn, api.KEY)
        cfg['waits'] = {'local_pacing': 2, 'budget_or_account': 3, 'permit': 4}
        db.set_setting(self.conn, api.KEY, cfg)
        self.conn.commit()
        self.finish(self.grant())
        with ledger._connect(self.bench) as bench:
            waits = dict(bench.execute('SELECT reason,seconds FROM waits'))
        self.assertEqual(waits['pacing'], 2)
        self.assertEqual(waits['cooldown'], 3)
        self.assertEqual(waits['permit'], 4)

    def test_chrome_deadline_blocks_mobile_and_common_next_pacing(self):
        chrome = self.identity(client_wait_ms=60000, client_wait_reason='local_window')
        chrome['transport'] = 'chrome'
        api.next_task(self.conn, {}, chrome)
        cfg = db.get_setting(self.conn, api.KEY)
        self.assertGreater(cfg['client_until'], time.time()+59)
        current = ledger.state(self.bench)['current_task']
        reply = api.next_task(self.conn, {}, self.identity())
        self.assertIsNone(reply['task'])
        self.assertEqual(reply['reason'], 'local_window')
        denied = api.permit(self.conn, {}, self.identity(task_id=current['task_id'], request_id='blocked-123', fingerprints={'device':'a'*64}))
        self.assertFalse(denied['granted'])
        self.assertEqual(denied['reason'], 'local_window')
        self.assertEqual(ledger.state(self.bench)['inflight'], None)

    def test_common_pace_is_reported_before_task_handoff(self):
        cfg = db.get_setting(self.conn, api.KEY)
        cfg['next_at'] = time.time()+30
        db.set_setting(self.conn, api.KEY, cfg)
        self.conn.commit()
        reply = api.next_task(self.conn, {}, self.identity())
        self.assertEqual(reply['reason'], 'local_pacing')
        self.assertGreater(reply['wait_ms'], 29000)
        self.assertIsNone(reply['task'])

    def test_warmup_done_exits_instead_of_waiting_other_transport(self):
        with ledger._connect(self.bench) as bench:
            bench.execute("UPDATE tasks SET state='done' WHERE warmup=1")
        reply = api.next_task(self.conn, {}, self.identity())
        self.assertTrue(reply['stopped'])
        self.assertEqual(reply['reason'], 'warmup_complete')

    def test_disable_accepts_bound_outbox_and_retry_after_seconds(self):
        body = self.grant()
        cfg = db.get_setting(self.conn, api.KEY)
        cfg['enabled'] = False
        db.set_setting(self.conn, api.KEY, cfg)
        self.conn.commit()
        reply = self.finish(body, status='rate_limit', terminal_warning=True, retry_after='120')
        self.assertTrue(reply['ack'])
        self.assertGreater(accounts.utc(db.get_setting(self.conn, 'cooldown')).timestamp(), time.time()+115)

    def test_expired_repeated_permit_never_resends_and_holds(self):
        body = self.grant()
        cfg = db.get_setting(self.conn, api.KEY)
        cfg['inflight']['at'] -= 100
        db.set_setting(self.conn, api.KEY, cfg)
        self.conn.commit()
        reply = api.permit(self.conn, {}, body)
        self.assertFalse(reply['granted'])
        self.assertTrue(reply['stopped'])
        self.assertIsNotNone(db.get_setting(self.conn, 'instagram_request_attention'))

    def test_fingerprint_changed_stops_before_gate(self):
        cfg = db.get_setting(self.conn, api.KEY)
        cfg['fingerprints'] = {self.transport:{'device':'b'*64}}
        db.set_setting(self.conn, api.KEY, cfg)
        self.conn.commit()
        task = api.next_task(self.conn, {}, self.identity())['task']
        reply = api.permit(self.conn, {}, self.identity(task_id=task['task_id'], request_id='change-123', fingerprints={'device':'a'*64}))
        self.assertTrue(reply['stopped'])
        self.assertFalse(reply['granted'])
        self.assertIsNone(db.get_setting(self.conn, 'instagram_request_gate'))
        self.assertEqual(ledger.state(self.bench)['state'], 'stopped')


if __name__ == '__main__':
    unittest.main()
