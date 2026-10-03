"""Connect measured graph yield to already eligible ordinary list jobs.

Eligibility, account ownership, page permissions and pacing stay in accounts.py.
This policy is experimental and off unless explicitly selected. It interleaves
ordinary lists at equal priority, including manually queued priority-zero lists;
positive explicit priority and experiments keep their existing order. Every selected list stays queued until its existing end condition.
"""
import acquisition
import acquisition_planner
import db

POLICY_KEY = 'graph_acquisition_policy'
CURRENT = 'current'
ADAPTIVE = 'observed_yield'
TURN_PREFIX = 'graph_acquisition_turn:'
FRESH_CONSUMED_PREFIX = 'graph_acquisition_fresh_consumed:'


def seed_identity(conn, handle, supplied=None):
    known = {str(row[0]) for row in conn.execute(
        'SELECT ig_id FROM seeds WHERE handle=? UNION SELECT ig_id FROM people WHERE handle=?',
        (handle, handle)) if row[0] is not None and str(row[0]).strip()}
    if supplied is not None:
        known.add(str(supplied))
    ids = {acquisition.normalize_id(value) for value in known}
    return next(iter(ids)) if len(ids) == 1 and None not in ids else None


def enabled(conn):
    return db.get_setting(conn, POLICY_KEY, CURRENT) == ADAPTIVE


def set_policy(conn, value):
    if value not in (CURRENT, ADAPTIVE):
        raise ValueError('Choose current or observed_yield.')
    db.set_setting(conn, POLICY_KEY, value)


def is_automatic(conn, job):
    # A later manual refresh of the same handle is a different job. Historical
    # discovery provenance must never authorize changing its priority.
    return bool(conn.execute(
        "SELECT 1 FROM collection_discovery WHERE job_id=? AND state='queued'",
        (job['id'],)).fetchone())


def _field(row, name, default=None):
    return row[name] if name in row.keys() else default


def _ordinary(row):
    return (row['kind'] == 'list' and not row['experiment_viewer_ig_id']
            and _field(row, 'page_size') is None and row['priority'] <= 0)


def admission_turn(conn, lane):
    value = db.get_setting(conn, TURN_PREFIX + str(lane), 0) if lane else 0
    return value if type(value) is int and 0 <= value < acquisition_planner.EXPLORATION_INTERVAL else 0


def record_admission(conn, lane, job):
    """One bounded counter per lane, written only after an ordinary job is taken.

    Polls, imports, positive-priority manual jobs and explicit trials do not
    advance the experimental exploration cadence.
    """
    if enabled(conn) and _ordinary(job):
        if is_automatic(conn, job) and fresh_turn_due(conn, lane):
            latest = conn.execute("SELECT id FROM collector_events WHERE lane=? "
                "AND kind='list' AND outcome='page' ORDER BY at DESC,id DESC LIMIT 1", (lane,)).fetchone()[0]
            db.set_setting(conn, FRESH_CONSUMED_PREFIX + str(lane), latest)
        db.set_setting(conn, TURN_PREFIX + str(lane),
                       (admission_turn(conn, lane) + 1) % acquisition_planner.EXPLORATION_INTERVAL)


def fresh_turn_due(conn, lane):
    """Four saved ordinary pages earn one fresh-network lease, never a poll."""
    import collection_suggestions
    if not enabled(conn) or not collection_suggestions.enabled(conn):
        return False
    consumed = db.get_setting(conn, FRESH_CONSUMED_PREFIX + str(lane), 0)
    if type(consumed) is not int or consumed < 0:
        return False
    rows = conn.execute(
        "SELECT j.*,c.id AS saved_event_id FROM collector_events c JOIN jobs j ON j.id=c.job_id "
        "WHERE c.lane=? AND c.kind='list' AND c.outcome='page' "
        "ORDER BY c.at DESC,c.id DESC LIMIT ?", (lane, 4)).fetchall()
    return len(rows) == 4 and all(r['saved_event_id'] > consumed and _ordinary(r) and r['priority'] == 0
                                and not is_automatic(conn, r) for r in rows)


def permits_fresh_admission(conn, lane, job):
    return (_ordinary(job) and job['priority'] == 0
            and not is_automatic(conn, job) and fresh_turn_due(conn, lane))


def select(conn, eligible, *, lane=None, last_list=None, yield_list=False, prefer_following=False):
    """Read only: choose from the scheduler's existing filtered SQL window."""
    rows = list(eligible)
    if not rows or not enabled(conn):
        return rows[0] if rows else None
    first = rows[0]
    if not _ordinary(first):
        return first
    if prefer_following and first['direction'] == 'following':
        rows = [r for r in rows if r['kind'] == 'list' and r['direction'] == 'following']
    # The separate automatic seek uses the scheduler's exact eligibility SQL.
    # Preserve requested positive-priority work and trials before exploration.
    protected = [r for r in rows if r['kind'] == 'list' and not _ordinary(r)]
    ordinary = [r for r in rows if _ordinary(r) and r['priority'] == 0
                and not is_automatic(conn, r)]
    automatic = [r for r in rows if _ordinary(r) and is_automatic(conn, r)]
    if ordinary and automatic:
        if not protected and fresh_turn_due(conn, lane):
            return automatic[0]
        # Taking a fresh job consumes the turn. Resume ordinary work even
        # when the default sticky-list ordering puts the fresh list first.
        rows = ordinary
        first = rows[0]
    candidates, by_id = [], {}
    for row in rows[:acquisition_planner.MAX_CANDIDATES]:
        if not _ordinary(row) or row['priority'] != first['priority']:
            continue
        # Keep the scheduler's mandatory quantum yield and productive-direction
        # fallback. Ownership was already checked by SQL; NULL means unclaimed.
        if yield_list and first['id'] != last_list and row['id'] == last_list:
            continue
        if prefer_following and _field(first, 'direction') == 'following' and _field(row, 'direction') != 'following':
            continue
        metrics = acquisition.recent_job_metrics(conn, row['id'], limit=20)
        pages = [m for m in metrics if m['source_kind'] == 'collector_page']
        # These are saved-page yields. The offline request-accounted replay is
        # the acceptance gate; no claim that this covers unlogged lookup costs.
        observed = {'requests': len(pages), 'pages': len(pages),
                    'new_edges': sum(m['new_canonical_edges'] for m in pages),
                    'new_people': sum(m['new_nodes'] for m in pages),
                    'last_admission': max((m['ingested_at'] for m in pages), default='')}
        by_id[row['id']] = row
        candidates.append({'job_id': row['id'], 'observed': observed})
    if not candidates:
        return first
    # Imports and other lanes cannot move this lane's exploration turn.
    turn = admission_turn(conn, lane)
    decision = acquisition_planner.choose(candidates, {'admissions': turn})
    return by_id[decision['job_id']]
