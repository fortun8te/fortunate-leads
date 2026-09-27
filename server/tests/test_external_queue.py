"""Offline durability, bounded retry and stale-worker checks."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import external_queue as queue


class ExternalQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'queue.sqlite'
        self.conn = sqlite3.connect(self.path)
        queue.ensure(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def row(self):
        return self.conn.execute('SELECT input_hash,state,attempts FROM external_attempts WHERE person_id=1').fetchone()

    def test_success_and_unverified_are_terminal_across_restart(self):
        for pid, answer, state in ((1, {'fit':70}, 'complete'), (2, None, 'unverified')):
            token = queue.claim(self.conn,pid,'first',now=10)
            self.assertEqual(token,10)
            self.assertTrue(queue.complete(self.conn,pid,'first',answer,claim_token=token,now=11))
            self.conn.commit()
            with sqlite3.connect(self.path) as restarted:
                self.assertFalse(queue.eligible(restarted,pid,'first',now=10000))
                self.assertIsNone(queue.claim(restarted,pid,'first',now=10000))
                self.assertEqual(restarted.execute('SELECT state FROM external_attempts WHERE person_id=?',(pid,)).fetchone()[0],state)

    def test_crashed_claim_retries_only_once_after_full_lease(self):
        first = queue.claim(self.conn,1,'same',now=0)
        self.assertEqual(first,0)
        self.assertIsNone(queue.claim(self.conn,1,'same',now=179))
        second = queue.claim(self.conn,1,'same',now=180)
        self.assertEqual(second,180)
        self.assertIsNone(queue.claim(self.conn,1,'same',now=10000))
        self.assertEqual(self.row(),('same','running',2))
        self.assertFalse(queue.complete(self.conn,1,'same',{'fit':90},claim_token=first,now=181))
        self.assertFalse(queue.complete(self.conn,1,'same',{'fit':90},now=181))
        self.assertTrue(queue.complete(self.conn,1,'same',None,claim_token=second,now=182))
        self.assertEqual(self.row(),('same','unverified',2))

    def test_changed_input_is_eligible_and_old_result_cannot_clobber_it(self):
        old = queue.claim(self.conn,1,'old',now=10)
        new = queue.claim(self.conn,1,'new',now=20)
        self.assertEqual(new,20)
        self.assertFalse(queue.complete(self.conn,1,'old',None,claim_token=old,now=30))
        self.assertEqual(self.row(),('new','running',1))
        self.assertTrue(queue.complete(self.conn,1,'new',{},claim_token=new,now=31))

    def test_competing_worker_cannot_claim_committed_live_lease(self):
        self.assertIsNotNone(queue.claim(self.conn,1,'same',now=10))
        self.conn.commit()
        with sqlite3.connect(self.path) as other:
            self.assertIsNone(queue.claim(other,1,'same',now=11))

    def test_explicit_retry_cannot_interrupt_live_lease(self):
        queue.claim(self.conn,1,'same',now=10)
        self.assertFalse(queue.retry(self.conn,1,now=11))
        self.assertTrue(queue.retry(self.conn,1,now=190))
        token = queue.claim(self.conn,1,'same',now=191)
        queue.complete(self.conn,1,'same',None,claim_token=token,now=192)
        self.assertTrue(queue.retry(self.conn,1,now=193))
        self.assertTrue(queue.eligible(self.conn,1,'same',now=193))

    def test_sql_eligibility_matches_helper(self):
        self.conn.execute('CREATE TABLE people(id INTEGER PRIMARY KEY)')
        self.conn.execute('CREATE TABLE local_reviews(person_id INTEGER PRIMARY KEY,input_hash TEXT)')
        self.conn.executemany('INSERT INTO people VALUES(?)',[(1,),(2,),(3,),(4,)])
        self.conn.executemany('INSERT INTO local_reviews VALUES(?,?)',[(n,'same') for n in range(1,5)])
        for pid in (1,2,3):
            queue.claim(self.conn,pid,'same',now=10)
        queue.complete(self.conn,2,'same',None,claim_token=10,now=11)
        queue.claim(self.conn,3,'same',now=190)
        for now in (11,200,1000):
            sql = 'SELECT p.id FROM people p JOIN local_reviews l ON l.person_id=p.id WHERE ' + queue.eligible_sql()
            actual = {r[0] for r in self.conn.execute(sql,{'external_now':now})}
            expected = {pid for pid in range(1,5) if queue.eligible(self.conn,pid,'same',now=now)}
            self.assertEqual(actual,expected)
