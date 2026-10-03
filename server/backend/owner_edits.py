"""owner edits keep read and write ownership explicit."""

import json
import re
import db
import owner
import owner_notes
import workflows
from .common import Bad, chunks, KEEP


def set_status(conn, pids, status=KEEP, note=KEEP, relationships=KEEP, familiarity=KEEP):
    """Upsert marks. KEEP leaves a field alone; None / '' clears it. A row with neither status nor note is removed."""
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    pids = list(dict.fromkeys(pids))
    affected = set()
    context_changed = relationships is not KEEP or familiarity is not KEEP
    if status is not KEEP or context_changed:
        for chunk in chunks(pids):
            placeholders = ','.join('?' * len(chunk))
            affected.update(r[0] for r in conn.execute(
                f'SELECT DISTINCT person_id FROM current_edges WHERE seed IN (SELECT seed FROM current_edges WHERE person_id IN ({placeholders}) '
                f'UNION SELECT handle FROM people WHERE id IN ({placeholders}))', (*chunk, *chunk)))
    affected.difference_update(pids)
    ts = db.now()
    for pid in dict.fromkeys(pids):
        old = conn.execute('SELECT status,note FROM marks WHERE person_id=?', (pid,)).fetchone()
        if context_changed:
            existing = conn.execute('SELECT * FROM owner_context WHERE person_id=?', (pid,)).fetchone()
            before_context = {'relationships': owner.relationships({'status': old['status'] if old else None,
                               'relationships': json.loads(existing['relationships']) if existing else []}),
                              'familiarity': existing['familiarity'] if existing else None}
            after_context = {'relationships': before_context['relationships'] if relationships is KEEP else relationships,
                             'familiarity': before_context['familiarity'] if familiarity is KEEP else familiarity}
            conn.execute('INSERT OR REPLACE INTO owner_context VALUES(?,?,?,?)',
                         (pid, json.dumps(after_context['relationships']), after_context['familiarity'], ts))
            if before_context != after_context:
                workflows.event(conn, pid, 'relationship', before=before_context, after=after_context)
            if relationships is not KEEP and 'client' not in relationships:
                conn.execute("UPDATE marks SET status=NULL,updated_at=? WHERE person_id=? AND status='client'", (ts, pid))
                for legacy_tag in conn.execute("SELECT tag FROM tags WHERE person_id=? AND source='manual' AND lower(trim(tag))='client'", (pid,)):
                    workflows.event(conn, pid, 'tag', body='Relationship updated', before=legacy_tag['tag'])
                conn.execute("DELETE FROM tags WHERE person_id=? AND source='manual' AND lower(trim(tag))='client'", (pid,))
        # A pipeline change must not erase the recorded client history.
        legacy_client = bool(old and old['status'] == 'client') or bool(conn.execute(
            "SELECT 1 FROM tags WHERE person_id=? AND source='manual' AND lower(trim(tag))='client'", (pid,)).fetchone())
        if relationships is KEEP and (status == 'client' or (status is not KEEP and legacy_client)):
            existing = conn.execute('SELECT * FROM owner_context WHERE person_id=?', (pid,)).fetchone()
            saved = owner.normalize_relationships((json.loads(existing['relationships']) if existing else []) + ['client'])
            conn.execute('INSERT OR REPLACE INTO owner_context VALUES(?,?,?,?)',
                         (pid, json.dumps(saved), existing['familiarity'] if existing else None, ts))

        if status is not KEEP and status != 'client':
            legacy = conn.execute("SELECT tag FROM tags WHERE person_id=? AND source='manual' "
                                  "AND lower(trim(tag))='client'", (pid,)).fetchall()
            for tag in legacy:
                workflows.event(conn, pid, 'tag', body='Relationship updated', before=tag['tag'])
            conn.execute("DELETE FROM tags WHERE person_id=? AND source='manual' AND lower(trim(tag))='client'", (pid,))
        for kind, value in (('status', status), ('note', note)):
            before = old[kind] if old else None
            if value is not KEEP and (value or None) != (before or None):
                workflows.event(conn, pid, kind, before=before, after=value or None)
    sets = [f'{col}=excluded.{col}' for col, v in (('status', status), ('note', note)) if v is not KEEP]
    if sets:
        rows = [(p, None if status is KEEP else status, None if note is KEEP else (note or None), ts) for p in pids]
        conn.executemany('INSERT INTO marks(person_id, status, note, updated_at) VALUES(?,?,?,?) ON CONFLICT(person_id) DO UPDATE SET '
                         + ', '.join(sets + ['updated_at=excluded.updated_at']), rows)
    for chunk in chunks(pids):
        conn.execute(f"DELETE FROM marks WHERE person_id IN ({','.join('?' * len(chunk))}) AND status IS NULL AND coalesce(note,'')=''", chunk)
    if sets or context_changed:
        touch(conn, pids)   # status and note feed the qualifier: the person is re-qualified on the next batch
        for pid in pids:
            owner_notes.invalidate(conn, pid)
    # A mark changes the yield of shared seeds, and a client's own seed becomes a stronger link.
    # One seed can hold tens of thousands of people: queue their local re-rank for the background
    # drain instead of doing it inside this request's write lock.
    db.mark_network_dirty(conn, affected)


def clean_tag(t):
    if not isinstance(t, str):
        raise Bad('tag must be a string')
    t = re.sub(r'\s+', ' ', t).strip()
    if not t or len(t) > 64 or ',' in t:
        raise Bad('tag must be 1-64 characters without commas')
    return t


def manual_tag(t):
    tag = clean_tag(t)
    if tag.casefold() == 'client':
        raise Bad('Use the Client relationship status instead of adding a Client label.')
    return tag


def tag_group(conn, tag, default='signal'):
    row = conn.execute("SELECT grp FROM tags WHERE tag=? ORDER BY source='manual' DESC LIMIT 1", (tag,)).fetchone()
    return row[0] if row else default


def touch(conn, pids):
    """Refresh owner edits ahead of bulk imports; also invalidate the map revision."""
    pids = list(dict.fromkeys(pids))
    ts = db.now()
    conn.executemany('UPDATE people SET updated_at=? WHERE id=?', [(ts, p) for p in pids])
    # The dedicated bounded rules worker keeps a tag/note edit from waiting behind
    # thousands of newly scraped profiles. It clears each entry after saving rules.
    conn.executemany('INSERT OR IGNORE INTO processing_rule_queue(person_id) VALUES(?)',
                     [(p,) for p in pids])


def add_manual(conn, pids, tags):
    for t in tags:
        grp = tag_group(conn, t)
        conn.executemany("INSERT OR REPLACE INTO tags VALUES(?,?,?,'manual')", [(p, t, grp) for p in pids])


def rule_out(conn, r):
    hits = conn.execute("SELECT count(*) FROM tags WHERE tag=? AND source='rule'", (r['tag'],)).fetchone()[0]
    return {'id': r['id'], 'tag': r['tag'], 'grp': r['grp'], 'field': r['field'], 'match': r['match'], 'hits': hits}
