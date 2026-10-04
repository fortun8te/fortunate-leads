"""Pace control: one bounds file, clamping, storage, delivery to the extension, and the stops it must never touch."""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from test_server import Base, db, server
import accounts
import pace
import pace_limits as limits

SHIPPED_DEFAULTS = {'list_gap': [7, 12], 'profile_gap': [35, 70], 'spacing': [2, 5], 'break_every': [40, 60],
                    'break_len': [90, 180], 'window_max': 72}
DEFAULT_ID = 'lg7-12|pg35-70|sp2-5|be40-60|bl90-180|wm72'
# The one deliberate widening: the list gap may go to 4.8-8.2 s, as far as the speed ramp needs to reach 285 pages an hour.
FASTEST = dict(SHIPPED_DEFAULTS, list_gap=[4.8, 8.2])


class LimitsFile(Base):
    """The numbers in extension/lib/pace-limits.js are the shipped pacing. Changing one must be deliberate."""

    def test_defaults_are_exactly_the_shipped_pacing(self):
        self.assertEqual(limits.defaults(), SHIPPED_DEFAULTS)
        self.assertEqual(limits.ident(limits.defaults()), DEFAULT_ID)

    def test_bounds_allow_nothing_faster_than_the_defaults_except_the_list_gap(self):
        self.assertEqual(limits.FIELDS['list_gap']['floor'], [4.8, 8.2])
        for f, d in SHIPPED_DEFAULTS.items():
            spec = limits.FIELDS[f]
            if f == 'list_gap':
                continue
            if spec['kind'] == 'int':
                self.assertEqual(spec['ceiling'], d)       # window_max is a ceiling: smaller is slower
                continue
            if f == 'break_every':                           # more pages between breaks is the fast direction
                self.assertEqual(spec['ceiling'], d)
            else:                                            # shorter waits are the fast direction
                self.assertEqual(spec['floor'], d)

    def test_every_field_default_sits_inside_its_own_bounds_and_the_file_checks_itself(self):
        limits.check(limits.LIMITS)
        broken = json.loads(json.dumps(limits.LIMITS))
        broken['fields']['list_gap']['floor'] = [7, 500]
        with self.assertRaises(RuntimeError):
            limits.check(broken)

    def test_budget_bounds_come_from_the_same_file(self):
        self.assertEqual(accounts.BUDGET_MAX, limits.budget_max())

    def test_the_file_can_be_swapped_without_touching_code(self):
        text = Path(limits.PATH).read_text()
        wide = text.replace('"floor": [4.8, 8.2]', '"floor": [3, 5.5]')
        self.assertNotEqual(text, wide)
        path = Path(self.tmp.name) / 'wide.js'
        path.write_text(wide)
        loaded = limits.load(path)
        self.assertEqual(loaded['fields']['list_gap']['floor'], [3, 5.5])
        with mock.patch.object(limits, 'LIMITS', loaded), mock.patch.object(limits, 'FIELDS', loaded['fields']):
            values, notes = limits.clamp({'list_gap': [3, 5.5]})
            self.assertEqual(values['list_gap'], [3, 5.5])    # faster, from the file alone
            self.assertEqual([n['field'] for n in notes if n['field'] == 'list_gap'], [])


class Clamp(Base):
    def test_values_inside_the_bounds_pass_through(self):
        got, notes = limits.clamp({'list_gap': [9, 14], 'break_every': [20, 30], 'window_max': 40})
        self.assertEqual((got['list_gap'], got['break_every'], got['window_max'], notes), ([9, 14], [20, 30], 40, []))

    def test_faster_than_the_bounds_is_held_at_the_bound_and_reported(self):
        got, notes = limits.clamp({'list_gap': [1, 2], 'profile_gap': [1, 2], 'spacing': [0, 1], 'break_len': [1, 2],
                                   'break_every': [100, 200], 'window_max': 500})
        self.assertEqual(got, FASTEST)
        self.assertEqual({n['field'] for n in notes}, {'list_gap', 'profile_gap', 'spacing', 'break_len', 'break_every', 'window_max'})
        self.assertTrue(all(n['why'] for n in notes))

    def test_slower_than_the_bounds_is_held_at_the_ceiling(self):
        got, _ = limits.clamp({'list_gap': [500, 900], 'window_max': 1})
        self.assertEqual(got['list_gap'], [96, 120])
        self.assertEqual(got['window_max'], 12)

    def test_a_range_keeps_its_jitter_and_a_swapped_range_is_straightened(self):
        got, _ = limits.clamp({'list_gap': [20, 20]})
        self.assertGreaterEqual(got['list_gap'][1], got['list_gap'][0] * 1.25)
        got, notes = limits.clamp({'list_gap': [30, 20]})
        self.assertEqual(got['list_gap'], [20, 30])
        got, _ = limits.clamp({'list_gap': [100, 100]})
        self.assertGreaterEqual(got['list_gap'][1], got['list_gap'][0] * 1.25)
        self.assertLessEqual(got['list_gap'][1], 120)

    def test_one_number_scales_the_default_shape_around_that_centre(self):
        got, notes = limits.clamp({'list_gap': 19})
        self.assertEqual(notes, [])
        self.assertAlmostEqual(sum(got['list_gap']) / 2, 19, delta=0.1)
        self.assertAlmostEqual(got['list_gap'][1] / got['list_gap'][0], 12 / 7, delta=0.02)

    def test_unusable_input_never_becomes_a_value(self):
        for bad in (None, 'fast', True, [1], [1, 2, 3], [math.nan, 5], [math.inf, 9], {'a': 1}):
            got, notes = limits.clamp({'list_gap': bad})
            self.assertEqual(got['list_gap'], [7, 12], bad)
            with self.assertRaises(ValueError):
                limits.clamp({'list_gap': bad}, strict=True)
        got, _ = limits.clamp({'window_max': math.nan})
        self.assertEqual(got['window_max'], 72)

    def test_clamp_is_idempotent_and_always_inside_the_bounds(self):
        import random
        rng = random.Random(4)
        for _ in range(500):
            ask = {f: ([rng.uniform(-50, 1500), rng.uniform(-50, 1500)] if limits.FIELDS[f]['kind'] == 'range' else rng.uniform(-5, 300))
                   for f in limits.ORDER}
            got, _ = limits.clamp(ask)
            again, notes = limits.clamp(got)
            self.assertEqual(got, again)
            self.assertEqual(notes, [])
            for f in limits.ORDER:
                spec = limits.FIELDS[f]
                if spec['kind'] == 'int':
                    self.assertTrue(spec['floor'] <= got[f] <= spec['ceiling'])
                else:
                    lo, hi = got[f]
                    self.assertTrue(spec['floor'][0] <= lo <= spec['ceiling'][0] and spec['floor'][1] <= hi <= spec['ceiling'][1], (f, got[f]))
                    self.assertGreaterEqual(hi, lo * spec['min_spread'] - 1e-9)

    def test_presets(self):
        self.assertEqual(limits.preset('normal'), (SHIPPED_DEFAULTS, []))
        slow, notes = limits.preset('slow')
        self.assertEqual(notes, [])
        for f in ('list_gap', 'profile_gap', 'spacing', 'break_len'):
            self.assertGreater(slow[f][0], SHIPPED_DEFAULTS[f][0])
        self.assertLess(slow['window_max'], 72)
        fast, notes = limits.preset('fast')
        self.assertEqual(fast, dict(SHIPPED_DEFAULTS, list_gap=[5, 9]))   # only the list gap is faster than normal
        self.assertTrue(notes)
        with self.assertRaises(ValueError):
            limits.preset('ludicrous')


class Api(Base):
    EXT_BODY = {'version': '3.9.31', 'state': 'idle', 'tab': 'ok', 'hold': None, 'day': '2026-10-4',
                'today': {'list': 0, 'profile': 0}}

    def beat(self, lane='lane1', ig='5501', handle='turtles', **extra):
        return self.call('/api/ext/heartbeat', dict(self.EXT_BODY, lane_id=lane, account={'ig_id': ig, 'handle': handle}, **extra))[1]

    def pace(self, body):
        return self.call('/api/pace', body)

    def test_everyone_starts_on_the_defaults_and_the_heartbeat_carries_them(self):
        reply = self.beat()
        self.assertEqual(reply['pace']['values'], SHIPPED_DEFAULTS)
        self.assertEqual(reply['pace']['id'], DEFAULT_ID)
        self.assertEqual((reply['pace']['preset'], reply['pace']['scope']), ('normal', 'global'))
        snap = self.call('/api/pace')[1]
        self.assertEqual(snap['global']['preset'], 'normal')
        self.assertEqual(snap['accounts'][0]['values'], SHIPPED_DEFAULTS)

    def test_global_change_reaches_the_next_heartbeat_and_lease_reply(self):
        self.beat()
        code, out = self.pace({'preset': 'slow'})
        self.assertEqual(code, 200)
        self.assertTrue(out['saved'])
        self.assertEqual(out['after']['preset'], 'slow')
        self.assertLess(out['expected']['after']['all_per_hour'], out['expected']['before']['all_per_hour'])
        self.assertEqual(self.beat()['pace']['values']['list_gap'], [14, 24])
        lease = self.call('/api/ext/next?lane=lane1&kinds=list,profile&ig_id=5501&handle=turtles&version=3.9.31')[1]
        self.assertEqual(lease['pace']['values']['list_gap'], [14, 24])

    def test_a_change_applies_in_both_directions(self):
        self.beat()
        self.pace({'preset': 'slow'})
        self.assertEqual(self.beat()['pace']['values']['list_gap'], [14, 24])
        self.pace({'preset': 'normal'})
        self.assertEqual(self.beat()['pace']['values']['list_gap'], [7, 12])
        self.pace({'values': {'list_gap': [20, 30]}})
        self.assertEqual(self.beat()['pace']['values']['list_gap'], [20, 30])

    def test_values_outside_the_bounds_are_clamped_and_reported_never_stored(self):
        self.beat()
        code, out = self.pace({'values': {'list_gap': [1, 2], 'window_max': 500}})
        self.assertEqual(code, 200)
        self.assertEqual({n['field'] for n in out['clamped']}, {'list_gap', 'window_max'})
        self.assertEqual(self.beat()['pace']['values'], FASTEST)
        self.assertEqual(db.get_setting(self.conn, 'pace')['global']['values'], FASTEST)

    def test_bad_input_is_a_plain_400(self):
        self.beat()
        for body, text in (({}, 'Nothing to change'), ({'preset': 'slow', 'reset': True}, 'one of'),
                           ({'values': {'nope': 1}}, 'Unknown pace setting'), ({'values': {'list_gap': 'quick'}}, 'needs'),
                           ({'preset': 'warp'}, 'slow, normal or fast'), ({'scope': 'planet', 'preset': 'slow'}, 'all accounts or one'),
                           ({'scope': 'account', 'lane_id': 'ghost', 'preset': 'slow'}, 'not connected'),
                           ({'budget': {'list': -1}}, '0 means no daily cap'), ({'budget': {'list': True}}, 'whole number'),
                           ({'budget': {'list': 99999}}, 'from 0 to'), ({'budget': {'other': 5}}, 'list and/or profile')):
            code, out = self.pace(body)
            self.assertEqual(code, 400, body)
            self.assertIn(text, out['error'])
        self.assertIsNone(db.get_setting(self.conn, 'pace', None))

    def test_dry_run_saves_nothing(self):
        self.beat()
        code, out = self.pace({'preset': 'slow', 'dry_run': True, 'budget': {'list': 111}})
        self.assertEqual(code, 200)
        self.assertFalse(out['saved'])
        self.assertEqual(out['after']['preset'], 'slow')
        self.assertEqual(self.beat()['pace']['values'], SHIPPED_DEFAULTS)
        self.assertEqual(db.get_setting(self.conn, 'budget')['list'], 3000)

    def test_account_override_applies_to_that_account_only_and_follows_its_identity(self):
        self.beat('lane1', '5501', 'turtles')
        self.beat('lane2', '5502', 'other')
        self.pace({'preset': 'slow', 'scope': 'account', 'lane_id': 'lane1'})
        one, two = self.beat('lane1', '5501', 'turtles')['pace'], self.beat('lane2', '5502', 'other')['pace']
        self.assertEqual((one['scope'], one['values']['list_gap']), ('account', [14, 24]))
        self.assertEqual((two['scope'], two['values']['list_gap']), ('global', [7, 12]))
        # the Chrome profile signs in as someone else: the override belongs to the old identity and stops applying
        swapped = self.beat('lane1', '5509', 'someone.else')['pace']
        self.assertEqual((swapped['scope'], swapped['values']['list_gap']), ('global', [7, 12]))
        self.assertEqual(self.beat('lane1', '5501', 'turtles')['pace']['values']['list_gap'], [14, 24])
        self.pace({'scope': 'account', 'lane_id': 'lane1', 'reset': True})
        self.assertEqual(self.beat('lane1', '5501', 'turtles')['pace']['scope'], 'global')

    def test_global_change_does_not_override_an_account_setting(self):
        self.beat('lane1', '5501', 'turtles')
        self.pace({'preset': 'slow', 'scope': 'account', 'lane_id': 'lane1'})
        self.pace({'values': {'list_gap': [30, 45]}})
        self.assertEqual(self.beat('lane1', '5501', 'turtles')['pace']['values']['list_gap'], [14, 24])

    def test_the_main_account_cannot_be_given_a_pace(self):
        self.beat('main', '5500', 'owner')
        self.conn.execute("UPDATE accounts SET is_main=1 WHERE lane_id='main'")
        self.conn.commit()
        code, out = self.pace({'scope': 'account', 'lane_id': 'main', 'preset': 'slow'})
        self.assertEqual(code, 400)
        self.assertIn('main account', out['error'])

    def test_persists_across_a_server_restart(self):
        self.beat()
        self.pace({'values': {'list_gap': [10, 16], 'break_every': [25, 35]}})
        self.conn.close()
        self.conn = db.connect(server.CFG['db'])        # a fresh connection is what a restarted server has
        self.assertEqual(self.beat()['pace']['values']['list_gap'], [10, 16])
        self.assertEqual(self.call('/api/pace')[1]['global']['values']['break_every'], [25, 35])

    def test_stored_values_are_clamped_again_with_the_bounds_in_force_now(self):
        self.beat()
        self.pace({'values': {'list_gap': [20, 30]}})
        tight = json.loads(json.dumps(limits.LIMITS))
        tight['fields']['list_gap']['ceiling'] = [15, 25]
        with mock.patch.object(limits, 'LIMITS', tight), mock.patch.object(limits, 'FIELDS', tight['fields']):
            self.assertEqual(self.beat()['pace']['values']['list_gap'], [15, 25])
        self.assertEqual(self.beat()['pace']['values']['list_gap'], [20, 30])   # bounds back: the stored choice is untouched

    def test_applied_pace_is_reported_back_and_compared(self):
        self.beat()
        self.pace({'preset': 'slow'})
        self.beat(pace={'id': DEFAULT_ID, 'preset': 'normal', 'scope': 'global'})
        acct = self.call('/api/pace')[1]['accounts'][0]
        self.assertFalse(acct['applied']['matches'])
        slow = limits.ident(limits.preset('slow')[0])
        self.beat(pace={'id': slow, 'preset': 'slow', 'scope': 'global'})
        acct = self.call('/api/pace')[1]['accounts'][0]
        self.assertTrue(acct['applied']['matches'])
        self.beat(pace={'id': 'DROP TABLE x;', 'preset': 'x'})          # junk is ignored, never stored
        self.assertEqual(self.call('/api/pace')[1]['accounts'][0]['applied']['id'], slow)

    def test_an_extension_that_reports_no_pace_shows_as_not_reporting(self):
        self.beat()
        self.assertIsNone(self.call('/api/pace')[1]['accounts'][0]['applied'])

    def test_readout_says_what_sets_the_pace_and_when_a_daily_limit_arrives(self):
        self.beat(today={'list': 380, 'profile': 0}, budget={'list': 400, 'profile': 100})
        self.pace({'budget': {'list': 400, 'profile': 100}})
        acct = self.call('/api/pace')[1]['accounts'][0]
        self.assertEqual(acct['expected']['binds'], 'daily_limit')
        self.assertEqual(acct['expected']['text'], 'The daily limit sets this pace.')
        self.assertTrue(any('20 left today' in line for line in acct['why']))
        self.assertTrue(any(line.startswith('Pages: one every 7 to 12 seconds') for line in acct['why']))
        self.pace({'budget': {'list': 0, 'profile': 0}})
        acct = self.call('/api/pace')[1]['accounts'][0]
        self.assertIn(acct['expected']['binds'], ('gap', 'gap_and_breaks'))

    def test_window_cap_is_named_when_it_is_what_limits_the_pace(self):
        self.beat()
        self.pace({'values': {'window_max': 12}})
        self.assertEqual(self.call('/api/pace')[1]['accounts'][0]['expected']['binds'], 'window')

    def test_budget_through_the_pace_call_uses_the_existing_stores(self):
        self.beat()
        code, out = self.pace({'budget': {'list': 400, 'profile': 100}})
        self.assertEqual(code, 200)
        self.assertEqual(db.get_setting(self.conn, 'budget'), {'list': 400, 'profile': 100})
        self.assertEqual(out['budget']['after'], {'list': 400, 'profile': 100})
        self.assertEqual(self.beat()['budget'], {'list': 400, 'profile': 100})
        self.pace({'budget': {'list': 250}, 'scope': 'account', 'lane_id': 'lane1'})
        self.assertEqual(self.beat()['budget'], {'list': 250, 'profile': 100})
        self.assertEqual(db.get_setting(self.conn, 'budget')['list'], 400)

    def test_every_account_view_has_the_safety_lines_and_no_pace_can_lift_a_stop(self):
        self.beat()
        db.set_setting(self.conn, 'cooldown', (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        db.set_setting(self.conn, 'paused_lists', True)
        self.conn.commit()
        self.pace({'preset': 'fast'})
        reply = self.beat()
        self.assertEqual(reply['stages'], {'list': False, 'profile': False})   # the shared hold still stops both
        snap = self.call('/api/pace')[1]
        self.assertTrue(any('waiting after an Instagram response' in item['text'] for item in snap['safety']['now']))
        self.assertTrue(snap['safety']['fixed'])


class StopsAreIndependentOfPace(Base):
    """A pace only decides how long to wait between requests that are already allowed."""

    def setUp(self):
        super().setUp()
        self.call('/api/scraper/seeds', {'handles': ['one', 'two'], 'directions': ['followers']})
        self.call('/api/ext/heartbeat', {'lane_id': 'lane1', 'account': {'ig_id': '5501', 'handle': 'turtles'}, 'version': '3.9.31',
                                         'state': 'idle', 'tab': 'ok', 'hold': None, 'day': '2026-10-4', 'today': {'list': 0, 'profile': 0}})

    def lease(self):
        return self.call('/api/ext/next?lane=lane1&version=3.9.31&ig_id=5501&handle=turtles')[1]

    def acquire(self, job):
        return self.call('/api/ext/request', dict(action='acquire', kind='list', lane_id='lane1', job_id=job['id'],
                                                  lease_token=job['lease_token'], account={'ig_id': '5501', 'handle': 'turtles'}))[1]

    def test_permit_gate_spacing_is_not_a_pace_value(self):
        self.assertEqual(accounts.REQUEST_SPACING_SECONDS, 2)
        self.assertNotIn('REQUEST', ' '.join(limits.ORDER).upper())

    def test_workspace_hold_warning_and_pauses_still_deny_permits_at_the_fastest_setting(self):
        self.call('/api/pace', {'preset': 'fast'})
        job = self.lease()['job']
        self.assertTrue(self.acquire(job)['granted'])
        # release, then each stop in turn
        gate = db.get_setting(self.conn, 'instagram_request_gate')
        self.call('/api/ext/request', {'action': 'release', 'lane_id': 'lane1', 'token': gate['active']['token'],
                                       'account': {'ig_id': '5501', 'handle': 'turtles'}})
        for key, value in (('cooldown', '2099-01-01T00:00:00Z'), ('paused_lists', True), ('paused', True)):
            before = db.get_setting(self.conn, key, None)
            db.set_setting(self.conn, key, value)
            self.conn.commit()
            self.assertFalse(self.acquire(job)['granted'], key)
            db.set_setting(self.conn, key, before)
            self.conn.commit()

    def test_a_spent_daily_budget_still_denies_at_the_fastest_setting(self):
        self.call('/api/pace', {'preset': 'fast', 'budget': {'list': 1}})
        job = self.lease()['job']
        self.conn.execute('UPDATE accounts SET today=? WHERE lane_id=?', (json.dumps({'list': 1, 'profile': 0}), 'lane1'))
        self.conn.commit()
        self.assertFalse(self.acquire(job)['granted'])

    def test_pace_never_changes_cooldown_bookkeeping_on_the_server(self):
        self.conn.execute("UPDATE accounts SET list_cool_until=?, hold='login' WHERE lane_id='lane1'",
                          ((datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),))
        self.conn.commit()
        before = tuple(self.conn.execute('SELECT list_cool_until,hold,paused FROM accounts').fetchone())
        for body in ({'preset': 'slow'}, {'preset': 'fast'}, {'values': {'list_gap': [60, 90]}}, {'reset': True}):
            self.call('/api/pace', body)
        self.assertEqual(tuple(self.conn.execute('SELECT list_cool_until,hold,paused FROM accounts').fetchone()), before)


class NoDailyCap(Base):
    def test_zero_is_no_cap_and_the_readout_says_so(self):
        self.call('/api/ext/heartbeat', {'lane_id': 'lane1', 'account': {'ig_id': '5501', 'handle': 't'}, 'version': '3.9.31',
                                         'state': 'idle', 'tab': 'ok', 'hold': None, 'day': '2026-10-4', 'today': {'list': 5000, 'profile': 900}})
        code, out = self.call('/api/pace', {'budget': {'list': 0, 'profile': 0}})
        self.assertEqual(code, 200)
        self.assertEqual(db.get_setting(self.conn, 'budget'), {'list': 0, 'profile': 0})
        acct = self.call('/api/pace')[1]['accounts'][0]
        self.assertIn('No daily cap on pages.', acct['why'])
        self.assertNotEqual(acct['expected']['binds'], 'daily_limit')
        self.assertEqual(limits.budget_default(), {'list': 0, 'profile': 0})

    def test_no_cap_never_stops_the_server_permit(self):
        self.call('/api/pace', {'budget': {'list': 0, 'profile': 0}})
        self.call('/api/scraper/seeds', {'handles': ['one'], 'directions': ['followers']})
        self.call('/api/ext/heartbeat', {'lane_id': 'lane1', 'account': {'ig_id': '5501', 'handle': 't'}, 'version': '3.9.31',
                                         'state': 'idle', 'tab': 'ok', 'hold': None, 'day': '2026-10-4', 'today': {'list': 99999, 'profile': 99999}})
        job = self.call('/api/ext/next?lane=lane1&version=3.9.31&ig_id=5501&handle=t')[1]['job']
        got = self.call('/api/ext/request', dict(action='acquire', kind='list', lane_id='lane1', job_id=job['id'], lease_token=job['lease_token'],
                                                 account={'ig_id': '5501', 'handle': 't'}))[1]
        self.assertTrue(got['granted'])


class SafetyStopsWinWithNoCap(Base):
    """No daily cap and the fastest pace: the true safety stops still deny every request."""

    def setUp(self):
        super().setUp()
        self.call('/api/pace', {'budget': {'list': 0, 'profile': 0}})
        self.call('/api/pace', {'preset': 'fast'})
        self.call('/api/scraper/seeds', {'handles': ['one', 'two'], 'directions': ['followers']})
        self.beat('lane1', '5501', 'turtles')

    def beat(self, lane, ig, handle):
        return self.call('/api/ext/heartbeat', {'lane_id': lane, 'account': {'ig_id': ig, 'handle': handle}, 'version': '3.9.31',
                                                'state': 'idle', 'tab': 'ok', 'hold': None, 'day': '2026-10-4', 'today': {'list': 0, 'profile': 0}})[1]

    def acquire(self, lane, ig, handle):
        job = self.call(f'/api/ext/next?lane={lane}&version=3.9.31&ig_id={ig}&handle={handle}')[1].get('job')
        if not job:
            return {'granted': False, 'nojob': True}
        return self.call('/api/ext/request', dict(action='acquire', kind='list', lane_id=lane, job_id=job['id'],
                                                  lease_token=job['lease_token'], account={'ig_id': ig, 'handle': handle}))[1]

    def test_baseline_grants(self):
        self.assertTrue(self.acquire('lane1', '5501', 'turtles')['granted'])

    def test_scraping_warning_stops_everything(self):
        job = self.call('/api/ext/next?lane=lane1&version=3.9.31&ig_id=5501&handle=turtles')[1]['job']
        server.record_scraping_warning(self.conn, 'lane1', '5501')
        self.conn.commit()
        got = self.call('/api/ext/request', dict(action='acquire', kind='list', lane_id='lane1', job_id=job['id'],
                                                 lease_token=job['lease_token'], account={'ig_id': '5501', 'handle': 'turtles'}))[1]
        self.assertFalse(got['granted'])
        self.assertEqual(self.beat('lane1', '5501', 'turtles')['stages'], {'list': False, 'profile': False})

    def test_a_login_or_security_hold_on_the_account_stops_it(self):
        self.call('/api/ext/heartbeat', {'lane_id': 'lane1', 'account': {'ig_id': '5501', 'handle': 'turtles'}, 'version': '3.9.31',
                                         'state': 'idle', 'tab': 'tab_challenge', 'hold': 'challenge', 'day': '2026-10-4', 'today': {'list': 0, 'profile': 0}})
        self.assertFalse(self.acquire('lane1', '5501', 'turtles')['granted'])

    def test_three_rate_limits_keep_the_workspace_stopped_whatever_the_pace(self):
        for n in range(3):
            self.call('/api/ext/error', {'lane_id': 'lane1', 'account': {'ig_id': '5501', 'handle': 'turtles'}, 'code': 'rate_limit',
                                         'kind': 'list', 'event_id': f'e{n}', 'retry_at': None})
        self.assertIsNotNone(server.workspace_cooldown(self.conn, datetime.now(timezone.utc)))
        self.assertFalse(self.acquire('lane1', '5501', 'turtles')['granted'])
        self.call('/api/pace', {'preset': 'fast'})
        self.assertEqual(self.beat('lane1', '5501', 'turtles')['stages'], {'list': False, 'profile': False})

    def test_the_main_account_never_gets_a_permit(self):
        self.beat('main', '5500', 'owner')
        self.conn.execute("UPDATE accounts SET is_main=1 WHERE lane_id='main'")
        self.conn.commit()
        self.assertFalse(self.acquire('main', '5500', 'owner')['granted'])
