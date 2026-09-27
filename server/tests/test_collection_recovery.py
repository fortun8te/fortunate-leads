"""Recovery routing uses saved evidence, without contacting Instagram."""
from datetime import datetime, timedelta, timezone
import accounts
import db
import test_server_accounts as lanes


class RecoveryTest(lanes.Base):
    nxt = lanes.LaneTest.nxt
    post = lanes.LaneTest.post
    seeds = lanes.LaneTest.seeds
    age = lanes.LaneTest.age

    def test_redirect_defers_followers_but_following_can_run_after_shared_hold(self):
        self.post('a', '/api/ext/heartbeat', {'version':'3.9.17', 'state':'idle'})
        self.seeds('failed', direction='followers')
        job = self.nxt('a', 'list')['job']
        self.post('a', '/api/ext/error', {'job_id':job['id'], 'code':'other',
                                        'reason':'list_html_home_redirect', 'http_status':200})
        until = db.get_setting(self.conn, 'cooldown')
        row = self.conn.execute("SELECT * FROM accounts WHERE lane_id='lane-a'").fetchone()
        route_until = accounts.follower_route_wait(self.conn, row, datetime.now(timezone.utc))
        self.assertGreater(accounts.utc(route_until), accounts.utc(until) + timedelta(minutes=80))
        retry = self.conn.execute('SELECT retry_not_before FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
        self.assertEqual(retry, route_until)
        self.seeds('healthy', direction='following')
        self.assertIsNone(self.nxt('a', 'list')['job'])  # Current shared hold remains enforced.
        db.set_setting(self.conn, 'cooldown', '2000-01-01T00:00:00Z')  # Simulate its expiry in this isolated fixture.
        self.conn.commit()
        self.assertEqual(self.nxt('a', 'list')['job']['seed'], 'healthy')
        self.conn.execute("INSERT INTO jobs(kind,handle,priority) VALUES('profile','ready_bio',100)")
        self.conn.commit()
        self.assertEqual(self.nxt('a', 'profile')['job']['handle'], 'ready_bio')
        self.assertEqual(db.get_setting(self.conn, 'cooldown'), '2000-01-01T00:00:00Z')

    def test_historical_repeated_redirects_recover_wait_without_mutating_shared_hold(self):
        self.post('a', '/api/ext/heartbeat', {'version':'3.9.17','state':'idle'})
        self.seeds('failed')
        job = self.nxt('a', 'list')['job']
        now = datetime.now(timezone.utc)
        for minutes in (35, 5):
            self.conn.execute("INSERT INTO collector_events(at,lane,job_id,kind,direction,outcome,reason) VALUES(?,?,?,'list','followers','other','list_html_home_redirect')",
                              ((now-timedelta(minutes=minutes)).isoformat(),'lane-a',job['id']))
        hold = (now+timedelta(hours=3)).isoformat()
        db.set_setting(self.conn, 'cooldown', hold)
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM accounts WHERE lane_id='lane-a'").fetchone()
        until = accounts.follower_route_wait(self.conn, row, now)
        self.assertEqual(accounts.utc(until), now+timedelta(hours=3, minutes=55))
        self.assertEqual(db.get_setting(self.conn, 'cooldown'), hold)
        self.assertIsNone(accounts.follower_route_wait(self.conn, row, now+timedelta(hours=5)))

    def test_main_bios_wait_for_alternates_even_when_alternate_offline(self):
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('acct.a',1)")
        self.conn.commit()
        for who in ('a','b'):
            self.post(who, '/api/ext/heartbeat', {'version':'3.9.17','state':'idle'})
        self.conn.execute("INSERT INTO jobs(kind,handle,priority) VALUES('profile','someone',100)")
        self.conn.commit()
        self.age('lane-b',20)
        self.assertIsNone(self.nxt('a','profile')['job'])
        self.assertEqual(self.nxt('b','profile')['job']['handle'],'someone')

    def test_cached_main_bio_released_when_alternate_connects(self):
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('acct.a',1)")
        self.conn.execute("INSERT INTO jobs(kind,handle,priority) VALUES('profile','someone',100)")
        self.conn.commit()
        job = self.nxt('a','profile')['job']  # Main-only setup remains usable.
        self.post('b', '/api/ext/heartbeat', {'version':'3.9.17','state':'idle'})
        out = self.post('a','/api/ext/request',{'action':'acquire','kind':'profile',
                        'job_id':job['id'],'lease_token':job['lease_token']})[1]
        self.assertFalse(out['granted'])
        self.assertTrue(out['stale'])
        self.assertEqual(self.nxt('b','profile')['job']['handle'],'someone')

    def test_historical_route_wait_hands_saved_cursor_to_healthy_viewer(self):
        self.seeds('historical', direction='followers')
        job = self.nxt('a', 'list')['job']
        lanes.LaneTest.page(self, 'a', job, 5, 'next')
        self.post('b', '/api/ext/heartbeat', {'version':'3.9.17','state':'idle'})
        now = datetime.now(timezone.utc)
        self.conn.execute("INSERT INTO collector_events(at,lane,job_id,kind,direction,outcome,reason) VALUES(?,?,?,'list','followers','other','list_html_home_redirect')",
                          ((now-timedelta(minutes=40)).isoformat(), 'lane-a', job['id']))
        self.conn.commit()
        self.assertIsNone(self.nxt('a', 'list')['job'])
        handed = self.nxt('b', 'list')['job']
        self.assertIsNotNone(handed, 'Saved historical route waits must release ownership to an eligible viewer')
        self.assertEqual((handed['id'], handed['cursor'], handed['received']), (job['id'], 'next', 5))
