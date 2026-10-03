"""Schema storage ownership; writes remain in the caller transaction."""

import json
from pathlib import Path

from .clock import now
from .connection import connect
from .invalidation import mark_network_dirty
from .map_schema import ensure_map_layout_dirty, init_map_membership_revision, init_map_person_degree
from .settings import get_setting, set_setting


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
  retry_not_before TEXT, limit_hits INT NOT NULL DEFAULT 0, page_size INT, experiment_viewer_ig_id TEXT, collection_backend TEXT NOT NULL DEFAULT 'chrome',
  backend_lane TEXT, backend_viewer_ig_id TEXT);
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
  list_cool_until TEXT, profile_cool_until TEXT, list_endpoint_until TEXT, rate TEXT, today TEXT, last_error TEXT, activity TEXT, text TEXT, collection_backend TEXT NOT NULL DEFAULT 'chrome');
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
    try:
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
                                 ('jobs', 'page_size', 'INT'), ('jobs', 'target_ig_id', 'TEXT'),
                                 ('jobs', 'experiment_viewer_ig_id', 'TEXT'),
                                 ('jobs', 'collection_backend', "TEXT NOT NULL DEFAULT 'chrome'"),
                                 ('jobs', 'backend_lane', 'TEXT'), ('jobs', 'backend_viewer_ig_id', 'TEXT'),
                                 ('accounts', 'collection_backend', "TEXT NOT NULL DEFAULT 'chrome'"),
                                 ('collector_events', 'route', 'TEXT'),
                                 ('collector_events', 'saved_entries', 'INT'),
                                 ('accounts', 'profile_cool_until', 'TEXT'),
                                 ('accounts', 'list_endpoint_until', 'TEXT'),
                                 ('lists', 'lane', 'TEXT'), ('lists', 'prev_lane', 'TEXT'),
                                 ('list_runs', 'member_count', 'INT NOT NULL DEFAULT 0'),
                                 ('lists', 'run_job_id', 'INT'), ('lists', 'released_at', 'TEXT'), ('lists', 'released_why', 'TEXT'),
                                 ('people', 'bio_src', 'TEXT'), ('people', 'bd_at', 'TEXT'),
                                 ('people', 'pic_refresh', 'INT NOT NULL DEFAULT 0'),
                                 ('people', 'pic_attempts', 'INT NOT NULL DEFAULT 0'),
                                 ('people', 'pic_retry_at', 'TEXT')):
            if col not in {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {decl}')
                if table == 'list_runs' and col == 'member_count':
                    # Existing tracked prefixes already have members. Backfill once
                    # before triggers start maintaining the exact count.
                    conn.execute('UPDATE list_runs SET member_count=(SELECT count(*) FROM list_members WHERE job_id=list_runs.job_id)')
        # Preserve identity allocation across merges/deletes. Include old cache names:
        # a deleted pre-upgrade row can otherwise donate its photo to the next insert.
        conn.execute('CREATE TABLE IF NOT EXISTS person_id_sequence(singleton INTEGER PRIMARY KEY CHECK(singleton=1), value INTEGER NOT NULL)')
        if not conn.execute('SELECT 1 FROM person_id_sequence').fetchone():
            highest = conn.execute('SELECT coalesce(max(id),0) FROM people').fetchone()[0]
            # Legacy deletions can leave references after the people row is gone.
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                table = '"' + row[0].replace('"', '""') + '"'
                if any(column[1] == 'person_id' for column in conn.execute(f'PRAGMA table_info({table})')):
                    value = conn.execute(f'SELECT max(person_id) FROM {table}').fetchone()[0]
                    if isinstance(value, int):
                        highest = max(highest, value)
            directory = Path(path).resolve().parent / 'pfp'
            if directory.is_dir():
                highest = max([highest] + [int(f.stem) for f in directory.glob('*.jpg')
                                          if f.stem.isdigit() and len(f.stem) < 19])
            conn.execute('INSERT INTO person_id_sequence VALUES(1,?)', (highest,))
        conn.execute("CREATE TRIGGER IF NOT EXISTS people_identity_immutable BEFORE UPDATE OF id ON people "
                     "WHEN NEW.id != OLD.id BEGIN SELECT RAISE(ABORT,'person id is immutable'); END")
        conn.execute("CREATE TRIGGER IF NOT EXISTS people_identity_allocation AFTER INSERT ON people BEGIN "
                     "SELECT CASE WHEN NEW.id <= (SELECT value FROM person_id_sequence WHERE singleton=1) "
                     "THEN RAISE(ABORT,'person id cannot be reused') END; "
                     "UPDATE person_id_sequence SET value=NEW.id WHERE singleton=1; END")
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
        conn.execute("CREATE INDEX IF NOT EXISTS jobs_profile_target ON jobs(target_ig_id,state) WHERE kind='profile'")
        conn.execute("CREATE INDEX IF NOT EXISTS jobs_profile_handle ON jobs(handle COLLATE NOCASE,state) WHERE kind='profile'")
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
        import lead_rank
        lead_rank.ensure(conn)
        import tag_facets
        tag_facets.ensure(conn)
        init_map_membership_revision(conn)
        ensure_map_layout_dirty(conn)
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

    except BaseException:
        try:
            conn.rollback()
        finally:
            conn.close()
        raise


# Background bookkeeping that the lead list, facets and map never show; writing it must not invalidate their cache.
REV_QUIET = {'people': {'pic_file', 'pic_refresh', 'pic_attempts', 'pic_retry_at', 'updated_at'}, 'verdicts': {'updated_at', 'input_hash', 'prompt'}}


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
