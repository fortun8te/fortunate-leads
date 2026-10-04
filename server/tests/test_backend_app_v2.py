"""Workspace isolation and complete application listener lifecycle."""

import os
os.environ.setdefault('FL_NO_ORSLOT', '1')

import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
import engine_start
import k2_connection
import llm
from backend.app import Application
from backend.common import AppConfig, Bad


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = AppConfig(str(Path(self.temp.name) / 'leads.sqlite'), port=0)
        self.app = Application(self.config)
        self.addCleanup(self.app.close)

    def wait_for(self, predicate, seconds=3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_construction_opens_no_data_or_services(self):
        with patch.object(db, 'connect') as connect, patch.object(engine_start, 'start') as start, \
                patch.object(threading.Thread, 'start') as threads:
            other = Application(self.config)
            self.addCleanup(other.close)
        connect.assert_not_called()
        start.assert_not_called()
        threads.assert_not_called()
        self.assertIsNone(other.qualification._research_pool)
        self.assertIsNone(other.qualification._llm_pool)
        self.assertIsNone(other.photos._pic_opener)

    def test_workspace_caches_and_photo_cursors_are_independent(self):
        other = Application(AppConfig(str(Path(self.temp.name) / 'other.sqlite'), port=0))
        self.addCleanup(other.close)
        self.app.cache.values['example'] = {'saved': True}
        self.app.cache.seed_links[0] = 'first-workspace'
        self.app.photos.check_id = 41
        self.app.collection.local_coverage_cache['example'] = 'first-workspace'
        self.assertEqual(other.cache.values, {})
        self.assertEqual(other.cache.seed_links, [None])
        self.assertEqual(other.photos.check_id, 0)
        self.assertEqual(other.collection.local_coverage_cache, {})

    def test_saved_data_workspace_cannot_start_or_change_host_services(self):
        conn = db.init(self.config.db)
        self.addCleanup(conn.close)
        with patch.object(engine_start, 'start') as start, \
                patch.object(k2_connection, 'save') as save, \
                patch.object(llm, 'add_key') as add_key:
            with self.assertRaises(Bad):
                self.app.collection.api_engine_start(conn, {}, {})
            with self.assertRaises(Bad):
                self.app.collection.api_control_set(conn, {}, {'action': 'connect_accounts'})
            with self.assertRaises(Bad):
                self.app.processing.api_k2_connection(conn, {}, {'location': 'this_mac'})
            with self.assertRaises(Bad):
                self.app.processing.api_llm_key_add(conn, {}, {'key': 'test'})
        start.assert_not_called()
        save.assert_not_called()
        add_key.assert_not_called()

    def test_other_thread_close_stops_listener_and_joins_serve(self):
        errors = []
        def serve():
            try:
                self.app.serve(workers=False)
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=serve)
        thread.start()
        self.assertTrue(self.wait_for(lambda: self.app.server is not None and self.app.serving.is_set()))
        port = self.app.server.server_address[1]
        self.assertEqual(self.app.config.port, port)
        self.assertEqual(self.app.close(), [])
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.app.close(), [])
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', port), timeout=0.2)

    def test_startup_failure_closes_already_bound_listener(self):
        with patch.object(self.app, 'start_workers', side_effect=RuntimeError('start failed')):
            with self.assertRaisesRegex(RuntimeError, 'start failed'):
                self.app.serve()
        self.assertTrue(self.app.closed)
        self.assertEqual(self.app.server.fileno(), -1)

    def test_close_before_serve_loop_never_waits_for_unstarted_loop(self):
        starting, release = threading.Event(), threading.Event()
        errors = []
        def startup():
            starting.set()
            release.wait(3)
        def serve():
            try:
                self.app.serve()
            except BaseException as exc:
                errors.append(exc)
        with patch.object(self.app, 'start_workers', side_effect=startup):
            thread = threading.Thread(target=serve)
            thread.start()
            self.assertTrue(starting.wait(3))
            began = time.monotonic()
            self.assertEqual(self.app.close(timeout=0.2), [])
            self.assertLess(time.monotonic() - began, 0.5)
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_repeated_close_reports_external_work_until_it_finishes(self):
        class Pool:
            running = True
            def idle(self):
                return not self.running
            def shutdown(self, wait=False):
                self.wait = wait
        pool = Pool()
        self.app.qualification._llm_pool = pool
        self.assertIn('external_ai', self.app.close())
        self.assertIn('external_ai', self.app.close())
        pool.running = False
        self.assertEqual(self.app.close(), [])
        self.assertFalse(pool.wait)

    def test_saved_data_only_primary_workspace_rejects_settings_and_workers(self):
        root = Path(self.temp.name)
        config = AppConfig(str(root / 'data' / 'leads.sqlite'), port=0,
                           root=root, saved_data_only=True)
        primary_preview = Application(config)
        self.addCleanup(primary_preview.close)
        self.assertTrue(config.is_primary_workspace)
        self.assertFalse(config.host_operations_allowed)
        # Even a settings file next to the primary database is a host setting
        # when the operator explicitly requested a saved-data-only preview.
        with self.assertRaises(Bad):
            primary_preview.processing.require_settings_write(root / 'data' / 'openrouter.json')
        with self.assertRaisesRegex(RuntimeError, 'Background work is disabled'):
            primary_preview.start_workers()
        self.assertEqual(primary_preview.supervisor.threads, [])

    def test_failed_worker_thread_start_does_not_break_shutdown(self):
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('cannot start')):
            with self.assertRaisesRegex(RuntimeError, 'cannot start'):
                self.app.supervisor.start('test', lambda conn: False, 1, 1)
        self.assertEqual(self.app.supervisor.threads, [])
        self.assertEqual(self.app.close(), [])

    def test_failed_service_thread_start_releases_lock_and_has_no_unstarted_join(self):
        root = Path(self.temp.name)
        config = AppConfig(str(root / 'data' / 'leads.sqlite'), root=root)
        primary = Application(config)
        self.addCleanup(primary.close)
        Path(config.db).parent.mkdir()
        conn = db.init(config.db)
        self.addCleanup(conn.close)
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('cannot start')):
            with self.assertRaisesRegex(RuntimeError, 'cannot start'):
                primary.local_services.schedule(conn)
        self.assertFalse(primary.local_services.lock.locked())
        self.assertIsNone(primary.local_services.thread)
        self.assertEqual(primary.local_services.state['state'], 'failed')
        self.assertEqual(primary.close(), [])


if __name__ == '__main__':
    unittest.main()
