"""Owner feedback affects future examples without treating machine tags as Michael's judgement."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'server'))

import db  # noqa: E402
import deepscout  # noqa: E402
import laya  # noqa: E402
import qualify  # noqa: E402
import server  # noqa: E402


class FeedbackLearningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'leads.sqlite'))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def person(self, handle, bio='A small skincare brand', ig_id=None):
        pid = db.upsert_person(self.conn, {'handle': handle, 'bio': bio, 'ig_id': ig_id})
        self.conn.commit()
        return pid

    def test_client_note_and_manual_tags_feed_future_examples_with_provenance(self):
        self.assertEqual(server.fewshot(self.conn), [])
        own = self.person('fortun8te')
        server.api_mark(self.conn, {}, {'status': 'client', 'note': 'My own account'}, own)
        pid = self.person('brand')
        server.api_mark(self.conn, {}, {'status': 'client', 'note': 'Great retention and founder relationship'}, pid)
        server.api_tag_edit(self.conn, {}, {'add': ['DTC skincare']}, pid)
        self.conn.execute("INSERT INTO tags VALUES(?, 'AI: Fit strong', 'signal', 'auto')", (pid,))
        self.conn.commit()

        examples = server.fewshot(self.conn)
        self.assertEqual(len(examples), 1)  # a new client does not wait for five marks
        self.assertEqual(examples[0]['person_id'], pid)
        self.assertEqual(examples[0]['status'], 'client')
        self.assertEqual(examples[0]['note'], 'Great retention and founder relationship')
        self.assertEqual(examples[0]['manual_tags'], ['DTC skincare'])
        text = qualify.fewshot_text(examples)
        self.assertIn('Great retention and founder relationship', text)
        self.assertIn('DTC skincare', text)
        self.assertNotIn('AI: Fit strong', text)
        self.assertNotIn('My own account', text)
        self.assertIn('owner feedback', text.lower())

    def test_note_and_tag_edits_change_future_prompt_without_mass_rerun(self):
        pid = self.person('brand')
        server.api_mark(self.conn, {}, {'status': 'client', 'note': 'First reason'}, pid)
        first = server.fewshot(self.conn)
        prior_version = qualify.prompt_version(first)
        self.conn.execute("INSERT OR REPLACE INTO verdicts(person_id,model,prompt,score,updated_at) VALUES(?,'m',?,50,'')",
                          (pid, prior_version))
        self.conn.commit()
        server.api_mark(self.conn, {}, {'note': 'Revised reason'}, pid)
        server.api_tag_edit(self.conn, {}, {'add': ['Repeat orders']}, pid)
        second = server.fewshot(self.conn)
        self.assertNotEqual(qualify.prompt_version(second), prior_version)
        self.assertIn('Revised reason', qualify.fewshot_text(second))
        self.assertIn('Repeat orders', qualify.fewshot_text(second))
        self.assertEqual(self.conn.execute('SELECT model FROM verdicts WHERE person_id=?', (pid,)).fetchone()[0], 'm')

    def test_examples_follow_stable_person_id_not_a_transferred_handle(self):
        old = self.person('brand', ig_id='100')
        server.api_mark(self.conn, {}, {'status': 'client'}, old)
        self.assertEqual(server.fewshot(self.conn)[0]['person_id'], old)
        new = self.person('brand', ig_id='200')
        self.assertNotEqual(old, new)
        self.assertEqual(server.fewshot(self.conn), [])

    def test_fortun8te_is_self_without_a_seed_row(self):
        self.assertEqual(server.me_handle(self.conn), 'fortun8te')
        own = self.person('fortun8te')
        other = self.person('goodbrand')
        for pid in (own, other):
            ts = self.conn.execute('SELECT updated_at FROM people WHERE id=?', (pid,)).fetchone()[0]
            self.conn.execute("INSERT INTO verdicts(person_id,prefilter,score,model,updated_at) VALUES(?,90,90,'rules',?)",
                              (pid, ts))
        self.conn.commit()
        self.assertEqual([r['id'] for r in server.llm_candidates(self.conn, 10, set())], [other])
        self.assertEqual([r['id'] for r in server.api_leads(self.conn, {}, {})['rows']], [other])

    def test_laya_skips_self_when_the_seed_row_is_missing(self):
        own = self.person('fortun8te')
        other = self.person('goodbrand')
        db.set_setting(self.conn, 'qualify', True)
        self.conn.commit()
        answer = {q['key']: 0.8 for q in laya.QUESTIONS}
        seen = []

        def score(rows):
            seen.extend(row['id'] for row in rows)
            return {row['id']: answer for row in rows}

        with patch.object(laya, 'available', return_value=True), \
             patch.object(laya, 'cache_signature', return_value='laya:feedback-test'), \
             patch.object(laya, 'decide', side_effect=score):
            self.assertTrue(server.laya_step(self.conn))
        self.assertEqual(seen, [other])
        self.assertNotIn(own, seen)

    def test_hermes_candidates_skip_self_without_sharing_owner_feedback(self):
        deepscout.ensure(self.conn)
        own = self.person('FORTUN8TE')
        other = self.person('goodbrand')
        server.api_mark(self.conn, {}, {'status': 'client', 'note': 'Private owner context'}, other)
        server.api_tag_edit(self.conn, {}, {'add': ['Private tag']}, other)
        for pid in (own, other):
            self.conn.execute("INSERT INTO verdicts(person_id,content_fit,score,model) VALUES(?,85,85,'offline-llm')", (pid,))
        self.conn.commit()
        self.assertEqual([p['id'] for p in deepscout.candidates(self.conn, 10, set())], [other])
        prompt = deepscout.prompt(dict(self.conn.execute('SELECT * FROM people WHERE id=?', (other,)).fetchone()))
        self.assertNotIn('Private owner context', prompt)
        self.assertNotIn('Private tag', prompt)


if __name__ == '__main__':
    unittest.main()
