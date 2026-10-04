"""Schema upgrade: the dry run changes nothing, the real run is backed up, a failure restores the backup."""
import hashlib
import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db  # noqa: E402
import dbsafe  # noqa: E402
import schema_upgrade  # noqa: E402


def make_old_database(path, people=7):
    """A database one release behind: a table and a column the current code adds are missing."""
    conn = db.init(str(path))
    for i in range(people):
        pid = db.upsert_person(conn, {'handle': f'p{i}', 'ig_id': str(900 + i)}, '2026-01-01')
        db.add_edge(conn, 'seed', pid, 'followers', '2026-01-01', observed=False)
    conn.execute("INSERT INTO marks(person_id,status,note,updated_at) VALUES(1,'client','n','2026-01-01')")
    conn.commit()
    conn.execute('DROP TABLE owner_note_reads')
    if sqlite3.sqlite_version_info >= (3, 35):
        conn.execute('ALTER TABLE people DROP COLUMN pic_attempts')
    conn.commit()
    conn.close()
    return Path(path)


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class SchemaUpgradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'data').mkdir()
        self.db = make_old_database(self.root / 'data' / 'leads.sqlite')

    def test_dry_run_reports_the_upgrade_and_leaves_the_database_alone(self):
        before = (fingerprint(self.db), self.db.stat().st_mtime_ns)
        report = schema_upgrade.dry_run(self.db)
        self.assertEqual((fingerprint(self.db), self.db.stat().st_mtime_ns), before)
        self.assertTrue(report['would_change'])
        self.assertIn('owner_note_reads', [i['name'] for i in report['schema']['created']])
        if sqlite3.sqlite_version_info >= (3, 35):
            self.assertEqual(report['columns_added'], {'people': ['pic_attempts']})
        self.assertTrue(report['safe'])
        self.assertEqual(report['evidence_rows_lost'], {})
        self.assertEqual(report['integrity_after'], 'ok')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['data'])  # snapshot scratch is gone

    def test_dry_run_of_a_current_database_changes_nothing(self):
        schema_upgrade.apply_upgrade(self.db, self.root / 'backups', check_open=False)
        report = schema_upgrade.dry_run(self.db)
        self.assertFalse(report['would_change'])

    def test_dry_run_refuses_something_that_is_not_a_workspace_database(self):
        other = self.root / 'other.sqlite'
        with sqlite3.connect(other) as conn:
            conn.execute('CREATE TABLE t(x)')
        with self.assertRaisesRegex(ValueError, 'not a Fortunate Leads database'):
            schema_upgrade.dry_run(other)

    def test_apply_backs_up_first_upgrades_and_keeps_every_row(self):
        counts = dbsafe.evidence_counts(sqlite3.connect(self.db))
        result = schema_upgrade.apply_upgrade(self.db, self.root / 'backups', check_open=False)
        backup = Path(result['backup'])
        self.assertTrue(backup.name.startswith('pre-upgrade-'))
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(dbsafe.verify_backup(backup)['counts'], counts)
        conn = sqlite3.connect(self.db)
        self.assertIn('owner_note_reads', dbsafe.table_names(conn))
        self.assertEqual(dbsafe.evidence_counts(conn), counts)
        # the backup is the OLD schema: it is the way back
        self.assertNotIn('owner_note_reads', dbsafe.table_names(sqlite3.connect(backup)))

    def test_failure_after_the_upgrade_restores_the_original_exactly(self):
        conn = sqlite3.connect(self.db)
        signature, counts = dbsafe.schema_signature(conn), dbsafe.evidence_counts(conn)
        conn.close()
        with self.assertRaises(schema_upgrade.UpgradeFailed) as caught:
            schema_upgrade.apply_upgrade(self.db, self.root / 'backups', check_open=False, inject_failure=True)
        self.assertTrue(caught.exception.rolled_back)
        conn = sqlite3.connect(self.db)
        self.assertEqual(dbsafe.schema_signature(conn), signature)
        self.assertEqual(dbsafe.evidence_counts(conn), counts)
        self.assertEqual(dbsafe.check_database(conn, full=True), [])
        # the failed (already upgraded) database is kept, not deleted
        failed = Path(caught.exception.report['failed_copy'])
        self.assertIn('owner_note_reads', dbsafe.table_names(sqlite3.connect(failed / 'leads.sqlite')))

    def test_an_exception_inside_init_restores_the_original(self):
        signature = dbsafe.schema_signature(sqlite3.connect(self.db))
        with mock.patch.object(schema_upgrade.db, 'init', side_effect=sqlite3.OperationalError('disk is full')):
            with self.assertRaises(schema_upgrade.UpgradeFailed) as caught:
                schema_upgrade.apply_upgrade(self.db, self.root / 'backups', check_open=False)
        self.assertTrue(caught.exception.rolled_back)
        self.assertEqual(dbsafe.schema_signature(sqlite3.connect(self.db)), signature)

    def test_apply_refuses_while_the_server_has_the_database_open(self):
        before = fingerprint(self.db)
        with mock.patch.object(dbsafe, 'database_in_use', return_value=True):
            with self.assertRaisesRegex(schema_upgrade.UpgradeFailed, 'stop the server'):
                schema_upgrade.apply_upgrade(self.db, self.root / 'backups')
        self.assertEqual(fingerprint(self.db), before)
        self.assertFalse((self.root / 'backups').exists())

    def test_rollback_restores_a_named_backup_and_keeps_the_replaced_files(self):
        result = schema_upgrade.apply_upgrade(self.db, self.root / 'backups', check_open=False)
        restored = schema_upgrade.rollback(result['backup'], self.db, self.root / 'recovery', check_open=False)
        self.assertNotIn('owner_note_reads', dbsafe.table_names(sqlite3.connect(self.db)))
        self.assertIn('owner_note_reads', dbsafe.table_names(sqlite3.connect(Path(restored['aside']) / 'leads.sqlite')))

    def test_command_line_exit_codes(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(schema_upgrade.main(['--db', str(self.db)]), 0)
        self.assertIn('would change anything', out.getvalue())
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            # nothing has the file open here, so the upgrade starts, fails on purpose and is rolled back: exit 3
            self.assertEqual(schema_upgrade.main(['--db', str(self.db), '--apply', '--backup-dir',
                                                  str(self.root / 'b'), '--inject-failure']), 3)
        self.assertIn('restored from', err.getvalue())
        self.assertNotEqual(schema_upgrade.main(['--db', str(self.root / 'missing.sqlite')]), 0)


class LegacyImportDryRunTest(unittest.TestCase):
    def test_dry_run_reports_additions_and_never_writes_the_destination(self):
        import migrate
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = sqlite3.connect(root / 'old.sqlite')
            old.executescript("""
                CREATE TABLE entities(id INT, handle TEXT, platform_id TEXT, created_at TEXT, platform TEXT);
                CREATE TABLE profile_current(entity_id INT, field TEXT, value_json TEXT, observed_at TEXT);
                CREATE TABLE collections(source_entity INT, side TEXT, displayed_count INT, coverage TEXT, observed_date TEXT);
                CREATE TABLE exporter_commands(payload TEXT, type TEXT, created_at TEXT);
                CREATE TABLE edges(src INT, dst INT, observed_date TEXT, relationship TEXT);
                CREATE TABLE known_people(entity_id INT, relation TEXT, note TEXT, added_at TEXT);
                INSERT INTO entities VALUES(1,'seed','1','2026-01-01','instagram'),(2,'newperson','2','2026-01-01','instagram');
                INSERT INTO collections VALUES(1,'followers',1,'complete','2026-01-01');
                INSERT INTO edges VALUES(2,1,'2026-01-01','follows');
            """)
            old.close()
            out = root / 'leads.sqlite'
            existing = make_old_database(out, people=3)
            before = (fingerprint(existing), existing.stat().st_mtime_ns)
            summary = migrate.dry_run(root / 'old.sqlite', out, None)
            self.assertEqual((fingerprint(existing), existing.stat().st_mtime_ns), before)
            self.assertEqual(summary['would_add']['people'], 2)
            self.assertEqual(sorted(p.name for p in root.iterdir() if not p.name.startswith('leads.sqlite-')),
                             ['leads.sqlite', 'old.sqlite'])
            # a destination that does not exist yet is not created either
            fresh = root / 'new' / 'leads.sqlite'
            fresh.parent.mkdir()
            self.assertEqual(migrate.dry_run(root / 'old.sqlite', fresh, None)['would_add']['people'], 2)
            self.assertFalse(fresh.exists())


if __name__ == '__main__':
    unittest.main()
