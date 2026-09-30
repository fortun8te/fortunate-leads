"""Thin page selection must match the original full-profile query exactly."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
import server


class ThinLeadPage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.temp.name) / 'leads.sqlite')
        for index in range(18):
            pid = db.upsert_person(self.conn, {'handle': f'person{index}', 'bio': 'A long bio ' * 200,
                                              'followers': None if index % 3 == 0 else index % 5}, '2026-01-01')
            if index % 4:
                self.conn.execute('INSERT INTO verdicts(person_id,tier,score,content_fit) VALUES(?,?,?,?)',
                                  (pid, 'unread' if index % 7 == 0 else 'hot', None if index % 5 == 0 else index % 3,
                                   None if index % 6 == 0 else (index % 3) * 35))
            if index % 3 == 0:
                self.conn.execute('INSERT INTO marks VALUES(?,?,?,?)',
                                  (pid, 'no' if index % 6 == 0 else 'interested', 'Saved note', '2026-01-01'))
            if index % 2:
                db.add_edge(self.conn, 'source', pid, 'following')
                self.conn.execute('INSERT INTO tags VALUES(?,?,?,?)', (pid, 'Founder', 'role', 'manual'))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def oracle(self, query):
        where, args = server.lead_filter(query)
        clause = ' WHERE ' + ' AND '.join([server.NOT_ME] + where)
        total = self.conn.execute(f'SELECT count(*) {server.PEOPLE_FROM}{clause}', args).fetchone()[0]
        rows = self.conn.execute(f'{server.LEAD_SQL}{clause} ORDER BY {server.SORTS[query["sort"][0]]}, p.id LIMIT ? OFFSET ?',
                                 args + [int(query['limit'][0]), int(query['offset'][0])]).fetchall()
        return total, server.lead_rows(self.conn, rows)

    def test_every_sort_filter_and_page_preserves_full_response(self):
        for sort in server.SORTS:
            for filters in ({}, {'status': ['all']}, {'tags': ['Founder']}, {'min_lists': ['1']},
                            {'fit': ['unread']}, {'q': ['Saved note']}):
                for offset in (0, 3, 50):
                    query = dict(filters, sort=[sort], limit=['4'], offset=[str(offset)])
                    with self.subTest(sort=sort, filters=filters, offset=offset):
                        total, rows = self.oracle(query)
                        page = server.api_leads(self.conn, query, {})
                        self.assertEqual(page['total'], total)
                        self.assertEqual(page['rows'], rows)
                        self.assertEqual(page['next_offset'], offset + len(rows))
                        self.assertEqual(page['has_more'], offset + len(rows) < total)

    def test_interrupted_summary_backfill_keeps_exact_fallback(self):
        self.conn.execute("UPDATE settings SET value='false' WHERE key='map_person_degree_v1'")
        query = {'sort': ['fit'], 'limit': ['5'], 'offset': ['3']}
        total, rows = self.oracle(query)
        page = server.api_leads(self.conn, query, {})
        self.assertEqual(page['total'], total)
        self.assertEqual(page['rows'], rows)


if __name__ == '__main__':
    unittest.main()
