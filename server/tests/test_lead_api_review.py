"""Lead browsing contracts against disposable databases; no network or models."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs
import db
import server


class LeadApiReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'leads.sqlite'
        self.conn = db.init(self.path)
        server.clear_caches()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        server.clear_caches()

    def person(self, handle, bio=None):
        pid = db.upsert_person(self.conn, {'handle': handle, 'bio': bio}, '2026-01-01')
        return pid

    def test_50000_tied_rows_paginate_without_loss(self):
        self.conn.executemany('INSERT INTO people(handle,first_seen,updated_at) VALUES(?,?,?)',
                              ((f'user{i}', '2026-01-01', '2026-01-01') for i in range(50000)))
        self.conn.commit()
        ids = []
        # Expansion is separately tested; exercise the real SQL ordering and offsets.
        with patch.object(server, 'lead_rows', side_effect=lambda c, rows: [{'id': r['id']} for r in rows]):
            offset = 0
            while True:
                out = server.api_leads(self.conn, {'limit': ['500'], 'offset': [str(offset)]}, {})
                self.assertEqual(out['total'], 50000)
                ids.extend(r['id'] for r in out['rows'])
                offset = out['next_offset']
                if not out['has_more']:
                    break
        self.assertEqual(ids, list(range(1, 50001)))

    def test_page_revision_and_rows_share_snapshot_during_external_write(self):
        self.person('before')
        self.conn.commit()
        before = server.data_rev(self.conn)
        other = db.connect(self.path)
        original = server.lead_rows
        def expand(c, rows):
            db.upsert_person(other, {'handle': 'after'}, '2026-01-01')
            other.commit()
            return original(c, rows)
        try:
            with patch.object(server, 'lead_rows', side_effect=expand):
                out = server.api_leads(self.conn, {}, {})
            self.assertEqual(out['rev'], before)
            self.assertEqual(out['total'], 1)
            self.assertEqual([r['handle'] for r in out['rows']], ['before'])
            self.assertGreater(server.data_rev(self.conn), before)
        finally:
            other.close()

    def test_search_literals_notes_and_filters_match_counts(self):
        match = self.person('match', '100%_real')
        self.person('other', '100xxreal')
        self.conn.execute("INSERT INTO marks VALUES(?,'interested','private note', '2026-01-01')", (match,))
        self.conn.execute("INSERT INTO tags VALUES(?,'Founder','role','manual')", (match,))
        self.conn.commit()
        for query in ('q=100%25_real', 'q=private+note&tags=Founder&status=interested'):
            q = parse_qs(query)
            out = server.api_leads(self.conn, q, {})
            self.assertEqual([r['id'] for r in out['rows']], [match])
            self.assertEqual(server.api_counts(self.conn, q, {})['interested'], 1)
            facet = next(r for r in server.api_tags(self.conn, q, {}) if r['tag'] == 'Founder')
            self.assertEqual(facet['count'], 1)

    def test_active_list_counts_keep_zero_degree_and_fit_is_independent(self):
        strong = self.person('strong')
        good = self.person('good')
        weak = self.person('weak')
        unread = self.person('unread')
        self.conn.executemany('INSERT INTO verdicts(person_id,tier,content_fit) VALUES(?,?,?)',
                              [(strong, 'cold', 70), (good, 'hot', 45), (weak, 'hot', 44)])
        db.add_edge(self.conn, 'source_a', strong, 'followers')
        db.add_edge(self.conn, 'source_b', strong, 'following')
        db.add_edge(self.conn, 'old_history', good, 'followers', observed=False)
        self.conn.commit()
        self.assertEqual(server.api_leads(self.conn, {'min_lists': ['2']}, {})['total'], 1)
        self.assertEqual(server.api_leads(self.conn, {'sort': ['connected']}, {})['rows'][0]['id'], strong)
        self.assertEqual({r['id']: r['lists'] for r in server.api_leads(self.conn, {}, {})['rows']},
                         {strong: 2, good: 0, weak: 0, unread: 0})
        for fit, expected in [('strong', strong), ('good', good), ('weak', weak), ('unread', unread)]:
            self.assertEqual([r['id'] for r in server.api_leads(self.conn, {'fit': [fit]}, {})['rows']], [expected])
        self.assertEqual(server.api_leads(self.conn, {'fit': ['good'], 'tier': ['cold']}, {})['total'], 0)
        with self.assertRaises(server.Bad):
            server.api_leads(self.conn, {'fit': ['excellent']}, {})
        self.conn.execute("UPDATE settings SET value='false' WHERE key='map_person_degree_v1'")
        self.assertEqual({r['id']: r['lists'] for r in server.api_leads(self.conn, {}, {})['rows']},
                         {strong: 2, good: 0, weak: 0, unread: 0})

    def test_same_count_tag_replacement_invalidates_cached_facets(self):
        pid = self.person('one')
        self.conn.execute("INSERT INTO tags VALUES(?,'Old','role','manual')", (pid,))
        self.conn.commit()
        old = server.data_rev(self.conn)
        self.assertEqual(server.api_tags(self.conn, {}, {})[0]['tag'], 'Old')
        self.conn.execute("UPDATE tags SET tag='New' WHERE person_id=?", (pid,))
        self.conn.commit()
        self.assertGreater(server.data_rev(self.conn), old)
        self.assertEqual(server.api_tags(self.conn, {}, {})[0]['tag'], 'New')

    def test_cache_isolates_database_connections(self):
        self.person('one')
        self.conn.commit()
        other = db.init(Path(self.tmp.name) / 'other.sqlite')
        try:
            db.upsert_person(other, {'handle': 'two', 'bio': 'bio'}, '2026-01-01')
            other.commit()
            self.assertEqual(server.data_rev(other), server.data_rev(self.conn))
            self.assertEqual(server.api_counts(self.conn, {}, {})['with_bio'], 0)
            self.assertEqual(server.api_counts(other, {}, {})['with_bio'], 1)
        finally:
            other.close()

    def test_uncommitted_results_do_not_poison_revision_cache(self):
        pid = self.person('one')
        self.conn.commit()
        self.conn.execute("UPDATE people SET bio='unsaved' WHERE id=?", (pid,))
        self.assertEqual(server.api_counts(self.conn, {}, {})['with_bio'], 1)
        self.conn.rollback()
        self.conn.execute("UPDATE people SET name='saved' WHERE id=?", (pid,))
        self.conn.commit()
        self.assertEqual(server.api_counts(self.conn, {}, {})['with_bio'], 0)

    def test_oversized_numbers_are_client_errors(self):
        for key in ('offset', 'limit', 'followers_min', 'min_lists'):
            with self.subTest(key=key), self.assertRaises(server.Bad):
                server.api_leads(self.conn, {key: ['9' * 100]}, {})

    def test_seed_peer_invalidation_refreshes_revision_without_duplicates(self):
        first = self.person('first')
        second = self.person('second')
        db.add_edge(self.conn, 'source_a', first, 'followers', flipped=set())
        db.add_edge(self.conn, 'source_a', second, 'following', flipped=set())
        self.conn.execute('DELETE FROM network_dirty')
        db.dirty_seed_members(self.conn, 'source_a', 'source_a')
        initial = dict(self.conn.execute('SELECT person_id,change_id FROM network_dirty'))
        self.assertEqual(set(initial), {first, second})
        db.dirty_seed_members(self.conn, 'source_a')
        updated = dict(self.conn.execute('SELECT person_id,change_id FROM network_dirty'))
        self.assertEqual(set(updated), {first, second})
        self.assertTrue(all(updated[pid] > initial[pid] for pid in updated))

if __name__ == '__main__':
    unittest.main()
