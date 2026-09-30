"""Lead pages expose only observed directions, using one page-batched read."""
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import server

class LeadConnectionRows(unittest.TestCase):
    def test_directional_page_evidence_excludes_absent_and_historical_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = db.init(Path(directory) / 'leads.sqlite')
            try:
                pid = db.upsert_person(conn, {'handle':'lead'}, '2026-09-30')
                for seed, direction in [('alice','followers'), ('alice','following'), ('bob','following'), ('old','followers')]:
                    db.add_edge(conn, seed, pid, direction)
                conn.execute("DELETE FROM edge_evidence WHERE seed='old'")
                conn.execute("UPDATE edge_evidence SET active=0 WHERE seed='bob'")
                conn.commit()
                queries = []
                conn.set_trace_callback(queries.append)
                page = server.api_leads(conn, {'status':['all'], 'limit':['10']}, {})
                conn.set_trace_callback(None)
                row = next(row for row in page['rows'] if row['id'] == pid)
                self.assertEqual(row['via'], ['alice'])
                self.assertEqual([(edge['seed'],edge['direction']) for edge in row['connection_edges']], [('alice','followers'),('alice','following')])
                self.assertTrue(all(edge['observed_at'] for edge in row['connection_edges']))
                self.assertEqual(sum(query.startswith('SELECT person_id, seed, direction, observed_at FROM current_edges WHERE person_id IN') for query in queries), 1)
            finally:
                conn.close()

if __name__ == '__main__':
    unittest.main()
