"""Selected profile references in private notes. Never relationship edges."""
import json
import re
import sqlite3

import owner

MAX_MENTIONS = 12


def ensure(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS note_mentions(
        person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        mentioned_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        token TEXT NOT NULL, PRIMARY KEY(person_id,token))''')


def contains(note, token):
    return bool(re.search(r'(?<![\w@.])' + re.escape(token) + r'(?![\w]|\.[\w.])', note or ''))


def get(conn, pid, note=None):
    if note is None:
        row = conn.execute('SELECT note FROM marks WHERE person_id=?', (pid,)).fetchone()
        note = row[0] if row else ''
    try:
        rows = conn.execute('''SELECT nm.mentioned_id AS person_id,nm.token,p.handle,p.name
            FROM note_mentions nm JOIN people p ON p.id=nm.mentioned_id
            WHERE nm.person_id=? ORDER BY nm.token LIMIT ?''', (pid, MAX_MENTIONS)).fetchall()
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
        return []
    return [dict(r) for r in rows if contains(note, r['token'])]


def save(conn, pid, note, refs=None):
    """Caller owns transaction. Omitted references retain only exact surviving tokens."""
    ensure(conn)
    existing = {r['token']: r['person_id'] for r in get(conn, pid, note)}
    if refs is None:
        refs = [{'person_id': target, 'token': token} for token, target in existing.items()]
    if not isinstance(refs, list) or len(refs) > MAX_MENTIONS:
        raise ValueError('Choose at most 12 profiles in a note')
    accepted = {}
    for ref in refs:
        if not isinstance(ref, dict):
            raise ValueError('Invalid note reference')
        target, token = ref.get('person_id'), ref.get('token')
        if isinstance(target, bool) or not isinstance(target, int) or not isinstance(token, str) or not re.fullmatch(r'@[A-Za-z0-9_.]{1,30}', token):
            raise ValueError('Invalid note reference')
        if not contains(note, token):
            raise ValueError('Referenced profile must appear exactly in the note')
        person = conn.execute('SELECT handle FROM people WHERE id=?', (target,)).fetchone()
        if not person or (token != '@' + person['handle'] and existing.get(token) != target):
            raise ValueError('Referenced profile does not match the selected handle')
        if token in accepted and accepted[token] != target:
            raise ValueError('A note reference cannot identify two profiles')
        accepted[token] = target
    conn.execute('DELETE FROM note_mentions WHERE person_id=?', (pid,))
    conn.executemany('INSERT INTO note_mentions VALUES(?,?,?)', [(pid, target, token) for token, target in accepted.items()])
    return get(conn, pid, note)


def search(conn, query):
    query = (query or '').strip().lstrip('@')[:60]
    columns = '''SELECT p.id,p.handle,p.name,p.pic_file,m.status,oc.relationships
        FROM people p LEFT JOIN marks m ON m.person_id=p.id
        LEFT JOIN owner_context oc ON oc.person_id=p.id'''
    if not query:
        # The owner's saved people first. Both LIMIT scans use integer primary keys.
        rows = conn.execute(columns + ''' WHERE oc.person_id IS NOT NULL
            ORDER BY oc.person_id DESC LIMIT 8''').fetchall()
        if not rows:
            rows = conn.execute(columns + ' ORDER BY p.id DESC LIMIT 8').fetchall()
        return {'people': [_suggestion(r) for r in rows]}
    escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    rows = conn.execute(columns + """ WHERE p.handle LIKE ? ESCAPE '\\' OR p.name LIKE ? ESCAPE '\\'
        ORDER BY CASE WHEN lower(p.handle)=lower(?) THEN 0 WHEN p.handle LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END,
        p.handle COLLATE NOCASE LIMIT 8""", (escaped+'%', '%'+escaped+'%', query, escaped+'%')).fetchall()
    return {'people': [_suggestion(r) for r in rows]}


def _suggestion(row):
    saved = owner.relationships({'relationships': row['relationships'] or '[]', 'status': row['status']})
    if 'client' in saved:
        saved = [value for value in saved if value != 'worked_with']
    return {'id': row['id'], 'handle': row['handle'], 'name': row['name'],
            'pic': f"/img/{row['id']}" if row['pic_file'] else None,
            'badges': [owner.RELATIONSHIPS[value] for value in saved[:2]]}


def _owner_context(conn, pid):
    row = conn.execute('SELECT status FROM marks WHERE person_id=?', (pid,)).fetchone()
    context = conn.execute('SELECT relationships,familiarity FROM owner_context WHERE person_id=?', (pid,)).fetchone()
    tags = [r[0] for r in conn.execute("SELECT tag FROM tags WHERE person_id=? AND source='manual' ORDER BY tag LIMIT 20", (pid,))]
    facts = {'status': row[0] if row else None, 'relationships': json.loads(context[0]) if context else [],
             'familiarity': context[1] if context else None, 'manual_tags': [t[:64] for t in tags]}
    facts['relationships'] = owner.relationships(facts)
    return facts


def context(conn, pid, note):
    refs = get(conn, pid, note)
    subject = conn.execute('SELECT handle FROM people WHERE id=?', (pid,)).fetchone()
    return {'subject': {'person_id': pid, 'handle': subject[0] if subject else None,
                        'owner_saved': _owner_context(conn, pid)},
            'mentions': [dict(person_id=r['person_id'], token=r['token'], handle=r['handle'],
                              name=(r['name'] or '')[:120], owner_saved=_owner_context(conn, r['person_id'])) for r in refs]}


def merge(conn, keep, drop):
    """Keep references attached through a verified database identity merge."""
    ensure(conn)
    conn.execute('UPDATE note_mentions SET mentioned_id=? WHERE mentioned_id=?', (keep, drop))
    conn.execute('''INSERT OR IGNORE INTO note_mentions(person_id,mentioned_id,token)
        SELECT ?,mentioned_id,token FROM note_mentions WHERE person_id=?''', (keep, drop))
    conn.execute('DELETE FROM note_mentions WHERE person_id=?', (drop,))
    # A combined note can be truncated by the existing merge policy.
    row = conn.execute('SELECT note FROM marks WHERE person_id=?', (keep,)).fetchone()
    save(conn, keep, (row[0] if row else '') or '')
