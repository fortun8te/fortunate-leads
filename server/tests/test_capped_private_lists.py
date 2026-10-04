"""Capped follower lists are not asked again; private lists nobody can view stop looking queued."""
import os
os.environ.setdefault('FL_NO_ORSLOT', '1')

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import accounts
import db
import server


class CappedAndPrivate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.tmp.name) / 'cap.sqlite')
        self.q = {'lane': ['alt'], 'kinds': ['list'], 'version': ['3.9.30']}
        self.acct = {'lane_id': 'alt', 'account': {'ig_id': '2', 'handle': 'alternate'}}
        accounts.touch(self.conn, 'alt', {'ig_id': '2', 'handle': 'alternate'}, version='3.9.30')
        accounts.touch(self.conn, 'main', {'ig_id': '1', 'handle': 'owner'}, version='3.9.30')
        self.conn.execute("UPDATE accounts SET is_main=1 WHERE lane_id='main'")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def lease(self):
        return server.ext_next(self.conn, self.q, dict(self.acct))['job']

    def cap(self, seed='brand'):
        db.queue_list(self.conn, seed, 'followers')
        self.conn.commit()
        job = self.lease()
        body = dict(self.acct, job_id=job['id'], lease_token=job['lease_token'], seed=seed, direction='followers',
                    requested_cursor=None, users=[{'handle': f'u{i}'} for i in range(25)], next_cursor=None, done=True,
                    limited=True, has_more=False, total=5000, total_source='current_run')
        server.ext_list_page(self.conn, self.q, body)
        return job

    def lst(self, seed, direction):
        return self.conn.execute('SELECT * FROM lists WHERE seed=? AND direction=?', (seed, direction)).fetchone()

    def test_start_and_add_do_not_requeue_a_capped_follower_list(self):
        self.cap()
        before = self.lst('brand', 'followers')
        self.assertEqual((before['state'], before['received']), ('partial', 25))
        self.assertFalse(db.queue_list(self.conn, 'brand', 'followers'))
        after = self.lst('brand', 'followers')
        self.assertEqual((after['state'], after['received'], after['error']), ('partial', 25, before['error']))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE kind='list' AND direction='followers' AND state='queued'").fetchone()[0], 0)

    def test_repair_keeps_a_capped_list_parked_and_explicit_refresh_still_works(self):
        self.cap()
        self.conn.execute("UPDATE lists SET updated_at='2026-01-01T00:00:00+00:00' WHERE seed='brand'")
        db.repair_lists(self.conn)
        self.assertEqual(self.lst('brand', 'followers')['state'], 'partial')
        self.assertTrue(db.queue_list(self.conn, 'brand', 'followers', refresh=True))

    def test_a_cap_routes_the_seed_to_following(self):
        self.cap()
        following = self.lst('brand', 'following')
        self.assertIsNotNone(following)
        self.assertEqual(following['state'], 'queued')
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE seed='brand' AND direction='following' AND state='queued'").fetchone()[0], 1)

    def deny(self, seed, direction, viewer):
        self.conn.execute('INSERT INTO list_private_denials(seed,direction,viewer_ig_id,denied_at) VALUES(?,?,?,?)',
                          (seed, direction, viewer, db.now()))

    def test_private_error_is_final_when_only_the_protected_main_is_left(self):
        db.queue_list(self.conn, 'hidden', 'following')
        self.conn.commit()
        job = self.lease()
        server.ext_error(self.conn, self.q, dict(self.acct, job_id=job['id'], lease_token=job['lease_token'], code='private',
                                                  reason='profile_private_wall', kind='list', message='private'))
        self.assertEqual(self.lst('hidden', 'following')['state'], 'private')

    def test_existing_queued_denied_list_is_parked_and_labelled(self):
        db.queue_list(self.conn, 'hidden', 'followers')
        self.deny('hidden', 'followers', '2')
        self.conn.commit()
        self.assertEqual(accounts.park_denied_lists(self.conn), 1)
        self.assertEqual(self.lst('hidden', 'followers')['state'], 'private')
        self.assertIsNone(accounts.pick_job(self.conn, 'alt', ['list'], datetime.now(timezone.utc)))

    def test_list_another_account_may_still_see_stays_queued_but_reports_blocked_now(self):
        accounts.touch(self.conn, 'two', {'ig_id': '3', 'handle': 'second'}, version='3.9.30')
        self.conn.execute("UPDATE accounts SET hold='challenge' WHERE lane_id='two'")
        db.queue_list(self.conn, 'hidden', 'following')
        self.deny('hidden', 'following', '2')
        self.conn.commit()
        self.assertEqual(accounts.park_denied_lists(self.conn), 0)          # 'two' may still see it later
        self.assertEqual(self.lst('hidden', 'following')['state'], 'queued')
        lists, _ = server.get_application().collection.list_coverage(self.conn)
        row = next(r for r in lists if r['seed'] == 'hidden')
        self.assertEqual(row['completion'], 'blocked')                       # not counted as work waiting

if __name__ == '__main__':
    unittest.main()
