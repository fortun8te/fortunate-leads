"""Large intersections must preserve ordering, canonical degrees and evidence."""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
import connection_graph
from connection_graph import compare


class VariableLimitedConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        # Python 3.9 lacks setlimit; enforce the older SQLite cap in either runtime.
        if len(parameters) > 999:
            raise AssertionError('query exceeds 999 bound variables')
        return super().execute(sql, parameters)


class ConnectionScalingTests(unittest.TestCase):
    def test_temp_store_and_caller_temp_objects_are_preserved(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        conn.execute('PRAGMA temp_store=MEMORY')
        conn.execute("INSERT INTO seeds(handle) VALUES('alice')")
        conn.execute("INSERT INTO seeds(handle) VALUES('bob')")
        conn.commit()
        compare(conn, 'alice', 'bob')
        self.assertEqual(conn.execute('PRAGMA temp_store').fetchone()[0], 2)
        self.assertFalse(conn.in_transaction)
        conn.execute('CREATE TEMP TABLE caller_data(value INT)')
        conn.execute('INSERT INTO caller_data VALUES(42)')
        compare(conn, 'alice', 'bob')
        self.assertEqual(conn.execute('SELECT value FROM caller_data').fetchone()[0], 42)
        self.assertTrue(conn.in_transaction)
        self.assertEqual(conn.execute('PRAGMA temp_store').fetchone()[0], 2)

    def test_dense_compare_never_materializes_endpoint_graph_in_python(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        stamp = '2026-01-01T00:00:00+00:00'
        conn.executemany('INSERT INTO seeds(handle) VALUES(?)', [('alice',), ('bob',)])
        conn.executemany('INSERT INTO people(id,handle,first_seen,updated_at) VALUES(?,?,?,?)',
                         ((i, f'user{i:04}', stamp, stamp) for i in range(1, 5001)))
        conn.executemany('INSERT INTO edges VALUES(?,?,?,?)',
                         ((seed, i, 'following', stamp) for seed in ('alice', 'bob')
                          for i in range(1, 5001)))
        with patch.object(connection_graph._Graph, 'observations', side_effect=AssertionError('full endpoint load')), \
             patch.object(connection_graph._Graph, 'degrees', side_effect=AssertionError('Python degree sets')):
            result = compare(conn, 'alice', 'bob', 20)
        self.assertEqual(result['total_candidates'], 5000)
        self.assertEqual([node['node']['handle'] for node in result['connectors']],
                         [f'user{i:04}' for i in range(1, 21)])
        self.assertTrue(all(node['rank']['observed_degree'] == 2 for node in result['connectors']))

    def test_dense_candidates_chunking_and_top_k(self):
        conn = sqlite3.connect(':memory:', factory=VariableLimitedConnection)
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        if hasattr(conn, 'setlimit'):
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        stamp = '2026-01-01T00:00:00+00:00'
        conn.executemany('INSERT INTO people(id,ig_id,handle,first_seen,updated_at) VALUES(?,?,?,?,?)',
                         ((i, str(i), f'user{i:04}', stamp, stamp) for i in range(1, 1201)))
        conn.executemany('INSERT INTO seeds(handle,ig_id) VALUES(?,?)',
                         [('alice', None), ('bob', None), ('other', None),
                          ('user0899', '899'), ('oldhub', '899')])
        conn.execute('INSERT INTO people(id,handle,first_seen,updated_at) VALUES(1201,?,?,?)',
                     ('oldhub', stamp, stamp))
        conn.executemany('INSERT INTO edges VALUES(?,?,?,?)',
                         ((seed, i, 'following', stamp) for seed in ('alice', 'bob')
                          for i in range(1, 901)))
        conn.executemany("INSERT INTO edges VALUES('other',?,'following',?)",
                         ((i, stamp) for i in range(1, 101)))
        conn.executemany('INSERT INTO edges VALUES(?,?,?,?)',
                         ((seed, i, direction, stamp) for seed in ('user0899', 'oldhub')
                          for i in range(901, 1201) for direction in ('following', 'followers')))
        # Independent source aliases and reverse edges must count each neighbor once.
        conn.executemany('INSERT INTO edges VALUES(?,?,?,?)',
                         [(seed, pid, 'followers', stamp) for seed in ('alice', 'bob')
                          for pid in (899, 900)])
        conn.execute("INSERT INTO edges VALUES('alice',1201,'following',?)", (stamp,))
        result = compare(conn, 'alice', 'bob', 20)
        self.assertEqual(result['total_candidates'], 900)
        self.assertEqual([c['node']['handle'] for c in result['connectors']],
                         ['user0900', 'user0899'] + [f'user{i:04}' for i in range(101, 119)])
        hub = result['connectors'][1]
        self.assertEqual(hub['rank']['observed_degree'], 302)
        self.assertEqual(len(hub['links']), 4)
        full = compare(conn, 'alice', 'bob', 100)
        self.assertEqual(result['connectors'], full['connectors'][:20])

    def test_history_counts_invalid_rows_and_breaks_valid_ties_by_page(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO seeds(handle) VALUES('alice')")
        conn.execute("INSERT INTO people(id,handle,first_seen,updated_at) VALUES(1,'bob','','')")
        conn.execute("INSERT INTO edges VALUES('alice',1,'following','2026-01-01T00:00:00Z')")
        conn.executemany("INSERT INTO edge_observations VALUES('alice',1,'following',?,?,?)", [
            ('bad', 1, 'invalid'), ('future', 2, '2099-01-01T00:00:00Z'),
            ('z', 3, '2026-02-01T00:00:00Z'), ('a', 4, '2026-02-01T00:00:00Z'),
            ('old', 5, '2026-01-01T00:00:00Z')])
        evidence = compare(conn, 'alice', 'bob')['direct_relationships'][0]['evidence'][0]
        self.assertEqual(evidence['observation_count'], 5)
        self.assertEqual(evidence['last_page_key'], 'z')
        self.assertEqual(evidence['last_job_id'], 3)
        self.assertEqual(evidence['last_observed'], '2026-02-01T00:00:00+00:00')


if __name__ == '__main__':
    unittest.main()
