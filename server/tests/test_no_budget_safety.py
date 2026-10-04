"""With no daily request budget (the default), every safety stop still wins."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounts
import db
from backend.common import workspace_cooldown


class NoBudgetSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = db.init(Path(self.temp.name) / 'nb.sqlite')
        self.addCleanup(self.conn.close)
        self.now = datetime.now(timezone.utc)
        accounts.touch(self.conn, 'alt', {'ig_id': '2', 'handle': 'alternate'}, version='3.9.30')
        accounts.touch(self.conn, 'main', {'ig_id': '1', 'handle': 'owner'}, version='3.9.30')
        self.conn.execute("UPDATE accounts SET is_main=1 WHERE lane_id='main'")
        self.conn.execute("INSERT INTO jobs(kind,seed,direction) VALUES('list','seed','following')")
        self.conn.commit()

    def row(self, lane='alt'):
        return self.conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()

    def test_default_budget_is_no_cap_and_never_stops_a_lane(self):
        self.assertEqual(accounts.budget_of(self.conn, self.row()), {'list': 0, 'profile': 0})
        self.conn.execute("UPDATE accounts SET today=?", ('{"list": 999999, "profile": 999999}',))
        self.assertTrue(accounts.request_budget_left(self.conn, self.row(), 'list', self.now))
        self.assertEqual(accounts.kinds_for(self.conn, self.row(), ['list', 'profile'], self.now), ['list', 'profile'])
        self.assertTrue(accounts.request_permit(self.conn, 'alt', kind='list', now=self.now)['granted'])

    def test_protected_main_still_gets_nothing(self):
        self.assertEqual(accounts.kinds_for(self.conn, self.row('main'), ['list', 'profile'], self.now), [])
        self.assertFalse(accounts.request_permit(self.conn, 'main', kind='list', now=self.now)['granted'])

    def test_hold_and_workspace_cooldown_and_warning_still_stop(self):
        db.set_setting(self.conn, 'cooldown', (self.now + timedelta(minutes=15)).isoformat())
        self.assertFalse(accounts.request_permit(self.conn, 'alt', kind='list', now=self.now)['granted'])
        self.assertTrue(workspace_cooldown(self.conn, self.now))
        db.set_setting(self.conn, 'cooldown', None)
        self.conn.execute("UPDATE accounts SET list_cool_until=? WHERE lane_id='alt'", ((self.now + timedelta(hours=2)).isoformat(),))
        self.assertEqual(accounts.kinds_for(self.conn, self.row(), ['list'], self.now), [])
        self.conn.execute("UPDATE accounts SET list_cool_until=NULL")
        db.set_setting(self.conn, 'instagram_scraping_warning', {'lane': 'alt', 'ig_id': '2', 'at': accounts.iso(self.now)})
        db.set_setting(self.conn, 'paused_lists', True)
        self.assertFalse(accounts.request_permit(self.conn, 'alt', kind='list', now=self.now)['granted'])
        self.assertTrue(accounts.paused_for(self.conn, self.row()))


if __name__ == '__main__':
    unittest.main()
