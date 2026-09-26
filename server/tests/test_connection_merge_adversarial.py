"""Identity and evidence invariants through the real person-upsert merge path."""
import itertools
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connections
import db


class ConnectionMergeAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)

    def tearDown(self):
        self.conn.close()

    def person(self, handle, ig=None):
        return db.upsert_person(self.conn, {'handle': handle, 'ig_id': ig}, '2020-01-01')

    def observe(self, pid, source_id, member_id, date, seed='source', direction='following'):
        db.add_edge(self.conn, seed, pid, direction, date,
                    seed_ig_id=source_id, person_ig_id=member_id)

    def observations(self, pid):
        return [tuple(row) for row in self.conn.execute(
            'SELECT seed,direction,seed_ig_id,person_ig_id,first_seen,last_seen '
            'FROM edge_observations WHERE person_id=? '
            'ORDER BY seed,direction,seed_ig_id,person_ig_id', (pid,))]

    def test_merge_identity_matrix_retains_distinct_captures_and_date_extrema(self):
        # Equal IDs, different IDs, and every placement of an unknown ID at both
        # endpoints. A local-record merge may combine only identical evidence keys.
        pairs = [('11', '11'), ('11', '12'), ('11', ''), ('', '11'), ('', '')]
        for index, (sources, members) in enumerate(itertools.product(pairs, repeat=2)):
            with self.subTest(source_ids=sources, member_ids=members):
                keep = self.person('old' + str(index), str(1000 + index))
                drop_handle = 'new' + str(index)
                drop = self.person(drop_handle)
                for date in ('2022-01-01', '2024-01-01'):
                    self.observe(keep, sources[0], members[0], date)
                for date in ('2025-01-01', '2021-01-01'):
                    self.observe(drop, sources[1], members[1], date)

                merged = db.upsert_person(self.conn,
                    {'handle': drop_handle, 'ig_id': str(1000 + index)}, '2030-01-01')

                self.assertEqual(merged, keep)
                if sources[0] == sources[1] and members[0] == members[1]:
                    expected = [('source', 'following', sources[0], members[0],
                                 '2021-01-01', '2025-01-01')]
                else:
                    expected = sorted([
                        ('source', 'following', sources[0], members[0], '2022-01-01', '2024-01-01'),
                        ('source', 'following', sources[1], members[1], '2021-01-01', '2025-01-01')])
                self.assertEqual(self.observations(keep), expected)
                self.assertEqual(self.observations(drop), [])
                self.assertIsNone(self.conn.execute('SELECT id FROM people WHERE id=?', (drop,)).fetchone())

    def test_known_conflicting_people_are_parked_without_merging_evidence(self):
        for existing_new_account in (False, True):
            with self.subTest(existing_new_account=existing_new_account):
                suffix = str(int(existing_new_account))
                reused = 'reused' + suffix
                old_id, new_id = str(210 + int(existing_new_account)), str(220 + int(existing_new_account))
                old = self.person(reused, old_id)
                new = self.person('elsewhere' + suffix, new_id) if existing_new_account else None
                self.observe(old, '1', old_id, '2020-01-01', seed='source' + suffix)
                self.conn.execute('INSERT INTO marks(person_id,note) VALUES(?,?)', (old, 'Keep old owner note'))
                if new is not None:
                    self.observe(new, '2', new_id, '2021-01-01', seed='other' + suffix)

                result = db.upsert_person(self.conn, {'handle': reused, 'ig_id': new_id})

                if new is not None:
                    self.assertEqual(result, new)
                self.assertNotEqual(result, old)
                old_row = self.conn.execute('SELECT ig_id,handle FROM people WHERE id=?', (old,)).fetchone()
                self.assertEqual(old_row['ig_id'], old_id)
                self.assertNotEqual(old_row['handle'], reused)
                self.assertEqual(self.conn.execute('SELECT note FROM marks WHERE person_id=?',
                    (old,)).fetchone()[0], 'Keep old owner note')
                self.assertEqual(self.observations(old)[0][3], old_id)
                graph = connections.explore(self.conn, 'ig:1', 'ig:' + old_id, 'following')
                self.assertEqual(graph['paths'][0]['nodes'], ['ig:1', 'ig:' + old_id])
                self.assertEqual(connections.explore(self.conn, 'ig:1', 'ig:' + new_id, 'following')['paths'], [])

    def test_merged_unknown_history_stays_inspectable_without_becoming_a_bridge(self):
        keep = self.person('old_bridge', '20')
        drop = self.person('bridge')
        target = self.person('target', '30')
        self.conn.execute("INSERT INTO seeds(handle,ig_id) VALUES('source','1'),('bridge','20')")
        self.observe(drop, '1', '', '2020-01-01')
        self.observe(drop, '', '', '2019-01-01')
        self.observe(target, '20', '30', '2021-01-01', seed='bridge')

        self.assertEqual(connections.explore(self.conn, 'ig:1', 'ig:30')['paths'], [])
        self.assertEqual(db.upsert_person(self.conn, {'handle': 'bridge', 'ig_id': '20'}), keep)

        unknown = 'legacy-person:' + str(keep)
        for source in ('ig:1', 'legacy:source'):
            result = connections.explore(self.conn, source, unknown, 'following')
            self.assertEqual(result['paths'][0]['nodes'], [source, unknown])
            self.assertEqual(result['target']['identity_basis'], 'legacy_person')
            self.assertIsNone(result['target']['ig_id'])
        for mode in ('all', 'following'):
            self.assertEqual(connections.explore(self.conn, 'ig:1', 'ig:30', mode)['paths'], [])
        found = {account['id'] for account in connections.search(self.conn, 'bridge')['accounts']}
        self.assertTrue({unknown, 'ig:20'} <= found)
        self.assertEqual({row[2:4] for row in self.observations(keep)}, {('1', ''), ('', '')})

    def test_repeated_merges_preserve_chain_and_reused_source_ownership(self):
        keep = self.person('old_bridge', '20')
        first = self.person('bridge')
        target = self.person('target', '30')
        self.conn.execute("INSERT INTO seeds(handle,ig_id) VALUES('source','99'),('bridge','20')")
        self.observe(first, '1', '20', '2020-01-01')
        self.observe(target, '20', '30', '2021-01-01', seed='bridge')
        expected_path = ['ig:1', 'ig:20', 'ig:30']
        before = connections.explore(self.conn, 'ig:1', 'ig:30', 'following')
        self.assertEqual(before['paths'][0]['nodes'], expected_path)

        self.assertEqual(db.upsert_person(self.conn, {'handle': 'bridge', 'ig_id': '20'}), keep)
        second = self.person('latest_bridge')
        self.observe(second, '1', '20', '2023-01-01')
        self.observe(second, '99', '20', '2022-01-01')
        self.assertEqual(db.upsert_person(self.conn, {'handle': 'latest_bridge', 'ig_id': '20'}), keep)

        after = connections.explore(self.conn, 'ig:1', 'ig:30', 'following')
        self.assertEqual(after['paths'], before['paths'])
        self.assertEqual(after['paths'][0]['nodes'], expected_path)
        self.assertEqual(self.observations(keep), [
            ('source', 'following', '1', '20', '2020-01-01', '2023-01-01'),
            ('source', 'following', '99', '20', '2022-01-01', '2022-01-01')])
        for source, start, end in (('1', '2020-01-01', '2023-01-01'),
                                   ('99', '2022-01-01', '2022-01-01')):
            direct = connections.explore(self.conn, 'ig:' + source, 'ig:20', 'following')
            self.assertEqual(direct['paths'][0]['nodes'], ['ig:' + source, 'ig:20'])
            self.assertEqual(direct['edges'][0]['evidence'], [{
                'seed': 'source', 'direction': 'following', 'seed_ig_id': source,
                'person_ig_id': '20', 'first_seen': start, 'last_seen': end}])
        self.assertEqual(connections.explore(self.conn, 'ig:30', 'ig:1', 'following')['paths'], [])

    def test_merge_retains_known_dates_when_historical_collision_has_missing_dates(self):
        # Legacy edges permit missing recording dates. Migration copies those NULLs
        # into observations, so a collision must not erase the other known date.
        for index, unknown_on_keep in enumerate((False, True)):
            with self.subTest(unknown_on_keep=unknown_on_keep):
                keep = self.person('old_dated' + str(index), str(300 + index))
                handle = 'dated' + str(index)
                drop = self.person(handle)
                known, unknown = (drop, keep) if unknown_on_keep else (keep, drop)
                self.observe(known, '', '', '2020-01-01')
                self.observe(known, '', '', '2024-01-01')
                self.conn.execute(
                    'INSERT INTO edge_observations(seed,person_id,direction,seed_ig_id,person_ig_id,first_seen,last_seen) '
                    "VALUES('source',?,'following','','',NULL,NULL)", (unknown,))

                self.assertEqual(db.upsert_person(self.conn,
                    {'handle': handle, 'ig_id': str(300 + index)}), keep)

                self.assertEqual(self.observations(keep), [
                    ('source', 'following', '', '', '2020-01-01', '2024-01-01')])


if __name__ == '__main__':
    unittest.main()
