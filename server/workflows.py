"""Small, local follow-up workflow; no outbound messages or scheduling service."""
import csv
import io
import json
import re
from datetime import date, datetime, timezone
from urllib.parse import parse_qs
import db

S = None


def calendar_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('date must be YYYY-MM-DD')
    date.fromisoformat(value)
    return value


def follow_up(conn, pid):
    row = conn.execute('SELECT due_on,note,completed_at,updated_at FROM followups WHERE person_id=?', (pid,)).fetchone()
    return dict(row) if row else None


def event(conn, pid, kind, body='', before=None, after=None, happened_at=None):
    ts = db.now()
    conn.execute('INSERT INTO activity(person_id,kind,body,before_value,after_value,happened_at,created_at) VALUES(?,?,?,?,?,?,?)',
                 (pid, kind, body, json.dumps(before) if before is not None else None,
                  json.dumps(after) if after is not None else None, happened_at or ts, ts))


def history(conn, pid, q=None):
    q = q or {}
    limit = S.qint(q, 'limit')
    limit = 50 if limit is None else limit
    if not 1 <= limit <= 100:
        raise ValueError('limit must be 1-100')
    args = [pid]
    where = 'person_id=?'
    cursor = q.get('cursor', [''])[0]
    if cursor:
        try:
            stamp, eid = json.loads(cursor)
            if not isinstance(stamp, str) or type(eid) is not int:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('invalid activity cursor') from None
        where += ' AND (happened_at<? OR (happened_at=? AND id<?))'
        args += [stamp, stamp, eid]
    rows = [dict(r) for r in conn.execute(f'SELECT id,kind,body,before_value,after_value,happened_at,created_at FROM activity WHERE {where} ORDER BY happened_at DESC,id DESC LIMIT ?', args + [limit + 1])]
    more = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        for key in ('before_value', 'after_value'):
            row[key] = json.loads(row[key]) if row[key] is not None else None
    return {'rows': rows, 'next_cursor': json.dumps([rows[-1]['happened_at'], rows[-1]['id']]) if more else None}


def api_history(conn, q, b, pid):
    S.person_row(conn, pid)
    return history(conn, pid, q)


def api_interaction(conn, q, b, pid):
    S.person_row(conn, pid)
    kind, body = b.get('kind'), b.get('body')
    if kind not in ('dm', 'reply', 'call', 'meeting', 'note'):
        raise ValueError('invalid interaction kind')
    if not isinstance(body, str) or not body.strip() or len(body) > 5000:
        raise ValueError('body must be 1-5000 characters')
    stamp = b.get('happened_at', db.now())
    if not isinstance(stamp, str):
        raise ValueError('happened_at must be an ISO timestamp with timezone')
    parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('happened_at must include timezone')
    stamp = parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')
    event(conn, pid, kind, body.strip(), happened_at=stamp)
    conn.commit()
    return history(conn, pid)


def api_follow_up(conn, q, b, pid):
    S.person_row(conn, pid)
    # Reserve the write lock before reading: concurrent clicks must not append duplicate events.
    conn.execute('BEGIN IMMEDIATE')
    old = follow_up(conn, pid)
    action = b.get('action')
    if action is not None:
        if action not in ('complete', 'clear') or 'due_on' in b or 'note' in b:
            raise ValueError('use complete or clear without date/note')
        if old and (action == 'clear' or not old['completed_at']):
            if action == 'clear':
                conn.execute('DELETE FROM followups WHERE person_id=?', (pid,))
            else:
                conn.execute('UPDATE followups SET completed_at=?,updated_at=? WHERE person_id=?', (db.now(), db.now(), pid))
            event(conn, pid, 'follow_up_' + ('completed' if action == 'complete' else 'cleared'), before=old, after=follow_up(conn, pid))
    else:
        due = calendar_date(b.get('due_on'))
        note = b.get('note', '')
        if not isinstance(note, str) or len(note) > 500:
            raise ValueError('note must be text up to 500 characters')
        note = note.strip()
        if not old or (old['due_on'], old['note'], old['completed_at']) != (due, note, None):
            conn.execute('INSERT INTO followups VALUES(?,?,?,?,?) ON CONFLICT(person_id) DO UPDATE SET due_on=excluded.due_on,note=excluded.note,completed_at=NULL,updated_at=excluded.updated_at', (pid, due, note, None, db.now()))
            event(conn, pid, 'follow_up_scheduled', before=old, after=follow_up(conn, pid))
    conn.commit()
    return {'follow_up': follow_up(conn, pid)}


def filters(q, where, args):
    mode = q.get('follow_up', [''])[0]
    if not mode:
        return
    if mode not in ('due', 'overdue', 'scheduled', 'completed', 'none'):
        raise ValueError('invalid follow_up filter')
    today = calendar_date(q.get('today', [date.today().isoformat()])[0])
    prefix = 'EXISTS (SELECT 1 FROM followups f WHERE f.person_id=p.id'
    if mode == 'none':
        where.append('NOT ' + prefix + ')')
    elif mode == 'completed':
        where.append(prefix + ' AND f.completed_at IS NOT NULL)')
    else:
        suffix = ' AND f.completed_at IS NULL'
        if mode in ('due', 'overdue'):
            suffix += ' AND f.due_on' + ('<=?' if mode == 'due' else '<?')
            args.append(today)
        where.append(prefix + suffix + ')')


class CsvResponse:
    def __init__(self, data):
        self.data = data


def safe_cell(value):
    if value is None:
        return ''
    if not isinstance(value, str):
        return value
    # Defend formula prefixes hidden behind spaces, BOM and control characters.
    probe = re.sub(r'^[\s\x00-\x1f\ufeff]+', '', value)
    if probe.startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')):
        return "'" + value
    return value


def api_export(conn, q, b):
    if ('ids' in b) == ('query' in b):
        raise ValueError('provide either ids or query')
    if 'ids' in b:
        ids = b['ids']
        if not isinstance(ids, list) or not ids or len(ids) > S.BULK_MAX or any(type(i) is not int or i < 1 for i in ids):
            raise ValueError(f'ids must be 1-{S.BULK_MAX} positive ids')
        # Temporary table avoids SQLite variable limits and preserves exact selected scope.
        conn.execute('CREATE TEMP TABLE IF NOT EXISTS export_ids(id INTEGER PRIMARY KEY)')
        conn.execute('DELETE FROM export_ids')
        conn.executemany('INSERT OR IGNORE INTO export_ids VALUES(?)', [(i,) for i in ids])
        found = conn.execute('SELECT count(*) FROM people JOIN export_ids ON people.id=export_ids.id').fetchone()[0]
        if found != len(set(ids)):
            raise ValueError('some selected people no longer exist; refresh your selection')
        where, args, order = ['p.id IN (SELECT id FROM export_ids)'], [], 'p.id'
    else:
        if not isinstance(b['query'], str) or len(b['query']) > 16000:
            raise ValueError('query must be a URL query string')
        query = parse_qs(b['query'].lstrip('?'))
        where, args = S.lead_filter(query)
        where = [S.NOT_ME] + where
        sort = query.get('sort', ['score'])[0]
        if sort not in S.SORTS:
            raise ValueError('invalid sort')
        order = S.SORTS[sort] + ',p.id'
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    fields = ['id','handle','name','instagram_url','bio','website','followers','following','posts','tier','score','role','status','note','tags','sources','bio_at','bio_src','follow_up_due','follow_up_note','follow_up_completed_at']
    writer.writerow(fields)
    cursor = conn.execute(S.LEAD_SQL + ' WHERE ' + ' AND '.join(where) + ' ORDER BY ' + order, args)
    while True:
        batch = cursor.fetchmany(400)
        if not batch:
            break
        for row in S.lead_rows(conn, batch):
            row['instagram_url'] = 'https://www.instagram.com/' + row['handle'] + '/'
            row['tags'] = '; '.join(t['tag'] for t in row['tags'])
            row['sources'] = '; '.join(row['via'])
            follow = row['follow_up'] or {}
            row.update(follow_up_due=follow.get('due_on'), follow_up_note=follow.get('note'), follow_up_completed_at=follow.get('completed_at'))
            writer.writerow([safe_cell(row.get(k)) for k in fields])
    return CsvResponse(('\ufeff' + output.getvalue()).encode('utf-8'))


def routes(server):
    global S
    S = server
    return [('POST', r'/api/person/(\d+)/follow-up', api_follow_up),
            ('GET', r'/api/person/(\d+)/activity', api_history),
            ('POST', r'/api/person/(\d+)/activity', api_interaction),
            ('POST', r'/api/leads/export', api_export)]
