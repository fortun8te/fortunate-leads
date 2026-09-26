"""Route-limit and payload contracts using fictional in-memory observations."""
import itertools
import random
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connections
import db


class ConnectionPathLimitTests(unittest.TestCase):
    def setUp(self):
        self.conn = self.database()
        self.observed = set()

    def database(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        return conn

    def accounts(self, ids, conn=None):
        conn = self.conn if conn is None else conn
        for ig in ids:
            conn.execute('INSERT INTO seeds(handle,ig_id) VALUES(?,?)', ('user' + ig, ig))
            db.upsert_person(conn, {'handle': 'user' + ig, 'ig_id': ig}, '2026-01-01')

    def observe(self, a, b, *, followers=False, conn=None):
        conn = self.conn if conn is None else conn
        seed, member = (b, a) if followers else (a, b)
        person = conn.execute('SELECT id FROM people WHERE ig_id=?', (member,)).fetchone()[0]
        db.add_edge(conn, 'user' + seed, person, 'followers' if followers else 'following',
                    '2026-01-01', seed_ig_id=seed, person_ig_id=member)
        self.observed.add(('ig:' + a, 'ig:' + b))

    def parallel_routes(self, intermediates=('2', '3', '4')):
        self.accounts(('1', '9', *intermediates))
        for middle in intermediates:
            self.observe('1', middle)
            self.observe(middle, '9', followers=True)

    def assert_payload(self, result):
        """Each selectable route must be explainable from its own response."""
        nodes = {n['id']: n for n in result['nodes']}
        edges = {e['id']: e for e in result['edges']}
        self.assertEqual(len(nodes), len(result['nodes']))
        self.assertEqual(len(edges), len(result['edges']))
        source, target = result['source']['id'], result['target']['id']
        used = {source, target}
        self.assertEqual(len({p['id'] for p in result['paths']}), len(result['paths']))
        for path in result['paths']:
            used.update(path['nodes'])
            self.assertEqual(path['nodes'][0], source)
            self.assertEqual(path['nodes'][-1], target)
            self.assertEqual(len(path['nodes']), len(set(path['nodes'])))
            self.assertEqual(path['length'], len(path['nodes']) - 1)
            self.assertIn(path['length'], (1, 2, 3))
            self.assertEqual(len(path['hops']), path['length'])
            self.assertEqual(path['id'], '|'.join(path['nodes']))
            reciprocal_count = 0
            for (a, b), hop in zip(zip(path['nodes'], path['nodes'][1:]), path['hops']):
                self.assertEqual((hop['from'], hop['to']), (a, b))
                edge = edges[hop['edge']]
                expected = (a, b) if hop['traversal'] == 'forward' else (b, a)
                self.assertEqual((edge['from'], edge['to']), expected)
                if result['mode'] == 'following':
                    self.assertEqual(hop['traversal'], 'forward')
                reciprocal = (a, b) in self.observed and (b, a) in self.observed
                self.assertEqual(hop['reciprocal'], reciprocal)
                self.assertEqual(edge['reciprocal'], reciprocal)
                if reciprocal:
                    self.assertIn(edge['to'] + '>' + edge['from'], edges)
                    reciprocal_count += 1
            self.assertEqual(path['reciprocal_hops'], reciprocal_count)
        self.assertEqual(set(nodes), used)
        for edge in edges.values():
            self.assertIn((edge['from'], edge['to']), self.observed)
            self.assertIn(edge['from'], nodes)
            self.assertIn(edge['to'], nodes)
            self.assertTrue(edge['evidence'])
            for evidence in edge['evidence']:
                pair = ('ig:' + evidence['seed_ig_id'], 'ig:' + evidence['person_ig_id'])
                if evidence['direction'] == 'followers':
                    pair = pair[::-1]
                self.assertEqual(pair, (edge['from'], edge['to']))
        self.assertEqual(result['direct_edges'], [e for e in result['edges']
                         if {e['from'], e['to']} == {source, target}])
        self.assertTrue(all(row['account'] in nodes for row in result['coverage']))

    def test_candidate_and_display_caps_are_independent_at_exact_boundaries(self):
        self.parallel_routes()
        for mode, candidate_cap, returned_cap in itertools.product(
                ('all', 'following'), (2, 3, 4), (1, 2, 3, 4)):
            with self.subTest(mode=mode, candidates=candidate_cap, returned=returned_cap):
                with patch.object(connections, 'PATH_LIMIT', candidate_cap):
                    result = connections.explore(self.conn, 'user1', 'user9', mode, returned_cap)
                found = min(3, candidate_cap)
                search = result['search']
                self.assertEqual(search['paths_found'], found)
                self.assertEqual(search['paths_displayed'], min(found, returned_cap))
                self.assertEqual(search['paths_clipped'], candidate_cap < 3)
                self.assertFalse(search['neighborhoods_clipped'])
                self.assertEqual(search['search_truncated'], candidate_cap < 3)
                self.assertEqual(search['results_truncated'], found > returned_cap)
                self.assertEqual(search['truncated'], candidate_cap < 3 or found > returned_cap)
                self.assert_payload(result)

    def test_direct_path_consumes_candidate_budget_and_remains_first(self):
        self.parallel_routes()
        self.observe('1', '9')
        self.observe('2', '1')
        self.observe('9', '2')
        with patch.object(connections, 'PATH_LIMIT', 1):
            result = connections.explore(self.conn, 'user1', 'user9', limit=50)
        self.assertEqual([p['nodes'] for p in result['paths']], [['ig:1', 'ig:9']])
        self.assertEqual(result['search']['paths_found'], 1)
        self.assertTrue(result['search']['search_truncated'])
        self.assertFalse(result['search']['results_truncated'])
        self.assert_payload(result)

    def test_candidate_cap_does_not_claim_globally_best_reciprocal_route(self):
        self.parallel_routes(('2', '3'))
        self.observe('3', '1')
        self.observe('9', '3')
        full = connections.explore(self.conn, 'user1', 'user9')
        self.assertEqual(full['paths'][0]['nodes'], ['ig:1', 'ig:3', 'ig:9'])
        self.assertEqual(full['paths'][0]['reciprocal_hops'], 2)
        with patch.object(connections, 'PATH_LIMIT', 1):
            capped = connections.explore(self.conn, 'user1', 'user9', limit=50)
        self.assertEqual(capped['paths'][0]['nodes'], ['ig:1', 'ig:2', 'ig:9'])
        self.assertEqual(capped['paths'][0]['reciprocal_hops'], 0)
        self.assertTrue(capped['search']['search_truncated'])
        self.assertIn('bounded', capped['ranking'].lower())
        self.assertIn('not a global ranking', capped['ranking'].lower())
        self.assertIn('do not prove absence', capped['interpretation'].lower())
        self.assert_payload(capped)

    def test_default_display_limit_does_not_clip_candidate_search(self):
        self.parallel_routes(tuple(str(i) for i in range(100, 114)))
        default = connections.explore(self.conn, 'user1', 'user9')
        expanded = connections.explore(self.conn, 'user1', 'user9', limit=50)
        self.assertEqual(default['paths'], expanded['paths'][:12])
        self.assertEqual(default['search']['paths_found'], 14)
        self.assertTrue(default['search']['results_truncated'])
        self.assertFalse(default['search']['search_truncated'])
        self.assertFalse(expanded['search']['truncated'])
        self.assert_payload(default)

    def test_ranking_and_selection_are_stable_across_limits_and_selector_forms(self):
        self.parallel_routes(('10', '2', '30'))
        self.observe('1', '9')
        self.observe('30', '1')
        self.observe('9', '30')
        self.accounts(('40', '50'))
        for a, b in (('1', '40'), ('40', '50'), ('50', '9')):
            self.observe(a, b)
            self.observe(b, a)
        full = connections.explore(self.conn, 'user1', 'user9', limit=50)
        self.assertEqual([p['nodes'] for p in full['paths']], [
            ['ig:1', 'ig:9'], ['ig:1', 'ig:30', 'ig:9'],
            ['ig:1', 'ig:10', 'ig:9'], ['ig:1', 'ig:2', 'ig:9'],
            ['ig:1', 'ig:40', 'ig:50', 'ig:9']])
        self.assertEqual([p['reciprocal_hops'] for p in full['paths']], [0, 2, 0, 0, 3])
        for mode, limit in itertools.product(('all', 'following'), (1, 2, 3, 4, 5, 50)):
            with self.subTest(mode=mode, limit=limit):
                result = connections.explore(self.conn, 'ig:1', 'seed:user9', mode, limit)
                self.assertEqual(result['paths'], full['paths'][:limit])
                self.assertEqual(result, connections.explore(self.conn, 'user1', 'user9', mode, limit))
                self.assert_payload(result)

    def test_uncapped_path_order_is_independent_of_insertion_order(self):
        ids = ['1', '9', '10', '2', '30', '40']
        follows = [('1', '10'), ('10', '9'), ('1', '2'), ('2', '9'),
                   ('1', '30'), ('30', '9'), ('30', '1'), ('1', '40'),
                   ('40', '30'), ('9', '30')]
        outputs = []
        for seed in range(4):
            conn = self.database()
            rng = random.Random(seed)
            shuffled_ids, shuffled_follows = ids[:], follows[:]
            rng.shuffle(shuffled_ids)
            rng.shuffle(shuffled_follows)
            self.accounts(shuffled_ids, conn)
            for a, b in shuffled_follows:
                self.observe(a, b, followers=(int(a) + int(b)) % 2 == 0, conn=conn)
            result = connections.explore(conn, 'user1', 'user9', limit=50)
            self.assertFalse(result['search']['truncated'])
            self.assert_payload(result)
            outputs.append((result['paths'], result['edges']))
        self.assertTrue(all(output == outputs[0] for output in outputs[1:]))

    def test_cycles_and_self_follows_never_become_routes(self):
        ids = ('1', '2', '3', '9')
        self.accounts(ids)
        for a, b in itertools.product(ids, repeat=2):
            self.observe(a, b, followers=int(a) % 2 == 0)
        expected = {('ig:1', 'ig:9'), ('ig:1', 'ig:2', 'ig:9'),
                    ('ig:1', 'ig:3', 'ig:9'), ('ig:1', 'ig:2', 'ig:3', 'ig:9'),
                    ('ig:1', 'ig:3', 'ig:2', 'ig:9')}
        for mode in ('all', 'following'):
            result = connections.explore(self.conn, 'user1', 'user9', mode, 50)
            self.assertEqual({tuple(p['nodes']) for p in result['paths']}, expected)
            self.assertEqual(result['search']['paths_found'], 5)
            self.assertFalse(result['search']['truncated'])
            self.assertTrue(all(e['from'] != e['to'] for e in result['edges']))
            self.assert_payload(result)

    def test_same_identity_through_different_selectors_has_no_return_route(self):
        self.parallel_routes()
        self.observe('2', '1')
        self.observe('1', '1')
        pid = self.conn.execute("SELECT id FROM people WHERE ig_id='1'").fetchone()[0]
        for mode, target in itertools.product(('all', 'following'), ('ig:1', 'seed:user1', f'person:{pid}')):
            with self.subTest(mode=mode, target=target):
                result = connections.explore(self.conn, 'user1', target, mode)
                self.assertEqual(result['source'], result['target'])
                self.assertEqual(result['paths'], [])
                self.assertEqual(result['edges'], [])
                self.assertEqual(result['search']['paths_found'], 0)
                self.assertEqual(result['search']['paths_displayed'], 0)
                self.assertFalse(result['search']['paths_clipped'])
                self.assert_payload(result)

    def test_backward_and_reciprocal_payloads_keep_the_recorded_arrows(self):
        self.accounts(('1', '2', '3', '9'))
        self.observe('2', '1', followers=True)
        self.observe('3', '2')
        self.observe('2', '3', followers=True)
        self.observe('3', '9')
        self.observe('3', '9', followers=True)  # Duplicate supporting source, same arrow.
        result = connections.explore(self.conn, 'user1', 'user9')
        path = result['paths'][0]
        self.assertEqual(path['nodes'], ['ig:1', 'ig:2', 'ig:3', 'ig:9'])
        self.assertEqual([h['traversal'] for h in path['hops']], ['backward', 'forward', 'forward'])
        self.assertEqual(path['reciprocal_hops'], 1)
        self.assertEqual(len(next(e for e in result['edges'] if e['id'] == 'ig:3>ig:9')['evidence']), 2)
        self.assert_payload(result)
        directed = connections.explore(self.conn, 'user1', 'user9', 'following')
        self.assertEqual(directed['paths'], [])
        self.assert_payload(directed)

    def test_real_candidate_cap_with_no_neighborhood_or_middle_clipping(self):
        # 71 shared neighbors yield 71 two-step + 71*70 three-step routes in all
        # mode. One arrow per neighbor pair gives 2,485 middle observations,
        # read twice, staying below the unmodified 5,000 middle-observation cap.
        middles = tuple(str(i) for i in range(100, 171))
        self.parallel_routes(middles)
        for a, b in itertools.combinations(middles, 2):
            self.observe(a, b)
        self.assertEqual(connections.PATH_LIMIT, 5000)
        self.assertEqual(len(middles) + len(middles) * (len(middles) - 1), 5041)
        result = connections.explore(self.conn, 'user1', 'user9', limit=50)
        self.assertFalse(result['search']['neighborhoods_clipped'])
        self.assertTrue(result['search']['paths_clipped'])
        self.assertTrue(result['search']['search_truncated'])
        self.assertTrue(result['search']['results_truncated'])
        self.assertEqual(result['search']['path_limit'], 5000)
        self.assertEqual(result['search']['paths_found'], 5000)
        self.assertEqual(result['search']['paths_displayed'], 50)
        self.assertEqual([p['nodes'] for p in result['paths']],
                         [['ig:1', 'ig:' + middle, 'ig:9'] for middle in middles[:50]])
        self.assert_payload(result)
        directed = connections.explore(self.conn, 'user1', 'user9', 'following', limit=50)
        self.assertEqual(directed['search']['paths_found'], 71 + 2485)
        self.assertFalse(directed['search']['search_truncated'])
        self.assertTrue(directed['search']['results_truncated'])
        self.assertEqual(directed['paths'], result['paths'])
        self.assert_payload(directed)


if __name__ == '__main__':
    unittest.main()
