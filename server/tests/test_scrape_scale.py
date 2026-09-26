"""Exact counts and bounded network context for long Instagram list runs."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ['FL_NO_ORSLOT'] = '1'
import db
import server


class ScrapeScaleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(Path(self.tmp.name) / 'scrape.sqlite')

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_network_yield_only_uses_touched_seeds_but_keeps_mark_counts(self):
        c = self.conn
        person = db.upsert_person(c, {'handle': 'founder'})
        positive = db.upsert_person(c, {'handle': 'brand'})
        negative = db.upsert_person(c, {'handle': 'fan'})
        unrelated = db.upsert_person(c, {'handle': 'unrelated'})
        db.add_edge(c, 'brand', person, 'followers')
        db.add_edge(c, 'other', person, 'following')
        for pid in (positive, negative):
            db.add_edge(c, 'brand', pid, 'followers')
        db.add_edge(c, 'far_away', unrelated, 'followers')
        c.execute("INSERT INTO marks(person_id,status) VALUES(?,'interested')", (positive,))
        c.execute("INSERT INTO marks(person_id,status) VALUES(?,'no')", (negative,))
        c.execute("INSERT INTO marks(person_id,status) VALUES(?,'interested')", (unrelated,))
        net = server.network_context(c, [person], me='')[person]
        self.assertEqual(net['lists'], 2)
        self.assertEqual(net['client_seeds'], 1)
        self.assertEqual((net['seed_yield'], net['seed_marked']), (round(2 / 6, 3), 2))

    def test_member_counter_survives_duplicate_page_and_identity_merge(self):
        c = self.conn
        db.queue_list(c, 'seed', 'followers')
        job = c.execute("SELECT id FROM jobs WHERE kind='list'").fetchone()['id']
        anonymous = db.upsert_person(c, {'handle': 'alias'})
        known = db.upsert_person(c, {'handle': 'real', 'ig_id': '77'})
        c.execute('INSERT INTO list_members VALUES(?,?,?)', (job, anonymous, '2026-01-01'))
        c.execute('INSERT INTO list_members VALUES(?,?,?)', (job, known, '2026-01-01'))
        self.assertEqual(c.execute('SELECT member_count FROM list_runs WHERE job_id=?', (job,)).fetchone()[0], 2)
        c.execute('INSERT INTO list_members VALUES(?,?,?) ON CONFLICT(job_id,person_id) '
                  'DO UPDATE SET observed_at=excluded.observed_at', (job, anonymous, '2026-01-02'))
        self.assertEqual(c.execute('SELECT member_count FROM list_runs WHERE job_id=?', (job,)).fetchone()[0], 2)
        db.upsert_person(c, {'handle': 'alias', 'ig_id': '77'})
        self.assertEqual(c.execute('SELECT member_count FROM list_runs WHERE job_id=?', (job,)).fetchone()[0], 1)
        self.assertEqual(c.execute('SELECT count(*) FROM list_members WHERE job_id=?', (job,)).fetchone()[0], 1)

    def test_handoff_copies_counter_and_tracked_prefix(self):
        c = self.conn
        db.queue_list(c, 'seed', 'following')
        prior = c.execute("SELECT id FROM jobs WHERE kind='list'").fetchone()['id']
        member = db.upsert_person(c, {'handle': 'alice'})
        c.execute('INSERT INTO list_members VALUES(?,?,?)', (prior, member, '2026-01-01'))
        c.execute('INSERT INTO list_page_requests VALUES(?,?,?)', (prior, '', 'next'))
        c.execute('UPDATE list_runs SET first_page_seen=1,total=2,total_source=\'current_run\' WHERE job_id=?', (prior,))
        c.execute("UPDATE lists SET cursor='next',received=1,run_job_id=?,state='error' WHERE seed='seed' AND direction='following'", (prior,))
        c.execute("UPDATE jobs SET state='error' WHERE id=?", (prior,))
        db.repair_lists(c)
        new = c.execute("SELECT id FROM jobs WHERE kind='list' AND seed='seed' AND direction='following' AND state='queued'").fetchone()['id']
        self.assertNotEqual(new, prior)
        run = c.execute('SELECT first_page_seen,total,total_source,member_count FROM list_runs WHERE job_id=?', (new,)).fetchone()
        self.assertEqual(tuple(run), (1, 2, 'current_run', 1))

    def test_member_delete_and_job_reassignment_update_both_counts(self):
        c = self.conn
        c.executemany('INSERT INTO list_runs(job_id) VALUES(?)', [(1,), (2,)])
        c.executemany('INSERT INTO list_members VALUES(1,?,?)', [(1, 'a'), (2, 'b')])
        c.execute('UPDATE list_members SET job_id=2 WHERE job_id=1 AND person_id=2')
        self.assertEqual([r[0] for r in c.execute('SELECT member_count FROM list_runs ORDER BY job_id')], [1, 1])
        c.execute('DELETE FROM list_members WHERE job_id=2 AND person_id=2')
        self.assertEqual([r[0] for r in c.execute('SELECT member_count FROM list_runs ORDER BY job_id')], [1, 0])

    def test_old_database_backfills_counter_once(self):
        path = Path(self.tmp.name) / 'old.sqlite'
        import sqlite3
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE list_runs(job_id INTEGER PRIMARY KEY, first_page_seen INT DEFAULT 0, total INT, total_source TEXT DEFAULT 'unknown')")
        old.execute('CREATE TABLE list_members(job_id INT, person_id INT, observed_at TEXT, PRIMARY KEY(job_id,person_id))')
        old.execute("INSERT INTO list_runs VALUES(1,1,2,'current_run')")
        old.executemany("INSERT INTO list_members VALUES(1,?,'old')", [(1,), (2,)])
        old.commit()
        old.close()
        migrated = db.init(path)
        try:
            self.assertEqual(migrated.execute('SELECT member_count FROM list_runs WHERE job_id=1').fetchone()[0], 2)
            migrated.execute("INSERT INTO list_members VALUES(1,3,'new')")
            self.assertEqual(migrated.execute('SELECT member_count FROM list_runs WHERE job_id=1').fetchone()[0], 3)
        finally:
            migrated.close()


if __name__ == '__main__':
    unittest.main()
