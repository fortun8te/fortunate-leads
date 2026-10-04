"""Versioned local evidence: bounded rows, cause codes, never browser content.

Lifecycle writers share the caller's transaction. Existing page evidence remains
in collector_events; it is not copied or re-counted when a request is released.
"""
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

CODES = frozenset(('request_acquired', 'request_released', 'request_release_replayed',
                   'request_release_rejected', 'request_expired_unknown',
                   'request_completed', 'request_release_failed', 'request_operator_reviewed'))
REASONS = frozenset(('ack', 'replayed', 'token_mismatch', 'lease_expired',
                     'network', 'unknown', 'not_started', 'completed', 'operator_confirmed'))
MAX_ROWS = 20000
SUMMARY_ROWS = 5000


def utc(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('since must be an ISO UTC timestamp')
    try:
        out = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('since must be an ISO UTC timestamp') from None
    if out.tzinfo is None:
        raise ValueError('since must include a timezone')
    return out.astimezone(timezone.utc)


def token_ref(token):
    return hashlib.sha256(token.encode()).hexdigest()[:16] if isinstance(token, str) and token else None


def record(conn, code, lane=None, kind=None, token=None, ig_id=None,
           job_id=None, direction=None, now=None, reason=None):
    if code not in CODES or reason is not None and reason not in REASONS:
        raise ValueError('unknown pipeline cause code')
    if kind not in (None, 'list', 'profile') or direction not in (None, 'followers', 'following'):
        raise ValueError('unknown pipeline request type')
    # Identity fields are structured IDs, never a client supplied message/url.
    for value in (lane, ig_id):
        if value is not None and (not isinstance(value, str) or len(value) > 64 or
                                  not all(c.isalnum() or c in '_-' for c in value)):
            raise ValueError('invalid pipeline identity')
    if job_id is not None and (type(job_id) is not int or job_id < 1):
        raise ValueError('invalid pipeline job id')
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('pipeline_events_started_at',?)", (json.dumps(at),))
    result = conn.execute('INSERT INTO pipeline_events(at,code,lane,kind,ig_id,job_id,direction,token_ref,reason) '
                         'VALUES(?,?,?,?,?,?,?,?,?)',
                         (at, code, lane, kind, ig_id, job_id, direction, token_ref(token), reason))
    # Amortized maintenance, bounded even under a long unattended run. Read APIs
    # never delete logs; failed caller transactions also roll back their evidence.
    if result.lastrowid % 128 == 0:
        trimmed = conn.execute('DELETE FROM pipeline_events WHERE id<=?', (result.lastrowid - MAX_ROWS,)).rowcount
        trimmed += conn.execute('DELETE FROM pipeline_events WHERE at<?',
                     ((datetime.fromisoformat(at) - timedelta(days=30)).isoformat(),)).rowcount
        if trimmed:
            oldest = conn.execute('SELECT at FROM pipeline_events ORDER BY at LIMIT 1').fetchone()[0]
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('pipeline_events_retained_since',?)", (json.dumps(oldest),))


def safe_code(value):
    return value if isinstance(value, str) and len(value) <= 100 and all(
        c.isalnum() or c in '_-' for c in value) else 'other'


def summary(conn, since=None, lane=None, now=None, ig_id=None):
    now = now or datetime.now(timezone.utc)
    start = utc(since) if since else now - timedelta(hours=2)
    if start > now or start < now - timedelta(days=7):
        raise ValueError('since must be within the last seven days')
    if lane is not None and (not isinstance(lane, str) or len(lane) > 64 or not lane or
                             not all(c.isalnum() or c in '_-' for c in lane)):
        raise ValueError('invalid lane')
    where, params = 'at>=? AND at<=?', [start.isoformat(), now.isoformat()]
    if lane:
        where += ' AND lane=?'
        params.append(lane)
    for identity in (ig_id,):
        if identity is not None and (not isinstance(identity, str) or not identity.isdigit() or len(identity) > 64):
            raise ValueError('invalid Instagram identity')
    life_where = where + (' AND ig_id=?' if ig_id else '')
    page_where = where + (' AND viewer_ig_id=?' if ig_id else '')
    args = [*params, *([ig_id] if ig_id else []), SUMMARY_ROWS + 1]
    lifecycle = [dict(r) for r in conn.execute(
        f'SELECT id,at,code,lane,kind,ig_id,job_id,direction,token_ref,reason FROM pipeline_events '
        f'WHERE {life_where} ORDER BY at DESC,id DESC LIMIT ?', args)]
    pages = [dict(r) for r in conn.execute(
        f'SELECT id,at,lane,job_id,kind,direction,outcome,reason,returned_count,saved_entries,new_links,viewer_ig_id '
        f'FROM collector_events WHERE {page_where} ORDER BY at DESC,id DESC LIMIT ?', args)]
    lifecycle_truncated, pages_truncated = len(lifecycle) > SUMMARY_ROWS, len(pages) > SUMMARY_ROWS
    truncated = lifecycle_truncated or pages_truncated
    lifecycle, pages = lifecycle[:SUMMARY_ROWS], pages[:SUMMARY_ROWS]
    evidence_start = conn.execute("SELECT value FROM settings WHERE key='pipeline_events_started_at'").fetchone()
    trimmed_start = conn.execute("SELECT value FROM settings WHERE key='pipeline_events_retained_since'").fetchone()
    starts = [utc(json.loads(row[0])) for row in (evidence_start, trimmed_start) if row]
    available_start = max(starts) if starts else None
    retention_start = json.loads(trimmed_start[0]) if trimmed_start else None
    retention_complete = bool(available_start and start >= available_start)
    counts = Counter(r['code'] for r in lifecycle)
    acquired, durations = {}, []
    for row in reversed(lifecycle):
        key = (row['lane'], row['ig_id'], row['token_ref'])
        if row['code'] == 'request_acquired':
            acquired[key] = utc(row['at'])
        elif row['code'] == 'request_released' and key in acquired:
            duration = (utc(row['at']) - acquired.pop(key)).total_seconds()
            if duration >= 0:
                durations.append(duration)
    directions = defaultdict(lambda: {'pages': 0, 'returned': 0, 'saved': 0, 'new_links': 0})
    failures = Counter()
    intervals = []
    previous = {}
    for row in reversed(pages):
        if row['outcome'] != 'page':
            failures[safe_code(row['reason'] or row['outcome'])] += 1
            continue
        d = directions[row['direction'] or 'unknown']
        d['pages'] += 1
        for key, field in (('returned', 'returned_count'), ('saved', 'saved_entries'), ('new_links', 'new_links')):
            d[key] += row[field] or 0
        key = (row['lane'], row['job_id'], row['direction'])
        at = utc(row['at'])
        if key in previous:
            delta = (at - previous[key]).total_seconds()
            if 0 < delta < 60:
                intervals.append(delta)
        previous[key] = at
    elapsed = max(1, (now - start).total_seconds())
    total_pages = sum(r['pages'] for r in directions.values())
    # Do not attribute global person first_seen to a lane. This is explicitly a
    # workspace count, regardless of the optional lifecycle/page lane filter.
    profiles = conn.execute('SELECT count(*) FROM people WHERE first_seen>=? AND first_seen<=?',
                            (start.isoformat(), now.isoformat())).fetchone()[0]
    last_stop = next((r for r in lifecycle if r['code'] == 'request_expired_unknown'), None)
    latest_failure = next((r for r in pages if r['outcome'] != 'page'), None)
    if latest_failure:
        latest_failure = {k: latest_failure[k] for k in ('at', 'lane', 'job_id', 'kind', 'direction')}
        source = next(r for r in pages if r['outcome'] != 'page')
        latest_failure['code'] = safe_code(source['reason'] or source['outcome'])
    # Tokens and client messages are never returned. Page/error bodies are not
    # read at all; recent lifecycle rows carry only whitelisted fields.
    return {'v': 1, 'at': now.isoformat(), 'since': start.isoformat(), 'lane': lane, 'ig_id': ig_id,
            'coverage': {'complete': not truncated and retention_complete,
                         'lifecycle_retained_since': retention_start,
                         'lifecycle_available_since': available_start.isoformat() if available_start else None,
                         'lifecycle_complete': retention_complete and not lifecycle_truncated,
                         'collector_complete': not pages_truncated, 'max_rows_per_stream': SUMMARY_ROWS,
                         'verified_identity_only': bool(ig_id), 'lifecycle_rows': len(lifecycle), 'collector_rows': len(pages),
                         'scope': 'latest_rows_in_window' if truncated else 'partial_lifecycle_window' if not retention_complete else 'window'},
            'requests': dict(counts), 'directions': dict(directions), 'failures': dict(failures),
            'workspace_new_profiles': profiles, 'last_stop': last_stop, 'last_failure': latest_failure,
            'cadence': {'active_same_job_samples': len(intervals),
                        'request_ack_samples': len(durations),
                        'request_ack_mean_seconds': round(sum(durations) / len(durations), 3) if durations else None,
                        'active_mean_seconds': round(sum(intervals) / len(intervals), 3) if intervals else None,
                        'observed_pages_per_hour': round(total_pages * 3600 / elapsed, 2),
                        'includes_waits': True}, 'recent': lifecycle[:12]}
