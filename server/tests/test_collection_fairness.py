"""Saved-page quanta keep both queues moving without restarting any list."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounts
import db


class CollectionFairnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.init(str(Path(self.tmp.name) / 'fair.sqlite'))
        self.addCleanup(self.conn.close)
        self.now = datetime.now(timezone.utc)
        accounts.touch(self.conn, 'lane', {'ig_id': '101', 'handle': 'alt'}, version='3.9.17')
        self.large = self.add_list('large', 200)
        self.small = self.add_list('small', 100)
        self.conn.execute("INSERT INTO jobs(kind,handle,priority,state,created_at) VALUES('profile','bio',1,'queued',?)",
                          (accounts.iso(self.now),))
        self.bio = self.conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        self.tick = 0

    def add_list(self, seed, priority):
        self.conn.execute('INSERT INTO seeds(handle) VALUES(?)', (seed,))
        self.conn.execute("INSERT INTO lists(seed,direction,state) VALUES(?,'following','queued')", (seed,))
        self.conn.execute("INSERT INTO jobs(kind,seed,direction,priority,state,created_at) VALUES('list',?,'following',?,'queued',?)",
                          (seed, priority, accounts.iso(self.now)))
        return self.conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    def page(self, job):
        self.tick += 1
        at = accounts.iso(self.now + timedelta(seconds=self.tick))
        self.conn.execute('INSERT INTO pages(job_id,cursor,at,lane,users) VALUES(?,?,?,?,50)',
                          (job, str(self.tick), at, 'lane'))
        self.conn.execute("INSERT INTO collector_events(at,lane,job_id,kind,outcome) VALUES(?,'lane',?,'list','page')", (at,job))
        self.conn.execute("UPDATE lists SET lane='lane',cursor=?,state='running' WHERE seed=(SELECT seed FROM jobs WHERE id=?)",
                          (str(self.tick),job))

    def pick(self, kinds=('list','profile')):
        return accounts.pick_job(self.conn, 'lane', list(kinds), self.now + timedelta(seconds=self.tick+1))

    def test_bio_gets_turn_after_four_saved_list_pages(self):
        for _ in range(4):
            self.assertEqual(self.pick()['id'], self.large)
            self.page(self.large)
        self.assertEqual(self.pick()['id'], self.bio)

    def test_list_rotation_keeps_saved_cursor_and_gives_successor_a_quantum(self):
        for _ in range(4):
            self.assertEqual(self.pick(('list',))['id'], self.large)
            self.page(self.large)
        self.assertEqual(self.pick(('list',))['id'], self.small)
        self.page(self.small)
        self.assertEqual(self.pick(('list',))['id'], self.small)
        self.conn.execute("UPDATE jobs SET state='done' WHERE id=?", (self.small,))
        self.assertEqual(self.pick(('list',))['id'], self.large)
        row=self.conn.execute("SELECT cursor,lane FROM lists WHERE seed='large'").fetchone()
        self.assertEqual(tuple(row), ('4','lane'))

    def test_rotation_does_not_wait_when_other_work_is_delayed(self):
        for _ in range(4): self.page(self.large)
        future=accounts.iso(self.now+timedelta(hours=1))
        self.conn.execute('UPDATE jobs SET retry_not_before=? WHERE id IN (?,?)', (future,self.small,self.bio))
        self.assertEqual(self.pick()['id'], self.large)

    def test_passive_bio_cannot_reset_scheduled_fairness(self):
        for _ in range(4): self.page(self.large)
        self.conn.execute("INSERT INTO collector_events(at,lane,kind,outcome,route) VALUES(?,'lane','profile','profile','passive')",
                          (accounts.iso(self.now+timedelta(seconds=5)),))
        self.assertEqual(self.pick()['id'], self.bio)

    def test_profile_attempt_returns_turn_to_lists(self):
        for _ in range(4): self.page(self.large)
        self.conn.execute("INSERT INTO collector_events(at,lane,job_id,kind,outcome) VALUES(?,'lane',?,'profile','other')",
                          (accounts.iso(self.now+timedelta(seconds=5)),self.bio))
        self.assertEqual(self.pick()['id'], self.small)

    def test_explicit_list_only_poll_does_not_lease_bio(self):
        for _ in range(4): self.page(self.large)
        self.assertEqual(self.pick(('list',))['kind'], 'list')


class ExperimentEligibilityTest(unittest.TestCase):
    setUp = CollectionFairnessTest.setUp
    add_list = CollectionFairnessTest.add_list
    pick = CollectionFairnessTest.pick

    def test_experiment_requires_pinned_capable_alternate(self):
        self.conn.execute("UPDATE jobs SET experiment_viewer_ig_id='101',page_size=100 WHERE id=?",(self.large,))
        for version, identity, main, expected in (
                ('3.9.17','101',0,self.small),
                ('3.9.20','999',0,self.small),
                ('3.9.20','101',1,self.small),
                ('3.9.20','101',0,self.large)):
            with self.subTest(version=version,identity=identity,main=main):
                self.conn.execute('UPDATE accounts SET version=?,ig_id=?,is_main=? WHERE lane_id=?',
                                  (version,identity,main,'lane'))
                self.assertEqual(self.pick(('list',))['id'],expected)

    def test_private_experiment_is_not_reopened_as_unpinned_work(self):
        self.conn.execute("UPDATE jobs SET experiment_viewer_ig_id='999',page_size=100,state='done' WHERE id=?",(self.large,))
        self.conn.execute("UPDATE lists SET state='private',released_why='private' WHERE seed='large'")
        self.conn.execute("INSERT INTO list_private_denials(seed,direction,viewer_ig_id,denied_at) VALUES('large','following','999',?)",(accounts.iso(self.now),))
        row=self.conn.execute("SELECT * FROM accounts WHERE lane_id='lane'").fetchone()
        accounts.reopen_private_for_viewer(self.conn,row,self.now)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE seed='large'").fetchone()[0],1)
