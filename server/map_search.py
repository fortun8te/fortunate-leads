"""Search for people on the map: one ranked, indexed answer over every saved profile.

``/api/map/search?q=`` used to match handles by prefix and names only among the top of the layout, so most
people could not be found by name, accents and word order broke matches, and bios were ignored. This module
keeps a small side index next to the layout files and ranks like this:

  1000 exact handle         940 exact name        850 handle prefix      800 name prefix
   760 whole words          700 word prefixes     600 contains (3+ letters)
   450 words in the bio     300..180 close spelling (one or two letters off)

Inside a tier people are ordered by closeness to the owner, then follower count. Text is folded first
(case, accents, fancy Unicode letters, ``@``, dots and underscores), so ``andre elmenhorst`` finds
``André Elmenhorst`` and ``chambers matthew`` finds ``Matthew Chambers``.

The index is a separate SQLite file (``<db>.search/index.v1.sqlite``), never the main database. It uses FTS5
(word prefixes, trigram substrings, bios) and falls back to plain indexed tables and scans when this SQLite
build has no FTS5. It follows the people table by ``updated_at`` and takes closeness from the layout store.
"""
import argparse
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

SCHEMA_VERSION = 1
INDEX_FILE = 'index.v1.sqlite'
LIMIT_DEFAULT, LIMIT_MAX = 12, 30
SYNC_EVERY = 3.0                  # seconds between checks for changed people
NEAR_EVERY = 120.0                # seconds between closeness refreshes from the layout store
RECONCILE_EVERY = 600.0
BACKGROUND_ABOVE = 400_000        # people; larger databases build in the background
FUZZY_MAX_CANDIDATES = 1500

_SPECIAL = str.maketrans({'ø': 'o', 'ß': 'ss', 'æ': 'ae', 'œ': 'oe', 'ł': 'l', 'đ': 'd', 'ı': 'i', 'ð': 'd', 'þ': 'th'})
_TOKEN = re.compile(r'[^\W_]+', re.UNICODE)
_URL_HOSTS = ('instagram.com', 'www.instagram.com', 'm.instagram.com')

# Tier scores. The names are used by the browser to explain a match.
EXACT_HANDLE, EXACT_NAME, HANDLE_PREFIX, NAME_PREFIX, WHOLE_WORDS, WORD_PREFIX, CONTAINS, BIO, FUZZY = (
    1000, 940, 850, 800, 760, 700, 600, 450, 300)


def fold(text):
    """Lowercase, no accents, fancy Unicode letters flattened. ``Zé``, ``ZÉ`` and ``ze`` fold the same."""
    text = unicodedata.normalize('NFKD', str(text or '')).casefold().translate(_SPECIAL)
    return ''.join(ch for ch in text if not unicodedata.combining(ch))


def tokens(text):
    return _TOKEN.findall(fold(text))


class Parsed:
    __slots__ = ('raw', 'text', 'tokens', 'compact', 'key')

    def __init__(self, raw, text, toks):
        self.raw, self.text, self.tokens = raw, text, toks
        self.compact = ''.join(toks)
        self.key = ' '.join(toks)

    def __bool__(self):
        return bool(self.tokens)


def parse(text):
    """Normalize what was typed. Profile links reduce to the handle; other links are refused."""
    text = str(text or '').strip()
    if len(text) > 512:
        raise ValueError('Search for a name or Instagram handle.')
    if text:
        candidate = text if '://' in text else 'https://' + text
        parsed = urlsplit(candidate)
        if parsed.hostname and parsed.hostname.lower() in _URL_HOSTS:
            parts = [unquote(part) for part in parsed.path.split('/') if part]
            if len(parts) != 1 or not re.fullmatch(r'[A-Za-z0-9_.]{1,30}', parts[0]):
                raise ValueError('Use an Instagram profile link, not a post or reel.')
            text = parts[0]
        elif '://' in text or text.lower().startswith(('www.', 'instagram.com/')):
            raise ValueError('Search for a name, handle or Instagram profile link.')
    folded = fold(text.lstrip('@').strip())
    folded = re.sub(r'\s+', ' ', folded)
    return Parsed(text, folded, _TOKEN.findall(folded))


# ---------- edit distance ----------

def distance(a, b, limit):
    """Optimal string alignment distance (insert, delete, substitute, swap neighbours); limit+1 when beyond."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    if limit == 1:
        return _within_one(a, b)
    previous2 = None
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i] + [0] * len(b)
        best = current[0]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if previous2 is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                value = min(value, previous2[j - 2] + 1)
            current[j] = value
            best = min(best, value)
        if best > limit:
            return limit + 1
        previous2, previous = previous, current
    return previous[-1]


def _within_one(a, b):
    """1 when ``a`` and ``b`` differ by one substitution, insertion, deletion or neighbour swap, else 2."""
    la, lb = len(a), len(b)
    i = 0
    shorter = min(la, lb)
    while i < shorter and a[i] == b[i]:
        i += 1
    if la == lb:
        if a[i + 1:] == b[i + 1:]:
            return 1
        if i + 1 < la and a[i] == b[i + 1] and a[i + 1] == b[i] and a[i + 2:] == b[i + 2:]:
            return 1
        return 2
    if la > lb:
        return 1 if a[i + 1:] == b[i:] else 2
    return 1 if b[i + 1:] == a[i:] else 2


def allowed_edits(length):
    return 0 if length < 4 else 1 if length < 10 else 2


# ---------- ranking ----------

def classify(q, h, hs, n, w):
    """(score, field, kind) of the best way ``q`` matches a person, or None. Pure and exact."""
    text, compact, key, toks = q.text, q.compact, q.key, q.tokens
    if not compact:
        return None
    if h == text or hs == compact:
        return EXACT_HANDLE, 'handle', 'exact'
    if n == key:
        return EXACT_NAME, 'name', 'exact'
    if hs.startswith(compact) or h.startswith(text):
        return HANDLE_PREFIX, 'handle', 'prefix'
    if n.startswith(key):
        return NAME_PREFIX, 'name', 'prefix'
    words = w.split()
    if words:
        named = n.split()
        if all(any(word == t for word in words) for t in toks):
            return WHOLE_WORDS, 'name' if all(t in named for t in toks) else 'handle', 'word'
        if all(any(word.startswith(t) for word in words) for t in toks):
            return WORD_PREFIX, 'name' if all(any(x.startswith(t) for x in named) for t in toks) else 'handle', 'word'
    if len(compact) >= 3:
        if compact in hs or text in h:
            return CONTAINS, 'handle', 'contains'
        if key in n:
            return CONTAINS, 'name', 'contains'
    return None


def fuzzy_cost(q, hs, w):
    """Total letters off when every query word is close to some word of the person, else None."""
    words = w.split()
    total = 0
    for token in q.tokens:
        allowed = allowed_edits(len(token))
        if allowed == 0 and len(token) < 3:
            return None
        best = None
        for word in words:
            d = distance(token, word, allowed)
            if d <= allowed and (best is None or d < best):
                best = d
        if best is None and len(q.tokens) == 1:
            allowed = allowed_edits(len(q.compact))
            d = distance(q.compact, hs, allowed)
            best = d if d <= allowed else None
        if best is None:
            return None
        total += best
    return total


# ---------- index ----------

SCHEMA = """
CREATE TABLE entry(id INTEGER PRIMARY KEY, h TEXT NOT NULL, hs TEXT NOT NULL, n TEXT NOT NULL, w TEXT NOT NULL,
  bio TEXT, near INTEGER NOT NULL DEFAULT 0, fol INTEGER NOT NULL DEFAULT 0, upd TEXT NOT NULL DEFAULT '');
CREATE INDEX entry_h ON entry(h);
CREATE INDEX entry_hs ON entry(hs);
CREATE INDEX entry_n ON entry(n);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
"""
FTS_SCHEMA = """
CREATE VIRTUAL TABLE f_tri USING fts5(h, hs, n, content='entry', content_rowid='id', tokenize='trigram');
CREATE VIRTUAL TABLE f_word USING fts5(w, content='entry', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2', prefix='1 2 3 4');
CREATE VIRTUAL TABLE f_bio USING fts5(bio, content='entry', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2', prefix='3');
CREATE VIRTUAL TABLE f_tri_v USING fts5vocab(f_tri, 'row');
"""
PLAIN_SCHEMA = "CREATE TABLE word(w TEXT NOT NULL, id INTEGER NOT NULL, PRIMARY KEY(w,id)) WITHOUT ROWID;"
FTS5 = None
_PEOPLE = 'SELECT id,handle,name,bio,followers,updated_at FROM people'


def has_fts5():
    global FTS5
    if FTS5 is None:
        try:
            c = sqlite3.connect(':memory:')
            c.execute("CREATE VIRTUAL TABLE t USING fts5(a, tokenize='trigram')")
            c.close()
            FTS5 = True
        except sqlite3.Error:
            FTS5 = False
    return FTS5


def index_dir(db_path):
    return Path(str(db_path) + '.search')


def _entry_values(row):
    pid, handle, name, bio, followers, updated = row
    h = fold(handle)
    toks = _TOKEN.findall(h)
    name_tokens = _TOKEN.findall(fold(name))
    words = list(dict.fromkeys(toks + name_tokens))
    return (pid, h, ''.join(toks), ' '.join(name_tokens), ' '.join(words), fold(bio) if bio else None,
            followers if followers is not None and followers >= 0 else 0, updated or '')


class Index:
    """One side index. Safe for many reader threads; builds and syncs are serialized."""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.dir = index_dir(db_path)
        self.path = self.dir / INDEX_FILE
        self.local = threading.local()
        self.write_lock = threading.Lock()
        self.last_sync = 0.0
        self.last_reconcile = time.time()
        self.build_gate = threading.Lock()
        self.building = False
        self.fts = has_fts5()

    # -- connections --
    def reader(self):
        try:
            stamp = os.stat(self.path)
        except OSError:
            return None
        stamp = (stamp.st_ino, stamp.st_size > 0)
        held = getattr(self.local, 'conn', None)
        if held is None or getattr(self.local, 'stamp', None) != stamp:
            if held is not None:
                held.close()
            held = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
            held.execute('PRAGMA query_only=1')
            held.execute('PRAGMA cache_size=-32768')
            held.execute('PRAGMA mmap_size=268435456')
            self.local.conn, self.local.stamp = held, stamp
        return held

    def close(self):
        held = getattr(self.local, 'conn', None)
        if held is not None:
            held.close()
            self.local.conn = None

    def meta(self, conn, key, default=None):
        row = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def ready(self):
        conn = self.reader()
        if conn is None:
            return False
        try:
            return self.meta(conn, 'schema') == SCHEMA_VERSION and self.meta(conn, 'fts') == self.fts and bool(self.meta(conn, 'complete'))
        except sqlite3.Error:
            return False

    # -- building --
    def build(self, main, progress=None):
        """Index every person into a fresh file, then swap it in."""
        with self.write_lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / (INDEX_FILE + '.building')
            for suffix in ('', '-wal', '-shm', '-journal'):
                try:
                    os.unlink(str(tmp) + suffix)
                except OSError:
                    pass
            conn = sqlite3.connect(str(tmp))
            started = time.time()
            try:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA synchronous=OFF')
                conn.executescript(SCHEMA)
                if not self.fts:
                    conn.executescript(PLAIN_SCHEMA)
                total = main.execute('SELECT count(*) FROM people').fetchone()[0]
                done, newest = 0, ('', 0)
                batch = []
                for row in main.execute(_PEOPLE):
                    values = _entry_values(row)
                    batch.append(values)
                    if (values[7], values[0]) > newest:
                        newest = (values[7], values[0])
                    if len(batch) >= 5000:
                        self._insert(conn, batch)
                        done += len(batch)
                        batch = []
                        if progress:
                            progress(done, total)
                if batch:
                    self._insert(conn, batch)
                if self.fts:
                    conn.executescript(FTS_SCHEMA)
                    for table in ('f_tri', 'f_word', 'f_bio'):
                        conn.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
                self._put(conn, 'schema', SCHEMA_VERSION)
                self._put(conn, 'fts', self.fts)
                self._put(conn, 'watermark', list(newest))
                self._put(conn, 'built_at', time.time())
                self._put(conn, 'near_at', 0)
                self._put(conn, 'near_key', None)
                self._put(conn, 'complete', True)
                conn.commit()
            except BaseException:
                conn.close()
                raise
            conn.close()
            os.replace(tmp, self.path)
            for suffix in ('-wal', '-shm', '-journal'):
                try:
                    os.unlink(str(tmp) + suffix)
                except OSError:
                    pass
            self.last_sync = time.time()
            return {'people': done, 'seconds': round(time.time() - started, 2), 'fts5': self.fts}

    def _insert(self, conn, batch):
        conn.executemany('INSERT OR REPLACE INTO entry(id,h,hs,n,w,bio,fol,upd) VALUES(?,?,?,?,?,?,?,?)', batch)
        if not self.fts:
            conn.executemany('INSERT OR IGNORE INTO word(w,id) VALUES(?,?)',
                             [(word, row[0]) for row in batch for word in row[4].split()])

    def _put(self, conn, key, value):
        conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (key, json.dumps(value)))

    # -- keeping up --
    def _writer(self):
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        return conn

    def sync(self, main, limit=4000):
        """Apply people changed since the last sync (by updated_at). Returns how many were applied."""
        if not self.write_lock.acquire(blocking=False):
            return 0
        applied = 0
        try:
            conn = self._writer()
            try:
                while applied < limit:
                    mark = self.meta(conn, 'watermark', ['', 0])
                    rows = main.execute(_PEOPLE + ' WHERE updated_at>? OR (updated_at=? AND id>?) '
                                        'ORDER BY updated_at,id LIMIT 1000', (mark[0], mark[0], mark[1])).fetchall()
                    if not rows:
                        break
                    conn.execute('BEGIN')
                    for row in rows:
                        values = _entry_values(row)
                        old = conn.execute('SELECT h,hs,n,w,bio,near FROM entry WHERE id=?', (row[0],)).fetchone()
                        if old is not None:
                            self._unindex(conn, row[0], old)
                        near = old[5] if old else 0
                        conn.execute('INSERT OR REPLACE INTO entry(id,h,hs,n,w,bio,near,fol,upd) VALUES(?,?,?,?,?,?,?,?,?)',
                                     (*values[:6], near, values[6], values[7]))
                        self._index(conn, row[0], values)
                    last = rows[-1]
                    self._put(conn, 'watermark', [last[5] or '', last[0]])
                    conn.commit()
                    applied += len(rows)
            finally:
                conn.close()
        finally:
            self.write_lock.release()
        return applied

    def _unindex(self, conn, pid, old):
        h, hs, n, w, bio, _ = old
        if self.fts:
            conn.execute("INSERT INTO f_tri(f_tri,rowid,h,hs,n) VALUES('delete',?,?,?,?)", (pid, h, hs, n))
            conn.execute("INSERT INTO f_word(f_word,rowid,w) VALUES('delete',?,?)", (pid, w))
            if bio:
                conn.execute("INSERT INTO f_bio(f_bio,rowid,bio) VALUES('delete',?,?)", (pid, bio))
        else:
            conn.execute('DELETE FROM word WHERE id=?', (pid,))

    def _index(self, conn, pid, values):
        _, h, hs, n, w, bio, _, _ = values
        if self.fts:
            conn.execute('INSERT INTO f_tri(rowid,h,hs,n) VALUES(?,?,?,?)', (pid, h, hs, n))
            conn.execute('INSERT INTO f_word(rowid,w) VALUES(?,?)', (pid, w))
            if bio:
                conn.execute('INSERT INTO f_bio(rowid,bio) VALUES(?,?)', (pid, bio))
        else:
            conn.executemany('INSERT OR IGNORE INTO word(w,id) VALUES(?,?)', [(word, pid) for word in w.split()])

    def reconcile(self, main):
        """Rebuild when the people table and the index disagree about who exists (merges, deletions)."""
        conn = self.reader()
        have = conn.execute('SELECT count(*) FROM entry').fetchone()[0]
        want = main.execute('SELECT count(*) FROM people').fetchone()[0]
        if have != want:
            self.build(main)
            return True
        return False

    def refresh_near(self, store, force=False):
        """Copy closeness from the layout store so ties sort by nearness to the owner."""
        if store is None:
            return 0
        key = f'{store.build_id}:{store.rev}'
        conn = self.reader()
        if not force and (self.meta(conn, 'near_key') == key
                          or time.time() - self.meta(conn, 'near_at', 0) < NEAR_EVERY):
            return 0
        if not self.write_lock.acquire(blocking=False):
            return 0
        changed = 0
        try:
            writer = self._writer()
            try:
                current = dict(writer.execute('SELECT id,near FROM entry'))
                updates = [(closeness, pid) for pid, closeness in store.conn.execute('SELECT person_id,cl FROM mp')
                           if current.get(pid, closeness) != closeness]
                writer.execute('BEGIN')
                writer.executemany('UPDATE entry SET near=? WHERE id=?', updates)
                self._put(writer, 'near_key', key)
                self._put(writer, 'near_at', time.time())
                writer.commit()
                changed = len(updates)
            finally:
                writer.close()
        finally:
            self.write_lock.release()
        return changed

    def stats(self):
        conn = self.reader()
        if conn is None:
            return {'ready': False}
        return {'ready': self.ready(), 'people': conn.execute('SELECT count(*) FROM entry').fetchone()[0],
                'bytes': os.path.getsize(self.path), 'fts5': self.fts, 'built_at': self.meta(conn, 'built_at')}


_INDEXES = {}
_INDEXES_LOCK = threading.Lock()


def index_for(db_path):
    key = str(db_path)
    with _INDEXES_LOCK:
        index = _INDEXES.get(key)
        if index is None or index.fts != has_fts5():
            index = _INDEXES[key] = Index(db_path)
        return index


def forget(db_path=None):
    """Drop cached index handles (tests)."""
    with _INDEXES_LOCK:
        gone = list(_INDEXES.values()) if db_path is None else [_INDEXES.pop(str(db_path), None)]
        if db_path is None:
            _INDEXES.clear()
    for index in gone:
        if index is not None:
            index.close()


def ensure(main, db_path, wait=True):
    """The index for this database, built when missing. Returns (index, building)."""
    index = index_for(db_path)
    if index.ready():
        return index, False
    people = main.execute('SELECT count(*) FROM people').fetchone()[0]
    if people > BACKGROUND_ABOVE and not wait:
        if not index.building:
            index.building = True

            def run():
                conn = None
                try:
                    import db
                    conn = db.connect(index.db_path)
                    index.build(conn)
                finally:
                    index.building = False
                    if conn is not None:
                        conn.close()
            threading.Thread(target=run, daemon=True).start()
        return index, True
    with index.build_gate:
        if not index.ready():
            index.build(main)
    return index, False


def keep_current(index, main, store):
    now = time.time()
    if now - index.last_sync >= SYNC_EVERY:
        index.last_sync = now
        index.sync(main)
        if now - index.last_reconcile >= RECONCILE_EVERY:
            index.last_reconcile = now
            index.reconcile(main)
    if store is not None:
        index.refresh_near(store)


# ---------- candidate search ----------

_COLUMNS = 'e.id,e.h,e.hs,e.n,e.w,e.near,e.fol'


def _phrase(text):
    return '"' + text.replace('"', '""') + '"'


def _candidates(index, conn, q, wanted):
    """{id: (h,hs,n,w,near,fol)} from the cheapest indexed lookups, widening only when needed."""
    found = {}
    pad = max(wanted * 4, 40)

    def take(rows):
        for pid, h, hs, n, w, near, fol in rows:
            found.setdefault(pid, (h, hs, n, w, near, fol))

    text, compact, key = q.text, q.compact, q.key
    take(conn.execute(f'SELECT {_COLUMNS} FROM entry e WHERE e.h=? OR e.hs=? OR e.n=? LIMIT 100', (text, compact, key)))
    upper = text + '\U0010ffff'
    take(conn.execute(f'SELECT {_COLUMNS} FROM entry e WHERE e.h>=? AND e.h<? ORDER BY e.near DESC,e.fol DESC LIMIT ?',
                      (text, upper, pad)))
    if compact != text:
        take(conn.execute(f'SELECT {_COLUMNS} FROM entry e WHERE e.hs>=? AND e.hs<? ORDER BY e.near DESC,e.fol DESC LIMIT ?',
                          (compact, compact + '\U0010ffff', pad)))
    take(conn.execute(f'SELECT {_COLUMNS} FROM entry e WHERE e.n>=? AND e.n<? ORDER BY e.near DESC,e.fol DESC LIMIT ?',
                      (key, key + '\U0010ffff', pad)))
    if index.fts:
        match = ' '.join(_phrase(t) + '*' for t in q.tokens)
        take(conn.execute(f'SELECT {_COLUMNS} FROM f_word JOIN entry e ON e.id=f_word.rowid WHERE f_word MATCH ? '
                          'ORDER BY e.near DESC,e.fol DESC,e.id LIMIT ?', (match, pad)))
    else:
        first = max(q.tokens, key=len)
        rows = conn.execute(f'SELECT DISTINCT {_COLUMNS} FROM word JOIN entry e ON e.id=word.id WHERE word.w>=? AND word.w<? '
                            'ORDER BY e.near DESC,e.fol DESC,e.id LIMIT ?', (first, first + '\U0010ffff', pad * 4))
        take(rows)
    return found


def _strong(found, q, floor):
    count = 0
    for h, hs, n, w, near, fol in found.values():
        match = classify(q, h, hs, n, w)
        if match is not None and match[0] >= floor:
            count += 1
    return count


def _contains(index, conn, q, pad):
    """People whose handle or name contains what was typed, whichever separators it used."""
    needles = list(dict.fromkeys(n for n in (q.text, q.compact, q.key) if len(n) >= 3))
    if not needles:
        return []
    found = []
    for needle in needles:
        if index.fts:
            found.extend(conn.execute(
                f'SELECT {_COLUMNS} FROM f_tri JOIN entry e ON e.id=f_tri.rowid WHERE f_tri MATCH ? '
                'ORDER BY e.near DESC,e.fol DESC,e.id LIMIT ?', (_phrase(needle), pad)))
        else:
            like = '%' + re.sub(r'([\\%_])', r'\\\1', needle) + '%'
            found.extend(conn.execute(
                f"SELECT {_COLUMNS} FROM entry e WHERE e.h LIKE ? ESCAPE '\\' OR e.hs LIKE ? ESCAPE '\\' OR e.n LIKE ? ESCAPE '\\' "
                'ORDER BY e.near DESC,e.fol DESC,e.id LIMIT ?', (like, like, like, pad)))
    return found


def _bio(index, conn, q, pad):
    if len(q.compact) < 3:
        return []
    if index.fts:
        match = ' '.join(_phrase(t) + ('*' if len(t) >= 3 else '') for t in q.tokens)
        return conn.execute(f'SELECT {_COLUMNS} FROM f_bio JOIN entry e ON e.id=f_bio.rowid WHERE f_bio MATCH ? '
                            'ORDER BY e.near DESC,e.fol DESC,e.id LIMIT ?', (match, pad)).fetchall()
    clauses = ' AND '.join("e.bio LIKE ? ESCAPE '\\'" for _ in q.tokens)
    args = ['%' + re.sub(r'([\\%_])', r'\\\1', t) + '%' for t in q.tokens]
    return conn.execute(f'SELECT {_COLUMNS} FROM entry e WHERE e.bio IS NOT NULL AND {clauses} '
                        'ORDER BY e.near DESC,e.fol DESC,e.id LIMIT ?', (*args, pad)).fetchall()


def _swaps(token):
    return {token[:i] + token[i + 1] + token[i] + token[i + 2:] for i in range(len(token) - 1) if token[i] != token[i + 1]}


def _fuzzy(index, conn, q):
    """People whose handle or name is one or two letters off.

    Candidates are people sharing one of the rarest three-letter runs of the longest typed word, plus
    people matching a neighbour swap (``cearve`` for ``cerave``). Every candidate is then measured exactly.
    """
    if not index.fts:
        return {}
    ids = set()
    variants = set()
    for token in q.tokens:
        if len(token) >= 4:
            variants |= _swaps(token)
    if len(q.tokens) > 1 or len(q.compact) >= 4:
        variants |= _swaps(q.compact) if len(q.tokens) == 1 else set()
    if variants:
        match = ' OR '.join(_phrase(v) for v in sorted(variants)[:40])
        ids.update(r[0] for r in conn.execute('SELECT rowid FROM f_word WHERE f_word MATCH ? LIMIT 300', (match,)))
        marks = ','.join('?' * len(variants))
        ids.update(r[0] for r in conn.execute(f'SELECT id FROM entry WHERE hs IN ({marks}) LIMIT 300', sorted(variants)))
    probe = max(q.tokens, key=len) if len(q.tokens) > 1 else q.compact
    if len(probe) >= 4:
        grams = list(dict.fromkeys(probe[i:i + 3] for i in range(len(probe) - 2)))
        allowed = allowed_edits(len(probe))
        marks = ','.join('?' * len(grams))
        document = dict(conn.execute(f'SELECT term,doc FROM f_tri_v WHERE term IN ({marks})', grams))
        present = [g for g in grams if document.get(g)]
        # One edit spoils at most three runs, so a close spelling shares at least this many of them.
        need = max(1, len(grams) - 3 * allowed)
        if present and len(present) >= need:
            union = ' UNION ALL '.join('SELECT rowid FROM f_tri WHERE f_tri MATCH ?' for _ in present)
            ids.update(r[0] for r in conn.execute(
                f'SELECT g.rowid FROM ({union}) g JOIN entry e ON e.id=g.rowid GROUP BY g.rowid HAVING count(*)>=? '
                'ORDER BY e.near DESC,e.fol DESC LIMIT ?', (*[_phrase(g) for g in present], need, FUZZY_MAX_CANDIDATES)))
    ids = sorted(ids)[:FUZZY_MAX_CANDIDATES]
    found = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for pid, h, hs, n, w, near, fol in conn.execute(
                f'SELECT {_COLUMNS} FROM entry e WHERE e.id IN ({",".join("?" * len(chunk))})', chunk):
            found[pid] = (h, hs, n, w, near, fol)
    return found


def rank(index, conn, q, limit):
    """Best matches for ``q``: [(score, field, kind, id, near, fol)] sorted, at most ``limit``."""
    wanted = max(limit, LIMIT_DEFAULT)
    pad = max(wanted * 4, 40)
    found = _candidates(index, conn, q, wanted)
    strong = _strong(found, q, WORD_PREFIX)
    if strong < wanted:
        for pid, h, hs, n, w, near, fol in _contains(index, conn, q, pad):
            found.setdefault(pid, (h, hs, n, w, near, fol))
    scored = {}
    for pid, (h, hs, n, w, near, fol) in found.items():
        match = classify(q, h, hs, n, w)
        if match is not None:
            scored[pid] = (*match, near, fol)
    if len(scored) < wanted:
        for pid, h, hs, n, w, near, fol in _bio(index, conn, q, pad):
            if pid not in scored:
                scored[pid] = (BIO, 'bio', 'words', near, fol)
    best = max((v[0] for v in scored.values()), default=0)
    if best < WORD_PREFIX:
        for pid, (h, hs, n, w, near, fol) in _fuzzy(index, conn, q).items():
            if pid in scored and scored[pid][0] >= BIO:
                continue
            cost = fuzzy_cost(q, hs, w)
            if cost:
                scored[pid] = (FUZZY - 60 * min(cost, 2), 'name', 'close', near, fol)
    ordered = sorted(scored.items(), key=lambda item: (-item[1][0], -item[1][3], -item[1][4], item[0]))
    return [(v[0], v[1], v[2], pid, v[3], v[4]) for pid, v in ordered[:limit]]


# ---------- the endpoint ----------

def _snippet(bio, q):
    text = ' '.join(str(bio or '').split())
    folded = fold(text)
    if len(folded) != len(text):
        return text[:90]
    spot = min((folded.find(t) for t in q.tokens if folded.find(t) >= 0), default=0)
    start = max(0, spot - 30)
    piece = text[start:start + 100]
    return ('…' if start else '') + piece + ('…' if start + 100 < len(text) else '')


def search(conn, db_path, params):
    """Answer /api/map/search. ``conn`` is the main database."""
    import map_view as MV
    started = time.perf_counter()
    q = parse(params.get('q', [''])[0])
    try:
        limit = max(1, min(LIMIT_MAX, int(params.get('limit', [LIMIT_DEFAULT])[0])))
    except ValueError:
        raise ValueError('limit must be a whole number') from None
    if params.get('warm', [''])[0] == '1' and not q:
        index, building = ensure(conn, db_path, wait=False)
        return {'rev': 0, 'q': '', 'results': [], 'building': building}
    with MV.reader(db_path, 'closeness') as store:
        rev = store.rev if store else 0
        if not q:
            return {'rev': rev, 'q': '', 'results': [], 'took_ms': 0.0}
        index, building = ensure(conn, db_path, wait=True)
        keep_current(index, conn, store)
        reader = index.reader()
        ranked = rank(index, reader, q, limit * 2)
        ids = [r[3] for r in ranked]
        people = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            for row in conn.execute('SELECT p.id,p.handle,p.name,p.pic_file,p.followers,coalesce(d.degree,0),p.bio '
                                    'FROM people p LEFT JOIN map_person_degree d ON d.person_id=p.id '
                                    f'WHERE p.id IN ({",".join("?" * len(chunk))})', chunk):
                people[row[0]] = row
        owner = ((store.meta('plan', {}) or {}).get('owner_id') or 0) if store else 0
        details = MV._details(store, ids) if store else {}
        positions = {}
        for mode in MV.ML.MODES:
            with MV.reader(db_path, mode) as mode_store:
                if mode_store is None:
                    continue
                for pid, row in MV._details(mode_store, ids).items():
                    positions.setdefault(pid, {})[mode] = {'x': round(MV.ML.coord(row[1]), 7), 'y': round(MV.ML.coord(row[2]), 7),
                                                           'rank': round(row[3], 6), 'cluster': row[4]}
    results = []
    for score, field, kind, pid, near, fol in ranked:
        row = people.get(pid)
        if row is None:
            continue
        d = details.get(pid)
        cls = d[5] if d else 0
        follow = cls // 64
        relation = 'you' if pid == owner else 'mutual' if (follow & 3) == 3 else 'follows_you' if follow & 2 else 'you_follow' if follow & 1 else None
        item = {'id': pid, 'handle': row[1], 'name': row[2], 'pic': f'/img/{pid}' if row[3] else None,
                'followers': row[4], 'source_count': row[5], 'relation': relation,
                'followed': bool(follow & 1), 'follows_me': bool(follow & 2),
                'following_evidence': 'observed' if follow & 1 else 'absent' if follow & 4 else 'unknown',
                'fit': d[6] if d else None, 'status': MV.ML.CODE_STATUS.get((cls % 64) >> 3),
                'lead': MV.ML.is_lead(cls), 'closeness': d[7] / 1000.0 if d else None,
                'pending': pid not in positions, 'positions': positions.get(pid, {}),
                'match': {'field': field, 'kind': kind}}
        if field == 'bio':
            item['snippet'] = _snippet(row[6], q)
        results.append(item)
        if len(results) == limit:
            break
    return {'rev': rev, 'q': q.text, 'results': results, 'building': building,
            'took_ms': round((time.perf_counter() - started) * 1000, 1)}


def main():
    ap = argparse.ArgumentParser(description='Build and inspect the map search index.')
    ap.add_argument('--db', required=True)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('build')
    sub.add_parser('status')
    args = ap.parse_args()
    import db
    conn = db.connect(args.db)
    index = index_for(args.db)
    if args.cmd == 'build':
        print(json.dumps(index.build(conn)))
    else:
        print(json.dumps(index.stats()))


if __name__ == '__main__':
    main()
