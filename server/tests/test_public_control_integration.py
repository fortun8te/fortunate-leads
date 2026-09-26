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
import public_bios
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

    def public_state(self, **heartbeat):
        db.set_setting(self.conn, 'public_bios', {**public_bios.DEFAULTS, 'on': True})
        db.set_setting(self.conn, 'public_bios_heartbeat',
                       {'epoch': db.utc_now().timestamp(), 'inflight': 1, 'healthyEgresses': 1, **heartbeat})
        self.conn.commit()
        return control.snapshot(self.conn)['stages'][1]

    def test_public_only_worker_is_running_without_chrome(self):
        self.conn.execute("INSERT INTO jobs(kind,handle) VALUES('profile','person')")
        state = self.public_state()
        self.assertEqual(state['state'], 'running')
        self.assertIn('public bios', state['now'])
        self.assertEqual(state['queue'], 1)

    def test_public_worker_reports_cooldown_and_pause(self):
        self.conn.execute("INSERT INTO jobs(kind,handle) VALUES('profile','person')")
        db.set_setting(self.conn, 'public_bios_cooldowns', {'lane': {'until': db.utc_now().timestamp() + 120}})
        state = self.public_state(healthyEgresses=0)
        self.assertEqual(state['state'], 'waiting')
        self.assertGreater(state['wait']['seconds'], 110)
        control.set_stage(self.conn, 'bios', True)
        self.conn.commit()
        self.assertEqual(control.snapshot(self.conn)['stages'][1]['state'], 'paused')

    def test_public_worker_idle_and_stale_heartbeat(self):
        self.assertEqual(self.public_state(inflight=0)['state'], 'idle')
        state = self.public_state(epoch=0)
        self.assertEqual(state['state'], 'waiting')
        self.assertNotIn('Chrome', state['now'])


if __name__ == '__main__':
    unittest.main()
