import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db


class ObservationStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'test.sqlite')
        self.conn = db.init(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_migration_never_backfills_todays_source_id_or_recency(self):
        pid = db.upsert_person(self.conn, {'handle': 'target', 'ig_id': '8'})
        self.conn.execute("INSERT INTO seeds(handle,ig_id) VALUES('reused','999')")
        self.conn.execute("INSERT INTO edges VALUES('reused',?,'following','2020-01-01')", (pid,))
        self.conn.execute("DELETE FROM settings WHERE key='edge_observations_v1'")
        self.conn.commit()
        other = db.init(self.path)
        self.assertEqual(tuple(other.execute('SELECT seed_ig_id,first_seen,last_seen FROM edge_observations').fetchone()),
                         ('', '2020-01-01', None))
        other.close()
        other = db.init(self.path)
        self.assertEqual(other.execute('SELECT count(*) FROM edge_observations').fetchone()[0], 1)
        other.close()

    def test_repeated_observations_and_reused_handle_keep_separate_identities(self):
        pid = db.upsert_person(self.conn, {'handle': 'target', 'ig_id': '8'})
        self.assertTrue(db.add_edge(self.conn, 'a', pid, 'following', '2020-01-01', seed_ig_id='1'))
        self.assertFalse(db.add_edge(self.conn, 'a', pid, 'following', '2021-01-01', seed_ig_id='1'))
        db.add_edge(self.conn, 'a', pid, 'following', '2022-01-01', seed_ig_id='2')
        db.add_edge(self.conn, 'a', pid, 'following', '2023-01-01', seed_ig_id='2', observe=False)
        self.assertEqual([tuple(r) for r in self.conn.execute('SELECT seed_ig_id,first_seen,last_seen FROM edge_observations ORDER BY seed_ig_id')],
                         [('1','2020-01-01','2021-01-01'), ('2','2022-01-01','2022-01-01')])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges').fetchone()[0], 1)

    def test_person_merge_preserves_all_source_evidence_and_dates(self):
        keep = db.upsert_person(self.conn, {'handle': 'old', 'ig_id': '8'})
        drop = db.upsert_person(self.conn, {'handle': 'new'})
        db.add_edge(self.conn, 'a', keep, 'following', '2021-01-01', seed_ig_id='1')
        db.add_edge(self.conn, 'a', drop, 'following', '2020-01-01', seed_ig_id='1')
        db.add_edge(self.conn, 'a', drop, 'following', '2022-01-01', seed_ig_id='1')
        db.add_edge(self.conn, 'a', drop, 'following', '2022-01-01', seed_ig_id='2')
        self.assertEqual(db.upsert_person(self.conn, {'handle': 'new', 'ig_id': '8'}), keep)
        rows = [tuple(r) for r in self.conn.execute('SELECT person_id,seed_ig_id,first_seen,last_seen FROM edge_observations ORDER BY seed_ig_id')]
        self.assertEqual(rows, [(keep,'1','2020-01-01','2022-01-01'), (keep,'2','2022-01-01','2022-01-01')])
        self.assertFalse(self.conn.execute('SELECT 1 FROM people WHERE id=?', (drop,)).fetchone())


if __name__ == '__main__':
    unittest.main()
