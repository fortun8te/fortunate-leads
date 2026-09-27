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
            accounts.touch(self.conn, f'lane{i}', {'ig_id': str(100+i), 'handle': f'acct{i}'}, version='3.9.15')
        self.conn.commit()

    def acquire(self, i, seconds=0, kind='list'):
        now = self.now + timedelta(seconds=seconds)
        self.conn.execute('UPDATE accounts SET last_seen=? WHERE lane_id=?', (accounts.iso(now), f'lane{i}'))
        self.conn.commit()
        return accounts.request_permit(self.conn, f'lane{i}', kind=kind, now=now)

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

    def test_crashed_holder_expires_and_old_release_cannot_clear_new_owner(self):
        first = self.acquire(0)
        self.assertFalse(self.acquire(1, 89)['granted'])
        next_ = self.acquire(1, 91)
        self.assertTrue(next_['granted'])
        self.assertFalse(accounts.request_permit(self.conn, 'lane0', token=first['token'], now=self.now+timedelta(seconds=92))['released'])
        self.assertEqual(db.get_setting(self.conn, 'instagram_request_gate')['active']['token'], next_['token'])

    def test_paused_kind_or_account_is_removed_from_wait_queue(self):
        first = self.acquire(0)
        self.acquire(1, kind='list')
        self.acquire(2, kind='profile')
        db.set_setting(self.conn, 'paused_lists', True)
        self.conn.commit()
        accounts.request_permit(self.conn, 'lane0', token=first['token'], now=self.now)
        self.assertTrue(self.acquire(2, 2, 'profile')['granted'])
