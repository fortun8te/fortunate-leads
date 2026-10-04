"""Backup, verify and restore helpers: a restore either lands exactly or leaves the original alone."""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db  # noqa: E402
import dbsafe  # noqa: E402


def make_database(path, people=5):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = db.init(str(path))
    for i in range(people):
        pid = db.upsert_person(conn, {'handle': f'person{i}', 'ig_id': str(1000 + i), 'bio': f'bio {i}'}, '2026-01-01')
        db.add_edge(conn, 'seed', pid, 'followers', '2026-01-01', observed=False)
    conn.execute("INSERT INTO marks(person_id,status,note,updated_at) VALUES(1,'interested','n','2026-01-01')")
    conn.commit()
    conn.close()
    return Path(path)


class DbsafeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_backup_of_a_wal_database_is_single_file_and_matches(self):
        source = make_database(self.root / 'leads.sqlite')
        # leave the source mid-WAL, as the live server does
        live = sqlite3.connect(source)
        live.execute('PRAGMA journal_mode=WAL')
        live.execute("INSERT INTO settings(key,value) VALUES('extra','1')")
        live.commit()
        info = dbsafe.backup_database(source, self.root / 'backups' / 'one.sqlite')
        live.close()
        out = self.root / 'backups' / 'one.sqlite'
        self.assertEqual(dbsafe.sidecars(out), [])
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertEqual(info['counts']['people'], 5)
        with sqlite3.connect(out) as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            self.assertEqual(conn.execute("SELECT value FROM settings WHERE key='extra'").fetchone()[0], '1')
        with self.assertRaises(FileExistsError):
            dbsafe.backup_database(source, out)

    def test_verify_backup_flags_missing_tables_and_damage(self):
        good = make_database(self.root / 'good.sqlite')
        self.assertTrue(dbsafe.verify_backup(good, full=True)['ok'])
        empty = self.root / 'empty.sqlite'
        with sqlite3.connect(empty) as conn:
            conn.execute('CREATE TABLE unrelated(x)')
        report = dbsafe.verify_backup(empty)
        self.assertFalse(report['ok'])
        self.assertIn('missing tables', ' '.join(report['problems']))
        broken = self.root / 'broken.sqlite'
        data = good.read_bytes()
        broken.write_bytes(data[:len(data) // 2])
        self.assertFalse(dbsafe.verify_backup(broken, full=True)['ok'])
        self.assertFalse(dbsafe.verify_backup(self.root / 'absent.sqlite')['ok'])

    def test_verify_never_touches_the_original(self):
        good = make_database(self.root / 'good.sqlite')
        before = (good.stat().st_mtime_ns, good.read_bytes())
        dbsafe.verify_backup(good, full=True)
        self.assertEqual((good.stat().st_mtime_ns, good.read_bytes()), before)
        self.assertEqual(dbsafe.sidecars(good), [])

    def test_restore_replaces_database_and_keeps_old_files_aside(self):
        live = make_database(self.root / 'data' / 'leads.sqlite', people=3)
        backup = make_database(self.root / 'backups' / 'b.sqlite', people=8)
        (self.root / 'data' / 'leads.sqlite-wal').write_bytes(b'')
        result = dbsafe.restore_database(backup, live, self.root / 'recovery', check_open=False)
        with sqlite3.connect(live) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM people').fetchone()[0], 8)
        self.assertEqual(live.stat().st_mode & 0o777, 0o600)
        aside = Path(result['aside'])
        with sqlite3.connect(aside / 'leads.sqlite') as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM people').fetchone()[0], 3)
        self.assertTrue((aside / 'leads.sqlite-wal').exists())
        self.assertEqual(result['counts']['people'], 8)

    def test_restore_of_a_damaged_backup_changes_nothing(self):
        live = make_database(self.root / 'data' / 'leads.sqlite', people=3)
        before = live.read_bytes()
        broken = self.root / 'broken.sqlite'
        broken.write_bytes(make_database(self.root / 'x.sqlite').read_bytes()[:4096 * 2])
        with self.assertRaises(dbsafe.RestoreError):
            dbsafe.restore_database(broken, live, self.root / 'recovery', check_open=False)
        self.assertEqual(live.read_bytes(), before)
        self.assertFalse((self.root / 'recovery').exists())

    def test_restore_refuses_a_database_another_process_has_open(self):
        live = make_database(self.root / 'data' / 'leads.sqlite')
        backup = make_database(self.root / 'b.sqlite')
        before = live.read_bytes()
        with mock.patch.object(dbsafe, 'database_in_use', return_value=True):
            with self.assertRaisesRegex(dbsafe.RestoreError, 'open in another process'):
                dbsafe.restore_database(backup, live, self.root / 'recovery')
        self.assertEqual(live.read_bytes(), before)

    def test_restore_puts_the_original_back_when_the_final_step_fails(self):
        live = make_database(self.root / 'data' / 'leads.sqlite', people=3)
        backup = make_database(self.root / 'b.sqlite', people=8)
        before = live.read_bytes()
        real_replace = os.replace

        def failing(src, dst):
            if str(dst) == str(live):
                raise OSError('simulated rename failure')
            return real_replace(src, dst)
        with mock.patch.object(dbsafe.os, 'replace', side_effect=failing):
            with self.assertRaises(OSError):
                dbsafe.restore_database(backup, live, self.root / 'recovery', check_open=False)
        self.assertEqual(live.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in live.parent.iterdir()), ['leads.sqlite'])

    def test_restore_into_a_missing_target_just_places_the_backup(self):
        backup = make_database(self.root / 'b.sqlite', people=4)
        target = self.root / 'fresh' / 'leads.sqlite'
        target.parent.mkdir()
        result = dbsafe.restore_database(backup, target, self.root / 'recovery', check_open=False)
        self.assertIsNone(result['aside'])
        with sqlite3.connect(target) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM people').fetchone()[0], 4)

    def test_schema_and_orphan_helpers(self):
        path = make_database(self.root / 'a.sqlite')
        conn = sqlite3.connect(path)
        before = dbsafe.schema_signature(conn)
        columns = dbsafe.column_map(conn)
        conn.execute('CREATE TABLE extra(x)')
        conn.execute('ALTER TABLE people ADD COLUMN scratch TEXT')
        after = dbsafe.schema_signature(conn)
        diff = dbsafe.schema_diff(before, after)
        self.assertEqual([c['name'] for c in diff['created']], ['extra'])
        self.assertIn('people', [c['name'] for c in diff['changed']])
        self.assertEqual(dbsafe.column_diff(columns, dbsafe.column_map(conn)), {'people': ['scratch']})
        self.assertEqual(dbsafe.logical_orphans(conn)['edges_without_person'], 0)
        conn.execute("INSERT INTO marks(person_id,status,note,updated_at) VALUES(9999,'no','','2026-01-01')")
        self.assertEqual(dbsafe.logical_orphans(conn)['marks_without_person'], 1)
        self.assertEqual(dbsafe.evidence_lost({'people': 5, 'edges': 5}, {'people': 5, 'edges': 4}), {'edges': (5, 4)})
        self.assertEqual(dbsafe.evidence_lost({'people': 5}, {}), {'people': (5, None)})
        conn.close()

    def test_clone_database_carries_a_nonempty_wal(self):
        source = make_database(self.root / 'src.sqlite')
        (self.root / 'src.sqlite-wal').write_bytes(b'x' * 64)
        dbsafe.clone_database(source, self.root / 'dst.sqlite', allow_copy=True)
        self.assertTrue((self.root / 'dst.sqlite-wal').exists())
        source2 = make_database(self.root / 'src2.sqlite')
        dbsafe.clone_database(source2, self.root / 'dst2.sqlite', allow_copy=True)
        self.assertFalse((self.root / 'dst2.sqlite-wal').exists())

    def test_clone_refuses_to_overwrite(self):
        source = make_database(self.root / 'src.sqlite')
        target = self.root / 'dst.sqlite'
        target.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            dbsafe.clone_file(source, target, allow_copy=True)
        self.assertEqual(target.read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main()
