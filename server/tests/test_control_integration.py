"""Offline coverage for shared controls across public collection and manual website reads."""
import os
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

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_settings_off_stays_off_after_lists_finish(self):
        db.set_setting(self.conn, 'qualify', True)
        db.set_setting(self.conn, 'qualify_auto', True)
        self.conn.execute("INSERT INTO seeds(handle) VALUES('seed')")
        self.conn.execute("INSERT INTO lists(seed,direction,state) VALUES('seed','following','done')")
        self.conn.commit()
        self.assertEqual(server.api_qualify(self.conn, {}, {'on': False}), {'qualify': False})
        self.assertFalse(server.auto_qualify(self.conn))
        self.assertFalse(db.get_setting(self.conn, 'qualify_auto'))
        self.assertTrue(control.stage_paused(self.conn, 'ai'))

    def test_local_laya_runs_with_external_qualification_off(self):
        pid = db.upsert_person(self.conn, {'handle': 'localonly', 'bio': 'Founder of a clothing brand'})
        self.conn.commit()
        db.set_setting(self.conn, 'qualify_auto', True)
        self.assertEqual(server.api_qualify(self.conn, {}, {'on': False, 'local_laya': True}),
                         {'qualify': False, 'local_laya': True})
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

        def pause_during_research(conn, _items):
            control.set_stage(conn, 'ai', True)
            conn.commit()

        with patch.object(server, 'research', side_effect=pause_during_research), \
                patch.object(server.qualify, 'llm_verdicts', create=True) as model:
            self.assertEqual(server.run_llm(self.conn, rows, {}), 0)
            model.assert_not_called()
        with patch.object(server.llm, 'refresh_models') as refresh:
            self.assertFalse(server.models_step(self.conn))
            refresh.assert_not_called()

        control.set_stage(self.conn, 'ai', False)
        self.conn.commit()

        def pause_during_model(_items, _examples):
            control.set_stage(self.conn, 'ai', True)
            self.conn.commit()
            return [{'score': 90, 'tier': 'hot', 'role': 'buyer', 'reason': 'old reply', 'model': 'llm'}]

        with patch.object(server, 'research'), patch.object(server.qualify, 'llm_verdicts',
                                                          side_effect=pause_during_model, create=True):
            self.assertEqual(server.run_llm(self.conn, rows, {}), 0)
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
        self.assertIn('AI scoring is off', result['site']['error'])

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
