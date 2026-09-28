"""Mobile collectors cannot borrow Chrome lanes, cursors, or unbound jobs."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import json
import accounts
import db
import server
import test_server_accounts as lanes


class MobileBackendFenceTest(lanes.Base):
    post = lanes.LaneTest.post
    nxt = lanes.LaneTest.nxt
    seeds = lanes.LaneTest.seeds

    def setUp(self):
        super().setUp()
        self.post('a','/api/ext/heartbeat',{'version':'3.9.21','state':'idle'})
        self.conn.execute("UPDATE accounts SET collection_backend='mobile',version='mobile-1' WHERE lane_id='lane-a'")
        db.set_setting(self.conn,'mobile_backend_enabled',True)
        db.upsert_person(self.conn,{'ig_id':'909','handle':'fresh','is_private':False,'following':2})
        self.conn.commit()

    def mobile_post(self,path,body=None,viewer='101',lane='lane-a'):
        return self.call(path,dict(body or {},backend='mobile',lane_id=lane,account={'ig_id':viewer,'handle':'acct.a'}))

    def mobile_next(self,viewer='101',lane='lane-a'):
        return self.call(f'/api/ext/next?backend=mobile&lane={lane}&ig_id={viewer}&kinds=list&version=mobile-1')

    def queue(self):
        code,out=self.mobile_post('/api/mobile/queue',{'seed':'fresh','direction':'following','count':200})
        self.assertEqual(code,200,out)
        return out['job']['id']

    def test_chrome_rejected_before_touch_on_mobile_lane(self):
        before=dict(self.conn.execute("SELECT * FROM accounts WHERE lane_id='lane-a'").fetchone())
        for path,body in (('/api/ext/heartbeat',{'account':{'ig_id':'wrong'},'hold':None}),
                          ('/api/ext/error',{'code':'rate_limit'}),
                          ('/api/ext/profile',{'profile':{'ig_id':'888','handle':'wrong','bio':'wrong'}})):
            with self.subTest(path=path):
                status,_=self.post('a',path,body)
                self.assertEqual(status,400)
        self.assertEqual(self.call('/api/ext/next?lane=lane-a&ig_id=999&version=3.9.21')[0],400)
        self.assertEqual(dict(self.conn.execute("SELECT * FROM accounts WHERE lane_id='lane-a'").fetchone()),before)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM collector_events').fetchone()[0],0)
        self.assertIsNone(db.get_setting(self.conn,'cooldown'))

    def test_disabled_unknown_wrong_identity_and_main_mobile_rejected(self):
        self.assertEqual(self.mobile_next(viewer='999')[0],400)
        self.assertEqual(self.mobile_next(lane='unknown')[0],400)
        db.set_setting(self.conn,'mobile_backend_enabled',False);self.conn.commit()
        self.assertEqual(self.mobile_next()[0],400)
        db.set_setting(self.conn,'mobile_backend_enabled',True)
        self.conn.execute("UPDATE accounts SET is_main=1 WHERE lane_id='lane-a'");self.conn.commit()
        self.assertEqual(self.mobile_next()[0],400)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM accounts').fetchone()[0],1)

    def test_readonly_preflight_and_fresh_mobile_job_marker(self):
        before=self.conn.execute("SELECT last_seen FROM accounts WHERE lane_id='lane-a'").fetchone()[0]
        code,out=self.call('/api/mobile/state?backend=mobile&lane=lane-a&ig_id=101')
        self.assertEqual(code,200,out)
        self.assertTrue(out['backend_cursor_isolated'])
        self.assertEqual(out['stages'],{'list':True,'profile':False})
        self.assertEqual(self.conn.execute("SELECT last_seen FROM accounts WHERE lane_id='lane-a'").fetchone()[0],before)
        jid=self.queue();code,out=self.mobile_next();self.assertEqual(code,200,out)
        self.assertEqual(out['backend'],'mobile')
        self.assertEqual(out['job']['id'],jid)
        self.assertEqual(out['job']['collection_backend'],'mobile')
        self.assertEqual(out['job']['backend_lane'],'lane-a')
        self.assertEqual(out['job']['backend_viewer_ig_id'],'101')
        self.assertEqual(out['job']['viewer_ig_id'],'101')

    def test_jobs_do_not_cross_backends_or_pinned_lanes(self):
        self.seeds('chrome_only',direction='following')
        self.assertIsNone(self.mobile_next()[1]['job'])
        jid=self.queue()
        self.post('b','/api/ext/heartbeat',{'version':'3.9.21'})
        chrome=self.nxt('b','list')['job'];self.assertEqual(chrome['seed'],'chrome_only')
        self.conn.execute("UPDATE accounts SET collection_backend='mobile',version='mobile-1' WHERE lane_id='lane-b'")
        self.conn.commit()
        self.assertIsNone(self.mobile_next(viewer='102',lane='lane-b')[1]['job'])
        leased=self.mobile_next()[1]['job']
        self.assertEqual(leased['id'],jid)
        self.assertEqual(self.mobile_post('/api/ext/error',
            {'job_id':jid,'lease_token':leased['lease_token'],'code':'rate_limit','kind':'list'},
            viewer='102',lane='lane-b')[0],400)
        self.assertIsNone(db.get_setting(self.conn,'cooldown'))

    def test_mobile_permit_and_page_keep_shared_gate_and_provenance(self):
        self.queue();job=self.mobile_next()[1]['job']
        body={'action':'acquire','kind':'list','job_id':job['id'],'lease_token':job['lease_token']}
        code,grant=self.mobile_post('/api/ext/request',body)
        self.assertEqual(code,200,grant);self.assertTrue(grant['granted'])
        self.assertEqual(db.get_setting(self.conn,'instagram_request_gate')['active']['lane'],'lane-a')
        page={'job_id':job['id'],'lease_token':job['lease_token'],'seed':'fresh','ig_id':'909',
              'direction':'following','requested_cursor':None,'next_cursor':None,'done':True,'has_more':False,
              'total':2,'total_source':'current_run','requested_count':200,
              'users':[{'ig_id':'801','handle':'member_one'},{'ig_id':'802','handle':'member_two'}]}
        self.assertEqual(self.post('a','/api/ext/list-page',page)[0],400)
        self.assertEqual(self.mobile_post('/api/ext/list-page',page,viewer='999')[0],400)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM list_members WHERE job_id=?',(job['id'],)).fetchone()[0],0)
        code,out=self.mobile_post('/api/ext/list-page',page);self.assertEqual(code,200,out)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM list_members WHERE job_id=?',(job['id'],)).fetchone()[0],2)
        db.set_setting(self.conn,'mobile_backend_enabled',False);self.conn.commit()
        code,released=self.mobile_post('/api/ext/request',{'action':'release','token':grant['token']})
        self.assertEqual(code,200,released);self.assertTrue(released['released'])

    def test_mobile_cannot_submit_chrome_result_or_warning(self):
        self.seeds('chrome_only',direction='following')
        self.post('b','/api/ext/heartbeat',{'version':'3.9.21'})
        job=self.nxt('b','list')['job']
        for path,body in (('/api/ext/error',{'code':'rate_limit','kind':'list'}),
                          ('/api/ext/request',{'action':'acquire','kind':'list'}),
                          ('/api/ext/list-page',{'seed':'chrome_only','direction':'following','users':[]})):
            with self.subTest(path=path):
                status,_=self.mobile_post(path,dict(body,job_id=job['id'],lease_token=job['lease_token']))
                self.assertEqual(status,400)
        self.assertIsNone(db.get_setting(self.conn,'cooldown'))
        self.assertEqual(self.conn.execute('SELECT state FROM jobs WHERE id=?',(job['id'],)).fetchone()[0],'leased')

    def test_mobile_rate_limit_and_security_hold_stop_other_routes(self):
        self.queue();job=self.mobile_next()[1]['job']
        body={'job_id':job['id'],'lease_token':job['lease_token'],'kind':'list','code':'rate_limit',
              'reason':'http_429','http_status':429}
        code,out=self.mobile_post('/api/ext/error',body);self.assertEqual(code,200,out)
        self.assertIsNotNone(server.workspace_cooldown(self.conn,datetime.now(timezone.utc)))
        self.assertIsNone(self.nxt('b','list')['job'])
        self.assertIsNone(self.mobile_next()[1]['job'])
        self.conn.execute("UPDATE accounts SET hold='challenge' WHERE lane_id='lane-a'");self.conn.commit()
        code,_=self.mobile_post('/api/ext/heartbeat',{'version':'mobile-1','hold':None})
        self.assertEqual(code,200)
        self.assertEqual(self.conn.execute("SELECT hold FROM accounts WHERE lane_id='lane-a'").fetchone()[0],'challenge')

    def test_disabled_after_issue_still_accepts_bound_warning_but_not_new_permit(self):
        self.queue();job=self.mobile_next()[1]['job']
        binding={'job_id':job['id'],'lease_token':job['lease_token'],'kind':'list'}
        self.assertTrue(self.mobile_post('/api/ext/request',dict(binding,action='acquire'))[1]['granted'])
        db.set_setting(self.conn,'mobile_backend_enabled',False);self.conn.commit()
        self.assertEqual(self.mobile_post('/api/ext/request',dict(binding,action='acquire'))[0],400)
        self.assertEqual(self.mobile_post('/api/ext/error',dict(binding,lease_token='wrong',code='rate_limit',reason='http_429',http_status=429))[0],400)
        self.assertIsNone(db.get_setting(self.conn,'cooldown'))
        code,out=self.mobile_post('/api/ext/error',dict(binding,code='rate_limit',reason='http_429',http_status=429))
        self.assertEqual(code,200,out)
        self.assertIsNotNone(server.workspace_cooldown(self.conn,datetime.now(timezone.utc)))

    def test_disabled_after_issue_still_accepts_bound_checkpoint(self):
        self.queue();job=self.mobile_next()[1]['job']
        binding={'job_id':job['id'],'lease_token':job['lease_token'],'kind':'list'}
        self.assertTrue(self.mobile_post('/api/ext/request',dict(binding,action='acquire'))[1]['granted'])
        db.set_setting(self.conn,'mobile_backend_enabled',False);self.conn.commit()
        page={'job_id':job['id'],'lease_token':job['lease_token'],'seed':'fresh','ig_id':'909',
              'direction':'following','requested_cursor':None,'next_cursor':'more','done':False,'has_more':True,
              'total':2,'total_source':'current_run','requested_count':200,
              'users':[{'ig_id':'801','handle':'member_one'}]}
        code,out=self.mobile_post('/api/ext/list-page',page);self.assertEqual(code,200,out)
        self.assertEqual(self.conn.execute('SELECT member_count FROM list_runs WHERE job_id=?',(job['id'],)).fetchone()[0],1)
        self.assertEqual(self.mobile_next()[0],400)

    def test_mobile_permit_charges_budget_and_stale_heartbeat_cannot_reset_it(self):
        self.conn.execute("UPDATE accounts SET budget=? WHERE lane_id='lane-a'",('{"list":1}',));self.conn.commit()
        self.queue();job=self.mobile_next()[1]['job']
        binding={'action':'acquire','job_id':job['id'],'lease_token':job['lease_token'],'kind':'list'}
        grant=self.mobile_post('/api/ext/request',binding)[1];self.assertTrue(grant['granted'])
        self.assertFalse(self.mobile_post('/api/ext/request',binding)[1]['granted'])
        self.assertTrue(self.mobile_post('/api/ext/request',{'action':'release','token':grant['token']})[1]['released'])
        self.mobile_post('/api/ext/heartbeat',{'version':'mobile-1','today':{'list':0,'profile':0}})
        row=self.conn.execute("SELECT * FROM accounts WHERE lane_id='lane-a'").fetchone()
        self.assertEqual(json.loads(row['today'])['list'],1)
        self.assertFalse(accounts.request_budget_left(self.conn,row,'list',datetime.now(timezone.utc)))
        self.assertFalse(self.mobile_post('/api/ext/request',binding)[1]['granted'])
        self.assertEqual(json.loads(self.conn.execute("SELECT today FROM account_identity_state WHERE lane_id='lane-a' AND ig_id='101'").fetchone()[0])['list'],1)

    def test_failed_budget_write_cannot_leave_a_committed_mobile_permit(self):
        self.queue();job=self.mobile_next()[1]['job']
        binding={'action':'acquire','job_id':job['id'],'lease_token':job['lease_token'],'kind':'list'}
        with patch.object(accounts,'touch',side_effect=ValueError('counter unavailable')):
            self.assertEqual(self.mobile_post('/api/ext/request',binding)[0],400)
        self.assertFalse((db.get_setting(self.conn,'instagram_request_gate') or {}).get('active'))
        self.assertEqual(json.loads(self.conn.execute("SELECT today FROM accounts WHERE lane_id='lane-a'").fetchone()[0] or '{}').get('list',0),0)

    def test_mobile_spacing_survives_release_and_new_client_poll(self):
        self.queue();job=self.mobile_next()[1]['job']
        binding={'action':'acquire','job_id':job['id'],'lease_token':job['lease_token'],'kind':'list'}
        grant=self.mobile_post('/api/ext/request',binding)[1]
        self.assertTrue(grant['granted'])
        self.mobile_post('/api/ext/request',{'action':'release','token':grant['token']})
        # Even if the global slot is free, a different process cannot reset the
        # same Instagram identity's persisted minimum interval.
        db.set_setting(self.conn,'instagram_request_gate',{})
        self.conn.commit()
        denied=self.mobile_post('/api/ext/request',binding)[1]
        self.assertFalse(denied['granted'])
        self.assertGreater(denied['wait_ms'],1000)
        self.assertIsNotNone(db.get_setting(self.conn,'mobile_request_after:101'))
        self.assertEqual(json.loads(self.conn.execute("SELECT today FROM accounts WHERE lane_id='lane-a'").fetchone()[0])['list'],1)
