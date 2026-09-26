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
                                        'sources': ['https://example.com/about']})
        self.conn.commit()

    def test_profile_refresh_keeps_scout_as_final_verdict(self):
        pid = self.lead()
        self.scout_lead(pid)
        before = self.conn.execute('SELECT model, content_fit FROM verdicts WHERE person_id=?', (pid,)).fetchone()
        self.assertEqual(before['model'], 'leadscout')
        self.conn.execute("UPDATE people SET bio='Founder of a skincare brand. Ships worldwide', "
                          "updated_at='9999-01-01' WHERE id=?", (pid,))
        self.conn.commit()
        server.qualify_batch(self.conn)
        after = self.conn.execute('SELECT model, content_fit, reason FROM verdicts WHERE person_id=?', (pid,)).fetchone()
        self.assertEqual(after['model'], 'leadscout')
        self.assertGreaterEqual(after['content_fit'], 85)
        self.assertEqual(after['reason'], 'A skincare brand.')

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
