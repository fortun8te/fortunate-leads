"""Global seen-cache: every profile is discovered many times but enriched at most once per TTL.

Backed by its own SQLite file (default data/scale.sqlite, gitignored with data/), separate from
leads.sqlite so the hot path never contends with the web app. `import_known()` marks people the
main database already has a bio for, so the pool never re-fetches them.

States: new -> leased -> done | private | not_found | dead (5 failures), or back to new with a
retry_at after a transient failure. Being seen in more lists raises priority (people in 2+ seed
lists are worth reading first, as in the existing profile planner).
"""
import sqlite3
import threading
import time

TTL_DONE = 30 * 86400.0
MAX_ATTEMPTS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen(
  handle TEXT PRIMARY KEY COLLATE NOCASE,
  ig_id TEXT,
  first_seen REAL NOT NULL,
  sources INTEGER NOT NULL DEFAULT 1,
  priority REAL NOT NULL DEFAULT 0,
  is_private INTEGER,
  is_verified INTEGER,
  state TEXT NOT NULL DEFAULT 'new',
  enriched_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  retry_at REAL NOT NULL DEFAULT 0,
  lease_until REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS seen_pick ON seen(state, priority DESC, first_seen);
CREATE TABLE IF NOT EXISTS seen_src(handle TEXT COLLATE NOCASE, src TEXT, PRIMARY KEY(handle, src));
"""


def default_priority(user, sources):
    p = 1.0 + 2.0 * (sources - 1)
    if user.get('is_verified'):
        p += 1.0
    if user.get('is_private'):
        p -= 3.0          # still readable logged-out (bio, counts), just less useful
    return p


class SeenStore:
    def __init__(self, path=':memory:', priority_fn=default_priority, ttl=TTL_DONE):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if path != ':memory:':
            self.conn.execute('PRAGMA journal_mode=WAL')
            self.conn.execute('PRAGMA synchronous=NORMAL')
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.priority_fn, self.ttl = priority_fn, ttl

    # ---- discovery ------------------------------------------------------
    def add_discovered(self, users, source=None, now=None):
        """users: [{handle, ig_id?, is_private?, is_verified?}]. Returns handles that are new."""
        now = time.time() if now is None else now
        fresh = []
        with self.lock:
            c = self.conn
            c.execute('BEGIN IMMEDIATE')
            try:
                for u in users:
                    h = str(u.get('handle') or '').strip().lstrip('@').lower()
                    if not h or len(h) > 30:
                        continue
                    new_src = True
                    if source:
                        new_src = c.execute('INSERT OR IGNORE INTO seen_src(handle,src) VALUES(?,?)',
                                            (h, str(source))).rowcount == 1
                    row = c.execute('SELECT sources FROM seen WHERE handle=?', (h,)).fetchone()
                    if row is None:
                        c.execute('INSERT INTO seen(handle,ig_id,first_seen,sources,priority,is_private,is_verified) '
                                  'VALUES(?,?,?,?,?,?,?)',
                                  (h, u.get('ig_id'), now, 1, self.priority_fn(u, 1),
                                   _b(u.get('is_private')), _b(u.get('is_verified'))))
                        fresh.append(h)
                    elif new_src:
                        n = row['sources'] + 1
                        c.execute('UPDATE seen SET sources=?, priority=?, ig_id=coalesce(ig_id,?) WHERE handle=?',
                                  (n, self.priority_fn(u, n), u.get('ig_id'), h))
                c.execute('COMMIT')
            except Exception:
                c.execute('ROLLBACK')
                raise
        return fresh

    def import_known(self, handles, now=None):
        """Mark handles the main DB already enriched as done (never fetched again inside the TTL)."""
        now = time.time() if now is None else now
        with self.lock:
            self.conn.execute('BEGIN IMMEDIATE')
            for h in handles:
                h = str(h).strip().lstrip('@').lower()
                if h:
                    self.conn.execute("INSERT INTO seen(handle,first_seen,state,enriched_at) VALUES(?,?,'done',?) "
                                      "ON CONFLICT(handle) DO UPDATE SET state='done', enriched_at=excluded.enriched_at",
                                      (h, now, now))
            self.conn.execute('COMMIT')

    # ---- enrichment queue ----------------------------------------------
    def lease(self, n, now=None, lease_s=300.0):
        """Up to n handles to enrich, best first. Expired leases and stale 'done' rows come back."""
        now = time.time() if now is None else now
        with self.lock:
            c = self.conn
            c.execute('BEGIN IMMEDIATE')
            rows = c.execute(
                "SELECT handle, ig_id FROM seen WHERE "
                "(state='new' AND retry_at<=?) OR (state='leased' AND lease_until<=?) "
                "OR (state='done' AND enriched_at<?) "
                "ORDER BY priority DESC, first_seen LIMIT ?", (now, now, now - self.ttl, int(n))).fetchall()
            for r in rows:
                c.execute("UPDATE seen SET state='leased', lease_until=? WHERE handle=?", (now + lease_s, r['handle']))
            c.execute('COMMIT')
        return [(r['handle'], r['ig_id']) for r in rows]

    def done(self, handle, outcome='done', now=None, ig_id=None):
        now = time.time() if now is None else now
        state = outcome if outcome in ('done', 'private', 'not_found') else 'done'
        with self.lock:
            self.conn.execute('UPDATE seen SET state=?, enriched_at=?, attempts=0, lease_until=0, '
                              'ig_id=coalesce(?, ig_id) WHERE handle=?', (state, now, ig_id, handle.lower()))

    def fail(self, handle, now=None, backoff_s=600.0):
        """Transient failure: back to the queue later; parked after MAX_ATTEMPTS."""
        now = time.time() if now is None else now
        with self.lock:
            row = self.conn.execute('SELECT attempts FROM seen WHERE handle=?', (handle.lower(),)).fetchone()
            n = (row['attempts'] if row else 0) + 1
            state = 'dead' if n >= MAX_ATTEMPTS else 'new'
            self.conn.execute('UPDATE seen SET state=?, attempts=?, retry_at=?, lease_until=0 WHERE handle=?',
                              (state, n, now + backoff_s * n, handle.lower()))

    def release(self, handle):
        """Give a lease back untouched (the egress failed, not the profile)."""
        with self.lock:
            self.conn.execute("UPDATE seen SET state='new', lease_until=0 WHERE handle=? AND state='leased'",
                              (handle.lower(),))

    def known(self, handle):
        return self.conn.execute('SELECT 1 FROM seen WHERE handle=?', (handle.lower(),)).fetchone() is not None

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def __del__(self):
        self.close()

    def stats(self):
        out = {r['state']: r['n'] for r in self.conn.execute('SELECT state, count(*) n FROM seen GROUP BY state')}
        out['total'] = sum(out.values())
        return out


def _b(v):
    return None if v is None else int(bool(v))
