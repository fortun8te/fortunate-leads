import os; os.environ.setdefault('FL_NO_ORSLOT', '1')  # tests never see the real key pool
"""Multi-account lanes: leasing, roles, handoff, main-account protection, migration, account endpoints."""
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server import Base  # noqa: E402  (installs the qualify stub first)

import accounts  # noqa: E402
import control  # noqa: E402
import db  # noqa: E402
import server  # noqa: E402

ACCT = {'a': ('lane-a', '101', 'acct.a'), 'b': ('lane-b', '102', 'acct.b'), 'c': ('lane-c', '103', 'acct.c')}


class LaneTest(Base):
    def nxt(self, who, kinds=None):
        lane, ig, handle = ACCT[who]
        q = f'/api/ext/next?lane={lane}&ig_id={ig}&handle={handle}' + (f'&kinds={kinds}' if kinds else '')
        return self.call(q)[1]

    def post(self, who, path, body):
        lane, ig, handle = ACCT[who]
        return self.call(path, dict(body, lane_id=lane, account={'ig_id': ig, 'handle': handle}))

    def page(self, who, job, n, cursor, done=False):
        users = [{'ig_id': f'{job["seed"]}{cursor}{i}'.replace('.', ''), 'handle': f'{job["seed"]}_{cursor}_{i}'} for i in range(n)]
        users = [dict(u, ig_id=str(abs(hash(u['ig_id'])) % 10**12)) for u in users]
        return self.post(who, '/api/ext/list-page', {'job_id': job['id'], 'seed': job['seed'], 'direction': job['direction'],
                                                     'users': users, 'next_cursor': cursor, 'done': done})

    def seeds(self, *handles, direction='followers'):
        self.call('/api/scraper/seeds', {'handles': list(handles), 'directions': [direction]})

    def age(self, lane, minutes):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec='microseconds')
        self.conn.execute('UPDATE accounts SET last_seen=? WHERE lane_id=?', (ts, lane))
        self.conn.commit()

    def test_exclusive_and_sticky(self):
        self.seeds('s1', 's2')
        ja, jb = self.nxt('a')['job'], self.nxt('b')['job']
        self.assertEqual({ja['seed'], jb['seed']}, {'s1', 's2'})   # two lanes, two different lists
        self.assertIsNone(self.nxt('c')['job'])                    # nothing is handed out twice
        self.assertEqual(self.page('a', ja, 5, 'c1')[1]['received'], 5)
        self.assertIsNone(self.nxt('c', 'list')['job'])            # a's list waits for a, not for c
        again = self.nxt('a')['job']
        self.assertEqual((again['id'], again['cursor'], again['received']), (ja['id'], 'c1', 5))
        rows = {r['lane_id']: r for r in self.conn.execute('SELECT * FROM accounts')}
        self.assertEqual((rows['lane-a']['ig_id'], rows['lane-a']['handle']), ('101', 'acct.a'))
        lanes = dict(self.conn.execute('SELECT seed, lane FROM lists').fetchall())
        self.assertEqual(lanes, {ja['seed']: 'lane-a', jb['seed']: 'lane-b'})
        self.assertEqual(self.conn.execute("SELECT lane, users FROM pages").fetchall()[0][:], ('lane-a', 5))

    def test_following_lists_first_and_label_and_share(self):
        self.seeds('s1', direction='followers')
        self.seeds('s2', direction='following')
        self.assertEqual(self.nxt('a')['job']['direction'], 'following')   # following lists are small and dense: first
        self.post('b', '/api/ext/heartbeat', {'version': '3.9.0', 'state': 'idle'})
        acct = self.call('/api/accounts/lane-b', {'label': 'Scout 2'})[1]['account']
        self.assertEqual((acct['label'], acct['name']), ('Scout 2', '@acct.b'))
        self.assertEqual(self.call('/api/accounts/lane-b', {'label': 'x' * 41})[0], 400)
        self.assertEqual(self.call('/api/settings/accounts', {'main_list_share': 1.5})[0], 400)
        self.assertEqual(self.call('/api/settings/accounts', {'main_list_share': 0.25})[1]['main_list_share'], 0.25)
        self.assertEqual(self.call('/api/accounts')[1]['main_list_share'], 0.25)

    def test_follower_redirect_pause_hands_saved_cursor_to_other_viewer(self):
        self.seeds('target', direction='followers')
        first = self.nxt('a', 'list')['job']
        self.page('a', first, 5, 'next')
        self.post('b', '/api/ext/heartbeat', {'version': '3.9.6', 'state': 'idle'})
        until = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.6', 'state': 'running',
                  'list_endpoint_until': until, 'cool': {'list': None, 'profile': None}})
        self.assertTrue(any(x.get('code') == 'list_endpoint_wait' for x in self.call('/api/accounts')[1]['alerts']))
        self.assertIsNone(self.nxt('a', 'list')['job'])
        handed = self.nxt('b', 'list')['job']
        self.assertEqual((handed['id'], handed['cursor'], handed['received']),
                         (first['id'], 'next', 5))

    def test_recent_follower_redirects_allow_one_following_probe(self):
        self.seeds('f1', 'f2', 'f3', direction='followers')
        self.seeds('other', direction='following')
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.5', 'state': 'idle'})
        now = datetime.now(timezone.utc)
        self.conn.execute("UPDATE jobs SET priority=200 WHERE direction='followers'")
        self.conn.execute("UPDATE lists SET lane='lane-a' WHERE direction='followers'")
        self.conn.execute("UPDATE lists SET error='other (list_html_home_redirect)', updated_at=? "
                          "WHERE direction='followers'", (now.isoformat(),))
        self.conn.commit()
        job = accounts.pick_job(self.conn, 'lane-a', ['list'], now)
        self.assertEqual(job['direction'], 'following')
        self.conn.execute("UPDATE lists SET error='other (list_html_home_redirect)', updated_at=? "
                          "WHERE direction='following'", (now.isoformat(),))
        self.assertEqual(accounts.pick_job(self.conn, 'lane-a', ['list'], now)['direction'], 'followers')
        self.conn.execute("INSERT INTO pages(job_id,cursor,at) VALUES(?,?,?)", (job['id'], '', now.isoformat()))
        self.assertEqual(accounts.pick_job(self.conn, 'lane-a', ['list'], now)['direction'], 'following')

    def test_roles_and_pause(self):
        pid = db.upsert_person(self.conn, {'ig_id': '7', 'handle': 'dave'})
        self.conn.commit()
        self.call(f'/api/person/{pid}/read', {})
        self.seeds('s1')
        self.nxt('a')   # a takes the list
        self.post('b', '/api/ext/heartbeat', {'version': '3.9.0', 'state': 'idle'})   # b checks in
        self.call('/api/scraper/pause', {'paused': False})
        self.assertEqual(self.call('/api/accounts/lane-b', {'role': 'lists'})[1]['account']['role'], 'lists')
        self.assertIsNone(self.nxt('b', 'profile')['job'])          # a lists-only lane never reads bios
        self.call('/api/accounts/lane-b', {'role': 'bios'})
        self.assertEqual(self.nxt('b')['job']['kind'], 'profile')   # a bios lane gets the bio, not a list
        self.assertEqual(self.call('/api/accounts/lane-c', {'role': 'x'})[0], 404)
        self.nxt('c')
        self.assertEqual(self.call('/api/accounts/lane-c', {'role': 'x'})[0], 400)
        self.assertEqual(self.call('/api/accounts/lane-c', {'paused': 'yes'})[0], 400)
        self.assertEqual(self.call('/api/accounts/lane-c', {'budget': {'list': 99999, 'profile': 20}})[0], 400)   # out of range
        r = self.call('/api/accounts/lane-c', {'paused': True, 'budget': {'list': 3000, 'profile': 20}, 'label': ' spare '})[1]['account']
        self.assertEqual((r['paused'], r['budget'], r['budget_custom'], r['label'], r['status']),
                         (True, {'list': 3000, 'profile': 20}, True, 'spare', 'paused'))
        self.assertEqual(self.nxt('c'), {'ok': True, 'paused': True, 'budget': {'list': 3000, 'profile': 20}, 'job': None,
                                         'cooldown_until': None, 'stages': {'list': True, 'profile': True}})
        hb = self.post('c', '/api/ext/heartbeat', {'version': '3.9.0', 'state': 'paused'})[1]
        self.assertEqual(hb, {'ok': True, 'paused': True, 'budget': {'list': 3000, 'profile': 20}, 'stages': {'list': True, 'profile': True}})
        self.call('/api/accounts/lane-c', {'budget': None})
        self.assertEqual(self.nxt('a')['budget'], {'list': 3000, 'profile': 300})

    def test_handoff_on_login_keeps_cursor(self):
        self.seeds('s1')
        ja = self.nxt('a')['job']
        self.nxt('b')
        self.page('a', ja, 4, 'c1')
        self.nxt('a')   # a resumes its list...
        self.post('a', '/api/ext/error', {'job_id': ja['id'], 'code': 'login', 'retry_at': None, 'message': 'login_required'})
        self.assertIsNone(self.nxt('b')['job'])
        db.set_setting(self.conn, 'cooldown', '2000-01-01T00:00:00Z')
        self.conn.commit()
        self.assertIsNone(self.nxt('b')['job'])  # expiry alone cannot resume security-paused collection
        self.assertEqual(self.call('/api/control', {'action': 'resume', 'stage': 'all'})[0], 200)
        jb = self.nxt('b')['job']     # explicit operator resume after shared hold expires
        self.assertEqual((jb['id'], jb['cursor'], jb['received']), (ja['id'], 'c1', 4))
        self.page('b', jb, 3, 'c2')
        self.assertEqual(self.conn.execute("SELECT received, lane FROM lists WHERE seed='s1'").fetchone()[:], (7, 'lane-b'))
        body = self.call('/api/accounts')[1]
        a = next(x for x in body['accounts'] if x['lane_id'] == 'lane-a')
        self.assertEqual((a['status'], a['hold']), ('needs_login', 'login'))
        self.assertIn('@acct.a logged out — open its Chrome profile and log in; its list moved to @acct.b',
                      [x['text'] for x in body['alerts']])
        self.assertEqual(db.get_setting(self.conn, 'handoffs')[-1]['why'], 'login')
        self.nxt('a')   # logged back in: asks for work again, the hold clears, the list stays with b
        self.assertIsNone(self.conn.execute("SELECT hold FROM accounts WHERE lane_id='lane-a'").fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT lane FROM lists WHERE seed='s1'").fetchone()[0], 'lane-b')

    def test_handoff_when_offline(self):
        self.seeds('s1')
        ja = self.nxt('a')['job']
        self.page('a', ja, 2, 'c1')
        self.age('lane-a', 5)
        self.assertIsNone(self.nxt('b')['job'])       # 5 min quiet: still a's
        self.age('lane-a', 11)
        jb = self.nxt('b')['job']                     # > 10 min: b resumes it
        self.assertEqual((jb['id'], jb['cursor']), (ja['id'], 'c1'))
        texts = [x['text'] for x in self.call('/api/accounts')[1]['alerts']]
        self.assertTrue(any(t.startswith('@acct.a offline for 11m — open its Chrome profile; its list moved to @acct.b') for t in texts), texts)

    def test_rate_limit_holds_all_lanes_until_workspace_wait_expires(self):
        self.seeds('s1', 's2', 's3')
        ja, jb = self.nxt('a')['job'], self.nxt('b')['job']
        self.page('a', ja, 2, 'c1')
        ja = self.nxt('a')['job']  # the next page has a fresh lease
        self.post('a', '/api/ext/error', {'job_id': ja['id'], 'code': 'rate_limit', 'retry_at': '2099-01-01T00:00:00Z', 'message': '429'})
        s = self.call('/api/scraper')[1]
        by = {a['lane_id']: a for a in s['accounts']}
        self.assertEqual(by['lane-a']['status'], 'online')  # account-level status remains separate from shared hold
        self.assertEqual(by['lane-b']['status'], 'running')   # b still holds its in-flight lease
        self.assertTrue(db.get_setting(self.conn, 'cooldown'))
        self.page('b', jb, 2, None, done=True)
        self.assertIsNone(self.nxt('b')['job'])
        self.assertIsNone(self.nxt('c', 'profile')['job'])
        self.assertEqual(self.nxt('b')['stages'], {'list': False, 'profile': False})
        db.set_setting(self.conn, 'cooldown', '2000-01-01T00:00:00Z')
        self.conn.commit()
        jb2 = self.nxt('b')['job']                     # b can take unrelated work after the wait
        self.assertEqual(jb2['seed'], 's3')
        blocked = self.conn.execute('SELECT retry_not_before FROM jobs WHERE id=?', (ja['id'],)).fetchone()[0]
        self.assertGreater(datetime.fromisoformat(blocked), datetime.now(timezone.utc))
        self.assertIsNone(self.nxt('c', 'list')['job'])
        self.conn.execute('UPDATE jobs SET retry_not_before=NULL WHERE id=?', (ja['id'],))
        self.conn.commit()
        self.page('b', jb2, 1, None, done=True)
        resumed = self.nxt('b')['job']
        self.assertEqual((resumed['seed'], resumed['cursor']), (ja['seed'], 'c1'))

    def test_workspace_wait_is_shared_persistent_and_cannot_be_shortened(self):
        self.seeds('first', 'second')
        job = self.nxt('a', 'list')['job']
        longer = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        shorter = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        self.post('a', '/api/ext/error', {'job_id': job['id'], 'code': 'soft_block',
                                         'retry_at': longer, 'message': 'feedback_required'})
        self.assertIsNone(self.nxt('b', 'list')['job'])
        heartbeat = self.post('b', '/api/ext/heartbeat', {'version': '3.9.12', 'state': 'idle'})[1]
        self.assertFalse(heartbeat['paused'])
        self.assertEqual(heartbeat['stages'], {'list': False, 'profile': False})
        self.assertEqual(heartbeat['cooldown_until'], db.get_setting(self.conn, 'cooldown'))
        self.post('b', '/api/ext/error', {'code': 'rate_limit', 'kind': 'profile',
                                         'retry_at': shorter, 'message': '429'})
        self.assertGreaterEqual(datetime.fromisoformat(db.get_setting(self.conn, 'cooldown')),
                                datetime.fromisoformat(longer))
        # The hold is read from SQLite, so a new connection sees it too.
        with db.connect(server.CFG['db']) as fresh:
            self.assertEqual(server.workspace_cooldown(fresh, datetime.now(timezone.utc)),
                             db.get_setting(self.conn, 'cooldown'))
        db.set_setting(self.conn, 'cooldown', '2000-01-01T00:00:00Z')
        self.conn.commit()
        self.assertEqual(self.nxt('b', 'list')['job']['seed'], 'second')
        self.assertEqual(self.post('b', '/api/ext/heartbeat', {'version': '3.9.12', 'state': 'idle'})[1]['stages'],
                         {'list': True, 'profile': True})

    def test_home_redirect_holds_all_accounts_but_generic_error_does_not(self):
        self.seeds('first', 'second', 'third')
        db.set_setting(self.conn, 'paused_bios', True)
        self.conn.commit()
        first = self.nxt('a', 'list')['job']
        self.post('a', '/api/ext/error', {'job_id': first['id'], 'code': 'other',
                                         'reason': 'list_html_home_redirect', 'message': 'home page'})
        hold = datetime.fromisoformat(db.get_setting(self.conn, 'cooldown'))
        self.assertGreater(hold, datetime.now(timezone.utc) + timedelta(minutes=29))
        self.assertIsNone(self.nxt('b', 'list')['job'])
        self.assertEqual(self.nxt('b')['stages'], {'list': False, 'profile': False})
        db.set_setting(self.conn, 'cooldown', '2000-01-01T00:00:00Z')
        self.conn.commit()
        resumed = self.nxt('b', 'list')
        self.assertEqual(resumed['stages'], {'list': True, 'profile': False})
        second = resumed['job']
        self.post('b', '/api/ext/error', {'job_id': second['id'], 'code': 'other',
                                         'reason': 'http_500', 'message': 'temporary response'})
        self.assertIsNone(server.workspace_cooldown(self.conn, datetime.now(timezone.utc)))
        self.assertEqual(self.nxt('c', 'list')['job']['seed'], 'third')

    def test_stale_home_redirect_still_holds_other_accounts(self):
        self.seeds('first', 'second')
        job = self.nxt('a', 'list')['job']
        self.conn.execute("UPDATE jobs SET state='done', lane=NULL WHERE id=?", (job['id'],))
        self.conn.commit()
        self.post('a', '/api/ext/error', {'job_id': job['id'], 'kind': 'list', 'code': 'other',
                                        'reason': 'list_html_home_redirect'})
        self.assertIsNotNone(server.workspace_cooldown(self.conn, datetime.now(timezone.utc)))
        self.assertIsNone(self.nxt('b')['job'])
        self.assertEqual(self.conn.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'done')

    def test_reported_kind_cannot_turn_profile_error_into_home_redirect_hold(self):
        self.nxt('a', 'profile')
        self.conn.execute("INSERT INTO jobs(kind,handle,state,created_at) VALUES('profile','person','done',?)", (db.now(),))
        jid = self.conn.execute('SELECT max(id) FROM jobs').fetchone()[0]
        self.conn.commit()
        response = self.post('a', '/api/ext/error', {'job_id': jid, 'kind': 'list', 'code': 'other',
                                        'reason': 'list_html_home_redirect'})
        self.assertEqual(response[0], 200)
        self.assertTrue(response[1]['stale'])
        self.assertIsNone(server.workspace_cooldown(self.conn, datetime.now(timezone.utc)))

    def test_start_all_succeeds_after_hold_expires(self):
        db.set_setting(self.conn, 'paused_lists', True)
        db.set_setting(self.conn, 'paused_bios', True)
        db.set_setting(self.conn, 'cooldown', '2000-01-01T00:00:00Z')
        self.conn.commit()
        self.assertEqual(self.call('/api/control', {'action': 'start_all'})[0], 200)
        self.assertFalse(db.get_setting(self.conn, 'paused_lists'))
        self.assertFalse(db.get_setting(self.conn, 'paused_bios'))

    def test_invalid_stored_hold_fails_closed(self):
        for raw in ('not-a-date', 42, False):
            db.set_setting(self.conn, 'cooldown', raw)
            self.conn.commit()
            self.assertEqual(self.call('/api/ext/next')[0], 400)
            self.assertEqual(self.call('/api/control', {'action': 'start_all'})[0], 400)

    def test_security_warnings_hold_all_stages_and_block_start_engine(self):
        from unittest.mock import patch
        self.seeds('first', 'second')
        for code in ('login', 'challenge'):
            self.post('a', '/api/ext/error', {'kind': 'profile', 'code': code})
            self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
            self.assertTrue(db.get_setting(self.conn, 'paused_bios'))
            self.assertEqual(self.call('/api/control', {'action': 'start_all'})[0], 400)
            self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
            self.assertTrue(db.get_setting(self.conn, 'paused_bios'))
            self.assertIsNone(self.nxt('b')['job'])
            self.assertEqual(self.nxt('c')['stages'], {'list': False, 'profile': False})
            with patch.object(server.engine_start, 'start') as launch:
                self.assertEqual(self.call('/api/engine/start', {})[0], 400)
                launch.assert_not_called()

    def test_legacy_resumes_cannot_clear_security_pause_during_shared_hold(self):
        self.seeds('first', 'second')
        self.nxt('b')
        self.call('/api/accounts/lane-b', {'paused': True})
        for code in ('login', 'challenge'):
            self.post('a', '/api/ext/error', {'kind': 'profile', 'code': code})
            for stage in ('lists', 'bios', 'all'):
                self.assertEqual(self.call('/api/control', {'stage': stage, 'action': 'resume'})[0], 400)
                self.assertEqual(self.call('/api/ext/control', {'stage': stage, 'action': 'resume'})[0], 400)
            self.assertEqual(self.call('/api/control', {'account': 'lane-b', 'action': 'resume'})[0], 400)
            self.assertEqual(self.call('/api/accounts/lane-b', {'paused': False})[0], 400)
            self.assertEqual(self.call('/api/scraper/pause', {'paused': False})[0], 400)
            self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
            self.assertTrue(db.get_setting(self.conn, 'paused_bios'))
            self.assertEqual(self.conn.execute("SELECT paused FROM accounts WHERE lane_id='lane-b'").fetchone()[0], 1)
            # Expiring the timer must not undo the security pause.
            db.set_setting(self.conn, 'cooldown', '2000-01-01T00:00:00Z')
            self.conn.commit()
            self.assertIsNone(self.nxt('c')['job'])
            self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
            self.assertTrue(db.get_setting(self.conn, 'paused_bios'))
        self.assertEqual(self.call('/api/control', {'stage': 'all', 'action': 'resume'})[0], 200)
        self.assertFalse(db.get_setting(self.conn, 'paused_lists'))
        self.assertFalse(db.get_setting(self.conn, 'paused_bios'))

    def test_profile_rate_limit_quarantines_same_handle_across_lanes(self):
        self.nxt('a', 'profile')
        self.nxt('b', 'profile')
        self.conn.execute("INSERT INTO jobs(kind,handle,priority) VALUES('profile','hot',10000)")
        self.conn.execute("INSERT INTO jobs(kind,handle,priority) VALUES('profile','hot',9000)")
        self.conn.execute("INSERT INTO jobs(kind,handle,priority) VALUES('profile','other',100)")
        self.conn.commit()
        job = self.nxt('a', 'profile')['job']
        self.assertEqual(job['handle'], 'hot')
        self.post('a', '/api/ext/error', {'job_id': job['id'], 'code': 'rate_limit',
                                           'retry_at': '2099-01-01T00:00:00Z', 'message': '429'})
        self.assertIsNone(self.nxt('b', 'profile')['job'])
        db.set_setting(self.conn, 'cooldown', '2000-01-01T00:00:00Z')
        self.conn.commit()
        following = self.nxt('b', 'profile')['job']
        self.assertEqual(following['handle'], 'other')
        blocked = self.conn.execute("SELECT retry_not_before FROM jobs WHERE handle='hot'").fetchall()
        self.assertEqual(len(blocked), 2)
        self.assertTrue(all(datetime.fromisoformat(r[0]) > datetime.now(timezone.utc) for r in blocked))
        self.assertTrue(all(datetime.fromisoformat(r[0]) < datetime.now(timezone.utc) + timedelta(hours=1)
                            for r in blocked))  # a lane's long cooldown is not shared by the target

    def test_explicit_retry_after_is_shared_with_target(self):
        self.conn.execute("INSERT INTO jobs(kind,handle,priority) VALUES('profile','hot',100)")
        self.conn.commit()
        job = self.nxt('a', 'profile')['job']
        future = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
        self.post('a', '/api/ext/error', {'job_id': job['id'], 'code': 'rate_limit',
                                           'retry_at': future, 'retry_after': future, 'message': '429'})
        blocked = self.conn.execute('SELECT retry_not_before FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
        self.assertGreaterEqual(datetime.fromisoformat(blocked), datetime.fromisoformat(future))

    def test_profile_only_cooldown_does_not_mask_list_readiness(self):
        self.nxt('a', 'list')
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.0', 'state': 'cooldown',
                                             'cooldown_until': future, 'cool': {'list': None, 'profile': future}})
        row = self.conn.execute("SELECT * FROM accounts WHERE lane_id='lane-a'").fetchone()
        now = datetime.now(timezone.utc)
        self.assertIsNone(control.lane_wait(self.conn, row, 'list', now))
        self.assertEqual(control.lane_wait(self.conn, row, 'profile', now)[0],
                         'Instagram asked us to slow down, resting')

    def test_long_routine_request_gap_is_not_reported_as_an_instagram_limit(self):
        self.nxt('a', 'list')
        future = (datetime.now(timezone.utc) + timedelta(seconds=45)).isoformat()
        db.set_setting(self.conn, 'ext_ready', {'lane-a': {'list': future}})
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM accounts WHERE lane_id='lane-a'").fetchone()
        why, seconds = control.lane_wait(self.conn, row, 'list', datetime.now(timezone.utc))
        self.assertEqual(why, 'Waiting between requests')
        self.assertGreater(seconds, 0)
        self.assertNotIn('Instagram', why)

    def test_main_account_protected(self):
        self.conn.execute("INSERT INTO seeds(handle, is_me) VALUES('acct.a', 1)")
        self.conn.commit()
        pid = db.upsert_person(self.conn, {'ig_id': '7', 'handle': 'dave'})
        self.conn.commit()
        self.call(f'/api/person/{pid}/read', {})
        self.seeds('s1')
        got = self.nxt('a', 'list')['job']              # Michael's own account, alone: auto main, but still reads lists
        self.assertEqual(got['kind'], 'list')
        self.assertTrue(self.call('/api/accounts')[1]['accounts'][0]['is_main'])
        self.assertIn('Your main account is the only one online, so it reads lists too — add a second account to protect it',
                      [x['text'] for x in self.call('/api/accounts')[1]['alerts']])
        self.assertIsNone(self.nxt('b', 'list')['job'])  # the main account's in-flight page keeps its lease
        self.assertEqual(self.page('a', got, 2, 'c1')[1]['received'], 2)
        jb = self.nxt('b', 'list')['job']                 # then the second account resumes at the saved cursor
        self.assertEqual((jb['kind'], jb['cursor'], jb['received']), ('list', 'c1', 2))
        self.assertIsNone(self.nxt('a', 'list')['job'])
        self.assertEqual(self.nxt('a', 'list,profile')['job']['kind'], 'profile')   # main: bios only now
        self.call('/api/accounts/lane-b', {'role': 'bios'})   # nobody else takes lists: the main account does again
        self.conn.execute("UPDATE jobs SET leased_until='2000-01-01' WHERE kind='list'")
        self.conn.commit()
        self.assertEqual(self.nxt('a', 'list')['job']['kind'], 'list')

    def test_main_takes_lists_when_alts_reach_their_budget(self):
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('acct.a',1)")
        self.conn.commit()
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.0', 'state': 'idle'})
        self.post('b', '/api/ext/heartbeat', {'version': '3.9.0', 'state': 'idle', 'today': {'list': 1}})
        self.call('/api/accounts/lane-b', {'budget': {'list': 1, 'profile': 300}})
        self.seeds('s1')
        self.assertIsNone(self.nxt('b', 'list')['job'])
        self.assertEqual(self.nxt('a', 'list')['job']['seed'], 's1')

    def test_pick_job_rechecks_eligibility_after_pause_and_identity_switch(self):
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('acct.a',1)")
        self.conn.commit()
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.12', 'state': 'idle'})
        self.post('b', '/api/ext/heartbeat', {'version': '3.9.12', 'state': 'idle'})
        self.seeds('s1')
        now = datetime.now(timezone.utc)
        self.assertIsNone(accounts.pick_job(self.conn, 'lane-a', ['list'], now))
        self.call('/api/accounts/lane-b', {'paused': True})
        self.assertEqual(accounts.pick_job(self.conn, 'lane-a', ['list'], now)['seed'], 's1')
        self.call('/api/accounts/lane-b', {'paused': False})
        self.assertIsNone(accounts.pick_job(self.conn, 'lane-a', ['list'], now))
        self.call('/api/ext/heartbeat', {'lane_id': 'lane-a',
                  'account': {'ig_id': '999', 'handle': 'newacct'},
                  'version': '3.9.12', 'state': 'idle'})
        self.assertEqual(accounts.pick_job(self.conn, 'lane-a', ['list'], now)['seed'], 's1')

    def test_main_takes_followers_when_every_alt_endpoint_is_waiting(self):
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('acct.a',1)")
        self.conn.commit()
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.6', 'state': 'idle'})
        until = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        for who in ('b', 'c'):
            self.post(who, '/api/ext/heartbeat', {'version': '3.9.6', 'state': 'idle',
                                                  'list_endpoint_until': until})
        self.seeds('target', direction='followers')
        self.seeds('other', direction='following')
        self.conn.execute("UPDATE lists SET lane='lane-b', cursor='next', received=25 "
                          "WHERE seed='target' AND direction='followers'")
        self.conn.commit()
        self.assertEqual(self.nxt('b', 'list')['job']['seed'], 'other')
        self.assertIsNone(self.nxt('c', 'list')['job'])
        job = self.nxt('a', 'list')['job']
        self.assertEqual((job['seed'], job['cursor'], job['received']), ('target', 'next', 25))

    def test_server_does_not_lease_during_account_cooldown(self):
        self.seeds('target', direction='following')
        until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.6', 'state': 'cooldown',
                  'cool': {'list': until, 'profile': None}})
        self.assertIsNone(self.nxt('a', 'list')['job'])
        self.assertEqual(self.nxt('b', 'list')['job']['seed'], 'target')
        self.conn.execute("INSERT INTO jobs(kind,handle,priority) VALUES('profile','someone',100)")
        self.conn.commit()
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.6', 'state': 'cooldown',
                  'cool': {'list': until, 'profile': until}})
        self.assertIsNone(self.nxt('a', 'profile')['job'])
        self.assertEqual(self.nxt('b', 'profile')['job']['handle'], 'someone')

    def test_default_lane_backwards_compatible(self):
        self.seeds('s1')
        job = self.call('/api/ext/next')[1]['job']
        self.call('/api/ext/heartbeat', {'version': '3.3.0', 'state': 'running', 'today': {'list': 3, 'profile': 0}})
        s = self.call('/api/scraper')[1]
        self.assertEqual([a['lane_id'] for a in s['accounts']], ['default'])
        self.assertEqual((s['ext']['version'], s['ext']['state'], s['ext']['today']), ('3.3.0', 'running', {'list': 3, 'profile': 0}))
        self.assertEqual(self.conn.execute('SELECT lane FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'default')

    def test_remove_releases(self):
        self.seeds('s1')
        ja = self.nxt('a')['job']
        self.assertEqual(self.call('/api/accounts/lane-a/remove', {})[1], {'ok': True, 'removed': 1})
        self.assertEqual(self.nxt('b')['job']['id'], ja['id'])
        self.assertEqual([a['lane_id'] for a in self.call('/api/accounts')[1]['accounts']], ['lane-b'])
        self.assertEqual(self.call('/api/accounts/nope/remove', {})[1]['removed'], 0)

    def test_setup(self):
        s = self.call('/api/setup')[1]
        self.assertTrue(s['extension_path'].endswith('extension'))
        self.assertTrue(Path(s['extension_path'], 'manifest.json').is_file())
        self.assertEqual(s['extension_id'], 'fgdbghllamedgihmdcolaggnbhnakjnf')

    def test_same_account_twice(self):
        self.seeds('s1', 's2')
        first = self.nxt('a', 'list')['job']
        ACCT['z'] = ('lane-z', '101', 'acct.a')
        try:
            self.assertIsNone(self.nxt('z', 'list')['job'])
            self.conn.execute("INSERT INTO jobs(kind,handle,priority) VALUES('profile','someone',100)")
            self.conn.commit()
            self.assertIsNone(self.nxt('z', 'profile')['job'])
            self.assertEqual(self.nxt('b', 'list')['job']['seed'], 's2' if first['seed'] == 's1' else 's1')
            self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE state='leased' AND lane='lane-z'").fetchone()[0], 0)
        finally:
            del ACCT['z']
        self.assertTrue(any('logged in on 2 Chrome profiles' in x['text'] for x in self.call('/api/accounts')[1]['alerts']))

    def test_duplicate_account_takes_over_after_owner_offline(self):
        self.seeds('s1')
        first = self.nxt('a', 'list')['job']
        ACCT['z'] = ('lane-z', '101', 'acct.a')
        try:
            self.assertIsNone(self.nxt('z', 'list')['job'])
            self.age('lane-a', 11)
            self.conn.execute("UPDATE jobs SET leased_until='2000-01-01' WHERE id=?", (first['id'],))
            self.conn.commit()
            replacement = self.nxt('z', 'list')['job']
            self.assertEqual(replacement['id'], first['id'])
            self.assertEqual(self.conn.execute('SELECT lane FROM lists WHERE seed=?', (first['seed'],)).fetchone()[0], 'lane-z')
        finally:
            del ACCT['z']

    def test_duplicate_account_cannot_bypass_cooldown_or_budget(self):
        self.seeds('s1')
        ACCT['z'] = ('lane-z', '101', 'acct.a')
        try:
            self.nxt('a', 'profile')
            self.nxt('z', 'profile')
            future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
            self.post('a', '/api/ext/heartbeat', {'version': '3.9.6', 'state': 'cooldown',
                      'cool': {'list': future, 'profile': None}, 'today': {'list': 1}})
            self.call('/api/accounts/lane-a', {'budget': {'list': 1, 'profile': 300}})
            self.age('lane-a', 11)
            self.assertIsNone(self.nxt('z', 'list')['job'])
            self.conn.execute("UPDATE accounts SET list_cool_until=NULL WHERE lane_id='lane-a'")
            self.conn.commit()
            self.assertIsNone(self.nxt('z', 'list')['job'])
        finally:
            del ACCT['z']

    def test_duplicate_identity_ownership_respects_roles(self):
        self.seeds('s1')
        self.nxt('a', 'profile')
        self.call('/api/accounts/lane-a', {'role': 'bios'})
        self.conn.execute("INSERT INTO jobs(kind,handle,priority) VALUES('profile','someone',100)")
        self.conn.commit()
        ACCT['z'] = ('lane-z', '101', 'acct.a')
        try:
            self.post('z', '/api/ext/heartbeat', {'version': '3.9.6', 'state': 'idle'})
            self.call('/api/accounts/lane-z', {'role': 'lists'})
            old_job = self.nxt('a', 'profile')['job']
            self.assertEqual(old_job['handle'], 'someone')
            self.assertIsNone(self.nxt('z', 'list')['job'])
            self.assertIsNone(self.nxt('a', 'list')['job'])
            self.assertIsNone(self.nxt('z', 'profile')['job'])
            alerts = [x['text'] for x in self.call('/api/accounts')[1]['alerts']]
            self.assertTrue(any('Set one profile to both' in text for text in alerts), alerts)
            self.call('/api/accounts/lane-z', {'role': 'both'})
            self.assertIsNone(self.nxt('z', 'list')['job'])  # wait for the old lane's live lease
            self.conn.execute("UPDATE jobs SET leased_until='2000-01-01' WHERE id=?", (old_job['id'],))
            self.conn.commit()
            self.assertEqual(self.nxt('z', 'list')['job']['seed'], 's1')
            self.assertFalse(any('Set one profile to both' in x['text'] for x in self.call('/api/accounts')[1]['alerts']))
        finally:
            del ACCT['z']

    def test_identity_change_clears_previous_account_limits(self):
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('acct.a',1)")
        self.conn.commit()
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.6', 'state': 'cooldown',
                  'cooldown_until': future, 'cool': {'list': future, 'profile': future},
                  'list_endpoint_until': future, 'today': {'list': 3, 'profile': 5}})
        self.assertTrue(self.conn.execute("SELECT is_main FROM accounts WHERE lane_id='lane-a'").fetchone()[0])
        # The extension reports fresh state for the newly signed-in identity.
        self.call('/api/ext/heartbeat', {'lane_id': 'lane-a', 'account': {'ig_id': '999', 'handle': 'newacct'},
                  'version': '3.9.6', 'state': 'idle', 'cooldown_until': None,
                  'cool': {'list': None, 'profile': None}, 'list_endpoint_until': None,
                  'today': {'list': 0, 'profile': 0}})
        row = self.conn.execute("SELECT * FROM accounts WHERE lane_id='lane-a'").fetchone()
        self.assertEqual((row['ig_id'], row['handle'], row['is_main']), ('999', 'newacct', 0))
        self.assertEqual(accounts.jload(row['today']), {'list': 0, 'profile': 0})
        self.assertTrue(all(row[k] is None for k in ('rate', 'cooldown_until', 'list_cool_until',
                                                   'profile_cool_until', 'list_endpoint_until')))
        self.seeds('s1')
        self.assertEqual(self.call('/api/ext/next?lane=lane-a&ig_id=999&handle=newacct&kinds=list')[1]['job']['seed'], 's1')

    def test_switching_back_restores_daily_usage_before_next_lease(self):
        self.seeds('s1')
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.6', 'state': 'idle',
                                             'today': {'list': 1, 'profile': 0}})
        self.call('/api/accounts/lane-a', {'budget': {'list': 1, 'profile': 300}})
        self.call('/api/ext/next?lane=lane-a&ig_id=999&handle=newacct&kinds=profile')
        self.assertIsNone(self.call('/api/ext/next?lane=lane-a&ig_id=101&handle=acct.a&kinds=list')[1]['job'])
        self.assertEqual((accounts.jload(self.conn.execute("SELECT today FROM accounts WHERE lane_id='lane-a'").fetchone()[0]) or {})['list'], 1)
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.6', 'state': 'idle',
                                             'today': {'list': 0, 'profile': 0}})
        self.assertIsNone(self.nxt('a', 'list')['job'])

    def test_identity_switch_releases_old_lease_and_list_owner(self):
        self.seeds('s1')
        job = self.nxt('a', 'list')['job']
        self.call('/api/ext/next?lane=lane-a&ig_id=999&handle=newacct&kinds=profile')
        lease = self.conn.execute('SELECT state,lane,lease_token FROM jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual(tuple(lease), ('queued', None, None))
        owner = self.conn.execute("SELECT lane,prev_lane,released_why FROM lists WHERE seed='s1'").fetchone()
        self.assertEqual(tuple(owner), (None, 'lane-a', 'identity_changed'))

    def test_switching_back_restores_cooldown_before_next_lease(self):
        self.seeds('s1')
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.6', 'state': 'cooldown',
                                             'cool': {'list': future, 'profile': None}})
        self.call('/api/ext/next?lane=lane-a&ig_id=999&handle=newacct&kinds=profile')
        self.assertIsNone(self.call('/api/ext/next?lane=lane-a&ig_id=101&handle=acct.a&kinds=list')[1]['job'])
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.6', 'state': 'idle',
                                             'cool': {'list': None, 'profile': None}})
        self.assertIsNone(self.nxt('a', 'list')['job'])
        self.assertGreater(datetime.fromisoformat(self.conn.execute(
            "SELECT list_cool_until FROM account_identity_state WHERE lane_id='lane-a' AND ig_id='101'").fetchone()[0]),
            datetime.now(timezone.utc))

    def test_identity_switch_merges_fresh_heartbeat_with_saved_state(self):
        today = datetime.now().date()
        client_day = f'{today.year}-{today.month}-{today.day}'
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.6', 'state': 'idle', 'day': client_day,
                                             'today': {'list': 1, 'profile': 0}})
        self.call('/api/ext/next?lane=lane-a&ig_id=999&handle=newacct&kinds=profile')
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.call('/api/ext/heartbeat', {'lane_id': 'lane-a', 'account': {'ig_id': '101', 'handle': 'acct.a'},
                  'version': '3.9.6', 'state': 'cooldown', 'day': client_day,
                  'today': {'list': 2, 'profile': 0}, 'cool': {'list': future, 'profile': None}})
        row = self.conn.execute("SELECT today,list_cool_until FROM accounts WHERE lane_id='lane-a'").fetchone()
        self.assertEqual(accounts.jload(row['today'])['list'], 2)
        self.assertGreater(datetime.fromisoformat(row['list_cool_until']), datetime.now(timezone.utc))

    def test_local_day_rollover_does_not_keep_yesterdays_budget(self):
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        old_day = f'{yesterday.year}-{yesterday.month}-{yesterday.day}'
        new_day = f'{today.year}-{today.month}-{today.day}'
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.6', 'state': 'idle', 'day': old_day,
                                             'today': {'list': 3, 'profile': 0}})
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.6', 'state': 'idle', 'day': new_day,
                                             'today': {'list': 0, 'profile': 0}})
        row = self.conn.execute("SELECT today FROM accounts WHERE lane_id='lane-a'").fetchone()
        self.assertEqual(accounts.jload(row['today'])['list'], 0)
        saved = self.conn.execute("SELECT day,today FROM account_identity_state WHERE lane_id='lane-a' AND ig_id='101'").fetchone()
        self.assertEqual((saved['day'], accounts.jload(saved['today'])['list']), (today.isoformat(), 0))


class MigrationTest(unittest.TestCase):
    OLD = """
    CREATE TABLE people(id INTEGER PRIMARY KEY, ig_id TEXT UNIQUE, handle TEXT UNIQUE NOT NULL COLLATE NOCASE, name TEXT, pic_url TEXT,
      pic_file TEXT, is_private INT, is_verified INT, bio TEXT, website TEXT, category TEXT, followers INT, following INT, posts INT,
      is_business INT, bio_at TEXT, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE lists(seed TEXT COLLATE NOCASE, direction TEXT CHECK(direction IN('followers','following')), state TEXT,
      cursor TEXT, received INT DEFAULT 0, total INT, error TEXT, updated_at TEXT, PRIMARY KEY(seed,direction));
    CREATE TABLE jobs(id INTEGER PRIMARY KEY, kind TEXT CHECK(kind IN('list','profile')), seed TEXT, direction TEXT,
      handle TEXT, priority INT DEFAULT 0, state TEXT DEFAULT 'queued', attempts INT DEFAULT 0, leased_until TEXT, created_at TEXT);
    CREATE TABLE pages(job_id INT, cursor TEXT, PRIMARY KEY(job_id, cursor));
    CREATE TABLE tags(person_id INT, tag TEXT, grp TEXT, source TEXT CHECK(source IN('auto','manual')), PRIMARY KEY(person_id,tag));
    INSERT INTO lists VALUES('s1','followers','running','c9',40,100,NULL,'2026-09-20');
    INSERT INTO jobs(kind, seed, direction, state, leased_until) VALUES('list','s1','followers','leased','2000-01-01');
    INSERT INTO pages VALUES(1,'c9');
    """

    def test_old_db_gains_lanes(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / 'old.sqlite')
            c = sqlite3.connect(path)
            c.executescript(self.OLD)
            c.commit()
            c.close()
            for _ in range(2):   # idempotent
                conn = db.init(path)
                conn.close()
            conn = db.connect(path)
            cols = {t: {r[1] for r in conn.execute(f'PRAGMA table_info({t})')} for t in ('jobs', 'lists', 'pages', 'accounts')}
            self.assertTrue({'lane'} <= cols['jobs'])
            self.assertTrue({'lane', 'prev_lane', 'released_at', 'released_why'} <= cols['lists'])
            self.assertTrue({'at', 'lane', 'users'} <= cols['pages'])
            self.assertTrue({'lane_id', 'ig_id', 'handle', 'role', 'budget', 'paused', 'is_main', 'first_seen', 'last_seen', 'version',
                             'state', 'cooldown_until', 'rate', 'last_error'} <= cols['accounts'])
            self.assertEqual(conn.execute('SELECT cursor, received FROM lists').fetchone()[:], ('c9', 40))
            # an unowned expired lease from before lanes is simply leased to the first lane that asks, cursor intact
            conn.execute('BEGIN IMMEDIATE')
            accounts.touch(conn, 'default')
            job = accounts.pick_job(conn, 'default', ['list'], datetime.now(timezone.utc))
            conn.rollback()
            self.assertEqual((job['seed'], job['owner']), ('s1', None))
            conn.close()

    def test_existing_account_usage_seeds_identity_ledger_once(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / 'existing.sqlite')
            conn = db.init(path)
            seen = datetime.now(timezone.utc).isoformat()
            conn.execute('INSERT INTO accounts(lane_id,ig_id,first_seen,last_seen,today) VALUES(?,?,?,?,?)',
                         ('lane-a', '101', seen, seen, '{"list": 4, "profile": 2}'))
            conn.commit()
            conn.close()
            for _ in range(2):
                conn = db.init(path)
                saved = conn.execute("SELECT day,today FROM account_identity_state WHERE lane_id='lane-a' AND ig_id='101'").fetchone()
                self.assertEqual((saved['day'], accounts.jload(saved['today'])),
                                 (datetime.now().date().isoformat(), {'list': 4, 'profile': 2}))
                self.assertEqual(conn.execute('SELECT count(*) FROM account_identity_state').fetchone()[0], 1)
                conn.close()


if __name__ == '__main__':
    unittest.main()
