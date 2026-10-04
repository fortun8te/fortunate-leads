"""Opt-in shared-account twin: two Chrome profiles on @trashtheturtles. No Instagram traffic; temp DB only."""
import os; os.environ.setdefault('FL_NO_ORSLOT', '1')  # tests never see the real key pool
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server import Base  # noqa: E402  (installs the qualify stub first)

import account_twin  # noqa: E402
import accounts  # noqa: E402
import db  # noqa: E402

TURTLES = account_twin.TWIN_IG_ID
A, B = 'lane-a', 'lane-b'
MAIN_LANE, MAIN_ID = 'lane-main', '55966675376'
WARNED_LANE, WARNED_ID = 'lane-dih', '25764895364'
BUDGET = {'list': 1500, 'profile': 100}


def now():
    return datetime.now(timezone.utc)


class TwinBase(Base):
    def beat(self, lane, ig_id=TURTLES, handle='trashtheturtles', tab='ok', **extra):
        body = dict(lane_id=lane, account={'ig_id': ig_id, 'handle': handle}, version='3.9.30', state='idle', tab=tab,
                    hold=None, today={'list': 0, 'profile': 0})
        body.update(extra)
        return self.call('/api/ext/heartbeat', body)

    def nxt(self, lane, ig_id=TURTLES, handle='trashtheturtles', kinds='list'):
        return self.call(f'/api/ext/next?lane={lane}&ig_id={ig_id}&handle={handle}&version=3.9.30&kinds={kinds}')[1]

    def post(self, lane, path, body, ig_id=TURTLES, handle='trashtheturtles'):
        return self.call(path, dict(body, lane_id=lane, account={'ig_id': ig_id, 'handle': handle}))

    def twin(self, **body):
        return self.call('/api/account-twin', body)

    def setup_two_profiles(self, budget=BUDGET):
        self.beat(A)
        self.beat(B)
        for lane in (A, B):
            self.call(f'/api/accounts/{lane}', {'budget': dict(budget)})
        # B signed in later than A: A is the original profile
        self.conn.execute("UPDATE accounts SET first_seen='2026-09-26T00:00:00+00:00' WHERE lane_id=?", (A,))
        self.conn.execute("UPDATE accounts SET first_seen='2026-10-04T00:00:00+00:00' WHERE lane_id=?", (B,))
        self.conn.commit()

    def seed_baseline(self, requests=150, minutes=60, share=1.0):
        cfg = account_twin.config(self.conn)
        start = now() - timedelta(minutes=minutes)
        slots = int(minutes * 60 // 300) + 1
        base = cfg['baseline']
        base.update(started_at=accounts.iso(start), requests=requests,
                    slots=[int(start.timestamp() // 300) + i for i in range(int(slots * share))])
        account_twin.save(self.conn, cfg)
        self.conn.commit()

    def go_live(self, per_hour=150):
        """Baseline accepted and twin enabled at the given cap."""
        self.setup_two_profiles()
        self.assertEqual(self.twin(action='start_baseline', reviewed=True, lane_id=A)[0], 200)
        self.seed_baseline(requests=per_hour)
        self.assertEqual(self.twin(action='finish_baseline', reviewed=True)[0], 200)
        code, out = self.twin(action='enable', reviewed=True, lane_ids=[A, B])
        self.assertEqual(code, 200, out)
        return out

    def age_step(self, minutes, requests=None):
        cfg = account_twin.config(self.conn)
        step = cfg['step']
        step['started_at'] = accounts.iso(now() - timedelta(minutes=minutes))
        if requests is not None:
            step['requests'] = requests
        account_twin.save(self.conn, cfg)
        self.conn.commit()

    def lane(self, lane):
        return self.conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()

    def seeds(self, *handles):
        self.call('/api/scraper/seeds', {'handles': list(handles), 'directions': ['following']})

    def settings_like(self, prefix):
        return [r[0] for r in self.conn.execute('SELECT key FROM settings WHERE key LIKE ?', (prefix + '%',))]


class DefaultOffTests(TwinBase):
    def test_nothing_changes_while_off(self):
        self.setup_two_profiles()
        self.seeds('s1', 's2')
        self.assertIsNone(account_twin.config(self.conn))
        first = self.nxt(A)['job']
        self.assertIsNotNone(first)
        self.assertIsNone(self.nxt(B)['job'], 'the duplicate profile gets no work unless the test is on')
        self.assertFalse(accounts.identity_owner(self.conn, self.lane(B), now()))
        self.assertEqual(account_twin.capacity_wait(self.conn, A, 'list', now().timestamp()), 0)
        permit = accounts.request_permit(self.conn, A, kind='list')
        self.assertTrue(permit['granted'])
        accounts.request_permit(self.conn, A, token=permit['token'])
        self.assertEqual(self.settings_like('account_twin'), [], 'a normal permit leaves no twin state behind')
        status = self.call('/api/account-twin')[1]
        self.assertEqual((status['state'], status['enabled']), ('off', False))
        text = ' '.join(a['text'] for a in self.call('/api/accounts')[1]['alerts'])
        self.assertIn('logged in on 2 Chrome profiles', text)

    def test_two_signed_in_profiles_never_switch_it_on(self):
        self.setup_two_profiles()
        for _ in range(3):
            self.beat(A); self.beat(B)
        self.assertEqual(self.twin(action='check')[1]['state'], 'off')
        self.assertEqual(self.settings_like('account_twin'), [])

    def test_every_action_needs_review_and_order(self):
        self.setup_two_profiles()
        self.assertEqual(self.twin(action='start_baseline', lane_id=A)[0], 400)
        self.assertEqual(self.twin(action='enable', reviewed=True, lane_ids=[A, B])[0], 400)   # no baseline yet
        self.assertEqual(self.twin(action='advance', reviewed=True)[0], 400)
        self.assertEqual(self.twin(action='nonsense', reviewed=True)[0], 400)
        self.assertEqual(self.settings_like('account_twin'), [])

    def test_damaged_setting_fails_closed(self):
        self.setup_two_profiles()
        self.conn.execute("INSERT OR REPLACE INTO settings VALUES('account_twin','\"junk\"')")
        self.conn.commit()
        self.assertIsNone(account_twin.config(self.conn))
        self.seeds('s1')
        self.nxt(A)
        self.assertIsNone(self.nxt(B)['job'])

    def test_the_extension_and_chrome_startup_do_not_know_about_it(self):
        root = Path(__file__).resolve().parents[2]
        for rel in ('extension/background.js', 'extension/lib/core.js', 'server/browser_startup.py', 'ops/startup_profiles.json'):
            self.assertNotIn('account_twin', (root / rel).read_text())
            self.assertNotIn('account-twin', (root / rel).read_text())


class EligibilityTests(TwinBase):
    def other(self, lane, ig_id, handle, **fields):
        self.beat(lane, ig_id, handle)
        if fields:
            self.conn.execute(f"UPDATE accounts SET {','.join(k + '=?' for k in fields)} WHERE lane_id=?", (*fields.values(), lane))
            self.conn.commit()

    def test_main_warned_and_other_accounts_are_refused(self):
        self.setup_two_profiles()
        self.other(MAIN_LANE, MAIN_ID, 'fortun8te', is_main=1)
        self.other(WARNED_LANE, WARNED_ID, 'dihfluencer')
        self.other('lane-z', '77', 'someone.else')
        for lane in (MAIN_LANE, WARNED_LANE, 'lane-z'):
            with self.subTest(lane=lane):
                self.assertEqual(self.twin(action='start_baseline', reviewed=True, lane_id=lane)[0], 400)
        self.assertEqual(self.settings_like('account_twin'), [])
        self.assertEqual(account_twin.lane_blockers(self.conn, self.lane(MAIN_LANE), now())[0][0], 'account_excluded')
        self.assertEqual(account_twin.lane_blockers(self.conn, self.lane(WARNED_LANE), now())[0][0], 'account_excluded')
        self.assertEqual(account_twin.lane_blockers(self.conn, self.lane('lane-z'), now())[0][0], 'account_not_allowed')

    def test_a_lane_cannot_borrow_the_allowed_handle_or_id(self):
        self.setup_two_profiles()
        self.other('lane-fake', '88', 'trashtheturtles')          # right handle, wrong Instagram id
        self.other('lane-fake2', MAIN_ID, 'trashtheturtles')      # the main account's id
        self.other('lane-fake3', TURTLES, 'fortun8te')            # allowed id, main handle
        for lane in ('lane-fake', 'lane-fake2', 'lane-fake3'):
            with self.subTest(lane=lane):
                self.assertTrue(account_twin.lane_blockers(self.conn, self.lane(lane), now()))
        self.assertEqual(self.twin(action='start_baseline', reviewed=True, lane_id='lane-fake')[0], 400)

    def test_main_flag_on_the_allowed_lane_blocks_it(self):
        self.setup_two_profiles()
        self.conn.execute('UPDATE accounts SET is_main=1 WHERE lane_id=?', (A,)); self.conn.commit()
        self.assertEqual(self.twin(action='start_baseline', reviewed=True, lane_id=A)[0], 400)

    def test_warning_hold_cooldown_old_extension_and_budget_block(self):
        self.setup_two_profiles()
        cases = [
            ('warning', lambda: db.set_setting(self.conn, 'instagram_scraping_warning', {'lane': A, 'ig_id': TURTLES, 'message': 'x', 'at': accounts.iso(now())})),
            ('hold', lambda: self.conn.execute("UPDATE accounts SET hold='challenge' WHERE lane_id=?", (A,))),
            ('cooling', lambda: self.conn.execute('UPDATE accounts SET list_cool_until=? WHERE lane_id=?', (accounts.iso(now() + timedelta(hours=1)), B))),
            ('old_extension', lambda: self.conn.execute("UPDATE accounts SET version='3.9.21' WHERE lane_id=?", (B,))),
            ('offline', lambda: self.conn.execute("UPDATE accounts SET last_seen='2026-01-01T00:00:00+00:00' WHERE lane_id=?", (B,))),
            ('budget_unbounded', lambda: self.call(f'/api/accounts/{B}', {'budget': {'list': 0, 'profile': 100}})),
            ('budget_mismatch', lambda: self.call(f'/api/accounts/{B}', {'budget': {'list': 900, 'profile': 100}})),
        ]
        for code, mutate in cases:
            with self.subTest(code=code):
                self.setup_two_profiles()
                self.conn.execute("UPDATE accounts SET hold=NULL,list_cool_until=NULL,version='3.9.30',last_seen=? WHERE ig_id=?", (accounts.iso(now()), TURTLES))
                db.set_setting(self.conn, 'instagram_scraping_warning', None)
                self.conn.execute('DELETE FROM account_identity_state')
                self.conn.commit()
                mutate(); self.conn.commit()
                pf = account_twin.preflight(self.conn, now(), primary=A, twin=B)
                self.assertFalse(pf['ok'])
                self.assertIn(code, {b['code'] for b in pf['blockers']})

    def test_third_profile_and_missing_second_profile_block(self):
        self.setup_two_profiles()
        self.beat('lane-c')
        self.assertIn('extra_profile', {b['code'] for b in account_twin.preflight(self.conn, now(), primary=A, twin=B)['blockers']})
        self.conn.execute('DELETE FROM accounts WHERE lane_id IN (?,?)', (B, 'lane-c')); self.conn.commit()
        self.assertIn('need_two_profiles', {b['code'] for b in account_twin.preflight(self.conn, now())['blockers']})


class FlowTests(TwinBase):
    def test_baseline_needs_time_requests_and_steady_collection(self):
        self.setup_two_profiles()
        self.assertEqual(self.twin(action='start_baseline', reviewed=True, lane_id=A)[0], 200)
        self.assertEqual(self.twin(action='start_baseline', reviewed=True, lane_id=A)[0], 400)      # already running
        self.assertIn('more minutes', self.twin(action='finish_baseline', reviewed=True)[1]['error'])
        self.seed_baseline(requests=20)
        self.assertIn('at least', self.twin(action='finish_baseline', reviewed=True)[1]['error'])
        self.seed_baseline(requests=200, share=0.4)
        self.assertIn('idle too often', self.twin(action='finish_baseline', reviewed=True)[1]['error'])
        self.seed_baseline(requests=200)
        code, out = self.twin(action='finish_baseline', reviewed=True)
        self.assertEqual((code, out['state']), (200, 'baseline_done'))
        self.assertGreaterEqual(out['baseline']['per_hour'], 190)

    def test_baseline_alone_does_not_activate_the_second_profile(self):
        self.setup_two_profiles()
        self.seeds('s1', 's2')
        self.twin(action='start_baseline', reviewed=True, lane_id=A)
        self.assertIsNotNone(self.nxt(A)['job'])
        self.assertIsNone(self.nxt(B)['job'])
        permit = accounts.request_permit(self.conn, A, kind='list')
        self.assertTrue(permit['granted'])
        self.assertEqual(account_twin.config(self.conn)['baseline']['requests'], 1)
        self.assertFalse(accounts.request_permit(self.conn, B, kind='list')['granted'])

    def test_enable_starts_at_the_baseline_and_both_profiles_work(self):
        out = self.go_live(per_hour=150)
        self.assertEqual(out['current']['cap_per_hour'], 150)
        self.assertEqual(out['current']['label'], 'twin_same_total')
        self.seeds('s1', 's2', 's3')
        ja, jb = self.nxt(A)['job'], self.nxt(B)['job']
        self.assertIsNotNone(ja)
        self.assertIsNotNone(jb)
        self.assertNotEqual(ja['id'], jb['id'])
        self.assertNotEqual(ja['seed'], jb['seed'])
        text = ' '.join(a['text'] for a in self.call('/api/accounts')[1]['alerts'])
        self.assertIn('second-profile test', text)
        self.assertNotIn('log the extra ones', text)

    def test_steps_grow_25_percent_and_stop_at_what_one_profile_may_do(self):
        ceiling = account_twin.single_profile_ceiling(self.conn)
        self.go_live(per_hour=250)
        self.assertIn('more minutes', self.twin(action='advance', reviewed=True)[1]['error'])
        self.age_step(61, requests=100)
        self.assertIn('did not reach its cap', self.twin(action='advance', reviewed=True)[1]['error'])
        self.age_step(61, requests=250)
        code, out = self.twin(action='advance', reviewed=True)
        self.assertEqual((code, out['current']['cap_per_hour']), (200, 312))
        self.assertEqual(len(out['history']), 1)
        self.assertLessEqual(312, ceiling)
        self.age_step(61, requests=312)
        error = self.twin(action='advance', reviewed=True)[1]['error']
        self.assertIn('more than one profile may do', error)
        self.assertIn('speed control', error)
        self.assertEqual(account_twin.config(self.conn)['step']['cap_per_hour'], 312)

    def test_there_is_no_second_speed_setting(self):
        """No raise-the-cap action, no twin rate constants: the only way up is the extension's own pacing."""
        self.go_live(per_hour=250)
        self.assertEqual(self.twin(action='raise_ceiling', reviewed=True, ceiling_per_hour=500, acknowledge_risk=True, reason='testing a higher step')[0], 400)
        for name in ('CEILING_PER_HOUR', 'ABSOLUTE_MAX_PER_HOUR', 'WINDOW_SECONDS', 'WINDOW_MAX'):
            self.assertFalse(hasattr(account_twin, name), name)
        self.assertNotIn('ceiling', account_twin.config(self.conn))

    def test_the_ceiling_follows_the_extension_pacing(self):
        import tempfile
        before = account_twin.single_profile_ceiling(self.conn)
        src = account_twin.CORE_JS
        with tempfile.TemporaryDirectory() as tmp:
            slow = Path(tmp) / 'core.js'
            slow.write_text(src.read_text().replace('listGap: [7e3, 12e3]', 'listGap: [14e3, 24e3]'))
            account_twin.CORE_JS = slow
            try:
                after = account_twin.single_profile_ceiling(self.conn)
            finally:
                account_twin.CORE_JS = src
        self.assertLess(after, before * 0.7)
        self.assertEqual(account_twin.single_profile_ceiling(self.conn), before)

    def test_pacing_read_matches_the_extension(self):
        import shutil, subprocess
        if not shutil.which('node'):
            self.skipTest('node is not installed')
        js = ("const FL=require(process.argv[1]);const P=FL.PACE;"
              "console.log(JSON.stringify({listGap:P.listGap,breakEvery:P.breakEvery,breakLen:P.breakLen,profileGap:P.profileGap,"
              "spacing:P.spacing,window:P.window/60000,windowMax:P.windowMax}))")
        want = json.loads(subprocess.check_output(['node', '-e', js, str(account_twin.CORE_JS)]))
        got = account_twin.pacing()
        keys = ('listGap', 'breakEvery', 'breakLen', 'profileGap', 'spacing')
        self.assertEqual({k: got[k] for k in keys}, {k: want[k] for k in keys})
        self.assertEqual((got['window'] / 60, got['windowMax']), (want['window'], want['windowMax']))

    def test_the_cap_is_never_above_one_profiles_rate_even_from_a_big_baseline(self):
        self.setup_two_profiles()
        self.twin(action='start_baseline', reviewed=True, lane_id=A)
        self.seed_baseline(requests=900)
        self.twin(action='finish_baseline', reviewed=True)
        out = self.twin(action='enable', reviewed=True, lane_ids=[A, B])[1]
        self.assertEqual(out['current']['cap_per_hour'], account_twin.single_profile_ceiling(self.conn))

    def test_enable_names_the_baseline_profile_first_and_refreshes_stale_baselines(self):
        self.setup_two_profiles()
        self.twin(action='start_baseline', reviewed=True, lane_id=A)
        self.seed_baseline()
        self.twin(action='finish_baseline', reviewed=True)
        self.assertEqual(self.twin(action='enable', reviewed=True, lane_ids=[B, A])[0], 400)
        self.assertEqual(self.twin(action='enable', reviewed=True, lane_ids=[A, A])[0], 400)
        cfg = account_twin.config(self.conn)
        cfg['baseline_result']['ended_at'] = accounts.iso(now() - timedelta(hours=25))
        account_twin.save(self.conn, cfg); self.conn.commit()
        self.assertIn('more than a day', self.twin(action='enable', reviewed=True, lane_ids=[A, B])[1]['error'])

    def test_disable_restores_the_single_profile_rule(self):
        self.go_live()
        self.seeds('s1', 's2', 's3')
        self.assertIsNotNone(self.nxt(B)['job'])
        code, out = self.twin(action='disable', reviewed=True, reason='done for today')
        self.assertEqual((code, out['state']), (200, 'finished'))
        self.assertTrue(self.lane(B)['paused'])
        self.assertFalse(self.lane(A)['paused'])
        self.assertIsNone(self.nxt(B)['job'])
        self.assertFalse(accounts.identity_owner(self.conn, self.lane(B), now()))
        self.assertEqual(account_twin.capacity_wait(self.conn, A, 'list', now().timestamp()), 0)
        self.assertEqual(self.twin(action='reset', reviewed=True)[1]['state'], 'off')
        self.assertEqual(self.settings_like('account_twin'), [])

    def test_pausing_or_resuming_one_profile_does_the_same_to_its_twin(self):
        self.go_live()
        self.call(f'/api/accounts/{A}', {'paused': True})
        self.assertEqual((self.lane(A)['paused'], self.lane(B)['paused']), (1, 1))
        self.call(f'/api/accounts/{B}', {'paused': False})
        self.assertEqual((self.lane(A)['paused'], self.lane(B)['paused']), (0, 0))


class SharedAccountTests(TwinBase):
    def test_work_is_split_without_overlap_or_double_counting(self):
        self.go_live()
        self.seeds('s1', 's2', 's3', 's4')
        ja, jb = self.nxt(A)['job'], self.nxt(B)['job']
        self.assertEqual({ja['seed'], jb['seed']} & {'s1', 's2', 's3', 's4'}, {ja['seed'], jb['seed']})
        self.assertNotEqual(ja['seed'], jb['seed'])
        # each list sticks to the profile that started it, whoever asks next
        owners = {r['seed']: r['lane'] for r in self.conn.execute("SELECT seed,lane FROM lists WHERE lane IS NOT NULL")}
        self.assertEqual(owners, {ja['seed']: A, jb['seed']: B})
        users = lambda tag, n: [{'ig_id': f'{tag}{i}', 'handle': f'{tag}_{i}'} for i in range(n)]
        body = lambda job, tag, n: {'job_id': job['id'], 'seed': job['seed'], 'direction': 'following', 'users': users(tag, n),
                                    'next_cursor': 'c1', 'done': False, 'requested_cursor': None}
        pa = self.post(A, '/api/ext/list-page', body(ja, 'a', 5))
        pb = self.post(B, '/api/ext/list-page', body(jb, 'b', 7))
        self.assertEqual((pa[1]['received'], pb[1]['received']), (5, 7))
        again_a, again_b = self.nxt(A)['job'], self.nxt(B)['job']
        self.assertEqual((again_a['seed'], again_b['seed']), (ja['seed'], jb['seed']))
        self.assertEqual((again_a['cursor'], again_b['cursor']), ('c1', 'c1'))
        # a page replayed by the other profile or by the same one is not counted twice
        self.assertTrue(self.post(A, '/api/ext/list-page', body(ja, 'a', 5))[1].get('duplicate'))
        self.assertTrue(self.post(B, '/api/ext/list-page', body(jb, 'b', 7))[1].get('duplicate'))
        status = self.twin(action='check')[1]['current']
        self.assertEqual(status['list_pages'], 2, 'one account, two pages, counted once each')
        self.assertEqual(status['people'], 12)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM pages').fetchone()[0], 2)

    def test_one_profile_never_takes_the_other_profiles_bio_target(self):
        self.go_live()
        self.conn.execute("INSERT INTO jobs(kind,handle,priority) VALUES('profile','someone',100)"); self.conn.commit()
        first = self.nxt(A, kinds='profile')['job']
        self.assertEqual(first['handle'], 'someone')
        self.assertIsNone(self.nxt(B, kinds='profile')['job'])

    def test_daily_budget_is_one_pool_for_both_profiles(self):
        self.setup_two_profiles(budget={'list': 10, 'profile': 100})
        self.twin(action='start_baseline', reviewed=True, lane_id=A)
        self.seed_baseline()
        self.twin(action='finish_baseline', reviewed=True)
        self.assertEqual(self.twin(action='enable', reviewed=True, lane_ids=[A, B])[0], 200)
        self.seeds('s1', 's2', 's3')
        self.beat(A, today={'list': 6, 'profile': 0})
        self.beat(B, today={'list': 3, 'profile': 0})
        self.assertIsNotNone(self.nxt(A)['job'])              # 9 of 10 used by the account
        self.beat(B, today={'list': 4, 'profile': 0})         # 10 of 10: nobody may start another list request
        self.assertIsNone(self.nxt(A)['job'])
        self.assertIsNone(self.nxt(B)['job'])
        self.assertFalse(accounts.request_permit(self.conn, A, kind='list')['granted'])
        self.assertFalse(accounts.request_permit(self.conn, B, kind='list')['granted'])

    def test_one_profiles_429_stops_both_and_ends_the_test(self):
        self.go_live()
        self.seeds('s1', 's2', 's3')
        ja = self.nxt(A)['job']
        self.nxt(B)
        self.post(A, '/api/ext/error', {'job_id': ja['id'], 'code': 'rate_limit', 'message': 'HTTP 429'})
        out = self.twin(action='check')[1]
        self.assertEqual(out['state'], 'tripped')
        self.assertEqual(out['trip']['code'], 'rate_limit')
        self.assertEqual(out['trip']['lane'], A)
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
        self.assertTrue(db.get_setting(self.conn, 'paused_bios'))
        self.assertTrue(self.lane(B)['paused'])
        for lane in (A, B):
            self.assertFalse(accounts.request_permit(self.conn, lane, kind='list')['granted'])
            self.assertIsNone(self.nxt(lane)['job'])
        # The wait belongs to the Instagram account, not to the profile that saw it: even after the
        # workspace pause is lifted by hand, the second profile is still resting.
        db.set_setting(self.conn, 'cooldown', None)
        db.set_setting(self.conn, 'paused_lists', False)
        db.set_setting(self.conn, 'paused_bios', False)
        self.conn.execute('UPDATE accounts SET paused=0'); self.conn.commit()
        self.assertTrue(accounts.identity_cooling(self.conn, self.lane(B), 'list', now()))
        self.assertTrue(accounts.identity_cooling(self.conn, self.lane(A), 'list', now()))
        self.assertIsNone(self.nxt(B)['job'])
        self.assertFalse(accounts.request_permit(self.conn, B, kind='list')['granted'])
        self.assertFalse(account_twin.is_member(self.conn, self.lane(B)), 'the second profile is excluded again')

    def test_the_other_profile_reporting_the_429_does_the_same(self):
        self.go_live()
        self.seeds('s1', 's2')
        self.nxt(A)
        jb = self.nxt(B)['job']
        self.post(B, '/api/ext/error', {'job_id': jb['id'], 'code': 'soft_block', 'message': 'empty page'})
        self.assertEqual(self.twin(action='check')[1]['trip']['lane'], B)
        self.assertTrue(accounts.identity_cooling(self.conn, self.lane(A), 'list', now()))
        self.assertIsNone(self.nxt(A)['job'])

    def test_every_adverse_signal_ends_the_test_once(self):
        cases = [('rate_limit', {}), ('soft_block', {}), ('login', {}), ('challenge', {}),
                 ('other', {'reason': 'list_html_home_redirect', 'http_status': 200}),
                 ('other', {'reason': 'scraping_warning'})]
        for code, extra in cases:
            with self.subTest(code=code, extra=extra):
                self.go_live()
                self.seeds('r1')
                job = self.nxt(A)['job']
                self.post(A, '/api/ext/error', dict({'job_id': job['id'], 'code': code, 'message': 'x'}, **extra))
                out = self.twin(action='check')[1]
                self.assertEqual(out['state'], 'tripped')
                self.assertTrue(out['trip']['at'])
                self.twin(action='reset', reviewed=True)
                self.conn.execute('DELETE FROM jobs'); self.conn.execute('DELETE FROM lists'); self.conn.execute('DELETE FROM accounts')
                for k in ('cooldown', 'paused_lists', 'paused_bios', 'instagram_scraping_warning', 'instagram_collection_isolation'):
                    db.set_setting(self.conn, k, None)
                self.conn.execute('DELETE FROM account_identity_state'); self.conn.commit()

    def test_login_or_security_check_holds_both_profiles(self):
        self.go_live()
        self.seeds('s1', 's2')
        ja = self.nxt(A)['job']
        self.nxt(B)
        self.post(A, '/api/ext/error', {'job_id': ja['id'], 'code': 'challenge', 'message': 'checkpoint'})
        self.assertEqual((self.lane(A)['hold'], self.lane(B)['hold']), ('challenge', 'challenge'))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE state='leased'").fetchone()[0], 0)

    def test_a_warning_page_on_the_second_profile_ends_the_test_for_both(self):
        self.go_live()
        self.beat(B, tab='tab_scraping_warning')
        out = self.twin(action='check')[1]
        self.assertEqual(out['state'], 'tripped')
        self.assertIn(out['trip']['code'], ('hold', 'scraping_warning'))
        self.beat(A)                                   # the warning is keyed by Instagram id
        self.assertEqual(self.lane(A)['hold'], 'challenge')
        self.assertFalse(accounts.request_permit(self.conn, A, kind='list')['granted'])

    def test_a_new_cooldown_seen_in_a_heartbeat_ends_the_test(self):
        self.go_live()
        future = accounts.iso(now() + timedelta(minutes=20))
        self.beat(B, cool={'list': future, 'profile': None})
        self.assertEqual(self.twin(action='check')[1]['trip']['code'], 'cooldown')
        self.assertTrue(accounts.identity_cooling(self.conn, self.lane(A), 'list', now()))

    def test_unexplained_errors_stop_the_test_at_three(self):
        self.go_live()
        self.seeds('s1', 's2', 's3', 's4')
        for i in range(3):
            self.assertEqual(self.twin(action='check')[1]['state'], 'twin')
            job = self.nxt(A)['job']
            self.post(A, '/api/ext/error', {'job_id': job['id'], 'code': 'other', 'reason': 'not_json', 'message': 'odd'})
        self.assertEqual(self.twin(action='check')[1]['state'], 'tripped')

    def test_private_and_missing_targets_are_not_adverse(self):
        self.go_live()
        self.seeds('s1', 's2', 's3')
        for code in ('private', 'not_found'):
            job = self.nxt(A)['job']
            self.post(A, '/api/ext/error', {'job_id': job['id'], 'code': code, 'reason': 'profile_private_wall' if code == 'private' else 'page_not_found', 'message': 'x'})
        self.assertEqual(self.twin(action='check')[1]['state'], 'twin')

    def test_another_accounts_trouble_does_not_end_the_test(self):
        self.go_live()
        self.beat('lane-z', '77', 'someone.else')
        self.post('lane-z', '/api/ext/error', {'code': 'rate_limit', 'message': 'x'}, ig_id='77', handle='someone.else')
        self.assertEqual(self.twin(action='check')[1]['state'], 'twin')
        self.assertTrue(db.get_setting(self.conn, 'cooldown'), 'the workspace-wide pause still applies')

    def test_a_warning_on_another_account_with_the_turtles_selected_alone(self):
        """The real situation: @dihfluencer is warned and only @trashtheturtles may continue."""
        self.setup_two_profiles()
        self.beat(WARNED_LANE, WARNED_ID, 'dihfluencer', tab='tab_scraping_warning')
        db.set_setting(self.conn, 'budget', {'list': 200, 'profile': 100}); self.conn.commit()
        self.twin(action='start_baseline', reviewed=True, lane_id=A)
        self.seed_baseline()
        self.twin(action='finish_baseline', reviewed=True)
        self.assertEqual(self.call('/api/control', {'action': 'resume_selected_accounts', 'collection_stages': ['lists'],
                                                    'accounts': [{'lane_id': A, 'ig_id': TURTLES}]})[0], 200)
        self.assertEqual(self.twin(action='enable', reviewed=True, lane_ids=[A, B])[0], 200)
        self.assertEqual(db.get_setting(self.conn, 'instagram_collection_isolation')['accounts'], {A: TURTLES})
        self.assertFalse(accounts.paused_for(self.conn, self.lane(B)), 'the isolation allows the second profile through the twin')
        self.assertTrue(accounts.paused_for(self.conn, self.lane(WARNED_LANE)))
        # Start/Resume pauses everything not selected; the twin follows its original profile
        self.call('/api/control', {'action': 'resume_selected_accounts', 'collection_stages': ['lists'],
                                   'accounts': [{'lane_id': A, 'ig_id': TURTLES}]})
        self.assertFalse(accounts.paused_for(self.conn, self.lane(B)))
        self.assertTrue(accounts.paused_for(self.conn, self.lane(WARNED_LANE)))
        self.assertFalse(accounts.request_permit(self.conn, WARNED_LANE, kind='list')['granted'])


class CapTests(TwinBase):
    def simulate(self, cap, hours=2, step=10):
        """Both profiles ask for a permit every `step` seconds for `hours`; returns granted [(t, lane)]."""
        self.go_live(per_hour=cap)
        start = now()
        grants, tokens = [], {}
        for i in range(0, int(hours * 3600), step):
            at = start + timedelta(seconds=i)
            for lane in (A, B):
                self.conn.execute('UPDATE accounts SET last_seen=? WHERE lane_id=?', (accounts.iso(at), lane))
                if lane in tokens:
                    accounts.request_permit(self.conn, lane, token=tokens.pop(lane), now=at)
                    continue
                result = accounts.request_permit(self.conn, lane, kind='list', now=at)
                if result['granted']:
                    grants.append((at.timestamp(), lane))
                    tokens[lane] = result['token']
        return grants

    def test_combined_requests_never_exceed_the_cap(self):
        cap = 120
        grants = self.simulate(cap)
        times = [g[0] for g in grants]
        self.assertGreater(len(times), cap * 1.6, 'the cap is used, not starved')
        for i, t in enumerate(times):
            self.assertLessEqual(sum(1 for x in times[:i + 1] if x > t - 3600), cap)
            self.assertLessEqual(sum(1 for x in times[:i + 1] if x > t - account_twin.pacing()['window']), account_twin.pacing()['windowMax'])
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertLess(max(gaps), 5 * 3600 / cap, 'requests are spread out, not bunched then silent')
        self.assertEqual({lane for _, lane in grants}, {A, B}, 'both profiles get turns')
        self.assertEqual(account_twin.config(self.conn)['step']['requests'], len(grants), 'every grant counted once')

    def test_raising_the_cap_raises_the_rate_only_that_far(self):
        self.go_live(per_hour=100)
        cfg = account_twin.config(self.conn)
        cfg['step']['cap_per_hour'] = 200
        account_twin.save(self.conn, cfg); self.conn.commit()
        start = now()
        times, tokens = [], {}
        for i in range(0, 3600, 10):
            at = start + timedelta(seconds=i)
            for lane in (A, B):
                self.conn.execute('UPDATE accounts SET last_seen=? WHERE lane_id=?', (accounts.iso(at), lane))
                if lane in tokens:
                    accounts.request_permit(self.conn, lane, token=tokens.pop(lane), now=at); continue
                result = accounts.request_permit(self.conn, lane, kind='list', now=at)
                if result['granted']:
                    times.append(at.timestamp()); tokens[lane] = result['token']
        self.assertLessEqual(len(times), 200)
        self.assertGreater(len(times), 150)

    def test_replayed_releases_and_refused_asks_do_not_count(self):
        self.go_live()
        permit = accounts.request_permit(self.conn, A, kind='list')
        self.assertTrue(permit['granted'])
        self.assertFalse(accounts.request_permit(self.conn, B, kind='list')['granted'])    # B waits behind A
        accounts.request_permit(self.conn, A, token=permit['token'])
        accounts.request_permit(self.conn, A, token=permit['token'])                          # replay
        self.assertEqual(account_twin.config(self.conn)['step']['requests'], 1)


class ReportTests(TwinBase):
    def test_status_reports_the_proof_metric_and_the_abort_rules(self):
        self.go_live(per_hour=150)
        out = self.twin(action='check')[1]
        self.assertEqual(out['limits']['ceiling_per_hour'], account_twin.single_profile_ceiling(self.conn))
        self.assertEqual(out['limits']['window_requests'], account_twin.pacing()['windowMax'])
        self.assertGreaterEqual(len(out['abort_rules']), 5)
        for key in ('requests_per_hour', 'list_pages_per_hour', 'people', 'bios', 'cap_per_hour', 'utilisation', 'vs_baseline', 'minutes_left'):
            self.assertIn(key, out['current'])
        self.assertEqual(out['baseline']['per_hour'], 150)
