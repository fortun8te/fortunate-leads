"""Small, local follow-up workflow; no outbound messages or scheduling service."""
import json
import re
from datetime import date, datetime, timezone
import db

from backend.common import qint, KEEP, STATUSES, status_in



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
    limit = qint(q, 'limit')
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
    from backend.queries import person_row
    from backend.owner_edits import set_status
    person_row(conn, pid)
    return history(conn, pid, q)


def api_interaction(conn, q, b, pid):
    from backend.queries import person_row
    from backend.owner_edits import set_status
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
    try:
        stamp = parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except OverflowError:
        raise ValueError('happened_at is outside the supported date range') from None
    status = b.get('status', KEEP)
    status = status_in(status) if isinstance(status, str) else status
    if status is not KEEP and status is not None and status not in STATUSES:
        raise ValueError('bad status')
    reminder = validate_follow_up(b['follow_up']) if 'follow_up' in b else None

    # Validate first, then hold the write lock through every change and its history.
    # The response is built before commit so a failed read also rolls back the save.
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        person = person_row(conn, pid)
        event(conn, pid, kind, body.strip(), happened_at=stamp)
        if status is not KEEP:
            set_status(conn, [pid], status=status)
        if reminder is not None:
            apply_follow_up(conn, pid, reminder)
        result = dict(history(conn, pid), status=person['status'] if status is KEEP else status,
                      follow_up=follow_up(conn, pid))
    return result


def validate_follow_up(b):
    """Normalize a reminder instruction without reading or changing stored state."""
    if not isinstance(b, dict):
        raise ValueError('follow_up must be an object')
    action = b.get('action')
    if action in ('complete', 'clear'):
        if 'due_on' in b or 'note' in b:
            raise ValueError('use complete or clear without date/note')
        return {'action': action}
    if action not in (None, 'schedule', 'complete_and_schedule'):
        raise ValueError('invalid follow_up action')
    due = calendar_date(b.get('due_on'))
    note = b.get('note', '')
    if not isinstance(note, str) or len(note) > 500:
        raise ValueError('note must be text up to 500 characters')
    return {'action': action or 'schedule', 'due_on': due, 'note': note.strip()}


def apply_follow_up(conn, pid, instruction):
    """Apply a validated instruction inside the caller's transaction; log actual changes."""
    old = follow_up(conn, pid)
    action = instruction['action']
    if action == 'clear':
        if old:
            conn.execute('DELETE FROM followups WHERE person_id=?', (pid,))
            event(conn, pid, 'follow_up_cleared', before=old)
        return
    if action in ('complete', 'complete_and_schedule'):
        if old and not old['completed_at']:
            ts = db.now()
            conn.execute('UPDATE followups SET completed_at=?,updated_at=? WHERE person_id=?', (ts, ts, pid))
            completed = follow_up(conn, pid)
            event(conn, pid, 'follow_up_completed', before=old, after=completed)
            old = completed
        if action == 'complete':
            return
    due, note = instruction['due_on'], instruction['note']
    if not old or (old['due_on'], old['note'], old['completed_at']) != (due, note, None):
        conn.execute('INSERT INTO followups(person_id,due_on,note,completed_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(person_id) DO UPDATE SET due_on=excluded.due_on,note=excluded.note,completed_at=NULL,updated_at=excluded.updated_at', (pid, due, note, None, db.now()))
        event(conn, pid, 'follow_up_scheduled', before=old, after=follow_up(conn, pid))


def api_follow_up(conn, q, b, pid):
    from backend.queries import person_row
    from backend.owner_edits import set_status
    reminder = validate_follow_up(b)
    with conn:
        # Reserve the write lock before reading: repeated completion stays a no-op.
        conn.execute('BEGIN IMMEDIATE')
        person_row(conn, pid)
        apply_follow_up(conn, pid, reminder)
        result = {'follow_up': follow_up(conn, pid)}
    return result


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


def routes():
    return [('POST', r'/api/person/(\d+)/follow-up', api_follow_up),
            ('GET', r'/api/person/(\d+)/activity', api_history),
            ('POST', r'/api/person/(\d+)/activity', api_interaction)]
