"""Legacy imports must never leave a partly upgraded destination."""

import json
import os
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db  # noqa: E402
import migrate  # noqa: E402


class AtomicLegacyImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old = self.root / 'old.sqlite'
        self.out = self.root / 'leads.sqlite'
        old = sqlite3.connect(self.old)
        old.executescript("""
            CREATE TABLE entities(id INT, handle TEXT, platform_id TEXT, created_at TEXT, platform TEXT);
            CREATE TABLE profile_current(entity_id INT, field TEXT, value_json TEXT, observed_at TEXT);
            CREATE TABLE collections(source_entity INT, side TEXT, displayed_count INT, coverage TEXT, observed_date TEXT);
            CREATE TABLE exporter_commands(payload TEXT, type TEXT, created_at TEXT);
            CREATE TABLE edges(src INT, dst INT, observed_date TEXT, relationship TEXT);
            CREATE TABLE known_people(entity_id INT, relation TEXT, note TEXT, added_at TEXT);
            INSERT INTO entities VALUES(1,'seed','1','2026-01-01','instagram'),
                                       (2,'newperson','2','2026-01-01','instagram');
            INSERT INTO collections VALUES(1,'followers',1,'complete','2026-01-01');
            INSERT INTO edges VALUES(2,1,'2026-01-01','follows');
        """)
        old.close()

    def seeded_destination(self):
        conn = db.init(str(self.out))
        pid = db.upsert_person(conn, {'handle': 'existing', 'ig_id': '99'})
        db.add_edge(conn, 'oldseed', pid, 'followers', observed=False)
        conn.execute("INSERT INTO tags VALUES(?,?,?,'auto')", (pid, 'via @oldseed', 'source'))
        conn.execute("INSERT INTO verdicts(person_id,score,tier,reason,updated_at) VALUES(?,80,'hot','keep','2026-01-01')", (pid,))
        conn.execute("DELETE FROM settings WHERE key='edge_evidence_v1'")
        conn.commit()
        conn.close()

    def destination_state(self):
        conn = sqlite3.connect(self.out)
        try:
            return tuple(conn.iterdump())
        finally:
            conn.close()

    def assert_no_stage_left(self):
        self.assertEqual(list(self.root.glob('.leads.sqlite.import-*')), [])

    def test_bad_source_schema_leaves_existing_destination_unchanged(self):
        self.seeded_destination()
        before = self.destination_state()
        self.old.unlink()
        sqlite3.connect(self.old).close()

        with self.assertRaises(sqlite3.OperationalError):
            migrate.migrate(self.old, self.out, None)

        self.assertEqual(self.destination_state(), before)
        self.assert_no_stage_left()

    def test_bad_json_leaves_existing_destination_unchanged(self):
        self.seeded_destination()
        before = self.destination_state()
        old = sqlite3.connect(self.old)
        old.execute("INSERT INTO profile_current VALUES(2,'biography','{bad','2026-01-02')")
        old.commit()
        old.close()

        with self.assertRaises(json.JSONDecodeError):
            migrate.migrate(self.old, self.out, None)

        self.assertEqual(self.destination_state(), before)
        self.assert_no_stage_left()

    def test_late_failure_after_partial_import_leaves_destination_unchanged(self):
        self.seeded_destination()
        before = self.destination_state()

        with patch.object(migrate.db, 'add_edge', side_effect=RuntimeError('injected after person import')):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                migrate.migrate(self.old, self.out, None)

        self.assertEqual(self.destination_state(), before)
        self.assert_no_stage_left()

    def test_existing_destination_produces_separate_review_file(self):
        self.seeded_destination()
        before = self.destination_state()

        result = migrate.migrate(self.old, self.out, None)

        self.assertEqual(self.destination_state(), before)
        staged = Path(result['staged_path'])
        self.assertTrue(staged.is_file())
        conn = sqlite3.connect(staged)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM people WHERE handle='newperson'").fetchone()[0], 1)
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        finally:
            conn.close()

    def test_concurrent_destination_creation_keeps_both_databases(self):
        original_link = os.link

        def race(stage, destination):
            with sqlite3.connect(destination) as other:
                other.execute('CREATE TABLE concurrent_owner(value TEXT)')
                other.execute("INSERT INTO concurrent_owner VALUES('untouched')")
            return original_link(stage, destination)

        with patch.object(migrate.os, 'link', side_effect=race):
            result = migrate.migrate(self.old, self.out, None)
        with sqlite3.connect(self.out) as other:
            self.assertEqual(other.execute('SELECT value FROM concurrent_owner').fetchone()[0], 'untouched')
        with sqlite3.connect(result['staged_path']) as staged:
            self.assertEqual(staged.execute('SELECT count(*) FROM people').fetchone()[0], 2)
        self.assertEqual(Path(result['staged_path']).stat().st_mode & 0o777, 0o600)

    def test_active_wal_database_keeps_edits_made_during_import(self):
        self.seeded_destination()
        active = db.connect(str(self.out))
        self.addCleanup(active.close)
        active.execute("INSERT INTO settings(key,value) VALUES('before_import','from WAL')")
        active.commit()
        original_import = migrate._migrate_into

        def import_while_active(old, staged, batch):
            active.execute("INSERT INTO settings(key,value) VALUES('during_import','new edit')")
            active.commit()
            return original_import(old, staged, batch)

        with patch.object(migrate, '_migrate_into', side_effect=import_while_active):
            result = migrate.migrate(self.old, self.out, None)
        self.assertEqual(active.execute("SELECT value FROM settings WHERE key='during_import'").fetchone()[0], 'new edit')
        self.assertEqual(active.execute("SELECT count(*) FROM people WHERE handle='newperson'").fetchone()[0], 0)
        with sqlite3.connect(result['staged_path']) as staged:
            self.assertEqual(staged.execute("SELECT value FROM settings WHERE key='before_import'").fetchone()[0], 'from WAL')
            self.assertEqual(staged.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            self.assertIsNone(staged.execute("SELECT value FROM settings WHERE key='during_import'").fetchone())

    def test_missing_source_does_not_create_source_or_destination(self):
        self.old.unlink()
        with self.assertRaises(sqlite3.OperationalError):
            migrate.migrate(self.old, self.out, None)
        self.assertFalse(self.old.exists())
        self.assertFalse(self.out.exists())
        self.assert_no_stage_left()

    def test_wrong_existing_destination_is_rejected(self):
        with sqlite3.connect(self.out) as conn:
            conn.execute('CREATE TABLE unrelated(value TEXT)')
        before = self.destination_state()
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'not a Fortunate Leads'):
            migrate.migrate(self.old, self.out, None)
        self.assertEqual(self.destination_state(), before)
        self.assert_no_stage_left()

    def test_manual_history_and_current_edits_survive(self):
        self.seeded_destination()
        with sqlite3.connect(self.out) as conn:
            pid = conn.execute("SELECT id FROM people WHERE handle='existing'").fetchone()[0]
            conn.execute("INSERT INTO marks(person_id,status,note,updated_at) VALUES(?,'client','Keep this note','2026-09-01')", (pid,))
            conn.execute("INSERT INTO tags VALUES(?,'Personal note','signal','manual')", (pid,))
        before = self.destination_state()
        result = migrate.migrate(self.old, self.out, None)
        self.assertEqual(self.destination_state(), before)
        with sqlite3.connect(result['staged_path']) as conn:
            self.assertEqual(conn.execute('SELECT status,note FROM marks WHERE person_id=?', (pid,)).fetchone(), ('client', 'Keep this note'))
            self.assertEqual(conn.execute("SELECT count(*) FROM tags WHERE person_id=? AND tag='Personal note' AND source='manual'", (pid,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM edges WHERE seed='oldseed'").fetchone()[0], 1)

    def test_cli_explains_staged_import(self):
        self.seeded_destination()
        batch = self.root / 'batch.txt'
        batch.write_text('')
        run = subprocess.run([sys.executable, migrate.__file__, '--old', str(self.old),
                              '--out', str(self.out), '--batch', str(batch)],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('staged_path', run.stdout)
        self.assertIn('destination database was not changed', run.stdout)
        self.assertIn('reconciled before replacement', run.stdout)

    def test_new_destination_is_published_after_success(self):
        result = migrate.migrate(self.old, self.out, None)
        self.assertNotIn('staged_path', result)
        self.assertTrue(self.out.is_file())
        self.assert_no_stage_left()
        conn = sqlite3.connect(self.out)
        try:
            self.assertEqual(conn.execute('SELECT count(*) FROM people').fetchone()[0], 2)
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
