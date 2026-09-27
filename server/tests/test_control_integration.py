"""Offline coverage for shared controls across public collection and manual website reads."""
import os
from contextlib import nullcontext
os.environ.setdefault('FL_NO_ORSLOT', '1')
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import control
import db
import laya
import qual_api
import server


class PublicControlIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.temp.name) / 'test.sqlite')
        lease = patch.object(server.resource_budget, 'lease', side_effect=lambda *_a, **_k: nullcontext())
        lease.start()
        self.addCleanup(lease.stop)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_settings_off_stays_off_after_lists_finish(self):
        db.set_setting(self.conn, 'qualify', True)
        db.set_setting(self.conn, 'qualify_auto', True)
        self.conn.execute("INSERT INTO seeds(handle) VALUES('seed')")
        self.conn.execute("INSERT INTO lists(seed,direction,state) VALUES('seed','following','done')")
        self.conn.commit()
        out = server.api_qualify(self.conn, {}, {'on': False})
        self.assertFalse(out['qualify'])
        self.assertEqual(out['processing']['mode'], 'RLAI')
        self.assertFalse(server.auto_qualify(self.conn))
        self.assertFalse(db.get_setting(self.conn, 'qualify_auto'))
        self.assertTrue(control.stage_paused(self.conn, 'ai'))

    def test_local_laya_runs_with_external_qualification_off(self):
        pid = db.upsert_person(self.conn, {'handle': 'localonly', 'bio': 'Founder of a clothing brand'})
        self.conn.commit()
        db.set_setting(self.conn, 'qualify_auto', True)
        out = server.api_qualify(self.conn, {}, {'on': False, 'local_laya': True})
        self.assertFalse(out['qualify'])
        self.assertTrue(out['local_laya'])
        self.assertEqual(out['processing']['mode'], 'RLAI')
        self.assertFalse(db.get_setting(self.conn, 'qualify_auto'))
        with self.assertRaises(server.Bad):
            server.api_qualify(self.conn, {}, {'on': False, 'auto': True, 'local_laya': True})
        with self.assertRaises(server.Bad):
            server.api_qualify(self.conn, {}, {'auto': True})
        server.api_scout_set(self.conn, {}, {'on': False})
        control.set_stage(self.conn, 'lists', True)
        control.set_stage(self.conn, 'bios', True)
        self.conn.commit()
        answers = {pid: {question['key']: 0.5 for question in laya.QUESTIONS}}
        with patch.object(laya, 'available', return_value=True), \
                patch.object(laya, 'decide', return_value=answers) as decide:
            for _ in range(3):
                server.laya_step(self.conn)
                if self.conn.execute('SELECT 1 FROM laya WHERE person_id=?', (pid,)).fetchone():
                    break
            self.assertTrue(self.conn.execute('SELECT 1 FROM laya WHERE person_id=?', (pid,)).fetchone())
            decide.assert_called()
            self.assertFalse(server.LLMPool().step(self.conn))
            self.assertFalse(db.get_setting(self.conn, 'scout'))
            self.assertTrue(server.api_scraper_status(self.conn, {}, {})['local_laya'])
            self.assertIn('Laya', control.snapshot(self.conn)['stages'][2]['now'])
            control.start_all(self.conn)
            self.assertFalse(db.get_setting(self.conn, 'qualify'))
            self.assertTrue(db.get_setting(self.conn, 'local_laya'))
            control.stop_all(self.conn)
            self.assertFalse(db.get_setting(self.conn, 'local_laya'))
            self.assertFalse(server.laya_step(self.conn))

    def test_external_mode_keeps_local_models_enabled(self):
        out = server.api_qualify(self.conn, {}, {'on': True, 'local_laya': True})
        self.assertEqual(out['processing']['mode'], 'RLEAI')
        self.assertTrue(out['qualify'])
        self.assertTrue(out['local_laya'])
        self.assertTrue(out['processing']['capabilities']['notes'])
        self.assertTrue(out['processing']['capabilities']['local_qualification'])
        self.assertFalse(out['processing']['capabilities']['deep_dive'])

    def test_local_rules_continue_while_collection_is_paused(self):
        control.stop_all(self.conn)
        server.api_qualify(self.conn, {}, {'on': False, 'local_laya': True})
        with patch.object(server, 'drain_network_dirty', return_value=0), \
                patch.object(server, 'qualify_batch', return_value=True) as score:
            self.assertTrue(server.background_qualify(self.conn))
            score.assert_called_once_with(self.conn)
            control.stop_all(self.conn)
            self.assertFalse(server.background_qualify(self.conn))
            self.assertEqual(score.call_count, 1)

    def test_external_model_work_stops_after_switch_off(self):
        pid = db.upsert_person(self.conn, {'handle': 'modelcandidate', 'bio': 'Founder of a clothing brand'})
        self.conn.commit()
        server.qualify_batch(self.conn)
        rows = self.conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchall()
        control.set_stage(self.conn, 'ai', False)
        self.conn.commit()

        self.conn.execute("INSERT INTO local_reviews(person_id,input_hash,status,escalation_reason,updated_at) VALUES(?,'hash','needs_research','role_unclear','now')", (pid,))
        self.conn.commit()

        def pause_during_cached_research(conn, _person):
            control.set_stage(conn, 'ai', True)
            conn.commit()
            return None

        with patch.object(server.websearch, 'cached', side_effect=pause_during_cached_research), \
                patch.object(server.external_harness, 'broad') as model:
            self.assertEqual(server.run_llm(self.conn, rows, {}), 0)
            model.assert_not_called()
        # A temporary test database must never start/unload actual local services.
        with patch.object(server.local_model, 'maintain_service') as maintain, \
                patch.object(server, 'schedule_local_services') as start:
            self.assertFalse(server.local_services_step(self.conn))
            maintain.assert_not_called()
            start.assert_not_called()

        control.set_stage(self.conn, 'ai', False)
        self.conn.commit()

        def pause_during_model(*_args, **_kwargs):
            control.set_stage(self.conn, 'ai', True)
            self.conn.commit()
            return {'score': 90, 'tier': 'hot', 'role': 'buyer', 'reason': 'old reply', 'model': 'llm'}

        with patch.object(server.external_harness, 'broad', side_effect=pause_during_model) as model:
            self.assertEqual(server.run_llm(self.conn, rows, {}), 0)
            model.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT model FROM verdicts WHERE person_id=?', (pid,)).fetchone()[0],
                         'rules')

    def test_deeper_keeps_page_facts_without_model_when_ai_off(self):
        control.stop_all(self.conn)
        pid = db.upsert_person(self.conn, {'handle': 'person', 'website': 'https://example.com'})
        self.conn.commit()
        route = next(fn for _, path, fn in qual_api.routes(server) if path.endswith('/deeper'))
        with patch.object(qual_api, 'fetch', return_value=('https://example.com', '<title>Shop</title>Add to cart')), \
                patch.object(qual_api, 'summarise') as model:
            result = route(self.conn, {}, {}, pid)
        model.assert_not_called()
        self.assertTrue(result['site']['signals']['cart'])
        self.assertIsNone(result['site']['model'])
        self.assertIsNone(result['site']['error'])
        self.assertEqual(result['site']['summary_source'], 'page_excerpt')
        self.assertEqual(result['site']['summary'], 'Add to cart')

    def test_pause_during_site_fetch_stops_new_model_call(self):
        control.set_stage(self.conn, 'ai', False)
        pid = db.upsert_person(self.conn, {'handle': 'person'})
        qual_api._ensure(self.conn)
        self.conn.commit()
        def fetch(_url):
            control.set_stage(self.conn, 'ai', True)
            self.conn.commit()
            return 'https://example.com', '<title>Shop</title>'
        with patch.object(qual_api, 'fetch', side_effect=fetch), patch.object(qual_api, 'summarise') as model:
            qual_api.read_site(self.conn, pid, 'https://example.com')
        model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
