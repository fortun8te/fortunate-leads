import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
from connection_graph import compare


class ConnectionGraphTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.a = self.person('alice', '100')
        self.b = self.person('bob', '200')
        self.seed('alice', '100')
        self.seed('bob', '200')

    def tearDown(self):
        self.c.close()

    def person(self, handle, ig=None):
        return self.c.execute("INSERT INTO people(handle,ig_id,first_seen,updated_at) VALUES(?,?,'2026-01-01','2026-01-01')", (handle, ig)).lastrowid

    def seed(self, handle, ig=None):
        self.c.execute('INSERT INTO seeds(handle,ig_id) VALUES(?,?)', (handle, ig))

    def edge(self, seed, pid, direction, ts='2026-01-01T00:00:00+00:00'):
        self.c.execute('INSERT OR IGNORE INTO edges VALUES(?,?,?,?)', (seed, pid, direction, ts))

    def test_direction_and_provenance(self):
        x = self.person('connector', '300')
        self.edge('alice', x, 'following')
        self.edge('bob', x, 'followers')
        r = compare(self.c, '@ALICE', 'bob')
        self.assertEqual(r['connectors'][0]['motifs'], ['directed_path'])
        self.assertEqual({(l['source'], l['target']) for l in r['graph']['links']}, {('ig:100', 'ig:300'), ('ig:300', 'ig:200')})
        self.assertTrue(all(l['evidence'] for l in r['graph']['links']))

    def test_failed_collection_without_rows_is_still_incomplete(self):
        self.c.execute("INSERT INTO lists(seed,direction,state,received) VALUES('alice','followers','error',0)")
        r = compare(self.c, 'alice', 'bob')
        row = next(c for c in r['coverage'] if c['seed'] == 'alice' and c['direction'] == 'followers')
        self.assertEqual((row['status'], row['state']), ('partial', 'error'))

    def test_deduplicate_direction_from_two_source_lists(self):
        self.edge('alice', self.b, 'following')
        self.edge('bob', self.a, 'followers')
        r = compare(self.c, 'alice', 'bob')
        self.assertEqual(len(r['direct_relationships']), 1)
        self.assertEqual(len(r['direct_relationships'][0]['evidence']), 2)
        self.assertEqual(r['total_candidates'], 0)

    def test_id_alias_collapse_and_self_rejection(self):
        self.seed('old_alice', '100')
        with self.assertRaises(ValueError):
            compare(self.c, 'alice', 'old_alice')
        self.edge('old_alice', self.b, 'following')
        self.assertEqual(compare(self.c, 'alice', 'bob')['direct_relationships'][0]['source'], 'ig:100')

    def test_idless_alias_preserves_incoming_edges_and_degree(self):
        alias = self.person('oldalice')
        self.seed('oldalice', '100')
        x = self.person('x', '300')
        y = self.person('y', '400')
        self.seed('x', '300')
        self.edge('x', alias, 'following')
        self.edge('x', y, 'following')
        r = compare(self.c, 'alice', 'y')
        self.assertEqual(r['source']['handle'], 'alice')
        self.assertEqual(r['source']['person_id'], self.a)
        self.assertEqual(r['connectors'][0]['node']['handle'], 'x')
        self.assertEqual(r['connectors'][0]['rank']['observed_degree'], 2)
        self.assertEqual(r['connectors'][0]['motifs'], ['shared_follower'])
        self.assertEqual(r['total_candidates'], compare(self.c, 'oldalice', 'y')['total_candidates'])
        # A known ID conflict is not a safe alias.
        self.c.execute("UPDATE people SET ig_id='999' WHERE id=?", (alias,))
        self.assertEqual(compare(self.c, 'alice', 'y')['total_candidates'], 0)
        with self.assertRaises(ValueError):
            compare(self.c, 'oldalice', 'y')

    def test_manual_known_on_safe_alias_is_preserved(self):
        x = self.person('connector', '300')
        alias = self.person('oldconnector')
        self.seed('connector', '300')
        self.seed('oldconnector', '300')
        self.edge('alice', x, 'following')
        self.edge('bob', x, 'followers')
        self.c.execute("INSERT INTO tags VALUES(?,'Already know them','signal','manual')", (alias,))
        self.assertTrue(compare(self.c, 'alice', 'bob')['connectors'][0]['manual_known'])
        self.c.execute("UPDATE people SET ig_id='999' WHERE id=?", (alias,))
        self.assertFalse(compare(self.c, 'alice', 'bob')['connectors'][0]['manual_known'])

    def test_conflicting_handle_does_not_merge(self):
        self.c.execute("UPDATE seeds SET ig_id='999' WHERE handle='alice'")
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            compare(self.c, 'alice', 'bob')
        self.edge('alice', self.b, 'following')
        self.seed('charlie', '888')
        r = compare(self.c, 'charlie', 'bob')
        self.assertEqual(r['total_candidates'], 0)

    def test_idless_handle_fallback(self):
        x = self.person('idless')
        self.seed('idless', '300')
        self.edge('alice', x, 'following')
        self.edge('idless', self.b, 'following')
        r = compare(self.c, 'alice', 'bob')
        self.assertEqual(r['connectors'][0]['node']['id'], 'ig:300')
        self.assertEqual(r['connectors'][0]['rank']['observed_degree'], 2)

    def test_nonseed_endpoints_show_missing_collection_coverage(self):
        self.person('uncollected', '555')
        r = compare(self.c, 'alice', 'uncollected')
        missing = [row for row in r['coverage'] if row['seed'] == 'uncollected']
        self.assertEqual(len(missing), 2)
        self.assertTrue(all(row['status'] == 'uncollected' for row in missing))

    def test_seed_without_id_uses_same_handle_person(self):
        self.c.execute("UPDATE seeds SET ig_id=NULL WHERE handle='alice'")
        self.edge('alice', self.b, 'following')
        r = compare(self.c, 'alice', 'bob')
        self.assertEqual(r['source']['id'], 'ig:100')
        self.assertEqual(len(r['direct_relationships']), 1)

    def test_all_motifs_and_reciprocity_ranks_first(self):
        x = self.person('reciprocal')
        y = self.person('directed')
        for s in ('alice', 'bob'):
            for d in ('followers', 'following'):
                self.edge(s, x, d)
        self.edge('alice', y, 'following')
        self.edge('bob', y, 'followers')
        r = compare(self.c, 'alice', 'bob')
        self.assertEqual(r['connectors'][0]['node']['handle'], 'reciprocal')
        self.assertEqual(len(r['connectors'][0]['motifs']), 5)
        reverse = compare(self.c, 'bob', 'alice')
        self.assertEqual(reverse['connectors'][1]['motifs'], ['reverse_path'])

    def test_shared_follower_and_followee_are_distinct(self):
        for handle, direction in [('follower', 'followers'), ('followee', 'following')]:
            x = self.person(handle)
            self.edge('alice', x, direction)
            self.edge('bob', x, direction)
        r = compare(self.c, 'alice', 'bob')
        self.assertEqual({c['motifs'][0] for c in r['connectors']}, {'shared_follower', 'shared_followee'})

    def test_hubs_and_limits_deterministic(self):
        x, y = self.person('a_hub'), self.person('z_specific')
        for pid in (x, y):
            for s in ('alice', 'bob'):
                self.edge(s, pid, 'followers')
        self.seed('other')
        self.edge('other', x, 'followers')
        r = compare(self.c, 'alice', 'bob', 1)
        self.assertEqual(r['total_candidates'], 2)
        self.assertTrue(r['truncated'])
        self.assertEqual(r['connectors'][0]['node']['handle'], 'z_specific')
        self.assertEqual(len(r['graph']['nodes']), 3)
        self.assertEqual(len(r['graph']['links']), 2)
        self.assertEqual(r, compare(self.c, 'alice', 'bob', 1))

    def test_truncated_results_only_read_displayed_edge_history(self):
        self.c.execute('CREATE TABLE IF NOT EXISTS edge_observations(seed TEXT,person_id INT,direction TEXT,page_key TEXT,job_id INT,observed_at TEXT)')
        for i in range(80):
            pid = self.person('candidate' + str(i))
            self.edge('alice', pid, 'following')
            self.edge('bob', pid, 'followers')
        statements = []
        self.c.set_trace_callback(statements.append)
        try:
            r = compare(self.c, 'alice', 'bob', 1)
        finally:
            self.c.set_trace_callback(None)
        self.assertEqual(r['total_candidates'], 80)
        self.assertEqual(len(r['graph']['links']), 2)
        reads = [sql for sql in statements if 'SELECT observed_at, job_id, page_key FROM edge_observations' in sql]
        self.assertEqual(len(reads), 2)
        self.assertTrue(all(link['evidence'] for link in r['graph']['links']))

    def test_coverage_historical_and_unknown(self):
        self.c.execute("INSERT INTO lists(seed,direction,state,received,total) VALUES('alice','followers','done',8,10)")
        self.c.execute("INSERT INTO lists(seed,direction,state,received,total) VALUES('alice','following','done',8,NULL)")
        self.c.execute("INSERT INTO lists(seed,direction,state,received,total) VALUES('bob','followers','running',2,9)")
        r = compare(self.c, 'alice', 'bob')
        self.assertEqual([c['status'] for c in r['coverage']], ['count_mismatch', 'reported_complete', 'partial', 'uncollected'])
        self.assertEqual(r['graph']['links'], [])

    def test_manual_known_only_not_pipeline(self):
        x = self.person('connector')
        for s in ('alice', 'bob'):
            self.edge(s, x, 'following')
        self.c.execute("INSERT INTO marks(person_id,status) VALUES(?,'client')", (x,))
        self.c.execute("INSERT INTO tags VALUES(?,'Already know them','signal','auto')", (x,))
        self.assertFalse(compare(self.c, 'alice', 'bob')['connectors'][0]['manual_known'])
        self.c.execute("UPDATE tags SET source='manual'")
        self.assertTrue(compare(self.c, 'alice', 'bob')['connectors'][0]['manual_known'])

    def test_reject_bad_input(self):
        self.person('parked~4', '400')
        for handle in ('unknown', 'parked~4', '', 'a/b'):
            with self.assertRaises(ValueError):
                compare(self.c, handle, 'bob')
        for limit in (0, 101, True, 1.5, '5'):
            with self.assertRaises(ValueError):
                compare(self.c, 'alice', 'bob', limit)

    def test_untrusted_dates_do_not_become_freshness(self):
        for timestamp in ('not a date', '2099-01-01T00:00:00+00:00', '2026-01-01'):
            self.c.execute('DELETE FROM edges')
            self.edge('alice', self.b, 'following', timestamp)
            evidence = compare(self.c, 'alice', 'bob')['direct_relationships'][0]['evidence'][0]
            self.assertIsNone(evidence['first_observed'])
            self.assertIsNone(evidence['last_observed'])

    def test_observation_ledger_is_optional_and_honest(self):
        self.c.execute('CREATE TABLE IF NOT EXISTS edge_observations(seed TEXT,person_id INT,direction TEXT,page_key TEXT,job_id INT,observed_at TEXT)')
        self.edge('alice', self.b, 'following')
        for key, ts in [('one', '2026-02-01T00:00:00+00:00'), ('future', '2099-01-01T00:00:00+00:00')]:
            self.c.execute('INSERT INTO edge_observations VALUES(?,?,?,?,?,?)', ('alice', self.b, 'following', key, 7, ts))
        e = compare(self.c, 'alice', 'bob')['direct_relationships'][0]['evidence'][0]
        self.assertEqual(e['observation_count'], 2)
        self.assertEqual(e['last_observed'], '2026-02-01T00:00:00+00:00')
        self.assertEqual(e['last_page_key'], 'one')
        self.assertEqual(e['last_job_id'], 7)


class SnapshotTests(unittest.TestCase):
    def test_concurrent_collection_cannot_mix_graph_and_coverage_snapshots(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'snapshot.sqlite')
            reader = db.init(path)
            writer = db.connect(path)
            try:
                alice = db.upsert_person(writer, {'handle': 'alice', 'ig_id': '100'})
                bob = db.upsert_person(writer, {'handle': 'bob', 'ig_id': '200'})
                writer.execute("INSERT INTO seeds(handle,ig_id) VALUES('alice','100')")
                writer.commit()
                mutated = []

                def collect_during_comparison(sql):
                    # Seed registry reads have established the reader's snapshot.
                    if not mutated and sql.startswith('SELECT * FROM people WHERE handle='):
                        mutated.append(True)
                        db.add_edge(writer, 'alice', bob, 'following')
                        writer.execute("INSERT INTO lists(seed,direction,state,received,total) "
                                       "VALUES('alice','following','done',1,1)")
                        writer.commit()

                reader.set_trace_callback(collect_during_comparison)
                before = compare(reader, 'alice', 'bob')
                reader.set_trace_callback(None)
                self.assertEqual(mutated, [True])
                self.assertFalse(reader.in_transaction)
                self.assertEqual(before['direct_relationships'], [])
                coverage = next(row for row in before['coverage']
                                if row['seed'] == 'alice' and row['direction'] == 'following')
                self.assertEqual(coverage['status'], 'uncollected')
                after = compare(reader, 'alice', 'bob')
                self.assertEqual(len(after['direct_relationships']), 1)
                coverage = next(row for row in after['coverage']
                                if row['seed'] == 'alice' and row['direction'] == 'following')
                self.assertEqual(coverage['status'], 'reported_complete')
            finally:
                reader.close()
                writer.close()

    def test_comparison_preserves_caller_transaction_and_releases_own_on_error(self):
        conn = db.init(':memory:')
        try:
            db.upsert_person(conn, {'handle': 'alice', 'ig_id': '100'})
            db.upsert_person(conn, {'handle': 'bob', 'ig_id': '200'})
            compare(conn, 'alice', 'bob')
            self.assertTrue(conn.in_transaction)
            conn.rollback()
            self.assertEqual(conn.execute('SELECT count(*) FROM people').fetchone()[0], 0)
            with self.assertRaises(ValueError):
                compare(conn, 'unknown', 'bob')
            self.assertFalse(conn.in_transaction)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
