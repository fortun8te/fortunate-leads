"""Opt-in follower recovery preserves all existing holds and warning handling."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import accounts
import db
import server
import test_server_accounts as lanes


class ScopedFollowerRecoveryTest(lanes.Base):
    nxt = lanes.LaneTest.nxt
    post = lanes.LaneTest.post
    seeds = lanes.LaneTest.seeds

    def prepare(self, direction='followers'):
        self.seeds('failed', direction=direction)
        job = self.nxt('a', 'list')['job']
        db.set_setting(self.conn, 'follower_redirect_recovery', 'route')
        self.conn.commit()
        return job

    def report_redirect(self, job, **changes):
        body = {'job_id':job['id'], 'lease_token':job['lease_token'], 'code':'other',
                'reason':'list_html_home_redirect', 'http_status':200}
        body.update(changes)
        return self.post('a', '/api/ext/error', body)

    def test_exact_follower_redirect_scopes_future_wait_and_prevents_target_bounce(self):
        job = self.prepare()
        self.assertEqual(self.report_redirect(job)[0], 200)
        self.assertIsNone(db.get_setting(self.conn, 'cooldown'))
        row = self.conn.execute("SELECT * FROM accounts WHERE lane_id='lane-a'").fetchone()
        route = accounts.follower_route_wait(self.conn, row, datetime.now(timezone.utc))
        retry = self.conn.execute('SELECT retry_not_before FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
        self.assertEqual(route, retry)
        self.assertIsNone(self.nxt('b', 'list')['job'])
        self.seeds('healthy', direction='following')
        self.assertEqual(self.nxt('a', 'list')['job']['seed'], 'healthy')
        self.conn.execute("INSERT INTO jobs(kind,handle,priority) VALUES('profile','bio',100)")
        self.conn.commit()
        self.assertEqual(self.nxt('a', 'profile')['job']['handle'], 'bio')

    def test_existing_workspace_hold_is_preserved_exactly_until_its_expiry(self):
        job = self.prepare()
        until = accounts.iso(datetime.now(timezone.utc) + timedelta(minutes=10))
        db.set_setting(self.conn, 'cooldown', until)
        self.conn.commit()
        self.report_redirect(job)
        self.assertEqual(db.get_setting(self.conn, 'cooldown'), until)
        self.seeds('healthy', direction='following')
        self.assertIsNone(self.nxt('a', 'list')['job'])
        future = accounts.utc(until) + timedelta(seconds=1)
        with patch.object(server, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = future
            self.assertEqual(self.nxt('a', 'list')['job']['seed'], 'healthy')
        self.assertEqual(db.get_setting(self.conn, 'cooldown'), until)

    def test_all_follower_leases_and_cached_permits_obey_workspace_route_hold(self):
        job = self.prepare()
        self.seeds('other_followers', direction='followers')
        cached = self.nxt('b', 'list')['job']
        self.report_redirect(job)
        self.assertIsNotNone(db.get_setting(self.conn, 'followers_route_until'))
        denied = self.post('b', '/api/ext/request', {'action':'acquire', 'kind':'list',
                          'job_id':cached['id'], 'lease_token':cached['lease_token']})[1]
        self.assertFalse(denied['granted'])
        self.assertTrue(denied['stale'])
        self.assertIsNone(self.nxt('b', 'list')['job'])
        self.seeds('healthy', direction='following')
        self.assertEqual(self.nxt('b', 'list')['job']['seed'], 'healthy')

    def test_ambiguous_response_remains_shared(self):
        for changes in ({'http_status':0}, {'http_status':429}, {'retry_after':'invalid'},
                        {'retry_at':accounts.iso(datetime.now(timezone.utc)+timedelta(hours=1))}):
            with self.subTest(changes=changes):
                self.conn.execute('DELETE FROM collector_events')
                self.conn.execute('DELETE FROM jobs')
                self.conn.execute('DELETE FROM lists')
                self.conn.execute('DELETE FROM accounts')
                self.conn.execute('DELETE FROM account_identity_state')
                db.set_setting(self.conn, 'cooldown', None)
                self.conn.commit()
                job = self.prepare()
                self.report_redirect(job, **changes)
                self.assertIsNotNone(server.workspace_cooldown(self.conn, datetime.now(timezone.utc)))

    def test_following_redirect_is_not_given_follower_exception(self):
        job = self.prepare('following')
        self.report_redirect(job)
        self.assertIsNotNone(server.workspace_cooldown(self.conn, datetime.now(timezone.utc)))

    def test_stale_and_expired_lease_remain_shared(self):
        job = self.prepare()
        self.conn.execute("UPDATE jobs SET leased_until='2000-01-01T00:00:00Z' WHERE id=?",(job['id'],))
        self.conn.commit()
        self.report_redirect(job)
        self.assertIsNotNone(server.workspace_cooldown(self.conn, datetime.now(timezone.utc)))

    def test_other_account_warning_keeps_shared_policy(self):
        job = self.prepare()
        self.post('b', '/api/ext/heartbeat', {'version':'3.9.17'})
        until=accounts.iso(datetime.now(timezone.utc)+timedelta(hours=1))
        self.conn.execute("UPDATE accounts SET profile_cool_until=? WHERE lane_id='lane-b'",(until,))
        self.conn.commit()
        self.report_redirect(job)
        self.assertIsNotNone(server.workspace_cooldown(self.conn, datetime.now(timezone.utc)))

    def test_actual_warning_still_stops_other_routes_in_opt_in_mode(self):
        job = self.prepare()
        self.report_redirect(job,code='rate_limit',reason='http_429',http_status=429)
        self.seeds('healthy',direction='following')
        self.assertIsNone(self.nxt('b','list')['job'])
        self.assertIsNotNone(server.workspace_cooldown(self.conn, datetime.now(timezone.utc)))
