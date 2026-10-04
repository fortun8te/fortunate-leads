"""The automatic speed ramp on virtual time: steps, ceiling, every adverse event, recovery, persistence, logging."""
import json
import shutil
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from test_server import Base, db
import accounts
import control
import pace
import pace_limits as limits
import ramp

T0 = datetime(2026, 10, 5, 8, 0, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
LANE, IG, HANDLE = 'lane1', '5501', 'trashtheturtles'


def at(delta):
    return T0 + delta


class RampBase(Base):
    real_bounds = False

    def setUp(self):
        super().setUp()
        if not self.real_bounds:
            # The climbing logic is tested with a wide list-gap floor so several steps fit below the ceiling.
            # The real, narrow floor is covered in class Bounds.
            patch = mock.patch.dict(limits.FIELDS['list_gap'], {'floor': [1.0, 1.7]})
            patch.start()
            self.addCleanup(patch.stop)
        accounts.touch(self.conn, LANE, {'ig_id': IG, 'handle': HANDLE})
        self.conn.commit()
        self.logs = Path(self.tmp.name) / 'logs' / 'events.jsonl'

    # ---- helpers
    def online(self, now):
        self.conn.execute('UPDATE accounts SET last_seen=?', (ramp.iso(now),))
        self.conn.commit()

    def feed(self, t0, t1, per_hour, **kw):
        """Saved list pages at an even rate between t0 and t1."""
        step = timedelta(seconds=3600 / per_hour)
        t = t0 + step
        rows = []
        while t <= t1:
            rows.append((ramp.iso(t), LANE, 'list', 'following', 'page', None, 200, kw.get('returned', 190)))
            t += step
        self.conn.executemany('INSERT INTO collector_events(at,lane,kind,direction,outcome,reason,http_status,returned_count) VALUES(?,?,?,?,?,?,?,?)', rows)
        self.conn.commit()

    def event(self, when, outcome, reason=None, status=None):
        self.conn.execute('INSERT INTO collector_events(at,lane,kind,direction,outcome,reason,http_status) VALUES(?,?,?,?,?,?,?)',
                          (ramp.iso(when), LANE, 'list', 'following', outcome, reason, status))
        self.conn.commit()

    def turn_on(self, **extra):
        ramp.post(self.conn, {}, dict({'action': 'on', 'step_factor': 1.2}, **extra))
        self.online(T0)
        ramp.tick(self.conn, T0)       # enrols the account

    def lane(self):
        return ramp.config(self.conn)['lanes'][self.row()['lane_id']]

    def row(self):
        return self.conn.execute('SELECT * FROM accounts WHERE lane_id=?', (LANE,)).fetchone()

    def gap(self):
        return pace.resolve(self.conn, self.row())['values']['list_gap']

    def run_window(self, start, per_hour, hours=2):
        """Feed one clean window and tick at its end; returns the end time."""
        end = start + hours * H
        self.feed(start, end, per_hour)
        self.online(end)
        ramp.tick(self.conn, end)
        return end

    def last_action(self):
        return self.lane()['history'][-1]['action']

    def log(self):
        return [json.loads(l) for l in self.logs.read_text().splitlines()] if self.logs.exists() else []

    def logged(self, name):
        return [e for e in self.log() if e['component'] == 'ramp' and e['event'] == name]


class Steps(RampBase):
    def test_off_by_default_and_changes_nothing(self):
        self.online(T0)
        self.assertFalse(ramp.config(self.conn)['enabled'])
        self.assertFalse(ramp.config(self.conn)['auto_resume'])
        self.assertFalse(ramp.tick(self.conn, T0))
        self.feed(T0, T0 + 3 * H, 200)
        self.assertFalse(ramp.tick(self.conn, T0 + 3 * H))
        self.assertEqual(self.gap(), [7, 12])
        self.assertEqual(ramp.config(self.conn)['lanes'], {})

    def test_clean_windows_step_up_by_the_factor_then_clamp_at_the_ceiling(self):
        self.turn_on()
        end = self.run_window(T0, 200)                       # 200 pages/h measured, so the first target is 1.2x = 240
        self.assertEqual(self.last_action(), 'step_up')
        st = self.lane()
        self.assertEqual(st['level'], 1)
        self.assertAlmostEqual(st['levels'][1]['predicted'], 240, delta=6)    # 1.2x of the 200 measured
        self.assertLess(self.gap()[0], 7)                    # the list gap narrowed through the pace API
        end = self.run_window(end, 240)                      # 240 * 1.2 = 288 is above 285: clipped to the ceiling
        self.assertEqual(self.last_action(), 'step_up')
        self.assertTrue(278 <= self.lane()['levels'][2]['predicted'] <= 285.5)
        self.assertEqual(self.lane()['mode'], 'at_ceiling')
        gap = self.gap()
        end = self.run_window(end, 284)                      # at the ceiling: hold, never past it
        self.assertEqual(self.last_action(), 'at_ceiling')
        self.assertEqual(self.gap(), gap)
        self.assertEqual(self.lane()['level'], 2)
        text = '\n'.join(ramp.status(self.conn, end)['lines'])
        self.assertIn('at the ceiling', text)
        self.assertIn('unmeasured', text)

    def test_a_step_with_no_measurable_benefit_is_undone_and_not_repeated(self):
        self.turn_on(step_factor=1.5)
        end = self.run_window(T0, 160)
        self.assertEqual(self.last_action(), 'step_up')
        end = self.run_window(end, 161)
        self.assertEqual(self.last_action(), 'step_down')
        self.assertEqual(self.lane()['level'], 0)
        self.assertEqual(self.gap(), [7, 12])
        self.run_window(end, 160)
        self.assertEqual(self.last_action(), 'hold')
        self.assertEqual(self.lane()['mode'], 'no_effect')

    def test_user_ceiling_above_the_proven_is_explicit_and_labelled_unmeasured(self):
        out = ramp.post(self.conn, {}, {'action': 'set', 'ceiling': 330})
        text = '\n'.join(out['lines'])
        self.assertEqual(out['ceiling'], 330)
        self.assertTrue(out['above_proven'])
        self.assertIn('above the proven 285', text)
        self.assertIn('Nothing above 285', text)
        self.assertTrue(self.logged('ceiling_set')[-1]['above_proven'])
        self.assertEqual(ramp.config(self.conn)['ceiling'], 330)
        self.assertEqual(ramp.config(self.conn)['enabled'], False)    # setting a ceiling never turns the ramp on
        with self.assertRaises(ValueError):
            ramp.post(self.conn, {}, {'action': 'set', 'ceiling': True})

    def test_a_window_without_enough_runtime_does_not_count(self):
        self.turn_on()
        self.feed(T0, T0 + 1 * H, 200)                       # only the first hour of the 2 h window ran
        end = T0 + 2 * H
        self.online(end)
        ramp.tick(self.conn, end)
        self.assertEqual(self.lane()['level'], 0)
        self.assertEqual(self.last_action(), 'hold')
        self.assertIn('ran', self.lane()['history'][-1]['reason'])
        self.assertEqual(ramp.parse(self.lane()['window_start']), end)       # the window starts again
        self.assertEqual(self.gap(), [7, 12])

    def test_too_few_pages_cannot_be_measured(self):
        self.turn_on()
        self.feed(T0, T0 + 2 * H, 12)
        self.online(T0 + 2 * H)
        ramp.tick(self.conn, T0 + 2 * H)
        self.assertEqual(self.lane()['level'], 0)

    def test_the_window_is_not_checked_early(self):
        self.turn_on()
        self.feed(T0, T0 + 90 * timedelta(minutes=1), 200)
        self.online(T0 + timedelta(minutes=90))
        ramp.tick(self.conn, T0 + timedelta(minutes=90))
        self.assertEqual(self.lane()['level'], 0)
        self.assertEqual(self.lane()['history'][-1]['action'], 'enrolled')


class Bounds(RampBase):
    real_bounds = True

    def test_never_below_the_lower_gap_bound_in_the_bounds_file(self):
        floor = limits.FIELDS['list_gap']['floor']
        plan = ramp.plan_step(limits.defaults(), 230, 1.5, 285)
        self.assertGreaterEqual(plan['values']['list_gap'][0], floor[0])
        self.assertGreaterEqual(plan['values']['list_gap'][1], floor[1])
        wild = ramp.plan_step(limits.defaults(), 230, 1.5, 5000)         # a huge user ceiling still stops at the bounds
        self.assertEqual(wild['values']['list_gap'], floor)
        self.assertTrue(wild['at_bound'] or wild['predicted'] < wild['target'])

    def test_shipped_floor_is_preserved_even_when_a_high_ceiling_is_requested(self):
        self.assertEqual(limits.FIELDS['list_gap']['floor'], [7, 12])
        plan = ramp.plan_step(limits.defaults(), 230, 4, 5000)
        self.assertEqual(plan['values']['list_gap'], [7, 12])

    def test_a_slower_start_can_return_toward_but_never_exceed_shipped_pace(self):
        pace.post(self.conn, {}, {'scope': 'account', 'lane_id': LANE, 'values': {'list_gap': [14, 24]}})
        self.turn_on(step_factor=1.5)
        end = self.run_window(T0, 150)
        self.assertEqual(self.last_action(), 'step_up')
        self.assertGreaterEqual(self.gap()[0], 7)
        self.assertGreaterEqual(self.gap()[1], 12)
        for rate in (220, 250, 280):
            end = self.run_window(end, rate)
            self.assertGreaterEqual(self.gap()[0], 7)
            self.assertGreaterEqual(self.gap()[1], 12)

    def test_default_pace_cannot_be_sped_up_by_the_ramp(self):
        self.turn_on(step_factor=1.5)
        self.run_window(T0, 160)
        self.assertEqual(self.last_action(), 'at_bound')
        self.assertEqual(self.gap(), [7, 12])



class Eligibility(RampBase):
    def test_main_account_is_never_ramped(self):
        self.conn.execute('UPDATE accounts SET is_main=1')
        self.conn.commit()
        self.turn_on()
        self.assertEqual(ramp.config(self.conn)['lanes'], {})

    def test_a_held_or_warned_account_never_starts(self):
        self.conn.execute("UPDATE accounts SET hold='challenge'")
        self.conn.commit()
        self.turn_on()
        self.assertEqual(ramp.config(self.conn)['lanes'], {})

    def test_only_opted_in_handles_ramp(self):
        accounts.touch(self.conn, 'lane2', {'ig_id': '9902', 'handle': 'someoneelse'})
        self.conn.commit()
        self.turn_on()
        self.assertEqual(list(ramp.config(self.conn)['lanes']), [LANE])

    def test_identity_change_discards_the_state(self):
        self.turn_on()
        self.conn.execute("UPDATE accounts SET ig_id='7777'")
        self.conn.commit()
        ramp.tick(self.conn, T0 + 60 * timedelta(seconds=1))
        self.assertEqual(self.logged('identity_changed')[-1]['level'], 'warn')


class Adverse(RampBase):
    def climbed(self):
        self.turn_on()
        end = self.run_window(T0, 200)
        self.assertEqual(self.lane()['level'], 1)
        return end

    def test_a_429_steps_back_to_the_previous_level_and_holds_two_clean_windows(self):
        end = self.climbed()
        base_gap = self.lane()['levels'][0]['values']['list_gap']
        self.feed(end, end + 30 * timedelta(minutes=1), 240)
        self.event(end + timedelta(minutes=31), 'rate_limit', status=429)
        ramp.tick(self.conn, end + timedelta(minutes=32))
        self.assertEqual(self.gap(), base_gap)
        self.assertEqual(self.lane()['level'], 0)
        self.assertEqual(self.lane()['hold_left'], 2)
        self.assertEqual(self.lane()['mode'], 'holding')
        down = self.logged('step_down')[-1]
        self.assertEqual(down['kind'], 'rate_limit')
        self.assertEqual(down['http_status'], 429)
        # two clean windows hold; the third steps up again
        w = end + timedelta(minutes=32)
        w = self.run_window(w, 200)
        self.assertEqual(self.last_action(), 'hold')
        w = self.run_window(w, 200)
        self.assertEqual(self.last_action(), 'hold')
        self.assertEqual(self.lane()['level'], 0)
        w = self.run_window(w, 200)
        self.assertEqual(self.last_action(), 'step_up')

    def test_a_soft_block_does_the_same(self):
        end = self.climbed()
        self.event(end + timedelta(minutes=5), 'soft_block')
        ramp.tick(self.conn, end + timedelta(minutes=6))
        self.assertEqual(self.lane()['level'], 0)
        self.assertEqual(self.logged('step_down')[-1]['kind'], 'soft_block')

    def test_a_second_limit_in_a_day_stops_the_ramp_for_a_person(self):
        end = self.climbed()
        self.event(end + timedelta(minutes=5), 'rate_limit', status=429)
        ramp.tick(self.conn, end + timedelta(minutes=6))
        self.event(end + timedelta(hours=3), 'rate_limit', status=429)
        ramp.tick(self.conn, end + timedelta(hours=3, minutes=1))
        self.assertEqual(self.lane()['mode'], 'stopped')
        self.assertIn('ops/set-pace --ramp resume', '\n'.join(ramp.status(self.conn, end)['lines']))
        n = len(self.lane()['history'])
        ramp.tick(self.conn, end + timedelta(hours=9))        # stopped: nothing happens by itself
        self.assertEqual(len(self.lane()['history']), n)

    def stops(self, setup):
        end = self.climbed()
        setup()
        ramp.tick(self.conn, end + timedelta(minutes=5))
        st = self.lane()
        self.assertEqual(st['mode'], 'stopped')
        self.assertEqual(st['level'], 0)                      # and back down a level
        self.assertTrue(self.logged('step_down')[-1]['kind'] == 'stop')
        return st

    def test_challenge_and_login_events_stop_the_ramp(self):
        self.stops(lambda: self.event(T0 + 2 * H + timedelta(minutes=2), 'challenge'))

    def test_login_event_stops_the_ramp(self):
        self.stops(lambda: self.event(T0 + 2 * H + timedelta(minutes=2), 'login'))

    def test_an_account_hold_stops_the_ramp(self):
        self.stops(lambda: (self.conn.execute("UPDATE accounts SET hold='challenge'"), self.conn.commit()))

    def test_a_scraping_warning_stops_the_ramp(self):
        self.stops(lambda: (db.set_setting(self.conn, 'instagram_scraping_warning', {'message': 'warning', 'lane': LANE}), self.conn.commit()))

    def test_an_unconfirmed_request_stops_the_ramp(self):
        self.stops(lambda: (db.set_setting(self.conn, 'instagram_request_attention', {'message': 'no end', 'lane': LANE}), self.conn.commit()))

    def freezes(self, setup, expect):
        end = self.climbed()
        setup()
        self.feed(end, end + 2 * H, 240)
        self.online(end + 2 * H)
        setup()
        ramp.tick(self.conn, end + 2 * H)
        self.assertEqual(self.lane()['level'], 1, expect)
        self.assertEqual(self.last_action(), 'freeze', expect)
        self.assertEqual(self.logged('freeze')[-1]['level'], 'warn')
        return end + 2 * H

    def test_a_cooldown_freezes_instead_of_stepping(self):
        until = ramp.iso(T0 + 5 * H)
        self.freezes(lambda: (self.conn.execute('UPDATE accounts SET cooldown_until=?', (until,)), self.conn.commit()), 'cooldown')

    def test_a_paused_collection_freezes(self):
        self.freezes(lambda: (db.set_setting(self.conn, 'paused_lists', True), self.conn.commit()), 'pause')

    def test_a_low_disk_freezes(self):
        usage = namedtuple('usage', 'total used free')
        end = self.climbed()
        with mock.patch.object(ramp.shutil, 'disk_usage', return_value=usage(100 << 30, 99 << 30, 1 << 30)):
            self.feed(end, end + 2 * H, 240)
            self.online(end + 2 * H)
            ramp.tick(self.conn, end + 2 * H)
        self.assertEqual(self.lane()['level'], 1)
        self.assertEqual(self.last_action(), 'freeze')
        self.assertIn('disk', self.lane()['history'][-1]['reason'])

    def test_repeated_home_redirects_step_back_one_level(self):
        end = self.climbed()
        for m in (20, 50):
            self.event(end + timedelta(minutes=m), 'other', 'list_html_home_redirect')
        self.feed(end, end + 2 * H, 240)
        self.online(end + 2 * H)
        self.conn.execute("UPDATE accounts SET last_seen=?", (ramp.iso(end + 2 * H),))
        for m in range(10, 121, 10):                         # the worker ticks all the time
            ramp.tick(self.conn, end + timedelta(minutes=m))
        self.assertEqual(self.lane()['level'], 0)
        self.assertIn('redirect', self.logged('step_down')[-1]['reason'])

    def test_extension_offline_at_the_decision_freezes(self):
        end = self.climbed()
        self.feed(end, end + 2 * H, 240)
        self.conn.execute('UPDATE accounts SET last_seen=?', (ramp.iso(end + 2 * H - timedelta(minutes=10)),))
        self.conn.commit()
        ramp.tick(self.conn, end + 2 * H)
        self.assertEqual(self.lane()['level'], 1)
        self.assertEqual(self.last_action(), 'freeze')
        self.assertIn('offline', self.lane()['history'][-1]['reason'])

    def test_response_time_up_is_an_early_warning_then_a_step_back(self):
        end = self.climbed()
        # a clean window at the new level sets the response-time baseline (about 15 s per page at 240/h)
        end = self.run_window(end, 240)
        end = self.run_window(end, 280)                      # at the ceiling: a plain hold, baseline overhead recorded
        level = self.lane()['level']
        self.assertEqual(self.last_action(), 'at_ceiling')
        # then pages arrive much slower than the pace allows: extra time per page more than doubles
        end = self.run_window(end, 150)
        self.assertEqual(self.last_action(), 'hold')
        self.assertIn('response time is up', self.lane()['history'][-1]['reason'])
        self.assertEqual(self.lane()['level'], level)
        end = self.run_window(end, 80)
        self.assertEqual(self.last_action(), 'step_down')
        self.assertEqual(self.lane()['level'], level - 1)
        self.assertEqual(self.logged('step_down')[-1]['previous_overhead_s'] > 0, True)

    def test_a_step_that_made_things_worse_is_undone(self):
        self.turn_on()
        end = self.run_window(T0, 200)
        self.assertEqual(self.lane()['level'], 1)
        end = self.run_window(end, 120)                       # slower than before the step, despite the faster pace
        self.assertEqual(self.last_action(), 'step_down')
        self.assertEqual(self.lane()['level'], 0)
        self.assertEqual(self.lane()['hold_left'], 2)

    def test_an_empty_page_run_freezes(self):
        end = self.climbed()
        self.feed(end, end + 2 * H, 240, returned=0)
        self.online(end + 2 * H)
        ramp.tick(self.conn, end + 2 * H)
        self.assertEqual(self.last_action(), 'freeze')
        self.assertEqual(self.lane()['level'], 1)

    def test_changing_the_pace_by_hand_makes_the_ramp_stand_down(self):
        self.turn_on()
        pace.post(self.conn, {}, {'scope': 'account', 'lane_id': LANE, 'values': {'list_gap': [20, 30]}})
        self.feed(T0, T0 + 2 * H, 200)
        self.online(T0 + 2 * H)
        ramp.tick(self.conn, T0 + 2 * H)
        self.assertEqual(self.lane()['mode'], 'manual')
        self.assertEqual(self.gap(), [20, 30])               # left exactly as the person set it
        self.assertTrue(self.logged('stand_down'))


class Recovery(RampBase):
    def turn_on(self, **extra):
        super().turn_on(**dict({'auto_resume': True}, **extra))

    def test_a_long_outage_resumes_one_level_lower_never_from_the_top(self):
        self.turn_on()
        end = self.run_window(T0, 200)
        end = self.run_window(end, 240)
        self.assertEqual(self.lane()['level'], 2)
        ramp.tick(self.conn, end + timedelta(minutes=2))      # sees the last page
        self.online(end + 3 * H)
        ramp.tick(self.conn, end + timedelta(minutes=45))     # nothing arrived for 45 minutes (server down, Mac asleep, Chrome closed)
        back = end + 3 * H                                    # collection returns
        self.feed(back, back + timedelta(minutes=10), 240)
        self.online(back + timedelta(minutes=10))
        ramp.tick(self.conn, back + timedelta(minutes=10))
        self.assertEqual(self.lane()['level'], 1)
        self.assertEqual(self.lane()['hold_left'], 1)
        entry = self.logged('step_down')[-1]
        self.assertEqual(entry['kind'], 'outage')
        self.assertGreaterEqual(entry['gap_min'], 150)
        self.assertEqual(ramp.parse(self.lane()['window_start']) >= back, True)

    def test_a_short_gap_is_not_an_outage(self):
        self.turn_on()
        end = self.run_window(T0, 200)
        ramp.tick(self.conn, end + timedelta(minutes=2))
        self.feed(end + timedelta(minutes=12), end + timedelta(minutes=30), 240)
        self.online(end + timedelta(minutes=30))
        ramp.tick(self.conn, end + timedelta(minutes=30))
        self.assertEqual(self.lane()['level'], 1)
        self.assertFalse(self.logged('step_down'))

    def test_the_server_being_away_restarts_the_window_and_is_logged(self):
        self.turn_on()
        ramp.tick(self.conn, T0 + timedelta(minutes=1))
        self.online(T0 + 3 * H)
        ramp.tick(self.conn, T0 + 3 * H)
        self.assertTrue(self.logged('server_gap'))
        self.assertEqual(ramp.parse(self.lane()['window_start']), T0 + 3 * H)

    def pause_like_the_server(self, hit_at, code='rate_limit'):
        self.event(hit_at, code, status=429)
        ramp.record_protective_stop(self.conn, LANE, code, hit_at)
        db.set_setting(self.conn, 'paused_lists', True)
        db.set_setting(self.conn, 'paused_bios', True)
        db.set_setting(self.conn, 'cooldown', ramp.iso(hit_at + timedelta(minutes=15)))
        self.conn.commit()

    def test_auto_resume_after_a_rate_limit_restarts_collection_one_level_lower(self):
        self.turn_on()
        end = self.run_window(T0, 200)
        hit = end + timedelta(minutes=10)
        self.pause_like_the_server(hit)
        ramp.tick(self.conn, hit + timedelta(minutes=1))
        self.assertEqual(self.lane()['level'], 0)
        ramp.tick(self.conn, hit + timedelta(minutes=90))     # too early: the rest is 2 hours
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
        self.online(hit + 2 * H + timedelta(minutes=5))
        ramp.tick(self.conn, hit + 2 * H + timedelta(minutes=5))
        self.assertFalse(db.get_setting(self.conn, 'paused_lists'))
        self.assertFalse(db.get_setting(self.conn, 'paused_bios'))
        ev = self.logged('auto_resume')[-1]
        self.assertEqual(ev['code'], 'rate_limit')
        self.assertEqual(ev['level'], 'warn')
        self.assertGreaterEqual(ev['rested_min'], 120)
        self.assertEqual(self.lane()['level'], 0)             # still the lower level: never from the top

    def test_lists_only_recovery_keeps_bios_paused(self):
        db.set_setting(self.conn, 'paused_bios', True)
        self.conn.commit()
        self.turn_on()
        hit = T0 + timedelta(minutes=10)
        self.pause_like_the_server(hit)
        ramp.tick(self.conn, hit + timedelta(minutes=1))
        self.online(hit + 3 * H)
        ramp.tick(self.conn, hit + 3 * H)
        self.assertFalse(db.get_setting(self.conn, 'paused_lists'))
        self.assertTrue(db.get_setting(self.conn, 'paused_bios'))

    def test_operator_stop_invalidates_recovery_even_when_already_paused(self):
        self.turn_on()
        hit = T0 + timedelta(minutes=10)
        self.pause_like_the_server(hit)
        ramp.tick(self.conn, hit + timedelta(minutes=1))
        control.apply(self.conn, {'stage': 'collection', 'action': 'pause'})
        self.online(hit + 3 * H)
        ramp.tick(self.conn, hit + 3 * H)
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
        self.assertTrue(db.get_setting(self.conn, 'paused_bios'))
        self.assertFalse(self.logged('auto_resume'))

    def test_unproven_pause_is_never_auto_resumed(self):
        self.turn_on()
        hit = T0 + timedelta(minutes=10)
        self.pause_like_the_server(hit)
        db.set_setting(self.conn, 'ramp_protective_stop', None)
        self.conn.commit()
        ramp.tick(self.conn, hit + timedelta(minutes=1))
        self.online(hit + 3 * H)
        ramp.tick(self.conn, hit + 3 * H)
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
        self.assertFalse(self.logged('auto_resume'))

    def test_active_request_waits_and_identity_change_cancels_recovery(self):
        self.turn_on()
        hit = T0 + timedelta(minutes=10)
        self.pause_like_the_server(hit)
        ramp.tick(self.conn, hit + timedelta(minutes=1))
        db.set_setting(self.conn, 'instagram_request_gate', {'active': {'token': 'pending'}})
        self.online(hit + 3 * H)
        ramp.tick(self.conn, hit + 3 * H)
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
        db.set_setting(self.conn, 'instagram_request_gate', None)
        self.conn.execute("UPDATE accounts SET ig_id='new-identity'")
        self.conn.commit()
        self.online(hit + 4 * H)
        ramp.tick(self.conn, hit + 4 * H)
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
        self.assertFalse(self.logged('auto_resume'))

    def test_warning_isolation_does_not_grant_automatic_recovery(self):
        db.set_setting(self.conn, 'instagram_collection_isolation', {'accounts': {LANE: IG}})
        self.conn.commit()
        ramp.record_protective_stop(self.conn, LANE, 'rate_limit', T0)
        self.assertIsNone(db.get_setting(self.conn, 'ramp_protective_stop'))

    def test_auto_resume_never_follows_a_warning_challenge_or_login(self):
        self.turn_on()
        end = self.run_window(T0, 200)
        hit = end + timedelta(minutes=10)
        self.pause_like_the_server(hit, 'challenge')
        self.conn.execute("UPDATE accounts SET hold='challenge'")
        self.conn.commit()
        ramp.tick(self.conn, hit + timedelta(minutes=1))
        self.assertEqual(self.lane()['mode'], 'stopped')
        self.online(hit + 6 * H)
        ramp.tick(self.conn, hit + 6 * H)
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
        self.assertFalse(self.logged('auto_resume'))

    def test_auto_resume_waits_while_any_account_has_a_hold_or_a_warning(self):
        self.turn_on()
        end = self.run_window(T0, 200)
        hit = end + timedelta(minutes=10)
        self.pause_like_the_server(hit)
        ramp.tick(self.conn, hit + timedelta(minutes=1))
        accounts.touch(self.conn, 'lane2', {'ig_id': '9902', 'handle': 'other'}, hold='login')
        self.conn.commit()
        self.online(hit + 3 * H)
        ramp.tick(self.conn, hit + 3 * H)
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
        db.set_setting(self.conn, 'instagram_scraping_warning', {'message': 'x', 'lane': 'lane2'})
        self.conn.execute("UPDATE accounts SET hold=NULL WHERE lane_id='lane2'")
        self.conn.commit()
        ramp.tick(self.conn, hit + 4 * H)
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))

    def test_auto_resume_can_be_turned_off(self):
        self.turn_on(auto_resume=False)
        end = self.run_window(T0, 200)
        hit = end + timedelta(minutes=10)
        self.pause_like_the_server(hit)
        ramp.tick(self.conn, hit + timedelta(minutes=1))
        self.online(hit + 5 * H)
        ramp.tick(self.conn, hit + 5 * H)
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))

    def test_a_person_who_already_restarted_collection_is_not_overridden(self):
        self.turn_on()
        end = self.run_window(T0, 200)
        hit = end + timedelta(minutes=10)
        self.pause_like_the_server(hit)
        ramp.tick(self.conn, hit + timedelta(minutes=1))
        db.set_setting(self.conn, 'paused_lists', False)
        self.conn.commit()
        self.online(hit + 3 * H)
        ramp.tick(self.conn, hit + 3 * H)
        self.assertFalse(self.logged('auto_resume'))
        self.assertIsNone(self.lane()['resume_due'])

    def test_a_redirect_stop_is_restarted_after_the_rest(self):
        self.turn_on()
        end = self.run_window(T0, 200)
        hit = end + timedelta(minutes=10)
        self.event(hit, 'other', 'list_html_home_redirect')
        ramp.record_protective_stop(self.conn, LANE, 'home_redirect', hit)
        db.set_setting(self.conn, 'paused_lists', True)
        db.set_setting(self.conn, 'paused_bios', True)
        self.conn.commit()
        ramp.tick(self.conn, hit + timedelta(minutes=1))
        self.assertTrue(self.logged('redirect_seen'))
        self.online(hit + 2 * H + timedelta(minutes=2))
        ramp.tick(self.conn, hit + 2 * H + timedelta(minutes=2))
        self.assertFalse(db.get_setting(self.conn, 'paused_lists'))
        self.assertEqual(self.logged('auto_resume')[-1]['code'], 'home_redirect')
        # then the first pages after the outage step down one level
        back = hit + 2 * H + timedelta(minutes=3)
        self.feed(back, back + timedelta(minutes=10), 200)
        self.online(back + timedelta(minutes=10))
        ramp.tick(self.conn, back + timedelta(minutes=10))
        self.assertEqual(self.lane()['level'], 0)

    def test_a_third_automatic_restart_in_a_day_needs_a_person(self):
        self.turn_on()
        end = self.run_window(T0, 200)
        t = end
        for i in range(3):
            hit = t + timedelta(minutes=10)
            self.pause_like_the_server(hit, 'soft_block')
            self.conn.execute('DELETE FROM collector_events WHERE outcome="soft_block" AND at<?', (ramp.iso(hit - timedelta(minutes=1)),))
            cfg = ramp.config(self.conn)
            cfg['lanes'][LANE]['rate_events'] = []           # keep the 429 counter out of this test; it is about restarts
            ramp.save(self.conn, cfg)
            ramp.tick(self.conn, hit + timedelta(minutes=1))
            t = hit + 2 * H + timedelta(minutes=5)
            self.online(t)
            ramp.tick(self.conn, t)
        self.assertEqual(len(self.logged('auto_resume')), 2)
        self.assertEqual(self.lane()['mode'], 'stopped')
        self.assertTrue(self.logged('auto_resume_refused'))


class Persistence(RampBase):
    def test_state_survives_a_server_restart(self):
        self.turn_on()
        end = self.run_window(T0, 200)
        before = ramp.config(self.conn)
        fresh = db.connect(db_path(self.conn))                 # a new process opens the same database
        try:
            after = ramp.config(fresh)
            self.assertEqual(after['lanes'][LANE]['level'], 1)
            self.assertEqual(after['lanes'][LANE]['levels'][1]['values'], before['lanes'][LANE]['levels'][1]['values'])
            self.assertTrue(after['enabled'])
            self.assertEqual(ramp.parse(after['lanes'][LANE]['window_start']), end)
            # and the pace itself is still the stepped one
            self.assertEqual(pace.resolve(fresh, fresh.execute('SELECT * FROM accounts').fetchone())['values']['list_gap'], self.gap())
            fresh.execute("UPDATE accounts SET last_seen=?", (ramp.iso(end + 2 * H),))
            fresh.commit()
            self.feed(end, end + 2 * H, 240)
            ramp.tick(fresh, end + 2 * H)
            self.assertEqual(ramp.config(fresh)['lanes'][LANE]['level'], 2)
        finally:
            fresh.close()

    def test_off_stops_stepping_but_leaves_the_pace_and_restore_resets_it(self):
        self.turn_on()
        end = self.run_window(T0, 200)
        gap = self.gap()
        ramp.post(self.conn, {}, {'action': 'off'})
        self.feed(end, end + 2 * H, 240)
        self.online(end + 2 * H)
        ramp.tick(self.conn, end + 2 * H)
        self.assertEqual(self.gap(), gap)
        ramp.post(self.conn, {}, {'action': 'restore', 'account': '@trashtheturtles'})
        self.assertEqual(self.gap(), [7, 12])
        self.assertEqual(ramp.config(self.conn)['lanes'], {})
        self.assertTrue(self.logged('restore'))

    def test_a_person_resumes_a_stopped_ramp_from_the_pace_running_now(self):
        self.turn_on()
        end = self.run_window(T0, 200)
        self.event(end + timedelta(minutes=5), 'challenge')
        ramp.tick(self.conn, end + timedelta(minutes=6))
        self.assertEqual(self.lane()['mode'], 'stopped')
        ramp.post(self.conn, {}, {'action': 'resume', 'account': '@trashtheturtles'})
        self.assertEqual(self.lane()['mode'], 'running')
        self.assertEqual(self.lane()['level'], 0)
        self.assertTrue(self.logged('resume'))


class Logging(RampBase):
    def test_every_decision_is_a_structured_event_with_the_numbers(self):
        self.turn_on()
        self.run_window(T0, 200)
        names = {e['event'] for e in self.log() if e['component'] == 'ramp'}
        self.assertTrue({'ramp_on', 'enrolled', 'step_up'} <= names)
        step = self.logged('step_up')[-1]
        for key in ('reason', 'pages', 'pages_per_hour', 'runtime_share', 'target', 'expected', 'to_id', 'list_gap', 'ramp_level', 'lane', 'account', 'ts', 'level'):
            self.assertIn(key, step, key)
        self.assertEqual(step['account'], HANDLE)
        self.assertEqual(step['pages'], 400)
        self.assertGreater(step['runtime_share'], 0.9)

    def test_status_text_says_what_happened_in_plain_words(self):
        self.turn_on()
        end = self.run_window(T0, 200)
        text = '\n'.join(ramp.status(self.conn, end)['lines'])
        for needle in ('Ramp is ON', 'ceiling of 285', 'trashtheturtles', 'Level 1', 'step up', 'Last window', 'unmeasured'):
            self.assertIn(needle.lower(), text.lower(), needle)

    def test_the_log_line_is_readable_by_ops_diagnose_shape(self):
        self.turn_on()
        line = self.log()[0]
        self.assertRegex(line['ts'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$')
        self.assertEqual(line['component'], 'ramp')

    def test_logging_failure_never_breaks_the_controller(self):
        with mock.patch.object(ramp, '_data_dir', side_effect=OSError('disk')):
            self.turn_on()
            self.run_window(T0, 200)
        self.assertEqual(self.lane()['level'], 1)

    def test_obs_is_used_when_it_is_active(self):
        fake = mock.Mock()
        fake.active.return_value = True
        with mock.patch.dict('sys.modules', {'obs': fake}):
            ramp.emit(self.conn, 'step_up', 'info', reason='x', pages=3)
        fake.event.assert_called_once_with('ramp', 'step_up', 'info', reason='x', pages=3)


class Api(RampBase):
    def test_the_http_api_and_the_worker_hook(self):
        code, out = self.call('/api/ramp', {'action': 'on', 'ceiling': 285, 'interval_s': 7200, 'step_factor': 1.5})
        self.assertEqual(code, 200)
        self.assertTrue(out['enabled'])
        code, out = self.call('/api/ramp')
        self.assertEqual(out['ceiling'], 285)
        self.assertEqual(out['proven_ceiling'], 285)
        code, out = self.call('/api/ramp', {'action': 'explode'})
        self.assertEqual(code, 400)
        import server
        self.assertIs(ramp.step(self.conn), False)

    def test_set_pace_cli_prints_the_readout(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader('set_pace', str(Path(__file__).resolve().parents[2] / 'ops' / 'set-pace'))
        spec = importlib.util.spec_from_loader('set_pace', loader)
        cli = importlib.util.module_from_spec(spec)
        loader.exec_module(cli)
        sent = []

        def request(base, path, body=None):
            sent.append((path, body))
            return ramp.post(self.conn, {}, body) if body is not None else ramp.status(self.conn)
        lines = []
        cli.run(['--ramp', 'on', '--ceiling', '300'], lines.append, request)
        self.assertEqual(sent[0], ('/api/ramp', {'action': 'on', 'ceiling': 300}))
        self.assertIn('above the proven 285', '\n'.join(lines))
        lines.clear()
        cli.run(['--ramp', 'status'], lines.append, request)
        self.assertTrue(lines[0].startswith('Ramp is ON'))


def db_path(conn):
    return conn.execute('PRAGMA database_list').fetchone()[2]
