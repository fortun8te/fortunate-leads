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


def load_tests(loader, tests, pattern):
    # only this file's tests; the lane helpers come from LaneTest, its tests run in their own module
    own = [n for n in vars(ControlTest) if n.startswith('test_')]
    return unittest.TestSuite(ControlTest(n) for n in own)


if __name__ == '__main__':
    unittest.main()
