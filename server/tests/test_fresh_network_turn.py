"""Bounded fresh-network turns use temporary databases and no Instagram calls."""
import unittest
from datetime import datetime, timedelta, timezone
import test_collection_suggestions as suggestion_fixture
import accounts
import acquisition_dispatch as dispatch
import collection_suggestions as suggestions
import db


class FreshNetworkTurnTests(unittest.TestCase):
    def setUp(self):
        self.fixture = suggestion_fixture.CollectionSuggestions()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.conn = self.fixture.conn
        self.now = datetime.now(timezone.utc)
        self.row = self.fixture.account(version='3.9.31')
        self.fixture.person('fresh', 90, ig_id='201')
        db.queue_list(self.conn, 'old_giant', 'following', priority=0)
        self.old = self.conn.execute('SELECT max(id) FROM jobs').fetchone()[0]
        dispatch.set_policy(self.conn, dispatch.ADAPTIVE)
        self.tick = 0

    def page(self, job=None):
        job = job or self.old
        self.tick += 1
        self.conn.execute("INSERT INTO collector_events(at,lane,job_id,kind,outcome) VALUES(?,'test',?,'list','page')",
                          (accounts.iso(self.now + timedelta(seconds=self.tick)), job))
        self.conn.execute("UPDATE lists SET state='running',lane='test',cursor=? WHERE seed=(SELECT seed FROM jobs WHERE id=?)",
                          ('cursor-' + str(self.tick), job))

    def earn(self):
        for _ in range(4):
            self.page()

    def admit(self):
        return suggestions.queue_when_idle(self.conn, self.row, self.now + timedelta(seconds=60))

    def pick(self):
        return accounts.pick_job(self.conn, 'test', ['list'], self.now + timedelta(seconds=60))

    def test_four_saved_pages_admit_and_dispatch_one_fresh_then_resume(self):
        self.assertIsNone(self.admit())
        self.earn()
        self.assertEqual(self.admit()['handle'], 'fresh')
        picked = self.pick()
        self.assertEqual(picked['seed'], 'fresh')
        accounts.took(self.conn, 'test', picked)
        self.page(picked['id'])
        self.assertEqual(self.pick()['id'], self.old)
        self.assertEqual(self.conn.execute("SELECT cursor FROM lists WHERE seed='old_giant'").fetchone()[0], 'cursor-4')
        self.earn()
        self.assertEqual(self.pick()['seed'], 'fresh')

    def test_failed_opportunity_consumed_at_take_not_read_or_poll(self):
        self.earn(); self.admit()
        before = self.conn.total_changes
        for _ in range(3):
            self.assertEqual(self.pick()['seed'], 'fresh')
        self.assertEqual(self.conn.total_changes, before)
        picked = self.pick()
        accounts.took(self.conn, 'test', picked)
        self.conn.commit()
        # No saved exploration page: transport failed. Persisted take still spends it.
        self.assertEqual(self.pick()['id'], self.old)
        self.assertFalse(dispatch.fresh_turn_due(self.conn, 'test'))
        for _ in range(3): self.page()
        self.assertEqual(self.pick()['id'], self.old)
        self.page()
        self.assertEqual(self.pick()['seed'], 'fresh')

    def test_more_than_sixty_four_ordinary_jobs_does_not_hide_fresh(self):
        for i in range(80): db.queue_list(self.conn, 'backlog'+str(i), 'following')
        self.earn(); self.admit()
        self.assertEqual(self.pick()['seed'], 'fresh')

    def test_default_policy_keeps_discovery_idle_only(self):
        dispatch.set_policy(self.conn, dispatch.CURRENT)
        self.earn()
        self.assertIsNone(self.admit())
        self.assertEqual(self.pick()['id'], self.old)

    def test_positive_priority_and_pinned_trial_block_admission(self):
        self.earn()
        self.conn.execute('UPDATE jobs SET priority=100 WHERE id=?', (self.old,))
        self.assertIsNone(self.admit())
        self.conn.execute('UPDATE jobs SET priority=0,page_size=100,experiment_viewer_ig_id=? WHERE id=?',
                          (self.row['ig_id'], self.old))
        self.assertIsNone(self.admit())

    def test_disabled_auto_and_pending_cap(self):
        self.earn(); suggestions.set_enabled(self.conn, False)
        self.assertIsNone(self.admit())
        suggestions.set_enabled(self.conn, True)
        self.admit()
        self.fixture.person('fresh_second', 80, ig_id='202')
        self.assertEqual(self.admit()['handle'], 'fresh_second')
        self.fixture.person('fresh_third', 70, ig_id='203')
        self.assertIsNone(self.admit())
        self.assertEqual(suggestions._pending_auto(self.conn), 2)

    def test_pause_cooldown_and_other_owner_remain_guarded(self):
        self.earn()
        db.set_setting(self.conn, 'paused_lists', True)
        self.assertIsNone(self.admit())
        db.set_setting(self.conn, 'paused_lists', False)
        self.conn.execute("UPDATE accounts SET list_cool_until=? WHERE lane_id='test'",
                          (accounts.iso(self.now + timedelta(hours=1)),))
        row = self.conn.execute("SELECT * FROM accounts WHERE lane_id='test'").fetchone()
        self.assertIsNone(suggestions.queue_when_idle(self.conn, row, self.now))
        self.conn.execute("UPDATE accounts SET list_cool_until=NULL WHERE lane_id='test'")
        self.admit()
        accounts.touch(self.conn, 'other', {'ig_id':'902','handle':'othercollector'}, version='3.9.31')
        self.conn.execute("UPDATE lists SET lane='other' WHERE seed='fresh'")
        self.assertEqual(self.pick()['id'], self.old)

    def test_following_scope_does_not_dispatch_follower_backlog(self):
        self.conn.execute("UPDATE jobs SET direction='followers' WHERE id=?", (self.old,))
        self.conn.execute("UPDATE lists SET direction='followers' WHERE seed='old_giant'")
        self.earn(); db.set_setting(self.conn, 'follower_lists', False)
        self.assertEqual(self.admit()['directions'], ['following'])
        self.assertEqual(self.pick()['seed'], 'fresh')

    def test_transaction_rollback_preserves_opportunity_and_removes_admission(self):
        self.earn(); self.conn.commit()
        self.conn.execute('BEGIN IMMEDIATE')
        self.admit()
        accounts.took(self.conn, 'test', self.pick())
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(suggestions._pending_auto(self.conn), 0)
        self.assertTrue(dispatch.fresh_turn_due(self.conn, 'test'))

    def test_delayed_and_denied_fresh_job_beyond_window_stays_ineligible(self):
        for i in range(80): db.queue_list(self.conn, 'backlog'+str(i), 'following')
        self.earn(); self.admit()
        fresh = self.conn.execute("SELECT id FROM jobs WHERE seed='fresh'").fetchone()[0]
        self.conn.execute('UPDATE jobs SET retry_not_before=? WHERE id=?',
                          (accounts.iso(self.now + timedelta(hours=1)), fresh))
        self.assertNotEqual(self.pick()['id'], fresh)
        self.conn.execute('UPDATE jobs SET retry_not_before=NULL WHERE id=?', (fresh,))
        self.conn.execute("INSERT INTO list_private_denials(seed,direction,viewer_ig_id,denied_at) VALUES('fresh','following','900',?)", (accounts.iso(self.now),))
        self.assertNotEqual(self.pick()['id'], fresh)

    def test_persisted_spent_opportunity_survives_reopen(self):
        self.earn(); self.admit(); accounts.took(self.conn, 'test', self.pick())
        self.conn.commit()
        path = self.conn.execute('PRAGMA database_list').fetchone()[2]
        reopened = db.init(path)
        try:
            self.assertFalse(dispatch.fresh_turn_due(reopened, 'test'))
            picked = accounts.pick_job(reopened, 'test', ['list'], self.now + timedelta(seconds=60))
            self.assertEqual(picked['id'], self.old)
        finally: reopened.close()

    def test_selected_identity_budget_and_main_account_guards(self):
        self.earn()
        db.set_setting(self.conn, 'instagram_collection_isolation', {'accounts': {'other':'902'}})
        self.assertIsNone(self.admit())
        db.set_setting(self.conn, 'instagram_collection_isolation', None)
        for fields in ({'is_main':1}, {'hold':'login'}, {'budget':'{"list":1}', 'today':'{"list":1}'}, {'role':'bios'}):
            row = self.fixture.account(version='3.9.31', **fields)
            self.assertIsNone(suggestions.queue_when_idle(self.conn, row, self.now))

    def test_follower_redirect_fallback_keeps_automatic_following(self):
        self.earn(); self.admit()
        fresh = self.pick()
        accounts.took(self.conn, 'test', fresh)
        self.conn.execute("UPDATE jobs SET direction='followers' WHERE id=?", (self.old,))
        self.conn.execute("UPDATE lists SET direction='followers' WHERE seed='old_giant'")
        for i in range(3):
            self.conn.execute("INSERT INTO lists(seed,direction,state,error,updated_at) VALUES(?,'followers','partial','list_html_home_redirect',?)",
                              ('redirect'+str(i), accounts.iso(self.now)))
        self.assertEqual(self.pick()['id'], fresh['id'])
