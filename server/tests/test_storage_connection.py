"""Storage lifecycle checks use isolated databases and the true implementation owner."""

import os
os.environ.setdefault('FL_NO_ORSLOT', '1')
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
from storage import identity, schema
from storage.connection import ConnectionFactory, connect, connection as connection_context, transaction


class StorageConnectionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'isolated.sqlite')
        conn = schema.init(self.path)
        conn.close()

    def assert_closed(self, conn):
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')

    def test_compatibility_exports_are_the_owner_functions(self):
        self.assertIs(db.connect, connect)
        self.assertIs(db.init, schema.init)
        self.assertIs(db.upsert_person, identity.upsert_person)

    def test_factory_preserves_defaults_and_supports_request_budgets(self):
        with ConnectionFactory(self.path).connection() as conn:
            self.assertIs(conn.row_factory, sqlite3.Row)
            self.assertEqual(conn.execute('PRAGMA busy_timeout').fetchone()[0], 15000)
            self.assertEqual(conn.execute('PRAGMA cache_size').fetchone()[0], -32768)
        self.assert_closed(conn)
        factory = ConnectionFactory(self.path, timeout=0.025, cache_kib=2048, mmap_bytes=0)
        with factory.connection() as conn:
            self.assertEqual(conn.execute('PRAGMA busy_timeout').fetchone()[0], 25)
            self.assertEqual(conn.execute('PRAGMA cache_size').fetchone()[0], -2048)
            self.assertEqual(conn.execute('PRAGMA mmap_size').fetchone()[0], 0)
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(conn.execute('PRAGMA temp_store').fetchone()[0], 1)
        self.assert_closed(conn)

    def test_transaction_commits_and_closes(self):
        with transaction(self.path) as conn:
            self.assertTrue(conn.in_transaction)
            pid = db.upsert_person(conn, {'handle': 'alice', 'ig_id': '1'})
            db.add_edge(conn, 'brand', pid, 'following')
        self.assert_closed(conn)
        with connection_context(self.path) as saved:
            self.assertEqual(saved.execute('SELECT count(*) FROM people').fetchone()[0], 1)
            self.assertEqual(saved.execute('SELECT count(*) FROM current_edges').fetchone()[0], 1)

    def test_failed_import_rolls_back_profiles_evidence_and_checkpoints(self):
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            with ConnectionFactory(self.path).transaction() as conn:
                pid = db.upsert_person(conn, {'handle': 'alice', 'ig_id': '1'})
                db.add_edge(conn, 'brand', pid, 'following')
                db.observe_edge(conn, 'brand', pid, 'following', 'page-1', None, db.now())
                db.queue_list(conn, 'brand', 'following')
                raise RuntimeError('injected after checkpoint persistence')
        self.assert_closed(conn)
        with connection_context(self.path) as saved:
            for table in ('people', 'edges', 'edge_observations', 'edge_evidence', 'lists', 'list_runs', 'jobs', 'network_dirty'):
                self.assertEqual(saved.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0, table)

    def test_interrupt_rolls_back_and_closes(self):
        with self.assertRaises(KeyboardInterrupt):
            with transaction(self.path) as conn:
                db.upsert_person(conn, {'handle': 'alice'})
                raise KeyboardInterrupt()
        self.assert_closed(conn)
        with connection_context(self.path) as saved:
            self.assertEqual(saved.execute('SELECT count(*) FROM people').fetchone()[0], 0)

    def test_transaction_protects_ddl_and_context_does_not_commit_unfinished_work(self):
        with self.assertRaises(RuntimeError):
            with transaction(self.path) as conn:
                conn.execute('CREATE TABLE import_test(value TEXT)')
                raise RuntimeError('injected')
        with connection_context(self.path) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='import_test'").fetchone())
            db.upsert_person(conn, {'handle': 'unfinished'})
        with connection_context(self.path) as saved:
            self.assertEqual(saved.execute('SELECT count(*) FROM people').fetchone()[0], 0)

    def test_request_lock_timeout_is_bounded(self):
        writer = connect(self.path)
        self.addCleanup(writer.close)
        writer.execute('BEGIN IMMEDIATE')
        start = time.monotonic()
        with self.assertRaises(sqlite3.OperationalError):
            with transaction(self.path, timeout=0.025, cache_kib=2048) as conn:
                db.set_setting(conn, 'test', True)
        self.assertLess(time.monotonic() - start, 1.0)
        self.assert_closed(conn)

    def test_wal_transition_is_not_repeated_for_initialized_file(self):
        statements = []
        real_connect = sqlite3.connect

        def traced(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with patch('storage.connection.sqlite3.connect', side_effect=traced):
            connect(self.path).close()
        self.assertIn('PRAGMA journal_mode', statements)
        self.assertNotIn('PRAGMA journal_mode=WAL', statements)

    def test_schema_initialization_failure_closes_the_connection(self):
        conn = connect(self.path)
        with patch.object(schema, 'connect', return_value=conn), \
                patch.object(schema, 'migrate_statuses', side_effect=RuntimeError('injected migration failure')):
            with self.assertRaisesRegex(RuntimeError, 'migration failure'):
                schema.init(self.path)
        self.assert_closed(conn)
        with connection_context(self.path) as saved:
            self.assertEqual(saved.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_connection_configuration_failure_closes_the_connection(self):
        class FailingConnection(sqlite3.Connection):
            def execute(self, statement, *args):
                if statement.startswith('PRAGMA cache_size'):
                    raise sqlite3.OperationalError('injected configuration failure')
                return super().execute(statement, *args)

        conn = sqlite3.connect(self.path, factory=FailingConnection)
        with patch('storage.connection.sqlite3.connect', return_value=conn):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'configuration failure'):
                connect(self.path)
        self.assert_closed(conn)

    def test_invalid_resource_options_rejected_before_open(self):
        for options in ({'timeout': -1}, {'timeout': float('nan')}, {'timeout': True},
                        {'cache_kib': 0}, {'cache_kib': -1}, {'mmap_bytes': -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                connect(self.path, **options)


if __name__ == '__main__':
    unittest.main()
