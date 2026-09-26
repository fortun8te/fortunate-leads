"""Legacy imports keep seed identities and either commit or release their transaction."""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
import migrate


class MigrationIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old = self.root / 'legacy.sqlite'
        self.out = self.root / 'leads.sqlite'
        with sqlite3.connect(self.old) as conn:
            conn.executescript('''
                CREATE TABLE entities(id TEXT PRIMARY KEY, platform TEXT, handle TEXT, platform_id TEXT, created_at TEXT);
                CREATE TABLE collections(source_entity TEXT, side TEXT, displayed_count INT, coverage TEXT, observed_date TEXT);
                CREATE TABLE profile_current(entity_id TEXT, field TEXT, value_json TEXT, observed_at TEXT);
                CREATE TABLE exporter_commands(payload TEXT, type TEXT, created_at TEXT);
                CREATE TABLE edges(src TEXT, dst TEXT, relationship TEXT, observed_date TEXT);
                CREATE TABLE known_people(entity_id TEXT, relation TEXT, note TEXT, added_at TEXT);
            ''')
        conn.close()
        db.init(self.out).close()

    def tearDown(self):
        self.tmp.cleanup()

    def source(self, entities, seeds):
        with sqlite3.connect(self.old) as conn:
            conn.execute('DELETE FROM entities')
            conn.execute('DELETE FROM collections')
            conn.executemany("INSERT INTO entities VALUES(?,'instagram',?,?,'2020-01-01')", entities)
            conn.executemany("INSERT INTO collections VALUES(?,'followers',100,'complete','2020-01-01')",
                             [(seed,) for seed in seeds])
        conn.close()

    def rows(self, sql):
        conn = sqlite3.connect(self.out)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def snapshot(self):
        return {table: self.rows('SELECT * FROM ' + table) for table in
                ('people', 'seeds', 'edges', 'lists', 'marks', 'jobs', 'tags')}

    def run_import(self):
        # An existing destination is never overwritten: the import is staged for review.
        # Accept it here, as the operator would, so repeat imports build on the result.
        summary = migrate.migrate(self.old, self.out, None)
        staged = summary.pop('staged_path', None)
        if staged:
            os.replace(staged, self.out)
        return summary

    def test_conflicts_rejected_in_both_entity_orders_and_on_repeat(self):
        entities = [('A', 'brand', '1'), ('B', 'Brand', '2')]
        for seeds in (['B'], ['A', 'B']):
            for ordered in (entities, list(reversed(entities))):
                with self.subTest(seeds=seeds, ordered=ordered):
                    self.source(ordered, seeds)
                    before = self.snapshot()
                    for _ in range(2):
                        with self.assertRaisesRegex(ValueError, 'seed account identity changed'):
                            self.run_import()
                        self.assertEqual(self.snapshot(), before)

    def test_blank_ids_import_as_missing_and_reimport(self):
        self.source([('A', 'alpha', ''), ('B', 'beta', '  '), ('C', 'gamma', None)], ['A', 'B', 'C'])
        first = self.run_import()
        self.assertEqual(self.run_import(), first)
        self.assertEqual(self.rows('SELECT handle,ig_id FROM people ORDER BY handle'),
                         [('alpha', None), ('beta', None), ('gamma', None)])

    def test_blank_import_does_not_erase_known_person_identity(self):
        conn = db.connect(self.out)
        db.upsert_person(conn, {'handle': 'brand', 'ig_id': '1'})
        conn.execute("INSERT INTO seeds(handle) VALUES('brand')")
        conn.commit()
        conn.close()
        self.source([('A', 'BRAND', '')], ['A'])
        self.run_import()
        self.assertEqual(self.rows('SELECT ig_id FROM people'), [('1',)])
        self.source([('A', 'brand', '2')], ['A'])
        with self.assertRaisesRegex(ValueError, 'seed account identity changed'):
            self.run_import()

    def test_missing_destination_identity_is_filled_and_conflict_preserved(self):
        self.source([('A', 'Brand', '1')], ['A'])
        for value in (None, '', '  ', '1', '2'):
            with self.subTest(value=value):
                conn = db.connect(self.out)
                conn.execute('DELETE FROM people')
                conn.execute('DELETE FROM seeds')
                conn.execute("INSERT INTO seeds(handle,ig_id) VALUES('brand',?)", (value,))
                conn.commit()
                conn.close()
                before = self.snapshot()
                if value == '2':
                    with self.assertRaises(ValueError):
                        self.run_import()
                    self.assertEqual(self.snapshot(), before)
                else:
                    first = self.run_import()
                    self.assertEqual(self.run_import(), first)
                    self.assertEqual(self.rows('SELECT ig_id FROM seeds'), [('1',)])

    def test_case_normalization_preserves_completed_collection(self):
        self.source([('A', 'BRAND', '1'), ('B', 'bob', '2')], ['A'])
        with sqlite3.connect(self.old) as conn:
            conn.execute("INSERT INTO edges VALUES('B','A','follows','2020-01-01')")
        conn.close()
        first = self.run_import()
        self.assertEqual(self.run_import(), first)
        self.assertEqual(self.rows('SELECT seed,state,received,total FROM lists'), [('brand', 'done', 1, 100)])

    def test_same_id_aliases_can_be_imported_twice(self):
        self.source([('A', 'oldname', '1'), ('B', 'NewName', '1')], ['A', 'B'])
        first = self.run_import()
        self.assertEqual(self.run_import(), first)
        # Equal timestamps give no rename order: the first handle stays and the alias folds into it.
        self.assertEqual(self.rows('SELECT handle,ig_id FROM people'), [('oldname', '1')])
        self.assertEqual(self.rows('SELECT handle,ig_id FROM seeds ORDER BY handle'), [('oldname', '1')])

    def test_source_connection_closes_when_destination_initialization_fails(self):
        original_connect = sqlite3.connect
        opened = []

        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        with patch.object(sqlite3, 'connect', side_effect=connect), patch.object(db, 'init', side_effect=RuntimeError('init failed')):
            with self.assertRaisesRegex(RuntimeError, 'init failed'):
                self.run_import()
        self.assertTrue(opened)   # staging opens extra connections; every one must be closed
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')

    def test_retained_failure_rolls_back_closes_both_connections_and_releases_lock(self):
        self.source([('A', 'brand', '1'), ('B', 'bob', '2')], ['A'])
        with sqlite3.connect(self.old) as conn:
            conn.execute("INSERT INTO edges VALUES('B','A','follows','2020-01-01')")
        conn.close()
        before = self.snapshot()
        original_connect, original_add = sqlite3.connect, db.add_edge
        for failure in (RuntimeError, KeyboardInterrupt):
            with self.subTest(failure=failure):
                opened, retained = [], []

                def connect(*args, **kwargs):
                    connection = original_connect(*args, **kwargs)
                    opened.append(connection)
                    return connection

                def fail_after_edge(*args, **kwargs):
                    original_add(*args, **kwargs)
                    raise failure('interrupted after writing')

                with patch.object(sqlite3, 'connect', side_effect=connect), patch.object(db, 'add_edge', side_effect=fail_after_edge):
                    try:
                        self.run_import()
                    except BaseException as exc:
                        retained.append(exc)
                self.assertEqual(len(retained), 1)
                self.assertIsInstance(retained[0], failure)
                self.assertGreaterEqual(len(opened), 2)
                for connection in opened:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        connection.execute('SELECT 1')
                self.assertEqual(self.snapshot(), before)
                writer = original_connect(self.out, timeout=0.1)
                try:
                    writer.execute('BEGIN IMMEDIATE')
                    writer.rollback()
                finally:
                    writer.close()
        self.assertEqual(self.run_import()['edges'], 1)


if __name__ == '__main__':
    unittest.main()
