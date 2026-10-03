from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from test_server import Base, db, server
import accounts


class PermitApiTest(Base):
    def lease(self, lane='lane1', version='3.9.17'):
        self.call('/api/scraper/seeds', {'handles': ['one', 'two'], 'directions': ['followers']})
        return self.call(f'/api/ext/next?lane={lane}&version={version}')[1]

    def request(self, job, lane='lane1', **extra):
        return self.call('/api/ext/request', dict(action='acquire', kind='list', lane_id=lane,
                         job_id=job['id'], lease_token=job['lease_token'], **extra))

    def test_shared_hold_denies_even_a_valid_cached_job_and_old_clients_must_upgrade(self):
        old = self.lease(version='3.9.16')
        self.assertTrue(old['upgrade_required'])
        self.assertEqual(old['minimum_version'], '3.9.17')
        self.assertIsNone(old['job'])
        job = self.lease()['job']
        db.set_setting(self.conn, 'cooldown', '2099-01-01T00:00:00Z')
        self.conn.commit()
        code, result = self.request(job)
        self.assertEqual(code, 200)
        self.assertFalse(result['granted'])
        self.assertIsNone(db.get_setting(self.conn, 'instagram_request_gate'))

    def test_valid_waiting_job_is_renewed_but_expired_or_mismatched_job_never_gets_permit(self):
        job = self.lease()['job']
        self.conn.execute('UPDATE jobs SET leased_until=? WHERE id=?',
                          ((datetime.now(timezone.utc)+timedelta(seconds=20)).isoformat(), job['id']))
        self.conn.commit()
        _, result = self.request(job)
        self.assertTrue(result['lease_renewed'])
        self.assertTrue(result['granted'])
        until = accounts.utc(self.conn.execute('SELECT leased_until FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])
        self.assertGreater(until, datetime.now(timezone.utc)+timedelta(minutes=9))
        self.call('/api/ext/request', {'action':'release', 'lane_id':'lane1','token':result['token']})
        self.conn.execute("UPDATE jobs SET leased_until='2000-01-01T00:00:00Z' WHERE id=?", (job['id'],))
        self.conn.commit()
        _, rejected = self.request(job)
        self.assertFalse(rejected['granted'])
        self.assertTrue(rejected['stale'])

    def test_waiting_beyond_local_eight_minute_ttl_keeps_owned_job_valid(self):
        job = self.lease()['job']
        start = datetime.now(timezone.utc)
        # A busy queue keeps denying this lane; its valid job must survive the wait.
        db.set_setting(self.conn, 'instagram_request_gate', {
            'active': {'lane': 'other', 'kind': 'list', 'token': 'other',
                       'until': (start + timedelta(minutes=12)).timestamp()},
            'queue': [], 'next_at': 0})
        self.conn.commit()
        for minute in range(10):
            now = start + timedelta(minutes=minute)
            self.conn.execute('UPDATE accounts SET last_seen=? WHERE lane_id=?', (accounts.iso(now), 'lane1'))
            self.conn.commit()
            with patch.object(server, 'datetime', wraps=datetime) as clock:
                clock.now.return_value = now
                result = server.ext_request(self.conn, {}, dict(action='acquire', kind='list',
                    lane_id='lane1', job_id=job['id'], lease_token=job['lease_token']))
            self.assertFalse(result['granted'])
            self.assertTrue(result['lease_renewed'])
            until = accounts.utc(self.conn.execute('SELECT leased_until FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])
            self.assertGreater(until, now + timedelta(minutes=9))

    def test_stopped_stage_denies_permit_without_releasing_existing_token(self):
        job = self.lease()['job']
        result = self.request(job)[1]
        self.assertTrue(result['granted'])
        db.set_setting(self.conn, 'paused_lists', True)
        self.conn.commit()
        self.assertFalse(self.request(job)[1]['granted'])
        self.assertEqual(db.get_setting(self.conn, 'instagram_request_gate')['active']['token'], result['token'])

    def test_account_limit_changed_after_leasing_blocks_actual_request(self):
        job = self.lease()['job']
        self.conn.execute('UPDATE accounts SET budget=?, today=? WHERE lane_id=?',
                          ('{"list":200,"profile":100}', '{"list":200}', 'lane1'))
        self.conn.commit()
        code, result = self.request(job)
        self.assertEqual(code, 200)
        self.assertFalse(result['granted'])
        self.assertIsNone(db.get_setting(self.conn, 'instagram_request_gate')['active'])
        self.assertEqual(self.conn.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'leased')

    def test_following_scope_blocks_cached_follower_lease_without_losing_cursor(self):
        job = self.lease()['job']
        self.conn.execute("UPDATE lists SET cursor='saved-cursor' WHERE seed=? AND direction='followers'", (job['seed'],))
        db.set_setting(self.conn, 'follower_lists', False)
        self.conn.commit()
        code, result = self.request(job)
        self.assertEqual(code, 200)
        self.assertFalse(result['granted'])
        self.assertTrue(result['stale'])
        self.assertEqual(self.conn.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'queued')
        self.assertEqual(self.conn.execute("SELECT cursor FROM lists WHERE seed=? AND direction='followers'", (job['seed'],)).fetchone()[0], 'saved-cursor')
        self.assertIsNone(db.get_setting(self.conn, 'instagram_request_gate'))
