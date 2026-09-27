import os; os.environ.setdefault('FL_NO_ORSLOT', '1')
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import biofetch  # noqa: E402
import db  # noqa: E402

# Shape from Meta's docs (IG User > business_discovery).
OK = {'business_discovery': {'username': 'acme', 'name': 'Acme', 'biography': 'We make things', 'website': 'https://acme.co',
                             'followers_count': 1200, 'follows_count': 80, 'media_count': 40, 'profile_picture_url': 'https://example.com/pfp.jpg', 'id': '1784'}, 'id': '999'}
NOT_BIZ = {'error': {'code': 110, 'error_subcode': 2207013, 'message': 'Cannot find User'}}
LIMIT = {'error': {'code': 4, 'message': 'Application request limit reached'}}


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'l.sqlite'))
        for h in ('acme', 'bob'):
            db.upsert_person(self.conn, {'handle': h})
        self.conn.commit()
        self.calls = []
        self.replies = []
        biofetch.FETCH[0] = lambda url: (self.calls.append(url), self.replies.pop(0))[1]
        self.t = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)

    def tearDown(self):
        biofetch.FETCH[0] = biofetch._get
        self.conn.close()
        self.tmp.cleanup()

    def row(self, h):
        return self.conn.execute('SELECT * FROM people WHERE handle=?', (h,)).fetchone()

    def test_shared_warning_stops_graph_calls_and_expiry_allows_pending_work(self):
        biofetch.save(self.conn, {'on': True, 'token': 'tok', 'ig_user_id': '999'})
        for value in ('2099-01-01T00:00:00Z', 'not-a-date', 42, False):
            db.set_setting(self.conn, 'cooldown', value)
            self.assertFalse(biofetch.step(self.conn, self.t))
            self.assertEqual(self.calls, [])
        db.set_setting(self.conn, 'cooldown', '2000-01-01T00:00:00Z')
        self.replies = [(200, OK, {})]
        self.assertTrue(biofetch.step(self.conn, self.t))
        self.assertEqual(len(self.calls), 1)

    def test_off_by_default(self):
        self.assertFalse(biofetch.step(self.conn, self.t))
        self.assertEqual(self.calls, [])
        self.assertFalse(biofetch.public(self.conn)['on'])

    def test_paused_stage_or_workspace_makes_no_request(self):
        biofetch.save(self.conn, {'on': True, 'token': 'tok', 'ig_user_id': '999'})
        for setting in ('paused', 'paused_bios'):
            db.set_setting(self.conn, setting, True)
            self.assertFalse(biofetch.step(self.conn, self.t))
            db.set_setting(self.conn, setting, False)
        self.assertEqual(self.calls, [])

    def test_hit_miss_gap_and_limit(self):
        biofetch.save(self.conn, {'on': True, 'token': 'tok', 'ig_user_id': '999', 'gap': 2})
        self.replies = [(200, OK, {}), (400, NOT_BIZ, {})]
        self.conn.execute("UPDATE people SET handle='zz' WHERE handle='bob'")  # order: acme first
        self.assertTrue(biofetch.step(self.conn, self.t))
        r = self.row('acme')
        self.assertEqual((r['bio'], r['website'], r['followers'], r['bio_src']), ('We make things', 'https://acme.co', 1200, 'meta_bd'))
        self.assertEqual(r['pic_url'], 'https://example.com/pfp.jpg')
        self.assertIn('business_discovery.username%28acme%29', self.calls[0])
        self.assertFalse(biofetch.step(self.conn, self.t + timedelta(seconds=1)))  # gap
        self.assertTrue(biofetch.step(self.conn, self.t + timedelta(seconds=3)))
        z = self.row('zz')
        self.assertIsNone(z['bio_at'])
        self.assertIsNotNone(z['bd_at'])  # tried, left for the extension
        self.assertFalse(biofetch.step(self.conn, self.t + timedelta(seconds=6)))  # nobody left
        self.assertEqual(biofetch.state(self.conn)['hits'], 1)

    def test_throttle_stops(self):
        biofetch.save(self.conn, {'on': True, 'token': 'tok', 'ig_user_id': '999'})
        self.replies = [(400, LIMIT, {})]
        biofetch.step(self.conn, self.t)
        self.assertFalse(biofetch.step(self.conn, self.t + timedelta(minutes=5)))
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.row('acme')['bd_at'])  # retried after the pause

    def test_usage_header_slows(self):
        biofetch.save(self.conn, {'on': True, 'token': 'tok', 'ig_user_id': '999'})
        self.replies = [(200, OK, {'x-business-use-case-usage': '{"999":[{"call_count":80,"total_time":5}]}'})]
        biofetch.step(self.conn, self.t)
        self.assertIn('until', biofetch.state(self.conn))

    def test_bad_token_switches_off(self):
        biofetch.save(self.conn, {'on': True, 'token': 'tok', 'ig_user_id': '999'})
        self.replies = [(400, {'error': {'code': 190}}, {})]
        biofetch.step(self.conn, self.t)
        self.assertFalse(biofetch.settings(self.conn)['on'])

    def test_validation(self):
        with self.assertRaises(ValueError):
            biofetch.save(self.conn, {'gap': 0.1})
        self.assertNotIn('tok', biofetch.save(self.conn, {'token': 'secrettok1'})['token'][:-4])


if __name__ == '__main__':
    unittest.main()
