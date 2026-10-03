"""Prepared filtered facets match the original source relations, including edits."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import tag_facets
import test_lead_thin_page
from test_tag_facets_scale import legacy_facets
import server


class FilteredFacetsTests(unittest.TestCase):
    setUp = test_lead_thin_page.ThinLeadPage.setUp
    tearDown = test_lead_thin_page.ThinLeadPage.tearDown

    def compare(self):
        for query in ({'tags': ['Founder']}, {'status': ['all']}, {'min_lists': ['1']},
                      {'fit': ['unread']}, {'fit': ['strong,good']}, {'q': ['Saved note']},
                      {'tier': ['hot']}, {'tags': ['Missing']}, {}):
            with self.subTest(query=query):
                where, args = server.lead_filter(query)
                selected = f"SELECT p.id {server.PEOPLE_FROM} WHERE {' AND '.join([server.NOT_ME] + where)}"
                self.assertEqual(tag_facets.filtered(self.conn, selected, args), legacy_facets(self.conn, query))

    def test_projected_overlap_groups_owners_orphans_and_edits(self):
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('person1',1)")
        self.conn.execute("UPDATE people SET handle='FORTUN8TE' WHERE id=4")
        self.conn.execute("INSERT INTO owner_context VALUES(5,'[\"friend\",\"client\"]',NULL,'now')")
        self.conn.execute("INSERT INTO tags VALUES(5,'Client','relationship','manual')")
        self.conn.execute("INSERT INTO tags VALUES(3,'Fit: strong','signal','auto')")
        self.conn.execute("INSERT INTO tags VALUES(999,'Orphan',NULL,'manual')")
        self.conn.execute("INSERT INTO tags VALUES(7,'Ungrouped',NULL,'manual')")
        self.conn.commit()
        self.assertTrue(tag_facets.ready(self.conn))
        self.compare()
        self.conn.execute("UPDATE seeds SET is_me=0 WHERE handle='person1'")
        self.conn.execute("UPDATE marks SET status='interested' WHERE status='no'")
        self.conn.execute("UPDATE owner_context SET relationships='[\"friend\"]' WHERE person_id=5")
        self.conn.execute('DELETE FROM verdicts WHERE person_id=3')
        self.compare()
        self.conn.execute('INSERT INTO seeds(handle,is_me) VALUES(NULL,1)')
        self.compare()

    def test_unprepared_copy_uses_original_fallback(self):
        db.set_setting(self.conn, 'tag_facets_ready', False)
        self.assertIsNone(tag_facets.filtered(self.conn, 'SELECT id FROM people'))
        query = {'tags': ['Founder']}
        self.assertEqual(server.tag_facets(self.conn, query), legacy_facets(self.conn, query))

    def test_selection_runs_once_across_projection_branches(self):
        calls = []
        self.conn.create_function('visit', 1, lambda pid: calls.append(pid) or pid)
        tag_facets.filtered(self.conn, 'SELECT visit(id) AS id FROM people')
        self.assertEqual(len(calls), self.conn.execute('SELECT count(*) FROM people').fetchone()[0])
        self.assertEqual(len(set(calls)), len(calls))


if __name__ == '__main__':
    unittest.main()
