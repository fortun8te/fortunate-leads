"""Owner feedback affects future examples without treating machine tags as Michael's judgement."""

import sys
from contextlib import nullcontext
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
import processing_modes
from test_server import external_ready


class FeedbackLearningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'leads.sqlite'))
        processing_modes.set_mode(self.conn, 'RLEAI')
        self.conn.commit()

    def legacy_client_tag(self, pid):
        # Compatibility fixture: new edits use the relationship field.
        self.conn.execute("INSERT OR REPLACE INTO tags VALUES(?,'Client','signal','manual')", (pid,))
        server.touch(self.conn, [pid])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def person(self, handle, bio='A small skincare brand', ig_id=None):
        pid = db.upsert_person(self.conn, {'handle': handle, 'bio': bio, 'ig_id': ig_id})
        self.conn.commit()
        return pid

    def test_client_and_manual_tags_feed_future_examples_but_note_stays_local(self):
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
        self.assertEqual(examples[0]['manual_tags'], ['DTC skincare'])
        text = qualify.fewshot_text(examples)
        self.assertNotIn('Great retention and founder relationship', text)
        self.assertIn('DTC skincare', text)
        self.assertNotIn('AI: Fit strong', text)
        self.assertNotIn('My own account', text)
        self.assertIn('owner feedback', text.lower())
        person = server.with_owner(self.conn, dict(self.conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()))
        self.assertNotIn('Great retention and founder relationship', qualify._packet(person, [], []))

    def test_manual_client_tag_without_status_is_a_provenance_labeled_future_example(self):
        self.assertEqual(server.fewshot(self.conn), [])
        pid = self.person('taggedclient')
        auto = self.person('autotagged')
        self.conn.execute("INSERT INTO tags VALUES(?, 'Client', 'signal', 'auto')", (auto,))
        self.conn.commit()
        server.api_mark(self.conn, {}, {'note': 'Private context for this lead only'}, pid)
        self.legacy_client_tag(pid)

        examples = server.fewshot(self.conn)
        self.assertEqual(len(examples), 1)
        self.assertEqual(examples[0]['person_id'], pid)
        self.assertEqual(examples[0]['label'], 'good')
        self.assertIsNone(examples[0]['status'])
        self.assertEqual(examples[0]['feedback_source'], 'manual_client_tag')
        self.assertIsNone(self.conn.execute('SELECT status FROM marks WHERE person_id=?', (pid,)).fetchone()[0])
        prompt = qualify.fewshot_text(examples)
        self.assertIn('manually tagged', prompt)
        self.assertIn('legacy relationship record', prompt)
        self.assertNotIn('Private context for this lead only', prompt)
        self.assertNotIn('@autotagged', prompt)

        person = server.with_owner(self.conn, dict(self.conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()))
        self.assertIn('manually tagged', qualify._packet(person, [], []))
        self.assertNotIn('Private context for this lead only', qualify._packet(person, [], []))

    def test_manual_client_tag_keeps_one_slot_among_eight_marked_examples(self):
        marked = [self.person(f'marked{i}') for i in range(8)]
        for pid in marked:
            server.api_mark(self.conn, {}, {'status': 'client'}, pid)
            self.legacy_client_tag(pid)
        self.assertEqual(len(server.fewshot(self.conn)), 8)
        tagged = self.person('taggedclient')
        self.legacy_client_tag(tagged)
        examples = server.fewshot(self.conn)
        self.assertEqual(len(examples), 8)
        self.assertIn(tagged, {e['person_id'] for e in examples})
        self.assertEqual(sum(e['feedback_source'] == 'manual_client_tag' for e in examples), 1)

    def test_explicit_no_mark_overrides_manual_client_tag_and_removal_drops_example(self):
        pid = self.person('taggedclient')
        self.legacy_client_tag(pid)
        self.assertEqual(server.fewshot(self.conn)[0]['label'], 'good')
        server.api_mark(self.conn, {}, {'status': 'no'}, pid)
        examples = server.fewshot(self.conn)
        self.assertEqual(len(examples), 1)
        self.assertEqual(examples[0]['label'], 'no')
        self.assertEqual(examples[0]['feedback_source'], 'status_mark')
        self.assertNotIn('Client', qualify.fewshot_text(examples))
        server.api_mark(self.conn, {}, {'status': None, 'relationships': []}, pid)
        self.assertEqual(server.fewshot(self.conn), [])

    def test_note_stays_local_and_tag_edit_changes_future_prompt_without_mass_rerun(self):
        pid = self.person('brand')
        server.api_mark(self.conn, {}, {'status': 'client', 'note': 'First reason'}, pid)
        first = server.fewshot(self.conn)
        prior_version = qualify.prompt_version(first)
        self.conn.execute("INSERT OR REPLACE INTO verdicts(person_id,model,prompt,score,updated_at) VALUES(?,'m',?,50,'')",
                          (pid, prior_version))
        self.conn.commit()
        server.api_mark(self.conn, {}, {'note': 'Revised reason'}, pid)
        note_only = server.fewshot(self.conn)
        self.assertEqual(qualify.prompt_version(note_only), prior_version)
        person = server.with_owner(self.conn, dict(self.conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()))
        self.assertNotIn('Revised reason', qualify._packet(person, [], []))
        server.api_tag_edit(self.conn, {}, {'add': ['Repeat orders']}, pid)
        second = server.fewshot(self.conn)
        self.assertNotEqual(qualify.prompt_version(second), prior_version)
        self.assertNotIn('Revised reason', qualify.fewshot_text(second))
        self.assertIn('Repeat orders', qualify.fewshot_text(second))
        self.assertEqual(self.conn.execute('SELECT model FROM verdicts WHERE person_id=?', (pid,)).fetchone()[0], 'm')

    def test_legacy_saved_example_note_is_removed_before_future_prompt(self):
        pid = self.person('brand')
        server.api_mark(self.conn, {}, {'status': 'client', 'note': 'Do not repeat this private note'}, pid)
        example = server.feedback_example(self.conn, pid)
        old_example = {**example, 'note': 'Do not repeat this private note'}
        db.set_setting(self.conn, 'fewshot', {'n': 1, 'examples': [old_example],
                                              'version': qualify.prompt_version([old_example])})
        self.conn.commit()
        examples = server.fewshot(self.conn)
        self.assertNotIn('note', examples[0])
        self.assertNotIn('Do not repeat this private note', qualify.fewshot_text(examples))
        self.assertNotIn('note', db.get_setting(self.conn, 'fewshot')['examples'][0])

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
            self.conn.execute("INSERT INTO verdicts(person_id,prefilter,score,model,updated_at) VALUES(?,90,90,'local:k2',?)",
                              (pid, ts))
        external_ready(self.conn, [own, other])
        self.assertEqual([r['id'] for r in server.llm_candidates(self.conn, 10, set())], [other])
        self.assertEqual([r['id'] for r in server.api_leads(self.conn, {}, {})['rows']], [other])

    def test_laya_skips_self_when_the_seed_row_is_missing(self):
        own = self.person('fortun8te')
        other = self.person('goodbrand')
        processing_modes.set_mode(self.conn, 'RLAI')
        self.conn.commit()
        answer = {q['key']: 0.8 for q in laya.QUESTIONS}
        seen = []

        def score(rows):
            seen.extend(row['id'] for row in rows)
            return {row['id']: answer for row in rows}

        with patch.object(laya, 'available', return_value=True), \
             patch.object(laya, 'cache_signature', return_value='laya:feedback-test'), \
             patch.object(laya, 'decide', side_effect=score), \
             patch.object(server.resource_budget, 'lease', side_effect=lambda *_a, **_k: nullcontext()):
            self.assertTrue(server.laya_step(self.conn))
        self.assertEqual(seen, [other])
        self.assertNotIn(own, seen)

    def test_stale_self_laya_queue_is_cleaned_without_a_signature_change(self):
        # Simulate a database created before the self queue guard existed.
        self.conn.execute('DROP TRIGGER IF EXISTS laya_queue_self_guard')
        own = self.person('FORTUN8TE')
        other = self.person('goodbrand')
        self.assertEqual([r[0] for r in self.conn.execute('SELECT person_id FROM laya_queue ORDER BY person_id')],
                         [own, other])
        db.set_setting(self.conn, 'laya_queue_signature', 'unchanged')
        self.conn.commit()
        self.conn.close()
        self.conn = db.init(str(Path(self.tmp.name) / 'leads.sqlite'))
        self.assertEqual(db.get_setting(self.conn, 'laya_queue_signature'), 'unchanged')
        self.assertEqual([r[0] for r in self.conn.execute('SELECT person_id FROM laya_queue')], [other])
        self.conn.execute("UPDATE people SET bio='changed' WHERE id=?", (own,))
        self.assertEqual([r[0] for r in self.conn.execute('SELECT person_id FROM laya_queue')], [other])

    def test_self_is_not_counted_as_remaining_bio_or_ai_work(self):
        own = self.person('fortun8te')
        other = self.person('goodbrand')
        db.set_setting(self.conn, 'bio_min', 0)
        db.set_setting(self.conn, 'llm_min', 0)
        for pid in (own, other):
            ts = self.conn.execute('SELECT updated_at FROM people WHERE id=?', (pid,)).fetchone()[0]
            self.conn.execute("INSERT INTO verdicts(person_id,prefilter,score,model,updated_at) "
                              "VALUES(?,90,90,'local:k2',?)", (pid, ts))
        external_ready(self.conn, [own, other])
        snapshot = server.progress(self.conn, [])
        self.assertEqual(snapshot['bios']['left'], 1)
        self.assertEqual(snapshot['qualify']['left'], 1)
        self.assertEqual(server.ai_left(self.conn), 1)

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
