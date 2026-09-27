"""Offline regressions for model identity, tag replacement and network changes."""
import importlib.util
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import db, server, external_ready

spec = importlib.util.spec_from_file_location('integrity_qualify', Path(__file__).resolve().parents[1] / 'qualify.py')
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)


class QualifierIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'leads.sqlite'))
        db.set_setting(self.conn, 'qualify', True)  # isolated test DB; every model call is mocked
        self.qualifier = patch.object(server, 'qualify', q)
        self.qualifier.start()

    def tearDown(self):
        self.qualifier.stop()
        self.conn.close()
        self.tmp.cleanup()

    def person(self, handle, bio='Skincare brand. Shop now'):
        pid = db.upsert_person(self.conn, {'handle': handle, 'bio': bio})
        return pid

    def model(self, role='buyer', fit=90):
        def result(person, tags, edges, net, examples, **kwargs):
            it = {'person': person, 'tags': tags, 'net': net}
            return q._verdict({'role': role, 'fit': fit, 'decision_maker': True, 'evidence': [it['person']['bio']]}, it['person'], it['tags'],
                               'fake-model', q.prompt_version(examples), it['net'])
        return patch.object(server.external_harness, 'broad', side_effect=result)

    def run_model(self, pid):
        external_ready(self.conn, [pid])
        rows = self.conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchall()
        return server.run_llm(self.conn, rows, {})

    def test_batch_requires_valid_unique_ids_and_allows_reordering(self):
        items = [{'person': {'handle': h, 'bio': 'Skincare brand ' + h}, 'tags': [], 'edges': []} for h in ('a', 'b')]
        base = {'role': 'buyer', 'fit': 80, 'handle': 'a', 'evidence': ['Skincare brand']}
        cases = [([dict(base, reason='missing')], [None, None]),
                 ([dict(base, id=1, handle='b', reason='b')], [None, 80]),
                 ([dict(base, id=0), dict(base, id=0)], [None, None]),
                 ([dict(base, id=True), dict(base, id=0.5)], [None, None]),
                 ([dict(base, id=1, handle='b', reason='b'), dict(base, id='0', reason='a')], [80, 80])]
        for results, expected in cases:
            with self.subTest(results=results), patch.object(q, '_providers', return_value=types.SimpleNamespace(
                    chat=lambda *a, **k: (json.dumps({'results': results}), 'fake'))):
                self.assertEqual([v['fit'] if v else None for v in q.llm_verdicts(items)], expected)

    def test_decision_maker_does_not_invent_founder(self):
        verdict = q._verdict({'role': 'buyer', 'fit': 85, 'decision_maker': True, 'evidence': ['Head of purchasing']},
                             {'bio': 'Head of purchasing at Glow skincare brand'}, [], 'fake', 'v')
        self.assertIn(('AI: Decision maker', 'ai'), verdict['tags'])
        self.assertNotIn(('Founder', 'signal'), verdict['tags'])

    def test_rerun_replaces_model_tags_and_retains_rules_and_manual(self):
        pid = self.person('glow', 'Founder of a skincare brand')
        self.conn.execute("INSERT INTO tags VALUES(?,'Keep manual','signal','manual')", (pid,))
        self.conn.execute("INSERT INTO tag_rules(tag,grp,field,match) VALUES('Keep rule','signal','bio','skincare')")
        server.qualify_batch(self.conn)
        with self.model():
            self.assertEqual(self.run_model(pid), 1)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM tags WHERE tag='AI: Top fit'").fetchone())
        with self.model('unrelated', 10):
            self.assertEqual(self.run_model(pid), 1)
        tags = {r[0] for r in self.conn.execute('SELECT tag FROM tags WHERE person_id=?', (pid,))}
        self.assertNotIn('AI: Top fit', tags)
        self.assertNotIn('Fit: strong', tags)
        self.assertTrue({'Founder', 'Keep manual', 'Keep rule'} <= tags)
        self.assertEqual(self.conn.execute('SELECT content_fit FROM verdicts WHERE person_id=?', (pid,)).fetchone()[0], 10)

    def test_client_mark_refreshes_connected_model_without_calling_model(self):
        seed = self.person('operator')
        lead = self.person('glow')
        db.add_edge(self.conn, 'operator', lead, 'following')
        server.qualify_batch(self.conn)
        with self.model():
            self.assertEqual(self.run_model(lead), 1)
        before = self.conn.execute('SELECT score FROM verdicts WHERE person_id=?', (lead,)).fetchone()[0]
        with patch.object(server.external_harness, 'broad', side_effect=AssertionError('must not call model')):
            server.set_status(self.conn, [seed], status='client')
            server.drain_network_dirty(self.conn)   # peers re-rank on the next background pass
        after = self.conn.execute('SELECT * FROM verdicts WHERE person_id=?', (lead,)).fetchone()
        self.assertGreater(after['score'], before)
        self.assertEqual(after['model'], 'fake-model')
        net = server.network_context(self.conn, [lead])[lead]
        self.assertEqual(after['score'], q.blend(90, net))
        p = server.with_owner(self.conn, dict(self.conn.execute('SELECT * FROM people WHERE id=?', (lead,)).fetchone()))
        self.assertEqual(after['input_hash'], q.input_hash(p, server.edges_of(self.conn, lead), net))
        server.set_status(self.conn, [seed], status=None, relationships=[])
        server.drain_network_dirty(self.conn)
        self.assertEqual(self.conn.execute('SELECT score FROM verdicts WHERE person_id=?', (lead,)).fetchone()[0], before)

    def test_shared_seed_yield_refreshes_unmarked_rules(self):
        marked, lead = self.person('marked'), self.person('lead')
        for pid in (marked, lead):
            db.add_edge(self.conn, 'shared', pid, 'following')
        server.qualify_batch(self.conn)
        before = self.conn.execute('SELECT score FROM verdicts WHERE person_id=?', (lead,)).fetchone()[0]
        server.set_status(self.conn, [marked], status='client')
        server.drain_network_dirty(self.conn)   # peers re-rank on the next background pass
        after = self.conn.execute('SELECT score FROM verdicts WHERE person_id=?', (lead,)).fetchone()[0]
        self.assertGreater(after, before)

    def test_prompt_version_refreshes_frozen_examples_only_eligible_rows(self):
        warm, cold = self.person('warm'), self.person('cold')
        server.qualify_batch(self.conn)
        old_examples = server.fewshot(self.conn)
        for pid, score in ((warm, 70), (cold, 10)):
            self.conn.execute("UPDATE verdicts SET model='fake',prompt=?,score=? WHERE person_id=?",
                              (q.prompt_version(old_examples), score, pid))
        with patch.object(q, 'PROMPT_VERSION', 'changed'):
            self.assertEqual(server.fewshot(self.conn), old_examples)
            self.assertEqual(db.get_setting(self.conn, 'fewshot')['version'], q.prompt_version(old_examples))
        models = dict(self.conn.execute('SELECT person_id,model FROM verdicts'))
        self.assertEqual(models, {warm: 'fake', cold: 'fake'})

    def test_profile_change_during_model_call_discards_reply(self):
        pid = self.person('glow')
        server.qualify_batch(self.conn)
        def changed(person, tags, edges, net, examples, **kwargs):
            db.upsert_person(self.conn, {'handle': 'glow', 'bio': 'Different business'})
            self.conn.commit()
            return q._verdict({'role': 'buyer', 'fit': 90, 'evidence': ['Skincare brand']}, person, [], 'fake', 'v')
        with patch.object(server.external_harness, 'broad', side_effect=changed):
            self.assertEqual(self.run_model(pid), 0)
        self.assertEqual(self.conn.execute('SELECT model FROM verdicts WHERE person_id=?', (pid,)).fetchone()[0], 'local:test')

    def test_identical_reread_does_not_requeue_rules_but_changed_bio_does(self):
        profile = {'handle': 'glow', 'bio': 'Founder of a skincare brand',
                   'bio_at': '2026-01-01', 'bio_src': 'extension'}
        pid = db.upsert_person(self.conn, profile, '2026-01-01')
        self.assertEqual(server.qualify_batch(self.conn), 1)
        initial = self.conn.execute('SELECT updated_at FROM verdicts WHERE person_id=?', (pid,)).fetchone()[0]

        db.upsert_person(self.conn, profile, '2026-01-02')
        self.assertEqual(server.qualify_batch(self.conn), 0)
        db.upsert_person(self.conn, {**profile, 'bio_at': '2026-01-03'}, '2026-01-03')
        self.assertEqual(server.qualify_batch(self.conn), 0)
        self.assertEqual(self.conn.execute('SELECT updated_at FROM verdicts WHERE person_id=?', (pid,)).fetchone()[0], initial)

        db.upsert_person(self.conn, {**profile, 'bio': 'Now selling skincare',
                                     'bio_at': '2026-01-04'}, '2026-01-04')
        self.assertEqual(server.qualify_batch(self.conn), 1)
        self.assertEqual(self.conn.execute('SELECT updated_at FROM verdicts WHERE person_id=?', (pid,)).fetchone()[0], '2026-01-04')
