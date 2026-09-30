"""Workspace request coordination against temp SQLite, without Instagram traffic."""
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounts
import control
import db


class RequestPermitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'gate.sqlite')
        self.conn = db.init(self.path)
        self.addCleanup(self.conn.close)
        self.now = datetime.now(timezone.utc)
        for i in range(25):
            accounts.touch(self.conn, f'lane{i}', {'ig_id': str(100+i), 'handle': f'acct{i}'}, version='3.9.17')
        self.conn.commit()

    def acquire(self, i, seconds=0, kind='list'):
        now = self.now + timedelta(seconds=seconds)
        self.conn.execute('UPDATE accounts SET last_seen=? WHERE lane_id=?', (accounts.iso(now), f'lane{i}'))
        self.conn.commit()
        return accounts.request_permit(self.conn, f'lane{i}', kind=kind, now=now)

    def test_scraping_warning_blocks_all_lanes_even_if_pause_settings_are_cleared(self):
        db.set_setting(self.conn, 'instagram_scraping_warning', {'lane': 'lane0', 'message': 'Review Instagram warning'})
        for setting in ('paused', 'paused_lists', 'paused_bios'):
            db.set_setting(self.conn, setting, False)
        self.conn.commit()
        for lane in (0, 1, 24):
            self.assertFalse(self.acquire(lane)['granted'])
        with self.assertRaises(ValueError):
            control.apply(self.conn, {'action': 'start_all'})
        self.assertTrue(control.stage_paused(self.conn, 'lists'))

    def test_concurrent_requests_grant_only_one_persisted_permit(self):
        barrier = threading.Barrier(10)
        def work(i):
            conn = db.connect(self.path)
            try:
                barrier.wait()
                return accounts.request_permit(conn, f'lane{i}', kind='list', now=self.now)
            finally:
                conn.close()
        with ThreadPoolExecutor(max_workers=10) as workers:
            results = list(workers.map(work, range(10)))
        self.assertEqual(sum(r['granted'] for r in results), 1)
        self.assertEqual(len(db.get_setting(self.conn, 'instagram_request_gate')['queue']), 9)

    def test_fifo_prevents_same_lane_from_jumpstarting_again(self):
        first = self.acquire(0)
        for i in range(1, 25):
            self.assertFalse(self.acquire(i)['granted'])
        self.assertFalse(accounts.request_permit(self.conn, 'lane1', token=first['token'], now=self.now)['released'])
        token, owner = first['token'], 0
        for i in range(1, 25):
            accounts.request_permit(self.conn, f'lane{owner}', token=token, now=self.now+timedelta(seconds=(i-1)*3))
            self.assertFalse(self.acquire(owner, (i-1)*3+1)['granted'])
            permit = self.acquire(i, i*3)
            self.assertTrue(permit['granted'], i)
            token, owner = permit['token'], i

    def test_expired_unconfirmed_request_pauses_every_lane_until_manual_resume(self):
        db.set_setting(self.conn, 'local_laya', True)
        self.conn.commit()
        first = self.acquire(0)
        self.assertFalse(self.acquire(1, 89)['granted'])
        for seconds in (91, 92, 180, 600):
            self.assertFalse(self.acquire(1, seconds)['granted'])
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
        self.assertTrue(db.get_setting(self.conn, 'paused_bios'))
        self.assertTrue(db.get_setting(self.conn, 'local_laya'))
        self.assertIn('Check the account tab', db.get_setting(self.conn, 'instagram_request_attention')['message'])
        snapshot = control.snapshot(self.conn)
        self.assertEqual(snapshot['instagram_request_attention']['lane'], 'lane0')
        for stage in snapshot['stages'][:2]:
            self.assertTrue(stage['paused'])
            self.assertIn('Check the account tab', stage['now'])
        self.assertNotIn('Check the account tab', snapshot['stages'][2]['now'])
        self.assertFalse(accounts.request_permit(self.conn, 'lane0', token=first['token'], now=self.now+timedelta(seconds=601))['released'])
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))  # late callback cannot resume
        db.set_setting(self.conn, 'paused_lists', False)
        db.set_setting(self.conn, 'paused_bios', False)
        self.conn.commit()
        self.assertIsNotNone(control.snapshot(self.conn)['instagram_request_attention'])
        next_ = self.acquire(1, 602)
        self.assertIsNone(control.snapshot(self.conn)['instagram_request_attention'])
        self.assertTrue(next_['granted'])
        self.assertFalse(accounts.request_permit(self.conn, 'lane0', token=first['token'], now=self.now+timedelta(seconds=603))['released'])
        self.assertEqual(db.get_setting(self.conn, 'instagram_request_gate')['active']['token'], next_['token'])

    def test_concurrent_expiry_waiters_cannot_start_over_unconfirmed_request(self):
        self.acquire(0)
        later = self.now + timedelta(seconds=91)
        self.conn.execute('UPDATE accounts SET last_seen=?', (accounts.iso(later),))
        self.conn.commit()
        barrier = threading.Barrier(10)
        def work(i):
            conn = db.connect(self.path)
            try:
                barrier.wait()
                return accounts.request_permit(conn, f'lane{i}', kind='list', now=later)
            finally:
                conn.close()
        with ThreadPoolExecutor(max_workers=10) as workers:
            results = list(workers.map(work, range(1, 11)))
        self.assertFalse(any(result['granted'] for result in results))
        self.assertTrue(all(result['wait_ms'] == 15000 for result in results))
        self.assertTrue(db.get_setting(self.conn, 'paused_lists'))
        self.assertTrue(db.get_setting(self.conn, 'paused_bios'))

    def test_confirmed_late_completion_before_another_acquire_needs_no_manual_pause(self):
        first = self.acquire(0)
        self.assertTrue(accounts.request_permit(self.conn, 'lane0', token=first['token'], now=self.now+timedelta(seconds=91))['released'])
        self.assertFalse(db.get_setting(self.conn, 'paused_lists'))
        self.assertFalse(self.acquire(1, 92)['granted'])
        self.assertTrue(self.acquire(1, 93)['granted'])

    def test_shared_warning_or_paused_stage_cannot_be_bypassed_by_direct_acquire(self):
        db.set_setting(self.conn, 'paused_lists', True)
        self.conn.commit()
        self.assertFalse(self.acquire(0)['granted'])
        db.set_setting(self.conn, 'cooldown', accounts.iso(self.now+timedelta(minutes=15)))
        self.conn.commit()
        self.assertFalse(self.acquire(1, kind='profile')['granted'])
        db.set_setting(self.conn, 'cooldown', 'invalid')
        self.conn.commit()
        self.assertFalse(self.acquire(1, kind='profile')['granted'])

    def test_paused_kind_or_account_is_removed_from_wait_queue(self):
        first = self.acquire(0)
        self.acquire(1, kind='list')
        self.acquire(2, kind='profile')
        db.set_setting(self.conn, 'paused_lists', True)
        self.conn.commit()
        accounts.request_permit(self.conn, 'lane0', token=first['token'], now=self.now)
        self.assertTrue(self.acquire(2, 2, 'profile')['granted'])

    def test_cached_job_cannot_acquire_after_account_budget_role_or_cooldown_changes(self):
        for restriction in ('budget', 'role', 'cooldown', 'identity'):
            with self.subTest(restriction=restriction):
                self.conn.execute("UPDATE accounts SET role='both', budget=NULL, today=NULL, list_cool_until=NULL, ig_id='100' WHERE lane_id='lane0'")
                if restriction == 'budget':
                    self.conn.execute('UPDATE accounts SET budget=?, today=? WHERE lane_id=?',
                                      ('{"list":1}', '{"list":1}', 'lane0'))
                elif restriction == 'role':
                    self.conn.execute("UPDATE accounts SET role='bios' WHERE lane_id='lane0'")
                elif restriction == 'cooldown':
                    self.conn.execute('UPDATE accounts SET list_cool_until=? WHERE lane_id=?',
                                      (accounts.iso(self.now + timedelta(minutes=10)), 'lane0'))
                else:
                    self.conn.execute("UPDATE accounts SET ig_id='101', first_seen='2099-01-01' WHERE lane_id='lane0'")
                self.conn.commit()
                self.assertFalse(self.acquire(0)['granted'])
                self.assertIsNone(db.get_setting(self.conn, 'instagram_request_gate')['active'])

    def test_newly_exhausted_front_waiter_does_not_block_next_healthy_account(self):
        first = self.acquire(0)
        self.assertFalse(self.acquire(1)['granted'])
        self.assertFalse(self.acquire(2)['granted'])
        self.conn.execute('UPDATE accounts SET budget=?, today=? WHERE lane_id=?',
                          ('{"list":1}', '{"list":1}', 'lane1'))
        self.conn.commit()
        accounts.request_permit(self.conn, 'lane0', token=first['token'], now=self.now)
        self.assertTrue(self.acquire(2, 2)['granted'])
        self.assertNotIn('lane1', [entry['lane'] for entry in db.get_setting(self.conn, 'instagram_request_gate')['queue']])

    def test_account_cooldown_does_not_prevent_releasing_inflight_request(self):
        first = self.acquire(0)
        self.conn.execute('UPDATE accounts SET list_cool_until=? WHERE lane_id=?',
                          (accounts.iso(self.now + timedelta(minutes=10)), 'lane0'))
        self.conn.commit()
        self.assertTrue(accounts.request_permit(self.conn, 'lane0', token=first['token'], now=self.now)['released'])
        self.assertTrue(self.acquire(1, 2)['granted'])
