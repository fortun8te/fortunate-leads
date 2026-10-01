"""Main-account protection covers old leases and duplicate browser identities."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounts
import db


class MainCollectionProtection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = db.init(Path(self.temp.name) / 'main.sqlite')
        self.addCleanup(self.conn.close)
        self.now = datetime.now(timezone.utc)
        accounts.touch(self.conn, 'main', {'ig_id': '1', 'handle': 'owner'}, version='3.9.30')
        self.conn.execute("UPDATE accounts SET is_main=1 WHERE lane_id='main'")
        self.conn.execute("INSERT INTO jobs(kind,seed,direction) VALUES('list','seed','following')")
        self.conn.execute("INSERT INTO jobs(kind,handle) VALUES('profile','person')")
        self.conn.commit()

    def row(self, lane='main'):
        return self.conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()

    def test_main_alone_gets_no_automated_jobs_even_with_legacy_share(self):
        for share in (0, .2, 1):
            db.set_setting(self.conn, 'main_list_share', share)
            self.assertEqual(accounts.kinds_for(self.conn, self.row(), ['list','profile'], self.now), [])
            self.assertIsNone(accounts.pick_job(self.conn, 'main', ['list','profile'], self.now))
            self.assertEqual(db.get_setting(self.conn, 'main_list_share'), share)

    def test_stale_main_lease_cannot_acquire_a_collection_permit(self):
        self.conn.execute("UPDATE jobs SET state='leased',lane='main',leased_until=?", ((self.now + timedelta(minutes=10)).isoformat(),))
        for kind in ('list', 'profile'):
            self.assertFalse(accounts.request_permit(self.conn, 'main', kind=kind, now=self.now)['granted'])
        self.assertFalse((db.get_setting(self.conn, 'instagram_request_gate') or {}).get('active'))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)

    def test_same_main_identity_in_other_chrome_profile_is_protected(self):
        accounts.touch(self.conn, 'duplicate', {'ig_id': '1', 'handle': 'owner'}, version='3.9.30')
        self.conn.execute("UPDATE accounts SET paused=1 WHERE lane_id='main'")
        self.assertEqual(accounts.kinds_for(self.conn, self.row('duplicate'), ['list','profile'], self.now), [])
        for kind in ('list','profile'):
            self.assertFalse(accounts.request_permit(self.conn, 'duplicate', kind=kind, now=self.now)['granted'])

    def test_existing_request_can_finish_without_granting_another(self):
        self.conn.execute("UPDATE accounts SET is_main=0 WHERE lane_id='main'")
        permit = accounts.request_permit(self.conn, 'main', kind='list', now=self.now)
        self.assertTrue(permit['granted'])
        self.conn.execute("UPDATE accounts SET is_main=1 WHERE lane_id='main'")
        self.assertTrue(accounts.request_permit(self.conn, 'main', token=permit['token'], now=self.now)['released'])
        self.assertFalse(accounts.request_permit(self.conn, 'main', kind='list', now=self.now + timedelta(seconds=5))['granted'])

    def test_main_protection_does_not_change_pause_state_or_block_distinct_alt(self):
        accounts.touch(self.conn, 'alt', {'ig_id':'2','handle':'alternate'}, version='3.9.30')
        self.assertTrue(accounts.request_permit(self.conn, 'alt', kind='list', now=self.now)['granted'])
        self.assertEqual(self.row()['paused'], 0)
        self.assertEqual(self.row()['hold'], None)
        self.assertFalse(db.get_setting(self.conn, 'paused'))

    def test_owner_identity_stays_protected_after_lane_and_handle_change(self):
        self.conn.execute("INSERT INTO seeds(handle,is_me) VALUES('owner',1)")
        db.upsert_person(self.conn, {'ig_id':'1','handle':'owner'})
        self.conn.execute("DELETE FROM accounts WHERE lane_id='main'")
        accounts.touch(self.conn, 'renamed-lane', {'ig_id':'1','handle':'changed-name'}, version='3.9.30')
        self.assertTrue(accounts.collection_protected(self.conn, self.row('renamed-lane')))
        self.assertFalse(accounts.request_permit(self.conn, 'renamed-lane', kind='list', now=self.now)['granted'])

    def test_exported_status_explains_personal_use(self):
        import control
        account = control.snapshot(self.conn)['accounts'][0]
        self.assertTrue(account['collection_protected'])
        self.assertEqual(account['reason_code'], 'main_reserved')
        self.assertIn('Personal use', account['now'])
        self.assertTrue(accounts.out(self.conn, self.row(), self.now)['collection_protected'])
