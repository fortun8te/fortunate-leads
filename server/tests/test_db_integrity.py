"""Stable identity merges and truthful collection completion."""
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db


class IntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init(str(Path(self.tmp.name) / 'db.sqlite'))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_merge_keeps_richer_profile_notes_ai_and_edges(self):
        c = self.conn
        keep = db.upsert_person(c, {'handle': 'old', 'ig_id': '1'}, '2026-01-02')
        drop = db.upsert_person(c, {'handle': 'new', 'bio': 'Rich bio', 'website': 'https://a.test',
                                    'bio_at': '2026-02-02', 'bio_src': 'extension'}, '2026-01-01')
        db.add_edge(c, 'seed', keep, 'followers', '2026-02-02')
        db.add_edge(c, 'seed', drop, 'followers', '2026-01-01')
        db.add_edge(c, 'seed', drop, 'following', '2026-01-01')
        c.execute('INSERT INTO list_members VALUES(1,?,?)', (drop, '2026-01-01'))
        c.execute("INSERT INTO laya VALUES(?,'hash','{}',90,'2026-02-02')", (drop,))
        c.execute("INSERT INTO marks VALUES(?,'contacted','First note','2026-01-01')", (keep,))
        c.execute("INSERT INTO marks VALUES(?,'client','New note','2026-02-02')", (drop,))
        c.execute("INSERT INTO tags VALUES(?,'tag','signal','auto')", (keep,))
        c.execute("INSERT INTO tags VALUES(?,'tag','signal','manual')", (drop,))
        self.assertEqual(db.upsert_person(c, {'handle': 'new', 'ig_id': '1'}), keep)
        person = c.execute('SELECT * FROM people WHERE id=?', (keep,)).fetchone()
        self.assertEqual((person['bio'], person['website'], person['first_seen']), ('Rich bio', 'https://a.test', '2026-01-01'))
        self.assertEqual(c.execute('SELECT count(*) FROM people').fetchone()[0], 1)
        self.assertEqual(c.execute('SELECT count(*) FROM edges').fetchone()[0], 2)
        self.assertEqual(c.execute("SELECT first_seen FROM edges WHERE direction='followers'").fetchone()[0], '2026-01-01')
        self.assertEqual(c.execute('SELECT person_id FROM laya').fetchone()[0], keep)
        self.assertEqual(c.execute('SELECT person_id FROM list_members').fetchone()[0], keep)
        self.assertEqual(tuple(c.execute('SELECT status,note FROM marks').fetchone()), ('client', 'First note\n\nNew note'))
        self.assertEqual(c.execute('SELECT source FROM tags').fetchone()[0], 'manual')

    def test_short_list_after_retry_budget_is_partial_and_manual_retry_restarts(self):
        c = self.conn
        db.queue_list(c, 'seed', 'followers')
        c.execute("UPDATE jobs SET state='done'")
        c.execute("UPDATE lists SET state='done',received=20,total=100,cursor='old',run_job_id=1")
        db.set_setting(c, 'lists_reopened', {'seed|followers': db.REOPEN_MAX})
        self.assertEqual(db.repair_lists(c, dry=True)['partial'], 1)
        self.assertEqual(c.execute("SELECT state FROM lists WHERE direction='followers'").fetchone()[0], 'done')
        db.repair_lists(c)
        self.assertEqual(c.execute("SELECT state FROM lists WHERE direction='followers'").fetchone()[0], 'partial')
        self.assertEqual(db.repair_lists(c)['partial'], 0)
        self.assertTrue(db.queue_list(c, 'seed', 'followers'))
        self.assertEqual(tuple(c.execute("SELECT state,cursor,received,run_job_id FROM lists WHERE direction='followers'").fetchone()), ('queued', None, 0, None))

    def test_extension_observes_shared_public_retry_wait(self):
        import accounts
        from datetime import timedelta
        c = self.conn
        ts = db.now()
        job = c.execute("INSERT INTO jobs(kind,handle,created_at) VALUES('profile','waiting',?)", (ts,)).lastrowid
        now = db.utc_now()
        c.execute('INSERT INTO public_bio_retries VALUES(?,?,1)', (job, (now + timedelta(minutes=3)).isoformat()))
        self.assertIsNone(accounts.pick_job(c, 'test', ['profile'], now))
        self.assertEqual(accounts.pick_job(c, 'test', ['profile'], now + timedelta(minutes=4))['id'], job)


class FreshnessTest(unittest.TestCase):
    setUp = IntegrityTest.setUp
    tearDown = IntegrityTest.tearDown
    def test_identity_fold_rejects_stale_bio(self):
        c = self.conn
        keep = db.upsert_person(c, {'handle': 'old', 'ig_id': '1'}, '2026-01-01')
        db.upsert_person(c, {'handle': 'new', 'bio': 'new bio', 'bio_at': '2026-03-01'}, '2026-03-01')
        db.upsert_person(c, {'handle': 'new', 'ig_id': '1', 'bio': 'stale', 'bio_at': '2026-02-01'}, '2026-02-01')
        row = c.execute('SELECT * FROM people WHERE id=?', (keep,)).fetchone()
        self.assertEqual((row['bio'], row['bio_at'], row['updated_at']), ('new bio', '2026-03-01', '2026-03-01'))
        db.upsert_person(c, {'handle': 'new', 'ig_id': '1', 'bio': '', 'bio_at': '2026-04-01'}, '2026-04-01')
        self.assertEqual(c.execute('SELECT bio FROM people').fetchone()[0], '')

    def test_resume_copies_run_members_and_keeps_history(self):
        c = self.conn
        db.queue_list(c, 'seed', 'followers')
        original = c.execute('SELECT id FROM jobs').fetchone()[0]
        person = db.upsert_person(c, {'handle': 'member'})
        c.execute('INSERT INTO list_members VALUES(?,?,?)', (original, person, db.now()))
        c.execute("UPDATE lists SET state='error',cursor='resume',received=1,run_job_id=?", (original,))
        c.execute("UPDATE jobs SET state='error'")
        db.repair_lists(c)
        row = c.execute("SELECT * FROM lists WHERE direction='followers'").fetchone()
        self.assertNotEqual(row['run_job_id'], original)
        self.assertEqual((row['received'], row['cursor']), (1, 'resume'))
        self.assertEqual(c.execute('SELECT count(*) FROM list_members').fetchone()[0], 2)
        self.assertEqual(c.execute('SELECT state FROM jobs WHERE id=?', (original,)).fetchone()[0], 'error')

    def test_seed_rename_moves_relationships_and_rejects_old_lease(self):
        c = self.conn
        owner = db.upsert_person(c, {'handle': 'before', 'ig_id': '1'})
        member = db.upsert_person(c, {'handle': 'member'})
        db.queue_list(c, 'before', 'following')
        db.add_edge(c, 'before', member, 'following')
        c.execute("UPDATE jobs SET state='leased',lease_token='old-token'")
        db.upsert_person(c, {'handle': 'after', 'ig_id': '1'})
        self.assertEqual(tuple(c.execute('SELECT handle,ig_id FROM seeds').fetchone()), ('after', '1'))
        self.assertEqual(c.execute('SELECT seed FROM lists').fetchone()[0], 'after')
        self.assertEqual(tuple(c.execute('SELECT seed,state,lease_token FROM jobs').fetchone()), ('after', 'queued', None))
        self.assertEqual(c.execute('SELECT seed FROM edges').fetchone()[0], 'after')
        self.assertEqual(c.execute('SELECT handle FROM people WHERE id=?', (owner,)).fetchone()[0], 'after')

    def test_new_holder_cannot_inherit_seed_edges(self):
        c = self.conn
        old = db.upsert_person(c, {'handle': 'shared', 'ig_id': '1'})
        member = db.upsert_person(c, {'handle': 'member'})
        db.queue_list(c, 'shared', 'followers')
        db.add_edge(c, 'shared', member, 'followers')
        new = db.upsert_person(c, {'handle': 'shared', 'ig_id': '2'})
        self.assertNotEqual(old, new)
        parked = c.execute('SELECT handle FROM people WHERE id=?', (old,)).fetchone()[0]
        self.assertTrue(parked.startswith('shared~'))
        self.assertEqual(c.execute('SELECT seed FROM edges').fetchone()[0], parked)
        self.assertEqual(c.execute('SELECT state FROM jobs').fetchone()[0], 'cancelled')

    def test_destination_seed_collision_keeps_histories_separate(self):
        c = self.conn
        db.upsert_person(c, {'handle': 'before', 'ig_id': '1'})
        db.queue_list(c, 'before', 'following')
        db.queue_list(c, 'after', 'following')
        c.execute("UPDATE seeds SET ig_id='2' WHERE handle='after'")
        db.upsert_person(c, {'handle': 'after', 'ig_id': '1'})
        self.assertEqual(c.execute("SELECT ig_id FROM seeds WHERE handle='after'").fetchone()[0], '1')
        self.assertEqual(c.execute("SELECT count(*) FROM seeds WHERE ig_id='2' AND handle LIKE 'after~%'").fetchone()[0], 1)
        self.assertEqual(c.execute('SELECT count(*) FROM lists').fetchone()[0], 2)

    def test_same_id_seed_collision_combines_proven_identity(self):
        c = self.conn
        db.upsert_person(c, {'handle': 'before', 'ig_id': '1'})
        member = db.upsert_person(c, {'handle': 'member'})
        for name in ('before', 'after'):
            db.queue_list(c, name, 'following')
            c.execute('UPDATE seeds SET ig_id=? WHERE handle=?', ('1', name))
            db.add_edge(c, name, member, 'following')
        c.execute("UPDATE lists SET updated_at='2026-01-01' WHERE seed='before'")
        c.execute("UPDATE lists SET updated_at='2026-02-01',cursor='latest' WHERE seed='after'")
        db.upsert_person(c, {'handle': 'after', 'ig_id': '1'})
        self.assertEqual(c.execute('SELECT count(*) FROM seeds').fetchone()[0], 1)
        self.assertEqual(tuple(c.execute('SELECT seed,cursor FROM lists').fetchone()), ('after', 'latest'))
        self.assertEqual(c.execute('SELECT count(*) FROM edges').fetchone()[0], 1)
        self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[0], 1)
        self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)

    def test_newer_empty_bio_survives_older_duplicate(self):
        c = self.conn
        db.upsert_person(c, {'handle': 'old', 'ig_id': '1', 'bio': '', 'bio_at': '2026-03-01'}, '2026-03-01')
        db.upsert_person(c, {'handle': 'new', 'bio': 'obsolete', 'bio_at': '2026-01-01'}, '2026-01-01')
        db.upsert_person(c, {'handle': 'new', 'ig_id': '1'}, '2026-04-01')
        self.assertEqual(c.execute('SELECT bio FROM people').fetchone()[0], '')


class MigrationTest(unittest.TestCase):
    setUp = IntegrityTest.setUp
    tearDown = IntegrityTest.tearDown
    def test_rerun_preserves_current_list_and_manual_mark(self):
        import sqlite3
        import migrate
        old_path = str(Path(self.tmp.name) / 'legacy.sqlite')
        old = sqlite3.connect(old_path)
        old.executescript("""
            CREATE TABLE entities(id INT,handle TEXT,platform_id TEXT,created_at TEXT,platform TEXT);
            INSERT INTO entities VALUES(1,'seed','1','2026-01-01','instagram');
            CREATE TABLE profile_current(entity_id INT,field TEXT,value_json TEXT,observed_at TEXT);
            INSERT INTO profile_current VALUES(1,'biography','"old bio"','2026-01-01');
            CREATE TABLE collections(source_entity INT,side TEXT,displayed_count INT,coverage TEXT,observed_date TEXT);
            INSERT INTO collections VALUES(1,'followers',100,'complete','2026-01-01');
            CREATE TABLE exporter_commands(payload TEXT,type TEXT,created_at TEXT);
            CREATE TABLE edges(src INT,dst INT,observed_date TEXT,relationship TEXT);
            CREATE TABLE known_people(entity_id INT,relation TEXT,note TEXT,added_at TEXT);
            INSERT INTO known_people VALUES(1,'client','old note','2026-01-01');
        """)
        old.commit()
        old.close()
        c = self.conn
        person = db.upsert_person(c, {'handle': 'seed', 'ig_id': '1', 'bio': 'new bio', 'bio_at': '2026-03-01'}, '2026-03-01')
        db.queue_list(c, 'seed', 'followers')
        c.execute("UPDATE lists SET state='running',cursor='fresh-cursor',received=30,run_job_id=1")
        c.execute("INSERT INTO marks VALUES(?,'talking','my note','2026-03-01')", (person,))
        c.commit()
        for _ in range(2):
            migrate.migrate(old_path, str(Path(self.tmp.name) / 'db.sqlite'), None)
        self.assertEqual(tuple(c.execute('SELECT state,cursor,received,run_job_id FROM lists').fetchone()), ('running', 'fresh-cursor', 30, 1))
        self.assertEqual(tuple(c.execute('SELECT status,note FROM marks').fetchone()), ('talking', 'my note'))
        self.assertEqual(c.execute('SELECT bio FROM people').fetchone()[0], 'new bio')
