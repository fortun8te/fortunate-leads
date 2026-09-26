"""Directed motif oracle, independent of which account supplied each list."""
import itertools
import sqlite3
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connections
import db


class ConnectionMotifTests(unittest.TestCase):
    IDS = {'a': '101', 'c': '303', 'b': '202'}
    ARROWS = (('a', 'c'), ('c', 'a'), ('c', 'b'), ('b', 'c'))
    KINDS = ('shared_followees', 'shared_followers', 'forward_chain', 'reverse_chain')

    @contextmanager
    def graph(self, ids=None, order=None, prefix=''):
        ids = ids or self.IDS
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(db.SCHEMA)
            people = {}
            handles = {name: prefix + name for name in ids}
            for name in order or ids:
                people[name] = db.upsert_person(conn, {'handle': handles[name], 'ig_id': ids[name]}, '2020-01-01')
                conn.execute('INSERT INTO seeds VALUES(?,?,?,?)', (handles[name], ids[name], 0, '2020-01-01'))
            yield conn, ids, people, handles
        finally:
            conn.close()

    def follow(self, fixture, arrow, encoding=3):
        """1 = follower's following list, 2 = followee's followers list, 3 = both."""
        conn, ids, people, handles = fixture
        follower, followee = arrow
        for flag, source, member, direction in (
                (1, follower, followee, 'following'),
                (2, followee, follower, 'followers')):
            if encoding & flag:
                # Replaying the same import must not become another piece of evidence.
                for _ in range(2):
                    db.add_edge(conn, handles[source], people[member], direction, '2020-01-01',
                                seed_ig_id=ids[source], person_ig_id=ids[member])

    @staticmethod
    def membership(arrows, a, c, b):
        # Each definition is a conjunction of two actual follows, never list direction.
        return {
            'shared_followees': (a, c) in arrows and (b, c) in arrows,
            'shared_followers': (c, a) in arrows and (c, b) in arrows,
            'forward_chain': (a, c) in arrows and (c, b) in arrows,
            'reverse_chain': (c, a) in arrows and (b, c) in arrows,
        }

    def test_exhaustive_motifs_and_list_encodings_in_both_modes_and_orders(self):
        # Four independent arrows, each absent or recorded by either/both source lists:
        # 4**4 = 256 representations of all 16 possible directed three-account graphs.
        for encodings in itertools.product(range(4), repeat=4):
            present = {arrow for arrow, encoding in zip(self.ARROWS, encodings) if encoding}
            encodings_by_arrow = dict(zip(self.ARROWS, encodings))
            with self.graph(order=('b', 'c', 'a') if sum(encodings) % 2 else None) as fixture:
                conn, ids, _, _ = fixture
                for arrow, encoding in zip(self.ARROWS, encodings):
                    self.follow(fixture, arrow, encoding)
                for a, b in (('a', 'b'), ('b', 'a')):
                    expected = self.membership(present, a, 'c', b)
                    for mode in ('all', 'following'):
                        with self.subTest(encodings=encodings, source=a, mode=mode):
                            result = connections.explore(conn, 'ig:' + ids[a], 'ig:' + ids[b], mode)
                            self.assertEqual(set(result['common_neighbors']), set(self.KINDS))
                            for kind, member in expected.items():
                                actual = result['common_neighbors'][kind]
                                self.assertEqual(actual['count'], int(member))
                                self.assertEqual(actual['displayed'], int(member))
                                self.assertEqual([p['id'] for p in actual['accounts']], ['ig:303'] if member else [])
                                self.assertFalse(actual['bounded'])
                            reachable = any(expected.values()) if mode == 'all' else expected['forward_chain']
                            self.assertEqual(len(result['paths']), int(reachable))
                            self.assertEqual(result['search']['paths_found'], int(reachable))
                            self.assertFalse(result['search']['truncated'])
                            self.assertEqual(result['direct_edges'], [])
                            if not reachable:
                                self.assertEqual(result['edges'], [])
                                continue
                            path = result['paths'][0]
                            nodes = ['ig:' + ids[name] for name in (a, 'c', b)]
                            self.assertEqual(path['nodes'], nodes)
                            self.assertEqual(path['id'], '|'.join(nodes))
                            self.assertEqual(path['length'], 2)
                            reciprocal = sum((x, y) in present and (y, x) in present
                                             for x, y in ((a, 'c'), ('c', b)))
                            self.assertEqual(path['reciprocal_hops'], reciprocal)
                            edges = {(e['from'], e['to']): e for e in result['edges']}
                            self.assertEqual(set(edges), {('ig:' + ids[x], 'ig:' + ids[y]) for x, y in present})
                            for x, y in present:
                                edge = edges['ig:' + ids[x], 'ig:' + ids[y]]
                                self.assertEqual(edge['id'], 'ig:' + ids[x] + '>ig:' + ids[y])
                                self.assertEqual(edge['reciprocal'], (y, x) in present)
                                expected_evidence = set()
                                encoding = encodings_by_arrow[x, y]
                                if encoding & 1:
                                    expected_evidence.add((x, 'following', ids[x], ids[y]))
                                if encoding & 2:
                                    expected_evidence.add((y, 'followers', ids[y], ids[x]))
                                actual_evidence = [(e['seed'], e['direction'], e['seed_ig_id'], e['person_ig_id'])
                                                   for e in edge['evidence']]
                                self.assertEqual(set(actual_evidence), expected_evidence)
                                self.assertEqual(len(actual_evidence), len(expected_evidence))
                            for hop, (x, y) in zip(path['hops'], ((a, 'c'), ('c', b))):
                                forward = (x, y) in present
                                self.assertEqual((hop['from'], hop['to']), ('ig:' + ids[x], 'ig:' + ids[y]))
                                self.assertEqual(hop['traversal'], 'forward' if forward else 'backward')
                                self.assertEqual(hop['edge'], 'ig:' + ids[x if forward else y] + '>ig:' + ids[y if forward else x])
                                self.assertEqual(hop['reciprocal'], (x, y) in present and (y, x) in present)
                                if mode == 'following':
                                    self.assertEqual(hop['traversal'], 'forward')

    def test_overlapping_categories_count_accounts_not_motifs_or_evidence(self):
        # Nine connected direction combinations. Each category contains four accounts;
        # their memberships sum to 16 but there are only nine distinct middle accounts.
        middles = list(itertools.product((1, 2, 3), repeat=2))
        ids = {'a': '101', 'b': '202', **{f'c{i}': str(900 - i) for i in range(9)}}
        present = set()
        with self.graph(ids) as fixture:
            conn = fixture[0]
            for i, (left, right) in enumerate(middles):
                c = f'c{i}'
                for flag, arrow in ((left & 1, ('a', c)), (left & 2, (c, 'a')),
                                    (right & 1, (c, 'b')), (right & 2, ('b', c))):
                    if flag:
                        present.add(arrow)
                        self.follow(fixture, arrow)
            for mode, paths_found in (('all', 9), ('following', 4)):
                with self.subTest(mode=mode):
                    result = connections.explore(conn, 'a', 'b', mode, limit=2)
                    self.assertEqual(result['search']['paths_found'], paths_found)
                    self.assertEqual(result['search']['paths_displayed'], 2)
                    self.assertTrue(result['search']['results_truncated'])
                    self.assertFalse(result['search']['search_truncated'])
                    self.assertEqual(len({p['id'] for p in result['paths']}), 2)
                    self.assertEqual(result['paths'][0]['nodes'], ['ig:101', 'ig:892', 'ig:202'])
                    for kind in self.KINDS:
                        members = sorted('ig:' + ids[f'c{i}'] for i in range(9)
                                         if self.membership(present, 'a', f'c{i}', 'b')[kind])
                        common = result['common_neighbors'][kind]
                        self.assertEqual(common['count'], 4)
                        self.assertEqual(common['displayed'], 2)
                        self.assertEqual([p['id'] for p in common['accounts']], members[:2])
                        self.assertFalse(common['bounded'])

    def test_path_edge_and_member_ids_survive_handle_changes_and_import_order(self):
        snapshots = []
        for order, prefix in ((('a', 'c', 'b'), ''), (('b', 'c', 'a'), 'renamed_')):
            with self.graph(order=order, prefix=prefix) as fixture:
                for arrow in self.ARROWS[::1 if not prefix else -1]:
                    self.follow(fixture, arrow)
                result = connections.explore(fixture[0], 'ig:101', 'ig:202')
                snapshots.append({
                    'paths': result['paths'],
                    'edges': [(e['id'], e['from'], e['to'], e['reciprocal']) for e in result['edges']],
                    'members': {kind: [p['id'] for p in result['common_neighbors'][kind]['accounts']]
                                for kind in self.KINDS},
                })
        self.assertEqual(snapshots[0], snapshots[1])

    def test_malformed_self_follows_do_not_create_motifs_paths_or_reciprocity(self):
        with self.graph() as fixture:
            conn, ids, people, _ = fixture
            for mode in ('all', 'following'):
                result = connections.explore(conn, 'a', 'b', mode)
                self.assertEqual(result['paths'], [])
            for name in ids:
                self.follow(fixture, (name, name))
            # Another source handle with the same captured ID is still a self-follow.
            conn.execute('INSERT INTO seeds VALUES(?,?,?,?)', ('old_c', ids['c'], 0, '2020-01-01'))
            db.add_edge(conn, 'old_c', people['c'], 'following', '2020-01-01',
                        seed_ig_id=ids['c'], person_ig_id=ids['c'])
            for mode in ('all', 'following'):
                result = connections.explore(conn, 'a', 'b', mode)
                self.assertEqual(result['paths'], [])
                self.assertEqual(result['edges'], [])
                self.assertEqual([result['common_neighbors'][k]['count'] for k in self.KINDS], [0] * 4)
                self.assertEqual(connections.explore(conn, 'a', 'a', mode)['paths'], [])
            self.follow(fixture, ('a', 'c'), 2)
            self.follow(fixture, ('c', 'b'), 1)
            for mode in ('all', 'following'):
                result = connections.explore(conn, 'a', 'b', mode)
                self.assertEqual(len(result['paths']), 1)
                self.assertEqual(result['paths'][0]['nodes'], ['ig:101', 'ig:303', 'ig:202'])
                self.assertEqual(result['paths'][0]['reciprocal_hops'], 0)
                self.assertEqual([result['common_neighbors'][k]['count'] for k in self.KINDS], [0, 0, 1, 0])
                self.assertEqual(len(result['edges']), 2)
                self.assertTrue(all(e['from'] != e['to'] and not e['reciprocal'] for e in result['edges']))


if __name__ == '__main__':
    unittest.main()
