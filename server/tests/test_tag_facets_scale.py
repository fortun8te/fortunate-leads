"""Default tag counts stay exact without first enumerating every profile."""
import unittest

import server
import tag_projection
import test_lead_thin_page


def legacy_facets(conn, query):
    where, args = server.lead_filter(query)
    counts, totals = {}, {}
    for branch, projected in enumerate(tag_projection.parts()):
        selected = f"SELECT p.id {server.PEOPLE_FROM} WHERE {' AND '.join([server.NOT_ME] + where)}"
        counted = (f'WITH f AS MATERIALIZED ({selected}) SELECT t.tag,t.source,count(*) '
                   f'FROM f JOIN ({projected}) t ON t.person_id=f.id GROUP BY t.tag,t.source' if branch == 0 else
                   f'SELECT t.tag,t.source,count(*) FROM ({projected}) t '
                   f'WHERE t.person_id IN ({selected}) GROUP BY t.tag,t.source')
        for tag, source, n in conn.execute(counted, args):
            counts[tag, source] = counts.get((tag, source), 0) + n
        for tag, source, grp, n in conn.execute(
                f'SELECT t.tag,t.source,min(t.grp),count(*) FROM ({projected}) t GROUP BY t.tag,t.source'):
            previous = totals.get((tag, source), (grp, 0))
            totals[tag, source] = (grp, previous[1] + n)
    out = [{'tag': tag, 'grp': grp, 'source': source, 'count': counts.get((tag, source), 0), 'total': total}
           for (tag, source), (grp, total) in totals.items()]
    return sorted(out, key=lambda row: (-row['count'], -row['total'], row['tag'], row['source']))


class TagFacetsScale(unittest.TestCase):
    setUp = test_lead_thin_page.ThinLeadPage.setUp
    tearDown = test_lead_thin_page.ThinLeadPage.tearDown

    def check(self, query):
        self.assertEqual(server.tag_facets(self.conn, query), legacy_facets(self.conn, query))

    def test_all_branches_and_filters_match_original_counts(self):
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('person1',1)")
        self.conn.execute("UPDATE people SET handle='FORTUN8TE' WHERE id=4")
        self.conn.execute("INSERT INTO owner_context VALUES(5,'[\"friend\",\"client\"]',NULL,'now')")
        self.conn.execute("INSERT INTO tags VALUES(5,'Client','relationship','manual')")
        self.conn.execute("INSERT INTO tags VALUES(3,'Fit: strong','signal','auto')")
        self.conn.execute("INSERT INTO tags VALUES(999,'Orphan','signal','manual')")
        for query in ({}, {'status': ['all']}, {'tags': ['Founder']}, {'min_lists': ['1']},
                      {'fit': ['unread']}, {'q': ['Saved note']}, {'tier': ['hot']}):
            with self.subTest(query=query):
                self.check(query)
        self.conn.execute("UPDATE seeds SET is_me=0 WHERE handle='person1'")
        self.conn.execute("UPDATE marks SET status='interested' WHERE status='no'")
        self.conn.execute("UPDATE owner_context SET relationships='[\"friend\"]' WHERE person_id=5")
        self.conn.execute('DELETE FROM verdicts WHERE person_id=3')
        self.check({})

    def test_unrelated_people_do_not_change_tag_facets(self):
        expected = server.tag_facets(self.conn, {})
        self.conn.executemany('INSERT INTO people(handle,first_seen,updated_at) VALUES(?,?,?)',
                              [(f'untagged{i}', 'now', 'now') for i in range(1000)])
        self.assertEqual(server.tag_facets(self.conn, {}), expected)


if __name__ == '__main__':
    unittest.main()
