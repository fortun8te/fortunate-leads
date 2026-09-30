"""Account readiness and explicit abandonment of an expired benchmark."""
import copy
import time
from datetime import datetime, timedelta, timezone

from test_server import Base
import db
import onboarding


class SetupRecoveryTest(Base):
    def seed_benchmark(self, active=False):
        now = time.time()
        self.cfg = {'enabled': True, 'lane_id': 'old-lane', 'viewer_id': '55', 'path': '/not-opened.sqlite',
                    'inflight': {'at': now - (10 if active else 200), 'token': 'expired-token', 'request_id': 'request-1'},
                    'attempts': 60}
        self.gate = {'active': {'until': now + 90 if active else now - 100, 'lane': 'old-lane',
                                'token': 'expired-token', 'kind': 'list'}, 'queue': [], 'next_at': now - 90}
        db.set_setting(self.conn, 'raw_edge_benchmark', self.cfg)
        db.set_setting(self.conn, 'instagram_request_gate', self.gate)
        db.set_setting(self.conn, 'instagram_request_attention', {'lane': 'old-lane', 'message': 'Unconfirmed benchmark'})
        self.conn.commit()

    def test_setup_reports_main_connection_separately_from_benchmark_and_pause(self):
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('fortun8te',1)")
        self.conn.commit()
        self.call('/api/ext/heartbeat', {'lane_id': 'main', 'account': {'ig_id': '123', 'handle': 'fortun8te'},
                                        'version': '3.9.27', 'state': 'paused'})
        self.seed_benchmark()
        instagram = self.call('/api/setup')[1]['instagram']
        self.assertEqual(instagram['state'], 'connected')
        self.assertTrue(instagram['connected'])
        self.assertEqual(instagram['ig_id'], '123')
        self.assertIn('benchmark', instagram['collection_blocker'])
        self.assertFalse(instagram['messages']['imported'])
        self.assertEqual(instagram['inbox_url'], 'https://www.instagram.com/direct/inbox/')

    def test_connection_does_not_invent_login_for_offline_or_security_check(self):
        for account, state in [({}, 'missing'), ({'online': False,'ig_id':'123'},'offline'),
                               ({'online':True,'ig_id':None},'signed_out'),
                               ({'online':True,'ig_id':'123','hold':'challenge'},'security_check')]:
            with self.subTest(state=state):
                result = onboarding.instagram_setup(account, 'fortun8te')
                self.assertEqual(result['state'],state)
                self.assertFalse(result['connected'])

    def test_expired_review_preserves_unknown_outcome_checkpoints_and_safety(self):
        self.call('/api/scraper/seeds', {'handles': ['brand'], 'directions': ['following']})
        self.conn.execute("UPDATE lists SET cursor='saved-cursor',received=42")
        hold = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        db.set_setting(self.conn,'cooldown',hold)
        self.conn.commit()
        jobs = [tuple(row) for row in self.conn.execute('SELECT * FROM jobs')]
        self.seed_benchmark()
        code, result = self.call('/api/control', {'action':'stop_benchmark','checked_account_tab':True})
        self.assertEqual(code,200)
        self.assertTrue(all(stage['paused'] for stage in result['stages'][:2]))
        cfg = db.get_setting(self.conn,'raw_edge_benchmark')
        self.assertFalse(cfg['enabled'])
        self.assertEqual(cfg['inflight'],self.cfg['inflight'])
        self.assertEqual(cfg['operator_stop']['outcome'],'abandoned_unconfirmed')
        self.assertEqual(cfg['operator_stop']['request'],self.gate['active'])
        self.assertEqual(db.get_setting(self.conn,'cooldown'),hold)
        self.assertEqual(jobs,[tuple(row) for row in self.conn.execute('SELECT * FROM jobs')])
        self.assertEqual(tuple(self.conn.execute('SELECT cursor,received FROM lists').fetchone()),('saved-cursor',42))
        self.assertIsNone(db.get_setting(self.conn,'instagram_request_gate')['active'])
        self.assertGreater(db.get_setting(self.conn,'instagram_request_gate')['next_at'],time.time())
        self.assertEqual(self.call('/api/control', {'stage':'collection','action':'resume'})[0],400)
        self.assertFalse(db.get_setting(self.conn,'qualify'))

    def test_review_requires_expiry_explicit_check_and_matching_request(self):
        cases = [('unchecked',False,False),('active',True,True),('unrelated',False,True),('invalid',False,True)]
        for case, active, checked in cases:
            with self.subTest(case=case):
                self.seed_benchmark(active)
                if case=='unrelated':
                    self.gate['active']['token']='another-token'
                if case=='invalid':
                    self.gate['active']['until']='bad-time'
                db.set_setting(self.conn,'instagram_request_gate',self.gate)
                self.conn.commit()
                before = copy.deepcopy(db.get_setting(self.conn,'raw_edge_benchmark'))
                self.assertEqual(self.call('/api/control', {'action':'stop_benchmark','checked_account_tab':checked})[0],400)
                self.assertEqual(db.get_setting(self.conn,'raw_edge_benchmark'),before)
                self.assertEqual(db.get_setting(self.conn,'instagram_request_gate'),self.gate)
