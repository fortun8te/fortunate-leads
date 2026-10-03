"""Degree deltas equal an independent full graph query through mutations."""
import random
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db


class IncrementalDegreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'graph.sqlite')
        self.conn = db.init(self.path)
        self.addCleanup(lambda: self.conn.close())
        self.at = '2026-01-01T00:00:00+00:00'
        self.conn.executemany('INSERT INTO people(id,handle,first_seen,updated_at) VALUES(?,?,?,?)',[(i,h,self.at,self.at) for i,h in [(1,'one'),(2,'two'),(3,'three')]])
        self.conn.commit()

    def oracle(self):
        expected = dict(self.conn.execute('SELECT e.person_id,count(DISTINCT e.seed) FROM current_edges e JOIN people p ON p.id=e.person_id GROUP BY e.person_id'))
        actual = dict(self.conn.execute('SELECT person_id,degree FROM map_person_degree'))
        self.assertEqual(actual,expected)
        self.assertEqual(set(map(tuple,self.conn.execute('SELECT person_id,seed FROM map_seed_member'))),
                         set(map(tuple,self.conn.execute('SELECT DISTINCT person_id,seed FROM current_edges'))))
        self.assertEqual(dict(self.conn.execute('SELECT seed,degree FROM map_seed_degree')),
                         dict(self.conn.execute('SELECT seed,count(*) FROM map_seed_member GROUP BY seed')))
        return actual

    def edge(self, seed, person, direction='following', active=1):
        self.conn.execute('INSERT OR IGNORE INTO edges VALUES(?,?,?,?)',(seed,person,direction,self.at))
        self.conn.execute('INSERT INTO edge_evidence VALUES(?,?,?,?,?,?) ON CONFLICT(seed,person_id,direction) DO UPDATE SET active=excluded.active',
                          (seed,person,direction,active,self.at,self.at))

    def test_same_source_two_orientations_retirement_reactivation_and_zero(self):
        self.edge('source',1)
        self.edge('source',1,'followers')
        self.assertEqual(self.oracle(),{1:1})
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE direction='following'")
        self.assertEqual(self.oracle(),{1:1})
        self.conn.execute('UPDATE edge_evidence SET active=0')
        self.assertEqual(self.oracle(),{})
        self.conn.execute('UPDATE edge_evidence SET active=1')
        self.assertEqual(self.oracle(),{1:1})
        self.conn.execute('DELETE FROM edges')
        self.assertEqual(self.oracle(),{})

    def test_missing_identity_then_insert_and_delete_person(self):
        self.edge('source',99)
        self.edge('another',99)
        self.assertEqual(self.oracle(),{})
        self.conn.execute("INSERT INTO people(id,handle,first_seen,updated_at) VALUES(99,'later',?,?)",(self.at,self.at))
        self.assertEqual(self.oracle(),{99:2})
        self.conn.execute('DELETE FROM people WHERE id=99')
        self.assertEqual(self.oracle(),{})
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE person_id=99")
        self.oracle()

    def test_merge_rename_duplicate_and_transaction_rollback(self):
        self.edge('source',1)
        self.edge('source',2,'followers')
        self.edge('another',2)
        db.merge_people(self.conn,keep=1,drop=2)
        self.assertEqual(self.oracle(),{1:2})
        db.move_seed(self.conn,'another','renamed')
        self.assertEqual(self.oracle(),{1:2})
        self.edge('source',1)
        self.assertEqual(self.oracle(),{1:2})
        self.conn.commit()
        self.conn.execute('DELETE FROM edge_evidence')
        self.assertEqual(self.oracle(),{})
        self.conn.rollback()
        self.assertEqual(self.oracle(),{1:2})

    def test_score_hidden_and_layout_stay_correct(self):
        self.conn.execute('INSERT INTO verdicts(person_id,score) VALUES(1,87)')
        self.conn.execute("INSERT INTO marks(person_id,status) VALUES(1,'no')")
        self.edge('source',1)
        self.edge('another',1)
        row=self.conn.execute('SELECT degree,score,hidden FROM map_person_degree WHERE person_id=1').fetchone()
        self.assertEqual(tuple(row),(2,87,1))
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE seed='source'")
        row=self.conn.execute('SELECT degree,score,hidden FROM map_person_degree WHERE person_id=1').fetchone()
        self.assertEqual(tuple(row),(1,87,1))
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM map_layout_dirty WHERE person_id=1').fetchone())
        self.oracle()

    def test_migration_repairs_missing_membership_and_stale_degree_once(self):
        self.edge('source',1)
        self.edge('another',1)
        self.conn.execute('DELETE FROM map_seed_member')
        self.conn.execute("UPDATE settings SET value='false' WHERE key='map_seed_member_v1'")
        self.conn.execute("DELETE FROM settings WHERE key='map_person_degree_v2'")
        self.conn.execute('INSERT OR REPLACE INTO map_person_degree VALUES(1,99,NULL,0)')
        # Mimic obsolete trigger presence, without letting it touch data.
        self.conn.execute('CREATE TRIGGER map_degree_edges_insert AFTER INSERT ON edges BEGIN SELECT 1; END')
        self.conn.commit()
        self.conn.close()
        self.conn=db.init(self.path)
        self.assertEqual(self.oracle(),{1:2})
        self.assertTrue(db.get_setting(self.conn,'map_person_degree_v2'))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='map_degree_edges_insert'").fetchone())
        # A second initialization must preserve exact counts.
        self.conn.commit()
        self.conn.close()
        self.conn=db.init(self.path)
        self.assertEqual(self.oracle(),{1:2})

    def test_missing_membership_trigger_repairs_stale_summary_in_one_transaction(self):
        self.edge('source',1)
        self.conn.execute('DROP TRIGGER map_member_edge_evidence_update')
        self.conn.execute('UPDATE edge_evidence SET active=0')
        self.conn.commit()
        self.assertEqual(dict(self.conn.execute('SELECT person_id,degree FROM map_person_degree')),{1:1})
        db.init_map_person_degree(self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.oracle(),{})
        with sqlite3.connect(self.path) as reader:
            self.assertEqual(dict(reader.execute('SELECT person_id,degree FROM map_person_degree')),{1:1})
        self.conn.commit()
        with sqlite3.connect(self.path) as reader:
            self.assertEqual(dict(reader.execute('SELECT person_id,degree FROM map_person_degree')),{})

    def test_failed_standalone_trigger_replacement_can_roll_back_every_change(self):
        self.edge('source',1)
        self.conn.commit()
        before=list(map(tuple,self.conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")))
        self.conn.set_authorizer(lambda action,a,b,c,d: sqlite3.SQLITE_DENY
            if action==sqlite3.SQLITE_CREATE_TRIGGER and a=='map_degree_member_delete' else sqlite3.SQLITE_OK)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                db.init_map_person_degree(self.conn)
            self.assertTrue(self.conn.in_transaction)
        finally:
            self.conn.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
            self.conn.rollback()
        self.assertEqual(list(map(tuple,self.conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"))),before)
        self.assertEqual(self.oracle(),{1:1})
        self.edge('another',1)
        self.assertEqual(self.oracle(),{1:2})

    def test_sibling_trigger_creation_order_does_not_change_results(self):
        triggers=list(self.conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND (name LIKE 'map_member_%' OR name LIKE 'map_degree_%' OR name LIKE 'map_seed_degree_%') ORDER BY rowid DESC"))
        for name,sql in triggers:
            self.conn.execute('DROP TRIGGER '+name)
        for name,sql in triggers:
            self.conn.execute(sql)
        rng=random.Random(11)
        for _ in range(120):
            seed,person,direction='s'+str(rng.randrange(5)),rng.randrange(1,4),rng.choice(['followers','following'])
            self.edge(seed,person,direction,rng.randrange(2))
            self.oracle()
            if rng.randrange(4)==0:
                self.conn.execute('DELETE FROM edges WHERE seed=? AND person_id=? AND direction=?',(seed,person,direction))
                self.oracle()


if __name__ == '__main__':
    unittest.main()
