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
            accounts.touch(self.c, lane, {'ig_id': viewer, 'handle': lane}, version='3.9.27')
            if main:
                self.c.execute('UPDATE accounts SET is_main=1 WHERE lane_id=?', (lane,))
        self.c.commit()

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def next(self, lane):
        return server.ext_next(self.c, {'lane': [lane], 'version': ['3.9.27'], 'kinds': ['list']}, {})['job']

    def deny(self, lane, job):
        return server.ext_error(self.c, {'lane': [lane]}, {'job_id': job['id'],
                                'lease_token': job['lease_token'], 'code': 'private',
                                'reason': 'profile_private_wall', 'message': 'verified private wall'})

    def test_alternates_preserve_cursor_and_main_stays_protected(self):
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
        self.assertIsNone(self.next('main'))
        saved = self.c.execute('SELECT cursor,received FROM lists').fetchone()
        self.assertEqual(tuple(saved), ('saved', 1))
        self.assertEqual(self.c.execute('SELECT count(*) FROM list_private_denials').fetchone()[0], 2)
        self.assertIsNone(self.next('bot'))
        self.assertIsNone(self.c.execute("SELECT value FROM settings WHERE key='cooldown'").fetchone())
        self.assertEqual(self.c.execute('SELECT count(*) FROM edges').fetchone()[0], 1)

    def test_offline_main_is_never_automated(self):
        self.c.execute('UPDATE accounts SET last_seen=? WHERE lane_id=?',
                       (accounts.iso(db.utc_now() - timedelta(hours=1)), 'main'))
        self.c.commit()
        self.deny('bot', self.next('bot'))
        self.deny('bot2', self.next('bot2'))
        self.assertEqual(self.c.execute('SELECT state FROM lists').fetchone()[0], 'queued')
        self.assertEqual(self.c.execute('SELECT state FROM jobs WHERE kind="list"').fetchone()[0], 'queued')
        self.assertIsNone(self.next('main'))

    def test_cooling_main_is_never_automated(self):
        future = accounts.iso(db.utc_now() + timedelta(minutes=30))
        self.c.execute('UPDATE accounts SET list_cool_until=? WHERE lane_id=?', (future, 'main'))
        self.c.commit()
        self.deny('bot', self.next('bot'))
        self.deny('bot2', self.next('bot2'))
        self.assertEqual(self.c.execute('SELECT state FROM lists').fetchone()[0], 'queued')
        self.assertEqual(self.c.execute('SELECT state FROM jobs WHERE kind="list"').fetchone()[0], 'queued')
        self.assertIsNone(self.next('main'))

    def test_unconfirmed_html_redirect_preserves_target_and_holds_workspace(self):
        job = self.next('bot')
        server.ext_error(self.c, {'lane': ['bot']}, {'job_id': job['id'], 'lease_token': job['lease_token'],
            'code': 'other', 'reason': 'list_html_home_redirect', 'message': 'HTML list redirect; wall unconfirmed'})
        row = self.c.execute('SELECT state,retry_not_before FROM jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual(row['state'], 'queued')
        self.assertGreater(accounts.utc(row['retry_not_before']), db.utc_now())
        self.assertGreater(accounts.utc(db.get_setting(self.c, 'cooldown')), db.utc_now())
        db.queue_list(self.c, 'another', 'followers')
        self.c.commit()
        self.assertIsNone(self.next('bot'))
        self.assertIsNone(self.next('bot2'))

    def test_repeated_html_redirect_does_not_permanently_fail_unknown_access(self):
        for _ in range(6):
            job = self.next('bot')
            self.assertIsNotNone(job)
            server.ext_error(self.c, {'lane': ['bot']}, {'job_id': job['id'],
                'lease_token': job['lease_token'], 'code': 'other',
                'reason': 'list_html_home_redirect', 'message': 'wall unconfirmed'})
            row = self.c.execute('SELECT state,retry_not_before FROM jobs WHERE id=?', (job['id'],)).fetchone()
            self.assertEqual(row['state'], 'queued')
            self.assertIsNotNone(row['retry_not_before'])
            self.assertEqual(self.c.execute('SELECT state FROM lists').fetchone()[0], 'queued')
            self.assertIsNone(self.next('bot2'))
            # Advance saved deadlines in this isolated fixture only. Production
            # must preserve the full workspace and endpoint waits.
            db.set_setting(self.c, 'cooldown', '2000-01-01T00:00:00Z')
            db.set_setting(self.c, 'followers_route_until', '2000-01-01T00:00:00Z')
            self.c.execute("UPDATE accounts SET list_endpoint_until='2000-01-01T00:00:00Z'")
            self.c.execute("UPDATE account_identity_state SET list_endpoint_until='2000-01-01T00:00:00Z'")
            self.c.execute("UPDATE collector_events SET at='2000-01-01T00:00:00Z'")
            self.c.execute('UPDATE jobs SET retry_not_before=NULL WHERE id=?', (job['id'],))
            self.c.commit()

    def test_stale_denial_cannot_exclude_new_holder(self):
        job = self.next('bot')
        self.c.execute("UPDATE jobs SET state='queued',lane=NULL,lease_token=NULL,leased_until=NULL WHERE id=?", (job['id'],))
        self.c.execute('UPDATE lists SET lane=NULL')
        self.c.commit()
        fresh = self.next('bot2')
        self.assertTrue(self.deny('bot', job)['stale'])
        self.assertEqual(self.c.execute('SELECT count(*) FROM list_private_denials').fetchone()[0], 0)
        self.assertEqual(self.c.execute('SELECT lease_token FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], fresh['lease_token'])

    def test_identity_switch_rejects_old_denial(self):
        job = self.next('bot')
        accounts.touch(self.c, 'bot', {'ig_id': '404', 'handle': 'changed'})
        self.c.commit()
        self.assertTrue(self.deny('bot', job)['stale'])
        self.assertEqual(self.c.execute('SELECT count(*) FROM list_private_denials').fetchone()[0], 0)
        self.assertIsNotNone(self.next('bot'))

    def test_unverified_private_response_keeps_real_soft_block_cooldown(self):
        job = self.next('bot')
        server.ext_error(self.c, {'lane': ['bot']}, {'job_id': job['id'], 'lease_token': job['lease_token'],
                         'code': 'private', 'message': 'private JSON without page proof'})
        self.assertEqual(self.c.execute('SELECT count(*) FROM list_private_denials').fetchone()[0], 0)
        self.assertIsNotNone(self.c.execute("SELECT value FROM settings WHERE key='cooldown'").fetchone())
        self.assertEqual(self.c.execute('SELECT state FROM lists').fetchone()[0], 'queued')

    def test_manual_refresh_clears_old_viewer_denials(self):
        # A terminal private result requires all registered viewers to be denied.
        # This fixture has only the two alternate identities, plus their duplicate.
        self.c.execute("DELETE FROM accounts WHERE lane_id='main'")
        self.c.commit()
        self.deny('bot', self.next('bot'))
        self.deny('bot2', self.next('bot2'))
        self.assertEqual(self.c.execute('SELECT state FROM lists').fetchone()[0], 'private')
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

    def test_main_never_takes_private_or_ordinary_lists(self):
        self.deny('bot', self.next('bot'))
        self.deny('bot2', self.next('bot2'))
        db.queue_list(self.c, 'ordinary', 'followers')
        self.c.execute("UPDATE jobs SET priority=999 WHERE seed='ordinary' AND kind='list'")
        self.c.commit()
        self.assertIsNone(self.next('main'))
        db.set_setting(self.c, 'main_list_share', 1)
        self.c.commit()
        self.assertIsNone(self.next('main'))

    def test_login_warning_stops_handoff_until_operator_recovers(self):
        job = self.next('bot')
        server.ext_error(self.c, {'lane': ['bot']}, {'job_id': job['id'], 'lease_token': job['lease_token'],
                         'code': 'login', 'message': 'login_required'})
        self.assertIsNone(self.next('bot2'))
        self.assertTrue(db.get_setting(self.c, 'paused_lists'))
        self.assertTrue(db.get_setting(self.c, 'paused_bios'))
        self.assertEqual(self.c.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'queued')


if __name__ == '__main__':
    unittest.main()
