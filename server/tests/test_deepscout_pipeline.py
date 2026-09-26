"""Offline Leadscout regressions. Hermes and OpenRouter are never called."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import db, server


scout = server.deepscout


class LeadscoutPipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'leads.sqlite'))
        scout.ensure(self.conn)
        db.set_setting(self.conn, 'qualify', True)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def lead(self):
        pid = db.upsert_person(self.conn, {'handle': 'glow', 'bio': 'Founder of a skincare brand'})
        self.conn.commit()
        server.qualify_batch(self.conn)
        self.conn.execute("UPDATE verdicts SET model='offline-llm', content_fit=80, score=80 WHERE person_id=?", (pid,))
        self.conn.commit()
        return pid

    def scout_lead(self, pid, verdict='strong'):
        person = dict(self.conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone())
        scout.apply(self.conn, person, {'verdict': verdict, 'reachable': True,
                                        'summary': 'A skincare brand.', 'tags': ['Skincare'],
                                        'sources': ['https://example.com/about'],
                                        'evidence': [{'source': 'profile.bio', 'quote': 'Founder of a skincare brand'}]})
        self.conn.commit()

    def test_changed_profile_expires_scout_and_can_be_researched_again(self):
        pid = self.lead()
        self.scout_lead(pid)
        before = self.conn.execute('SELECT model, content_fit FROM verdicts WHERE person_id=?', (pid,)).fetchone()
        self.assertEqual(before['model'], 'leadscout')
        self.conn.execute("UPDATE people SET bio='Founder of a skincare brand. Ships worldwide', "
                          "updated_at='9999-01-01' WHERE id=?", (pid,))
        self.conn.commit()
        with patch.object(server.qualify, 'input_hash', side_effect=lambda p, *args: p.get('bio')):
            server.qualify_batch(self.conn)
        after = self.conn.execute('SELECT model, reason FROM verdicts WHERE person_id=?', (pid,)).fetchone()
        self.assertEqual(after['model'], 'rules')
        self.assertNotEqual(after['reason'], 'A skincare brand.')
        self.assertTrue(scout.result(self.conn, pid)['stale'])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tags WHERE person_id=? AND grp='scout'", (pid,)).fetchone()[0], 0)
        self.conn.execute("UPDATE verdicts SET model='offline-llm', content_fit=80, score=80 WHERE person_id=?", (pid,))
        self.conn.commit()
        self.assertEqual([p['id'] for p in scout.candidates(self.conn, 3, set())], [pid])

    def test_unrelated_timestamp_refresh_keeps_current_scout(self):
        pid = self.lead()
        self.scout_lead(pid)
        self.conn.execute("UPDATE people SET updated_at='9999-01-01' WHERE id=?", (pid,))
        self.conn.commit()
        server.qualify_batch(self.conn)
        self.assertEqual(self.conn.execute('SELECT model FROM verdicts WHERE person_id=?', (pid,)).fetchone()[0], 'leadscout')
        self.assertIsNotNone(scout.result(self.conn, pid))

    def test_material_edit_invalidates_current_scout_even_while_ai_paused(self):
        pid = self.lead()
        self.scout_lead(pid)
        self.conn.execute("INSERT INTO marks(person_id,status,note,updated_at) VALUES(?,'interested','Keep this',?)",
                          (pid, db.now()))
        db.set_setting(self.conn, 'qualify', False)
        self.conn.commit()
        self.conn.execute("UPDATE people SET website='https://changed.example',updated_at='9999-01-01' WHERE id=?", (pid,))
        self.conn.commit()
        row = self.conn.execute('SELECT model,score,reason FROM verdicts WHERE person_id=?', (pid,)).fetchone()
        self.assertEqual(tuple(row), ('rules', None, None))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tags WHERE person_id=? AND grp='scout'", (pid,)).fetchone()[0], 0)
        self.assertTrue(scout.result(self.conn, pid)['stale'])
        self.assertEqual(tuple(self.conn.execute('SELECT status,note FROM marks WHERE person_id=?', (pid,)).fetchone()),
                         ('interested', 'Keep this'))

    def test_minor_follower_change_does_not_spend_another_scout_run(self):
        pid = self.lead()
        self.conn.execute('UPDATE people SET followers=12000 WHERE id=?', (pid,))
        self.conn.commit()
        self.scout_lead(pid)
        self.conn.execute('UPDATE people SET followers=12001 WHERE id=?', (pid,))
        self.conn.commit()
        self.assertEqual(self.conn.execute('SELECT model FROM verdicts WHERE person_id=?', (pid,)).fetchone()[0], 'leadscout')
        self.assertFalse(scout.result(self.conn, pid)['stale'])
        self.conn.execute('UPDATE people SET followers=310000 WHERE id=?', (pid,))
        self.conn.commit()
        self.assertEqual(self.conn.execute('SELECT model FROM verdicts WHERE person_id=?', (pid,)).fetchone()[0], 'rules')
        self.assertTrue(scout.result(self.conn, pid)['stale'])

    def test_scout_verdict_clears_prior_model_claims(self):
        pid = self.lead()
        self.conn.execute("UPDATE verdicts SET role='buyer', prompt='old prompt', evidence='[\"old quote\"]' WHERE person_id=?", (pid,))
        self.scout_lead(pid)
        row = self.conn.execute('SELECT model, role, prompt, evidence FROM verdicts WHERE person_id=?', (pid,)).fetchone()
        self.assertEqual(row['model'], 'leadscout')
        self.assertEqual((row['role'], row['prompt'], row['evidence']), (None, None, None))

    def test_legacy_scout_rows_become_unverified_on_schema_upgrade(self):
        pid = self.lead()
        self.conn.execute('DROP TABLE deep_research')
        self.conn.execute("""CREATE TABLE deep_research(person_id INTEGER PRIMARY KEY, verdict TEXT, reachable INT,
            summary TEXT, tags TEXT, sources TEXT, raw TEXT, at TEXT NOT NULL)""")
        self.conn.execute("INSERT INTO deep_research VALUES(?, 'strong', 1, 'Old read', '[]', '[]', '{}', ?)", (pid, db.now()))
        self.conn.execute("UPDATE verdicts SET model='leadscout',score=90,content_fit=85,reason='Old read' WHERE person_id=?", (pid,))
        self.conn.execute("INSERT INTO tags VALUES(?,'Scout: Strong','scout','auto')", (pid,))
        self.conn.execute("INSERT INTO marks(person_id,status,note,updated_at) VALUES(?,'interested','Owner note',?)", (pid, db.now()))
        self.conn.commit()
        scout.ensure(self.conn)
        row = self.conn.execute('SELECT model,score,reason FROM verdicts WHERE person_id=?', (pid,)).fetchone()
        self.assertEqual((row['model'], row['score'], row['reason']), ('rules', None, None))
        history = scout.result(self.conn, pid)
        self.assertTrue(history['stale'])
        self.assertEqual(history['summary'], 'Old read')
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tags WHERE person_id=? AND grp='scout'", (pid,)).fetchone()[0], 0)
        mark = self.conn.execute('SELECT status,note FROM marks WHERE person_id=?', (pid,)).fetchone()
        self.assertEqual(tuple(mark), ('interested', 'Owner note'))
        db.set_setting(self.conn, 'qualify', False)
        self.conn.commit()
        self.assertEqual(server.qualify_batch(self.conn), 1)  # local rules still run while AI is paused
        self.assertEqual(self.conn.execute('SELECT model FROM verdicts WHERE person_id=?', (pid,)).fetchone()[0], 'rules')
        self.assertTrue(scout.result(self.conn, pid)['stale'])

    def test_inflight_llm_result_cannot_replace_scout(self):
        pid = self.lead()
        rows = self.conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchall()

        def answer(items, examples):
            self.scout_lead(pid)
            return [{'score': 55, 'tier': 'warm', 'role': 'buyer', 'reason': 'older model read',
                     'model': 'offline-llm', 'fit': 55, 'content_fit': 55, 'tags': []}]

        with patch.object(server.qualify, 'llm_verdicts', side_effect=answer, create=True):
            server.run_llm(self.conn, rows, {})
        row = self.conn.execute('SELECT model, content_fit FROM verdicts WHERE person_id=?', (pid,)).fetchone()
        self.assertEqual((row['model'], row['content_fit']), ('leadscout', 85))

    def test_failed_hermes_process_cannot_be_accepted_as_a_verdict(self):
        payload = json.dumps({'verdict': 'strong', 'reachable': True})
        with patch.object(scout.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, payload, 'error')):
            self.assertIsNone(scout.run({'handle': 'glow'}))

    def test_fewshot_refresh_does_not_queue_scout_for_bulk_llm_again(self):
        pid = self.lead()
        self.scout_lead(pid)
        with patch.object(server.qualify, 'prompt_version', side_effect=lambda examples: str(len(examples)), create=True):
            server.fewshot(self.conn)
            for i in range(6):
                marked = db.upsert_person(self.conn, {'handle': f'marked{i}', 'bio': 'Founder of a skincare brand'})
                self.conn.execute("INSERT INTO marks(person_id,status,updated_at) VALUES(?,'interested',?)",
                                  (marked, db.now()))
            self.conn.commit()
            server.fewshot(self.conn)
        self.assertEqual(self.conn.execute('SELECT model FROM verdicts WHERE person_id=?', (pid,)).fetchone()[0],
                         'leadscout')

    def test_stale_hermes_reply_is_not_saved_after_profile_changes(self):
        pid = self.lead()
        old = dict(self.conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone())
        self.conn.execute("UPDATE people SET bio='New profile for a different business', updated_at='9999-01-01' "
                          'WHERE id=?', (pid,))
        self.conn.commit()
        pool = scout.ScoutPool(self.conn.execute('PRAGMA database_list').fetchone()[2])
        answer = {'verdict': 'strong', 'reachable': True, 'summary': 'Old skincare read.', 'sources': []}
        with patch.object(scout, 'run', return_value=answer):
            pool._work(old)
        self.assertIsNone(scout.result(self.conn, pid))
        self.assertNotIn(pid, pool.failed)

    def test_status_counts_waiting_without_loading_candidates(self):
        pid = self.lead()
        self.assertEqual(pid, 1)
        scout.ensure(self.conn)
        with patch.object(scout, 'candidates', side_effect=AssertionError('status loaded all people')):
            with patch.object(scout, 'STATE_DB', Path(self.tmp.name) / 'missing.sqlite'):
                self.assertEqual(scout.status(self.conn)['waiting'], 1)
