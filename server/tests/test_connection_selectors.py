"""User-visible selector and search-selection behavior, using fictional in-memory data."""

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connections
import db


class ConnectionSelectorTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.addCleanup(self.conn.close)
        self.alice = self.person('fictional_alice', '101')
        self.target = self.person('fictional_target', '900')
        self.seed('fictional_alice', '101', own=True)
        self.observation('fictional_alice', self.target, '101', '900')

    def person(self, handle, ig=None):
        return db.upsert_person(self.conn, {'handle': handle, 'ig_id': ig}, '2020-01-01')

    def seed(self, handle, ig=None, own=False):
        self.conn.execute('INSERT INTO seeds VALUES(?,?,?,?)', (handle, ig, own, '2020-01-01'))

    def observation(self, seed, pid, source_id=None, member_id=None):
        db.add_edge(self.conn, seed, pid, 'following', '2020-01-01',
                    seed_ig_id=source_id, person_ig_id=member_id)

    def assert_selectable(self, account):
        # A clicked search result is submitted by its returned ID, not its label.
        result = connections.explore(self.conn, account['id'], 'ig:900')
        self.assertEqual(result['source']['id'], account['id'])
        self.assertEqual(result['source']['identity_basis'], account['identity_basis'])
        self.assertEqual(result['source']['ig_id'], account['ig_id'])
        return result

    def test_handle_id_and_profile_url_aliases_find_the_same_recorded_path(self):
        expected = connections.explore(self.conn, 'ig:101', 'ig:900')
        selectors = (
            'fictional_alice', '@fictional_alice', 'FiCtIoNaL_AlIcE',
            ' \t@FICTIONAL_ALICE\n', 'ig:101', '  ig:101  ',
            f'person:{self.alice}', f' person:{self.alice}\t',
            'seed:fictional_alice', 'seed:@FICTIONAL_ALICE',
            'https://instagram.com/fictional_alice',
            'https://www.instagram.com/FICTIONAL_ALICE/',
            ' HTTPS://WWW.INSTAGRAM.COM/fictional_alice/?utm_source=fixture#profile ',
        )
        for selector in selectors:
            with self.subTest(selector=selector):
                actual = connections.explore(self.conn, selector, '@FICTIONAL_TARGET')
                self.assertEqual(actual['source']['id'], 'ig:101')
                self.assertEqual(actual['target']['id'], 'ig:900')
                self.assertEqual(actual['paths'], expected['paths'])
                self.assertEqual(actual['edges'], expected['edges'])

    def test_normalized_search_queries_return_ids_that_explore_consistently(self):
        for query in ('fictional_ali', '@FICTIONAL_ALI', ' \tFiCtIoNaL_AlI\n',
                      'https://www.instagram.com/FICTIONAL_ALICE/?source=fixture'):
            with self.subTest(query=query):
                accounts = connections.search(self.conn, query)['accounts']
                self.assertEqual([a['id'] for a in accounts], ['ig:101'])
                self.assertEqual(self.assert_selectable(accounts[0])['paths'][0]['length'], 1)

    def test_search_selection_preserves_current_old_and_unidentified_source_owners(self):
        self.seed('fictional_reused', '202')
        self.person('fictional_reused', '303')
        self.observation('fictional_reused', self.target, '404', '900')
        self.observation('fictional_reused', self.target, None, '900')
        found = connections.search(self.conn, 'fictional_reused')['accounts']
        self.assertEqual({a['id'] for a in found},
                         {'ig:202', 'ig:303', 'ig:404', 'legacy:fictional_reused'})
        for account in found:
            with self.subTest(account=account['id']):
                result = self.assert_selectable(account)
                self.assertEqual(bool(result['paths']),
                                 account['id'] in {'ig:404', 'legacy:fictional_reused'})
        by_seed = connections.explore(self.conn, 'seed:FICTIONAL_REUSED', 'fictional_reused')
        self.assertEqual(by_seed['source']['id'], 'ig:202')
        self.assertEqual(by_seed['target']['id'], 'ig:303')
        self.assertEqual(connections.explore(self.conn, 'legacy:@FICTIONAL_REUSED',
                                            'ig:900')['source']['id'], 'legacy:fictional_reused')

    def test_legacy_member_selection_remains_separate_after_later_enrichment(self):
        pid = self.person('fictional_later')
        self.observation('fictional_alice', pid, '101', None)
        self.person('fictional_later', '505')
        accounts = connections.search(self.conn, 'fictional_later')['accounts']
        self.assertEqual({a['id'] for a in accounts}, {'ig:505', f'legacy-person:{pid}'})
        for account in accounts:
            with self.subTest(account=account['id']):
                self.assert_selectable(account)
                result = connections.explore(self.conn, 'ig:101', account['id'])
                self.assertEqual(bool(result['paths']), account['id'] == f'legacy-person:{pid}')
        current = connections.explore(self.conn, f'person:{pid}', 'ig:900')
        self.assertEqual(current['source']['id'], 'ig:505')

    def test_all_supported_canonical_kinds_survive_search_selection(self):
        self.person('fictional_local')
        self.seed('fictional_seedonly')
        pid = self.person('fictional_unknown')
        self.observation('fictional_legacy', pid)
        prefixes = set()
        for query in ('fictional_alice', 'fictional_local', 'fictional_seedonly',
                      'fictional_unknown', 'fictional_legacy'):
            accounts = connections.search(self.conn, query)['accounts']
            self.assertTrue(accounts, query)
            for account in accounts:
                with self.subTest(query=query, account=account['id']):
                    prefixes.add(account['id'].split(':', 1)[0])
                    self.assert_selectable(account)
        self.assertEqual(prefixes, {'ig', 'person', 'seed', 'legacy', 'legacy-person'})

    def test_source_search_alias_keeps_identity_after_source_renames(self):
        self.seed('fictional_old_name', '606')
        self.person('fictional_new_name', '606')
        self.observation('fictional_old_name', self.target, '606', '900')
        for query in ('fictional_old_name', 'fictional_new_name'):
            with self.subTest(query=query):
                accounts = connections.search(self.conn, query)['accounts']
                self.assertEqual([a['id'] for a in accounts], ['ig:606'])
                result = self.assert_selectable(accounts[0])
                self.assertEqual(result['source']['handle'], 'fictional_new_name')
                self.assertEqual(result['paths'][0]['nodes'], ['ig:606', 'ig:900'])

    def test_historical_member_id_keeps_captured_identity_after_row_changes(self):
        pid = self.person('fictional_before', '707')
        self.observation('fictional_alice', pid, '101', '707')
        self.conn.execute('UPDATE people SET handle=?,ig_id=? WHERE id=?',
                          ('fictional_after', '808', pid))
        historical = connections.explore(self.conn, 'ig:101', 'ig:707')
        current = connections.explore(self.conn, 'ig:101', f'person:{pid}')
        self.assertEqual(historical['target']['ig_id'], '707')
        self.assertNotEqual(historical['target']['handle'], 'fictional_after')
        self.assertEqual(historical['paths'][0]['nodes'], ['ig:101', 'ig:707'])
        self.assertEqual(current['target']['id'], 'ig:808')
        self.assertEqual(current['paths'], [])

    def test_numerical_handle_is_distinct_from_an_explicit_instagram_id(self):
        self.person('101', '909')
        result = connections.explore(self.conn, '@101', 'ig:101')
        self.assertEqual((result['source']['id'], result['target']['id']), ('ig:909', 'ig:101'))
        self.assertEqual([a['id'] for a in connections.search(self.conn, '101')['accounts']], ['ig:909'])

    def test_search_uses_literal_handle_prefixes(self):
        wanted = self.person('fictional_a.b_1', '111')
        self.person('fictional_axb_1', '112')
        self.person('fictional_a.bx1', '113')
        accounts = connections.search(self.conn, '@FICTIONAL_A.B_')['accounts']
        self.assertEqual([a['person_id'] for a in accounts], [wanted])
        self.assert_selectable(accounts[0])

    def test_malformed_numeric_selectors_are_rejected_before_account_lookup(self):
        malformed = ('', '-1', '+1', '1.0', '1e2', '1 01', ' 101', '１０１', '١٠١',
                     '101\x00', '101/1', "1 OR 1=1")
        for prefix, max_digits in (('ig:', 32), ('person:', 18), ('legacy-person:', 18)):
            for suffix in (*malformed, '9' * (max_digits + 1)):
                with self.subTest(selector=prefix + suffix):
                    with self.assertRaises(ValueError):
                        connections.explore(self.conn, prefix + suffix, 'ig:900')

    def test_valid_but_unknown_selectors_are_missing_not_malformed(self):
        for selector in ('fictional_absent', '@FICTIONAL_ABSENT',
                         'https://instagram.com/fictional_absent/',
                         'ig:999999', 'ig:' + '9' * 32,
                         'person:999999', 'person:' + '9' * 18,
                         'seed:fictional_absent', 'legacy:fictional_absent',
                         'legacy-person:999999', 'legacy-person:' + '9' * 18):
            with self.subTest(selector=selector):
                with self.assertRaises(LookupError):
                    connections.explore(self.conn, selector, 'ig:900')
        self.assertEqual(connections.search(self.conn, 'fictional_absent')['accounts'], [])

    def test_external_and_non_profile_urls_cannot_select_a_local_account(self):
        invalid = (
            'https://example.com/fictional_alice',
            'https://instagram.com.example.com/fictional_alice',
            'https://www.instagram.com.example.com/fictional_alice',
            'https://instagram.com@example.com/fictional_alice',
            'https://example.com/?next=https://instagram.com/fictional_alice',
            'https://instagram.com/p/fictional_alice/',
            'https://instagram.com/reel/fictional_alice/',
            'https://instagram.com/fictional_alice/followers/',
            'https://instagram.com/', 'https://[broken/fictional_alice',
        )
        for selector in invalid:
            with self.subTest(selector=selector):
                with self.assertRaises(ValueError):
                    connections.explore(self.conn, selector, 'ig:900')
                with self.assertRaises(ValueError):
                    connections.search(self.conn, selector)

    def test_non_text_and_empty_account_selectors_are_rejected(self):
        for selector in (None, False, 101, [], {}, '', ' \t\n', '@', 'fictional alice'):
            with self.subTest(selector=selector):
                with self.assertRaises(ValueError):
                    connections.explore(self.conn, selector, 'ig:900')
        for query in (None, False, 101, [], {}):
            with self.subTest(query=query):
                with self.assertRaises(ValueError):
                    connections.search(self.conn, query)

    def test_search_never_returns_a_selector_that_its_own_explorer_rejects(self):
        # Import/storage accepts text IDs. A displayed account must remain selectable
        # even when that imported ID cannot be treated as a known Instagram identity.
        for index, malformed_id in enumerate(('not-a-number', '123 ', '9' * 33)):
            handle = f'fictional_bad_id_{index}'
            self.person(handle, malformed_id)
            accounts = connections.search(self.conn, handle)['accounts']
            self.assertTrue(accounts)
            for account in accounts:
                with self.subTest(stored_id=malformed_id, selected_id=account['id']):
                    self.assert_selectable(account)


if __name__ == '__main__':
    unittest.main()
