import os; os.environ.setdefault('FL_NO_ORSLOT', '1')  # tests never see the real key pool
"""The control strip model: three independent stages, per-account pause, Stop everything."""
import sys
import unittest
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

    def test_explicit_start_all_includes_ai_and_paused_accounts(self):
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.2', 'state': 'running'})
        self.ctl(stage='all', action='pause')
        self.ctl(account='lane-a', action='pause')
        out = self.ctl(action='start_all')
        self.assertFalse(any(s['paused'] for s in out['stages']))
        self.assertFalse(out['accounts'][0]['paused'])
        self.assertTrue(db.get_setting(self.conn, 'qualify'))
        self.assertFalse(db.get_setting(self.conn, 'qualify_auto'))
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
        self.assertIn('break', lists['now'])
        self.assertGreater(lists['wait']['seconds'], 150)
        self.assertEqual(self.call('/api/control', {'account': 'nope', 'action': 'pause'})[0], 404)
        self.assertEqual(self.call('/api/control', {'stage': 'x', 'action': 'pause'})[0], 400)
        self.assertEqual(self.call('/api/control', {'stage': 'lists', 'action': 'stop'})[0], 400)
        self.assertEqual(self.call('/api/ext/control')[0], 200)   # the extension reads the same model
        # ...and switches a stage; its POSTs carry an `account` object that must not be taken for an account pause
        out = self.post('a', '/api/ext/control', {'stage': 'bios', 'action': 'pause'})[1]
        self.assertTrue(self.stage(out, 'bios')['paused'])
        self.assertFalse(out['accounts'][0]['paused'])

    def test_list_endpoint_issue_is_account_specific_and_preserves_bios(self):
        self.seeds('s1', 's2')
        self.bio_job()
        first = self.nxt('a')['job']
        self.assertEqual(first['kind'], 'list')
        until = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        self.post('a', '/api/ext/heartbeat', {'version': '3.9.5', 'state': 'running',
                  'cool': {'list': until, 'profile': None}, 'list_endpoint_until': until})
        self.assertIsNone(self.nxt('a', 'list')['job'])
        import control
        row = self.conn.execute("SELECT * FROM accounts WHERE lane_id='lane-a'").fetchone()
        self.assertIn('Instagram returned its home page', control.lane_wait(self.conn, row, 'list', datetime.now(timezone.utc))[0])
        self.assertEqual(self.nxt('a', 'profile')['job']['kind'], 'profile')
        self.assertEqual(self.nxt('b', 'list')['job']['kind'], 'list')
        self.assertTrue(self.conn.execute('SELECT 1 FROM jobs WHERE id=? AND state IN (\'queued\',\'leased\')', (first['id'],)).fetchone())


def load_tests(loader, tests, pattern):
    # only this file's tests; the lane helpers come from LaneTest, its tests run in their own module
    own = [n for n in vars(ControlTest) if n.startswith('test_')]
    return unittest.TestSuite(ControlTest(n) for n in own)


if __name__ == '__main__':
    unittest.main()
