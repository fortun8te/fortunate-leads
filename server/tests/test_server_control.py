import os; os.environ.setdefault('FL_NO_ORSLOT', '1')  # tests never see the real key pool
"""The control strip model: three independent stages, per-account pause, Stop everything."""
import sys
import unittest
from unittest.mock import patch
import control
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server_accounts import LaneTest  # noqa: E402

import db  # noqa: E402


class ControlTest(LaneTest):
    def ctl(self, **b):
        code, out = self.call('/api/control', b) if b else self.call('/api/control')
        self.assertEqual(code, 200, out)
        return out

    def stage(self, out, sid):
        return next(s for s in out['stages'] if s['id'] == sid)

    def bio_job(self):
        pid = db.upsert_person(self.conn, {'ig_id': '7', 'handle': 'dave'})
        self.conn.commit()
        self.call(f'/api/person/{pid}/read', {})

    def test_collection_toggle_preserves_models_account_controls_and_attention(self):
        self.nxt('a')
        self.conn.execute("UPDATE accounts SET paused=1, hold='challenge' WHERE lane_id='lane-a'")
        attention = {'lane': 'lane-a', 'message': 'Check the account tab before resuming.'}
        db.set_setting(self.conn, 'instagram_request_attention', attention)
        self.conn.commit()
        for local, external, auto in ((True, False, False), (False, True, True)):
            for key, value in (('local_laya', local), ('qualify', external), ('qualify_auto', auto)):
                db.set_setting(self.conn, key, value)
            self.conn.commit()
            for action, paused in (('pause', True), ('resume', False)):
                out = self.ctl(stage='collection', action=action)
                self.assertEqual([s['paused'] for s in out['stages'][:2]], [paused, paused])
                self.assertEqual([db.get_setting(self.conn, key) for key in ('local_laya', 'qualify', 'qualify_auto')], [local, external, auto])
                row = self.conn.execute("SELECT paused,hold FROM accounts WHERE lane_id='lane-a'").fetchone()
                self.assertEqual(tuple(row), (1, 'challenge'))
                self.assertEqual(out['instagram_request_attention'], attention)

    def test_collection_toggle_rolls_back_both_stages_on_storage_failure(self):
        original = db.set_setting
        def fail_second(conn, key, value):
            if key == 'paused_bios':
                raise RuntimeError('write failed')
            return original(conn, key, value)
        with patch.object(db, 'set_setting', side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, 'write failed'):
                control.apply(self.conn, {'stage': 'collection', 'action': 'pause'})
        self.assertFalse(db.get_setting(self.conn, 'paused_lists'))
        self.assertFalse(db.get_setting(self.conn, 'paused_bios'))

    def test_collection_resume_cannot_clear_shared_warning(self):
        self.ctl(stage='collection', action='pause')
        db.set_setting(self.conn, 'cooldown', '2099-01-01T00:00:00Z')
        self.conn.commit()
        code, _ = self.call('/api/control', {'stage': 'collection', 'action': 'resume'})
        self.assertEqual(code, 400)
        self.assertEqual(db.get_setting(self.conn, 'cooldown'), '2099-01-01T00:00:00Z')
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
        self.assertTrue(db.get_setting(self.conn, 'paused_bios'))

    def test_shared_hold_explains_waiting_lists_bios_and_accounts(self):
        self.seeds('one')
        self.nxt('a', 'list')  # A stale-looking active lease must not hide the shared hold.
        self.bio_job()
        until = datetime.now(timezone.utc) + timedelta(minutes=30)
        db.set_setting(self.conn, 'cooldown', until.isoformat())
        db.set_setting(self.conn, 'local_laya', True)
        self.conn.commit()
        out = self.ctl()
        for stage in out['stages'][:2]:
            self.assertEqual(stage['state'], 'waiting')
            self.assertFalse(stage['paused'])
            self.assertEqual(stage['wait']['until'], until.isoformat())
            self.assertEqual(stage['wait']['scope'], 'workspace')
            self.assertGreater(stage['wait']['seconds'], 1700)
            self.assertIn('Resumes automatically', stage['now'])
        account = out['accounts'][0]
        self.assertEqual(account['state'], 'waiting')
        self.assertEqual(account['wait']['until'], until.isoformat())
        self.assertTrue(out['local_laya'])
        self.assertIsNone(self.stage(out, 'ai')['wait'])
        self.assertEqual(self.stage(out, 'ai')['state'], 'paused')
        db.set_setting(self.conn, 'cooldown', '2000-01-01T00:00:00Z')
        self.conn.commit()
        self.assertNotIn('collection is resting', self.stage(self.ctl(), 'lists')['now'])

    def test_shape(self):
        out = self.ctl()
        self.assertEqual([s['id'] for s in out['stages']], ['lists', 'bios', 'ai'])
        for s in out['stages']:
            for k in ('label', 'help', 'state', 'now', 'hour', 'today', 'paused', 'wait'):
                self.assertIn(k, s)
        self.assertEqual(self.stage(out, 'ai')['state'], 'paused')   # qualify defaults off
        self.assertFalse(out['all_paused'])

    def test_ai_rates_count_saved_scores_not_profile_revisions(self):
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(seconds=20)).isoformat()
        hour_old = (now - timedelta(minutes=5)).isoformat()
        day_old = (now - timedelta(hours=2)).isoformat()
        pid = db.upsert_person(self.conn, {'handle': 'scored', 'bio': 'founder'})
        self.conn.execute("INSERT INTO verdicts(person_id,model,updated_at) VALUES(?,'llm','2020-01-01')", (pid,))
        revision = db.get_setting(self.conn, 'lead_data_rev')
        self.conn.executemany('INSERT INTO ai_scoring_events(person_id,scored_at) VALUES(?,?)',
                              [(pid, recent), (pid, hour_old), (pid, day_old)])
        self.assertEqual(db.get_setting(self.conn, 'lead_data_rev'), revision)
        plan = self.conn.execute('EXPLAIN QUERY PLAN SELECT count(*) FROM ai_scoring_events WHERE scored_at>=?',
                                 (recent,)).fetchall()
        self.assertTrue(any('ai_scoring_events_at' in row['detail'] for row in plan))
        self.conn.commit()
        ai = self.stage(self.ctl(), 'ai')
        self.assertEqual((ai['minute'], ai['hour'], ai['today']),
                         (1, 2, 2 + int(day_old[:10] == now.date().isoformat())))
        self.conn.execute("UPDATE verdicts SET updated_at=? WHERE person_id=?", (now.isoformat(), pid))
        self.conn.commit()
        ai = self.stage(self.ctl(), 'ai')
        self.assertEqual((ai['minute'], ai['hour']), (1, 2))

    def test_lists_and_bios_pause_separately(self):
        self.bio_job()
        self.seeds('s1')
        self.ctl(stage='lists', action='pause')
        self.assertEqual(self.nxt('a')['job']['kind'], 'profile')   # lists paused, bios still handed out
        self.assertTrue(self.stage(self.ctl(), 'lists')['paused'])
        self.ctl(stage='lists', action='resume')
        self.ctl(stage='bios', action='pause')
        nxt = self.nxt('b')
        self.assertEqual(nxt['job']['kind'], 'list')
        self.assertEqual(nxt['stages'], {'list': True, 'profile': False})
        self.ctl(stage='lists', action='pause')
        self.assertIsNone(self.nxt('c')['job'])

    def test_legacy_pause_resumes_one_stage(self):
        self.call('/api/scraper/pause', {'paused': True})
        out = self.ctl()
        self.assertTrue(self.stage(out, 'lists')['paused'] and self.stage(out, 'bios')['paused'])
        out = self.ctl(stage='lists', action='resume')
        self.assertFalse(self.stage(out, 'lists')['paused'])
        self.assertTrue(self.stage(out, 'bios')['paused'])
        self.assertFalse(db.get_setting(self.conn, 'paused'))

    def test_stop_all_and_ai(self):
        self.ctl(stage='ai', action='resume')
        self.assertTrue(db.get_setting(self.conn, 'qualify'))
        out = self.ctl(stage='all', action='pause')
        self.assertTrue(out['all_paused'])
        self.assertFalse(db.get_setting(self.conn, 'qualify'))
        self.assertFalse(db.get_setting(self.conn, 'qualify_auto'))   # would otherwise switch itself back on
        out = self.ctl(stage='all', action='resume')
        # Resume all restarts collection; AI model calls keep their own explicit switch.
        self.assertFalse(any(s['paused'] for s in out['stages'] if s['id'] != 'ai'))
        self.assertFalse(db.get_setting(self.conn, 'qualify'))

    def test_explicit_start_all_preserves_ai_choice_and_resumes_accounts(self):
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.2', 'state': 'running'})
        self.ctl(stage='all', action='pause')
        self.ctl(account='lane-a', action='pause')
        out = self.ctl(action='start_all')
        self.assertFalse(any(s['paused'] for s in out['stages'] if s['id'] != 'ai'))
        self.assertTrue(self.stage(out, 'ai')['paused'])
        self.assertFalse(out['accounts'][0]['paused'])
        self.assertFalse(db.get_setting(self.conn, 'qualify'))
        self.assertFalse(db.get_setting(self.conn, 'qualify_auto'))
        self.ctl(stage='ai', action='resume')
        self.ctl(stage='lists', action='pause')
        out = self.ctl(action='start_all')
        self.assertFalse(self.stage(out, 'ai')['paused'])
        self.assertTrue(db.get_setting(self.conn, 'qualify'))
        self.assertEqual(self.call('/api/control', {'stage': 'ai', 'action': 'start_all'})[0], 400)

    def test_account_pause_and_waits(self):
        self.post('a', '/api/ext/heartbeat', {'version': '3.7.0', 'state': 'running'})
        self.seeds('s1')
        out = self.ctl(account='lane-a', action='pause')
        self.assertEqual(out['accounts'][0]['state'], 'paused')
        self.assertIsNone(self.nxt('a')['job'])
        self.ctl(account='lane-a', action='resume')
        soon = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
        self.post('a', '/api/ext/heartbeat', {'version': '3.7.0', 'state': 'running', 'ready': {'list': soon}})
        lists = self.stage(self.ctl(), 'lists')
        self.assertEqual(lists['state'], 'waiting')
        self.assertIn('Waiting between requests', lists['now'])
        self.assertGreater(lists['wait']['seconds'], 150)
        self.assertEqual(self.call('/api/control', {'account': 'nope', 'action': 'pause'})[0], 404)
        self.assertEqual(self.call('/api/control', {'stage': 'x', 'action': 'pause'})[0], 400)
        self.assertEqual(self.call('/api/control', {'stage': 'lists', 'action': 'stop'})[0], 400)
        self.assertEqual(self.call('/api/ext/control')[0], 200)   # the extension reads the same model
        # ...and switches a stage; its POSTs carry an `account` object that must not be taken for an account pause
        out = self.post('a', '/api/ext/control', {'stage': 'bios', 'action': 'pause'})[1]
        self.assertTrue(self.stage(out, 'bios')['paused'])
        self.assertFalse(out['accounts'][0]['paused'])

    def test_follower_redirect_pause_keeps_following_and_bios_available(self):
        self.seeds('s1', 's2')
        self.bio_job()
        first = self.nxt('a')['job']
        self.assertEqual(first['kind'], 'list')
        profile = self.nxt('a', 'profile')['job']
        self.assertEqual(profile['kind'], 'profile')
        self.seeds('other', direction='following')
        until = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.5', 'state': 'running',
                  'cool': {'list': None, 'profile': None}, 'list_endpoint_until': until})
        self.assertEqual(self.nxt('a', 'list')['job']['direction'], 'following')
        import control
        row = self.conn.execute("SELECT * FROM accounts WHERE lane_id='lane-a'").fetchone()
        self.assertIsNone(control.lane_wait(self.conn, row, 'list', datetime.now(timezone.utc)))
        kept = self.conn.execute('SELECT state,lane FROM jobs WHERE id=?', (profile['id'],)).fetchone()
        self.assertEqual((kept['state'], kept['lane']), ('leased', 'lane-a'))
        self.assertEqual(self.nxt('b', 'list')['job']['kind'], 'list')
        real_limit = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.post('a', '/api/ext/heartbeat', {'cool': {'list': real_limit, 'profile': None},
                  'list_endpoint_until': until})
        row = self.conn.execute("SELECT * FROM accounts WHERE lane_id='lane-a'").fetchone()
        why, seconds = control.lane_wait(self.conn, row, 'list', datetime.now(timezone.utc))
        self.assertIn('slow down', why)
        self.assertGreater(seconds, 110 * 60)
        kept = self.conn.execute('SELECT state,lane FROM jobs WHERE id=?', (profile['id'],)).fetchone()
        self.assertEqual((kept['state'], kept['lane']), ('leased', 'lane-a'))
        self.assertTrue(self.conn.execute('SELECT 1 FROM jobs WHERE id=? AND state IN (\'queued\',\'leased\')', (first['id'],)).fetchone())

    def test_follower_pause_does_not_mark_all_lists_waiting(self):
        self.seeds('s1')
        job = self.nxt('a')['job']
        until = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.5', 'state': 'running',
                  'list_endpoint_until': until})
        # An old lease can remain visible briefly while the extension reports its circuit wait.
        self.conn.execute("UPDATE jobs SET state='leased', lane='lane-a', leased_until=? WHERE id=?",
                          (until, job['id']))
        self.conn.commit()
        out = self.ctl()
        for item in (self.stage(out, 'lists'), out['accounts'][0]):
            self.assertNotEqual(item['state'], 'waiting')

    def test_old_profile_lease_does_not_mask_rate_limit_wait(self):
        self.bio_job()
        job = self.nxt('a', 'profile')['job']
        until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.5', 'state': 'cooldown',
                  'cool': {'list': None, 'profile': until}})
        self.conn.execute("UPDATE jobs SET state='leased', lane='lane-a', leased_until=? WHERE id=?",
                          (until, job['id']))
        self.conn.commit()
        out = self.ctl()
        for item in (self.stage(out, 'bios'), out['accounts'][0]):
            self.assertEqual(item['state'], 'waiting')
            self.assertIn('slow down', item['now'])
            self.assertGreater(item['wait']['seconds'], 110 * 60)


def load_tests(loader, tests, pattern):
    # only this file's tests; the lane helpers come from LaneTest, its tests run in their own module
    own = [n for n in vars(ControlTest) if n.startswith('test_')]
    return unittest.TestSuite(ControlTest(n) for n in own)


if __name__ == '__main__':
    unittest.main()
