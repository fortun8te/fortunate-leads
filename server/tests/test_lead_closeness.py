"""The Closest and Newest lead orders: exact tiers, stable pages, and the fast path equals the plain query."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
import server

OWNER = 'fortun8te'


class LeadOrders(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.temp.name) / 'leads.sqlite')
        self.ids = {}
        # (handle, first_seen, followers, owner directions, other sources)
        people = [
            ('plain_big', '2026-01-01T10:00:00+00:00', 90000, (), ('a',)),
            ('plain_small', '2026-01-02T10:00:00+00:00', 10, (), ('a', 'b', 'c')),
            ('plain_none', '2026-01-03T10:00:00+00:00', None, (), ()),
            ('i_follow_big', '2026-01-04T10:00:00+00:00', 5000, ('following',), ('a',)),
            ('i_follow_small', '2026-01-04T10:00:00+00:00', 20, ('following',), ()),
            ('follows_me', '2026-01-05T10:00:00+00:00', 30, ('followers',), ()),
            ('mutual_small', '2026-01-06T10:00:00+00:00', 40, ('followers', 'following'), ()),
            ('mutual_big', '2026-01-06T10:00:00+00:00', 400, ('followers', 'following'), ('a', 'b')),
            ('friend', '2026-01-07T10:00:00+00:00', 50, (), ()),
            ('hidden_mutual', '2026-01-08T10:00:00+00:00', 60, ('followers', 'following'), ()),
        ]
        for handle, first_seen, followers, directions, sources in people:
            pid = db.upsert_person(self.conn, {'handle': handle, 'followers': followers}, first_seen)
            self.ids[handle] = pid
            for direction in directions:
                db.add_edge(self.conn, OWNER, pid, direction)
            for source in sources:
                db.add_edge(self.conn, source, pid, 'following')
        self.conn.execute('INSERT INTO owner_context(person_id,relationships,familiarity,updated_at) VALUES(?,?,?,?)',
                          (self.ids['friend'], '["friend"]', None, '2026-01-09'))
        self.conn.execute("INSERT INTO marks VALUES(?,?,?,?)", (self.ids['hidden_mutual'], 'no', None, '2026-01-09'))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def handles(self, **query):
        query = {k: [str(v)] for k, v in query.items()}
        page = server.api_leads(self.conn, query, {})
        return [row['handle'] for row in page['rows']], page

    def oracle(self, query):
        """The same order as one plain query, with no fast path."""
        where, args = server.lead_filter(query)
        clause = ' WHERE ' + ' AND '.join([server.NOT_ME] + where)
        rows = self.conn.execute(f'SELECT p.handle, {server.LISTS} AS lists {server.PEOPLE_FROM}{clause} ORDER BY {server.SORTS[query["sort"][0]]}, p.id', args).fetchall()
        return [row[0] for row in rows]

    def test_closeness_tiers_then_shared_lists_then_followers(self):
        handles, page = self.handles(sort='closeness', limit=50)
        self.assertEqual(handles, [
            'mutual_big', 'mutual_small',          # follow each other: more shared lists, then followers
            'follows_me',                           # follows the owner
            'i_follow_big', 'i_follow_small',       # the owner follows them
            'friend',                               # a recorded relationship, no follow
            'plain_small', 'plain_big', 'plain_none'])  # nobody direct: shared lists, then followers
        self.assertEqual(page['total'], len(handles))
        self.assertNotIn('hidden_mutual', handles, 'people marked not a fit stay out of the default list')

    def test_closeness_fast_path_matches_the_plain_query_for_every_page_and_filter(self):
        for filters in ({}, {'status': ['all']}, {'min_lists': ['1']}, {'q': ['_small']}, {'relationship': ['mutual']}, {'has_bio': ['0']}):
            query = dict(filters, sort=['closeness'])
            expected = self.oracle(query)
            for size in (1, 2, 3, 4, 50):
                got = []
                offset = 0
                while True:
                    page = server.api_leads(self.conn, dict(query, limit=[str(size)], offset=[str(offset)]), {})
                    if not page['rows']:
                        break
                    got += [row['handle'] for row in page['rows']]
                    offset = page['next_offset']
                with self.subTest(filters=filters, page_size=size):
                    self.assertEqual(got, expected)

    def test_closeness_pages_never_repeat_or_skip_a_person(self):
        seen = []
        for offset in range(0, 12, 2):
            handles, _ = self.handles(sort='closeness', status='all', limit=2, offset=offset)
            seen += handles
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(seen, self.oracle({'sort': ['closeness'], 'status': ['all']}))

    def test_closeness_ignores_old_follow_evidence(self):
        pid = self.ids['follows_me']
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE person_id=?", (pid,))
        self.conn.commit()
        handles, _ = self.handles(sort='closeness', limit=50)
        self.assertGreater(handles.index('follows_me'), handles.index('i_follow_small'),
                           'an edge that is no longer current does not count as a link')

    def test_newest_orders_by_when_the_person_was_first_found_and_is_stable(self):
        # Two pairs share a timestamp: the newer id comes first.
        newer = lambda *pair: sorted(pair, key=lambda h: -self.ids[h])
        expected = ['friend', *newer('mutual_big', 'mutual_small'), 'follows_me', *newer('i_follow_big', 'i_follow_small'),
                    'plain_none', 'plain_small', 'plain_big']
        handles, _ = self.handles(sort='recent', limit=50)
        self.assertEqual(handles, expected)
        # A profile refresh bumps updated_at; it must not move anyone in the list.
        self.conn.execute("UPDATE people SET updated_at='2030-01-01T00:00:00+00:00' WHERE handle='plain_big'")
        self.conn.commit()
        self.assertEqual(self.handles(sort='recent', limit=50)[0], expected)


if __name__ == '__main__':
    unittest.main()
