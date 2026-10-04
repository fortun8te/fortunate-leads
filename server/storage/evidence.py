"""Evidence storage ownership; writes remain in the caller transaction."""

import hashlib
import json

from .clock import now
from .handles import norm_handle, normalize_ig_id
from .invalidation import dirty_seed_members


def add_edge(conn, seed, person_id, direction, ts=None, observed=True, flipped=None):
    """flipped: a set that collects seeds whose membership changed, so a caller can re-rank each seed once."""
    seed, ts = norm_handle(seed), ts or now()
    previous = conn.execute('SELECT active FROM edge_evidence WHERE seed=? AND person_id=? AND direction=?',
                            (seed, person_id, direction)).fetchone()
    added = conn.execute('INSERT OR IGNORE INTO edges VALUES(?,?,?,?)',
                         (seed, person_id, direction, ts)).rowcount == 1
    if observed:
        conn.execute('INSERT INTO edge_evidence VALUES(?,?,?,1,?,?) ON CONFLICT(seed,person_id,direction) DO UPDATE SET '
                     'active=CASE WHEN excluded.checked_at>=edge_evidence.checked_at THEN 1 ELSE edge_evidence.active END, '
                     'observed_at=max(coalesce(edge_evidence.observed_at,\'\'),excluded.observed_at), '
                     'checked_at=max(edge_evidence.checked_at,excluded.checked_at)',
                     (seed, person_id, direction, ts, ts))
    current = conn.execute('SELECT active FROM edge_evidence WHERE seed=? AND person_id=? AND direction=?',
                           (seed, person_id, direction)).fetchone()
    if bool(previous and previous[0]) != bool(current and current[0]):
        if flipped is None:
            dirty_seed_members(conn, seed)
        else:
            flipped.add(seed)
    return added


def list_run_complete(conn, job_id):
    """Require a complete cursor chain and a fresh count for negative evidence."""
    run = conn.execute('SELECT * FROM list_runs WHERE job_id=?', (job_id,)).fetchone()
    if (not run or not run['first_page_seen'] or run['total_source'] != 'current_run'
            or type(run['total']) is not int or run['total'] < 0):
        return False
    pages = dict(conn.execute('SELECT requested_cursor,next_cursor FROM list_page_requests WHERE job_id=?', (job_id,)))
    cursor, seen = '', set()
    while cursor is not None:
        if cursor not in pages or cursor in seen:
            return False
        seen.add(cursor)
        cursor = pages[cursor]
    return len(seen) == len(pages) and run['member_count'] >= run['total']


def complete_list_snapshot(conn, seed, direction, job_id, completed_at=None):
    """Record absence only for a complete, tracked run. Call after updating lists, before commit.

    Historical edges are retained. An open/partial/legacy run cannot disprove them.
    Returns person IDs whose effective relationship changed.
    """
    seed = norm_handle(seed)
    row = conn.execute('SELECT state,run_job_id,cursor,received,total FROM lists WHERE seed=? AND direction=?',
                       (seed, direction)).fetchone()
    job = conn.execute('SELECT kind,seed,direction,state FROM jobs WHERE id=?', (job_id,)).fetchone()
    run = conn.execute('SELECT member_count FROM list_runs WHERE job_id=?', (job_id,)).fetchone()
    if (not row or not run or row['state'] != 'done' or row['run_job_id'] != job_id or row['cursor'] is not None
            or not job or job['kind'] != 'list' or job['state'] != 'done' or job['seed'] != seed or job['direction'] != direction
            or row['received'] != run['member_count']):
        return set()
    ts = completed_at or now()
    before = {r[0] for r in conn.execute('SELECT person_id FROM current_edges WHERE seed=? AND direction=?',
                                          (seed, direction))}
    # Every returned member is positive evidence, including pages copied during a cursor resume.
    conn.execute('INSERT INTO edge_evidence(seed,person_id,direction,active,observed_at,checked_at) '
                 'SELECT ?,m.person_id,?,1,m.observed_at,m.observed_at FROM list_members m WHERE m.job_id=? '
                 'ON CONFLICT(seed,person_id,direction) DO UPDATE SET active=1, '
                 'observed_at=max(coalesce(edge_evidence.observed_at,\'\'),excluded.observed_at),checked_at=excluded.checked_at '
                 'WHERE excluded.checked_at>=edge_evidence.checked_at',
                 (seed, direction, job_id))
    if not list_run_complete(conn, job_id):
        after = {r[0] for r in conn.execute('SELECT person_id FROM current_edges WHERE seed=? AND direction=?',
                                             (seed, direction))}
        if before != after:
            dirty_seed_members(conn, seed)
        return before ^ after
    conn.execute('INSERT INTO edge_evidence(seed,person_id,direction,active,observed_at,checked_at) '
                 'SELECT e.seed,e.person_id,e.direction,0,NULL,? FROM edges e '
                 'WHERE e.seed=? AND e.direction=? AND NOT EXISTS '
                 '(SELECT 1 FROM list_members m WHERE m.job_id=? AND m.person_id=e.person_id) '
                 'ON CONFLICT(seed,person_id,direction) DO UPDATE SET active=0,checked_at=excluded.checked_at '
                 'WHERE excluded.checked_at>=edge_evidence.checked_at',
                 (ts, seed, direction, job_id))
    after = {r[0] for r in conn.execute('SELECT person_id FROM current_edges WHERE seed=? AND direction=?',
                                         (seed, direction))}
    if before != after:
        dirty_seed_members(conn, seed)
    return before ^ after


def list_page_key(seed, direction, users, cursor, job_id=None, requested_cursor=None):
    """Replay identity, not proof of a complete snapshot or Instagram event time.

    Unmanaged imports have no collection run identifier. Deduplicate the same member
    batch conservatively, ignoring order and mutable profile fields. A genuine new
    observation of an identical batch requires a new managed collection job.
    """
    if job_id is not None:
        # The output cursor may repeat when Instagram stalls. The request cursor
        # still identifies the distinct page we saved before parking the run.
        return (f'job:{job_id}:request:{requested_cursor}' if requested_cursor is not None
                else f'job:{job_id}:next:{cursor or ""}')
    members = sorted({('id:' + ig_id) if (ig_id := normalize_ig_id(u.get('ig_id'))) is not None
                      else ('handle:' + norm_handle(u['handle'])) for u in users})
    payload = json.dumps([norm_handle(seed), direction, cursor or '', members], separators=(',', ':'))
    return 'import:' + hashlib.sha256(payload.encode()).hexdigest()


def observe_edge(conn, seed, person_id, direction, page_key, job_id, ts):
    return conn.execute('INSERT OR IGNORE INTO edge_observations'
                        '(seed,person_id,direction,page_key,job_id,observed_at) VALUES(?,?,?,?,?,?)',
                        (norm_handle(seed), person_id, direction, page_key, job_id, ts)).rowcount == 1
