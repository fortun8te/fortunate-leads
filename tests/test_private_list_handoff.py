"""Access denials are scoped to a seed, direction and Instagram viewer."""
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

os.environ['FL_NO_ORSLOT'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import accounts
import db
import server


class PrivateListHandoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = db.init(Path(self.tmp.name) / 'test.sqlite')
        db.queue_list(self.c, 'seed', 'followers')
        db.set_setting(self.c, 'main_list_share', 0)
        for lane, viewer, main in [('bot', '101', False), ('bot-copy', '101', False),
                                   ('bot2', '202', False), ('main', '303', True)]:
            accounts.touch(self.c, lane, {'ig_id': viewer, 'handle': lane})
            if main:
                self.c.execute('UPDATE accounts SET is_main=1 WHERE lane_id=?', (lane,))
        self.c.commit()

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def next(self, lane):
        return server.ext_next(self.c, {'lane': [lane]}, {})['job']

    def deny(self, lane, job):
        return server.ext_error(self.c, {'lane': [lane]}, {'job_id': job['id'],
                                'lease_token': job['lease_token'], 'code': 'private',
                                'reason': 'profile_private_wall', 'message': 'verified private wall'})

    def test_bot_to_bot2_to_main_and_duplicate_identity(self):
        first = self.next('bot')
        self.assertIsNotNone(first)
        server.ext_list_page(self.c, {'lane': ['bot']}, {'job_id': first['id'], 'lease_token': first['lease_token'],
            'requested_cursor': None, 'seed': 'seed', 'direction': 'followers', 'users': [{'handle': 'alice'}],
            'next_cursor': 'saved', 'done': False, 'total': 10, 'total_source': 'current_run'})
        first = self.next('bot')
        self.deny('bot', first)
        self.assertIsNone(self.next('bot-copy'))
        self.assertIsNone(self.next('bot'))
        second = self.next('bot2')
        self.assertEqual((second['cursor'], second['received']), ('saved', 1))
        self.deny('bot2', second)
        fallback = self.next('main')
        self.assertEqual((fallback['cursor'], fallback['received']), ('saved', 1))
        self.deny('main', fallback)
        self.assertEqual(self.c.execute('SELECT state FROM lists').fetchone()[0], 'partial')
        self.assertEqual(self.c.execute('SELECT count(*) FROM list_private_denials').fetchone()[0], 3)
        self.assertIsNone(self.next('bot'))
        self.assertIsNone(self.c.execute("SELECT value FROM settings WHERE key='cooldown'").fetchone())
        self.assertEqual(self.c.execute('SELECT count(*) FROM edges').fetchone()[0], 1)

    def test_offline_main_remains_a_possible_viewer(self):
        self.c.execute('UPDATE accounts SET last_seen=? WHERE lane_id=?',
                       (accounts.iso(db.utc_now() - timedelta(hours=1)), 'main'))
        self.c.commit()
        self.deny('bot', self.next('bot'))
        self.deny('bot2', self.next('bot2'))
        self.assertEqual(self.c.execute('SELECT state FROM lists').fetchone()[0], 'queued')
        self.assertEqual(self.c.execute('SELECT state FROM jobs WHERE kind="list"').fetchone()[0], 'queued')
        job = self.next('main')
        self.assertIsNotNone(job)
        self.assertEqual(job['cursor'], None)

    def test_cooling_main_remains_a_possible_viewer(self):
        future = accounts.iso(db.utc_now() + timedelta(minutes=30))
        self.c.execute('UPDATE accounts SET list_cool_until=? WHERE lane_id=?', (future, 'main'))
        self.c.commit()
        self.deny('bot', self.next('bot'))
        self.deny('bot2', self.next('bot2'))
        self.assertEqual(self.c.execute('SELECT state FROM lists').fetchone()[0], 'queued')
        self.assertEqual(self.c.execute('SELECT state FROM jobs WHERE kind="list"').fetchone()[0], 'queued')

    def test_unconfirmed_html_redirect_delays_target_without_account_cooldown(self):
        job = self.next('bot')
        server.ext_error(self.c, {'lane': ['bot']}, {'job_id': job['id'], 'lease_token': job['lease_token'],
            'code': 'other', 'reason': 'list_html_home_redirect', 'message': 'HTML list redirect; wall unconfirmed'})
        row = self.c.execute('SELECT state,retry_not_before FROM jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual(row['state'], 'queued')
        self.assertGreater(accounts.utc(row['retry_not_before']), db.utc_now())
        self.assertIsNone(self.c.execute("SELECT value FROM settings WHERE key='cooldown'").fetchone())
        db.queue_list(self.c, 'another', 'followers')
        self.c.commit()
        self.assertEqual(self.next('bot')['seed'], 'another')

    def test_stale_denial_cannot_exclude_new_holder(self):
        job = self.next('bot')
        self.c.execute("UPDATE jobs SET state='queued',lane=NULL,lease_token=NULL,leased_until=NULL WHERE id=?", (job['id'],))
        self.c.execute('UPDATE lists SET lane=NULL')
        self.c.commit()
        fresh = self.next('bot2')
        self.assertTrue(self.deny('bot', job)['stale'])
        self.assertEqual(self.c.execute('SELECT count(*) FROM list_private_denials').fetchone()[0], 0)
        self.assertEqual(self.c.execute('SELECT lease_token FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], fresh['lease_token'])

    def test_denial_uses_identity_at_lease_not_current_login(self):
        job = self.next('bot')
        accounts.touch(self.c, 'bot', {'ig_id': '404', 'handle': 'changed'})
        self.c.commit()
        self.deny('bot', job)
        self.assertEqual(self.c.execute('SELECT viewer_ig_id FROM list_private_denials').fetchone()[0], '101')
        self.assertIsNotNone(self.next('bot'))  # new Instagram identity can try

    def test_unverified_private_response_keeps_real_soft_block_cooldown(self):
        job = self.next('bot')
        server.ext_error(self.c, {'lane': ['bot']}, {'job_id': job['id'], 'lease_token': job['lease_token'],
                         'code': 'private', 'message': 'private JSON without page proof'})
        self.assertEqual(self.c.execute('SELECT count(*) FROM list_private_denials').fetchone()[0], 0)
        self.assertIsNotNone(self.c.execute("SELECT value FROM settings WHERE key='cooldown'").fetchone())
        self.assertEqual(self.c.execute('SELECT state FROM lists').fetchone()[0], 'queued')

    def test_manual_refresh_clears_old_viewer_denials(self):
        self.deny('bot', self.next('bot'))
        self.deny('bot2', self.next('bot2'))
        self.deny('main', self.next('main'))
        self.assertFalse(db.queue_list(self.c, 'seed', 'followers'))
        self.assertTrue(db.queue_list(self.c, 'seed', 'followers', refresh=True))
        self.c.commit()
        self.assertEqual(self.c.execute('SELECT count(*) FROM list_private_denials').fetchone()[0], 0)
        self.assertIsNotNone(self.next('bot'))

    def test_capped_partial_is_not_reopened_by_an_access_denial(self):
        self.deny('bot', self.next('bot'))
        self.c.execute("UPDATE jobs SET state='done' WHERE seed='seed' AND direction='followers'")
        self.c.execute("UPDATE lists SET state='partial',released_why='instagram_cap' "
                       "WHERE seed='seed' AND direction='followers'")
        accounts.touch(self.c, 'new-viewer', {'ig_id': '505', 'handle': 'newviewer'})
        self.c.commit()
        self.assertIsNone(self.next('new-viewer'))
        self.assertEqual(self.c.execute('SELECT state FROM lists').fetchone()[0], 'partial')

    def test_main_fallback_does_not_take_ordinary_list_over_share(self):
        self.deny('bot', self.next('bot'))
        self.deny('bot2', self.next('bot2'))
        db.queue_list(self.c, 'ordinary', 'followers')
        self.c.execute("UPDATE jobs SET priority=999 WHERE seed='ordinary' AND kind='list'")
        self.c.commit()
        self.assertEqual(self.next('main')['seed'], 'seed')

    def test_login_handoff_records_source_lane(self):
        job = self.next('bot')
        server.ext_error(self.c, {'lane': ['bot']}, {'job_id': job['id'], 'lease_token': job['lease_token'],
                         'code': 'login', 'message': 'login_required'})
        self.assertEqual(self.next('bot2')['id'], job['id'])
        self.assertEqual(db.get_setting(self.c, 'handoffs')[-1]['from'], 'bot')
        self.assertEqual(db.get_setting(self.c, 'handoffs')[-1]['to'], 'bot2')


if __name__ == '__main__':
    unittest.main()
