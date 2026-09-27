"""Current local evidence must finish before external research can start."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import db, server, external_ready


class ProcessingFollowthrough(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.init(str(Path(self.tmp.name) / 'followthrough.sqlite'))
        self.addCleanup(self.conn.close)
        server.owner_notes.ensure(self.conn)
        server.processing_state.ensure(self.conn)
        self.pid = db.upsert_person(self.conn, {'handle': 'followthrough_brand',
            'bio': 'Founder of a skincare brand. Shop our products.', 'followers': 2000})
        self.conn.commit()
        server.qualify_batch(self.conn)
        external_ready(self.conn, [self.pid])
        db.set_setting(self.conn, 'llm_min', 0)
        self.conn.commit()
        self.broad = patch.object(server.external_harness, 'broad', return_value=None).start()
        self.addCleanup(patch.stopall)

    def candidates(self):
        return [r['id'] for r in server.llm_candidates(self.conn, 5, {})]

    def test_current_review_is_eligible_but_new_local_work_blocks_it(self):
        self.assertEqual(self.candidates(), [self.pid])
        server.processing_state.enqueue(self.conn, self.pid)
        self.conn.commit()
        self.assertEqual(self.candidates(), [])
        self.broad.assert_not_called()

    def test_pending_private_note_blocks_external_work_even_when_profile_hash_matches(self):
        self.conn.execute('INSERT INTO marks VALUES(?,?,?,?)', (self.pid, None, 'He is my friend.', db.now()))
        self.conn.commit()
        # Recreate a current profile review deliberately; unfinished note work
        # must independently block escalation even without a local queue row.
        external_ready(self.conn, [self.pid])
        self.assertEqual(server.owner_notes.result(self.conn, self.pid)['state'], 'pending')
        rows = self.conn.execute('SELECT * FROM people WHERE id=?', (self.pid,)).fetchall()
        person = server.with_owner(self.conn, dict(rows[0]))
        self.assertIsNone(server.current_local_review(self.conn, person))
        self.assertEqual(server.run_llm(self.conn, rows, {}), 0)
        self.broad.assert_not_called()

    def test_model_prompt_and_content_identity_must_be_current(self):
        for column in ('input_hash', 'model', 'model_version', 'prompt'):
            with self.subTest(column=column):
                external_ready(self.conn, [self.pid])
                self.conn.execute(f'UPDATE local_reviews SET {column}=? WHERE person_id=?', ('stale', self.pid))
                self.conn.commit()
                rows = self.conn.execute('SELECT * FROM people WHERE id=?', (self.pid,)).fetchall()
                person = server.with_owner(self.conn, dict(rows[0]))
                self.assertIsNone(server.current_local_review(self.conn, person))
                self.assertEqual(server.run_llm(self.conn, rows, {}), 0)
        self.broad.assert_not_called()

    def test_null_external_result_is_durable_and_not_repeated_by_new_dispatcher(self):
        rows = server.llm_candidates(self.conn, 1, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(server.run_llm(self.conn, rows, {}), 0)
        self.broad.assert_called_once()
        self.assertEqual(self.candidates(), [])
        # A fresh dispatcher has no in-memory skip history. Durable accounting
        # must still prevent another identical paid attempt.
        self.assertFalse(server.LLMPool().step(self.conn))
        self.broad.assert_called_once()

    def test_pause_during_finished_call_keeps_attempt_terminal(self):
        def pause(*args, **kwargs):
            server.processing_modes.set_paused(self.conn, True)
            self.conn.commit()
            return None
        self.broad.side_effect = pause
        rows = server.llm_candidates(self.conn, 1, {})
        self.assertEqual(server.run_llm(self.conn, rows, {}), 0)
        row = self.conn.execute('SELECT state,input_hash FROM external_attempts WHERE person_id=?',
                                (self.pid,)).fetchone()
        self.assertEqual(row['state'], 'unverified')
        server.processing_modes.set_paused(self.conn, False)
        self.conn.commit()
        self.assertFalse(server.external_queue.eligible(self.conn, self.pid, row['input_hash'], now=10**12))
        self.assertEqual(self.candidates(), [])
        self.broad.assert_called_once()


if __name__ == '__main__':
    unittest.main()
