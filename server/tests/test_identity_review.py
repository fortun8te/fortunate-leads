"""Identity regressions run only against disposable SQLite databases."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db


class IdentityReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'test.sqlite'))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def person(self, handle, ig_id, ts):
        return db.upsert_person(self.conn, {'handle': handle, 'ig_id': ig_id}, ts)

    def test_older_rename_cannot_displace_newer_destination_owner(self):
        c = self.conn
        claimant = self.person('before', '1', '2026-01-01')
        owner = self.person('shared', '2', '2026-03-01')
        db.queue_list(c, 'shared', 'following')
        c.execute("UPDATE seeds SET ig_id='2' WHERE handle='shared'")
        member = self.person('member', '3', '2026-01-01')
        db.add_edge(c, 'shared', member, 'following')
        result = self.person('shared', '1', '2026-02-01')
        self.assertEqual(result, claimant)
        self.assertEqual(c.execute('SELECT handle FROM people WHERE id=?', (owner,)).fetchone()[0], 'shared')
        self.assertEqual(c.execute('SELECT handle FROM people WHERE id=?', (claimant,)).fetchone()[0], 'before')
        self.assertEqual(tuple(c.execute('SELECT handle,ig_id FROM seeds').fetchone()), ('shared', '2'))
        self.assertEqual(c.execute('SELECT seed FROM edges').fetchone()[0], 'shared')

    def test_historical_new_identity_is_retained_without_displacing_owner(self):
        c = self.conn
        owner = self.person('shared', '2', '2026-03-01')
        historical = self.person('shared', '1', '2026-02-01')
        self.assertNotEqual(historical, owner)
        self.assertEqual(c.execute('SELECT ig_id FROM people WHERE handle=?', ('shared',)).fetchone()[0], '2')
        self.assertTrue(c.execute('SELECT handle FROM people WHERE id=?', (historical,)).fetchone()[0].startswith('shared~'))
        self.assertEqual(self.person('shared', '1', '2026-04-01'), historical)
        self.assertEqual(c.execute('SELECT ig_id FROM people WHERE handle=?', ('shared',)).fetchone()[0], '1')

    def test_equal_timestamp_conflicting_claim_keeps_current_owner(self):
        owner = self.person('shared', '2', '2026-03-01')
        self.person('shared', '1', '2026-03-01')
        self.assertEqual(self.conn.execute('SELECT id FROM people WHERE handle=?', ('shared',)).fetchone()[0], owner)

    def profile_job(self, handle, state='leased'):
        return self.conn.execute("INSERT INTO jobs(kind,handle,state,lease_token,lane,leased_until) "
                                 "VALUES('profile',?,?,'old-token','lane','2099-01-01')", (handle, state)).lastrowid

    def test_nonseed_rename_moves_profile_work_and_revokes_old_lease(self):
        c = self.conn
        owner = self.person('before', '1', '2026-01-01')
        leased = self.profile_job('before')
        queued = self.profile_job('before', 'queued')
        self.person('after', '1', '2026-02-01')
        replacement = self.person('before', '2', '2026-03-01')
        self.assertNotEqual(owner, replacement)
        self.assertEqual(c.execute('SELECT count(*) FROM seeds').fetchone()[0], 0)
        for jid in (leased, queued):
            self.assertEqual(tuple(c.execute('SELECT handle,state,lease_token,lane,leased_until FROM jobs WHERE id=?',
                                             (jid,)).fetchone()), ('after', 'queued', None, None, None))

    def test_nonseed_displacement_cancels_old_profile_work(self):
        c = self.conn
        old = self.person('shared', '1', '2026-01-01')
        job = self.profile_job('shared')
        self.person('shared', '2', '2026-02-01')
        parked = c.execute('SELECT handle FROM people WHERE id=?', (old,)).fetchone()[0]
        self.assertTrue(parked.startswith('shared~'))
        self.assertEqual(tuple(c.execute('SELECT handle,state,lease_token FROM jobs WHERE id=?', (job,)).fetchone()),
                         (parked, 'cancelled', None))

    def test_note_only_duplicate_preserves_status_in_either_merge_direction(self):
        c = self.conn
        for status_on_keep in (True, False):
            with self.subTest(status_on_keep=status_on_keep):
                suffix = str(status_on_keep).lower()
                keep = self.person('old' + suffix, suffix, '2026-01-01')
                drop = db.upsert_person(c, {'handle': 'new' + suffix}, '2026-01-01')
                status_pid, note_pid = (keep, drop) if status_on_keep else (drop, keep)
                c.execute("INSERT INTO marks VALUES(?,'client','Client history','2026-01-01')", (status_pid,))
                c.execute("INSERT INTO marks VALUES(?,NULL,'New note','2026-02-01')", (note_pid,))
                db.merge_people(c, keep, drop)
                mark = c.execute('SELECT * FROM marks WHERE person_id=?', (keep,)).fetchone()
                self.assertEqual(mark['status'], 'client')
                self.assertEqual(set(mark['note'].split('\n\n')), {'Client history', 'New note'})
                self.assertEqual(mark['updated_at'], '2026-02-01')

    def test_two_explicit_statuses_keep_the_newer_choice(self):
        c = self.conn
        keep = self.person('old', '1', '2026-01-01')
        drop = db.upsert_person(c, {'handle': 'new'}, '2026-01-01')
        c.execute("INSERT INTO marks VALUES(?,'client',NULL,'2026-01-01')", (keep,))
        c.execute("INSERT INTO marks VALUES(?,'no',NULL,'2026-02-01')", (drop,))
        db.merge_people(c, keep, drop)
        self.assertEqual(tuple(c.execute('SELECT status,note FROM marks WHERE person_id=?', (keep,)).fetchone()), ('no', None))


if __name__ == '__main__':
    unittest.main()
