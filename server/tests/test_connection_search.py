"""Account-search contract tests using fictional, in-memory imports only."""

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connections
import db


class ConnectionSearchTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.addCleanup(self.conn.close)

    def person(self, handle, ig=None, **fields):
        return db.upsert_person(self.conn, dict(handle=handle, ig_id=ig, **fields), '2020-01-01')

    def seed(self, handle, ig=None, me=False):
        self.conn.execute('INSERT INTO seeds(handle,ig_id,is_me) VALUES(?,?,?)', (handle, ig, me))

    def observed(self, handle, source_id, pid, member_id=None, direction='following'):
        db.add_edge(self.conn, handle, pid, direction, '2020-01-01',
                    seed_ig_id=source_id, person_ig_id=member_id)

    def ids(self, query, limit=50):
        return [a['id'] for a in connections.search(self.conn, query, limit=limit)['accounts']]

    def test_imported_people_remain_searchable_without_edges_or_lead_qualification(self):
        expected = []
        for n in range(40):
            pid = self.person(f'catalog_{n:02}', str(1000 + n),
                              is_private=n % 2, bio=None if n % 3 else 'Fictional bio')
            expected.append(f'ig:{1000 + n}')
            self.conn.execute('INSERT INTO verdicts(person_id,prefilter,score,tier) VALUES(?,?,?,?)',
                              (pid, 0, 0, 'reject'))
            self.conn.execute('INSERT INTO marks(person_id,status) VALUES(?,?)', (pid, 'rejected'))
            self.conn.execute('INSERT INTO tags(person_id,tag,grp,source) VALUES(?,?,?,?)',
                              (pid, 'excluded_fixture', 'manual', 'manual'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM edges').fetchone()[0], 0)
        before = connections.search(self.conn, 'catalog_', limit=50)
        self.assertEqual([a['id'] for a in before['accounts']], expected)
        self.assertFalse(before['truncated'])

        self.conn.execute("UPDATE verdicts SET prefilter=100,score=100,tier='A'")
        self.conn.execute("UPDATE marks SET status='client'")
        self.conn.execute('DELETE FROM tags')
        db.set_setting(self.conn, 'bio_min', 100)
        self.assertEqual(connections.search(self.conn, 'catalog_', limit=50), before)

    def test_own_accounts_sort_first_then_case_insensitive_handle_and_id(self):
        self.seed('order_zulu', '9', me=True)
        self.seed('order_mike', '8', me=True)
        self.seed('order_beta', '2')
        self.person('order_alpha', '1')
        self.person('order_zulu', '9')
        expected = ['ig:8', 'ig:9', 'ig:1', 'ig:2']
        self.assertEqual(self.ids('ORDER_'), expected)
        result = connections.search(self.conn, 'order_', limit=2)
        self.assertEqual([a['id'] for a in result['accounts']], expected[:2])
        self.assertTrue(result['truncated'])
        self.assertTrue(all(a['is_me'] for a in result['accounts']))

    def test_people_sources_and_historical_aliases_deduplicate_by_account_id(self):
        self.seed('match_old_alias', '10', me=True)
        self.seed('match_other_alias', '10')
        self.person('match_current', '10')
        member = self.person('endpoint', '90')
        for alias in ('match_old_alias', 'match_other_alias'):
            for direction in ('following', 'followers'):
                self.observed(alias, '10', member, '90', direction)
        result = connections.search(self.conn, 'match_', limit=1)
        self.assertEqual([a['id'] for a in result['accounts']], ['ig:10'])
        account = result['accounts'][0]
        self.assertEqual(account['handle'], 'match_current')
        self.assertTrue(account['is_me'])
        self.assertTrue(account['is_seed'])

    def test_reused_source_handle_exposes_all_captured_owners_and_unknown_variant(self):
        self.seed('reused', '300', me=True)
        member = self.person('member', '900')
        for source_id in ('100', '200', None):
            for direction in ('following', 'followers'):
                self.observed('reused', source_id, member, '900', direction)
        result = connections.search(self.conn, 'reu', limit=50)
        self.assertEqual([a['id'] for a in result['accounts']],
                         ['ig:300', 'ig:100', 'ig:200', 'legacy:reused'])
        self.assertEqual([a['is_me'] for a in result['accounts']], [True, False, False, False])
        self.assertEqual(result['accounts'][-1]['identity_basis'], 'legacy_handle')
        self.assertFalse(result['truncated'])

    def test_unidentified_member_variant_survives_later_enrichment(self):
        member = self.person('member_old')
        self.observed('source', '1', member)
        self.person('member_old', '20')
        self.person('member_current', '20')
        self.assertEqual(set(self.ids('member_current')), {'ig:20', f'legacy-person:{member}'})
        legacy = next(a for a in connections.search(self.conn, 'member_current')['accounts']
                      if a['id'].startswith('legacy-person:'))
        self.assertEqual(legacy['identity_basis'], 'legacy_person')
        self.assertIsNone(legacy['ig_id'])
        # Older member handles are deliberately not indexed, as documented.
        self.assertEqual(self.ids('member_old'), [])

    def test_local_member_and_unknown_captured_member_are_distinct_endpoints(self):
        member = self.person('unidentified')
        self.observed('source', '1', member)
        self.assertEqual(set(self.ids('unidentified')),
                         {f'person:{member}', f'legacy-person:{member}'})
        for account in connections.search(self.conn, 'unidentified')['accounts']:
            result = connections.explore(self.conn, 'ig:1', account['id'])
            self.assertEqual(result['target']['id'], account['id'])
            self.assertEqual(len(result['paths']), int(account['id'].startswith('legacy-person:')))

    def test_every_returned_selector_round_trips_in_both_endpoint_positions(self):
        self.seed('select_own', '1', me=True)
        self.seed('select_seed_only')
        self.person('select_person', '2')
        local = self.person('select_local')
        self.seed('select_local')
        self.observed('select_historical', '3', local)
        self.observed('select_legacy_source', None, local)
        self.person('select_reused', '4')
        self.person('select_reused', '5')  # Retains account 4 under a parked handle.
        expected = {'ig:1', 'ig:2', 'ig:3', 'ig:4', 'ig:5', 'seed:select_seed_only',
                    f'person:{local}', f'legacy-person:{local}', 'legacy:select_legacy_source'}
        accounts = connections.search(self.conn, 'select_', limit=50)['accounts']
        self.assertEqual({a['id'] for a in accounts}, expected)
        self.assertEqual(len(accounts), len(expected))
        for account in accounts:
            for mode in ('all', 'following'):
                with self.subTest(selector=account['id'], mode=mode):
                    forward = connections.explore(self.conn, account['id'], 'ig:1', mode)
                    backward = connections.explore(self.conn, 'ig:1', account['id'], mode)
                    same = connections.explore(self.conn, account['id'], account['id'], mode)
                    self.assertEqual(forward['source']['id'], account['id'])
                    self.assertEqual(backward['target']['id'], account['id'])
                    self.assertEqual(same['source']['id'], account['id'])
                    self.assertEqual(same['target']['id'], account['id'])
                    self.assertEqual(same['paths'], [])

    def test_prefix_punctuation_is_literal_and_never_a_wildcard(self):
        handles = ['prefix_a', 'prefix_a.b', 'prefix_ab', 'prefix_a_b',
                   'prefix_axb', 'prefixa', 'prefixz', '_prefix', '.prefix']
        expected_by_handle = {handle: f'ig:{n}' for n, handle in enumerate(handles, 1)}
        for handle, selector in expected_by_handle.items():
            self.person(handle, selector[3:])
        # This oracle uses literal Python prefixes, independent of SQLite matching.
        for query in ('prefix_', 'prefix_a_', 'prefix_a.', '_', '.', 'prefix_a', 'prefixaz'):
            with self.subTest(query=query):
                expected = [expected_by_handle[h] for h in sorted(handles) if h.startswith(query)]
                self.assertEqual(self.ids(query), expected)

    def test_current_and_captured_source_prefixes_are_case_insensitive(self):
        self.person('mixed_member', '1')
        self.seed('MiXeD_Source', '2')
        member = self.person('anchor', '90')
        self.observed('MiXeD_Historical', '3', member, '90')
        expected = ['ig:3', 'ig:1', 'ig:2']
        for query in ('mixed_', 'MIXED_', '  @MiXeD_  ', 'https://www.instagram.com/MIXED_/?x=1'):
            with self.subTest(query=query):
                self.assertEqual(self.ids(query), expected)

    def test_source_only_prefixes_use_literal_punctuation(self):
        member = self.person('anchor', '90')
        handles = ['source_a.b', 'source_a_b', 'source_axb', '_source', '.source']
        expected_by_handle = {handle: f'ig:{n}' for n, handle in enumerate(handles, 1)}
        for handle, selector in expected_by_handle.items():
            self.observed(handle, selector[3:], member, '90')
        for query in ('source_a.', 'source_a_', '_', '.', 'source_'):
            with self.subTest(query=query):
                expected = [expected_by_handle[h] for h in sorted(handles) if h.startswith(query)]
                self.assertEqual(self.ids(query), expected)

    def test_empty_query_suggests_current_and_historical_sources_with_own_first(self):
        self.seed('z_own', '1', me=True)
        self.seed('b_source')
        member = self.person('ordinary_member', '90')
        self.observed('a_historical', '2', member, '90')
        self.observed('c_unidentified_source', None, member, '90')
        expected = ['ig:1', 'ig:2', 'seed:b_source', 'legacy:c_unidentified_source']
        for query in ('', '   '):
            result = connections.search(self.conn, query)
            self.assertEqual([a['id'] for a in result['accounts']], expected)
            self.assertFalse(result['truncated'])

    def test_invalid_search_wildcards_and_nontext_queries_fail_without_side_effects(self):
        self.person('safe', '1')
        invalid = ('%', 'safe%', 'safe\\_', 'safe*', "safe' OR 1=1 --",
                   'safe/name', 'https://example.org/safe', None, 123, [], {})
        for query in invalid:
            with self.subTest(query=query):
                with self.assertRaises(ValueError):
                    connections.search(self.conn, query)
        self.assertEqual(self.ids('safe'), ['ig:1'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM people').fetchone()[0], 1)

    def test_result_limit_and_truncation_count_unique_people(self):
        for n in range(51):
            self.person(f'limit_{n:02}', str(100 + n))
        default = connections.search(self.conn, 'limit_')
        self.assertEqual(default['limit'], 12)
        self.assertEqual(len(default['accounts']), 12)
        self.assertTrue(default['truncated'])
        largest = connections.search(self.conn, 'limit_', limit=50)
        self.assertEqual(len(largest['accounts']), 50)
        self.assertTrue(largest['truncated'])
        exact = connections.search(self.conn, 'limit_0', limit=10)
        self.assertEqual(len(exact['accounts']), 10)
        self.assertFalse(exact['truncated'])
        self.assertEqual(connections.search(self.conn, 'missing', limit=1),
                         {'accounts': [], 'truncated': False, 'limit': 1})

    def test_invalid_result_limits_are_rejected(self):
        for limit in (0, -1, 51, True, False, 1.0, '1', None):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    connections.search(self.conn, '', limit=limit)

    def add_duplicate_source_alias_fixture(self):
        member = self.person('target', '90')
        for alias, source_id in [('alias_a', '1'), ('alias_b', '1'),
                                 ('alias_c', '1'), ('alias_d', '2')]:
            self.observed(alias, source_id, member, '90')

    def test_alias_duplicates_do_not_hide_a_distinct_source_before_result_limit(self):
        self.add_duplicate_source_alias_fixture()
        result = connections.search(self.conn, 'alias', limit=2)
        self.assertEqual([a['id'] for a in result['accounts']], ['ig:1', 'ig:2'])
        self.assertFalse(result['truncated'])

    def test_alias_duplicates_do_not_falsely_report_more_matching_accounts(self):
        self.add_duplicate_source_alias_fixture()
        result = connections.search(self.conn, 'alias', limit=3)
        self.assertEqual([a['id'] for a in result['accounts']], ['ig:1', 'ig:2'])
        self.assertFalse(result['truncated'])


if __name__ == '__main__':
    unittest.main()
