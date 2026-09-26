"""Upgrade actual pre-observation databases without consulting the current schema.

The SQL below is frozen from base 9bac874. Fixtures contain fictional data only.
No test imports the HTTP test module or opens a user's collection database.
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connections
import db
import migrate


PRE_OBSERVATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY, ig_id TEXT UNIQUE, handle TEXT UNIQUE NOT NULL COLLATE NOCASE,
  name TEXT, pic_url TEXT, pic_file TEXT, is_private INT, is_verified INT,
  bio TEXT, website TEXT, category TEXT, followers INT, following INT, posts INT, is_business INT,
  bio_at TEXT, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seeds(handle TEXT PRIMARY KEY COLLATE NOCASE, ig_id TEXT, is_me INT DEFAULT 0, added_at TEXT);
CREATE TABLE IF NOT EXISTS lists(seed TEXT COLLATE NOCASE, direction TEXT CHECK(direction IN('followers','following')), state TEXT,
  cursor TEXT, received INT DEFAULT 0, total INT, error TEXT, updated_at TEXT, PRIMARY KEY(seed,direction));
CREATE TABLE IF NOT EXISTS edges(seed TEXT COLLATE NOCASE, person_id INT, direction TEXT, first_seen TEXT,
  PRIMARY KEY(seed,person_id,direction));
CREATE TABLE IF NOT EXISTS tags(person_id INT, tag TEXT, grp TEXT, source TEXT CHECK(source IN('auto','manual','rule')),
  PRIMARY KEY(person_id,tag));
CREATE TABLE IF NOT EXISTS tag_rules(id INTEGER PRIMARY KEY, tag TEXT NOT NULL, grp TEXT NOT NULL DEFAULT 'signal',
  field TEXT NOT NULL CHECK(field IN('bio','name','handle','category','website','any')), match TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS saved_views(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, query TEXT NOT NULL,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS verdicts(person_id INT PRIMARY KEY, prefilter INT, score INT, tier TEXT, role TEXT, reason TEXT,
  model TEXT, input_hash TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS laya(person_id INT PRIMARY KEY, input_hash TEXT, answers TEXT, fit INT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS marks(person_id INT PRIMARY KEY, status TEXT, note TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, kind TEXT CHECK(kind IN('list','profile')), seed TEXT, direction TEXT,
  handle TEXT, priority INT DEFAULT 0, state TEXT DEFAULT 'queued', attempts INT DEFAULT 0, leased_until TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS pages(job_id INT, cursor TEXT, at TEXT, PRIMARY KEY(job_id, cursor));  -- list pages already ingested
CREATE TABLE IF NOT EXISTS accounts(lane_id TEXT PRIMARY KEY, ig_id TEXT, handle TEXT, label TEXT,
  role TEXT NOT NULL DEFAULT 'both' CHECK(role IN('lists','bios','both')), budget TEXT, paused INT NOT NULL DEFAULT 0,
  is_main INT NOT NULL DEFAULT 0, first_seen TEXT, last_seen TEXT, version TEXT, state TEXT, hold TEXT, cooldown_until TEXT,
  list_cool_until TEXT, rate TEXT, today TEXT, last_error TEXT, activity TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS edges_person_seed ON edges(person_id, seed);   -- covering: lists count, seeds per person
CREATE INDEX IF NOT EXISTS tags_tag_src ON tags(tag, source, person_id, grp);   -- covering: facets, rule hits, tag filters
CREATE INDEX IF NOT EXISTS tags_person_src ON tags(person_id, source, tag);
CREATE INDEX IF NOT EXISTS verdicts_tier ON verdicts(tier, score);
CREATE INDEX IF NOT EXISTS verdicts_score ON verdicts(score);
CREATE INDEX IF NOT EXISTS people_updated ON people(updated_at);
CREATE INDEX IF NOT EXISTS people_followers ON people(followers);
CREATE INDEX IF NOT EXISTS people_first_seen ON people(first_seen);
CREATE INDEX IF NOT EXISTS people_bio_at ON people(bio_at);
CREATE INDEX IF NOT EXISTS marks_status ON marks(status);
CREATE INDEX IF NOT EXISTS jobs_next ON jobs(state, kind, priority);
CREATE INDEX IF NOT EXISTS jobs_handle ON jobs(handle);
"""


def snapshot(conn, tables):
    """Compare user data by existing columns, allowing additive schema changes."""
    result = {}
    for table in tables:
        columns = [r[1] for r in conn.execute(f'PRAGMA table_info({table})')]
        rows = [tuple(r) for r in conn.execute(f'SELECT {",".join(columns)} FROM {table}')]
        result[table] = (columns, sorted(rows, key=repr))
    return result


class PreObservationUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'old-leads.sqlite')
        self.make_old_database()

    def make_old_database(self):
        with closing(sqlite3.connect(self.path)) as old, old:
            old.executescript(PRE_OBSERVATION_SCHEMA)
            old.executescript("""
                INSERT INTO people(id,ig_id,handle,name,followers,first_seen,updated_at) VALUES
                  (1,'200','member','Member',123,'2019-01-01','2026-01-01'),
                  (2,NULL,'missing_id','Missing ID',NULL,'2019-01-02','2026-01-02'),
                  (3,'300','source','Current source',500,'2025-01-01','2026-01-03');
                INSERT INTO seeds VALUES
                  ('source','300',1,'2019-01-01'),('other','400',0,'2019-01-02');
                INSERT INTO edges VALUES
                  ('source',1,'following','2020-01-01'),
                  ('source',2,'followers',NULL),
                  ('other',1,'following','2021-05-01T10:11:12+00:00');
                INSERT INTO lists VALUES
                  ('source','following','running','page-three',1,9,NULL,'2026-09-01'),
                  ('source','followers','paused','last-page',1,NULL,'private','2026-09-02'),
                  ('other','following','done',NULL,1,1,NULL,'2026-09-03');
                INSERT INTO marks VALUES
                  (1,'client','Keep this client note','2026-02-01'),
                  (2,'talking','Next contact agreed','2026-02-02');
                INSERT INTO tags VALUES
                  (1,'Selected manually','signal','manual'),(2,'Rule match','role','rule');
                INSERT INTO verdicts VALUES
                  (1,90,87,'hot','founder','Manually reviewed evidence','fixture','hash','2026-02-03');
                INSERT INTO laya VALUES (1,'hash','{"answer":"keep"}',84,'2026-02-04');
                INSERT INTO tag_rules VALUES (1,'Rule match','role','bio','fictional founder','2026-02-05');
                INSERT INTO saved_views VALUES (1,'Client view','{"status":"client"}','2026-02-06');
                INSERT INTO jobs VALUES
                  (7,'list','source','following',NULL,4,'leased',2,'2027-01-01','2026-01-01');
                INSERT INTO pages VALUES (7,'page-two','2026-01-01');
                INSERT INTO settings VALUES ('paused','true'),('budget','{"profile":13,"list":57}');
                INSERT INTO accounts(lane_id,ig_id,handle,label,paused,is_main,first_seen,last_seen)
                  VALUES('fixture-lane','800','collector','Fictional collector',1,1,'2020-01-01','2026-01-01');
            """)
            self.original = snapshot(old, (
                'people', 'seeds', 'edges', 'lists', 'marks', 'tags', 'verdicts', 'laya',
                'tag_rules', 'saved_views', 'jobs', 'pages', 'accounts',
            ))
            self.assertIsNone(old.execute(
                "SELECT 1 FROM sqlite_master WHERE name='edge_observations'"
            ).fetchone())

    def open_upgraded(self):
        conn = db.init(self.path)
        self.addCleanup(conn.close)
        return conn

    def assert_original_data(self, conn):
        for table, (columns, rows) in self.original.items():
            with self.subTest(table=table):
                actual = [tuple(r) for r in conn.execute(f'SELECT {",".join(columns)} FROM {table}')]
                self.assertEqual(sorted(actual, key=repr), rows)

    def observations(self, conn):
        return [tuple(r) for r in conn.execute(
            'SELECT seed,person_id,direction,seed_ig_id,person_ig_id,first_seen,last_seen '
            'FROM edge_observations ORDER BY seed,person_id,direction,seed_ig_id,person_ig_id'
        )]

    def test_real_prechange_upgrade_preserves_lead_state_unknown_ids_and_dates(self):
        conn = self.open_upgraded()
        self.assert_original_data(conn)
        self.assertEqual(db.get_setting(conn, 'paused'), True)
        self.assertEqual(db.get_setting(conn, 'budget'), {'profile': 13, 'list': 57})
        self.assertEqual(self.observations(conn), [
            ('other', 1, 'following', '', '', '2021-05-01T10:11:12+00:00', None),
            ('source', 1, 'following', '', '', '2020-01-01', None),
            ('source', 2, 'followers', '', '', None, None),
        ])
        self.assertTrue(db.get_setting(conn, 'edge_observations_v1'))
        self.assertFalse(conn.in_transaction)
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_repeated_open_is_durable_and_does_not_refresh_or_duplicate_evidence(self):
        first = self.open_upgraded()
        expected = self.observations(first)
        first.close()
        for _ in range(3):
            conn = self.open_upgraded()
            self.assertEqual(self.observations(conn), expected)
            self.assert_original_data(conn)
            conn.close()

    def test_upgraded_unknown_endpoints_are_inspectable_but_not_bridges(self):
        conn = self.open_upgraded()
        direct = connections.explore(conn, 'legacy:source', 'legacy-person:1', 'following')
        self.assertEqual([p['nodes'] for p in direct['paths']], [['legacy:source', 'legacy-person:1']])
        self.assertIsNone(direct['source']['ig_id'])
        self.assertIsNone(direct['target']['ig_id'])
        self.assertEqual(direct['edges'][0]['evidence'][0]['first_seen'], '2020-01-01')
        self.assertIsNone(direct['edges'][0]['evidence'][0]['last_seen'])
        unknown_date = connections.explore(conn, 'legacy-person:2', 'legacy:source', 'following')
        self.assertEqual(len(unknown_date['paths']), 1)
        self.assertIsNone(unknown_date['edges'][0]['evidence'][0]['first_seen'])
        self.assertIsNone(unknown_date['edges'][0]['evidence'][0]['last_seen'])
        self.assertEqual(connections.explore(conn, 'legacy:source', 'legacy:other')['paths'], [])
        self.assertEqual(connections.explore(conn, 'source', 'member')['paths'], [])

        before = self.observations(conn)
        db.upsert_person(conn, {'handle': 'missing_id', 'ig_id': '999'}, '2026-09-26')
        conn.execute("UPDATE seeds SET ig_id='777' WHERE handle='source'")
        conn.commit()
        reopened = self.open_upgraded()
        self.assertEqual(self.observations(reopened), before)
        self.assertEqual(connections.explore(reopened, 'ig:999', 'seed:source')['paths'], [])

    def test_fresh_identified_capture_keeps_original_unknown_membership(self):
        conn = self.open_upgraded()
        before = self.observations(conn)
        self.assertFalse(db.add_edge(conn, 'source', 1, 'following', '2026-09-26',
                                     seed_ig_id='300', person_ig_id='200'))
        conn.commit()
        reopened = self.open_upgraded()
        self.assertEqual(len(self.observations(reopened)), len(before) + 1)
        self.assertTrue(set(before).issubset(self.observations(reopened)))
        self.assert_original_data(reopened)
        evidence = connections.explore(reopened, 'source', 'member')['edges'][0]['evidence']
        self.assertEqual([(e['first_seen'], e['last_seen']) for e in evidence],
                         [('2026-09-26', '2026-09-26')])

    def test_interrupted_backfill_never_commits_rows_without_marker_and_retries(self):
        writer = db.connect(self.path)
        self.addCleanup(writer.close)

        def fail_before_marker(conn, key, value):
            self.assertEqual(key, 'edge_observations_v1')
            self.assertEqual(len(self.observations(conn)), 3)
            # A separate connection must see neither the new evidence nor completion.
            with closing(sqlite3.connect(self.path)) as reader:
                self.assertEqual(reader.execute('SELECT count(*) FROM edge_observations').fetchone()[0], 0)
                self.assertIsNone(reader.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone())
            raise RuntimeError('simulated interruption before completion marker')

        with patch.object(db, 'connect', return_value=writer), patch.object(db, 'set_setting', side_effect=fail_before_marker):
            with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
                db.init(self.path)
        # Closing models process interruption. SQLite rolls the unfinished transaction back.
        writer.close()
        with closing(sqlite3.connect(self.path)) as reader:
            self.assertEqual(reader.execute('SELECT count(*) FROM edge_observations').fetchone()[0], 0)
            self.assert_original_data(reader)
        conn = self.open_upgraded()
        self.assertEqual(len(self.observations(conn)), 3)
        self.assertTrue(db.get_setting(conn, 'edge_observations_v1'))
        self.assert_original_data(conn)

    def test_older_tag_and_status_migrations_keep_notes_and_observation_history(self):
        with closing(sqlite3.connect(self.path)) as old, old:
            old.executescript("""
                DROP TABLE tags;
                CREATE TABLE tags(person_id INT,tag TEXT,grp TEXT,
                  source TEXT CHECK(source IN('auto','manual')),PRIMARY KEY(person_id,tag));
                INSERT INTO tags VALUES (1,'Preserved tag','signal','manual');
                UPDATE marks SET status='known' WHERE person_id=1;
                UPDATE marks SET status='good' WHERE person_id=2;
            """)
        conn = self.open_upgraded()
        self.assertEqual([tuple(r) for r in conn.execute('SELECT person_id,status,note FROM marks ORDER BY person_id')],
                         [(1, None, 'Keep this client note'), (2, 'interested', 'Next contact agreed')])
        self.assertEqual({tuple(r) for r in conn.execute('SELECT tag,source FROM tags WHERE person_id=1')},
                         {('Preserved tag', 'manual'), ('Already know them', 'manual')})
        conn.execute("INSERT INTO tags VALUES(2,'New rule','signal','rule')")
        conn.commit()
        before = self.observations(conn)
        reopened = self.open_upgraded()
        self.assertEqual(self.observations(reopened), before)
        self.assertEqual(reopened.execute('SELECT count(*) FROM tags').fetchone()[0], 3)


OLD_RESEARCH_SCHEMA = """
CREATE TABLE entities(id TEXT PRIMARY KEY,platform TEXT,handle TEXT,platform_id TEXT,created_at TEXT);
CREATE TABLE edges(src TEXT,dst TEXT,relationship TEXT,observed_date TEXT,evidence_id TEXT);
CREATE TABLE collections(source_entity TEXT,side TEXT,displayed_count INT,captured_count INT,
  coverage TEXT,observed_date TEXT,stop_reason TEXT);
CREATE TABLE profile_current(entity_id TEXT,field TEXT,value_json TEXT,observed_at TEXT);
CREATE TABLE known_people(entity_id TEXT,relation TEXT,note TEXT,added_at TEXT);
CREATE TABLE exporter_commands(payload TEXT,type TEXT,created_at TEXT);
CREATE TABLE imports(id TEXT PRIMARY KEY,metadata TEXT,created_at TEXT);
CREATE TABLE sightings(import_id TEXT,row_number INT,entity_id TEXT,raw_json TEXT);
CREATE TABLE evidence(id TEXT PRIMARY KEY,entity_id TEXT,observed_date TEXT,raw_ref TEXT);
"""


class HistoricalResearchImporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = Path(self.tmp.name) / 'research.sqlite'
        self.out = Path(self.tmp.name) / 'leads.sqlite'
        with closing(sqlite3.connect(self.old)) as old, old:
            old.executescript(OLD_RESEARCH_SCHEMA)
            old.executescript("""
                INSERT INTO entities VALUES
                  ('entity-source','instagram','source',NULL,'2020-01-01'),
                  ('entity-member','instagram','member',NULL,'2020-01-01');
                INSERT INTO edges VALUES
                  ('entity-source','entity-member','follows','2020-01-01','old-edge');
                INSERT INTO collections VALUES
                  ('entity-source','following',1,1,'complete','2020-01-01','done');
                INSERT INTO imports VALUES
                  ('old-import','{"source_profile":"source","side":"following","observed":"2020-01-01"}','2020-01-01');
                INSERT INTO sightings VALUES
                  ('old-import',1,'entity-member','{"handle":"member"}');
                INSERT INTO evidence VALUES
                  ('old-edge','entity-member','2020-01-01','old-import:1');
            """)

    def enrich_old_profiles(self):
        # This is the old Ledger.entity behavior: profile enrichment replaces NULL IDs.
        # It does not add a new follow observation or change the original raw import.
        with closing(sqlite3.connect(self.old)) as old, old:
            old.execute("UPDATE entities SET platform_id='100' WHERE id='entity-source'")
            old.execute("UPDATE entities SET platform_id='200' WHERE id='entity-member'")
            old.execute('INSERT INTO profile_current VALUES(?,?,?,?)',
                        ('entity-member', 'display_name', json.dumps('Enriched name'), '2026-09-26'))

    def observations(self):
        with closing(sqlite3.connect(self.out)) as conn:
            return [tuple(r) for r in conn.execute(
                'SELECT seed_ig_id,person_ig_id,first_seen,last_seen FROM edge_observations '
                'ORDER BY seed_ig_id,person_ig_id'
            )]

    def test_later_profile_ids_do_not_identify_earlier_handle_only_edges(self):
        self.enrich_old_profiles()
        migrate.migrate(self.old, self.out, None)
        self.assertEqual(self.observations(), [('', '', '2020-01-01', '2020-01-01')])
        conn = db.connect(str(self.out))
        self.addCleanup(conn.close)
        self.assertEqual(connections.explore(conn, 'source', 'member')['paths'], [])

    def test_reimport_after_enrichment_does_not_invent_second_historical_observation(self):
        migrate.migrate(self.old, self.out, None)
        original = self.observations()
        self.assertEqual(original, [('', '', '2020-01-01', '2020-01-01')])
        self.enrich_old_profiles()
        migrate.migrate(self.old, self.out, None)
        self.assertEqual(self.observations(), original)

    def test_original_raw_member_id_is_kept_without_guessing_source_identity(self):
        self.enrich_old_profiles()
        with closing(sqlite3.connect(self.old)) as old, old:
            old.execute('UPDATE sightings SET raw_json=?',
                        (json.dumps({'handle': 'member', 'platform_id': '200'}),))
        migrate.migrate(self.old, self.out, None)
        self.assertEqual(self.observations(), [('', '200', '2020-01-01', '2020-01-01')])


if __name__ == '__main__':
    unittest.main()
