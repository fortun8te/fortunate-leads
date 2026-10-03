"""Canonical novelty and replay integrity, using isolated SQLite fixtures only."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import acquisition as A


OLD = '2026-01-01T00:00:00+00:00'
NEW = '2026-02-01T00:00:00+00:00'


class Acquisition(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        A.ensure(self.conn)
        self.conn.commit()

    def page(self, **changes):
        args = dict(event_id='page1', source_kind='chrome', seed_ig_id='100',
                    direction='following', members=[{'ig_id': '200', 'handle': 'alice'}],
                    observed_at=OLD, job_id=1, source_ref='request:one')
        args.update(changes)
        return A.record_page(self.conn, **args)

    def test_inverse_direction_reuses_fact_and_keeps_both_origins(self):
        one = self.page()
        two = self.page(event_id='page2', source_kind='local_import', seed_ig_id='200',
                        direction='followers', members=[{'ig_id': '100', 'handle': 'owner'}],
                        observed_at=NEW, job_id=None, source_ref='sha256:source-file')
        self.assertEqual((one['new_canonical_edges'], one['new_nodes']), (1, 1))
        self.assertEqual((two['new_canonical_edges'], two['known_canonical_edges'], two['new_nodes']), (0, 1, 0))
        self.assertEqual(A.stats(self.conn)['canonical_edges'], 1)
        self.assertEqual(A.stats(self.conn)['observations'], 2)
        origins = self.conn.execute('SELECT source_kind,source_ref FROM acquisition_events ORDER BY event_id').fetchall()
        self.assertEqual([tuple(r) for r in origins], [('chrome', 'request:one'), ('local_import', 'sha256:source-file')])
        self.assertEqual(A.neighbors(self.conn, '200', 'followers')['members'][0]['ig_id'], '100')
        self.assertEqual(A.neighbors(self.conn, '100', 'following')['members'][0]['ig_id'], '200')
        self.assertEqual(A.neighbors(self.conn, '200', 'following')['members'], [])

    def test_reciprocal_follows_are_two_different_facts(self):
        self.page()
        reverse = self.page(event_id='reverse', seed_ig_id='200', members=[{'ig_id': '100'}])
        self.assertEqual(reverse['new_canonical_edges'], 1)
        self.assertEqual(A.stats(self.conn)['canonical_edges'], 2)

    def test_replayed_page_keeps_metrics_and_does_no_writes(self):
        first = self.page()
        before = self.conn.total_changes
        replay = self.page(members=[{'ig_id': '0200', 'handle': '@ALICE'}],
                           observed_at='2026-01-01T01:00:00+01:00')
        self.assertEqual(replay, dict(first, replayed=True))
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(A.stats(self.conn)['events'], 1)

    def test_replay_conflicts_reject_without_touching_original_evidence(self):
        self.page()
        before = A.stats(self.conn)
        for changes in ({'members': [{'ig_id': '300'}]}, {'direction': 'followers'},
                        {'observed_at': NEW}, {'source_kind': 'local_import'}, {'job_id': 2},
                        {'source_ref': 'different'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.page(**changes)
        self.assertEqual(A.stats(self.conn), before)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM acquisition_observations').fetchone()[0], 1)

    def test_dates_are_source_dates_and_unknown_never_becomes_import_time(self):
        self.page(observed_at=None)
        row = self.conn.execute('SELECT first_observed_at,last_observed_at FROM acquisition_facts').fetchone()
        self.assertEqual(tuple(row), (None, None))
        self.page(event_id='known-new', observed_at=NEW)
        self.page(event_id='known-old', observed_at=OLD)
        self.page(event_id='unknown-late', observed_at=None)
        row = self.conn.execute('SELECT first_observed_at,last_observed_at FROM acquisition_facts').fetchone()
        self.assertEqual(tuple(row), (A.observation_date(OLD), A.observation_date(NEW)))
        latest = A.recent_job_metrics(self.conn, 1, 1)[0]
        self.assertIsNone(latest['observed_at'])
        self.assertIsNotNone(latest['ingested_at'])

    def test_duplicate_unknown_self_and_missing_ids_have_disjoint_counts(self):
        event = self.page(members=[{'ig_id': '200'}, {'ig_id': 200}, {'ig_id': '100'},
                                   {'ig_id': 'not-an-id', 'handle': 'same_handle'},
                                   {'handle': 'same_handle'}, {'ig_id': 0}, {'ig_id': True}])
        self.assertEqual([event[key] for key in ('returned_members', 'distinct_members', 'duplicate_members',
                         'ignored_self_members', 'unresolved_members', 'new_canonical_edges')], [7, 1, 1, 1, 4, 1])
        self.assertEqual(A.stats(self.conn)['nodes'], 2)

    def test_invalid_source_dates_and_bounds_have_no_effect(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        for changes in ({'seed_ig_id': 0}, {'seed_ig_id': True}, {'seed_ig_id': 100.0},
                        {'seed_ig_id': '١٠٠'}, {'direction': 'mutual'}, {'observed_at': future},
                        {'observed_at': '2026-01-01'}, {'observed_at': ''}, {'job_id': True},
                        {'members': [{}] * 2001}, {'max_members': 10001}, {'members': ['200']}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.page(**changes)
        self.assertEqual(A.stats(self.conn)['events'], 0)
        self.assertEqual(A.stats(self.conn)['canonical_edges'], 0)

    def test_caller_rollback_and_ensure_do_not_commit(self):
        self.conn.execute('CREATE TABLE unrelated(value TEXT)')
        self.conn.execute("INSERT INTO unrelated VALUES('pending')")
        A.ensure(self.conn)
        self.page()
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM unrelated').fetchone()[0], 0)
        self.assertEqual(A.stats(self.conn)['events'], 0)

    def test_failure_mid_page_rolls_back_partial_facts_but_preserves_callers_work(self):
        self.conn.execute('CREATE TABLE unrelated(value TEXT)')
        self.conn.execute("INSERT INTO unrelated VALUES('keep')")
        self.conn.execute("CREATE TRIGGER reject_event BEFORE INSERT ON acquisition_events BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.page()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM acquisition_nodes').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM acquisition_facts').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM acquisition_observations').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM unrelated').fetchone()[0], 1)

    def legacy(self):
        self.conn.executescript('''CREATE TABLE people(id INTEGER PRIMARY KEY,ig_id TEXT UNIQUE,handle TEXT UNIQUE COLLATE NOCASE);
          CREATE TABLE seeds(handle TEXT PRIMARY KEY COLLATE NOCASE,ig_id TEXT);
          CREATE TABLE edges(seed TEXT COLLATE NOCASE,person_id INTEGER,direction TEXT,first_seen TEXT,
                             PRIMARY KEY(seed,person_id,direction));
          INSERT INTO people VALUES(1,'100','owner'),(2,'200','alice');
          INSERT INTO seeds VALUES('owner','100'),('alice','200');''')
        A.ensure(self.conn)

    def test_priming_preexisting_inverse_edge_prevents_coldstart_false_novelty(self):
        self.legacy()
        self.conn.execute("INSERT INTO edges VALUES('alice',1,'followers',?)", (OLD,))
        primed = A.prime_existing(self.conn, seed_ig_id='100', direction='following', members=[{'ig_id': '200'}])
        self.assertEqual((primed['primed_edges'], primed['primed_nodes']), (1, 2))
        fact = self.conn.execute('SELECT first_observed_at,last_observed_at FROM acquisition_facts').fetchone()
        self.assertEqual(tuple(fact), (None, None))
        self.assertEqual(A.stats(self.conn)['events'], 0)
        event = self.page()
        self.assertEqual((event['new_canonical_edges'], event['known_canonical_edges'], event['new_nodes']), (0, 1, 0))
        self.assertEqual(A.stats(self.conn)['baseline_edges'], 1)
        self.assertEqual(self.conn.execute('SELECT identity_basis FROM acquisition_baseline_facts').fetchone()[0],
                         'retrospective_consistent_saved_identity')
        again = A.prime_existing(self.conn, seed_ig_id='100', direction='following', members=[{'ig_id': '200'}])
        self.assertEqual((again['primed_edges'], again['primed_nodes']), (0, 0))

    def test_baseline_rejects_conflicting_seed_handle_identity(self):
        self.legacy()
        self.conn.execute("UPDATE seeds SET ig_id='999' WHERE handle='owner'")
        self.conn.execute("INSERT INTO edges VALUES('owner',2,'following',?)", (OLD,))
        result = A.prime_existing(self.conn, seed_ig_id='100', direction='following', members=[{'ig_id': '200'}])
        self.assertEqual(result['primed_edges'], 0)
        self.assertEqual(self.page()['new_canonical_edges'], 1)

    def test_baseline_primes_old_profile_nodes_without_inventing_edges(self):
        self.legacy()
        result = A.prime_existing(self.conn, seed_ig_id='100', direction='following', members=[{'ig_id': '200'}])
        self.assertEqual((result['primed_edges'], result['primed_nodes']), (0, 2))
        event = self.page()
        self.assertEqual((event['new_canonical_edges'], event['new_nodes']), (1, 0))

    def test_recent_job_read_is_bounded_with_100k_unrelated_events(self):
        self.page(job_id=7)
        self.conn.executemany('''INSERT INTO acquisition_events SELECT ?,payload_digest,source_kind,seed_ig_id,
          direction,observed_at,ingested_at,999,source_ref,returned_members,distinct_members,
          new_canonical_edges,known_canonical_edges,new_nodes,unresolved_members,duplicate_members,ignored_self_members
          FROM acquisition_events WHERE event_id='page1' ''', ((f'other-{i}',) for i in range(100000)))
        operations = [0]
        def progress():
            operations[0] += 100
            return int(operations[0] > 5000)
        self.conn.set_progress_handler(progress, 100)
        try:
            self.assertEqual([row['event_id'] for row in A.recent_job_metrics(self.conn, 7)], ['page1'])
            self.assertEqual(A.recent_job_metrics(self.conn, 888), [])
            self.assertEqual(A.stats(self.conn)['events'], 1)
        finally:
            self.conn.set_progress_handler(None, 0)

    def test_neighbors_seek_both_directions_without_rescanning_large_prefix(self):
        self.conn.executemany('INSERT INTO acquisition_facts VALUES(?,?,NULL,NULL)',
                              (('100', str(i)) for i in range(100000, 200000)))
        operations = [0]
        def progress():
            operations[0] += 100
            return int(operations[0] > 5000)
        self.conn.set_progress_handler(progress, 100)
        try:
            outgoing = A.neighbors(self.conn, '100', 'following', after='199989', limit=10)
            self.assertEqual([row['ig_id'] for row in outgoing['members']], [str(i) for i in range(199990, 200000)])
            incoming = A.neighbors(self.conn, '199999', 'followers', limit=10)
            self.assertEqual([row['ig_id'] for row in incoming['members']], ['100'])
        finally:
            self.conn.set_progress_handler(None, 0)

    def test_baseline_pair_lookups_are_bounded_with_100k_unrelated_edges(self):
        self.legacy()
        self.conn.executemany('INSERT INTO edges VALUES(?,?,?,?)',
                              (('unrelated', i, 'following', OLD) for i in range(100000, 200000)))
        self.conn.execute("INSERT INTO edges VALUES('alice',1,'followers',?)", (OLD,))
        operations = [0]
        def progress():
            operations[0] += 100
            return int(operations[0] > 5000)
        self.conn.set_progress_handler(progress, 100)
        try:
            result = A.prime_existing(self.conn, seed_ig_id='100', direction='following', members=[{'ig_id': '200'}])
            self.assertEqual(result['primed_edges'], 1)
        finally:
            self.conn.set_progress_handler(None, 0)


if __name__ == '__main__':
    unittest.main()
