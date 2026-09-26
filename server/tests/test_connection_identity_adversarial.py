"""Identity contracts tested against independently recorded fictional snapshots."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connections
import db


class ConnectionIdentityAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.addCleanup(self.conn.close)

    def person(self, handle, ig_id=None):
        return db.upsert_person(self.conn, {'handle': handle, 'ig_id': ig_id}, '2024-01-01')

    def seed(self, handle, ig_id=None):
        self.conn.execute('INSERT INTO seeds(handle,ig_id) VALUES(?,?)', (handle, ig_id))

    def observe(self, handle, source_id, person_id, member_id, direction='following'):
        # IDs come from the fixture's captured snapshot, never current metadata.
        db.add_edge(self.conn, handle, person_id, direction, '2024-02-01',
                    seed_ig_id=source_id, person_ig_id=member_id)

    def paths(self, source, target, mode='following'):
        result = connections.explore(self.conn, source, target, mode, limit=50)
        self.assertFalse(result['search']['search_truncated'])
        return {tuple(path['nodes']) for path in result['paths']}

    def assert_no_bridge(self, source, target):
        for mode in ('all', 'following'):
            with self.subTest(mode=mode):
                result = connections.explore(self.conn, source, target, mode)
                self.assertEqual(result['paths'], [])
                self.assertTrue(all(row['count'] == 0 for row in result['common_neighbors'].values()))

    def test_reused_source_handle_keeps_old_chain_and_separates_new_owner(self):
        self.seed('start', '10')
        self.seed('shared_name', '20')
        bridge = self.person('shared_name', '20')
        target = self.person('destination', '90')
        self.observe('start', '10', bridge, '20')
        self.observe('shared_name', '20', target, '90')

        self.person('shared_name', '30')
        self.conn.execute("UPDATE seeds SET ig_id='30' WHERE handle='shared_name'")

        self.assertEqual(self.paths('ig:10', 'ig:90'), {('ig:10', 'ig:20', 'ig:90')})
        self.assert_no_bridge('seed:shared_name', 'ig:90')
        result = connections.explore(self.conn, 'ig:20', 'ig:90')
        self.assertEqual(result['direct_edges'][0]['evidence'][0]['seed_ig_id'], '20')
        ids = {account['id'] for account in connections.search(self.conn, 'shared_name')['accounts']}
        self.assertTrue({'ig:20', 'ig:30'} <= ids)

    def test_reused_member_handle_cannot_join_new_sources_chain(self):
        self.seed('start', '10')
        old = self.person('shared_name', '20')
        target = self.person('destination', '90')
        self.observe('start', '10', old, '20')

        self.person('shared_name', '30')
        self.seed('shared_name', '30')
        self.observe('shared_name', '30', target, '90')

        self.assert_no_bridge('ig:10', 'ig:90')
        self.assertEqual(self.paths('ig:10', 'ig:20'), {('ig:10', 'ig:20')})
        self.assertEqual(self.paths('ig:30', 'ig:90'), {('ig:30', 'ig:90')})

    def test_swapped_source_and_member_names_preserve_independent_path_oracle(self):
        ids = ('10', '20', '30', '40', '90')
        names = ('alder', 'birch', 'cedar', 'dahlia', 'elm')
        people = {ig: self.person(name, ig) for name, ig in zip(names, ids)}
        for name, ig in zip(names, ids):
            self.seed(name, ig)
        arcs = {('10', '20'), ('20', '30'), ('30', '90'), ('40', '10'), ('40', '90')}
        handles = dict(zip(ids, names))
        for index, (source, member) in enumerate(sorted(arcs)):
            if index % 2:
                self.observe(handles[member], member, people[source], source, 'followers')
            else:
                self.observe(handles[source], source, people[member], member)

        # Each handle is reused by another already-known account. upsert_person
        # parks old names as needed; captured edge IDs must remain authoritative.
        rotated = names[1:] + names[:1]
        for ig, new_name in zip(ids, rotated):
            self.assertEqual(self.person(new_name, ig), people[ig])
        for ig, new_name in zip(ids, rotated):
            self.conn.execute('UPDATE seeds SET ig_id=? WHERE handle=?', (ig, new_name))

        def oracle(start, target, mode):
            found = set()

            def visit(path):
                if path[-1] == target:
                    if len(path) > 1:
                        found.add(tuple('ig:' + node for node in path))
                    return
                if len(path) == 4:
                    return
                for nxt in ids:
                    adjacent = (path[-1], nxt) in arcs or mode == 'all' and (nxt, path[-1]) in arcs
                    if nxt not in path and adjacent:
                        visit(path + [nxt])

            visit([start])
            return found

        for start in ids:
            for target in ids:
                for mode in ('all', 'following'):
                    with self.subTest(start=start, target=target, mode=mode):
                        self.assertEqual(self.paths('ig:' + start, 'ig:' + target, mode), oracle(start, target, mode))

    def test_member_snapshot_survives_current_row_id_mismatch_and_finds_true_profile(self):
        self.seed('start', '10')
        old_row = self.person('old_member', '20')
        target = self.person('destination', '90')
        self.observe('start', '10', old_row, '20')
        self.conn.execute("UPDATE people SET ig_id='30',handle='unrelated_owner' WHERE id=?", (old_row,))
        true_row = self.person('renamed_member', '20')
        self.seed('unrelated_owner', '30')
        self.observe('unrelated_owner', '30', target, '90')

        self.assert_no_bridge('ig:10', 'ig:90')
        self.assert_no_bridge('ig:10', 'person:' + str(old_row))
        result = connections.explore(self.conn, 'ig:10', 'ig:20')
        self.assertEqual(result['target']['handle'], 'renamed_member')
        self.assertEqual(result['target']['person_id'], true_row)
        self.assertEqual(result['paths'][0]['nodes'], ['ig:10', 'ig:20'])

    def test_known_historical_ids_without_matching_current_metadata_remain_inspectable(self):
        old_row = self.person('old_member', '20')
        self.observe('past_source', '10', old_row, '20')
        self.conn.execute("UPDATE people SET ig_id='30',handle='unrelated_owner' WHERE id=?", (old_row,))

        result = connections.explore(self.conn, 'ig:10', 'ig:20')
        self.assertEqual(result['paths'][0]['nodes'], ['ig:10', 'ig:20'])
        self.assertEqual(result['source']['handle_basis'], 'observed_source_handle')
        self.assertEqual(result['target']['handle_basis'], 'unavailable')
        self.assertEqual(result['target']['handle'], 'id:20')
        self.assertEqual(result['edges'][0]['evidence'][0]['person_ig_id'], '20')
        self.assert_no_bridge('ig:10', 'ig:30')

    def test_unknown_source_later_enriched_never_becomes_shared_bridge(self):
        start = self.person('start', '10')
        target = self.person('destination', '90')
        self.seed('unknown_source')
        self.observe('unknown_source', None, start, '10')
        self.observe('unknown_source', None, target, '90')
        self.conn.execute("UPDATE seeds SET ig_id='20' WHERE handle='unknown_source'")
        self.person('unknown_source', '20')

        self.assert_no_bridge('ig:10', 'ig:90')
        self.assert_no_bridge('ig:20', 'ig:90')
        for target_id in ('10', '90'):
            self.assertEqual(self.paths('legacy:unknown_source', 'ig:' + target_id),
                             {('legacy:unknown_source', 'ig:' + target_id)})
        accounts = connections.search(self.conn, 'unknown_source')['accounts']
        self.assertTrue({'legacy:unknown_source', 'ig:20'} <= {row['id'] for row in accounts})

    def test_unknown_member_merged_into_known_profile_stays_historical_only(self):
        self.seed('start', '10')
        known = self.person('old_bridge_name', '20')
        unknown = self.person('new_bridge_name')
        target = self.person('destination', '90')
        self.observe('start', '10', unknown, None)
        self.seed('new_bridge_name', '20')
        self.observe('new_bridge_name', '20', target, '90')

        self.assertEqual(self.person('new_bridge_name', '20'), known)
        self.assertIsNone(self.conn.execute('SELECT 1 FROM people WHERE id=?', (unknown,)).fetchone())
        self.assert_no_bridge('ig:10', 'ig:90')
        legacy = 'legacy-person:' + str(known)
        self.assertEqual(self.paths('ig:10', legacy), {('ig:10', legacy)})
        accounts = connections.search(self.conn, 'new_bridge_name')['accounts']
        self.assertTrue({legacy, 'ig:20'} <= {row['id'] for row in accounts})

    def test_unknown_member_and_source_with_same_handle_remain_distinct_endpoints(self):
        self.seed('start', '10')
        unknown = self.person('ambiguous')
        target = self.person('destination', '90')
        self.seed('ambiguous')
        self.observe('start', '10', unknown, None)
        self.observe('ambiguous', None, target, '90')

        self.assert_no_bridge('ig:10', 'ig:90')
        legacy_member = 'legacy-person:' + str(unknown)
        self.assertEqual(self.paths('ig:10', legacy_member), {('ig:10', legacy_member)})
        self.assertEqual(self.paths('legacy:ambiguous', 'ig:90'), {('legacy:ambiguous', 'ig:90')})
        self.assert_no_bridge(legacy_member, 'legacy:ambiguous')

    def test_deleted_current_member_does_not_erase_captured_known_id_edge(self):
        self.seed('start', '10')
        member = self.person('former_member', '20')
        self.observe('start', '10', member, '20')
        self.conn.execute('DELETE FROM people WHERE id=?', (member,))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edge_observations').fetchone()[0], 1)

        # The observation still names both IDs. Display metadata is optional.
        self.assertEqual(self.paths('ig:10', 'ig:20'), {('ig:10', 'ig:20')})

    def test_reused_deleted_local_row_never_transfers_captured_identity(self):
        self.seed('start', '10')
        member = self.person('former_member', '20')
        self.observe('start', '10', member, '20')
        self.conn.execute('DELETE FROM people WHERE id=?', (member,))
        replacement = self.person('replacement_member', '30')
        self.assertEqual(replacement, member)  # SQLite may reuse its highest rowid.
        self.assert_no_bridge('ig:10', 'ig:30')
        result = connections.explore(self.conn, 'ig:10', 'ig:20')
        self.assertEqual(result['target']['handle'], 'id:20')
        self.assertEqual(result['paths'][0]['nodes'], ['ig:10', 'ig:20'])


if __name__ == '__main__':
    unittest.main()
