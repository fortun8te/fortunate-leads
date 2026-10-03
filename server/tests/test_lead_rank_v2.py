"""Prepared filters and connected ordering must match every ordinary SQL page."""

import os
os.environ.setdefault('FL_NO_ORSLOT', '1')
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
import lead_rank
import server
from test_lead_thin_page import ThinLeadPage


class PreparedLeadQueries(ThinLeadPage):
    def setUp(self):
        super().setUp()
        fits = (None, -1, 0, 44.999, 45, 45.001, 69.999, 70, 70.001, 100)
        for index, row in enumerate(self.conn.execute('SELECT id FROM people').fetchall()):
            pid = row[0]
            self.conn.execute('INSERT INTO verdicts(person_id,tier,score,content_fit) VALUES(?,?,?,?) '
                              'ON CONFLICT(person_id) DO UPDATE SET tier=excluded.tier,score=excluded.score,content_fit=excluded.content_fit',
                              (pid, 'unread' if index % 5 == 0 else None if index % 7 == 0 else 'hot',
                               None if index % 4 == 0 else index % 3, fits[index % len(fits)]))
            for number in range(index % 4):
                db.add_edge(self.conn, f'additional{number}', pid, 'following')
        self.conn.commit()

    def check_all_pages(self, filters):
        for sort in lead_rank.ORDERS:
            query = dict(filters, sort=[sort], limit=['3'], offset=['0'])
            total, _ = self.oracle(query)
            offsets = sorted(set([1, 2, total + 10, *range(0, total + 3, 3)]))
            visited = []
            for offset in offsets:
                query['offset'] = [str(offset)]
                with self.subTest(sort=sort, filters=filters, offset=offset):
                    expected_total, expected_rows = self.oracle(query)
                    indexed = lead_rank.page_for_query(self.conn, sort, 3, offset, query)
                    self.assertIsNotNone(indexed)
                    self.assertEqual(indexed, (expected_total, [row['id'] for row in expected_rows]))
                    page = server.api_leads(self.conn, query, {})
                    self.assertEqual(page['total'], expected_total)
                    self.assertEqual(page['rows'], expected_rows)
                    self.assertEqual(page['next_offset'], offset + len(expected_rows))
                    self.assertEqual(page['has_more'], offset + len(expected_rows) < expected_total)
                    if offset % 3 == 0 and offset <= total:
                        visited.extend(row['id'] for row in page['rows'])
            self.assertEqual(len(visited), total)
            self.assertEqual(len(set(visited)), total)

    def test_every_fit_boundary_choice_and_degree_filter_on_every_page(self):
        for filters in ({}, {'min_lists': ['0']}, {'min_lists': ['1']}, {'min_lists': ['2']},
                        {'min_lists': ['9']}, {'fit': ['strong']}, {'fit': ['good']},
                        {'fit': ['weak']}, {'fit': ['unread']}, {'fit': ['strong,good']},
                        {'fit': ['unread,weak']}, {'fit': [' strong, good,strong ']},
                        {'fit': ['strong,good,weak,unread']},
                        {'fit': ['good'], 'min_lists': ['1']},
                        {'fit': ['strong,unread'], 'min_lists': ['2']}):
            self.check_all_pages(filters)

    def test_owner_absent_present_dynamic_hidden_and_null_semantics(self):
        self.check_all_pages({'min_lists': ['1']})
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('PERSON4',1)")
        self.conn.execute("UPDATE people SET handle='Fortun8Te' WHERE id=6")
        self.check_all_pages({'fit': ['strong,good,weak,unread']})
        self.conn.execute("UPDATE seeds SET is_me=0 WHERE handle='person4'")
        self.conn.execute("UPDATE people SET handle='person5' WHERE id=6")
        self.check_all_pages({})
        self.conn.execute('INSERT INTO seeds(handle,is_me) VALUES(NULL,1)')
        self.check_all_pages({})
        self.check_all_pages({'fit': ['good'], 'min_lists': ['1']})

    def test_degree_zero_missing_and_negative_legacy_values_remain_exact(self):
        # The reduced legacy fixture deliberately includes values that today's
        # constraints forbid. Missing degree rows represent null joined values.
        self.conn.execute('PRAGMA ignore_check_constraints=ON')
        self.conn.execute('INSERT OR REPLACE INTO map_person_degree(person_id,degree) VALUES(1,-2)')
        self.conn.execute('INSERT OR REPLACE INTO map_person_degree(person_id,degree) VALUES(2,0)')
        self.conn.execute('DELETE FROM map_person_degree WHERE person_id=3')
        self.check_all_pages({})
        self.check_all_pages({'min_lists': ['1']})

    def test_unimplemented_queries_and_invalid_filters_use_exact_fallback(self):
        for filters in ({'status': ['all']}, {'status': ['interested']}, {'tags': ['Founder']},
                        {'tier': ['hot']}, {'q': ['Saved note']}, {'seed': ['source']},
                        {'relationship': ['follows']}, {'followers_min': ['1']},
                        {'has_bio': ['0']}, {'q': ['']}, {'unknown': ['1']}):
            query = dict(filters, sort=['score'], limit=['4'], offset=['0'])
            self.assertIsNone(lead_rank.page_for_query(self.conn, 'score', 4, 0, query))
            total, rows = self.oracle(query)
            page = server.api_leads(self.conn, query, {})
            self.assertEqual((page['total'], page['rows']), (total, rows))
        for filters in ({'fit': ['invalid']}, {'min_lists': ['-1']}, {'min_lists': ['1.2']},
                        {'min_lists': [str(2 ** 63)]}, {'sort': ['invalid']}, {'limit': ['x']}, {'offset': ['x']}):
            query = dict(sort=['score'], limit=['4'], offset=['0'])
            query.update(filters)
            with patch.object(lead_rank, 'page_for_query', side_effect=AssertionError('validation was bypassed')):
                with self.assertRaises(server.Bad):
                    server.api_leads(self.conn, query, {})

    def test_populated_connected_index_upgrade_waits_for_explicit_preparation(self):
        self.conn.execute('DROP INDEX lead_rank_connected')
        statements = []
        self.conn.set_trace_callback(statements.append)
        try:
            lead_rank.ensure(self.conn)
        finally:
            self.conn.set_trace_callback(None)
        self.assertTrue(lead_rank.ready(self.conn, 'score'))
        self.assertTrue(lead_rank.ready(self.conn, 'fit'))
        self.assertFalse(lead_rank.ready(self.conn, 'connected'))
        self.assertFalse(any('CREATE INDEX lead_rank_connected' in statement for statement in statements))
        self.assertFalse(any(statement.lstrip().startswith('INSERT INTO lead_rank SELECT') for statement in statements))
        query = {'sort': ['connected'], 'limit': ['3'], 'offset': ['3'], 'min_lists': ['1']}
        self.assertIsNone(lead_rank.page_for_query(self.conn, 'connected', 3, 3, query))
        total, rows = self.oracle(query)
        page = server.api_leads(self.conn, query, {})
        self.assertEqual((page['total'], page['rows']), (total, rows))
        lead_rank.prepare(self.conn)
        self.assertTrue(lead_rank.ready(self.conn, 'connected'))
        self.check_all_pages({'min_lists': ['1']})

    def test_degree_or_ranking_readiness_failure_uses_exact_fallback(self):
        self.conn.execute("UPDATE settings SET value='false' WHERE key='map_person_degree_v1'")
        query = {'sort': ['connected'], 'limit': ['3'], 'offset': ['0'], 'fit': ['strong']}
        self.assertIsNone(lead_rank.page_for_query(self.conn, 'connected', 3, 0, query))
        total, rows = self.oracle(query)
        page = server.api_leads(self.conn, query, {})
        self.assertEqual((page['total'], page['rows']), (total, rows))

    def test_connected_index_order_does_not_sort_in_temporary_storage(self):
        plan = ' '.join(str(row[3]) for row in self.conn.execute(
            f'EXPLAIN QUERY PLAN SELECT person_id FROM lead_rank WHERE hidden=0 '
            f"ORDER BY {lead_rank.ORDERS['connected']} LIMIT 50"))
        self.assertIn('lead_rank_connected', plan)
        self.assertNotIn('TEMP B-TREE', plan)


if __name__ == '__main__':
    unittest.main()
