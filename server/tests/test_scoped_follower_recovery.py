"""Opt-in follower recovery preserves all existing holds and warning handling."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import accounts
import db
import server
import test_server_accounts as lanes


class ScopedFollowerRecoveryTest(lanes.Base):
    nxt = lanes.LaneTest.nxt
    def post(self, who, path, body):
        if path == '/api/ext/list-page':
            body = dict(body, http_status=200)
        return lanes.LaneTest.post(self, who, path, body)
    seeds = lanes.LaneTest.seeds

    def prepare(self, direction='followers'):
        self.seeds('recent_following', direction='following')
        evidence = self.nxt('a', 'list')['job']
        lanes.LaneTest.page(self, 'a', evidence, 1, None, done=True)
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


class IsolatedFollowerRecoveryTest(lanes.Base):
    nxt = lanes.LaneTest.nxt
    def post(self, who, path, body):
        if path == '/api/ext/list-page':
            body = dict(body, http_status=200)
        return lanes.LaneTest.post(self, who, path, body)
    seeds = lanes.LaneTest.seeds

    def prepare(self, evidence=True):
        self.post('b', '/api/ext/heartbeat', {'version':'3.9.30', 'tab':'tab_scraping_warning'})
        self.post('a', '/api/ext/heartbeat', {'version':'3.9.30', 'tab':'ok'})
        db.set_setting(self.conn, 'budget', {'list':200, 'profile':100})
        self.conn.commit()
        self.assertEqual(self.call('/api/control', {'action':'resume_selected_accounts',
            'accounts':[{'lane_id':'lane-a','ig_id':'101'}], 'collection_stages':['lists']})[0], 200)
        if evidence:
            self.seeds('independent', direction='following')
            page = self.nxt('a','list')['job']
            lanes.LaneTest.page(self,'a',page,1,None,done=True)
        self.seeds('failed', direction='followers')
        job = self.nxt('a','list')['job']
        db.set_setting(self.conn,'follower_redirect_recovery','route')
        self.conn.commit()
        return job

    def redirect(self, job):
        return self.post('a','/api/ext/error', {'job_id':job['id'],'lease_token':job['lease_token'],
            'code':'other','reason':'list_html_home_redirect','http_status':200})

    def test_verified_following_preserves_existing_isolation_and_failed_cursor(self):
        job = self.prepare()
        self.conn.execute("UPDATE lists SET cursor='saved-cursor', received=2 WHERE seed='failed'")
        self.conn.commit()
        warning = db.get_setting(self.conn,'instagram_scraping_warning')
        isolation = db.get_setting(self.conn,'instagram_collection_isolation')
        self.redirect(job)
        self.assertEqual(db.get_setting(self.conn,'instagram_collection_isolation'), isolation)
        self.assertEqual(db.get_setting(self.conn,'instagram_scraping_warning'), warning)
        self.assertFalse(db.get_setting(self.conn,'paused_lists'))
        self.assertTrue(db.get_setting(self.conn,'paused_bios'))
        self.assertTrue(self.conn.execute("SELECT hold FROM accounts WHERE lane_id='lane-b'").fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT cursor FROM lists WHERE seed='failed'").fetchone()[0],'saved-cursor')
        self.seeds('healthy',direction='following')
        self.assertEqual(self.nxt('a','list')['job']['seed'],'healthy')
        self.assertIsNone(self.nxt('b','list')['job'])

    def test_missing_independent_evidence_keeps_workspace_paused(self):
        job=self.prepare(evidence=False)
        self.redirect(job)
        self.assertIsNone(db.get_setting(self.conn,'instagram_collection_isolation'))
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))

    def test_new_warning_invalidates_route_evidence(self):
        job=self.prepare()
        self.post('b','/api/ext/heartbeat',{'version':'3.9.30','tab':'tab_scraping_warning'})
        self.redirect(job)
        self.assertIsNone(db.get_setting(self.conn,'instagram_collection_isolation'))
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))


    def test_mismatched_reporting_identity_cannot_preserve_isolation(self):
        job=self.prepare()
        self.call('/api/ext/error',{'lane_id':'lane-a','account':{'ig_id':'999'},
            'job_id':job['id'],'lease_token':job['lease_token'],'code':'other',
            'reason':'list_html_home_redirect','http_status':200})
        self.assertIsNone(db.get_setting(self.conn,'instagram_collection_isolation'))
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))

    def age_following_evidence_and_assert_pause(self, age):
        job=self.prepare()
        authorization=accounts.utc(db.get_setting(self.conn,'instagram_collection_isolation')['at'])
        self.conn.execute("UPDATE collector_events SET at=? WHERE direction='following'", ((authorization-age).isoformat(),))
        self.conn.commit()
        self.redirect(job)
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))
        self.assertIsNone(db.get_setting(self.conn,'instagram_collection_isolation'))

    def test_stale_evidence_cannot_preserve_isolation(self):
        self.age_following_evidence_and_assert_pause(timedelta(minutes=31))

    def test_pre_authorization_evidence_cannot_preserve_isolation(self):
        self.age_following_evidence_and_assert_pause(timedelta(seconds=1))

    def test_malformed_identity_history_wait_remains_paused(self):
        job=self.prepare()
        self.conn.execute("UPDATE account_identity_state SET list_cool_until='invalid' WHERE ig_id='101'")
        self.conn.commit()
        self.redirect(job)
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))
        self.assertIsNone(db.get_setting(self.conn,'instagram_collection_isolation'))

    def test_default_policy_still_stops_with_healthy_evidence(self):
        job=self.prepare()
        db.set_setting(self.conn,'follower_redirect_recovery',None)
        self.conn.commit()
        self.redirect(job)
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))
        self.assertIsNone(db.get_setting(self.conn,'instagram_collection_isolation'))

    def test_legacy_unbound_page_is_not_health_evidence(self):
        job=self.prepare()
        self.conn.execute("UPDATE collector_events SET viewer_ig_id=NULL WHERE direction='following'")
        self.conn.commit()
        self.redirect(job)
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))

    def test_job_reassignment_cannot_relabel_other_viewer_evidence(self):
        job=self.prepare()
        evidence=self.conn.execute("SELECT * FROM collector_events WHERE direction='following'").fetchone()
        self.assertEqual(evidence['viewer_ig_id'],'101')  # saved at accepted page ingestion
        self.conn.execute('UPDATE collector_events SET viewer_ig_id=? WHERE id=?',('999',evidence['id']))
        self.conn.execute('UPDATE jobs SET viewer_ig_id=? WHERE id=?',('101',evidence['job_id']))
        self.conn.commit()
        self.redirect(job)
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))
        self.assertEqual(self.conn.execute('SELECT viewer_ig_id FROM collector_events WHERE id=?',(evidence['id'],)).fetchone()[0],'999')

    def test_selected_security_warning_still_stops_with_healthy_evidence(self):
        job=self.prepare()
        self.post('a','/api/ext/error',{'job_id':job['id'],'lease_token':job['lease_token'],
            'code':'challenge','reason':'scraping_warning','http_status':200})
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))
        self.assertIsNone(db.get_setting(self.conn,'instagram_collection_isolation'))
        row=self.conn.execute("SELECT * FROM accounts WHERE lane_id='lane-a'").fetchone()
        self.assertTrue(accounts.warning_for(self.conn,row))
        self.assertEqual(row['hold'],'challenge')

    def test_active_identity_history_wait_stops_with_healthy_evidence(self):
        job=self.prepare()
        until=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        self.conn.execute('UPDATE account_identity_state SET list_cool_until=? WHERE ig_id=?',(until,'101'))
        self.conn.commit()
        self.redirect(job)
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))
        self.assertEqual(self.conn.execute("SELECT list_cool_until FROM account_identity_state WHERE ig_id='101'").fetchone()[0],until)

    def test_migration_leaves_legacy_event_viewer_unknown(self):
        job=self.prepare()
        self.conn.execute('ALTER TABLE collector_events DROP COLUMN viewer_ig_id')
        self.conn.commit()
        migrated=db.init(server.CFG['db'])
        try:
            self.assertIsNone(migrated.execute("SELECT viewer_ig_id FROM collector_events WHERE direction='following'").fetchone()[0])
        finally:
            migrated.close()
        self.redirect(job)
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))


    def set_evidence_status_and_assert_pause(self, status):
        job=self.prepare()
        self.conn.execute("UPDATE collector_events SET http_status=? WHERE direction='following'",(status,))
        self.conn.commit()
        self.redirect(job)
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))

    def test_unknown_transport_status_is_not_healthy_evidence(self):
        self.set_evidence_status_and_assert_pause(None)

    def test_rate_limited_transport_is_not_healthy_evidence(self):
        self.set_evidence_status_and_assert_pause(429)
