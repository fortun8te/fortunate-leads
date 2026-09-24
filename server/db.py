import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
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
CREATE INDEX IF NOT EXISTS edges_person ON edges(person_id);
CREATE INDEX IF NOT EXISTS tags_tag_src ON tags(tag, source, person_id, grp);   -- covering: facets, rule hits, tag filters
CREATE INDEX IF NOT EXISTS tags_person_src ON tags(person_id, source, tag);
CREATE INDEX IF NOT EXISTS verdicts_tier ON verdicts(tier, score);
CREATE INDEX IF NOT EXISTS verdicts_score ON verdicts(score);
CREATE INDEX IF NOT EXISTS people_updated ON people(updated_at);
CREATE INDEX IF NOT EXISTS people_followers ON people(followers);
CREATE INDEX IF NOT EXISTS people_first_seen ON people(first_seen);
CREATE INDEX IF NOT EXISTS marks_status ON marks(status);
CREATE INDEX IF NOT EXISTS jobs_next ON jobs(state, kind, priority);
CREATE INDEX IF NOT EXISTS jobs_handle ON jobs(handle);
"""

PERSON_FIELDS = ('ig_id', 'handle', 'name', 'pic_url', 'is_private', 'is_verified', 'bio', 'website', 'category',
                 'followers', 'following', 'posts', 'is_business', 'bio_at')
DEFAULTS = {'paused': False, 'budget': {'list': 2000, 'profile': 0}, 'qualify': False, 'qualify_auto': True, 'llm_workers': 4,
            'llm_min': 40}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def connect(path):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=15000')
    conn.execute('PRAGMA synchronous=NORMAL')
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


def init(path):
    conn = connect(path)
    conn.executescript(SCHEMA)
    migrate_tags(conn)
    conn.execute('DROP INDEX IF EXISTS tags_tag')  # superseded by the covering tags_tag_src
    if 'at' not in {r[1] for r in conn.execute('PRAGMA table_info(pages)')}:  # DBs created before pages were timestamped
        conn.execute('ALTER TABLE pages ADD COLUMN at TEXT')
    have = {r[1] for r in conn.execute('PRAGMA table_info(verdicts)')}
    for col in ('prompt', 'evidence'):  # DBs from before the staged qualifier
        if col not in have:
            conn.execute(f'ALTER TABLE verdicts ADD COLUMN {col} TEXT')
    conn.execute('CREATE INDEX IF NOT EXISTS pages_at ON pages(at)')
    conn.execute('CREATE INDEX IF NOT EXISTS edges_first_seen ON edges(first_seen)')
    conn.commit()
    return conn


def norm_handle(h):
    h = (h or '').strip().lstrip('@').rstrip('/')
    if 'instagram.com/' in h:
        h = h.split('instagram.com/')[1].split('/')[0].split('?')[0]
    return h.lower()


def upsert_person(conn, u, ts=None):
    ts = ts or now()
    vals = {k: u[k] for k in PERSON_FIELDS if u.get(k) is not None}
    for k in ('is_private', 'is_verified', 'is_business'):
        if k in vals:
            vals[k] = int(bool(vals[k]))
    if 'ig_id' in vals:
        vals['ig_id'] = str(vals['ig_id'])
    if 'handle' in vals:
        vals['handle'] = norm_handle(vals['handle'])
    by_id = vals.get('ig_id') and conn.execute('SELECT id FROM people WHERE ig_id=?', (vals['ig_id'],)).fetchone()
    by_handle = vals.get('handle') and conn.execute('SELECT id, ig_id FROM people WHERE handle=?', (vals['handle'],)).fetchone()
    if not by_id and by_handle and by_handle['ig_id'] and vals.get('ig_id') and by_handle['ig_id'] != vals['ig_id']:
        # a different account now holds this handle: the old row keeps its marks/edges/tags under a parked handle
        conn.execute("UPDATE people SET handle=handle||'~'||id WHERE id=?", (by_handle['id'],))
        by_handle = None
    if by_id and by_handle and by_id['id'] != by_handle['id']:
        if by_handle['ig_id']:  # the handle now belongs to this ig_id; the old holder renamed
            conn.execute("UPDATE people SET handle=handle||'~'||id WHERE id=?", (by_handle['id'],))
        else:  # same account seen before without ig_id: fold it in
            merge_people(conn, keep=by_id['id'], drop=by_handle['id'])
    row = by_id or by_handle
    if row:
        pid = row['id']
        if 'pic_url' in vals:  # a failed download ('') gets another try once the URL changes
            conn.execute("UPDATE people SET pic_file=NULL WHERE id=? AND pic_file='' AND pic_url IS NOT ?",
                         (pid, vals['pic_url']))
        if vals:
            conn.execute(f"UPDATE people SET {', '.join(k + '=?' for k in vals)}, updated_at=? WHERE id=?",
                         (*vals.values(), ts, pid))
        return pid
    cols = list(vals) + ['first_seen', 'updated_at']
    return conn.execute(f"INSERT INTO people({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                        (*vals.values(), ts, ts)).lastrowid


def merge_people(conn, keep, drop):
    for t in ('edges', 'tags', 'marks', 'verdicts'):
        conn.execute(f'UPDATE OR IGNORE {t} SET person_id=? WHERE person_id=?', (keep, drop))
        conn.execute(f'DELETE FROM {t} WHERE person_id=?', (drop,))
    conn.execute('DELETE FROM people WHERE id=?', (drop,))


def add_edge(conn, seed, person_id, direction, ts=None):
    return conn.execute('INSERT OR IGNORE INTO edges VALUES(?,?,?,?)',
                        (norm_handle(seed), person_id, direction, ts or now())).rowcount == 1


def get_setting(conn, key, default=None):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    # a copy: callers mutate dicts (budget.update) and must never change the shared defaults
    return json.loads(row[0] if row else json.dumps(DEFAULTS.get(key, default)))


def set_setting(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))


def queue_list(conn, seed, direction, priority=0):
    seed = norm_handle(seed)
    ts = now()
    conn.execute('INSERT OR IGNORE INTO seeds(handle, added_at) VALUES(?,?)', (seed, ts))
    row = conn.execute('SELECT state FROM lists WHERE seed=? AND direction=?', (seed, direction)).fetchone()
    if row and row['state'] in ('done', 'private'):
        return False
    conn.execute('INSERT INTO lists(seed, direction, state, updated_at) VALUES(?,?,?,?) '
                 "ON CONFLICT DO UPDATE SET state='queued', error=NULL, updated_at=excluded.updated_at",
                 (seed, direction, 'queued', ts))
    if not conn.execute("SELECT 1 FROM jobs WHERE kind='list' AND seed=? AND direction=? AND state IN ('queued','leased')",
                        (seed, direction)).fetchone():
        conn.execute('INSERT INTO jobs(kind, seed, direction, priority, created_at) VALUES(?,?,?,?,?)',
                     ('list', seed, direction, priority, ts))
    return True
