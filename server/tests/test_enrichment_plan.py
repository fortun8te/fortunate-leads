"""Saved-content dispatch uses current evidence, never network or inference."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import enrichment_plan as plan
import local_qualification
import processing_state


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'test.sqlite'))
        processing_state.ensure(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def person(self, handle, bio='Founder of a skincare brand. Shop our products.'):
        pid = db.upsert_person(self.conn, {'handle': handle, 'bio': bio, 'followers': 2000})
        self.conn.execute('INSERT OR IGNORE INTO local_queue(person_id) VALUES(?)', (pid,))
        self.conn.commit()
        return pid

    def cached(self, pid):
        person = plan._person(self.conn, pid)
        result = local_qualification.evaluate(person, [], [], generate=lambda *a, **k: {
            'handle': person['handle'], 'role': 'buyer', 'fit': 80,
            'evidence': ['Founder of a skincare brand'], 'research_needed': None})
        processing_state.put_review(self.conn, pid, result)
        self.conn.execute('INSERT OR IGNORE INTO local_queue(person_id) VALUES(?)', (pid,))
        self.conn.commit()
        return result

    def test_cached_content_is_drained_before_a_new_model_call(self):
        first = self.person('needs_inference')
        cached = self.person('saved_cache')
        self.cached(cached)
        with patch('local_model.complete_json', side_effect=AssertionError('No inference')):
            batch = plan.next_batch(self.conn)
            self.assertEqual(batch['model_calls_needed'], 1)
            self.assertEqual(batch['model_calls_avoided'], 1)
            self.assertEqual(plan.next_job(self.conn)['person_id'], cached)
        self.assertEqual(batch['entries'][0]['person_id'], first)
        self.assertNotIn('Founder of', json.dumps(batch))

    def test_owner_edit_priority_is_preserved(self):
        first = self.person('owner_edit')
        cached = self.person('bulk_cache')
        self.cached(cached)
        self.conn.execute('UPDATE local_queue SET priority=1 WHERE person_id=?', (first,))
        self.assertEqual(plan.next_job(self.conn)['person_id'], first)

    def test_changed_source_and_unsupported_cached_claim_require_fresh_review(self):
        pid = self.person('new_source')
        self.cached(pid)
        row = self.conn.execute('SELECT verdict FROM local_reviews WHERE person_id=?', (pid,)).fetchone()
        verdict = json.loads(row[0])
        verdict['reason'] = 'A well funded founder with a million euro budget.'
        self.conn.execute('UPDATE local_reviews SET verdict=? WHERE person_id=?', (json.dumps(verdict), pid))
        self.assertEqual(plan.next_batch(self.conn)['entries'][0]['source_state'], 'needs_fresh_review')
        self.cached(pid)
        self.conn.execute("UPDATE people SET bio='Creator. Reviewing skincare products.' WHERE id=?", (pid,))
        self.assertEqual(plan.next_batch(self.conn)['model_calls_needed'], 1)

    def test_inflated_fit_and_non_ai_generated_budget_badges_cannot_be_reused(self):
        pid = self.person('poisoned_fields')
        original = self.cached(pid)
        person = plan._person(self.conn, pid)
        self.assertTrue(plan.reusable_result(original, person))
        for changes in ({'content_fit': 999}, {'fit': 999}, {'score': 999},
                        {'role': 'unclear', 'content_fit': 65, 'fit': 65},
                        {'tags': original['verdict']['tags'] + [('Million euro budget', 'signal')]},
                        {'tags': original['verdict']['tags'] + [('Can hire an agency', 'role')]},
                        {'budget': 1000000}):
            with self.subTest(changes=changes):
                result = dict(original, verdict=dict(original['verdict'], **changes))
                self.assertFalse(plan.reusable_result(result, person))
                self.conn.execute('UPDATE local_reviews SET verdict=? WHERE person_id=?',
                                  (json.dumps(result['verdict']), pid))
                self.assertEqual(plan.next_batch(self.conn)['counts']['infer'], 1)

    def test_owner_budget_label_stays_separate_from_generated_cache(self):
        pid = self.person('manual_label')
        self.conn.execute("INSERT INTO tags VALUES(?,'Million euro budget','signal','manual')", (pid,))
        result = self.cached(pid)
        self.assertTrue(plan.reusable_result(result, plan._person(self.conn, pid)))
        self.assertEqual(self.conn.execute("SELECT source FROM tags WHERE person_id=? AND tag='Million euro budget'",
                                           (pid,)).fetchone()[0], 'manual')

    def test_empty_and_protected_inputs_never_enter_model_packet(self):
        empty = self.person('empty_bio', '')
        protected = self.person('fortun8te')
        self.conn.execute('INSERT OR IGNORE INTO local_queue(person_id) VALUES(?)', (protected,))
        batch = plan.next_batch(self.conn)
        actions = {e['person_id']: e['action'] for e in batch['entries']}
        self.assertEqual(actions[empty], 'skip_empty')
        self.assertEqual(actions[protected], 'skip_protected')
        self.assertEqual(batch['model_calls_needed'], 0)

    def test_revision_guard_refuses_a_stale_dispatch_manifest(self):
        pid = self.person('changed_revision')
        item = plan.next_batch(self.conn)['entries'][0]
        self.assertTrue(plan.current(self.conn, item))
        self.conn.execute('UPDATE local_queue SET revision=revision+1 WHERE person_id=?', (pid,))
        self.assertFalse(plan.current(self.conn, item))

    def test_current_terminal_failure_is_not_an_unlimited_retry(self):
        pid = self.person('unverified_reply')
        result = local_qualification.failure_result(plan._person(self.conn, pid), None, 'unsupported_evidence')
        processing_state.put_review(self.conn, pid, result)
        self.conn.execute('INSERT INTO local_queue(person_id) VALUES(?)', (pid,))
        self.assertEqual(plan.next_batch(self.conn)['counts']['reuse_terminal'], 1)
        self.conn.execute('UPDATE local_queue SET repair_attempt=1 WHERE person_id=?', (pid,))
        self.assertEqual(plan.next_batch(self.conn)['counts']['infer'], 1)

    def test_actual_worker_drains_cached_result_while_first_profile_needs_inference(self):
        import processing_modes
        import server
        first = self.person('first_uncached')
        cached = self.person('later_cached')
        self.cached(cached)
        processing_modes.set_mode(self.conn, 'RLAI')
        self.conn.commit()
        with patch.object(local_qualification, 'evaluate', side_effect=AssertionError('No new model call')):
            self.assertTrue(server._local_processing_step(self.conn))
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM local_queue WHERE person_id=?', (first,)).fetchone())
        self.assertIsNone(self.conn.execute('SELECT 1 FROM local_queue WHERE person_id=?', (cached,)).fetchone())
        verdict = self.conn.execute('SELECT model,role FROM verdicts WHERE person_id=?', (cached,)).fetchone()
        self.assertTrue(verdict['model'].startswith('local:'))
        self.assertEqual(verdict['role'], 'buyer')

    def test_actual_worker_repairs_malformed_cached_json(self):
        import processing_modes
        import server
        pid = self.person('malformed_cache')
        valid = self.cached(pid)
        self.conn.execute("UPDATE local_reviews SET verdict='{broken' WHERE person_id=?", (pid,))
        processing_modes.set_mode(self.conn, 'RLAI')
        self.conn.commit()
        with patch.object(local_qualification, 'evaluate', return_value=valid) as evaluate:
            self.assertTrue(server._local_processing_step(self.conn))
        evaluate.assert_called_once()
        saved = json.loads(self.conn.execute('SELECT verdict FROM local_reviews WHERE person_id=?', (pid,)).fetchone()[0])
        self.assertEqual(saved['role'], 'buyer')
        self.assertIsNone(self.conn.execute('SELECT 1 FROM local_queue WHERE person_id=?', (pid,)).fetchone())

    def test_100k_queue_selection_uses_indexed_bounded_reads(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        conn.executescript('''CREATE TABLE people(id INTEGER PRIMARY KEY,handle TEXT,bio TEXT);
            CREATE TABLE local_queue(person_id INTEGER PRIMARY KEY,priority INT,rank_band INT,retry_at REAL,
                revision INT,repair_attempt INT);
            CREATE INDEX local_queue_rank_ready ON local_queue(priority,rank_band,retry_at,person_id);''')
        conn.executemany('INSERT INTO people VALUES(?,?,?)', ((i, 'sample'+str(i), 'Saved bio') for i in range(1,100001)))
        conn.executemany('INSERT INTO local_queue VALUES(?,0,0,0,1,0)', ((i,) for i in range(1,100001)))
        started = time.monotonic()
        with patch.object(plan, 'decision', side_effect=lambda c, r: {
                'person_id': r['person_id'], 'action': 'infer'}):
            batch = plan.next_batch(conn, limit=64)
        self.assertEqual(batch['inspected'], 64)
        sqlplan = conn.execute('''EXPLAIN QUERY PLAN SELECT person_id FROM local_queue
            WHERE priority=0 AND rank_band=0 AND retry_at<=0 ORDER BY retry_at,person_id LIMIT 64''').fetchall()
        self.assertTrue(any('INDEX local_queue_rank_ready' in r[3] for r in sqlplan))
        self.assertLess(time.monotonic()-started, 2)
        conn.close()


if __name__ == '__main__':
    unittest.main()
