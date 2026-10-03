"""Metadata-only permit audit. A granted operation is not a counted HTTP request.

Navigation may produce many HTTP requests, cached responses, or none. Only an
explicit transport outcome is recorded; unknown is never promoted to sent.
Callers own transactions and existing permit/hold/budget decisions. No credentials,
URLs, response bodies, profiles or raw permit tokens are persisted here.
"""
from datetime import datetime, timezone
import hashlib
import re

PURPOSES = {'list_page', 'seed_lookup', 'profile_view', 'unknown'}


def ensure(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS request_audit(
      token_hash TEXT PRIMARY KEY,lane TEXT NOT NULL,viewer_ig_id TEXT NOT NULL,
      job_id INTEGER,kind TEXT NOT NULL,purpose TEXT NOT NULL,
      granted_at TEXT NOT NULL,released_at TEXT,sent INTEGER,http_status INTEGER,
      CHECK(kind IN ('list','profile')),CHECK(sent IN (0,1) OR sent IS NULL))''')
    conn.execute('CREATE INDEX IF NOT EXISTS request_audit_time ON request_audit(granted_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS request_audit_viewer_time ON request_audit(viewer_ig_id,granted_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS request_audit_job_time ON request_audit(job_id,granted_at)')


def _hash(token):
    if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{48}', token):
        return None
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def _owner(lane, viewer):
    if not isinstance(lane, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', lane):
        return None
    if isinstance(viewer, bool) or not isinstance(viewer, (str, int)):
        return None
    viewer = str(viewer)
    if not re.fullmatch(r'[1-9][0-9]{0,29}', viewer):
        return None
    return lane, viewer


def _date(value):
    try:
        instant = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        if instant.tzinfo is None:
            return None
        return instant.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def record_grant(conn, lane, viewer, kind, jobid, purpose, token, now):
    token_hash, owner, stamp = _hash(token), _owner(lane, viewer), _date(now)
    if (not token_hash or not owner or not stamp or not isinstance(kind, str) or kind not in ('list', 'profile')
            or not isinstance(purpose, str) or purpose not in PURPOSES or (jobid is not None and (type(jobid) is not int or jobid < 1))
            or purpose in ('list_page', 'seed_lookup') and kind != 'list'
            or purpose == 'profile_view' and kind != 'profile'):
        return False
    metadata = (owner[0], owner[1], jobid, kind, purpose, stamp)
    existing = conn.execute('SELECT lane,viewer_ig_id,job_id,kind,purpose,granted_at '
                            'FROM request_audit WHERE token_hash=?', (token_hash,)).fetchone()
    if existing:
        return tuple(existing) == metadata
    conn.execute('INSERT INTO request_audit(token_hash,lane,viewer_ig_id,job_id,kind,purpose,granted_at) '
                 'VALUES(?,?,?,?,?,?,?)', (token_hash, *metadata))
    return True


def record_release(conn, lane, viewer, token, now, *, sent=None, http_status=None):
    token_hash, owner, stamp = _hash(token), _owner(lane, viewer), _date(now)
    if not token_hash or not owner or not stamp:
        return False
    if sent is not None and type(sent) is not bool:
        return False
    if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
        return False
    # A response proves an HTTP request; contradictory payloads are not evidence.
    if http_status is not None and sent is not True:
        return False
    row = conn.execute('SELECT * FROM request_audit WHERE token_hash=? AND lane=? AND viewer_ig_id=?',
                       (token_hash, *owner)).fetchone()
    if row is None or stamp < row['granted_at']:
        return False
    if row['released_at'] is not None:
        # Retry timestamps can differ. The first release's facts stay immutable.
        return row['sent'] == (None if sent is None else int(sent)) and row['http_status'] == http_status
    conn.execute('UPDATE request_audit SET released_at=?,sent=?,http_status=? WHERE token_hash=? '
                 'AND released_at IS NULL', (stamp, sent, http_status, token_hash))
    return True


def rollup(conn, since, *, viewer=None, until=None):
    """Indexed grant-window counts; operations are never presented as HTTP totals."""
    start = _date(since)
    end = _date(until) if until is not None else None
    if not start or (until is not None and (not end or end <= start)):
        raise ValueError('Use a valid audit time window.')
    where, args = ['granted_at>=?'], [start]
    if end:
        where.append('granted_at<?')
        args.append(end)
    if viewer is not None:
        owner = _owner('audit', viewer)
        if not owner:
            raise ValueError('Use a numeric viewer identity.')
        where.append('viewer_ig_id=?')
        args.append(owner[1])
    rows = conn.execute('''SELECT kind,purpose,count(*) AS granted_operations,
      sum(released_at IS NOT NULL) AS released_operations,
      sum(released_at IS NULL) AS unreleased_operations,
      sum(coalesce(sent=1,0)) AS operations_with_confirmed_http,
      sum(coalesce(sent=0,0)) AS operations_confirmed_not_sent,
      sum(sent IS NULL) AS operations_with_unknown_http,
      sum(coalesce(http_status=429,0)) AS responses_429,
      sum(coalesce(http_status>=400,0)) AS responses_http_error
      FROM request_audit WHERE ''' + ' AND '.join(where) + ' GROUP BY kind,purpose', args)
    return {'since': start, 'until': end, 'groups': [dict(row) for row in rows],
            'scope': 'granted_operations_not_total_http_requests'}
