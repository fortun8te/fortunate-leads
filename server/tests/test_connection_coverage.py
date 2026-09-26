"""Coverage is collection metadata, not proof of a current or complete graph.

All fixtures are fictional in-memory databases. Exercise the public exploration
result rather than its private coverage implementation.
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connections
import db


class ConnectionCoverageTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.addCleanup(self.conn.close)

    def person(self, handle, ig):
        return db.upsert_person(self.conn, {'handle': handle, 'ig_id': ig}, '2020-01-01')

    def seed(self, handle, ig):
        self.conn.execute('INSERT INTO seeds VALUES(?,?,0,?)', (handle, ig, '2020-01-01'))

    def observation(self, handle, source_ig, member, direction='following', at='2020-01-01'):
        member_ig = self.conn.execute('SELECT ig_id FROM people WHERE id=?', (member,)).fetchone()[0]
        db.add_edge(self.conn, handle, member, direction, at,
                    seed_ig_id=source_ig, person_ig_id=member_ig)

    def collection(self, handle, direction, state, captured, total, error=None, at='2026-09-26'):
        self.conn.execute(
            'INSERT OR REPLACE INTO lists(seed,direction,state,received,total,error,updated_at) '
            'VALUES(?,?,?,?,?,?,?)', (handle, direction, state, captured, total, error, at))

    def pair(self):
        self.seed('start', '1')
        self.seed('target', '2')

    def row(self, result, account, direction, seed=None):
        rows = [row for row in result['coverage'] if row['account'] == account
                and row['direction'] == direction and (seed is None or row['seed'] == seed)]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def assert_cautious(self, result):
        # These are substantive API disclosures, not a ban on the words appearing
        # in a warning that explicitly rejects the stronger claim.
        self.assertIn('Missing paths and lists do not prove absence or an unfollow', result['interpretation'])
        self.assertIn('ended lists do not establish a complete or current graph', result['interpretation'])
        self.assertIn('path hops may not have coexisted', result['interpretation'])
        self.assertTrue(result['search']['bounded'])
        self.assertEqual(result['search']['max_hops'], 3)
        for row in result['coverage']:
            self.assertNotIn(row['status'], ('complete', 'current', 'absent', 'unfollowed'))
            self.assertIn('not historical identity or freshness proof', row['metadata_scope'])
            for claim in ('complete', 'current', 'fresh', 'absence_confirmed'):
                self.assertIsNot(row.get(claim), True)

    def test_uncollected_non_source_endpoints_report_both_directions(self):
        self.person('start', '1')
        self.person('target', '2')
        result = connections.explore(self.conn, 'start', 'target')
        self.assertEqual(result['paths'], [])
        self.assertEqual(result['direct_edges'], [])
        self.assertEqual(len(result['coverage']), 4)
        for account in ('ig:1', 'ig:2'):
            for direction in ('followers', 'following'):
                row = self.row(result, account, direction)
                self.assertEqual(row['status'], 'missing')
                self.assertEqual(row['captured'], 0)
                self.assertIsNone(row['total'])
                self.assertIsNone(row['state'])
                self.assertIsNone(row['updated_at'])
                self.assertEqual(row['count_relation'], 'unknown')
        self.assert_cautious(result)

    def test_collection_state_and_count_matrix_in_both_directions(self):
        self.pair()
        cases = [
            ('queued', 0, None, None, 'partial', 'unknown'),
            ('running', 2, 8, None, 'partial', 'below'),
            ('running', 8, 8, None, 'partial', 'equal'),
            ('paused', 2, 8, None, 'partial', 'below'),
            ('error', 2, 8, 'Temporary collection failure', 'partial', 'below'),
            ('error', 8, 8, 'Temporary collection failure', 'partial', 'equal'),
            ('done', 2, None, None, 'ended_unknown', 'unknown'),
            ('done', 0, None, None, 'ended_unknown', 'unknown'),
            ('done', 2, 8, None, 'ended_short', 'below'),
            ('done', 0, 8, None, 'ended_short', 'below'),
            ('done', 8, 8, None, 'ended_count_matched', 'equal'),
            ('done', 9, 8, None, 'ended_unknown', 'above'),
            ('done', 0, 0, None, 'ended_count_matched', 'equal'),
            ('done', 1, 0, None, 'ended_unknown', 'above'),
            ('done', 8, 8, 'Prior collection failure retained', 'ended_count_matched', 'equal'),
        ]
        for direction in ('followers', 'following'):
            other = 'following' if direction == 'followers' else 'followers'
            for state, captured, total, error, expected_status, relation in cases:
                with self.subTest(direction=direction, state=state, captured=captured, total=total, error=error):
                    self.conn.execute('DELETE FROM lists')
                    self.collection('start', direction, state, captured, total, error)
                    result = connections.explore(self.conn, 'start', 'target')
                    row = self.row(result, 'ig:1', direction)
                    self.assertEqual(row['state'], state)
                    self.assertEqual(row['status'], expected_status)
                    self.assertEqual(row['captured'], captured)
                    self.assertEqual(row['total'], total)
                    self.assertEqual(row['count_relation'], relation)
                    self.assertEqual(row['error'], error)
                    self.assertEqual(self.row(result, 'ig:1', other)['status'], 'missing')
                    self.assert_cautious(result)

    def test_followers_and_following_keep_independent_counts_and_errors(self):
        self.pair()
        self.collection('start', 'followers', 'error', 3, 8, 'Collector stopped')
        self.collection('start', 'following', 'done', 12, 12)
        self.collection('target', 'followers', 'running', 4, 20)
        result = connections.explore(self.conn, 'start', 'target')
        expected = {
            ('ig:1', 'followers'): ('partial', 3, 8, 'Collector stopped'),
            ('ig:1', 'following'): ('ended_count_matched', 12, 12, None),
            ('ig:2', 'followers'): ('partial', 4, 20, None),
            ('ig:2', 'following'): ('missing', 0, None, None),
        }
        self.assertEqual(len(result['coverage']), len(expected))
        for (account, direction), values in expected.items():
            row = self.row(result, account, direction)
            self.assertEqual(tuple(row[key] for key in ('status', 'captured', 'total', 'error')), values)
        self.assert_cautious(result)

    def test_matched_empty_lists_do_not_erase_old_evidence_or_assert_absence(self):
        self.pair()
        target = self.person('target', '2')
        self.observation('start', '1', target)
        before = connections.explore(self.conn, 'start', 'target')
        for seed in ('start', 'target'):
            for direction in ('followers', 'following'):
                self.collection(seed, direction, 'done', 0, 0)
        after = connections.explore(self.conn, 'start', 'target')
        self.assertEqual(after['paths'], before['paths'])
        self.assertEqual(after['edges'], before['edges'])
        self.assertEqual(after['paths'][0]['nodes'], ['ig:1', 'ig:2'])
        self.assertTrue(all(row['status'] == 'ended_count_matched' for row in after['coverage']))
        self.assert_cautious(after)

    def test_matched_counts_do_not_create_an_unrecorded_follow(self):
        self.pair()
        for seed in ('start', 'target'):
            for direction in ('followers', 'following'):
                self.collection(seed, direction, 'done', 8, 8)
        for mode in ('all', 'following'):
            with self.subTest(mode=mode):
                result = connections.explore(self.conn, 'start', 'target', mode)
                self.assertEqual(result['paths'], [])
                self.assertEqual(result['edges'], [])
                self.assertTrue(all(kind['count'] == 0 for kind in result['common_neighbors'].values()))
                self.assert_cautious(result)

    def test_coverage_contains_endpoints_and_every_displayed_intermediate(self):
        # start -> first <- second -> target. first/target have never been sources.
        self.seed('start', '1')
        self.seed('second', '3')
        self.seed('unrelated', '9')
        first, target = self.person('first', '2'), self.person('target', '4')
        self.observation('start', '1', first)
        self.observation('second', '3', first)
        self.observation('second', '3', target)
        self.collection('start', 'following', 'done', 1, 1)
        self.collection('second', 'following', 'running', 2, 6)
        self.collection('unrelated', 'followers', 'done', 99, 99)
        result = connections.explore(self.conn, 'start', 'target')
        self.assertEqual(result['paths'][0]['nodes'], ['ig:1', 'ig:2', 'ig:3', 'ig:4'])
        expected_accounts = {'ig:1', 'ig:2', 'ig:3', 'ig:4'}
        self.assertEqual({row['account'] for row in result['coverage']}, expected_accounts)
        self.assertEqual(len(result['coverage']), 8)
        for account in expected_accounts:
            self.assertEqual({row['direction'] for row in result['coverage'] if row['account'] == account},
                             {'followers', 'following'})
        for account in ('ig:2', 'ig:4'):
            self.assertTrue(all(row['status'] == 'missing' for row in result['coverage'] if row['account'] == account))
        directed = connections.explore(self.conn, 'start', 'target', 'following')
        self.assertEqual(directed['paths'], [])
        self.assertEqual({row['account'] for row in directed['coverage']}, {'ig:1', 'ig:4'})
        self.assert_cautious(result)
        self.assert_cautious(directed)

    def test_display_limit_coverage_is_for_returned_paths_not_all_candidates(self):
        self.pair()
        for handle, ig in (('first', '3'), ('second', '4')):
            member = self.person(handle, ig)
            self.observation('start', '1', member)
            self.observation('target', '2', member)
        all_paths = connections.explore(self.conn, 'start', 'target')
        displayed = connections.explore(self.conn, 'start', 'target', limit=1)
        expected_accounts = {'ig:1', 'ig:2', *displayed['paths'][0]['nodes']}
        self.assertEqual(all_paths['search']['paths_found'], 2)
        self.assertEqual(displayed['search']['paths_found'], 2)
        self.assertTrue(displayed['search']['results_truncated'])
        self.assertFalse(displayed['search']['search_truncated'])
        self.assertEqual({row['account'] for row in displayed['coverage']}, expected_accounts)
        self.assertEqual(len(displayed['coverage']), 6)
        self.assertEqual(len(all_paths['coverage']), 8)
        self.assert_cautious(displayed)

    def test_same_account_has_one_pair_of_coverage_rows(self):
        self.seed('start', '1')
        self.person('renamed', '1')
        self.collection('start', 'following', 'done', 2, 2)
        result = connections.explore(self.conn, 'seed:start', 'renamed')
        self.assertEqual(result['source']['id'], result['target']['id'])
        self.assertEqual(result['paths'], [])
        self.assertEqual(len(result['coverage']), 2)
        self.assertEqual(self.row(result, 'ig:1', 'following')['captured'], 2)
        self.assert_cautious(result)

    def test_recorded_source_alias_and_current_alias_keep_separate_list_metadata(self):
        self.seed('new_name', '1')
        member = self.person('target', '2')
        self.observation('old_name', '1', member)
        self.collection('old_name', 'following', 'done', 2, 5)
        self.collection('new_name', 'following', 'running', 8, 20)
        result = connections.explore(self.conn, 'new_name', 'target')
        old = self.row(result, 'ig:1', 'following', 'old_name')
        new = self.row(result, 'ig:1', 'following', 'new_name')
        self.assertEqual((old['status'], old['captured'], old['total']), ('ended_short', 2, 5))
        self.assertEqual((new['status'], new['captured'], new['total']), ('partial', 8, 20))
        self.assertEqual({row['seed'] for row in result['coverage'] if row['account'] == 'ig:1'},
                         {'new_name', 'old_name'})
        self.assertEqual(result['edges'][0]['evidence'][0]['seed'], 'old_name')
        self.assert_cautious(result)

    def test_reused_handle_does_not_merge_historical_and_current_owners(self):
        self.seed('reused', '99')
        member = self.person('bridge', '2')
        self.observation('reused', '77', member)
        self.observation('reused', '99', member)
        self.collection('reused', 'following', 'done', 10, 10)
        result = connections.explore(self.conn, 'ig:77', 'ig:99')
        self.assertEqual(result['paths'][0]['nodes'], ['ig:77', 'ig:2', 'ig:99'])
        self.assertEqual({row['account'] for row in result['coverage']}, {'ig:77', 'ig:2', 'ig:99'})
        for account in ('ig:77', 'ig:99'):
            row = self.row(result, account, 'following', 'reused')
            # Counts still use the current handle-keyed row. The public warning is
            # essential until list runs are bound to captured account identities.
            self.assertIn('keyed by handle', row['metadata_scope'])
            self.assertIn('not historical identity', row['metadata_scope'])
        self.assert_cautious(result)

    def test_unrelated_handle_owner_cannot_borrow_current_seed_coverage(self):
        self.seed('reused', '99')
        self.person('reused', '77')
        self.person('target', '2')
        self.collection('reused', 'following', 'done', 10, 10)
        result = connections.explore(self.conn, 'reused', 'target')
        self.assertEqual(result['source']['id'], 'ig:77')
        for direction in ('followers', 'following'):
            row = self.row(result, 'ig:77', direction)
            self.assertEqual(row['status'], 'missing')
            self.assertEqual(row['captured'], 0)
            self.assertIsNone(row['total'])
            self.assertIsNone(row['updated_at'])
        self.assert_cautious(result)

    def test_unknown_historical_source_coverage_remains_explicitly_unverified(self):
        self.seed('reused', '99')
        member = self.person('target', '2')
        self.observation('reused', None, member)
        self.collection('reused', 'following', 'done', 1, 1)
        result = connections.explore(self.conn, 'legacy:reused', 'target')
        self.assertEqual(result['source']['identity_basis'], 'legacy_handle')
        self.assertEqual(result['paths'][0]['nodes'], ['legacy:reused', 'ig:2'])
        for direction in ('followers', 'following'):
            row = self.row(result, 'legacy:reused', direction)
            self.assertEqual(row['identity_basis'], 'legacy_handle')
        self.assertNotIn('ig:99', {row['account'] for row in result['coverage']})
        self.assert_cautious(result)

    def test_recent_list_updates_do_not_refresh_edge_recording_dates(self):
        self.pair()
        member = self.person('target', '2')
        self.observation('start', '1', member, at='2020-01-01')
        before = connections.explore(self.conn, 'start', 'target')
        for state, at in (('queued', '2026-09-24'), ('running', '2026-09-25'), ('done', '2026-09-26')):
            with self.subTest(state=state):
                self.collection('start', 'following', state, 1, 1, at=at)
                changes_before = self.conn.total_changes
                result = connections.explore(self.conn, 'start', 'target')
                self.assertEqual(self.conn.total_changes, changes_before)
                self.assertEqual(result['edges'], before['edges'])
                self.assertEqual(self.row(result, 'ig:1', 'following')['updated_at'], at)
                evidence = result['edges'][0]['evidence'][0]
                self.assertEqual(evidence['first_seen'], '2020-01-01')
                self.assertEqual(evidence['last_seen'], '2020-01-01')
                self.assertIn('List updated_at is list metadata, not an edge observation date', result['interpretation'])
                self.assert_cautious(result)

    def test_unknown_last_recording_is_not_filled_from_list_metadata(self):
        self.pair()
        member = self.person('target', '2')
        self.observation('start', '1', member)
        self.conn.execute('UPDATE edge_observations SET last_seen=NULL')
        self.collection('start', 'following', 'done', 1, 1, at='2026-09-26')
        result = connections.explore(self.conn, 'start', 'target')
        evidence = result['edges'][0]['evidence'][0]
        self.assertIsNone(evidence['last_seen'])
        self.assertEqual(evidence['first_seen'], '2020-01-01')
        self.assertEqual(self.row(result, 'ig:1', 'following')['updated_at'], '2026-09-26')
        self.assert_cautious(result)


if __name__ == '__main__':
    unittest.main()
