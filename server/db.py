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

PERSON_FIELDS = ('ig_id', 'handle', 'name', 'pic_url', 'is_private', 'is_verified', 'bio', 'website', 'category',
                 'followers', 'following', 'posts', 'is_business', 'bio_at')
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


def init(path):
    conn = connect(path)
    conn.executescript(SCHEMA)
    migrate_tags(conn)
    conn.execute('DROP INDEX IF EXISTS tags_tag')  # superseded by the covering tags_tag_src
    conn.execute('DROP INDEX IF EXISTS edges_person')  # superseded by the covering edges_person_seed
    # columns added after the first release: ALTER only when missing, so any older DB opens as is
    for table, col, decl in (('pages', 'at', 'TEXT'), ('pages', 'lane', 'TEXT'), ('pages', 'users', 'INT'),
                             ('verdicts', 'prompt', 'TEXT'), ('verdicts', 'evidence', 'TEXT'),
                             ('jobs', 'lane', 'TEXT'), ('lists', 'lane', 'TEXT'), ('lists', 'prev_lane', 'TEXT'),
                             ('lists', 'released_at', 'TEXT'), ('lists', 'released_why', 'TEXT')):
        if col not in {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {decl}')
    conn.execute('CREATE INDEX IF NOT EXISTS pages_at ON pages(at)')
    conn.execute('CREATE INDEX IF NOT EXISTS pages_lane_at ON pages(lane, at)')
    conn.execute('CREATE INDEX IF NOT EXISTS jobs_lane ON jobs(lane) WHERE lane IS NOT NULL')
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


REOPEN_MAX = 2          # a list that keeps ending short (hidden accounts, IG caps) is reopened at most this often
SHORT_RATIO = 0.95      # done with received below this share of the known total = ended early
SHORT_MIN = 20          # ... and at least this many people missing


def repair_lists(conn, dry=False):
    """Make sure every seed has both lists queued until they are really complete. Returns counts; caller commits.
    - a seed without a followers/following list row gets one (queued);
    - a list in state paused/error/queued/running without a live job gets a job again (cursor kept: it resumes);
    - a list marked done with received well below its known total is reopened from the start (edges dedupe),
      at most REOPEN_MAX times per list."""
    ts = now()
    out = {'added': 0, 'requeued': 0, 'reopened': 0, 'seed_bios': 0}
    reopened = get_setting(conn, 'lists_reopened') or {}
    live = {(r[0].lower(), r[1]) for r in conn.execute(
        "SELECT seed, direction FROM jobs WHERE kind='list' AND state IN ('queued','leased')")}
    # a list without a total borrows it from the seed's profile (its followers / following count)
    if not dry:
        conn.execute("UPDATE lists SET total=(SELECT CASE lists.direction WHEN 'followers' THEN p.followers ELSE p.following END "
                     "FROM people p WHERE p.handle=lists.seed) WHERE total IS NULL")
    rows = {(r['seed'].lower(), r['direction']): r for r in conn.execute('SELECT * FROM lists')}
    # done lists nobody can judge yet: read the seed's profile first (its counts are the list totals)
    blind = {r['seed'] for r in rows.values() if r['state'] == 'done' and not r['total']}
    have = {r[0] for r in conn.execute("SELECT handle FROM jobs WHERE kind='profile' AND state IN ('queued','leased')")}
    have |= {r[0] for r in conn.execute('SELECT handle FROM people WHERE bio_at IS NOT NULL')}   # read once is enough
    out['seed_bios'] = len(blind - have)
    if not dry:
        for s in blind - have:
            conn.execute('INSERT INTO jobs(kind, handle, priority, created_at) VALUES(?,?,?,?)', ('profile', s, 10000, ts))
    todo = []
    for (s,) in conn.execute("SELECT handle FROM seeds WHERE instr(handle, '~')=0"):
        for d in ('followers', 'following'):
            r = rows.get((s.lower(), d))
            if r is None:
                out['added'] += 1
                todo.append((s, d, 'add'))
            elif r['state'] in ('paused', 'error', 'queued', 'running', None) and (s.lower(), d) not in live:
                out['requeued'] += 1
                todo.append((r['seed'], d, 'requeue'))
            elif r['state'] == 'done' and r['total'] and (r['received'] or 0) < SHORT_RATIO * r['total'] \
                    and r['total'] - (r['received'] or 0) >= SHORT_MIN and reopened.get(f'{s}|{d}', 0) < REOPEN_MAX:
                out['reopened'] += 1
                todo.append((r['seed'], d, 'reopen'))
    if dry:
        return out
    for s, d, what in todo:
        if what == 'add':
            conn.execute('INSERT OR IGNORE INTO lists(seed, direction, state, updated_at) VALUES(?,?,?,?)', (s, d, 'queued', ts))
        else:
            if what == 'reopen':
                reopened[f'{s}|{d}'] = reopened.get(f'{s}|{d}', 0) + 1
                conn.execute('UPDATE lists SET cursor=NULL WHERE seed=? AND direction=?', (s, d))
            conn.execute("UPDATE lists SET state='queued', error=NULL, lane=NULL, updated_at=? WHERE seed=? AND direction=?", (ts, s, d))
        if (s.lower(), d) not in live:
            conn.execute('INSERT INTO jobs(kind, seed, direction, priority, created_at) VALUES(?,?,?,?,?)', ('list', s, d, 0, ts))
            live.add((s.lower(), d))
    if out['reopened']:
        set_setting(conn, 'lists_reopened', reopened)
    return out
