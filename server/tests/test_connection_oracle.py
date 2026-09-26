"""Independent exhaustive oracle for the public connection exploration API.

Each fixture starts with a directed graph, then encodes that graph as observed
source lists. The expected answer uses permutations of vertices, never any
production graph helper or SQL result. Eight vertices permit at most 37 simple
paths of one to three hops per distinct pair, below the API's return limit.
"""

import itertools
import random
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connections
import db


GRAPH_COUNT = 128
RANDOM_SEED = 0xC0FFEE
FIRST_RECORDED = '2020-01-01T00:00:00Z'
LAST_RECORDED = '2021-01-01T00:00:00Z'
EVIDENCE_FIELDS = ('seed', 'direction', 'seed_ig_id', 'person_ig_id',
                   'first_seen', 'last_seen')


def generated_graph(sample):
    """Generate sparse, dense, reciprocal, isolated, empty and complete cases."""
    rng = random.Random(RANDOM_SEED + sample)
    size = rng.randint(2, 8)
    # Deliberately mix numeric widths so stable-ID order differs from number order.
    identifiers = rng.sample(('1', '2', '9', '10', '11', '21', '101', '1000'), size)
    nodes = tuple('ig:' + value for value in identifiers)
    probability = (0.08, 0.2, 0.35, 0.55, 0.8)[sample % 5]
    follows = {(a, b) for a in nodes for b in nodes
               if a != b and rng.random() < probability}
    if sample % 11 == 0:
        follows.clear()
    elif sample % 13 == 0:
        follows = set(itertools.permutations(nodes, 2))
    elif sample % 7 == 0:
        follows = {(a, b) for a, b in follows if nodes[-1] not in (a, b)}
    elif sample % 5 == 0:
        follows.update(((nodes[0], nodes[1]), (nodes[1], nodes[0])))
    return nodes, follows


def all_simple_paths(nodes, follows, source, target, mode):
    """Enumerate the definition directly, independently of the retrieval code."""
    if source == target:
        return []  # The API reports positive-length, cycle-free paths only.
    available = tuple(node for node in nodes if node not in (source, target))
    paths = []
    for middle_count in range(3):
        for middle in itertools.permutations(available, middle_count):
            path = (source,) + middle + (target,)
            if all((a, b) in follows or (mode == 'all' and (b, a) in follows)
                   for a, b in zip(path, path[1:])):
                paths.append(path)
    return sorted(paths, key=lambda path: (
        len(path),
        -sum((a, b) in follows and (b, a) in follows
             for a, b in zip(path, path[1:])),
        path,
    ))


def write_fixture(conn, nodes, follows, sample):
    """Encode each logical edge using either list orientation or both sources."""
    rng = random.Random(RANDOM_SEED * 2 + sample)
    handles = {node: 'oracle_%d_%d' % (sample, index)
               for index, node in enumerate(nodes)}
    people = {}
    for index, node in enumerate(nodes):
        people[node] = db.upsert_person(conn, {
            'handle': handles[node], 'ig_id': node[3:],
        }, FIRST_RECORDED)
        # Some source metadata is absent. Captured identity must still work.
        if index % 3 != sample % 3:
            conn.execute('INSERT INTO seeds VALUES(?,?,?,?)',
                         (handles[node], node[3:], index == 0, FIRST_RECORDED))

    evidence = {}
    for index, (a, b) in enumerate(sorted(follows)):
        orientation = (sample + index) % 3
        observations = []
        if orientation != 1:
            observations.append((a, b, 'following', handles[a]))
        if orientation != 0:
            observations.append((b, a, 'followers', handles[b]))
        if rng.random() < 0.25:
            # Another recorded source handle with the same captured account ID.
            observations.append((a, b, 'following', handles[a] + '_old'))
        expected = set()
        for source, member, direction, handle in observations:
            for timestamp in (FIRST_RECORDED, FIRST_RECORDED, LAST_RECORDED):
                db.add_edge(conn, handle, people[member], direction, timestamp,
                            seed_ig_id=source[3:], person_ig_id=member[3:])
            expected.add((handle, direction, source[3:], member[3:],
                          FIRST_RECORDED, LAST_RECORDED))
        evidence[a, b] = expected
    return handles, evidence


class ConnectionOracleTests(unittest.TestCase):
    def assert_matches_oracle(self, result, nodes, follows, evidence,
                              source, target, mode):
        expected = all_simple_paths(nodes, follows, source, target, mode)
        actual = [tuple(path['nodes']) for path in result['paths']]
        self.assertEqual(actual, expected)
        self.assertEqual(result['source']['id'], source)
        self.assertEqual(result['target']['id'], target)
        self.assertEqual(result['mode'], mode)
        self.assertEqual(result['search']['paths_found'], len(expected))
        self.assertEqual(result['search']['paths_displayed'], len(expected))
        self.assertFalse(result['search']['search_truncated'])
        self.assertFalse(result['search']['results_truncated'])
        self.assertFalse(result['search']['truncated'])

        used_nodes = {source, target}
        required_edges = {(a, b) for a, b in follows if {a, b} == {source, target}}
        for path in expected:
            used_nodes.update(path)
            for a, b in zip(path, path[1:]):
                required_edges.update(edge for edge in ((a, b), (b, a))
                                      if edge in follows)
        self.assertEqual({node['id'] for node in result['nodes']}, used_nodes)
        self.assertEqual(len(result['nodes']), len(used_nodes))
        returned_edges = {(edge['from'], edge['to']): edge for edge in result['edges']}
        self.assertEqual(set(returned_edges), required_edges)
        self.assertEqual(len(result['edges']), len(returned_edges))
        by_edge_id = {edge['id']: edge for edge in result['edges']}
        self.assertEqual(len(by_edge_id), len(returned_edges))

        expected_direct = {(a, b) for a, b in follows if {a, b} == {source, target}}
        self.assertEqual({(edge['from'], edge['to']) for edge in result['direct_edges']},
                         expected_direct)
        for (a, b), edge in returned_edges.items():
            self.assertEqual(edge['reciprocal'], (b, a) in follows)
            observed_evidence = {
                tuple(item[field] for field in EVIDENCE_FIELDS)
                for item in edge['evidence']
            }
            self.assertEqual(observed_evidence, evidence[a, b])
            self.assertEqual(len(edge['evidence']), len(observed_evidence))
            for item in edge['evidence']:
                list_owner = 'ig:' + item['seed_ig_id']
                list_member = 'ig:' + item['person_ig_id']
                observation = ((list_owner, list_member) if item['direction'] == 'following'
                               else (list_member, list_owner))
                self.assertEqual(observation, (a, b))

        self.assertEqual(len({path['id'] for path in result['paths']}), len(expected))
        for path in result['paths']:
            self.assertEqual(path['length'], len(path['nodes']) - 1)
            self.assertEqual(len(path['hops']), path['length'])
            self.assertLessEqual(path['length'], 3)
            self.assertEqual(len(path['nodes']), len(set(path['nodes'])))
            reciprocal_hops = 0
            for (a, b), hop in zip(zip(path['nodes'], path['nodes'][1:]), path['hops']):
                self.assertEqual((hop['from'], hop['to']), (a, b))
                edge = by_edge_id[hop['edge']]
                self.assertIn(hop['traversal'], ('forward', 'backward'))
                actual_direction = ((a, b) if hop['traversal'] == 'forward' else (b, a))
                self.assertEqual((edge['from'], edge['to']), actual_direction)
                self.assertIn(actual_direction, follows)
                if mode == 'following':
                    self.assertEqual(hop['traversal'], 'forward')
                reciprocal = (a, b) in follows and (b, a) in follows
                self.assertEqual(hop['reciprocal'], reciprocal)
                reciprocal_hops += reciprocal
            self.assertEqual(path['reciprocal_hops'], reciprocal_hops)

        # Overlap is defined by recorded arrow patterns in either traversal mode.
        intermediates = set(nodes) - {source, target}
        common = {
            'shared_followees': {k for k in intermediates
                                if (source, k) in follows and (target, k) in follows},
            'shared_followers': {k for k in intermediates
                                if (k, source) in follows and (k, target) in follows},
            'forward_chain': {k for k in intermediates
                              if (source, k) in follows and (k, target) in follows},
            'reverse_chain': {k for k in intermediates
                              if (k, source) in follows and (target, k) in follows},
        }
        for kind, expected_accounts in common.items():
            actual_common = result['common_neighbors'][kind]
            self.assertEqual(actual_common['count'], len(expected_accounts))
            self.assertEqual(actual_common['displayed'], len(expected_accounts))
            self.assertEqual({account['id'] for account in actual_common['accounts']},
                             expected_accounts)
            self.assertFalse(actual_common['bounded'])

    def test_128_generated_graphs_every_endpoint_pair_both_modes(self):
        for sample in range(GRAPH_COUNT):
            conn = sqlite3.connect(':memory:')
            conn.row_factory = sqlite3.Row
            try:
                conn.executescript(db.SCHEMA)
                nodes, follows = generated_graph(sample)
                handles, evidence = write_fixture(conn, nodes, follows, sample)
                before = conn.total_changes
                for source, target in itertools.product(nodes, repeat=2):
                    for mode in ('all', 'following'):
                        with self.subTest(sample=sample, source=source, target=target, mode=mode):
                            # Exercise both public selector forms, with no private helpers.
                            source_selector = source if sample % 2 else handles[source]
                            target_selector = handles[target] if sample % 2 else target
                            result = connections.explore(conn, source_selector, target_selector,
                                                         mode=mode, limit=50)
                            self.assert_matches_oracle(result, nodes, follows, evidence,
                                                       source, target, mode)
                self.assertEqual(conn.total_changes, before)
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
