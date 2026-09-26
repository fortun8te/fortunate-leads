import random
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connections
import db


class ConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)

    def person(self, handle, ig=None):
        return db.upsert_person(self.conn, {'handle': handle, 'ig_id': ig}, '2020-01-01')

    def seed(self, handle, ig=None, me=False):
        self.conn.execute('INSERT INTO seeds VALUES(?,?,?,?)', (handle, ig, me, '2020-01-01'))

    def edge(self, seed, pid, direction='following'):
        ig = self.conn.execute('SELECT ig_id FROM seeds WHERE handle=?', (seed,)).fetchone()[0]
        db.add_edge(self.conn, seed, pid, direction, '2020-01-01', seed_ig_id=ig, person_ig_id=self.conn.execute('SELECT ig_id FROM people WHERE id=?', (pid,)).fetchone()[0])

    def test_direction_evidence_duplicates_and_reciprocity(self):
        self.seed('a', '1', True)
        self.seed('b', '2')
        a, b = self.person('a', '1'), self.person('b', '2')
        self.edge('a', b)
        self.edge('b', a, 'followers')  # Same a->b from another source list.
        result = connections.explore(self.conn, 'a', 'b')
        self.assertEqual(len(result['edges']), 1)
        self.assertEqual(len(result['edges'][0]['evidence']), 2)
        self.assertEqual(result['edges'][0]['from'], 'ig:1')
        self.assertFalse(result['edges'][0]['reciprocal'])
        self.edge('a', b, 'followers')
        result = connections.explore(self.conn, 'a', 'b')
        self.assertEqual(len(result['paths']), 1)
        self.assertEqual(result['paths'][0]['reciprocal_hops'], 1)
        self.assertEqual(len(result['edges']), 2)

    def test_directed_backward_and_cycle_free(self):
        self.seed('a', '1')
        self.seed('c', '3')
        b, d = self.person('b', '2'), self.person('d', '4')
        self.edge('a', b, 'followers')  # b -> a
        self.edge('c', b)               # c -> b
        self.edge('c', d)               # c -> d
        result = connections.explore(self.conn, 'a', 'd')
        self.assertEqual(result['paths'][0]['nodes'], ['ig:1', 'ig:2', 'ig:3', 'ig:4'])
        self.assertEqual([h['traversal'] for h in result['paths'][0]['hops']], ['backward','backward','forward'])
        self.assertEqual(connections.explore(self.conn, 'a', 'd', 'following')['paths'], [])
        self.assertEqual(connections.explore(self.conn, 'a', 'a')['paths'], [])
        for path in result['paths']:
            self.assertEqual(len(path['nodes']), len(set(path['nodes'])))

    def test_common_neighbor_types(self):
        self.seed('a', '1')
        self.seed('b', '2')
        for handle, directions in [('out', ('following','following')), ('inc', ('followers','followers')),
                                   ('fwd', ('following','followers')), ('rev', ('followers','following'))]:
            pid = self.person(handle, str(100 + sum(map(ord, handle)) ))
            self.edge('a', pid, directions[0])
            self.edge('b', pid, directions[1])
        result = connections.explore(self.conn, 'a', 'b')
        for kind in ('shared_followees','shared_followers','forward_chain','reverse_chain'):
            self.assertEqual(result['common_neighbors'][kind]['count'], 1)

    def test_seed_identity_and_handle_reuse(self):
        self.seed('old_name', '1', True)
        self.person('new_name', '1')
        self.person('old_name', '2')
        self.seed('lonely', '3')
        rows = connections.search(self.conn, '')['accounts']
        self.assertEqual(rows[0]['id'], 'ig:1')
        self.assertEqual(rows[0]['handle'], 'new_name')
        result = connections.explore(self.conn, 'seed:old_name', 'old_name')
        self.assertNotEqual(result['source']['id'], result['target']['id'])
        self.assertEqual(connections.search(self.conn, 'lon')['accounts'][0]['id'], 'ig:3')

    def test_no_id_fallback_and_seed_only(self):
        pid = self.person('same')
        self.seed('same', '42')
        self.seed('alone')
        result = connections.explore(self.conn, 'same', 'seed:alone')
        self.assertEqual(result['source']['id'], 'person:' + str(pid))
        self.assertNotEqual(result['source']['id'], connections.explore(self.conn, 'seed:same', 'seed:alone')['source']['id'])
        self.assertEqual(result['source']['person_id'], pid)
        self.assertEqual(result['target']['id'], 'seed:alone')

    def test_coverage_and_no_false_recency(self):
        self.seed('a', '1')
        self.seed('b', '2')
        self.edge('a', self.person('b', '2'))
        self.conn.execute("INSERT INTO lists(seed,direction,state,received,total,updated_at) VALUES('a','following','done',2,8,'2026-09-01')")
        result = connections.explore(self.conn, 'a', 'b')
        coverage = {(c['seed'],c['direction']): c for c in result['coverage']}
        self.assertEqual(coverage['a','following']['status'], 'ended_short')
        self.assertEqual(coverage['a','followers']['status'], 'missing')
        self.assertEqual(result['edges'][0]['evidence'][0]['first_seen'], '2020-01-01')
        self.assertNotIn('last_seen', result['edges'][0])
        self.assertEqual(result['edges'][0]['evidence'][0]['last_seen'], '2020-01-01')
        for state, total, expected in [('done', None, 'ended_unknown'), ('running',8,'partial'), ('done',2,'ended_count_matched'), ('done',1,'ended_unknown')]:
            self.conn.execute("UPDATE lists SET state=?,total=?", (state,total))
            c = next(c for c in connections.explore(self.conn,'a','b')['coverage'] if c['seed']=='a' and c['direction']=='following')
            self.assertEqual(c['status'], expected)

    def test_stable_ranking_and_caps(self):
        self.seed('a', '1')
        self.seed('b', '2')
        for handle in ('x', 'y', 'z'):
            pid = self.person(handle, str(100 + sum(map(ord, handle)) ))
            self.edge('a', pid)
            self.edge('b', pid)
        self.edge('a', self.person('z'), 'followers')
        first = connections.explore(self.conn,'a','b',limit=1)
        self.assertEqual(first, connections.explore(self.conn,'a','b',limit=1))
        self.assertEqual(first['paths'][0]['reciprocal_hops'],1)
        self.assertTrue(first['search']['truncated'])
        self.assertFalse(first['search']['search_truncated'])
        self.assertTrue(first['search']['results_truncated'])
        self.assertEqual(first['search']['paths_found'],3)
        with patch.object(connections, 'NEIGHBOR_LIMIT', 1):
            self.assertTrue(connections.explore(self.conn,'a','b')['search']['neighborhoods_clipped'])
        with patch.object(connections, 'PATH_LIMIT', 1):
            self.assertTrue(connections.explore(self.conn,'a','b')['search']['paths_clipped'])
        self.assertTrue(connections.search(self.conn,'',limit=1)['truncated'])

    def test_legacy_handles_do_not_fabricate_bridges(self):
        self.seed('a', '1')
        self.seed('reused', '99')
        a, b = self.person('a', '1'), self.person('b', '2')
        db.add_edge(self.conn, 'reused', a, 'following', '2020-01-01', person_ig_id='1')
        db.add_edge(self.conn, 'reused', b, 'following', '2020-01-01', person_ig_id='2')
        self.assertEqual(connections.explore(self.conn, 'a', 'b')['paths'], [])
        result = connections.explore(self.conn, 'legacy:reused', 'b')
        self.assertEqual(result['source']['identity_basis'], 'legacy_handle')
        self.assertEqual(len(result['paths']), 1)
        self.assertEqual(connections.explore(self.conn, 'seed:reused', 'b')['paths'], [])
        self.assertIn('legacy:reused', [a['id'] for a in connections.search(self.conn, 'reu')['accounts']])

    def test_captured_source_id_survives_seed_reuse(self):
        self.seed('reused', '99')
        person = self.person('b', '2')
        db.add_edge(self.conn, 'reused', person, 'following', '2020-01-01', seed_ig_id='77', person_ig_id='2')
        result = connections.explore(self.conn, 'ig:77', 'b')
        self.assertEqual(result['source']['id'], 'ig:77')
        self.assertEqual(len(result['paths']), 1)
        self.assertEqual(connections.explore(self.conn, 'seed:reused', 'b')['paths'], [])

    def test_middle_cap_and_read_only_execution(self):
        self.seed('a', '1')
        self.seed('middle', '3')
        b, d = self.person('b', '2'), self.person('d', '4')
        self.edge('a', b)
        self.edge('middle', b)
        self.edge('middle', d)
        before = self.conn.total_changes
        with patch.object(connections, 'MIDDLE_EDGE_LIMIT', 0):
            result = connections.explore(self.conn, 'a', 'd')
        self.assertTrue(result['search']['neighborhoods_clipped'])
        self.assertEqual(result['paths'], [])
        self.assertEqual(self.conn.total_changes, before)
        with patch.object(connections, 'MIDDLE_QUERY_LIMIT', 0):
            clipped = connections.explore(self.conn, 'a', 'd')
        self.assertTrue(clipped['search']['search_truncated'])
        self.assertEqual(clipped['search']['middle_queries'], 0)
        target_coverage = [c for c in result['coverage'] if c['account'] == 'ig:4']
        self.assertEqual(len(target_coverage), 2)
        self.assertTrue(all(c['status'] == 'missing' for c in target_coverage))

    def test_direct_pair_survives_both_endpoint_caps(self):
        self.seed('a', '1')
        self.seed('b', '2')
        for i in range(6):
            pid = self.person('noise' + str(i), str(i + 10))
            self.edge('a', pid)
            self.edge('b', pid)
        a, b = self.person('a', '1'), self.person('b', '2')
        self.edge('a', b)
        self.edge('a', b, 'followers')
        with patch.object(connections, 'NEIGHBOR_LIMIT', 1):
            result = connections.explore(self.conn, 'a', 'b')
        self.assertTrue(result['search']['neighborhoods_clipped'])
        self.assertEqual(len(result['direct_edges']), 2)
        self.assertEqual(result['paths'][0]['length'], 1)
        self.assertEqual(result['paths'][0]['reciprocal_hops'], 1)

    def test_tiny_graph_oracle(self):
        # Independently enumerate every simple path; edge orientation is randomized
        # between followers/following lists so this also checks normalization.
        for sample in range(6):
            self.setUp()
            rng = random.Random(sample)
            ids = []
            for i in range(6):
                self.seed('u' + str(i), str(i + 1))
                ids.append(self.person('u' + str(i), str(i + 1)))
            observed = set()
            for a in range(6):
                for b in range(6):
                    if a != b and rng.random() < 0.35:
                        observed.add((a, b))
                        if rng.random() < 0.5:
                            self.edge('u' + str(a), ids[b])
                        else:
                            self.edge('u' + str(b), ids[a], 'followers')
            for mode in ('all', 'following'):
                expected = set()
                def visit(path):
                    if path[-1] == 5:
                        expected.add(tuple('ig:' + str(i + 1) for i in path))
                        return
                    if len(path) == 4:
                        return
                    for nxt in range(6):
                        if nxt not in path and ((path[-1], nxt) in observed or
                                                mode == 'all' and (nxt, path[-1]) in observed):
                            visit(path + [nxt])
                visit([0])
                result = connections.explore(self.conn, 'u0', 'u5', mode, limit=50)
                self.assertEqual({tuple(p['nodes']) for p in result['paths']}, expected, (sample, mode))
                edges = {e['id']: e for e in result['edges']}
                for path in result['paths']:
                    for hop in path['hops']:
                        edge = edges[hop['edge']]
                        a, b = int(edge['from'][3:]) - 1, int(edge['to'][3:]) - 1
                        self.assertIn((a, b), observed)
                        self.assertTrue(edge['evidence'])
                        self.assertEqual(edge['from'], hop['from'] if hop['traversal'] == 'forward' else hop['to'])

    def test_idless_member_never_promoted_into_bridge(self):
        self.seed('a', '1')
        self.seed('bridge', '99')
        self.seed('b', '2')
        bridge = self.person('bridge')
        b = self.person('b', '2')
        self.edge('a', bridge)
        self.edge('bridge', b)
        self.assertEqual(connections.explore(self.conn, 'a', 'b')['paths'], [])
        self.person('bridge', '99')  # Later enrichment must not rewrite old evidence.
        self.assertEqual(connections.explore(self.conn, 'a', 'b')['paths'], [])
        result = connections.explore(self.conn, 'a', 'legacy-person:' + str(bridge))
        self.assertEqual(len(result['paths']), 1)
        self.assertEqual(result['target']['identity_basis'], 'legacy_person')
        found = connections.search(self.conn, 'bridge')['accounts']
        self.assertIn('legacy-person:' + str(bridge), [a['id'] for a in found])
        self.assertIn('ig:99', [a['id'] for a in found])

    def test_captured_member_identity_survives_changed_people_row(self):
        self.seed('a', '1')
        member = self.person('old_name', '2')
        self.edge('a', member)
        self.conn.execute("UPDATE people SET ig_id='99',handle='new_owner' WHERE id=?", (member,))
        result = connections.explore(self.conn, 'a', 'ig:2')
        self.assertEqual(len(result['paths']), 1)
        self.assertNotEqual(result['target']['handle'], 'new_owner')
        self.assertEqual(connections.explore(self.conn, 'a', 'ig:99')['paths'], [])

    def test_non_source_neighbors_do_not_exhaust_middle_query_budget(self):
        self.seed('start', '1')
        self.seed('bridge', '2')
        target = self.person('target', '3')
        for i in range(250):
            pid = self.person(f'member{i}', str(1000 + i))
            self.edge('start', pid)
        self.edge('bridge', pid)
        self.edge('bridge', target)
        result = connections.explore(self.conn, 'start', 'target')
        self.assertEqual(result['paths'][0]['nodes'], ['ig:1', 'ig:1249', 'ig:2', 'ig:3'])
        self.assertFalse(result['search']['search_truncated'])
        self.assertLess(result['search']['middle_queries'], 10)

    def test_validation_injection_and_url(self):
        self.seed('safe', '1')
        self.assertEqual(connections.search(self.conn,'https://www.instagram.com/safe/?x=1')['accounts'][0]['id'], 'ig:1')
        for value in ("x' OR 1=1 --", 'https://evil.com/safe', 'a/b', 'person:1 OR 1=1', None):
            with self.assertRaises(ValueError):
                connections.explore(self.conn, value, 'safe')
        with self.assertRaises(LookupError):
            connections.explore(self.conn,'unknown','safe')
        with self.assertRaises(ValueError):
            connections.explore(self.conn,'safe','safe','nonsense')
        with self.assertRaises(ValueError):
            connections.search(self.conn,'safe',0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM seeds').fetchone()[0],1)


if __name__ == '__main__':
    unittest.main()
