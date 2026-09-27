import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

SCHEMA = """
CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY, ig_id TEXT UNIQUE, handle TEXT UNIQUE NOT NULL COLLATE NOCASE,
  name TEXT, pic_url TEXT, pic_file TEXT, is_private INT, is_verified INT,
  bio TEXT, website TEXT, category TEXT, followers INT, following INT, posts INT, is_business INT,
  bio_at TEXT, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seeds(handle TEXT PRIMARY KEY COLLATE NOCASE, ig_id TEXT, is_me INT DEFAULT 0, added_at TEXT);
CREATE TABLE IF NOT EXISTS lists(seed TEXT COLLATE NOCASE, direction TEXT CHECK(direction IN('followers','following')), state TEXT,
  cursor TEXT, received INT DEFAULT 0, total INT, error TEXT, updated_at TEXT, PRIMARY KEY(seed,direction));
CREATE INDEX IF NOT EXISTS lists_direction_updated ON lists(direction,updated_at);
CREATE TABLE IF NOT EXISTS edges(seed TEXT COLLATE NOCASE, person_id INT, direction TEXT, first_seen TEXT,
  PRIMARY KEY(seed,person_id,direction));
-- Only directly ingested page members get observations; never infer freshness from list timestamps.
CREATE TABLE IF NOT EXISTS edge_observations(seed TEXT COLLATE NOCASE NOT NULL, person_id INT NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN('followers','following')), page_key TEXT NOT NULL, job_id INT,
  observed_at TEXT NOT NULL, PRIMARY KEY(seed,person_id,direction,page_key));
CREATE INDEX IF NOT EXISTS edge_observations_person ON edge_observations(person_id,seed,direction,observed_at);
CREATE TABLE IF NOT EXISTS ingested_list_pages(page_key TEXT PRIMARY KEY, seed TEXT COLLATE NOCASE NOT NULL,
  direction TEXT NOT NULL, observed_at TEXT NOT NULL);
-- edges is discovery history. This table contains the latest decisive observation.
-- Existing edges without a row here remain historical/unverified after upgrade.
CREATE TABLE IF NOT EXISTS edge_evidence(seed TEXT COLLATE NOCASE, person_id INT, direction TEXT,
  active INT NOT NULL CHECK(active IN (0,1)), observed_at TEXT, checked_at TEXT NOT NULL,
  PRIMARY KEY(seed,person_id,direction));
CREATE VIEW IF NOT EXISTS current_edges AS
  SELECT e.seed,e.person_id,e.direction,e.first_seen,v.observed_at FROM edges e
  JOIN edge_evidence v ON v.seed=e.seed AND v.person_id=e.person_id AND v.direction=e.direction
  WHERE v.active=1;
-- Exact distinct current source count for bounded map reads. Rebuilt once on
-- upgrade, then maintained in the same transaction as edge/evidence changes.
CREATE TABLE IF NOT EXISTS map_person_degree(person_id INTEGER PRIMARY KEY, degree INTEGER NOT NULL CHECK(degree>0),
  score REAL, hidden INTEGER NOT NULL DEFAULT 0 CHECK(hidden IN (0,1)));
CREATE TABLE IF NOT EXISTS map_source_handles(handle TEXT PRIMARY KEY COLLATE NOCASE, refs INTEGER NOT NULL CHECK(refs>=0));
CREATE TABLE IF NOT EXISTS map_seed_member(person_id INTEGER NOT NULL, seed TEXT NOT NULL COLLATE NOCASE,
  PRIMARY KEY(person_id,seed));
CREATE INDEX IF NOT EXISTS map_seed_member_seed ON map_seed_member(seed,person_id);
CREATE TABLE IF NOT EXISTS map_seed_degree(seed TEXT PRIMARY KEY COLLATE NOCASE, degree INTEGER NOT NULL CHECK(degree>=0));
CREATE TABLE IF NOT EXISTS tags(person_id INT, tag TEXT, grp TEXT, source TEXT CHECK(source IN('auto','manual','rule')),
  PRIMARY KEY(person_id,tag));
CREATE TABLE IF NOT EXISTS tag_rules(id INTEGER PRIMARY KEY, tag TEXT NOT NULL, grp TEXT NOT NULL DEFAULT 'signal',
  field TEXT NOT NULL CHECK(field IN('bio','name','handle','category','website','any')), match TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS saved_views(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, query TEXT NOT NULL,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS verdicts(person_id INT PRIMARY KEY, prefilter INT, score INT, tier TEXT, role TEXT, reason TEXT,
  model TEXT, input_hash TEXT, updated_at TEXT);
-- One committed model verdict per row. Profile revisions and rule rescoring never alter this history.
CREATE TABLE IF NOT EXISTS ai_scoring_events(id INTEGER PRIMARY KEY, person_id INT NOT NULL, scored_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ai_scoring_events_at ON ai_scoring_events(scored_at);
CREATE INDEX IF NOT EXISTS ai_scoring_events_person ON ai_scoring_events(person_id);
CREATE TABLE IF NOT EXISTS laya(person_id INT PRIMARY KEY, input_hash TEXT, answers TEXT, fit INT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS marks(person_id INT PRIMARY KEY, status TEXT, note TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS owner_context(person_id INT PRIMARY KEY, relationships TEXT NOT NULL DEFAULT '[]', familiarity TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS followups(person_id INTEGER PRIMARY KEY, due_on TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', completed_at TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', before_value TEXT, after_value TEXT, happened_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS followups_due ON followups(completed_at,due_on,person_id);
CREATE INDEX IF NOT EXISTS activity_person_time ON activity(person_id,happened_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, kind TEXT CHECK(kind IN('list','profile')), seed TEXT, direction TEXT,
  handle TEXT, priority INT DEFAULT 0, state TEXT DEFAULT 'queued', attempts INT DEFAULT 0, leased_until TEXT, created_at TEXT,
  retry_not_before TEXT, limit_hits INT NOT NULL DEFAULT 0, page_size INT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS network_dirty(person_id INTEGER PRIMARY KEY, change_id INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS network_dirty_change ON network_dirty(change_id, person_id);   -- the drain reads in change order
-- Per-run proof is separate from display totals and transient error messages.
CREATE TABLE IF NOT EXISTS list_runs(job_id INTEGER PRIMARY KEY, first_page_seen INT NOT NULL DEFAULT 0,
  total INT, total_source TEXT NOT NULL DEFAULT 'unknown', member_count INT NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS list_page_requests(job_id INT, requested_cursor TEXT NOT NULL, next_cursor TEXT,
  PRIMARY KEY(job_id,requested_cursor));
CREATE TABLE IF NOT EXISTS list_members(job_id INT, person_id INT, observed_at TEXT, PRIMARY KEY(job_id,person_id));
CREATE TABLE IF NOT EXISTS pages(job_id INT, cursor TEXT, at TEXT, PRIMARY KEY(job_id, cursor));  -- list pages already ingested
CREATE TABLE IF NOT EXISTS collector_events(id INTEGER PRIMARY KEY, event_id TEXT UNIQUE, at TEXT NOT NULL,
  lane TEXT, job_id INT, kind TEXT, direction TEXT, outcome TEXT NOT NULL, reason TEXT,
  http_status INT, requested_count INT, returned_count INT, new_links INT);
CREATE INDEX IF NOT EXISTS collector_events_at ON collector_events(at);
CREATE INDEX IF NOT EXISTS collector_events_lane_at ON collector_events(lane,at);
CREATE TABLE IF NOT EXISTS accounts(lane_id TEXT PRIMARY KEY, ig_id TEXT, handle TEXT, label TEXT,
  role TEXT NOT NULL DEFAULT 'both' CHECK(role IN('lists','bios','both')), budget TEXT, paused INT NOT NULL DEFAULT 0,
  is_main INT NOT NULL DEFAULT 0, first_seen TEXT, last_seen TEXT, version TEXT, state TEXT, hold TEXT, cooldown_until TEXT,
  list_cool_until TEXT, profile_cool_until TEXT, list_endpoint_until TEXT, rate TEXT, today TEXT, last_error TEXT, activity TEXT, text TEXT);
-- A Chrome lane can switch Instagram identities and later switch back. Keep
-- each identity's same-day usage and active waits outside the mutable lane row.
CREATE TABLE IF NOT EXISTS account_identity_state(lane_id TEXT NOT NULL, ig_id TEXT NOT NULL,
  day TEXT, today TEXT, cooldown_until TEXT, list_cool_until TEXT, profile_cool_until TEXT,
  list_endpoint_until TEXT, PRIMARY KEY(lane_id,ig_id));
CREATE INDEX IF NOT EXISTS account_identity_ig ON account_identity_state(ig_id);
CREATE TABLE IF NOT EXISTS list_private_denials(seed TEXT NOT NULL COLLATE NOCASE, direction TEXT NOT NULL,
  viewer_ig_id TEXT NOT NULL, denied_at TEXT NOT NULL,
  PRIMARY KEY(seed,direction,viewer_ig_id));
CREATE INDEX IF NOT EXISTS list_members_person ON list_members(person_id);
CREATE INDEX IF NOT EXISTS edges_person_seed ON edges(person_id, seed);   -- covering: lists count, seeds per person
CREATE INDEX IF NOT EXISTS edge_evidence_person ON edge_evidence(person_id,seed);
CREATE INDEX IF NOT EXISTS tags_tag_src ON tags(tag, source, person_id, grp);   -- covering: facets, rule hits, tag filters
CREATE INDEX IF NOT EXISTS tags_person_src ON tags(person_id, source, tag);
CREATE INDEX IF NOT EXISTS verdicts_tier ON verdicts(tier, score);
CREATE INDEX IF NOT EXISTS verdicts_score ON verdicts(score);
CREATE INDEX IF NOT EXISTS verdicts_prefilter ON verdicts(prefilter DESC,person_id);
CREATE INDEX IF NOT EXISTS people_updated ON people(updated_at);
CREATE INDEX IF NOT EXISTS people_followers ON people(followers);
CREATE INDEX IF NOT EXISTS people_first_seen ON people(first_seen);
CREATE INDEX IF NOT EXISTS people_bio_at ON people(bio_at);
CREATE INDEX IF NOT EXISTS marks_status ON marks(status);
CREATE INDEX IF NOT EXISTS jobs_next ON jobs(state, kind, priority);
CREATE INDEX IF NOT EXISTS jobs_list_seed_state ON jobs(kind,seed,direction,state);
CREATE INDEX IF NOT EXISTS jobs_handle ON jobs(handle);
-- Only pending Laya work is ranked. The signature-specific population is rebuilt
-- once by laya_step; these triggers keep subsequent profile/rank changes current.
CREATE TABLE IF NOT EXISTS laya_queue(person_id INTEGER PRIMARY KEY, bio_blank INTEGER NOT NULL,
  prefilter INTEGER);
CREATE INDEX IF NOT EXISTS laya_queue_rank ON laya_queue(bio_blank,prefilter DESC,person_id);
CREATE TRIGGER IF NOT EXISTS laya_queue_person_insert AFTER INSERT ON people BEGIN
  INSERT OR REPLACE INTO laya_queue
  SELECT NEW.id,coalesce(NEW.bio,'')='',(SELECT prefilter FROM verdicts WHERE person_id=NEW.id)
  WHERE instr(NEW.handle,'~')=0 AND NOT EXISTS
    (SELECT 1 FROM seeds WHERE is_me=1 AND handle=NEW.handle);
END;
CREATE TRIGGER IF NOT EXISTS laya_queue_person_update AFTER UPDATE OF handle,name,bio,category,website,followers ON people
WHEN OLD.handle IS NOT NEW.handle OR OLD.name IS NOT NEW.name OR OLD.bio IS NOT NEW.bio
  OR OLD.category IS NOT NEW.category OR OLD.website IS NOT NEW.website OR OLD.followers IS NOT NEW.followers
BEGIN
  DELETE FROM laya_queue WHERE person_id=NEW.id;
  INSERT INTO laya_queue
  SELECT NEW.id,coalesce(NEW.bio,'')='',(SELECT prefilter FROM verdicts WHERE person_id=NEW.id)
  WHERE instr(NEW.handle,'~')=0 AND NOT EXISTS
    (SELECT 1 FROM seeds WHERE is_me=1 AND handle=NEW.handle);
END;
CREATE TRIGGER IF NOT EXISTS laya_queue_person_delete AFTER DELETE ON people BEGIN
  DELETE FROM laya_queue WHERE person_id=OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS laya_queue_verdict_insert AFTER INSERT ON verdicts BEGIN
  UPDATE laya_queue SET prefilter=NEW.prefilter WHERE person_id=NEW.person_id;
END;
CREATE TRIGGER IF NOT EXISTS laya_queue_verdict_update AFTER UPDATE OF prefilter ON verdicts
WHEN OLD.prefilter IS NOT NEW.prefilter BEGIN
  UPDATE laya_queue SET prefilter=NEW.prefilter WHERE person_id=NEW.person_id;
END;
CREATE TRIGGER IF NOT EXISTS laya_queue_verdict_delete AFTER DELETE ON verdicts BEGIN
  UPDATE laya_queue SET prefilter=NULL WHERE person_id=OLD.person_id;
END;
CREATE TRIGGER IF NOT EXISTS laya_queue_laya_delete AFTER DELETE ON laya BEGIN
  INSERT OR REPLACE INTO laya_queue
  SELECT p.id,coalesce(p.bio,'')='',(SELECT prefilter FROM verdicts WHERE person_id=p.id)
  FROM people p WHERE p.id=OLD.person_id AND instr(p.handle,'~')=0 AND NOT EXISTS
    (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle);
END;
CREATE TRIGGER IF NOT EXISTS laya_queue_laya_insert AFTER INSERT ON laya BEGIN
  INSERT OR REPLACE INTO laya_queue
  SELECT p.id,coalesce(p.bio,'')='',(SELECT prefilter FROM verdicts WHERE person_id=p.id)
  FROM people p WHERE p.id=NEW.person_id AND instr(p.handle,'~')=0 AND NOT EXISTS
    (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle);
END;
CREATE TRIGGER IF NOT EXISTS laya_queue_laya_update AFTER UPDATE ON laya BEGIN
  INSERT OR REPLACE INTO laya_queue
  SELECT p.id,coalesce(p.bio,'')='',(SELECT prefilter FROM verdicts WHERE person_id=p.id)
  FROM people p WHERE p.id=NEW.person_id AND instr(p.handle,'~')=0 AND NOT EXISTS
    (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle);
END;
CREATE TRIGGER IF NOT EXISTS laya_queue_seed_insert AFTER INSERT ON seeds WHEN NEW.is_me=1 BEGIN
  DELETE FROM laya_queue WHERE person_id IN (SELECT id FROM people WHERE handle=NEW.handle);
END;
CREATE TRIGGER IF NOT EXISTS laya_queue_seed_delete AFTER DELETE ON seeds WHEN OLD.is_me=1 BEGIN
  INSERT OR IGNORE INTO laya_queue
  SELECT p.id,coalesce(p.bio,'')='',v.prefilter FROM people p LEFT JOIN verdicts v ON v.person_id=p.id
  WHERE p.handle=OLD.handle AND instr(p.handle,'~')=0;
END;
CREATE TRIGGER IF NOT EXISTS laya_queue_seed_update AFTER UPDATE OF handle,is_me ON seeds
WHEN OLD.handle IS NOT NEW.handle OR OLD.is_me IS NOT NEW.is_me BEGIN
  INSERT OR IGNORE INTO laya_queue
  SELECT p.id,coalesce(p.bio,'')='',v.prefilter FROM people p LEFT JOIN verdicts v ON v.person_id=p.id
  WHERE p.handle=OLD.handle AND OLD.is_me=1 AND instr(p.handle,'~')=0;
  DELETE FROM laya_queue WHERE NEW.is_me=1 AND person_id IN (SELECT id FROM people WHERE handle=NEW.handle);
END;
-- Legacy databases can already have the owner's profile queued without an is_me seed.
-- This guard also catches later inserts from the profile/Laya maintenance triggers.
CREATE TRIGGER IF NOT EXISTS laya_queue_self_guard AFTER INSERT ON laya_queue
WHEN EXISTS (SELECT 1 FROM people WHERE id=NEW.person_id AND handle='fortun8te' COLLATE NOCASE)
BEGIN
  DELETE FROM laya_queue WHERE person_id=NEW.person_id;
END;
"""

BIO_FIELDS = {'bio', 'bio_at', 'bio_src', 'website', 'category', 'followers', 'following', 'posts', 'is_business'}

PERSON_FIELDS = ('ig_id', 'handle', 'name', 'pic_url', 'is_private', 'is_verified', 'bio', 'website', 'category',
                 'followers', 'following', 'posts', 'is_business', 'bio_at', 'bio_src')
# budget: per account per day (list pages, profile reads; profile 0 = no daily number). bio_min: prefilter floor for planned
# bio reads (an explicit read ignores it).
DEFAULTS = {'paused': False, 'budget': {'list': 3000, 'profile': 300}, 'qualify': False, 'qualify_auto': False, 'llm_workers': 8,
            'llm_min': 40, 'bio_min': 25, 'main_list_share': 0}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def utc_now():
    return datetime.now(timezone.utc)


def connect(path):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=15000')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA mmap_size=268435456')
    conn.execute('PRAGMA temp_store=MEMORY')
    return conn


TAGS_V2 = """CREATE TABLE tags_v2(person_id INT, tag TEXT, grp TEXT, source TEXT CHECK(source IN('auto','manual','rule')),
  PRIMARY KEY(person_id,tag))"""


def migrate_tags(conn):
    """DBs created before tag rules have CHECK(source IN('auto','manual')); SQLite can't alter a CHECK, so rebuild the table."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tags'").fetchone()[0]
    if "'rule'" in sql:
        return False
    conn.commit()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DROP TABLE IF EXISTS tags_v2')
        conn.execute(TAGS_V2)
        conn.execute('INSERT INTO tags_v2(person_id, tag, grp, source) SELECT person_id, tag, grp, source FROM tags')
        conn.execute('DROP TABLE tags')
        conn.execute('ALTER TABLE tags_v2 RENAME TO tags')
        conn.execute('CREATE INDEX IF NOT EXISTS tags_tag_src ON tags(tag, source, person_id, grp)')
        conn.execute('CREATE INDEX IF NOT EXISTS tags_person_src ON tags(person_id, source, tag)')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


# 2026-09: statuses became a pipeline (interested, contacted, talking, client, no = Not a fit).
# good -> interested. maybe and known are not pipeline steps: they become manual tags ('Maybe', 'Already know them'),
# the status is cleared and the note kept. Idempotent: only rows still holding an old value are touched.
OLD_STATUS_TAGS = {'maybe': 'Maybe', 'known': 'Already know them'}


def migrate_statuses(conn):
    conn.execute("UPDATE marks SET status='interested' WHERE status='good'")
    for old, tag in OLD_STATUS_TAGS.items():
        conn.execute("INSERT OR REPLACE INTO tags(person_id, tag, grp, source) SELECT person_id, ?, 'signal', 'manual' "
                     "FROM marks WHERE status=?", (tag, old))
        conn.execute('UPDATE marks SET status=NULL WHERE status=?', (old,))
    conn.execute("DELETE FROM marks WHERE status IS NULL AND coalesce(note,'')=''")


def init(path):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.execute("DELETE FROM laya_queue WHERE person_id IN "
                 "(SELECT id FROM people WHERE handle='fortun8te' COLLATE NOCASE)")
    migrate_tags(conn)
    conn.execute('DROP INDEX IF EXISTS tags_tag')  # superseded by the covering tags_tag_src
    conn.execute('DROP INDEX IF EXISTS edges_person')  # superseded by the covering edges_person_seed
    # columns added after the first release: ALTER only when missing, so any older DB opens as is
    for table, col, decl in (('pages', 'at', 'TEXT'), ('pages', 'lane', 'TEXT'), ('pages', 'users', 'INT'),
                             ('verdicts', 'prompt', 'TEXT'), ('verdicts', 'evidence', 'TEXT'), ('verdicts', 'content_fit', 'REAL'),
                             ('jobs', 'lane', 'TEXT'), ('jobs', 'lease_token', 'TEXT'), ('jobs', 'viewer_ig_id', 'TEXT'),
                             ('jobs', 'retry_not_before', 'TEXT'), ('jobs', 'limit_hits', 'INT NOT NULL DEFAULT 0'),
                             ('jobs', 'page_size', 'INT'),
                             ('accounts', 'profile_cool_until', 'TEXT'),
                             ('accounts', 'list_endpoint_until', 'TEXT'),
                             ('lists', 'lane', 'TEXT'), ('lists', 'prev_lane', 'TEXT'),
                             ('list_runs', 'member_count', 'INT NOT NULL DEFAULT 0'),
                             ('lists', 'run_job_id', 'INT'), ('lists', 'released_at', 'TEXT'), ('lists', 'released_why', 'TEXT'),
                             ('people', 'bio_src', 'TEXT'), ('people', 'bd_at', 'TEXT'),
                             ('people', 'pic_refresh', 'INT NOT NULL DEFAULT 0')):
        if col not in {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {decl}')
            if table == 'list_runs' and col == 'member_count':
                # Existing tracked prefixes already have members. Backfill once
                # before triggers start maintaining the exact count.
                conn.execute('UPDATE list_runs SET member_count=(SELECT count(*) FROM list_members WHERE job_id=list_runs.job_id)')
    conn.execute('INSERT OR IGNORE INTO account_identity_state '
                 '(lane_id,ig_id,day,today,cooldown_until,list_cool_until,profile_cool_until,list_endpoint_until) '
                 "SELECT lane_id,ig_id,date(last_seen,'localtime'),today,cooldown_until,list_cool_until,profile_cool_until,list_endpoint_until "
                 'FROM accounts WHERE ig_id IS NOT NULL')
    # Earlier collectors called a terminal page "done" even when the saved
    # list was capped or short. Correct only the job still identified as that
    # partial run; a later refresh may have superseded older job evidence.
    conn.execute("UPDATE jobs SET state='partial' WHERE kind='list' AND state='done' "
                 "AND EXISTS(SELECT 1 FROM lists l WHERE l.run_job_id=jobs.id AND l.state='partial')")
    for name in ('list_members_count_insert', 'list_members_count_delete', 'list_members_count_reassign'):
        conn.execute(f'DROP TRIGGER IF EXISTS {name}')
    conn.execute('CREATE TRIGGER list_members_count_insert AFTER INSERT ON list_members BEGIN '
                 'UPDATE list_runs SET member_count=member_count+1 WHERE job_id=NEW.job_id; END')
    conn.execute('CREATE TRIGGER list_members_count_delete AFTER DELETE ON list_members BEGIN '
                 'UPDATE list_runs SET member_count=member_count-1 WHERE job_id=OLD.job_id; END')
    conn.execute('CREATE TRIGGER list_members_count_reassign AFTER UPDATE OF job_id ON list_members '
                 'WHEN OLD.job_id!=NEW.job_id BEGIN '
                 'UPDATE list_runs SET member_count=member_count-1 WHERE job_id=OLD.job_id; '
                 'UPDATE list_runs SET member_count=member_count+1 WHERE job_id=NEW.job_id; END')
    conn.execute('CREATE INDEX IF NOT EXISTS pages_at ON pages(at)')
    conn.execute('CREATE INDEX IF NOT EXISTS pages_lane_at ON pages(lane, at)')
    conn.execute('CREATE INDEX IF NOT EXISTS jobs_lane ON jobs(lane) WHERE lane IS NOT NULL')
    conn.execute("CREATE INDEX IF NOT EXISTS lists_waiting_prev ON lists(prev_lane) "
                 "WHERE lane IS NULL AND prev_lane IS NOT NULL AND state IN ('queued','running')")
    conn.execute('CREATE INDEX IF NOT EXISTS edges_first_seen ON edges(first_seen)')
    conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('collector_events_started_at',?)",
                 (json.dumps(now()),))
    migrate_statuses(conn)
    if not get_setting(conn, 'owner_client_relationship_v1'):
        # Older builds stored Client only as a label. Promote it once while
        # keeping the label and every explicit relationship decision intact.
        conn.execute("INSERT INTO marks(person_id,status,note,updated_at) "
                     "SELECT DISTINCT t.person_id,'client',NULL,? FROM tags t "
                     "JOIN people p ON p.id=t.person_id WHERE t.source='manual' AND lower(trim(t.tag))='client' "
                     "ON CONFLICT(person_id) DO UPDATE SET status='client',updated_at=excluded.updated_at "
                     "WHERE coalesce(marks.status,'')=''", (now(),))
        set_setting(conn, 'owner_client_relationship_v1', True)
    if not get_setting(conn, 'owner_context_v1'):
        # Preserve historical Client labels without claiming current work or personal closeness.
        conn.execute("""INSERT OR IGNORE INTO owner_context(person_id,relationships,familiarity,updated_at)
            SELECT p.id,'["worked_with", "client"]',NULL,coalesce(m.updated_at,?) FROM people p
            LEFT JOIN marks m ON m.person_id=p.id
            WHERE m.status='client' OR EXISTS(SELECT 1 FROM tags t WHERE t.person_id=p.id
                AND t.source='manual' AND lower(trim(t.tag))='client')""", (now(),))
        set_setting(conn, 'owner_context_v1', True)

    # Before per-run evidence, every edge was treated as a current follow. Clear
    # derived claims once; keep the original edges and all human-entered data.
    if not conn.execute("SELECT 1 FROM settings WHERE key='edge_evidence_v1'").fetchone():
        conn.execute("DELETE FROM tags WHERE source='auto' AND grp='source'")
        conn.execute("UPDATE verdicts SET score=NULL,tier='unread',reason=NULL,updated_at='' "
                     "WHERE person_id IN (SELECT person_id FROM edges)")
        mark_network_dirty(conn, (r[0] for r in conn.execute("SELECT DISTINCT person_id FROM edges")))
        conn.execute("INSERT INTO settings(key,value) VALUES('edge_evidence_v1','true')")
    # Edges scraped before evidence tracking count as current until a fresh run of that list says
    # otherwise (complete_list_snapshot then marks the missing ones absent). Without this every older
    # list vanished from lead counts and the map until it was scraped again.
    if not conn.execute("SELECT 1 FROM settings WHERE key='edge_evidence_legacy_v1'").fetchone():
        conn.execute("INSERT OR IGNORE INTO edge_evidence(seed,person_id,direction,active,observed_at,checked_at) "
                     "SELECT seed,person_id,direction,1,first_seen,coalesce(first_seen,'') FROM edges")
        mark_network_dirty(conn, (r[0] for r in conn.execute("SELECT DISTINCT person_id FROM edges")))
        conn.execute("INSERT INTO settings(key,value) VALUES('edge_evidence_legacy_v1','true')")
    init_map_person_degree(conn)
    init_map_membership_revision(conn)
    # Revisions catch edits that counts/timestamps cannot distinguish, including
    # out-of-process imports. Settings is excluded to avoid recursive updates.
    conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('lead_data_rev','0')")
    add_rev_triggers(conn, ('people', 'verdicts', 'marks', 'owner_context', 'tags', 'seeds', 'edges', 'edge_evidence', 'tag_rules',
                            'followups', 'activity'))
    import owner_notes
    owner_notes.ensure(conn)
    import processing_state
    processing_state.ensure(conn)
    import external_queue
    external_queue.ensure(conn)
    import collection_suggestions
    collection_suggestions.ensure(conn)
    import processing_progress
    processing_progress.ensure(conn)
    conn.commit()
    return conn


def init_map_membership_revision(conn):
    """Keep overlap caches valid across profile edits without rescanning all edges."""
    # Bump on installation/repair too: a rebuilt membership table may differ
    # from an older cache even when its restored revision value happens to match.
    conn.execute("INSERT INTO settings(key,value) VALUES('map_membership_rev','1') "
                 "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        name = 'map_overlap_rev_' + operation.lower()
        conn.execute(f'DROP TRIGGER IF EXISTS {name}')
        when = ' WHEN OLD.person_id IS NOT NEW.person_id OR OLD.seed IS NOT NEW.seed' if operation == 'UPDATE' else ''
        conn.execute(f'CREATE TRIGGER {name} AFTER {operation} ON map_seed_member{when} BEGIN '
                     "UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='map_membership_rev'; END")


def init_map_person_degree(conn):
    """Backfill once, then keep map degrees and source handles exact.

    The ready marker and trigger installation commit together with the backfill;
    a failed migration is retried from scratch at the next initialization.
    """
    columns = {r[1] for r in conn.execute('PRAGMA table_info(map_person_degree)')}
    if 'score' not in columns:
        conn.execute('ALTER TABLE map_person_degree ADD COLUMN score REAL')
    if 'hidden' not in columns:
        conn.execute('ALTER TABLE map_person_degree ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0')
    def complete(key, pattern, count):
        row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        installed = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
                                 (pattern,)).fetchone()[0]
        return bool(row and row[0] == 'true' and installed == count)

    people_ready = complete('map_people_present_v1', 'map_people_%', 3)
    ready = complete('map_person_degree_v1', 'map_degree_%', 6) and people_ready
    if not ready:
        conn.execute('DELETE FROM map_person_degree')
        conn.execute('INSERT INTO map_person_degree(person_id,degree) '
                     'SELECT e.person_id,count(DISTINCT e.seed) FROM current_edges e '
                     'JOIN people p ON p.id=e.person_id '
                     'GROUP BY e.person_id HAVING count(DISTINCT e.seed)>0')
    rank_ready = complete('map_rank_v1', 'map_rank_%', 6)
    if not rank_ready or not ready:
        conn.execute('UPDATE map_person_degree SET '
                     'score=(SELECT score FROM verdicts WHERE person_id=map_person_degree.person_id), '
                     "hidden=EXISTS(SELECT 1 FROM marks WHERE person_id=map_person_degree.person_id AND status='no')")
    conn.execute('CREATE INDEX IF NOT EXISTS map_degree_rank ON map_person_degree(hidden,degree DESC,score DESC,person_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS map_score_rank ON map_person_degree(hidden,(score IS NULL),score DESC,degree DESC,person_id)')
    sources_ready = complete('map_source_handles_v1', 'map_sources_%', 6)
    if not sources_ready:
        conn.execute('DELETE FROM map_source_handles')
        conn.execute('INSERT INTO map_source_handles(handle,refs) '
                     'SELECT seed,count(*) FROM edges GROUP BY seed COLLATE NOCASE')
        conn.execute('INSERT INTO map_source_handles(handle,refs) SELECT handle,1 FROM seeds WHERE true '
                     'ON CONFLICT(handle) DO UPDATE SET refs=map_source_handles.refs+1')
    # Distinct current source membership is linear in the edge count. It keeps
    # source degrees exact without a source-wide rescan after each page.
    members_ready = complete('map_seed_member_v1', 'map_member_%', 6) and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_seed_degree_%'").fetchone()[0] == 2
    for name in ('map_seed_degree_insert', 'map_seed_degree_delete'):
        conn.execute(f'DROP TRIGGER IF EXISTS {name}')
    if not members_ready:
        conn.execute('DELETE FROM map_seed_member')
        conn.execute('DELETE FROM map_seed_degree')
        conn.execute('INSERT INTO map_seed_member(person_id,seed) '
                     'SELECT DISTINCT person_id,seed FROM current_edges')
        conn.execute('INSERT INTO map_seed_degree(seed,degree) '
                     'SELECT seed,count(*) FROM map_seed_member GROUP BY seed')
    conn.execute('CREATE TRIGGER map_seed_degree_insert AFTER INSERT ON map_seed_member BEGIN '
                 'INSERT INTO map_seed_degree(seed,degree) VALUES(NEW.seed,1) '
                 'ON CONFLICT(seed) DO UPDATE SET degree=degree+1; END')
    conn.execute('CREATE TRIGGER map_seed_degree_delete AFTER DELETE ON map_seed_member BEGIN '
                 'UPDATE map_seed_degree SET degree=degree-1 WHERE seed=OLD.seed; '
                 'DELETE FROM map_seed_degree WHERE seed=OLD.seed AND degree<=0; END')
    # Recreate on open, as revision triggers do. Old and new identities both
    # matter during merges, renames, and evidence rewrites.
    for table in ('edges', 'edge_evidence'):
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            name = f'map_degree_{table}_{operation.lower()}'
            conn.execute(f'DROP TRIGGER IF EXISTS {name}')
            when = ''
            if operation == 'UPDATE':
                when = (' WHEN OLD.seed IS NOT NEW.seed OR OLD.person_id IS NOT NEW.person_id '
                        'OR OLD.direction IS NOT NEW.direction' +
                        (' OR OLD.active IS NOT NEW.active' if table == 'edge_evidence' else ''))
            refs = ('OLD', 'NEW') if operation == 'UPDATE' else (('OLD',) if operation == 'DELETE' else ('NEW',))
            body = []
            for ref in refs:
                pid = f'{ref}.person_id'
                body.append(f'DELETE FROM map_person_degree WHERE person_id={pid};')
                body.append('INSERT INTO map_person_degree(person_id,degree,score,hidden) '
                            f'SELECT {pid},count(DISTINCT seed),'
                            f'(SELECT score FROM verdicts WHERE person_id={pid}),'
                            f"EXISTS(SELECT 1 FROM marks WHERE person_id={pid} AND status='no') "
                            'FROM current_edges e JOIN people p ON p.id=e.person_id '
                            f'WHERE e.person_id={pid} HAVING count(DISTINCT e.seed)>0;')
            conn.execute(f'CREATE TRIGGER {name} AFTER {operation} ON {table}{when} BEGIN {" ".join(body)} END')
            member_name = f'map_member_{table}_{operation.lower()}'
            conn.execute(f'DROP TRIGGER IF EXISTS {member_name}')
            member_body = []
            for ref in refs:
                pid, seed = f'{ref}.person_id', f'{ref}.seed'
                member_body.append(f'DELETE FROM map_seed_member WHERE person_id={pid} AND seed={seed};')
                member_body.append('INSERT INTO map_seed_member(person_id,seed) '
                                   f'SELECT {pid},{seed} WHERE EXISTS('
                                   'SELECT 1 FROM current_edges '
                                   f'WHERE person_id={pid} AND seed={seed});')
            conn.execute(f'CREATE TRIGGER {member_name} AFTER {operation} ON {table}{when} '
                         f'BEGIN {" ".join(member_body)} END')
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        name = f'map_people_{operation.lower()}'
        conn.execute(f'DROP TRIGGER IF EXISTS {name}')
        refs = ('OLD', 'NEW') if operation == 'UPDATE' else (('OLD',) if operation == 'DELETE' else ('NEW',))
        body = []
        for ref in refs:
            pid = f'{ref}.id'
            body.append(f'DELETE FROM map_person_degree WHERE person_id={pid};')
            body.append('INSERT INTO map_person_degree(person_id,degree,score,hidden) '
                        f'SELECT {pid},count(DISTINCT e.seed),'
                        f'(SELECT score FROM verdicts WHERE person_id={pid}),'
                        f"EXISTS(SELECT 1 FROM marks WHERE person_id={pid} AND status='no') "
                        'FROM current_edges e JOIN people p ON p.id=e.person_id '
                        f'WHERE e.person_id={pid} HAVING count(DISTINCT e.seed)>0;')
        when = ' WHEN OLD.id IS NOT NEW.id' if operation == 'UPDATE' else ''
        conn.execute(f'CREATE TRIGGER {name} AFTER {operation} ON people{when} BEGIN {" ".join(body)} END')
    for table, field, value in (('verdicts', 'score',
                                 '(SELECT score FROM verdicts WHERE person_id={pid})'),
                                ('marks', 'hidden',
                                 "EXISTS(SELECT 1 FROM marks WHERE person_id={pid} AND status='no')")):
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            name = f'map_rank_{table}_{operation.lower()}'
            conn.execute(f'DROP TRIGGER IF EXISTS {name}')
            refs = ('OLD', 'NEW') if operation == 'UPDATE' else (('OLD',) if operation == 'DELETE' else ('NEW',))
            body = ' '.join(f'UPDATE map_person_degree SET {field}={value.format(pid=ref + ".person_id")} '
                            f'WHERE person_id={ref}.person_id;' for ref in refs)
            when = ''
            if operation == 'UPDATE':
                when = (f' WHEN OLD.person_id IS NOT NEW.person_id OR OLD.{field if table == "verdicts" else "status"} '
                        f'IS NOT NEW.{field if table == "verdicts" else "status"}')
            conn.execute(f'CREATE TRIGGER {name} AFTER {operation} ON {table}{when} BEGIN {body} END')
    # A source remains excluded from lead dots if it is either an active seed
    # registry entry or appears in historical edges. Reference counts handle
    # rename/merge/delete without scanning all edge rows at map-read time.
    for table, column in (('edges', 'seed'), ('seeds', 'handle')):
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            name = f'map_sources_{table}_{operation.lower()}'
            conn.execute(f'DROP TRIGGER IF EXISTS {name}')
            if operation == 'INSERT':
                body = (f'INSERT INTO map_source_handles(handle,refs) VALUES(NEW.{column},1) '
                        'ON CONFLICT(handle) DO UPDATE SET refs=refs+1;')
                when = ''
            elif operation == 'DELETE':
                body = (f'UPDATE map_source_handles SET refs=refs-1 WHERE handle=OLD.{column}; '
                        f'DELETE FROM map_source_handles WHERE handle=OLD.{column} AND refs<=0;')
                when = ''
            else:
                body = (f'UPDATE map_source_handles SET refs=refs-1 WHERE handle=OLD.{column}; '
                        f'DELETE FROM map_source_handles WHERE handle=OLD.{column} AND refs<=0; '
                        f'INSERT INTO map_source_handles(handle,refs) VALUES(NEW.{column},1) '
                        'ON CONFLICT(handle) DO UPDATE SET refs=refs+1;')
                when = f' WHEN OLD.{column} IS NOT NEW.{column}'
            conn.execute(f'CREATE TRIGGER {name} AFTER {operation} ON {table}{when} BEGIN {body} END')
    conn.execute("INSERT INTO settings(key,value) VALUES('map_person_degree_v1','true') "
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value')
    conn.execute("INSERT INTO settings(key,value) VALUES('map_rank_v1','true') "
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value')
    conn.execute("INSERT INTO settings(key,value) VALUES('map_source_handles_v1','true') "
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value')
    conn.execute("INSERT INTO settings(key,value) VALUES('map_people_present_v1','true') "
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value')
    conn.execute("INSERT INTO settings(key,value) VALUES('map_seed_member_v1','true') "
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value')


_HANDLE = re.compile(r'[A-Za-z0-9._]{1,30}\Z')
_PARKED = re.compile(r'[A-Za-z0-9._]{1,30}~[A-Za-z0-9~]+\Z')
_IG_HOSTS = {'instagram.com', 'www.instagram.com', 'm.instagram.com', 'instagr.am', 'www.instagr.am'}
_RESERVED = {'p', 'reel', 'reels', 'tv', 'stories', 'explore', 'accounts', 'direct',
             'about', 'developer', '_u'}


def norm_handle(h):
    """Canonical handle, or empty text for an invalid external identity.

    Parked identities are retained for internal DB maintenance; queue_list
    applies the narrower external contract before creating work.
    """
    if not isinstance(h, str):
        return ''
    s = h.strip()
    if not s:
        return ''
    if s.startswith('@'):
        s = s[1:]
    if _PARKED.fullmatch(s):
        return s.lower()
    if s.lower() in _IG_HOSTS:
        return ''
    if '/' in s or '?' in s or '#' in s or ':' in s:
        url = s if s.lower().startswith(('http://', 'https://')) else 'https://' + s
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            if (parsed.scheme.lower() not in ('http', 'https') or host not in _IG_HOSTS
                    or parsed.username is not None or parsed.password is not None or parsed.port is not None):
                return ''
        except ValueError:
            return ''
        parts = parsed.path.split('/')
        if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2]):
            return ''
        s = unquote(parts[1])
    if not _HANDLE.fullmatch(s) or s.lower() in _RESERVED:
        return ''
    return s.lower()


# Background bookkeeping that the lead list, facets and map never show; writing it must not invalidate their cache.
REV_QUIET = {'people': {'pic_file', 'pic_refresh', 'updated_at'}, 'verdicts': {'updated_at', 'input_hash', 'prompt'}}


def add_rev_triggers(conn, tables):
    """Advance lead_data_rev on every edit the lead list, facets or map can show (the response-cache key)."""
    for table in tables:
        quiet = REV_QUIET.get(table, set())
        shown = [r[1] for r in conn.execute(f'PRAGMA table_info({table})') if r[1] not in quiet]
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            name = f'lead_rev_{table}_{operation.lower()}'
            when = f"UPDATE OF {','.join(shown)}" if operation == 'UPDATE' and quiet else operation
            conn.execute(f'DROP TRIGGER IF EXISTS {name}')   # redefine: columns may have been added
            conn.execute(f"CREATE TRIGGER {name} AFTER {when} ON {table} BEGIN "
                         "INSERT INTO settings(key,value) VALUES('lead_data_rev','1') "
                         "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1; END")


def mark_network_dirty(conn, person_ids):
    """Durable local rerank work. Caller commits; consumers acknowledge exact revisions."""
    ids = sorted(set(person_ids))
    if not ids:
        return None
    # The write takes SQLite's writer lock before reading the shared sequence.
    conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('network_change_id','0')")
    conn.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='network_change_id'")
    revision = int(conn.execute("SELECT value FROM settings WHERE key='network_change_id'").fetchone()[0])
    conn.executemany('INSERT INTO network_dirty VALUES(?,?) ON CONFLICT(person_id) DO UPDATE SET change_id=excluded.change_id',
                     ((pid, revision) for pid in ids))
    return revision


def dirty_seed_members(conn, *handles):
    ids = set()
    for handle in handles:
        ids.update(r[0] for r in conn.execute('SELECT person_id FROM edges WHERE seed=?', (handle,)))
        ids.update(r[0] for r in conn.execute('SELECT id FROM people WHERE handle=?', (handle,)))
    mark_network_dirty(conn, ids)


def move_seed(conn, old, new):
    """Move one proven seed identity and retain its list/job history."""
    dirty_seed_members(conn, old, new)
    conn.execute('UPDATE seeds SET handle=? WHERE handle=?', (new, old))
    conn.execute('UPDATE lists SET seed=? WHERE seed=?', (new, old))
    conn.execute('UPDATE edges SET seed=? WHERE seed=?', (new, old))
    conn.execute('UPDATE edge_evidence SET seed=? WHERE seed=?', (new, old))
    conn.execute('UPDATE jobs SET seed=? WHERE lower(seed)=lower(?)', (new, old))
    conn.execute('UPDATE jobs SET handle=? WHERE lower(handle)=lower(?)', (new, old))
    # A callback leased under the previous handle must not write to the new holder.
    conn.execute("UPDATE jobs SET state='queued',leased_until=NULL,lane=NULL,lease_token=NULL "
                 "WHERE state='leased' AND (seed=? OR handle=?)", (new, new))
    retries = get_setting(conn, 'lists_reopened') or {}
    for direction in ('followers', 'following'):
        key = f'{old}|{direction}'
        if key in retries:
            retries[f'{new}|{direction}'] = retries.pop(key)
    if retries:
        set_setting(conn, 'lists_reopened', retries)


def vacant_handle(conn, handle, suffix):
    candidate = f'{handle}~{suffix}'
    while conn.execute('SELECT 1 FROM people WHERE handle=? UNION ALL SELECT 1 FROM seeds WHERE handle=?',
                       (candidate, candidate)).fetchone():
        candidate += '~'
    return candidate


def rename_seed(conn, old, new, ig_id):
    dirty_seed_members(conn, old, new)
    seed = conn.execute('SELECT * FROM seeds WHERE handle=?', (old,)).fetchone()
    if not seed or (normalize_ig_id(seed['ig_id']) and normalize_ig_id(seed['ig_id']) != ig_id):
        # Profile work follows a proven person identity even without a seed.
        # Revoke any old lease before this handle can belong to someone else.
        conn.execute("UPDATE jobs SET handle=?,state=CASE WHEN state='leased' THEN 'queued' ELSE state END, "
                     "leased_until=NULL,lane=NULL,lease_token=NULL WHERE kind='profile' AND lower(handle)=lower(?)",
                     (new, old))
        return
    target = conn.execute('SELECT * FROM seeds WHERE handle=?', (new,)).fetchone()
    if target and target['ig_id'] == ig_id:
        # Both seed records have identity proof. Keep the newest list state and
        # every historical job/edge; retire a duplicate live run if necessary.
        for source_list in conn.execute('SELECT * FROM lists WHERE seed=?', (old,)).fetchall():
            dest_list = conn.execute('SELECT * FROM lists WHERE seed=? AND direction=?',
                                     (new, source_list['direction'])).fetchone()
            if dest_list:
                winner = source_list if (source_list['updated_at'] or '') > (dest_list['updated_at'] or '') else dest_list
                loser = dest_list if winner is source_list else source_list
                conn.execute('DELETE FROM lists WHERE seed=? AND direction=?', (loser['seed'], loser['direction']))
                conn.execute("UPDATE jobs SET state='cancelled',leased_until=NULL,lane=NULL,lease_token=NULL "
                             "WHERE seed=? AND direction=? AND kind='list' AND state IN ('queued','leased')",
                             (loser['seed'], loser['direction']))
        for edge in conn.execute('SELECT * FROM edges WHERE seed=?', (old,)).fetchall():
            conn.execute('INSERT INTO edges VALUES(?,?,?,?) ON CONFLICT(seed,person_id,direction) DO UPDATE '
                         'SET first_seen=min(edges.first_seen,excluded.first_seen)',
                         (new, edge['person_id'], edge['direction'], edge['first_seen']))
        conn.execute('DELETE FROM edges WHERE seed=?', (old,))
        for ev in conn.execute('SELECT * FROM edge_evidence WHERE seed=?', (old,)).fetchall():
            conn.execute('INSERT INTO edge_evidence VALUES(?,?,?,?,?,?) ON CONFLICT(seed,person_id,direction) DO UPDATE SET '
                         'active=CASE WHEN excluded.checked_at>edge_evidence.checked_at THEN excluded.active ELSE edge_evidence.active END, '
                         'observed_at=nullif(max(coalesce(edge_evidence.observed_at,\'\'),coalesce(excluded.observed_at,\'\')),\'\'), '
                         'checked_at=max(edge_evidence.checked_at,excluded.checked_at)',
                         (new, ev['person_id'], ev['direction'], ev['active'], ev['observed_at'], ev['checked_at']))
        conn.execute('DELETE FROM edge_evidence WHERE seed=?', (old,))
        conn.execute('UPDATE seeds SET is_me=max(is_me,?),added_at=min(coalesce(added_at,?),coalesce(?,added_at)) WHERE handle=?',
                     (seed['is_me'], seed['added_at'], seed['added_at'], new))
        conn.execute('DELETE FROM seeds WHERE handle=?', (old,))
        move_seed(conn, old, new)
        return
    if target:
        # Even an unverified destination may describe another holder. Keep it separate.
        parked = vacant_handle(conn, new, target['ig_id'] or 'seed')
        move_seed(conn, new, parked)
        conn.execute("UPDATE jobs SET state='cancelled',leased_until=NULL,lane=NULL,lease_token=NULL "
                     "WHERE (seed=? OR handle=?) AND state IN ('queued','leased')", (parked, parked))
    move_seed(conn, old, new)
    conn.execute('UPDATE seeds SET ig_id=? WHERE handle=?', (ig_id, new))


def park_person(conn, row):
    old = conn.execute('SELECT handle FROM people WHERE id=?', (row['id'],)).fetchone()[0]
    parked = vacant_handle(conn, old, row['id'])
    rename_seed(conn, old, parked, row['ig_id'])
    conn.execute('UPDATE people SET handle=? WHERE id=?', (parked, row['id']))
    conn.execute("UPDATE jobs SET state='cancelled',leased_until=NULL,lane=NULL,lease_token=NULL "
                 "WHERE (seed=? OR handle=?) AND state IN ('queued','leased')", (parked, parked))


def normalize_ig_id(value):
    """Blank external IDs are missing evidence, never a replacement identity."""
    return (str(value).strip() or None) if value is not None else None


def _preserve_seed_identity(conn, person):
    """Keep handle-keyed history attached to its known owner before a rename.

    An already identified seed can disagree with the current handle holder. Keep
    that evidence intact; only fill absent IDs, including legacy blank values.
    """
    ig_id = normalize_ig_id(person['ig_id'])
    if ig_id:
        seed = conn.execute('SELECT ig_id FROM seeds WHERE handle=?', (person['handle'],)).fetchone()
        if seed is not None and normalize_ig_id(seed['ig_id']) is None:
            conn.execute('UPDATE seeds SET ig_id=? WHERE handle=? AND ig_id IS ?',
                         (ig_id, person['handle'], seed['ig_id']))


def upsert_person(conn, u, ts=None):
    ts = ts or now()
    vals = {k: u[k] for k in PERSON_FIELDS if u.get(k) is not None}
    for k in ('is_private', 'is_verified', 'is_business'):
        if k in vals:
            vals[k] = int(bool(vals[k]))
    if 'ig_id' in vals:
        ig_id = normalize_ig_id(vals['ig_id'])
        if ig_id is None:
            del vals['ig_id']
        else:
            vals['ig_id'] = ig_id
    if 'handle' in vals:
        vals['handle'] = norm_handle(vals['handle'])
        if not vals['handle']:
            raise ValueError('invalid Instagram handle')
    by_id = vals.get('ig_id') and conn.execute('SELECT id, handle, updated_at FROM people WHERE ig_id=?', (vals['ig_id'],)).fetchone()
    if by_id and ts <= by_id['updated_at']:
        vals.pop('handle', None)  # an older observation cannot undo a known rename
    by_handle = vals.get('handle') and conn.execute('SELECT id, ig_id, handle, updated_at FROM people WHERE handle=?', (vals['handle'],)).fetchone()
    if by_handle and normalize_ig_id(by_handle['ig_id']) is None and vals.get('ig_id'):
        seed = conn.execute('SELECT ig_id FROM seeds WHERE handle=?', (by_handle['handle'],)).fetchone()
        seed_id = normalize_ig_id(seed['ig_id']) if seed is not None else None
        if seed_id and seed_id != vals['ig_id']:
            raise ValueError('seed account identity conflicts with incoming profile')
    if (by_handle and by_handle['ig_id'] and vals.get('ig_id')
            and by_handle['ig_id'] != vals['ig_id'] and ts <= by_handle['updated_at']):
        # A historical or tied claim cannot displace a newer proven owner.
        # Keep an existing identity at its known handle, or retain a newly
        # discovered historical identity under a separate parked handle.
        if by_id:
            vals.pop('handle', None)
        else:
            vals['handle'] = vacant_handle(conn, vals['handle'], vals['ig_id'])
        by_handle = None
    if not by_id and by_handle and by_handle['ig_id'] and vals.get('ig_id') and by_handle['ig_id'] != vals['ig_id']:
        # a different account now holds this handle: the old row keeps its marks/edges/tags under a parked handle
        park_person(conn, by_handle)
        by_handle = None
    if by_id and by_handle and by_id['id'] != by_handle['id']:
        if by_handle['ig_id']:  # the handle now belongs to this ig_id; the old holder renamed
            park_person(conn, by_handle)
        else:  # same account seen before without ig_id: fold it in
            merge_people(conn, keep=by_id['id'], drop=by_handle['id'])
    row = by_id or by_handle
    if row:
        pid = row['id']
        current = conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()
        if by_id and vals.get('handle') and current['handle'] != vals['handle']:
            rename_seed(conn, current['handle'], vals['handle'], vals['ig_id'])
        if current['bio_at'] and (vals.get('bio_at') or ts) < current['bio_at']:
            vals = {k: v for k, v in vals.items() if k not in BIO_FIELDS}
        if ts < current['updated_at']:
            vals = {k: v for k, v in vals.items() if k in BIO_FIELDS or k == 'ig_id' or current[k] is None}
        changes = {k: v for k, v in vals.items() if current[k] != v}
        if changes:
            # A successful reread can refresh the observation time/source without
            # changing the profile that qualification consumes. Preserve that
            # freshness, but do not schedule another rules pass for the same data.
            content_changed = any(k not in ('bio_at', 'bio_src') for k in changes)
            if 'pic_url' in changes:
                # CDN URLs rotate independently of the photo. Keep the last
                # downloaded image visible while a refresh waits for network access.
                changes['pic_refresh'] = int(bool(current['pic_file']))
                if not current['pic_file']:
                    changes['pic_file'] = None
            if content_changed:
                changes['updated_at'] = max(ts, current['updated_at'])
            conn.execute(f"UPDATE people SET {', '.join(k + '=?' for k in changes)} WHERE id=?",
                         (*changes.values(), pid))
        return pid
    cols = list(vals) + ['first_seen', 'updated_at']
    return conn.execute(f"INSERT INTO people({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                        (*vals.values(), ts, ts)).lastrowid


def _earliest_observed(left, right):
    """Prefer the earliest trustworthy instant over invalid/naive/future dates.

    If neither date establishes an instant, retain a deterministic source value
    for legacy compatibility. It remains unknown to evidence readers. Two
    missing values stay NULL; ledger callers use '' for their NOT NULL column.
    """
    valid = []
    cutoff = utc_now()
    for value in (left, right):
        if not isinstance(value, str):
            continue
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if dt.tzinfo is not None and dt <= cutoff:
                valid.append(dt.astimezone(timezone.utc))
        except (ValueError, OverflowError):
            continue
    if valid:
        return min(valid).isoformat()
    unknown = [value for value in (left, right) if isinstance(value, str) and value]
    return min(unknown) if unknown else None


def merge_people(conn, keep, drop):
    import workflows
    if keep == drop:
        return
    # Leave committing to the caller, including when called outside an existing transaction.
    if not conn.in_transaction:
        conn.execute('BEGIN')
    conn.execute('SAVEPOINT merge_people')
    try:
        if conn.execute('SELECT count(*) FROM people WHERE id IN (?,?)', (keep, drop)).fetchone()[0] != 2:
            raise ValueError('both people must exist before merging')
        pa = conn.execute('SELECT * FROM people WHERE id=?', (keep,)).fetchone()
        pb = conn.execute('SELECT * FROM people WHERE id=?', (drop,)).fetchone()
        affected_seeds = {r[0] for r in conn.execute('SELECT DISTINCT seed FROM edges WHERE person_id IN (?,?)', (keep, drop))}
        dirty_seed_members(conn, *affected_seeds)
        # Fold profile fields: the newer bio wins; other fields fill gaps on the survivor.
        fields = {}
        newer_bio = pb['bio_at'] and (not pa['bio_at'] or pb['bio_at'] > pa['bio_at'])
        for key in pa.keys():
            if key in ('id', 'ig_id', 'handle', 'first_seen', 'updated_at'):
                continue
            take = (newer_bio or not pa['bio_at']) if key in BIO_FIELDS else (pa[key] is None or pa[key] == '')
            if pb[key] is not None and take:
                fields[key] = pb[key]
        fields['first_seen'] = min(pa['first_seen'], pb['first_seen'])
        fields['updated_at'] = max(pa['updated_at'], pb['updated_at'])
        conn.execute(f"UPDATE people SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), keep))
        conn.execute('UPDATE activity SET person_id=? WHERE person_id=?', (keep, drop))
        kept, dropped = workflows.follow_up(conn, keep), workflows.follow_up(conn, drop)
        if dropped:
            # Keep an open reminder over completed; then earliest due. Record both snapshots on conflict.
            chosen = min([x for x in (kept, dropped) if x], key=lambda x: (x['completed_at'] is not None, x['due_on']))
            conn.execute('INSERT OR REPLACE INTO followups VALUES(?,?,?,?,?)', (keep, chosen['due_on'], chosen['note'], chosen['completed_at'], chosen['updated_at']))
            conn.execute('DELETE FROM followups WHERE person_id=?', (drop,))
            if kept:
                workflows.event(conn, keep, 'follow_up_merged', before={'kept': kept, 'merged': dropped}, after=chosen)

        # Human relationship history survives identity consolidation independently of outreach stage.
        for legacy_pid in (keep, drop):
            if conn.execute("SELECT 1 FROM marks WHERE person_id=? AND status='client' UNION SELECT 1 FROM tags WHERE person_id=? AND source='manual' AND lower(trim(tag))='client'", (legacy_pid, legacy_pid)).fetchone():
                context = conn.execute('SELECT * FROM owner_context WHERE person_id=?', (legacy_pid,)).fetchone()
                values = set(json.loads(context['relationships']) if context else []) | {'client', 'worked_with'}
                conn.execute('INSERT OR REPLACE INTO owner_context VALUES(?,?,?,?)',
                             (legacy_pid, json.dumps(sorted(values)), context['familiarity'] if context else None,
                              context['updated_at'] if context else now()))
        contexts = conn.execute('SELECT * FROM owner_context WHERE person_id IN (?,?) ORDER BY updated_at DESC', (keep, drop)).fetchall()
        if contexts:
            relations = sorted({value for r in contexts for value in json.loads(r['relationships'])})
            familiarity = next((r['familiarity'] for r in contexts if r['familiarity']), None)
            conn.execute('INSERT OR REPLACE INTO owner_context VALUES(?,?,?,?)',
                         (keep, json.dumps(relations), familiarity, contexts[0]['updated_at']))
            conn.execute('DELETE FROM owner_context WHERE person_id=?', (drop,))

        km = conn.execute('SELECT * FROM marks WHERE person_id=?', (keep,)).fetchone()
        dm = conn.execute('SELECT * FROM marks WHERE person_id=?', (drop,)).fetchone()
        before, after = {}, {}
        if km and dm:
            # The newer explicit status wins; a note-only duplicate carries no status decision.
            latest = dm if (dm['updated_at'] or '') > (km['updated_at'] or '') else km
            other = km if latest is dm else dm
            status = latest['status'] if latest['status'] is not None else other['status']
            notes = list(dict.fromkeys(x for x in (km['note'], dm['note']) if x))
            combined = '\n\n'.join(notes) or None
            # Keep the editable note within its limit; the full dropped note stays in history.
            note = combined if combined is None or len(combined) <= 5000 else km['note']
            before, after = dict(dm), {'status': status, 'note': note}
            conn.execute('UPDATE marks SET status=?,note=?,updated_at=? WHERE person_id=?',
                         (status, note, latest['updated_at'], keep))

        # Derived scores belong to their profile inputs. Keep the survivor's result.
        # Website reads instead keep the newest useful evidence, ahead of empty failures.
        def site_priority(row):
            try:
                signals = json.loads(row['signals'] or '{}')
            except (ValueError, TypeError):
                signals = {}
            useful = bool(row['title'] or row['summary'] or isinstance(signals, dict) and signals or not row['error'])
            try:
                stamp = datetime.fromisoformat((row['at'] or '').replace('Z', '+00:00'))
                stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
            except (ValueError, TypeError):
                stamp = datetime.min.replace(tzinfo=timezone.utc)
            return useful, stamp

        # Website reads are optional tables, created lazily by qual_api.
        optional = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('site_reads','site_evidence')")}
        keep_had_site = 'site_reads' in optional and \
            conn.execute('SELECT 1 FROM site_reads WHERE person_id=?', (keep,)).fetchone()
        conflicts, chosen_records = {}, {}
        singletons = ['verdicts', 'laya']
        if 'site_reads' in optional:
            singletons.append('site_reads')
        for table in singletons:
            kept = conn.execute(f'SELECT * FROM {table} WHERE person_id=?', (keep,)).fetchone()
            dropped = conn.execute(f'SELECT * FROM {table} WHERE person_id=?', (drop,)).fetchone()
            if kept and dropped:
                if any(kept[key] != dropped[key] for key in kept.keys() if key != 'person_id'):
                    chosen = kept
                    if table == 'site_reads' and site_priority(dropped) > site_priority(kept):
                        chosen = dropped
                        columns = tuple(key for key in
                                        ('url', 'final_url', 'title', 'summary', 'signals', 'error', 'model', 'at', 'content_hash')
                                        if key in dropped.keys())
                        conn.execute('UPDATE site_reads SET ' + ','.join(key + '=?' for key in columns) + ' WHERE person_id=?',
                                     (*[dropped[key] for key in columns], keep))
                        # Keep site evidence paired with the chosen read.
                        if 'site_evidence' in optional:
                            conn.execute('DELETE FROM site_evidence WHERE person_id=?', (keep,))
                            conn.execute('UPDATE site_evidence SET person_id=? WHERE person_id=?', (keep, drop))
                    conflicts[table] = {'kept': dict(kept), 'merged': dict(dropped)}
                    chosen_records[table] = dict(chosen, person_id=keep)
            conn.execute(f'UPDATE OR IGNORE {table} SET person_id=? WHERE person_id=?', (keep, drop))
            conn.execute(f'DELETE FROM {table} WHERE person_id=?', (drop,))

        for dropped in conn.execute('SELECT * FROM tags WHERE person_id=?', (drop,)).fetchall():
            kept = conn.execute('SELECT * FROM tags WHERE person_id=? AND tag=?', (keep, dropped['tag'])).fetchone()
            if kept and (kept['grp'], kept['source']) != (dropped['grp'], dropped['source']):
                chosen = dropped if dropped['source'] == 'manual' and kept['source'] != 'manual' else kept
                conflicts.setdefault('tags', []).append({'kept': dict(kept), 'merged': dict(dropped)})
                chosen_records.setdefault('tags', []).append(dict(chosen, person_id=keep))
                if chosen is dropped:
                    conn.execute('UPDATE tags SET grp=?,source=? WHERE person_id=? AND tag=?',
                                 (dropped['grp'], dropped['source'], keep, dropped['tag']))

        # Retain the earliest edge and every distinct page observation when identities merge.
        conn.create_function('earliest_observed', 2, _earliest_observed)
        conn.execute('INSERT INTO edges(seed,person_id,direction,first_seen) '
                     'SELECT seed,?,direction,first_seen FROM edges WHERE person_id=? '
                     'ON CONFLICT(seed,person_id,direction) DO UPDATE SET first_seen='
                     'earliest_observed(edges.first_seen,excluded.first_seen)',
                     (keep, drop))
        conn.execute('DELETE FROM edges WHERE person_id=?', (drop,))
        conn.execute('INSERT INTO edge_observations(seed,person_id,direction,page_key,job_id,observed_at) '
                     'SELECT seed,?,direction,page_key,job_id,observed_at FROM edge_observations WHERE person_id=? '
                     'ON CONFLICT(seed,person_id,direction,page_key) DO UPDATE SET '
                     # The ledger's NOT NULL date uses an empty string for unknown.
                     "observed_at=coalesce(earliest_observed(edge_observations.observed_at,excluded.observed_at),'')", (keep, drop))
        conn.execute('DELETE FROM edge_observations WHERE person_id=?', (drop,))
        for ev in conn.execute('SELECT * FROM edge_evidence WHERE person_id=?', (drop,)).fetchall():
            conn.execute('INSERT INTO edge_evidence VALUES(?,?,?,?,?,?) ON CONFLICT(seed,person_id,direction) DO UPDATE SET '
                         'active=CASE WHEN excluded.checked_at>edge_evidence.checked_at THEN excluded.active ELSE edge_evidence.active END, '
                         'observed_at=nullif(max(coalesce(edge_evidence.observed_at,\'\'),coalesce(excluded.observed_at,\'\')),\'\'), '
                         'checked_at=max(edge_evidence.checked_at,excluded.checked_at)',
                         (ev['seed'], keep, ev['direction'], ev['active'], ev['observed_at'], ev['checked_at']))
        conn.execute('DELETE FROM edge_evidence WHERE person_id=?', (drop,))
        conn.execute('INSERT INTO list_members SELECT job_id, ?, observed_at FROM list_members WHERE person_id=? '
                     'ON CONFLICT(job_id,person_id) DO UPDATE SET observed_at=max(list_members.observed_at,excluded.observed_at)', (keep, drop))
        conn.execute('DELETE FROM list_members WHERE person_id=?', (drop,))
        if 'site_evidence' in optional:
            if not keep_had_site:   # the survivor took the duplicate's only read: bring its evidence along
                conn.execute('UPDATE site_evidence SET person_id=? WHERE person_id=?', (keep, drop))
            conn.execute('DELETE FROM site_evidence WHERE person_id=?', (drop,))
        for table in ('tags', 'marks'):
            conn.execute(f'UPDATE OR IGNORE {table} SET person_id=? WHERE person_id=?', (keep, drop))
            conn.execute(f'DELETE FROM {table} WHERE person_id=?', (drop,))
        if conflicts:
            before['records'], after['records'] = conflicts, chosen_records
        if before:
            workflows.event(conn, keep, 'identity_merged', before=before, after=after)
        conn.execute('DELETE FROM network_dirty WHERE person_id=?', (drop,))
        mark_network_dirty(conn, [keep])
        import note_mentions
        note_mentions.merge(conn, keep, drop)
        # people.id may be reused after deleting the highest rowid. Move audit
        # history and invalidate input-bound caches before that identity vanishes.
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('processing_review_events', 'ai_scoring_events'):
            if table in tables:
                conn.execute(f'UPDATE {table} SET person_id=? WHERE person_id=?', (keep, drop))
        if 'processing_ai_history' in tables:
            conn.execute('''UPDATE OR IGNORE processing_ai_history
                SET person_id=?,verdict=json_set(verdict,'$.person_id',?) WHERE person_id=?''',
                (keep, keep, drop))
            # A colliding archived result is already retained on the survivor.
            conn.execute('DELETE FROM processing_ai_history WHERE person_id=?', (drop,))
            conn.execute('''DELETE FROM processing_ai_history AS h WHERE person_id=? AND id NOT IN (
                SELECT recent.id FROM processing_ai_history recent
                WHERE recent.person_id=h.person_id AND recent.model=h.model
                ORDER BY recent.id DESC LIMIT 2)''', (keep,))
        for table in ('external_attempts', 'web_research', 'owner_note_reads'):
            if table in tables:
                conn.execute(f'DELETE FROM {table} WHERE person_id=?', (drop,))
        conn.execute('DELETE FROM people WHERE id=?', (drop,))
    except Exception:
        conn.execute('ROLLBACK TO merge_people')
        conn.execute('RELEASE merge_people')
        raise
    conn.execute('RELEASE merge_people')


def add_edge(conn, seed, person_id, direction, ts=None, observed=True, flipped=None):
    """flipped: a set that collects seeds whose membership changed, so a caller can re-rank each seed once."""
    seed, ts = norm_handle(seed), ts or now()
    previous = conn.execute('SELECT active FROM edge_evidence WHERE seed=? AND person_id=? AND direction=?',
                            (seed, person_id, direction)).fetchone()
    added = conn.execute('INSERT OR IGNORE INTO edges VALUES(?,?,?,?)',
                         (seed, person_id, direction, ts)).rowcount == 1
    if observed:
        conn.execute('INSERT INTO edge_evidence VALUES(?,?,?,1,?,?) ON CONFLICT(seed,person_id,direction) DO UPDATE SET '
                     'active=CASE WHEN excluded.checked_at>=edge_evidence.checked_at THEN 1 ELSE edge_evidence.active END, '
                     'observed_at=max(coalesce(edge_evidence.observed_at,\'\'),excluded.observed_at), '
                     'checked_at=max(edge_evidence.checked_at,excluded.checked_at)',
                     (seed, person_id, direction, ts, ts))
    current = conn.execute('SELECT active FROM edge_evidence WHERE seed=? AND person_id=? AND direction=?',
                           (seed, person_id, direction)).fetchone()
    if bool(previous and previous[0]) != bool(current and current[0]):
        if flipped is None:
            dirty_seed_members(conn, seed)
        else:
            flipped.add(seed)
    return added


def list_run_complete(conn, job_id):
    """Require a complete cursor chain and a fresh count for negative evidence."""
    run = conn.execute('SELECT * FROM list_runs WHERE job_id=?', (job_id,)).fetchone()
    if (not run or not run['first_page_seen'] or run['total_source'] != 'current_run'
            or type(run['total']) is not int or run['total'] < 0):
        return False
    pages = dict(conn.execute('SELECT requested_cursor,next_cursor FROM list_page_requests WHERE job_id=?', (job_id,)))
    cursor, seen = '', set()
    while cursor is not None:
        if cursor not in pages or cursor in seen:
            return False
        seen.add(cursor)
        cursor = pages[cursor]
    return len(seen) == len(pages) and run['member_count'] >= run['total']


def complete_list_snapshot(conn, seed, direction, job_id, completed_at=None):
    """Record absence only for a complete, tracked run. Call after updating lists, before commit.

    Historical edges are retained. An open/partial/legacy run cannot disprove them.
    Returns person IDs whose effective relationship changed.
    """
    seed = norm_handle(seed)
    row = conn.execute('SELECT state,run_job_id,cursor,received,total FROM lists WHERE seed=? AND direction=?',
                       (seed, direction)).fetchone()
    job = conn.execute('SELECT kind,seed,direction,state FROM jobs WHERE id=?', (job_id,)).fetchone()
    run = conn.execute('SELECT member_count FROM list_runs WHERE job_id=?', (job_id,)).fetchone()
    if (not row or not run or row['state'] != 'done' or row['run_job_id'] != job_id or row['cursor'] is not None
            or not job or job['kind'] != 'list' or job['state'] != 'done' or job['seed'] != seed or job['direction'] != direction
            or row['received'] != run['member_count']):
        return set()
    ts = completed_at or now()
    before = {r[0] for r in conn.execute('SELECT person_id FROM current_edges WHERE seed=? AND direction=?',
                                          (seed, direction))}
    # Every returned member is positive evidence, including pages copied during a cursor resume.
    conn.execute('INSERT INTO edge_evidence(seed,person_id,direction,active,observed_at,checked_at) '
                 'SELECT ?,m.person_id,?,1,m.observed_at,m.observed_at FROM list_members m WHERE m.job_id=? '
                 'ON CONFLICT(seed,person_id,direction) DO UPDATE SET active=1, '
                 'observed_at=max(coalesce(edge_evidence.observed_at,\'\'),excluded.observed_at),checked_at=excluded.checked_at '
                 'WHERE excluded.checked_at>=edge_evidence.checked_at',
                 (seed, direction, job_id))
    if not list_run_complete(conn, job_id):
        after = {r[0] for r in conn.execute('SELECT person_id FROM current_edges WHERE seed=? AND direction=?',
                                             (seed, direction))}
        if before != after:
            dirty_seed_members(conn, seed)
        return before ^ after
    conn.execute('INSERT INTO edge_evidence(seed,person_id,direction,active,observed_at,checked_at) '
                 'SELECT e.seed,e.person_id,e.direction,0,NULL,? FROM edges e '
                 'WHERE e.seed=? AND e.direction=? AND NOT EXISTS '
                 '(SELECT 1 FROM list_members m WHERE m.job_id=? AND m.person_id=e.person_id) '
                 'ON CONFLICT(seed,person_id,direction) DO UPDATE SET active=0,checked_at=excluded.checked_at '
                 'WHERE excluded.checked_at>=edge_evidence.checked_at',
                 (ts, seed, direction, job_id))
    after = {r[0] for r in conn.execute('SELECT person_id FROM current_edges WHERE seed=? AND direction=?',
                                         (seed, direction))}
    if before != after:
        dirty_seed_members(conn, seed)
    return before ^ after


def list_page_key(seed, direction, users, cursor, job_id=None, requested_cursor=None):
    """Replay identity, not proof of a complete snapshot or Instagram event time.

    Unmanaged imports have no collection run identifier. Deduplicate the same member
    batch conservatively, ignoring order and mutable profile fields. A genuine new
    observation of an identical batch requires a new managed collection job.
    """
    if job_id is not None:
        # The output cursor may repeat when Instagram stalls. The request cursor
        # still identifies the distinct page we saved before parking the run.
        return (f'job:{job_id}:request:{requested_cursor}' if requested_cursor is not None
                else f'job:{job_id}:next:{cursor or ""}')
    members = sorted({('id:' + ig_id) if (ig_id := normalize_ig_id(u.get('ig_id'))) is not None
                      else ('handle:' + norm_handle(u['handle'])) for u in users})
    payload = json.dumps([norm_handle(seed), direction, cursor or '', members], separators=(',', ':'))
    return 'import:' + hashlib.sha256(payload.encode()).hexdigest()


def observe_edge(conn, seed, person_id, direction, page_key, job_id, ts):
    return conn.execute('INSERT OR IGNORE INTO edge_observations'
                        '(seed,person_id,direction,page_key,job_id,observed_at) VALUES(?,?,?,?,?,?)',
                        (norm_handle(seed), person_id, direction, page_key, job_id, ts)).rowcount == 1


def get_setting(conn, key, default=None):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    # a copy: callers mutate dicts (budget.update) and must never change the shared defaults
    return json.loads(row[0] if row else json.dumps(DEFAULTS.get(key, default)))


def set_setting(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))


def start_list_run(conn, job_id, seed, direction):
    """Attach a run, carrying tracked prefixes only when continuing a saved cursor."""
    if conn.execute('SELECT 1 FROM list_runs WHERE job_id=?', (job_id,)).fetchone():
        return
    old = conn.execute('SELECT cursor,run_job_id FROM lists WHERE seed=? AND direction=?',
                       (seed, direction)).fetchone()
    if old and old['cursor'] and old['run_job_id'] is not None and old['run_job_id'] != job_id:
        prior = old['run_job_id']
        conn.execute('INSERT OR IGNORE INTO list_runs(job_id,first_page_seen,total,total_source) '
                     'SELECT ?,first_page_seen,total,total_source FROM list_runs WHERE job_id=?',
                     (job_id, prior))
        conn.execute('INSERT OR IGNORE INTO list_members SELECT ?,person_id,observed_at FROM list_members WHERE job_id=?',
                     (job_id, prior))
        conn.execute('INSERT OR IGNORE INTO list_page_requests SELECT ?,requested_cursor,next_cursor FROM list_page_requests WHERE job_id=?',
                     (job_id, prior))
    conn.execute('INSERT OR IGNORE INTO list_runs(job_id,member_count) VALUES(?, '
                 '(SELECT count(*) FROM list_members WHERE job_id=?))', (job_id, job_id))
    # Do not count historical edges as a tracked prefix. Legacy received remains visible
    # until a page arrives, but can never certify completion.
    if old and old['cursor']:
        conn.execute('UPDATE lists SET run_job_id=? WHERE seed=? AND direction=?', (job_id, seed, direction))


def queue_list(conn, seed, direction, priority=0, refresh=False, page_size=None):
    seed = norm_handle(seed)
    if not seed or '~' in seed:
        raise ValueError('invalid Instagram seed handle')
    if direction not in ('followers', 'following'):
        raise ValueError('invalid list direction')
    if page_size is not None:
        if type(page_size) is not int or page_size not in (25, 50) or direction != 'followers' or refresh:
            raise ValueError('experimental page size requires a fresh follower list')
        if (conn.execute('SELECT 1 FROM seeds WHERE handle=?', (seed,)).fetchone()
                or conn.execute('SELECT 1 FROM lists WHERE seed=? AND direction=?', (seed, direction)).fetchone()
                or conn.execute('SELECT 1 FROM jobs WHERE seed=? AND direction=?', (seed, direction)).fetchone()
                or conn.execute('SELECT 1 FROM edges WHERE seed=? LIMIT 1', (seed,)).fetchone()):
            raise ValueError('experimental page size requires a new seed')
    ts = now()
    conn.execute('INSERT OR IGNORE INTO seeds(handle, added_at) VALUES(?,?)', (seed, ts))
    row = conn.execute('SELECT state FROM lists WHERE seed=? AND direction=?', (seed, direction)).fetchone()
    if conn.execute("SELECT 1 FROM jobs WHERE kind='list' AND seed=? AND direction=? AND state IN ('queued','leased')",
                    (seed, direction)).fetchone():
        return False  # duplicate requests never rewind or relabel work already in progress
    if refresh:
        conn.execute('DELETE FROM list_private_denials WHERE seed=? AND direction=?', (seed, direction))
    if row and row['state'] in ('done', 'private') and not refresh:
        return False
    if row and (refresh or row['state'] == 'partial'):
        conn.execute('UPDATE lists SET cursor=NULL, run_job_id=NULL, received=0, lane=NULL WHERE seed=? AND direction=?', (seed, direction))
    conn.execute('INSERT INTO lists(seed, direction, state, updated_at) VALUES(?,?,?,?) '
                 "ON CONFLICT DO UPDATE SET state='queued', error=NULL, updated_at=excluded.updated_at",
                 (seed, direction, 'queued', ts))
    if not conn.execute("SELECT 1 FROM jobs WHERE kind='list' AND seed=? AND direction=? AND state IN ('queued','leased')",
                        (seed, direction)).fetchone():
        job_id = conn.execute('INSERT INTO jobs(kind, seed, direction, priority, created_at, page_size) VALUES(?,?,?,?,?,?)',
                              ('list', seed, direction, priority, ts, page_size)).lastrowid
        start_list_run(conn, job_id, seed, direction)
    return True


REOPEN_MAX = 2          # bound automatic retries of unverified/short lists
SHORT_RATIO = 0.95      # done with received below this share of the known total = ended early
SHORT_MIN = 20          # ... and at least this many people missing
PARTIAL_RETRY_DELAY = timedelta(days=1)
PARTIAL_RETRY_BATCH = 2  # repair runs every 15 minutes; do not flood Instagram with old partials


def repair_lists(conn, dry=False):
    """Make sure every seed has both lists queued until they are really complete. Returns counts; caller commits.
    - a seed without a followers/following list row gets one (queued);
    - a list in state paused/error/queued/running without a live job gets a job again (cursor kept: it resumes);
    - a list marked done without a complete tracked run is reopened from the start;
    - recoverable terminal partials wait a day and retry in small batches, at most
      REOPEN_MAX times. Explicit Instagram caps and access denials stay parked.
    Cached profile totals never certify coverage."""
    ts = now()
    out = {'added': 0, 'requeued': 0, 'reopened': 0, 'seed_bios': 0, 'partial': 0}
    reopened = get_setting(conn, 'lists_reopened') or {}
    live = {(r[0].lower(), r[1]) for r in conn.execute(
        "SELECT seed, direction FROM jobs WHERE kind='list' AND state IN ('queued','leased')")}
    private_denials = {(r[0].lower(), r[1]) for r in conn.execute(
        'SELECT DISTINCT seed,direction FROM list_private_denials')}
    # a list without a total borrows it from the seed's profile (its followers / following count)
    if not dry:
        conn.execute("UPDATE lists SET total=(SELECT CASE lists.direction WHEN 'followers' THEN p.followers ELSE p.following END "
                     "FROM people p WHERE p.handle=lists.seed) WHERE total IS NULL")
    rows = {(r['seed'].lower(), r['direction']): r for r in conn.execute('SELECT * FROM lists')}
    # done lists nobody can judge yet: read the seed's profile first (its counts are the list totals)
    blind = {r['seed'] for r in rows.values() if r['state'] == 'done' and r['total'] is None}
    have = {r[0] for r in conn.execute("SELECT handle FROM jobs WHERE kind='profile' AND state IN ('queued','leased')")}
    have |= {r[0] for r in conn.execute('SELECT handle FROM people WHERE bio_at IS NOT NULL')}   # read once is enough
    out['seed_bios'] = len(blind - have)
    if not dry:
        for s in blind - have:
            conn.execute('INSERT INTO jobs(kind, handle, priority, created_at) VALUES(?,?,?,?)', ('profile', s, 10000, ts))
    todo = []
    partial_candidates = []
    for (s,) in conn.execute("SELECT handle FROM seeds WHERE instr(handle, '~')=0 AND NOT EXISTS "
                             "(SELECT 1 FROM collection_discovery d WHERE d.handle=seeds.handle AND d.state='queued')"):
        for d in ('followers', 'following'):
            r = rows.get((s.lower(), d))
            if r is None:
                out['added'] += 1
                todo.append((s, d, 'add'))
            elif r['state'] in ('paused', 'error', 'queued', 'running', None) and (s.lower(), d) not in live:
                out['requeued'] += 1
                todo.append((r['seed'], d, 'requeue'))
            elif r['state'] == 'done' and (s.lower(), d) not in live and (
                    r['cursor'] is not None or not list_run_complete(conn, r['run_job_id'])):
                if reopened.get(f'{s}|{d}', 0) < REOPEN_MAX:
                    out['reopened'] += 1
                    todo.append((r['seed'], d, 'reopen'))
                else:
                    out['partial'] += 1
                    todo.append((r['seed'], d, 'partial'))
            elif r['state'] == 'partial' and (s.lower(), d) not in live and (
                    (s.lower(), d) not in private_denials and r['released_why'] != 'private' and
                    not (r['error'] or '').startswith('Instagram limited this list;') and
                    reopened.get(f'{s}|{d}', 0) < REOPEN_MAX):
                try:
                    updated = datetime.fromisoformat(r['updated_at'])
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    continue  # uncertain age must not trigger automatic Instagram requests
                if utc_now() - updated >= PARTIAL_RETRY_DELAY:
                    partial_candidates.append((updated, r['seed'], d))
    for _, s, d in sorted(partial_candidates)[:PARTIAL_RETRY_BATCH]:
        out['reopened'] += 1
        todo.append((s, d, 'retry_partial'))
    if dry:
        return out
    for s, d, what in todo:
        if what == 'partial':
            conn.execute("UPDATE lists SET state='partial', error='Complete list coverage could not be verified after retries', updated_at=? WHERE seed=? AND direction=?", (ts, s, d))
            continue
        if what == 'add':
            conn.execute('INSERT OR IGNORE INTO lists(seed, direction, state, updated_at) VALUES(?,?,?,?)', (s, d, 'queued', ts))
        else:
            if what in ('reopen', 'retry_partial'):
                reopened[f'{s}|{d}'] = reopened.get(f'{s}|{d}', 0) + 1
                row = rows[(s.lower(), d)]
                # An interrupted cursor chain can continue with its tracked prefix.
                # A terminal short list or a cursor cycle needs a fresh first page.
                run = (conn.execute('SELECT first_page_seen FROM list_runs WHERE job_id=?',
                                    (row['run_job_id'],)).fetchone() if row['run_job_id'] is not None else None)
                tracked_prefix = bool(run and run[0])
                resume = (what == 'retry_partial' and row['cursor'] is not None and tracked_prefix and
                          not (row['error'] or '').startswith(('Instagram repeated its page cursor.',
                                                                'Earlier pages lack first-page tracking;',
                                                                'The page history does not prove')))
                if not resume:
                    conn.execute('UPDATE lists SET cursor=NULL,run_job_id=NULL,received=0 WHERE seed=? AND direction=?', (s, d))
            conn.execute("UPDATE lists SET state='queued', error=NULL, prev_lane=coalesce(lane,prev_lane), "
                         "lane=NULL, released_at=?, released_why='repair', updated_at=? WHERE seed=? AND direction=?",
                         (ts, ts, s, d))
        if (s.lower(), d) not in live:
            job_id = conn.execute('INSERT INTO jobs(kind, seed, direction, priority, created_at) VALUES(?,?,?,?,?)', ('list', s, d, 0, ts)).lastrowid
            start_list_run(conn, job_id, s, d)
            live.add((s.lower(), d))
    if out['reopened']:
        set_setting(conn, 'lists_reopened', reopened)
    return out
