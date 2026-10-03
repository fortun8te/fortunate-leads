"""Connection-only discovery uses existing gates without requiring profile reads."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import collection_suggestions as suggestions
import db


class NetworkDiscovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = db.init(str(Path(self.temp.name) / 'network.sqlite'))
        self.addCleanup(self.conn.close)
        suggestions.ensure(self.conn)
        self.next_ig = 10000

    def person(self, handle, sources=2, observed=True, **fields):
        self.next_ig += 1
        pid = db.upsert_person(self.conn, dict(handle=handle, ig_id=str(self.next_ig),
                                             is_private=0, **fields))
        for index in range(sources):
            db.add_edge(self.conn, 'source' + str(index), pid, 'following', observed=observed)
        return pid

    def rows(self, **kwargs):
        return suggestions.suggest(self.conn, **kwargs)['suggestions']

    def account(self):
        self.conn.execute("INSERT OR REPLACE INTO accounts(lane_id,handle,ig_id,last_seen,role,version) "
                          "VALUES('test','collector','900',?,'both','3.9.17')", (db.now(),))
        return self.conn.execute("SELECT * FROM accounts WHERE lane_id='test'").fetchone()

    def test_unread_no_verdict_or_bio_enters_with_connection_only_reason(self):
        pid = self.person('unread')
        before = self.conn.total_changes
        row = self.rows()[0]
        self.assertEqual((row['person_id'], row['observed_sources'], row['evidence_kind']), (pid, 2, 'connections'))
        self.assertEqual(row['directions'], ['following'])
        self.assertIsNone(row['business_fit'])
        self.assertEqual(row['reason'], 'Seen in 2 collected accounts’ lists')
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_one_source_two_directions_does_not_count_twice(self):
        pid = self.person('one', sources=1)
        db.add_edge(self.conn, 'source0', pid, 'followers')
        self.assertEqual(self.rows(), [])

    def test_historical_edges_never_qualify_even_with_stale_cached_degree(self):
        pid = self.person('historical', sources=4, observed=False)
        self.conn.execute('INSERT INTO map_person_degree(person_id,degree) VALUES(?,4)', (pid,))
        self.assertEqual(self.rows(), [])

    def test_removed_observation_does_not_qualify(self):
        pid = self.person('removed')
        self.conn.execute("UPDATE edge_evidence SET active=0 WHERE person_id=? AND seed='source0'", (pid,))
        self.assertEqual(self.rows(), [])

    def test_missing_public_state_or_stable_identity_excluded(self):
        for handle, field, value in [('private', 'is_private', 1), ('unknown', 'is_private', None),
                                     ('no_id', 'ig_id', None), ('parked', 'handle', 'parked~4')]:
            pid = self.person(handle)
            self.conn.execute('UPDATE people SET ' + field + '=? WHERE id=?', (value, pid))
        self.assertEqual(self.rows(), [])

    def test_explicit_rejections_and_hidden_targets_remain_excluded(self):
        pid = self.person('rejected')
        self.conn.execute("INSERT INTO marks(person_id,status) VALUES(?,'no')", (pid,))
        for tag in ('Blocked', 'Not a fit', 'Do not scrape'):
            pid = self.person('tagged' + str(self.next_ig))
            self.conn.execute("INSERT INTO tags VALUES(?,?,'signal','manual')", (pid, tag))
        self.person('hidden')
        suggestions.hide(self.conn, 'hidden')
        self.assertEqual(self.rows(), [])

    def test_reviewed_low_fit_remains_excluded(self):
        pid = self.person('weak')
        self.conn.execute("INSERT INTO verdicts(person_id,content_fit,score,prefilter,tier) VALUES(?,20,20,20,'cold')", (pid,))
        self.assertEqual(self.rows(), [])

    def test_protected_identity_and_historic_collector_excluded(self):
        for handle in ('main_alias', 'former_bot'):
            pid = self.person(handle)
            ig = self.conn.execute('SELECT ig_id FROM people WHERE id=?', (pid,)).fetchone()[0]
            if handle == 'main_alias':
                self.conn.execute("INSERT INTO seeds(handle,ig_id,is_me) VALUES('old_main',?,1)", (ig,))
            else:
                self.conn.execute("INSERT INTO account_identity_state(lane_id,ig_id) VALUES('old',?)", (ig,))
        self.assertEqual(self.rows(), [])

    def test_completed_partial_failed_and_queued_following_never_repeated(self):
        for state in ('done', 'partial', 'error', 'queued'):
            handle = 'prior_' + state
            self.person(handle)
            self.conn.execute("INSERT INTO jobs(kind,seed,direction,state) VALUES('list',?,'following',?)", (handle, state))
        self.assertEqual(self.rows(), [])

    def test_saved_fit_and_owner_priority_survive_both_policies(self):
        self.person('network', following=1)
        pid = self.person('fit', sources=0, following=6000)
        self.conn.execute("INSERT INTO verdicts(person_id,content_fit,score,prefilter,tier) VALUES(?,50,50,50,'warm')", (pid,))
        pid = self.person('client', sources=0, following=7000)
        self.conn.execute("INSERT INTO marks(person_id,status) VALUES(?,'client')", (pid,))
        for policy in ('saved_fit', 'bounded_completion'):
            suggestions.set_policy(self.conn, policy)
            rows = self.rows(limit=20, automatic=True, following_only=True)
            self.assertEqual([r['handle'] for r in rows], ['client', 'fit', 'network'])

    def test_queue_stays_finite_following_only_and_adds_no_profile_jobs(self):
        for index in range(4):
            self.person('network' + str(index))
        account = self.account()
        for index in range(2):
            result = suggestions.queue_when_idle(self.conn, account)
            self.assertEqual(result['directions'], ['following'])
            self.conn.execute("UPDATE jobs SET retry_not_before='2099-01-01' WHERE state='queued'")
        self.assertIsNone(suggestions.queue_when_idle(self.conn, account))
        rows = self.conn.execute('SELECT kind,direction FROM jobs').fetchall()
        self.assertEqual([tuple(row) for row in rows], [('list', 'following')] * 2)

    def test_warning_and_pauses_still_stop_network_fallback(self):
        self.person('network')
        account = self.account()
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        for key, value in [('paused_lists', True), ('cooldown', until), ('auto_discover', False)]:
            db.set_setting(self.conn, key, value)
            self.assertIsNone(suggestions.queue_when_idle(self.conn, account))
            self.conn.execute('DELETE FROM settings WHERE key=?', (key,))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_network_candidate_pool_uses_index_and_no_edge_scan(self):
        self.person('network')
        plan = ' '.join(str(tuple(row)) for row in self.conn.execute('EXPLAIN QUERY PLAN ' + suggestions.CANDIDATES,
            {'rank_pool': 2000, 'owner_pool': 500, 'limit': 6, 'following_only': True}))
        self.assertIn('discovery_network_candidates', plan)
        self.assertNotIn('SCAN edges', plan)
        self.assertNotIn('SCAN edge_evidence', plan)

    def test_keyset_moves_past_excluded_head_and_persists_across_restart(self):
        for index in range(4):
            handle = 'hidden' + str(index)
            self.person(handle, sources=4)
            suggestions.hide(self.conn, handle)
        target = self.person('useful_tail', sources=2)
        account = self.account()
        with mock.patch.object(suggestions, 'RANK_POOL', 2):
            self.assertIsNone(suggestions.queue_when_idle(self.conn, account))
            first = db.get_setting(self.conn, suggestions.NETWORK_CURSOR_KEY)
            self.assertGreater(first['person_id'], 0)
            self.conn.commit()
            # A new connection sees the durable cursor, not the excluded head.
            reopened = db.init(str(Path(self.temp.name) / 'network.sqlite'))
            self.addCleanup(reopened.close)
            self.assertEqual(db.get_setting(reopened, suggestions.NETWORK_CURSOR_KEY), first)
            account = reopened.execute("SELECT * FROM accounts WHERE lane_id='test'").fetchone()
            self.assertIsNone(suggestions.queue_when_idle(reopened, account))
            self.assertGreater(db.get_setting(reopened, suggestions.NETWORK_CURSOR_KEY)['person_id'], first['person_id'])
            picked = suggestions.queue_when_idle(reopened, account)
            self.assertEqual(picked['person_id'], target)
            self.assertEqual(reopened.execute("SELECT count(*) FROM jobs WHERE kind='list'").fetchone()[0], 1)

    def test_preview_and_paused_or_held_lane_do_not_advance_cursor(self):
        for index in range(3):
            self.person('hidden' + str(index))
            suggestions.hide(self.conn, 'hidden' + str(index))
        account = self.account()
        with mock.patch.object(suggestions, 'RANK_POOL', 2):
            before = self.conn.total_changes
            self.assertEqual(self.rows(), [])
            self.assertEqual(self.conn.total_changes, before)
            self.assertIsNone(db.get_setting(self.conn, suggestions.NETWORK_CURSOR_KEY))
            db.set_setting(self.conn, 'paused_lists', True)
            self.assertIsNone(suggestions.queue_when_idle(self.conn, account))
            self.assertIsNone(db.get_setting(self.conn, suggestions.NETWORK_CURSOR_KEY))
            db.set_setting(self.conn, 'paused_lists', False)
            self.conn.execute("UPDATE accounts SET hold='login' WHERE lane_id='test'")
            held = self.conn.execute("SELECT * FROM accounts WHERE lane_id='test'").fetchone()
            self.assertIsNone(suggestions.queue_when_idle(self.conn, held))
            self.assertIsNone(db.get_setting(self.conn, suggestions.NETWORK_CURSOR_KEY))

    def test_exhaustion_waits_before_wrap_and_discovers_later_added_accounts(self):
        account = self.account()
        now = datetime.now(timezone.utc)
        with mock.patch.object(suggestions, 'NETWORK_REVISIT_SECONDS', 2):
            self.assertIsNone(suggestions.queue_when_idle(self.conn, account, now))
            checkpoint = db.get_setting(self.conn, suggestions.NETWORK_CURSOR_KEY)
            self.assertIn('not_before', checkpoint)
            before = self.conn.total_changes
            self.assertIsNone(suggestions.queue_when_idle(self.conn, account, now))
            self.assertEqual(self.conn.total_changes, before)
            target = self.person('new_after_tail')
            self.assertIsNone(suggestions.queue_when_idle(self.conn, account, now))
            self.assertEqual(db.get_setting(self.conn, suggestions.NETWORK_CURSOR_KEY), checkpoint)
            picked = suggestions.queue_when_idle(self.conn, account, now + timedelta(seconds=3))
            self.assertEqual(picked['person_id'], target)

    def test_completed_identity_stays_excluded_after_cursor_wrap(self):
        pid = self.person('first')
        account = self.account()
        now = datetime.now(timezone.utc)
        picked = suggestions.queue_when_idle(self.conn, account, now)
        self.assertEqual(picked['person_id'], pid)
        self.conn.execute("UPDATE jobs SET state='done'")
        self.conn.execute('DELETE FROM jobs')
        self.conn.execute('DELETE FROM lists')
        self.conn.execute("UPDATE people SET handle='renamed_first' WHERE id=?", (pid,))
        db.set_setting(self.conn, suggestions.NETWORK_CURSOR_KEY, {'degree': suggestions.MAX_DEGREE, 'person_id': 0})
        self.assertIsNone(suggestions.queue_when_idle(self.conn, account, now))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_late_equal_degree_window_has_bounded_actual_sql_work(self):
        # Most graph candidates have the same small degree. A degree-only seek
        # plus an OR/person filter silently scans 98,000 preceding rows here.
        self.conn.executemany('INSERT INTO map_person_degree(person_id,degree) VALUES(?,2)',
                              ((pid,) for pid in range(1, 100001)))
        def measure(after):
            work = [0]
            def progress():
                work[0] += 100
                return int(work[0] > 150000)  # abort a prefix scan, not a wall-clock assertion
            self.conn.set_progress_handler(progress, 100)
            try:
                rows = self.conn.execute(suggestions.NETWORK_POOL,
                    {'network_degree': 2, 'network_person': after, 'rank_pool': 2000}).fetchall()
            finally:
                self.conn.set_progress_handler(None, 0)
            return rows, work[0]
        first, early_work = measure(0)
        last, late_work = measure(98000)
        self.assertEqual([row['person_id'] for row in first], list(range(1, 2001)))
        self.assertEqual([row['person_id'] for row in last], list(range(98001, 100001)))
        self.assertLessEqual(late_work, early_work * 2 + 1000)
        empty, tail_work = measure(100000)
        self.assertEqual(empty, [])
        self.assertLess(tail_work, 1000)

    def test_seek_merges_degree_boundary_without_skips_or_duplicates(self):
        self.conn.executemany('INSERT INTO map_person_degree(person_id,degree) VALUES(?,?)',
                              [(1, 4), (2, 4), (3, 4), (4, 3), (5, 3), (6, 2)])
        rows = self.conn.execute(suggestions.NETWORK_POOL,
                                {'network_degree': 4, 'network_person': 2, 'rank_pool': 3}).fetchall()
        self.assertEqual([(row['person_id'], row['degree']) for row in rows], [(3, 4), (4, 3), (5, 3)])
        rows = self.conn.execute(suggestions.NETWORK_POOL,
                                {'network_degree': 3, 'network_person': 5, 'rank_pool': 3}).fetchall()
        self.assertEqual([(row['person_id'], row['degree']) for row in rows], [(6, 2)])


if __name__ == '__main__':
    unittest.main()
