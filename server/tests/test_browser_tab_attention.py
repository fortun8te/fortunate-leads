"""Observed Instagram login/security pages outrank retained job leases."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server import Base
import db


class BrowserTabAttentionTests(Base):
    def beat(self, **fields):
        body = dict(lane_id='checked-lane', account={'ig_id': '101', 'handle': 'checked.account'},
                    version='3.9.28', state='idle', hold=None, **fields)
        code, out = self.call('/api/ext/heartbeat', body)
        self.assertEqual(code, 200, out)

    def account(self):
        return next(a for a in self.call('/api/accounts')[1]['accounts'] if a['lane_id'] == 'checked-lane')

    def test_tab_challenge_is_not_healthy_or_collecting_and_queue_poll_cannot_clear_it(self):
        self.beat(tab='ok')
        pid = db.upsert_person(self.conn, {'ig_id': '7', 'handle': 'retained.bio'})
        self.conn.commit()
        self.call(f'/api/person/{pid}/read', {})
        leased = self.call('/api/ext/next?lane=checked-lane&ig_id=101&handle=checked.account&kinds=profile')[1]
        self.assertEqual(leased['job']['handle'], 'retained.bio')
        self.beat(tab='tab_challenge', text='Instagram tab shows a security check')
        self.assertEqual(self.account()['status'], 'challenge')
        self.assertFalse(self.account()['healthy'])
        code, result = self.call('/api/ext/next?lane=checked-lane&ig_id=101&handle=checked.account')
        self.assertEqual(code, 200)
        self.assertIsNone(result['job'])
        self.assertEqual(self.account()['hold'], 'challenge')
        control = next(a for a in self.call('/api/control')[1]['accounts'] if a['lane_id'] == 'checked-lane')
        self.assertEqual(control['state'], 'waiting')
        self.assertIn('security check', control['now'])
        self.beat(tab='ok', text='No other work ready')
        self.assertIsNone(self.account()['hold'])
        self.assertNotEqual(self.account()['status'], 'challenge')

    def test_legacy_exact_security_text_and_login_tab_are_reported(self):
        self.beat(text='Instagram tab shows a security check')
        self.assertEqual(self.account()['status'], 'challenge')
        self.beat(tab='tab_login', text='Instagram tab is on the login page')
        self.assertEqual(self.account()['status'], 'needs_login')
        self.beat(tab='ok', text='Previous security check completed')
        self.assertIsNone(self.account()['hold'])

    def test_explicit_usable_tab_beats_stale_legacy_copy(self):
        self.beat(tab='ok', text='Instagram tab shows a security check')
        self.assertIsNone(self.account()['hold'])

    def test_identity_change_preserves_the_new_identity_security_observation(self):
        self.beat(tab='ok')
        code, _ = self.call('/api/ext/heartbeat', {'lane_id': 'checked-lane',
            'account': {'ig_id': '102', 'handle': 'other.account'}, 'version': '3.9.28',
            'state': 'idle', 'hold': None, 'tab': 'tab_challenge'})
        self.assertEqual(code, 200)
        self.assertEqual(self.account()['hold'], 'challenge')

    def test_network_retry_is_visible_instead_of_a_retained_job_being_called_active(self):
        self.beat(tab='ok')
        pid = db.upsert_person(self.conn, {'ig_id': '7', 'handle': 'retry.bio'})
        self.conn.commit()
        self.call(f'/api/person/{pid}/read', {})
        self.call('/api/ext/next?lane=checked-lane&ig_id=101&handle=checked.account&kinds=profile')
        code, _ = self.call('/api/ext/heartbeat', {'lane_id': 'checked-lane',
            'account': {'ig_id': '101', 'handle': 'checked.account'}, 'version': '3.9.29',
            'state': 'network_wait', 'hold': None, 'tab': 'ok', 'text': 'Retrying the Instagram connection'})
        self.assertEqual(code, 200)
        self.assertEqual(self.account()['status'], 'connection_error')
        self.assertFalse(self.account()['healthy'])
        body = self.call('/api/control')[1]
        self.assertEqual(next(a for a in body['accounts'] if a['lane_id'] == 'checked-lane')['state'], 'waiting')
        self.assertNotEqual(next(stage for stage in body['stages'] if stage['id'] == 'bios')['state'], 'running')
        self.beat(tab='ok', text='Ready')
        self.assertNotEqual(self.account()['status'], 'connection_error')

    def test_scraping_warning_is_sticky_and_requires_separate_review(self):
        self.beat(tab='tab_scraping_warning')
        self.assertTrue(db.get_setting(self.conn, 'instagram_scraping_warning'))
        self.beat(tab='ok')
        self.assertEqual(self.account()['hold'], 'challenge')
        self.assertEqual(self.call('/api/control', {'action': 'resume', 'stage': 'collection'})[0], 400)
        acknowledgment = {'action': 'acknowledge_scraping_warning', 'account': 'checked-lane', 'reviewed': True}
        self.assertEqual(self.call('/api/control', acknowledgment)[0], 400)
        self.call('/api/ext/heartbeat', {'lane_id':'checked-lane', 'account':{'ig_id':'101'}, 'version':'3.9.30', 'tab':'ok', 'hold':None})
        self.assertEqual(self.account()['hold'], 'challenge')
        self.call('/api/ext/heartbeat', {'lane_id':'checked-lane', 'account':{'ig_id':'999'}, 'version':'3.9.30', 'tab':'ok', 'hold':None})
        self.assertEqual(self.call('/api/control', acknowledgment)[0], 400)
        self.call('/api/ext/heartbeat', {'lane_id':'checked-lane', 'account':{'ig_id':'101'}, 'version':'3.9.30', 'tab':'ok', 'hold':None})
        self.assertEqual(self.call('/api/control', acknowledgment)[0], 200)
        self.assertIsNone(db.get_setting(self.conn, 'instagram_scraping_warning'))
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
        self.assertTrue(db.get_setting(self.conn, 'paused_bios'))

    def test_legacy_error_warning_url_sets_manual_hold_but_unrelated_host_does_not(self):
        self.beat(tab='ok')
        body = {'lane_id':'checked-lane', 'account':{'ig_id':'101'}, 'code':'other', 'reason':'no_profile_data', 'kind':'profile'}
        self.call('/api/ext/error', dict(body, message='other https://example.com/accounts/scraping_warning/'))
        self.assertFalse(db.get_setting(self.conn, 'instagram_scraping_warning'))
        self.call('/api/ext/error', dict(body, message='other https://www.instagram.com/accounts/scraping_warning/?challenge_context=abc'))
        self.assertTrue(db.get_setting(self.conn, 'instagram_scraping_warning'))
        self.beat(tab='ok')
        self.call('/api/ext/next?lane=checked-lane&ig_id=101&handle=checked.account')
        self.assertEqual(self.account()['hold'], 'challenge')

    def test_multiple_warning_accounts_require_separate_reviews_and_are_stopped(self):
        self.beat(tab='tab_scraping_warning')
        self.call('/api/ext/heartbeat', {'lane_id':'second-lane', 'account':{'ig_id':'202'}, 'version':'3.9.30', 'tab':'tab_scraping_warning'})
        for lane, uid in [('checked-lane','101'),('second-lane','202')]:
            self.call('/api/ext/heartbeat', {'lane_id':lane, 'account':{'ig_id':uid}, 'version':'3.9.30', 'tab':'ok', 'hold':None})
        control = self.call('/api/control')[1]
        self.assertTrue(control['collection']['stop_acknowledged'])
        self.assertEqual(self.call('/api/control', {'action':'acknowledge_scraping_warning','account':'checked-lane','reviewed':True})[0],200)
        self.assertEqual(db.get_setting(self.conn,'instagram_scraping_warning')['lane'],'second-lane')
        self.assertEqual(self.call('/api/control', {'action':'resume','stage':'collection'})[0],400)
        self.call('/api/ext/heartbeat', {'lane_id':'second-lane','account':{'ig_id':'202'},'version':'3.9.30','tab':'ok','hold':None})
        self.assertEqual(self.conn.execute("SELECT hold FROM accounts WHERE lane_id='second-lane'").fetchone()[0],'challenge')
        self.assertEqual(self.call('/api/control', {'action':'acknowledge_scraping_warning','account':'second-lane','reviewed':True})[0],200)
        self.assertIsNone(db.get_setting(self.conn,'instagram_scraping_warning'))
        self.assertEqual(set(db.get_setting(self.conn,'instagram_scraping_warning_reviewed')),{'checked-lane','second-lane'})
        self.assertTrue(db.get_setting(self.conn,'paused_lists'))
