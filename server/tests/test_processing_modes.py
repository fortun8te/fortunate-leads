"""Mode migration, cumulative capabilities and stale-result guards; no model calls."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import control
import db
import processing_modes as modes


class ProcessingModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'modes.sqlite'
        self.conn = sqlite3.connect(self.path)
        self.conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def select(self, mode):
        result = modes.set_mode(self.conn, mode)
        self.conn.commit()
        return result

    def test_legacy_read_preserves_choice_without_writing(self):
        self.assertEqual(modes.current_mode(self.conn), 'R')
        db.set_setting(self.conn, 'local_laya', True)
        self.assertEqual(modes.current_mode(self.conn), 'RLAI')
        db.set_setting(self.conn, 'qualify', True)
        self.assertEqual(modes.current_mode(self.conn), 'RLEAI')
        self.assertIsNone(db.get_setting(self.conn, 'processing_mode'))

    def test_capabilities_are_cumulative_and_research_opt_in(self):
        for mode in modes.MODES:
            state = self.select(mode)
            self.assertTrue(state['capabilities']['rules'])
            self.assertTrue(state['capabilities']['scraping'])
            for stage in ('laya', 'notes', 'local_qualification'):
                self.assertEqual(modes.allows(self.conn, stage), mode != 'R')
            self.assertEqual(modes.allows(self.conn, 'external'), mode == 'RLEAI')
            self.assertFalse(modes.allows(self.conn, 'deep_dive'))
        db.set_setting(self.conn, 'scout', True)
        self.assertTrue(modes.allows(self.conn, 'deep_dive'))
        self.select('RLAI')
        self.select('RLEAI')
        self.assertFalse(modes.allows(self.conn, 'deep_dive'))

    def test_explicit_mode_wins_over_stale_legacy_flags(self):
        self.select('R')
        db.set_setting(self.conn, 'qualify', True)
        db.set_setting(self.conn, 'local_laya', True)
        self.assertFalse(modes.allows(self.conn, 'notes'))
        self.assertFalse(modes.allows(self.conn, 'external'))

    def test_no_permission_ticket_and_invalid_stage(self):
        self.assertIsNone(modes.begin_work(self.conn, 'notes'))
        self.assertFalse(modes.result_current(self.conn, None))
        with self.assertRaises(ValueError):
            modes.allows(self.conn, 'typo')
        with self.assertRaises(ValueError):
            modes.begin_work(self.conn, 'typo')

    def test_reselect_is_idempotent_but_downgrade_upgrade_invalidates_work(self):
        self.select('RLAI')
        ticket = modes.begin_work(self.conn, 'notes')
        self.select('RLAI')
        self.assertTrue(modes.result_current(self.conn, ticket))
        self.select('R')
        self.select('RLAI')
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertFalse(modes.result_current(self.conn, ticket))
        self.conn.rollback()

    def test_atomic_failure_preserves_outer_transaction(self):
        self.select('RLAI')
        before = modes.snapshot(self.conn)
        db.set_setting(self.conn, 'unrelated', 'keep')
        original = db.set_setting
        def fail(conn, key, value):
            if key == 'qualify':
                raise RuntimeError('storage failure')
            original(conn, key, value)
        with patch.object(db, 'set_setting', side_effect=fail):
            with self.assertRaises(RuntimeError):
                modes.set_mode(self.conn, 'RLEAI')
        self.assertEqual(modes.snapshot(self.conn), before)
        self.assertEqual(db.get_setting(self.conn, 'unrelated'), 'keep')
        self.assertTrue(self.conn.in_transaction)

    def test_invalid_mode_does_not_mutate_or_start_transaction(self):
        for invalid in (None, True, {}, 'local', 'rlai'):
            with self.assertRaises(ValueError):
                modes.set_mode(self.conn, invalid)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM settings').fetchone()[0], 0)

    def test_atomic_mode_change_remains_uncommitted_until_caller_saves(self):
        observer = sqlite3.connect(self.path)
        try:
            modes.set_mode(self.conn, 'RLEAI')
            self.assertEqual(modes.current_mode(observer), 'R')
            self.conn.commit()
            self.assertEqual(modes.current_mode(observer), 'RLEAI')
            self.assertTrue(db.get_setting(observer, 'local_laya'))
            self.assertTrue(db.get_setting(observer, 'qualify'))
            self.assertFalse(db.get_setting(observer, 'qualify_auto'))
        finally:
            observer.close()

    def test_write_lock_serializes_mode_switch_with_result_save(self):
        self.select('RLAI')
        ticket = modes.begin_work(self.conn, 'notes')
        other = sqlite3.connect(self.path, timeout=0)
        try:
            self.conn.execute('BEGIN IMMEDIATE')
            self.assertTrue(modes.result_current(self.conn, ticket))
            with self.assertRaises(sqlite3.OperationalError):
                modes.set_mode(other, 'R')
            db.set_setting(self.conn, 'saved_result', 'valid before downgrade')
            self.conn.commit()
            modes.set_mode(other, 'R')
            other.commit()
            self.assertFalse(modes.result_current(self.conn, ticket))
        finally:
            other.close()

    def test_background_pause_preserves_mode_capabilities_and_other_settings(self):
        self.select('RLEAI')
        for key,value in (('scout',True),('paused_lists',False),('paused_bios',False),
                          ('sentinel_cache',{'keep':'yes'}),('local_queue_seed_cursor',123)):
            db.set_setting(self.conn,key,value)
        self.conn.commit()
        before = dict(self.conn.execute('SELECT key,value FROM settings'))
        result = modes.set_paused(self.conn,True)
        self.conn.commit()
        self.assertEqual(result['mode'],'RLEAI')
        self.assertTrue(result['paused'])
        for stage in modes.AI_STAGES:
            self.assertTrue(modes.allows(self.conn,stage))
            self.assertIsNone(modes.begin_work(self.conn,stage))
        self.assertIsNotNone(modes.begin_work(self.conn,'rules'))
        self.assertIsNotNone(modes.begin_work(self.conn,'scraping'))
        after = dict(self.conn.execute('SELECT key,value FROM settings'))
        for key,value in before.items():
            if key != 'processing_generation':
                self.assertEqual(after[key],value)
        generation = result['generation']
        self.assertEqual(modes.set_paused(self.conn,True)['generation'],generation)
        self.assertEqual(modes.set_paused(self.conn,False)['generation'],generation+1)

    def test_pause_and_resume_invalidates_previous_ai_tickets(self):
        self.select('RLAI')
        ticket = modes.begin_work(self.conn,'notes')
        modes.set_paused(self.conn,True)
        self.assertFalse(modes.result_current(self.conn,ticket))
        modes.set_paused(self.conn,False)
        self.assertFalse(modes.result_current(self.conn,ticket))
        self.assertTrue(modes.result_current(self.conn,modes.begin_work(self.conn,'notes')))

    def test_pause_validates_boolean_and_preserves_outer_transaction_on_failure(self):
        for invalid in (None,0,1,'true',{}):
            with self.assertRaises(ValueError):
                modes.set_paused(self.conn,invalid)
        self.assertFalse(self.conn.in_transaction)
        self.select('RLAI')
        before = modes.snapshot(self.conn)
        db.set_setting(self.conn,'unrelated','keep')
        original = db.set_setting
        def fail(conn,key,value):
            if key == 'processing_paused':
                raise RuntimeError('storage failure')
            original(conn,key,value)
        with patch.object(db,'set_setting',side_effect=fail):
            with self.assertRaises(RuntimeError):
                modes.set_paused(self.conn,True)
        self.assertEqual(modes.snapshot(self.conn),before)
        self.assertEqual(db.get_setting(self.conn,'unrelated'),'keep')

    def test_legacy_controls_keep_local_models_with_external(self):
        control.set_stage(self.conn, 'ai', False)
        self.assertEqual(modes.current_mode(self.conn), 'RLEAI')
        self.assertTrue(db.get_setting(self.conn, 'local_laya'))
        control.set_stage(self.conn, 'ai', True)
        self.assertEqual(modes.current_mode(self.conn), 'RLAI')
        control.stop_all(self.conn)
        self.assertEqual(modes.current_mode(self.conn), 'R')
        self.assertTrue(control.stage_paused(self.conn, 'bios'))
        self.assertTrue(control.stage_paused(self.conn, 'lists'))
        control.resume_all(self.conn)
        self.assertEqual(modes.current_mode(self.conn), 'R')


if __name__ == '__main__':
    unittest.main()
