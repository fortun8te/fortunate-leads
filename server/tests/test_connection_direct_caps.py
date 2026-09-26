"""Direct observations must survive the real endpoint and display limits.

All data is fictional and in memory. Assertions inspect the public explorer
response; no graph methods or limit constants are patched.
"""
import itertools
import sqlite3
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connections
import db


STAMP = '2020-01-01'
ENDPOINT_CAP = 1000


@contextmanager
def database():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    try:
        yield conn
    finally:
        conn.close()


def person(conn, handle, ig):
    return db.upsert_person(conn, {'handle': handle, 'ig_id': ig}, STAMP)


def observe(conn, handle, member, direction, source_ig, member_ig):
    db.add_edge(conn, handle, member, direction, STAMP,
                seed_ig_id=source_ig, person_ig_id=member_ig)


def crowded_pair(conn, noise_count, direction='following',
                 source_known=True, member_known=True, reciprocal=False):
    """Put the direct row after noise at both endpoints.

    Distinct noise populations cannot form another route between the pair.
    Each endpoint has noise_count + 1 observations, or + 2 if reciprocal.
    This needs about 2,001 observations overall; crowding only one endpoint
    lets the opposite endpoint recover the direct row without its safeguard.
    """
    source_ig = '1' if source_known else None
    member_ig = '2' if member_known else None
    conn.execute('INSERT INTO seeds VALUES(?,?,0,?)', ('z_owner', source_ig, STAMP))
    for index in range(noise_count):
        noise_ig = str(10000 + index)
        pid = person(conn, f'noise_member{index:04d}', noise_ig)
        observe(conn, 'z_owner', pid, direction, source_ig, noise_ig)

    # A later member ID and lexically later source handle put the direct row
    # beyond both caps, regardless of followers/following list orientation.
    member = person(conn, 'pair_member', member_ig)
    for index in range(noise_count):
        observe(conn, f'a_source{index:04d}', member, direction,
                str(20000 + index), member_ig)
    observe(conn, 'z_owner', member, direction, source_ig, member_ig)
    if reciprocal:
        other = 'followers' if direction == 'following' else 'following'
        observe(conn, 'z_owner', member, other, source_ig, member_ig)

    owner_key = 'ig:1' if source_known else 'legacy:z_owner'
    member_key = 'ig:2' if member_known else f'legacy-person:{member}'
    forward = (owner_key, member_key) if direction == 'following' else (member_key, owner_key)
    return owner_key, member_key, forward


class DirectCapTests(unittest.TestCase):
    def assert_flags(self, result, search_clipped, display_clipped):
        search = result['search']
        self.assertEqual(search['search_truncated'], search_clipped)
        self.assertEqual(search['results_truncated'], display_clipped)
        self.assertEqual(search['truncated'], search_clipped or display_clipped)
        self.assertEqual(search['paths_displayed'], len(result['paths']))

    def assert_direct(self, result, directed_edges, expect_path):
        actual = {(edge['from'], edge['to']) for edge in result['direct_edges']}
        self.assertEqual(actual, directed_edges)
        self.assertTrue(all(edge['evidence'] for edge in result['direct_edges']))
        self.assertEqual(len(result['paths']), int(expect_path))
        self.assertEqual(result['search']['paths_found'], int(expect_path))
        if expect_path:
            path = result['paths'][0]
            source, target = result['source']['id'], result['target']['id']
            self.assertEqual(path['nodes'], [source, target])
            self.assertEqual(path['length'], 1)
            self.assertEqual(path['hops'][0]['traversal'],
                             'forward' if (source, target) in directed_edges else 'backward')

    def test_direct_survives_both_endpoint_caps_in_every_orientation_and_identity_pair(self):
        for direction, source_known, member_known in itertools.product(
                ('following', 'followers'), (True, False), (True, False)):
            with self.subTest(direction=direction, source_known=source_known,
                              member_known=member_known), database() as conn:
                owner, member, edge = crowded_pair(conn, ENDPOINT_CAP, direction,
                                                   source_known, member_known)
                before = conn.total_changes
                # Both query orders cover all four source-list orientations.
                for start, end in ((owner, member), (member, owner)):
                    for mode in ('all', 'following'):
                        with self.subTest(start=start, mode=mode):
                            result = connections.explore(conn, start, end, mode)
                            self.assert_direct(result, {edge}, mode == 'all' or edge == (start, end))
                            self.assert_flags(result, search_clipped=True, display_clipped=False)
                            self.assertTrue(result['search']['neighborhoods_clipped'])
                self.assertEqual(conn.total_changes, before)

    def test_exact_endpoint_cap_does_not_claim_incomplete_search(self):
        for source_known, member_known in itertools.product((True, False), repeat=2):
            with self.subTest(source_known=source_known, member_known=member_known), database() as conn:
                owner, member, edge = crowded_pair(conn, ENDPOINT_CAP - 1,
                                                   source_known=source_known, member_known=member_known)
                result = connections.explore(conn, owner, member, 'following')
                self.assert_direct(result, {edge}, True)
                self.assert_flags(result, search_clipped=False, display_clipped=False)

    def test_reciprocal_direct_relations_survive_both_caps_with_legacy_endpoints(self):
        for source_known, member_known in itertools.product((True, False), repeat=2):
            with self.subTest(source_known=source_known, member_known=member_known), database() as conn:
                owner, member, _ = crowded_pair(conn, ENDPOINT_CAP, reciprocal=True,
                                                source_known=source_known, member_known=member_known)
                edges = {(owner, member), (member, owner)}
                for start, end in ((owner, member), (member, owner)):
                    for mode in ('all', 'following'):
                        with self.subTest(start=start, mode=mode):
                            result = connections.explore(conn, start, end, mode)
                            self.assert_direct(result, edges, True)
                            self.assertEqual(result['paths'][0]['reciprocal_hops'], 1)
                            self.assertTrue(all(edge['reciprocal'] for edge in result['direct_edges']))
                            self.assert_flags(result, search_clipped=True, display_clipped=False)

    def test_direct_evidence_exact_cap_and_cap_plus_one_keep_the_single_path(self):
        for count in (ENDPOINT_CAP, ENDPOINT_CAP + 1):
            with self.subTest(observations=count), database() as conn:
                member = person(conn, 'pair_member', '2')
                # Multiple historical handles can support the same captured-ID pair.
                for index in range(count):
                    observe(conn, f'old_owner{index:04d}', member, 'following', '1', '2')
                result = connections.explore(conn, 'ig:1', 'ig:2', 'following', limit=1)
                self.assert_direct(result, {('ig:1', 'ig:2')}, True)
                self.assert_flags(result, search_clipped=count > ENDPOINT_CAP, display_clipped=False)
                evidence = result['direct_edges'][0]['evidence']
                supports = {item['seed'] for item in evidence}
                expected_supports = {f'old_owner{index:04d}' for index in range(count)}
                self.assertEqual(len(evidence), len(supports))
                self.assertLessEqual(supports, expected_supports)
                if count == ENDPOINT_CAP:
                    self.assertEqual(supports, expected_supports)

    def test_many_forward_observations_cannot_hide_the_reciprocal_direction(self):
        with database() as conn:
            member = person(conn, 'pair_member', '2')
            for index in range(ENDPOINT_CAP):
                observe(conn, f'a_owner{index:04d}', member, 'following', '1', '2')
            observe(conn, 'z_owner', member, 'followers', '1', '2')
            for start, end in (('ig:1', 'ig:2'), ('ig:2', 'ig:1')):
                result = connections.explore(conn, start, end, 'following', limit=1)
                self.assert_direct(result, {('ig:1', 'ig:2'), ('ig:2', 'ig:1')}, True)
                self.assertEqual(result['paths'][0]['reciprocal_hops'], 1)
                self.assert_flags(result, search_clipped=True, display_clipped=False)

    def test_display_limit_and_endpoint_limit_report_distinct_missing_results(self):
        for crowded in (False, True):
            with self.subTest(crowded=crowded), database() as conn:
                bridge = person(conn, 'bridge', '3')
                member = person(conn, 'pair_member', '2')
                observe(conn, 'z_owner', bridge, 'following', '1', '3')
                observe(conn, 'bridge', member, 'following', '3', '2')
                observe(conn, 'z_owner', member, 'following', '1', '2')
                if crowded:
                    for index in range(ENDPOINT_CAP - 1):
                        noise_ig = str(10000 + index)
                        noise = person(conn, f'noise{index:04d}', noise_ig)
                        observe(conn, 'z_owner', noise, 'following', '1', noise_ig)
                result = connections.explore(conn, 'ig:1', 'ig:2', 'following', limit=1)
                self.assertEqual(result['paths'][0]['nodes'], ['ig:1', 'ig:2'])
                self.assertEqual(result['search']['paths_found'], 2)
                self.assert_flags(result, search_clipped=crowded, display_clipped=True)
                expanded = connections.explore(conn, 'ig:1', 'ig:2', 'following', limit=2)
                self.assertEqual({tuple(path['nodes']) for path in expanded['paths']},
                                 {('ig:1', 'ig:2'), ('ig:1', 'ig:3', 'ig:2')})
                self.assert_flags(expanded, search_clipped=crowded, display_clipped=False)


if __name__ == '__main__':
    unittest.main()
