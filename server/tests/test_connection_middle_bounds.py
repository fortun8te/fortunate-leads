"""Middle-join regression cases with planted paths and an independent edge oracle."""

import math
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connections
import db


class ConnectionMiddleBoundsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.addCleanup(self.conn.close)
        self.people = {}
        self.true_arrows = set()

    def person(self, account_id):
        account_id = str(account_id)
        if account_id not in self.people:
            self.people[account_id] = db.upsert_person(
                self.conn, {'handle': 'fictional_' + account_id, 'ig_id': account_id},
                '2026-01-01')
        return account_id

    def observe(self, source, member, direction='following'):
        source, member = str(source), str(member)
        self.person(source)
        self.person(member)
        db.add_edge(self.conn, 'fictional_' + source, self.people[member], direction,
                    '2026-01-01', seed_ig_id=source, person_ig_id=member)
        arrow = (source, member) if direction == 'following' else (member, source)
        self.true_arrows.add(tuple('ig:' + item for item in arrow))

    def fanout(self, left_count, right_count=None):
        """Only planted cross-links can join these two disjoint endpoint fans."""
        if right_count is None:
            right_count = left_count
        start, target = self.person(100000), self.person(900000)
        left = [self.person(200000 + i) for i in range(left_count)]
        right = [self.person(600000 + i) for i in range(right_count)]
        for member in left:
            self.observe(start, member)
        for member in right:
            self.observe(target, member, 'followers')
        return start, target, left, right

    def explore(self, start, target, mode='following', **kwargs):
        return connections.explore(self.conn, 'ig:' + start, 'ig:' + target,
                                   mode=mode, **kwargs)

    def assert_true_arrows(self, result):
        """Check canonical arrows and derive their meaning from each original list."""
        edges = {edge['id']: edge for edge in result['edges']}
        for edge in edges.values():
            self.assertIn((edge['from'], edge['to']), self.true_arrows)
            self.assertTrue(edge['evidence'])
            for evidence in edge['evidence']:
                arrow = ('ig:' + evidence['seed_ig_id'], 'ig:' + evidence['person_ig_id'])
                if evidence['direction'] == 'followers':
                    arrow = arrow[::-1]
                self.assertEqual(arrow, (edge['from'], edge['to']))
        for path in result['paths']:
            self.assertEqual(len(set(path['nodes'])), len(path['nodes']))
            self.assertEqual(path['length'], len(path['nodes']) - 1)
            for hop in path['hops']:
                edge = edges[hop['edge']]
                traversal = (hop['from'], hop['to'])
                expected = traversal if hop['traversal'] == 'forward' else traversal[::-1]
                self.assertEqual((edge['from'], edge['to']), expected)
                if result['mode'] == 'following':
                    self.assertEqual(hop['traversal'], 'forward')

    def test_planted_path_crosses_both_batch_boundaries_in_either_observation_orientation(self):
        # The last pair belongs to the next batch on both axes at 401 and 801.
        for width in (399, 400, 401, 801):
            for reverse_storage in (False, True):
                with self.subTest(width=width, reverse_storage=reverse_storage):
                    self.setUp()
                    start, target, left, right = self.fanout(width)
                    if reverse_storage:
                        self.observe(right[-1], left[-1], 'followers')
                    else:
                        self.observe(left[-1], right[-1])
                    expected = [['ig:' + value for value in (start, left[-1], right[-1], target)]]
                    before = self.conn.total_changes
                    for mode in ('all', 'following'):
                        result = self.explore(start, target, mode)
                        self.assertEqual([path['nodes'] for path in result['paths']], expected)
                        self.assert_true_arrows(result)
                        self.assertFalse(result['search']['truncated'])
                        self.assertEqual(result['search']['observations_read'], width * 2 + 1)
                        self.assertLessEqual(result['search']['middle_queries'],
                                             2 * math.ceil(width / 400) ** 2)
                    self.assertEqual(self.conn.total_changes, before)

    def test_reverse_follow_at_far_corner_remains_backward(self):
        start, target, left, right = self.fanout(401)
        self.observe(right[-1], left[-1])  # Actual follow goes right -> left.
        result = self.explore(start, target, 'all')
        self.assertEqual([p['nodes'] for p in result['paths']],
                         [['ig:' + value for value in (start, left[-1], right[-1], target)]])
        self.assertEqual([hop['traversal'] for hop in result['paths'][0]['hops']],
                         ['forward', 'backward', 'forward'])
        self.assert_true_arrows(result)
        forward = self.explore(start, target)
        self.assertEqual(forward['paths'], [])
        self.assertFalse(forward['search']['search_truncated'])

    def test_budget_exhaustion_reports_missing_far_corner_and_exact_budget_finds_it(self):
        start, target, left, right = self.fanout(401)
        # Seven empty source/member batches precede this reverse-stored record.
        self.observe(right[-1], left[-1], 'followers')
        for budget in (0, 1, 4, 7, 8):
            with self.subTest(budget=budget), patch.object(connections, 'MIDDLE_QUERY_LIMIT', budget):
                result = self.explore(start, target)
                self.assertEqual(result['search']['middle_queries'], budget)
                self.assertEqual(result['search']['search_truncated'], budget < 8)
                self.assertFalse(result['search']['results_truncated'])
                self.assertEqual(result['search']['paths_found'], int(budget == 8))
                self.assertEqual(result['search']['observations_read'], 802 + int(budget == 8))
                if budget == 8:
                    self.assertEqual(result['paths'][0]['nodes'],
                                     ['ig:' + value for value in (start, left[-1], right[-1], target)])
                    self.assert_true_arrows(result)

    def test_both_lists_support_one_middle_arrow_without_duplicate_paths(self):
        start, target, left, right = self.fanout(401)
        for _ in range(2):
            self.observe(left[-1], right[-1])
            self.observe(right[-1], left[-1], 'followers')
        result = self.explore(start, target)
        self.assertEqual(result['search']['paths_found'], 1)
        self.assertEqual(result['search']['observations_read'], 804)
        middle = next(edge for edge in result['edges']
                      if edge['from'] == 'ig:' + left[-1] and edge['to'] == 'ig:' + right[-1])
        self.assertEqual(len(middle['evidence']), 2)
        self.assertFalse(middle['reciprocal'])
        self.assertFalse(result['search']['search_truncated'])
        self.assert_true_arrows(result)

        self.observe(right[-1], left[-1])  # A genuinely different, reverse follow.
        reciprocal = self.explore(start, target, 'all')
        self.assertEqual(reciprocal['search']['paths_found'], 1)
        self.assertEqual(len(reciprocal['edges']), 4)
        self.assertEqual(reciprocal['paths'][0]['reciprocal_hops'], 1)
        self.assert_true_arrows(reciprocal)

    def test_real_5000_row_ceiling_discards_extra_paths_and_reports_search_truncation(self):
        start, target, left, right = self.fanout(71)
        for source in left:
            for member in right:
                self.observe(source, member)
        # 5,041 cross-links each create exactly one possible three-hop path.
        result = self.explore(start, target, limit=50)
        self.assertEqual(result['search']['middle_edge_limit'], 5000)
        self.assertEqual(result['search']['observations_read'], 142 + 5000)
        self.assertEqual(result['search']['paths_found'], 5000)
        self.assertEqual(result['search']['paths_displayed'], 50)
        self.assertTrue(result['search']['search_truncated'])
        self.assertTrue(result['search']['results_truncated'])
        self.assertFalse(result['search']['paths_clipped'])
        self.assertEqual(result['paths'][0]['nodes'],
                         ['ig:' + value for value in (start, left[0], right[0], target)])
        self.assert_true_arrows(result)

    def test_middle_row_budget_applies_across_multiple_batches(self):
        start, target, left, right = self.fanout(401)
        # Four independent paths, one in every 400-ID source/member batch.
        planted = [(left[i], right[j]) for i in (0, 400) for j in (0, 400)]
        for source, member in planted:
            self.observe(source, member)
        with patch.object(connections, 'MIDDLE_EDGE_LIMIT', 3):
            result = self.explore(start, target, limit=50)
        self.assertEqual(result['search']['observations_read'], 802 + 3)
        self.assertEqual(result['search']['paths_found'], 3)
        self.assertTrue(result['search']['search_truncated'])
        self.assertFalse(result['search']['results_truncated'])
        expected = {tuple('ig:' + value for value in (start, a, b, target)) for a, b in planted}
        actual = {tuple(path['nodes']) for path in result['paths']}
        self.assertLess(actual, expected)
        self.assert_true_arrows(result)

    @unittest.expectedFailure
    def test_exact_5000_rows_without_additional_evidence_do_not_claim_search_truncation(self):
        # The graph is identical whichever endpoint list records each follow.
        # At present, reaching the cap in the first orientation skips the empty
        # reverse probe and incorrectly claims that this complete search clipped.
        for reverse_storage in (True, False):
            with self.subTest(reverse_storage=reverse_storage):
                self.setUp()
                start, target, left, right = self.fanout(100, 50)
                for source in left:
                    for member in right:
                        if reverse_storage:
                            self.observe(member, source, 'followers')
                        else:
                            self.observe(source, member)
                result = self.explore(start, target, limit=50)
                self.assertEqual(result['search']['observations_read'], 5150)
                self.assertEqual(result['search']['paths_found'], 5000)
                self.assertTrue(result['search']['results_truncated'])
                self.assertFalse(result['search']['paths_clipped'])
                self.assertFalse(result['search']['search_truncated'])
                self.assert_true_arrows(result)

    def test_overlapping_neighborhoods_do_not_duplicate_evidence_or_routes(self):
        start, target = self.person(100000), self.person(900000)
        first, second = self.person(200000), self.person(200001)
        for member in (first, second):
            self.observe(start, member)
            self.observe(target, member, 'followers')
        self.observe(first, second)
        # The same observation matches both middle passes because both accounts
        # occur in both endpoint neighborhoods. It still supports one arrow.
        result = self.explore(start, target)
        expected = {(start, first, target), (start, second, target), (start, first, second, target)}
        self.assertEqual({tuple(path['nodes']) for path in result['paths']},
                         {tuple('ig:' + value for value in path) for path in expected})
        self.assertEqual(len(result['edges']), 5)
        self.assertTrue(all(len(edge['evidence']) == 1 for edge in result['edges']))
        self.assert_true_arrows(result)


if __name__ == '__main__':
    unittest.main()
