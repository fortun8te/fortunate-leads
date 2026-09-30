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
