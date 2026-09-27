"""Offline integration of real database queues, modes and local review orchestration."""
import os
os.environ.setdefault('FL_NO_ORSLOT', '1')
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import owner_notes
import processing_modes as modes
import processing_state as state
import server


class ProcessingPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'pipeline.sqlite')
        self.conn = db.init(self.path)
        self.addCleanup(self.conn.close)
        owner_notes.ensure(self.conn)
        state.ensure(self.conn)
        modes.set_mode(self.conn, 'RLAI')
        self.pid = db.upsert_person(self.conn, {'handle': 'testbrand', 'name': 'Test Brand',
            'bio': 'Founder of a skincare brand. Shop our products. Shipping to the US.',
            'followers': 1200, 'website': 'https://example.test'})
        self.conn.commit()
        self.output = {'handle': 'testbrand', 'role': 'buyer', 'fit': 84,
                       'evidence': ['Founder of a skincare brand'], 'research_needed': None}
        self.runtime = patch.object(server.local_model, 'complete_json', return_value=self.output).start()
        self.addCleanup(patch.stopall)
        # These assertions prevent accidental network research in every test.
        self.external = patch.object(server.qualify, 'llm_verdict', side_effect=AssertionError('External AI called')).start()
        self.network = patch.object(server, 'network_context', return_value={}).start()

    def review_count(self):
        return self.conn.execute('SELECT count(*) FROM local_reviews').fetchone()[0]

    def test_rules_mode_starts_no_note_profile_laya_or_external_inference(self):
        modes.set_mode(self.conn, 'R')
        self.conn.commit()
        with patch.object(server.laya, 'available') as laya:
            self.assertFalse(server.local_processing_step(self.conn))
            self.assertFalse(server.laya_step(self.conn))
            laya.assert_not_called()
        self.assertFalse(server.LLMPool().step(self.conn))
        self.runtime.assert_not_called()
        self.external.assert_not_called()
        self.assertEqual(self.review_count(), 0)

    def test_local_mode_saves_review_without_external_work(self):
        self.assertTrue(server.local_processing_step(self.conn))
        self.assertEqual(self.runtime.call_count, 1)
        self.assertEqual(self.review_count(), 1)
        verdict = self.conn.execute('SELECT model,content_fit FROM verdicts').fetchone()
        self.assertTrue(verdict['model'].startswith('local:'))
        self.assertEqual(verdict['content_fit'], 84)
        self.assertFalse(server.LLMPool().step(self.conn))
        self.external.assert_not_called()

    def test_owner_profile_never_consumes_local_inference(self):
        self.conn.execute("UPDATE people SET handle='fortun8te' WHERE id=?", (self.pid,))
        self.conn.commit()
        self.assertTrue(server.local_processing_step(self.conn))
        self.runtime.assert_not_called()
        self.assertEqual(self.review_count(), 0)
        self.assertEqual(len(state.next_pending(self.conn)), 0)

    def test_manual_pause_does_not_change_mode_or_dispatch_work(self):
        with patch.object(server, 'schedule_local_services'), patch.object(server.local_model, 'status',
                return_value={'ready': True, 'resources': {'allowed': True}}):
            response = server.api_local_processing(self.conn, {}, {'paused': True})
        self.assertEqual(response['state'], 'paused')
        self.assertEqual(response['processing']['mode'], 'RLAI')
        self.assertTrue(response['paused'])
        self.assertFalse(server.local_processing_step(self.conn))
        self.runtime.assert_not_called()
        self.assertEqual(len(state.next_pending(self.conn)), 1)

    def test_mode_flip_and_reenable_during_inference_discards_result(self):
        def infer(*args, **kwargs):
            other = db.connect(self.path)
            modes.set_mode(other, 'R')
            other.commit()
            modes.set_mode(other, 'RLAI')
            other.commit()
            other.close()
            return self.output
        self.runtime.side_effect = infer
        self.assertFalse(server.local_processing_step(self.conn))
        self.assertEqual(self.review_count(), 0)
        self.assertEqual(len(state.next_pending(self.conn)), 1)

    def test_new_queue_revision_is_not_acknowledged_by_old_result(self):
        before = state.next_pending(self.conn)[0]['revision']
        def infer(*args, **kwargs):
            other = db.connect(self.path)
            state.enqueue(other, self.pid)
            other.commit()
            other.close()
            return self.output
        self.runtime.side_effect = infer
        server.local_processing_step(self.conn)
        self.assertEqual(self.review_count(), 0)
        self.assertGreater(state.next_pending(self.conn)[0]['revision'], before)

    def test_profile_edit_during_inference_discards_stale_content(self):
        def infer(*args, **kwargs):
            other = db.connect(self.path)
            other.execute('UPDATE people SET bio=? WHERE id=?', ('Personal account.', self.pid))
            other.commit()
            other.close()
            return self.output
        self.runtime.side_effect = infer
        self.assertFalse(server.local_processing_step(self.conn))
        self.assertEqual(self.review_count(), 0)
        self.assertEqual(len(state.next_pending(self.conn)), 1)

    def test_owner_note_gets_first_slot_before_bulk_profile_review(self):
        self.conn.execute('INSERT INTO marks VALUES(?,?,?,?)', (self.pid, None, 'He is my friend.', db.now()))
        self.conn.commit()
        self.runtime.return_value = {'facts': [{'kind': 'friend', 'sentence': 0}]}
        self.assertTrue(server.local_processing_step(self.conn))
        self.assertEqual(self.runtime.call_count, 1)
        self.assertEqual(self.review_count(), 0)
        self.assertEqual(owner_notes.result(self.conn, self.pid)['state'], 'ready')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM owner_context').fetchone()[0], 0)
        self.runtime.return_value = self.output
        self.assertTrue(server.local_processing_step(self.conn))
        self.assertEqual(self.review_count(), 1)

    def test_current_cached_review_reblends_without_second_model_call(self):
        self.assertTrue(server.local_processing_step(self.conn))
        state.enqueue(self.conn, self.pid)
        self.conn.commit()
        self.assertTrue(server.local_processing_step(self.conn))
        self.assertEqual(self.runtime.call_count, 1)
        self.assertEqual(len(state.next_pending(self.conn)), 0)

    def test_local_review_preserves_confirmed_relationships_and_manual_tags(self):
        relations = json.dumps(['worked_with', 'client', 'friend'])
        self.conn.execute('INSERT INTO owner_context VALUES(?,?,?,?)', (self.pid, relations, 'close', db.now()))
        self.conn.execute("INSERT INTO tags VALUES(?,'My custom label','custom','manual')", (self.pid,))
        self.conn.execute('INSERT INTO marks VALUES(?,?,?,?)', (self.pid, 'contacted', '', db.now()))
        self.conn.commit()
        self.assertTrue(server.local_processing_step(self.conn))
        self.assertEqual(self.conn.execute('SELECT relationships,familiarity FROM owner_context').fetchone()[:], (relations, 'close'))
        self.assertEqual(self.conn.execute('SELECT status FROM marks').fetchone()[0], 'contacted')
        self.assertEqual(self.conn.execute("SELECT tag FROM tags WHERE source='manual'").fetchone()[0], 'My custom label')


if __name__ == '__main__':
    unittest.main()
