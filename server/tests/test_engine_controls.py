"""Independent engine intent and truthful in-flight stop acknowledgement."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import engine_controls
import processing_modes as modes


class EngineControlsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'engines.sqlite'
        self.conn = sqlite3.connect(self.path)
        self.conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        modes.set_mode(self.conn, 'RLEAI')
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def state(self, **kwargs):
        return engine_controls.snapshot(modes.snapshot(self.conn), **kwargs)

    def test_independent_gates_preserve_preset_and_unrelated_tickets(self):
        k2 = modes.begin_work(self.conn, 'notes')
        laya = modes.begin_work(self.conn, 'laya')
        external = modes.begin_work(self.conn, 'external')
        modes.set_engine(self.conn, 'k2', False)
        self.assertEqual(modes.current_mode(self.conn), 'RLEAI')
        self.assertTrue(modes.allows(self.conn, 'notes'))  # retained historical influence
        self.assertIsNone(modes.begin_work(self.conn, 'notes'))
        self.assertIsNone(modes.begin_work(self.conn, 'local_qualification'))
        self.assertFalse(modes.result_current(self.conn, k2))
        self.assertTrue(modes.result_current(self.conn, laya))
        self.assertTrue(modes.result_current(self.conn, external))
        modes.set_engine(self.conn, 'k2', True)
        self.assertFalse(modes.result_current(self.conn, k2))
        modes.set_engine(self.conn, 'laya', False)
        self.assertIsNone(modes.begin_work(self.conn, 'laya'))
        self.assertIsNotNone(modes.begin_work(self.conn, 'local_qualification'))
        self.assertFalse(modes.result_current(self.conn, laya))

    def test_rules_preset_cannot_be_bypassed_and_choices_survive_mode_change(self):
        modes.set_engine(self.conn, 'k2', False)
        modes.set_mode(self.conn, 'R')
        modes.set_engine(self.conn, 'laya', True)
        self.assertIsNone(modes.begin_work(self.conn, 'laya'))
        self.assertEqual(self.state()['engines']['laya']['state'], 'off')
        modes.set_mode(self.conn, 'RLAI')
        self.assertIsNotNone(modes.begin_work(self.conn, 'laya'))
        self.assertIsNone(modes.begin_work(self.conn, 'notes'))

    def test_pause_during_call_only_acknowledges_after_completion(self):
        started, finish = threading.Event(), threading.Event()
        observed = []
        def worker():
            conn = sqlite3.connect(self.path)
            try:
                ticket = modes.begin_work(conn, 'local_qualification')
                with engine_controls.work(conn, ticket) as admitted:
                    observed.append(admitted)
                    started.set()
                    finish.wait(3)
                    observed.append(modes.result_current(conn, ticket))
            finally:
                conn.close()
        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(started.wait(3))
        try:
            with engine_controls.lock:
                modes.set_paused(self.conn, True)
                self.conn.commit()
            state = self.state()
            self.assertEqual(state['engines']['k2']['state'], 'stopping')
            self.assertFalse(state['stop_acknowledged'])
            self.assertTrue(state['engines']['k2']['active'])
        finally:
            finish.set()
            thread.join(3)
        self.assertEqual(observed, [True, False])
        self.assertEqual(self.state()['engines']['k2']['state'], 'off')
        self.assertTrue(self.state()['stop_acknowledged'])

    def test_disabled_after_ticket_before_admission_does_not_start(self):
        ticket = modes.begin_work(self.conn, 'laya')
        modes.set_engine(self.conn, 'laya', False)
        with engine_controls.work(self.conn, ticket) as admitted:
            self.assertFalse(admitted)
        self.assertTrue(self.state()['engines']['laya']['stop_acknowledged'])

    def test_error_releases_activity_and_startup_prevents_stop_ack(self):
        ticket = modes.begin_work(self.conn, 'laya')
        with self.assertRaises(RuntimeError):
            with engine_controls.work(self.conn, ticket):
                self.assertEqual(self.state()['engines']['laya']['state'], 'running')
                modes.set_engine(self.conn, 'laya', False)
                self.assertEqual(self.state()['engines']['laya']['state'], 'stopping')
                raise RuntimeError('transport failed')
        self.assertEqual(self.state()['engines']['laya']['state'], 'off')
        self.assertEqual(self.state(starting=True)['engines']['laya']['state'], 'stopping')
        self.assertFalse(self.state(starting=True)['engines']['laya']['stop_acknowledged'])

    def test_remote_runtime_activity_overrides_disabled_intent(self):
        modes.set_engine(self.conn, 'k2', False)
        state = self.state(runtimes={'k2': {'busy': True, 'ready': True}})
        self.assertEqual(state['engines']['k2']['state'], 'stopping')
        self.assertFalse(state['engines']['k2']['stop_acknowledged'])

    def test_loaded_local_model_must_unload_but_remote_model_may_remain_ready(self):
        modes.set_engine(self.conn, 'k2', False)
        local = self.state(runtimes={'k2': {'ready': True}})['engines']['k2']
        self.assertEqual(local['state'], 'stopping')
        self.assertFalse(local['stop_acknowledged'])
        remote = self.state(runtimes={'k2': {'ready': True, 'managed_local': False}})['engines']['k2']
        self.assertEqual(remote['state'], 'off')
        self.assertTrue(remote['stop_acknowledged'])

    def test_api_acknowledgement_includes_external_inflight_work(self):
        import server
        modes.set_paused(self.conn, True)
        self.conn.commit()
        class Pool:
            def idle(self):
                return False
        with patch.object(server, 'POOL', [Pool()]), \
                patch.object(server.local_model, 'status', return_value={'ready': False}), \
                patch.object(server.local_model, 'is_remote', return_value=False), \
                patch.object(server.laya, 'last_known', return_value=False), \
                patch.object(server.laya, 'runtime_status', return_value={'ready': False}), \
                patch.object(server.resource_budget, 'state', return_value={}):
            result = server.api_engines(self.conn, {}, {})
        self.assertTrue(result['external_active'])
        self.assertFalse(result['stop_acknowledged'])

    def test_idempotence_validation_and_atomic_rollback(self):
        state = modes.snapshot(self.conn)
        modes.set_engine(self.conn, 'k2', True)
        self.assertEqual(modes.snapshot(self.conn), state)
        for engine, enabled in [('invalid', True), ('k2', 1), ('laya', 'false')]:
            with self.assertRaises(ValueError):
                modes.set_engine(self.conn, engine, enabled)
        original = db.set_setting
        def fail(conn, key, value):
            if key == 'engine_k2_generation':
                raise RuntimeError('write failure')
            return original(conn, key, value)
        with patch.object(db, 'set_setting', side_effect=fail):
            with self.assertRaises(RuntimeError):
                modes.set_engine(self.conn, 'k2', False)
        self.assertEqual(modes.snapshot(self.conn), state)


if __name__ == '__main__':
    unittest.main()
