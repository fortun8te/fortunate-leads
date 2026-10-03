"""Reviewed, reversible quarantine of unverified generated research.

Preview is read-only. No model, website, Instagram, settings, private notes,
manual tags or relationships are changed. Quarantine keeps the original content
locally, removes unsupported prose from its active record, and removes only
machine-owned scout badges. Restoring content never grants it factual authority.
"""
import hashlib
import json
import re

import db

VERSION = 'research-quarantine-v1'
REASON = 'Saved research is quarantined until its evidence is checked.'
MAX_BATCH = 1000
PROFILE_FIELDS = ('ig_id', 'handle', 'name', 'bio', 'website', 'followers', 'is_private', 'updated_at')


class ReviewChanged(ValueError):
    pass


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def ensure(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS evidence_quarantine(
        id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INTEGER NOT NULL,
        original_hash TEXT NOT NULL, quarantined_hash TEXT NOT NULL,
        payload TEXT NOT NULL, created_at TEXT NOT NULL, restored_at TEXT, replaced_at TEXT)''')
    conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS evidence_quarantine_active
        ON evidence_quarantine(person_id) WHERE restored_at IS NULL AND replaced_at IS NULL''')


def active(conn, person_id):
    return _table(conn, 'evidence_quarantine') and bool(conn.execute('''SELECT 1 FROM evidence_quarantine
        WHERE person_id=? AND restored_at IS NULL AND replaced_at IS NULL''', (person_id,)).fetchone())


def supersede(conn, person_id):
    """A new research result closes its older quarantine, retaining the backup."""
    if _table(conn, 'evidence_quarantine'):
        conn.execute('''UPDATE evidence_quarantine SET replaced_at=? WHERE person_id=?
            AND restored_at IS NULL AND replaced_at IS NULL''', (db.now(), person_id))


def _state(conn, person_id):
    row = conn.execute('SELECT * FROM deep_research WHERE person_id=?', (person_id,)).fetchone()
    if not row:
        return None
    person = conn.execute('SELECT ' + ','.join(PROFILE_FIELDS) + ' FROM people WHERE id=?',
                          (person_id,)).fetchone()
    verdict = conn.execute('SELECT * FROM verdicts WHERE person_id=?', (person_id,)).fetchone()
    tags = [list(r) for r in conn.execute("SELECT tag,grp FROM tags WHERE person_id=? AND source='auto' "
                                        "AND grp='scout' ORDER BY tag", (person_id,))]
    return {'research': dict(row), 'profile': dict(person) if person else None,
            'verdict': dict(verdict) if verdict else None, 'scout_tags': tags}


def _manifest(operation, entries):
    body = {'version': VERSION, 'operation': operation, 'entries': entries,
            'count': len(entries), 'classification': 'Unverified evidence, not proven false.',
            'preserves': ['Private notes', 'Manual tags', 'Relationships', 'Processing settings']}
    return dict(body, review_hash=_hash(body))


def preview(conn, after_id=0, limit=MAX_BATCH):
    """At most limit primary-key rows; the report contains no generated text."""
    if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= MAX_BATCH:
        raise ValueError('Choose a positive bounded preview range')
    if not _table(conn, 'deep_research'):
        return _manifest('quarantine', [])
    exclusion = ('''AND NOT EXISTS (SELECT 1 FROM evidence_quarantine q
        WHERE q.person_id=d.person_id AND q.restored_at IS NULL AND q.replaced_at IS NULL)'''
        if _table(conn, 'evidence_quarantine') else '')
    entries = []
    for row in conn.execute(f'''SELECT d.person_id FROM deep_research d
            WHERE d.person_id>? AND coalesce(d.verified,0)!=1 {exclusion}
            ORDER BY d.person_id LIMIT ?''', (after_id, limit)):
        pid = row[0]
        state = _state(conn, pid)
        research = state['research']
        text = str(research.get('summary') or '') + ' ' + str(research.get('tags') or '')
        flags = ['unverified_saved_research']
        if re.search(r'\b(?:budget|revenue|turnover|ad spend|spending|million|six[- ]figure|seven[- ]figure)\b', text, re.I):
            flags.append('budget_claim_needs_evidence')
        if re.search(r'\b(?:decision[- ]maker|buying authority|can hire|will hire|founder|owner|ceo)\b', text, re.I):
            flags.append('authority_claim_needs_evidence')
        entries.append({'person_id': pid, 'expected_hash': _hash(state), 'reasons': flags,
                        'can_quarantine': not state['verdict'] or state['verdict'].get('model') != 'leadscout'})
    return _manifest('quarantine', entries)


def _reviewed(manifest, review_hash, operation):
    if not isinstance(manifest, dict) or manifest.get('version') != VERSION or manifest.get('operation') != operation:
        raise ValueError('Unknown review format')
    body = {k: v for k, v in manifest.items() if k != 'review_hash'}
    if review_hash != manifest.get('review_hash') or _hash(body) != review_hash:
        raise ReviewChanged('The preview changed. Review it again before applying.')
    entries = manifest.get('entries')
    if not isinstance(entries, list) or len(entries) > MAX_BATCH:
        raise ValueError('Review must contain a bounded list')
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or type(entry.get('person_id')) is not int or entry['person_id'] <= 0:
            raise ValueError('Invalid review identity')
        if entry['person_id'] in seen or not re.fullmatch(r'[0-9a-f]{64}', str(entry.get('expected_hash') or '')):
            raise ValueError('Invalid review fingerprint')
        seen.add(entry['person_id'])
    return entries


def quarantine(conn, manifest, review_hash):
    """Caller commits/rolls back. Acquire the writer before validating all rows."""
    entries = _reviewed(manifest, review_hash, 'quarantine')
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    states = []
    for entry in entries:
        state = _state(conn, entry['person_id'])
        if not state or _hash(state) != entry['expected_hash'] or active(conn, entry['person_id']):
            raise ReviewChanged('Saved evidence changed. Create a new preview.')
        if state['research'].get('verified') == 1:
            raise ReviewChanged('This research has since been verified.')
        if state['verdict'] and state['verdict'].get('model') == 'leadscout':
            raise ReviewChanged('Review the active ranking fallback before quarantining this research.')
        states.append(state)
    ensure(conn)
    ids = []
    for state in states:
        pid = state['research']['person_id']
        conn.execute("UPDATE deep_research SET summary='',tags='[]',sources='[]',raw=NULL,"
                     'verified=0,verification_reason=? WHERE person_id=?', (REASON, pid))
        conn.execute("DELETE FROM tags WHERE person_id=? AND source='auto' AND grp='scout'", (pid,))
        current_hash = _hash(_state(conn, pid))
        cursor = conn.execute('''INSERT INTO evidence_quarantine
            (person_id,original_hash,quarantined_hash,payload,created_at) VALUES(?,?,?,?,?)''',
            (pid, _hash(state), current_hash, json.dumps(state, ensure_ascii=False), db.now()))
        ids.append(cursor.lastrowid)
    return {'quarantined': len(ids), 'quarantine_ids': ids, 'authority_changed': False}


def restore_preview(conn, limit=MAX_BATCH):
    if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
        raise ValueError('Choose a bounded restore batch')
    if not _table(conn, 'evidence_quarantine'):
        return _manifest('restore', [])
    entries = [{'person_id': r['person_id'], 'quarantine_id': r['id'],
                'expected_hash': r['quarantined_hash'], 'reasons': ['restore_saved_content_only']}
               for r in conn.execute('''SELECT id,person_id,quarantined_hash FROM evidence_quarantine
                WHERE restored_at IS NULL AND replaced_at IS NULL ORDER BY id LIMIT ?''', (limit,))]
    return _manifest('restore', entries)


def restore(conn, manifest, review_hash):
    """Restore reviewed content if unchanged. Unverified badges stay inactive."""
    entries = _reviewed(manifest, review_hash, 'restore')
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    pending = []
    for entry in entries:
        row = conn.execute('''SELECT * FROM evidence_quarantine WHERE id=? AND person_id=?
            AND restored_at IS NULL AND replaced_at IS NULL''',
            (entry.get('quarantine_id'), entry['person_id'])).fetchone()
        current = _state(conn, entry['person_id'])
        if not row or not current or row['quarantined_hash'] != entry['expected_hash'] or _hash(current) != entry['expected_hash']:
            raise ReviewChanged('Quarantined evidence changed. Restore cannot overwrite it.')
        payload = json.loads(row['payload'])
        if _hash(payload) != row['original_hash']:
            raise ReviewChanged('The saved backup changed. Restore stopped.')
        pending.append((row['id'], payload['research']))
    for qid, research in pending:
        columns = [k for k in research if k != 'person_id']
        conn.execute('UPDATE deep_research SET ' + ','.join(k + '=?' for k in columns) + ' WHERE person_id=?',
                     [research[k] for k in columns] + [research['person_id']])
        conn.execute('UPDATE evidence_quarantine SET restored_at=? WHERE id=?', (db.now(), qid))
    return {'restored': len(pending), 'authority_restored': False,
            'message': 'Saved content restored. Its evidence still needs checking.'}
