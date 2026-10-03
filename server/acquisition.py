"""Canonical acquisition evidence, independent of collector state and profile reads.

Both A's following entry B and B's follower entry A describe the same A -> B
fact. Events preserve their independent origins. Source dates never come from
import time, and positive evidence never asserts list completeness or friendship.

Call ensure at schema initialization. record_page writes only acquisition tables,
uses a bounded savepoint, and NEVER commits its caller's transaction. Backfilling
ambiguous historical handles is deliberately outside this module's API.
"""
from datetime import datetime, timezone
import hashlib
import json
import re


SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS acquisition_nodes(
       ig_id TEXT PRIMARY KEY,first_observed_at TEXT,last_observed_at TEXT) WITHOUT ROWID''',
    '''CREATE TABLE IF NOT EXISTS acquisition_facts(
       source_ig_id TEXT NOT NULL,target_ig_id TEXT NOT NULL,
       first_observed_at TEXT,last_observed_at TEXT,
       PRIMARY KEY(source_ig_id,target_ig_id),CHECK(source_ig_id!=target_ig_id)) WITHOUT ROWID''',
    '''CREATE INDEX IF NOT EXISTS acquisition_facts_incoming
       ON acquisition_facts(target_ig_id,source_ig_id)''',
    '''CREATE TABLE IF NOT EXISTS acquisition_events(
       event_id TEXT PRIMARY KEY,payload_digest TEXT NOT NULL,source_kind TEXT NOT NULL,
       seed_ig_id TEXT NOT NULL,direction TEXT NOT NULL CHECK(direction IN ('followers','following')),
       observed_at TEXT,ingested_at TEXT NOT NULL,job_id INTEGER,source_ref TEXT,
       returned_members INTEGER NOT NULL,distinct_members INTEGER NOT NULL,
       new_canonical_edges INTEGER NOT NULL,known_canonical_edges INTEGER NOT NULL,
       new_nodes INTEGER NOT NULL,unresolved_members INTEGER NOT NULL,
       duplicate_members INTEGER NOT NULL,ignored_self_members INTEGER NOT NULL)''',
    '''CREATE INDEX IF NOT EXISTS acquisition_events_job
       ON acquisition_events(job_id,ingested_at DESC,event_id DESC)''',
    '''CREATE INDEX IF NOT EXISTS acquisition_events_seed
       ON acquisition_events(seed_ig_id,direction,ingested_at DESC,event_id DESC)''',
    '''CREATE TABLE IF NOT EXISTS acquisition_observations(
       event_id TEXT NOT NULL,source_ig_id TEXT NOT NULL,target_ig_id TEXT NOT NULL,
       member_ig_id TEXT NOT NULL,member_handle TEXT,
       PRIMARY KEY(event_id,source_ig_id,target_ig_id)) WITHOUT ROWID''',
    '''CREATE INDEX IF NOT EXISTS acquisition_observations_pair
       ON acquisition_observations(source_ig_id,target_ig_id,event_id)''',
    '''CREATE TABLE IF NOT EXISTS acquisition_baseline_facts(
       source_ig_id TEXT NOT NULL,target_ig_id TEXT NOT NULL,primed_at TEXT NOT NULL,
       identity_basis TEXT NOT NULL,PRIMARY KEY(source_ig_id,target_ig_id)) WITHOUT ROWID''',
    '''CREATE TABLE IF NOT EXISTS acquisition_totals(
       singleton INTEGER PRIMARY KEY CHECK(singleton=1),events INTEGER NOT NULL DEFAULT 0,
       nodes INTEGER NOT NULL DEFAULT 0,canonical_edges INTEGER NOT NULL DEFAULT 0,
       observations INTEGER NOT NULL DEFAULT 0,unresolved_members INTEGER NOT NULL DEFAULT 0,
       baseline_edges INTEGER NOT NULL DEFAULT 0)''',
)

METRICS = ('returned_members', 'distinct_members', 'new_canonical_edges',
           'known_canonical_edges', 'new_nodes', 'unresolved_members',
           'duplicate_members', 'ignored_self_members')


def ensure(conn):
    """Schema setup without executescript's implicit commit."""
    for statement in SCHEMA:
        conn.execute(statement)
    columns = {row[1] for row in conn.execute('PRAGMA table_info(acquisition_totals)')}
    if 'baseline_edges' not in columns:
        conn.execute('ALTER TABLE acquisition_totals ADD COLUMN baseline_edges INTEGER NOT NULL DEFAULT 0')
    conn.execute('INSERT OR IGNORE INTO acquisition_totals(singleton) VALUES(1)')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='seeds'").fetchone():
        conn.execute('CREATE INDEX IF NOT EXISTS acquisition_seed_identity ON seeds(ig_id,handle) WHERE ig_id IS NOT NULL')


def normalize_id(value):
    """Accept positive digit strings or integers, never booleans/floats/handles."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value)
    if not re.fullmatch(r'[0-9]{1,30}', value) or int(value) == 0:
        return None
    return str(int(value))


def observation_date(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('Observation date must be a timestamp with a timezone, or null.')
    try:
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if instant.tzinfo is None or instant > datetime.now(timezone.utc):
            raise ValueError()
        return instant.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except (ValueError, OverflowError):
        raise ValueError('Observation date must include a timezone and cannot be in the future.') from None


def _text(value, field, maximum, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum or '\x00' in value:
        raise ValueError('Invalid ' + field + '.')
    return value


def _members(members, maximum):
    if type(maximum) is not int or not 1 <= maximum <= 10000:
        raise ValueError('Page limit must be between 1 and 10000 members.')
    if not isinstance(members, (list, tuple)) or len(members) > maximum:
        raise ValueError('Members must be a bounded list.')
    normalized = []
    for member in members:
        if not isinstance(member, dict):
            raise ValueError('Each member must be an object.')
        raw_id = member.get('ig_id')
        ig_id = normalize_id(raw_id)
        handle = member.get('handle')
        if handle is not None:
            handle = _text(handle, 'member handle', 80).strip().lstrip('@').lower()
            if not re.fullmatch(r'[a-z0-9_.]{1,30}', handle):
                raise ValueError('Invalid member handle.')
        if ig_id is None:
            # Preserve malformed-ID distinctions in the replay digest. No
            # unresolved data becomes an identity or directed graph fact.
            if raw_id is not None and not isinstance(raw_id, (str, int, float, bool)):
                raise ValueError('Invalid member ID representation.')
            if isinstance(raw_id, str) and len(raw_id) > 200:
                raise ValueError('Invalid member ID representation.')
        normalized.append({'ig_id': ig_id, 'handle': handle,
                           'unresolved_id': raw_id if ig_id is None else None})
    # Page ordering carries no graph meaning, but repeated rows still affect
    # returned/duplicate metrics and therefore remain in the digest.
    return sorted(normalized, key=lambda row: json.dumps(row, sort_keys=True, allow_nan=False))


def _metrics(event, replayed=False):
    return dict(event_id=event['event_id'], replayed=replayed,
                **{field: event[field] for field in METRICS})


def _touch_node(conn, ig_id, observed_at):
    added = conn.execute('INSERT OR IGNORE INTO acquisition_nodes VALUES(?,?,?)',
                         (ig_id, observed_at, observed_at)).rowcount
    if not added and observed_at is not None:
        conn.execute('''UPDATE acquisition_nodes SET
          first_observed_at=CASE WHEN first_observed_at IS NULL OR first_observed_at>? THEN ? ELSE first_observed_at END,
          last_observed_at=CASE WHEN last_observed_at IS NULL OR last_observed_at<? THEN ? ELSE last_observed_at END
          WHERE ig_id=?''', (observed_at, observed_at, observed_at, observed_at, ig_id))
    return added


def _prior_identity(conn, ig_id):
    """Resolve bounded agreeing legacy aliases; never guess from a reused handle."""
    people = conn.execute('SELECT id,handle FROM people WHERE ig_id=?', (ig_id,)).fetchall()
    pids = {row['id'] for row in people}
    handles = set()
    for person in people:
        if '~' in person['handle']:
            continue
        seed = conn.execute('SELECT ig_id FROM seeds WHERE handle=?', (person['handle'],)).fetchone()
        if seed is None or seed['ig_id'] is None or normalize_id(seed['ig_id']) == ig_id:
            handles.add(person['handle'])
    aliases = conn.execute('''SELECT s.handle,p.id,p.ig_id FROM seeds s
      LEFT JOIN people p ON p.handle=s.handle WHERE s.ig_id=? LIMIT 257''', (ig_id,)).fetchall()
    if len(aliases) > 256:
        raise ValueError('Too many saved identity aliases for bounded baseline priming.')
    for alias in aliases:
        if '~' in alias['handle'] or (alias['ig_id'] is not None and normalize_id(alias['ig_id']) != ig_id):
            continue
        handles.add(alias['handle'])
        if alias['id'] is not None:
            pids.add(alias['id'])
    return sorted(handles), sorted(pids)


def _old_edge(conn, owner_handles, member_pids, direction):
    # At most 256 agreeing aliases, each seek uses edges' primary key. The
    # common case is one handle and one numeric-identity person row.
    for handle in owner_handles:
        for pid in member_pids:
            if conn.execute('SELECT 1 FROM edges WHERE seed=? AND person_id=? AND direction=?',
                            (handle, pid, direction)).fetchone():
                return True
    return False


def prime_existing(conn, *, seed_ig_id, direction, members, max_members=2000):
    """Prime prior knowledge BEFORE mutating the legacy graph for a new page.

    Identity comes from the saved ID registry at priming time, not necessarily
    capture time. Baseline dates remain unknown and the retrospective basis is
    retained separately. No list completion/currentness is inferred. Both old
    orientations are checked; a previously collected inverse edge is not new.
    """
    seed_id = normalize_id(seed_ig_id)
    if seed_id is None or direction not in ('following', 'followers'):
        raise ValueError('Use a numeric source ID and valid list direction.')
    normalized = _members(members, max_members)
    unique = {row['ig_id'] for row in normalized if row['ig_id'] and row['ig_id'] != seed_id}
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    conn.execute('SAVEPOINT acquisition_baseline')
    try:
        identities = {ig_id: _prior_identity(conn, ig_id) for ig_id in unique | {seed_id}}
        nodes = 0
        for ig_id, (handles, pids) in identities.items():
            if handles or pids:
                nodes += _touch_node(conn, ig_id, None)
        added = 0
        for member_id in unique:
            src, dst = (seed_id, member_id) if direction == 'following' else (member_id, seed_id)
            src_handles, src_pids = identities[src]
            dst_handles, dst_pids = identities[dst]
            if not (_old_edge(conn, src_handles, dst_pids, 'following') or
                    _old_edge(conn, dst_handles, src_pids, 'followers')):
                continue
            changed = conn.execute('INSERT OR IGNORE INTO acquisition_facts VALUES(?,?,NULL,NULL)', (src, dst)).rowcount
            if changed:
                added += 1
                conn.execute('INSERT INTO acquisition_baseline_facts VALUES(?,?,?,?)',
                             (src, dst, datetime.now(timezone.utc).isoformat(timespec='microseconds'),
                              'retrospective_consistent_saved_identity'))
        if nodes or added:
            conn.execute('UPDATE acquisition_totals SET nodes=nodes+?,canonical_edges=canonical_edges+?, '
                         'baseline_edges=baseline_edges+? WHERE singleton=1', (nodes, added, added))
        conn.execute('RELEASE acquisition_baseline')
        return {'primed_nodes': nodes, 'primed_edges': added, 'source_dates': 'unknown',
                'identity_basis': 'retrospective_consistent_saved_identity'}
    except BaseException:
        conn.execute('ROLLBACK TO acquisition_baseline')
        conn.execute('RELEASE acquisition_baseline')
        raise


def record_page(conn, *, event_id, source_kind, seed_ig_id, direction, members,
                observed_at, job_id=None, source_ref=None, max_members=2000):
    """Atomically record one page without committing or changing collector state.

    new_nodes counts newly seen member IDs, excluding the page owner. stats().nodes
    includes every registered endpoint. Unknown dates remain null. Replays return
    original event metrics with replayed=True; a reused key with changed evidence
    raises ValueError. Malformed member IDs are unresolved, never handle-matched.
    """
    event_id = _text(event_id, 'event ID', 240)
    source_kind = _text(source_kind, 'source kind', 64)
    if not re.fullmatch(r'[a-z][a-z0-9_-]*', source_kind):
        raise ValueError('Invalid source kind.')
    source_ref = _text(source_ref, 'source reference', 4096, nullable=True)
    seed_id = normalize_id(seed_ig_id)
    if seed_id is None:
        raise ValueError('The source account needs a positive numeric Instagram ID.')
    if direction not in ('followers', 'following'):
        raise ValueError('Direction must be followers or following.')
    if job_id is not None and (type(job_id) is not int or job_id < 1):
        raise ValueError('Invalid job ID.')
    stamp = observation_date(observed_at)
    normalized = _members(members, max_members)
    payload = dict(source_kind=source_kind, seed_ig_id=seed_id, direction=direction,
                   members=normalized, observed_at=stamp, job_id=job_id, source_ref=source_ref)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                       separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    conn.execute('SAVEPOINT acquisition_page')
    try:
        existing = conn.execute('SELECT * FROM acquisition_events WHERE event_id=?', (event_id,)).fetchone()
        if existing is not None:
            if existing['payload_digest'] != digest:
                raise ValueError('This event ID already contains different acquisition evidence.')
            conn.execute('RELEASE acquisition_page')
            return _metrics(existing, replayed=True)
        event = dict(event_id=event_id, **{field: 0 for field in METRICS})
        event['returned_members'] = len(normalized)
        unique = {}
        for member in normalized:
            if member['ig_id'] is None:
                event['unresolved_members'] += 1
            elif member['ig_id'] == seed_id:
                event['ignored_self_members'] += 1
            elif member['ig_id'] in unique:
                event['duplicate_members'] += 1
            else:
                unique[member['ig_id']] = member
        event['distinct_members'] = len(unique)
        added_seed = _touch_node(conn, seed_id, stamp)
        for member_id, member in unique.items():
            event['new_nodes'] += _touch_node(conn, member_id, stamp)
            source_id, target_id = (seed_id, member_id) if direction == 'following' else (member_id, seed_id)
            added = conn.execute('INSERT OR IGNORE INTO acquisition_facts VALUES(?,?,?,?)',
                                 (source_id, target_id, stamp, stamp)).rowcount
            event['new_canonical_edges' if added else 'known_canonical_edges'] += 1
            if not added and stamp is not None:
                conn.execute('''UPDATE acquisition_facts SET
                  first_observed_at=CASE WHEN first_observed_at IS NULL OR first_observed_at>? THEN ? ELSE first_observed_at END,
                  last_observed_at=CASE WHEN last_observed_at IS NULL OR last_observed_at<? THEN ? ELSE last_observed_at END
                  WHERE source_ig_id=? AND target_ig_id=?''',
                  (stamp, stamp, stamp, stamp, source_id, target_id))
            conn.execute('INSERT INTO acquisition_observations VALUES(?,?,?,?,?)',
                         (event_id, source_id, target_id, member_id, member['handle']))
        conn.execute('''INSERT INTO acquisition_events(event_id,payload_digest,source_kind,seed_ig_id,
          direction,observed_at,ingested_at,job_id,source_ref,'''+ ','.join(METRICS) + ''')
          VALUES(''' + ','.join('?' for _ in range(9 + len(METRICS))) + ')',
          (event_id, digest, source_kind, seed_id, direction, stamp,
           datetime.now(timezone.utc).isoformat(timespec='microseconds'), job_id, source_ref,
           *(event[field] for field in METRICS)))
        conn.execute('''UPDATE acquisition_totals SET events=events+1,nodes=nodes+?,
          canonical_edges=canonical_edges+?,observations=observations+?,
          unresolved_members=unresolved_members+? WHERE singleton=1''',
          (added_seed + event['new_nodes'], event['new_canonical_edges'],
           event['distinct_members'], event['unresolved_members']))
        conn.execute('RELEASE acquisition_page')
        return _metrics(event)
    except BaseException:
        conn.execute('ROLLBACK TO acquisition_page')
        conn.execute('RELEASE acquisition_page')
        raise


def _limit(value, maximum=100):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError('Read limit must be between 1 and %d.' % maximum)
    return value


def recent_job_metrics(conn, job_id, limit=20):
    """Newest INGESTED page events, bounded and indexed; source date stays separate."""
    if type(job_id) is not int or job_id < 1:
        raise ValueError('Invalid job ID.')
    limit = _limit(limit)
    rows = conn.execute('''SELECT event_id,source_kind,seed_ig_id,direction,observed_at,
      ingested_at,job_id,source_ref,'''+ ','.join(METRICS) + ''' FROM acquisition_events
      WHERE job_id=? ORDER BY ingested_at DESC,event_id DESC LIMIT ?''', (job_id, limit))
    return [dict(row) for row in rows]


def stats(conn):
    """Maintained O(1) totals; these are saved evidence, not daily live throughput."""
    row = conn.execute('SELECT events,nodes,canonical_edges,observations,unresolved_members,baseline_edges '
                       'FROM acquisition_totals WHERE singleton=1').fetchone()
    return dict(row)


def neighbors(conn, ig_id, direction, *, after=None, limit=100):
    """Read direct canonical evidence in either orientation, without a new request.

    Cursor order is numeric-ID text order (stable, not numeric ranking). This
    returns known neighbors only; it makes no completeness or currentness claim.
    """
    ig_id = normalize_id(ig_id)
    if ig_id is None or direction not in ('followers', 'following'):
        raise ValueError('Use a numeric Instagram ID and a valid list direction.')
    limit = _limit(limit, 1000)
    if after is not None:
        after = normalize_id(after)
        if after is None:
            raise ValueError('Invalid neighbor cursor.')
    owner, member = ('source_ig_id', 'target_ig_id') if direction == 'following' else ('target_ig_id', 'source_ig_id')
    rows = conn.execute('SELECT ' + member + ' AS ig_id,first_observed_at,last_observed_at '
                        'FROM acquisition_facts WHERE ' + owner + '=? AND ' + member + '>? '
                        'ORDER BY ' + member + ' LIMIT ?', (ig_id, after or '', limit)).fetchall()
    return {'members': [dict(row) for row in rows],
            'next_after': rows[-1]['ig_id'] if len(rows) == limit else None,
            'coverage': 'observed_edges_only'}
