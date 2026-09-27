"""Legacy Client labels become one editable, filterable relationship."""
import os
os.environ.setdefault('FL_NO_ORSLOT', '1')
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import server


class OwnerRelationshipMigration(unittest.TestCase):
    def test_promotion_preserves_note_and_explicit_decision_and_clear_stays_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.sqlite'
            conn = db.init(path)
            old = db.upsert_person(conn, {'handle': 'legacy_client'})
            explicit = db.upsert_person(conn, {'handle': 'explicit_no'})
            conn.executemany("INSERT INTO tags VALUES(?,'Client','signal','manual')", [(old,), (explicit,)])
            conn.executemany('INSERT INTO marks VALUES(?,?,?,?)', [
                (old, None, 'Our original note', db.now()),
                (explicit, 'no', 'Owner decision', db.now())])
            conn.execute("DELETE FROM settings WHERE key='owner_client_relationship_v1'")
            conn.commit()
            conn.close()
            conn = db.init(path)
            self.assertEqual(conn.execute('SELECT status,note FROM marks WHERE person_id=?', (old,)).fetchone()[:],
                             ('client', 'Our original note'))
            self.assertEqual(conn.execute('SELECT status FROM marks WHERE person_id=?', (explicit,)).fetchone()[0], 'no')
            person = server.api_person(conn, {}, {}, old)
            self.assertEqual(person['status'], 'client')
            self.assertEqual(person['owner_status'], 'client')
            server.api_mark(conn, {}, {'status': None, 'relationships': []}, old)
            self.assertIsNone(server.api_person(conn, {}, {}, old)['owner_status'])
            self.assertEqual(conn.execute('SELECT note FROM marks WHERE person_id=?', (old,)).fetchone()[0], 'Our original note')
            self.assertEqual(conn.execute("SELECT count(*) FROM activity WHERE person_id=? AND kind='tag'", (old,)).fetchone()[0], 1)
            conn.close()
            conn = db.init(path)
            self.assertIsNone(server.api_person(conn, {}, {}, old)['owner_status'])
            conn.close()
