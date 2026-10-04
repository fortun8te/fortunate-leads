"""Read-only audit of saved list pages, plus the two safe cleanups for stale leases and duplicate profile jobs.

audit(conn) never writes. cleanup(conn, apply=False) only reports unless apply=True; even then it only requeues an
expired lease (no cursor, list, edge or photo is touched) and cancels duplicate pending profile jobs (rows are kept as
history). Nothing here deletes data.
"""
from datetime import datetime, timedelta, timezone

import accounts
import db

STALE_GRACE = timedelta(minutes=10)   # past lease expiry before cleanup acts: a finishing request keeps its token


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args)]


def audit(conn, limit=20):
    """Lost or double-saved pages, count drift and orphans. Every list is {count, sample}."""
    def found(sql, args=()):
        rows = _rows(conn, sql, args)
        return {'count': len(rows), 'sample': rows[:limit]}

    return {
        # a page said "next is X" and nothing ever asked for X, yet the run ended: a page was lost
        'lost_pages': found(
            "SELECT a.job_id,j.seed,j.direction,j.state AS job_state,a.requested_cursor,a.next_cursor FROM list_page_requests a "
            "JOIN jobs j ON j.id=a.job_id WHERE a.next_cursor IS NOT NULL AND j.state IN ('done','partial') "
            "AND NOT EXISTS(SELECT 1 FROM list_page_requests b WHERE b.job_id=a.job_id AND b.requested_cursor=a.next_cursor)"),
        # an error-state run whose last cursor was never requested: a stalled cursor, kept partial on purpose
        'stalled_cursors': found(
            "SELECT a.job_id,j.seed,j.direction,a.next_cursor FROM list_page_requests a JOIN jobs j ON j.id=a.job_id "
            "WHERE a.next_cursor IS NOT NULL AND j.state='error' "
            "AND NOT EXISTS(SELECT 1 FROM list_page_requests b WHERE b.job_id=a.job_id AND b.requested_cursor=a.next_cursor)"),
        # the run's member count must equal its saved members, and the list's received count must equal the run's
        'member_count_drift': found(
            "SELECT r.job_id,r.member_count,(SELECT count(*) FROM list_members m WHERE m.job_id=r.job_id) AS members "
            "FROM list_runs r WHERE r.member_count!=(SELECT count(*) FROM list_members m WHERE m.job_id=r.job_id)"),
        'received_drift': found(
            "SELECT l.seed,l.direction,l.received,r.member_count FROM lists l JOIN list_runs r ON r.job_id=l.run_job_id "
            "WHERE l.run_job_id IS NOT NULL AND l.received!=r.member_count"),
        # one saved page per (job, requested cursor); more page events than saved pages is a double count
        'double_counted_events': found(
            "SELECT e.job_id,count(*) AS events,(SELECT count(*) FROM list_page_requests r WHERE r.job_id=e.job_id) AS pages "
            "FROM collector_events e WHERE e.outcome='page' AND e.kind='list' AND e.job_id IS NOT NULL GROUP BY e.job_id "
            "HAVING count(*)>(SELECT count(*) FROM list_page_requests r WHERE r.job_id=e.job_id)"),
        'orphan_requests': found(
            "SELECT job_id FROM list_page_requests WHERE job_id NOT IN (SELECT id FROM jobs) GROUP BY job_id"),
        'orphan_edges': found(
            "SELECT count(*) AS n FROM edges e WHERE NOT EXISTS(SELECT 1 FROM people p WHERE p.id=e.person_id) HAVING n>0"),
        'duplicate_people': found(
            "SELECT ig_id,group_concat(handle) AS handles FROM people WHERE ig_id IS NOT NULL GROUP BY ig_id HAVING count(*)>1"),
        'duplicate_profile_jobs': found(
            "SELECT lower(handle) AS handle,count(*) AS n FROM jobs WHERE kind='profile' AND state IN ('queued','leased') "
            "GROUP BY lower(handle) HAVING count(*)>1"),
        'stale_leases': found(
            "SELECT id,kind,seed,direction,handle,lane,leased_until FROM jobs WHERE state='leased' AND leased_until<?",
            (accounts.iso(datetime.now(timezone.utc) - STALE_GRACE),)),
        'lists_without_job': found(
            "SELECT l.seed,l.direction,l.state FROM lists l WHERE l.state IN ('queued','running') AND NOT EXISTS("
            "SELECT 1 FROM jobs j WHERE j.kind='list' AND j.seed=l.seed AND j.direction=l.direction AND j.state IN ('queued','leased'))"),
    }


def release_expired_leases(conn, now=None, grace=STALE_GRACE, apply=False):
    """Requeue jobs whose lease ran out more than `grace` ago. A list keeps its cursor; the attempt is given back."""
    now = now or datetime.now(timezone.utc)
    rows = _rows(conn, "SELECT id,kind,seed,direction,handle,lane,leased_until FROM jobs WHERE state='leased' AND leased_until<?",
                 (accounts.iso(now - grace),))
    if apply and rows:
        conn.executemany("UPDATE jobs SET state='queued',lane=NULL,lease_token=NULL,leased_until=NULL,"
                         "attempts=max(attempts-1,0) WHERE id=? AND state='leased'", [(r['id'],) for r in rows])
        conn.executemany("UPDATE lists SET lane=NULL,prev_lane=coalesce(lane,prev_lane),released_at=?,released_why='lease_expired',"
                         "state=CASE WHEN state='running' THEN 'queued' ELSE state END WHERE seed=? AND direction=? AND lane=?",
                         [(accounts.iso(now), r['seed'], r['direction'], r['lane']) for r in rows if r['kind'] == 'list'])
    return rows


def dedupe_profile_jobs(conn, now=None, apply=False):
    """Pending profile jobs for the same person collapse to one (accounts.coalesce_profile_jobs); the rest are cancelled."""
    now = now or datetime.now(timezone.utc)
    groups = _rows(conn, "SELECT lower(handle) AS handle,min(id) AS keep,count(*) AS n FROM jobs "
                         "WHERE kind='profile' AND state IN ('queued','leased') GROUP BY lower(handle) HAVING count(*)>1")
    if apply:
        for g in groups:
            job = conn.execute('SELECT * FROM jobs WHERE id=?', (g['keep'],)).fetchone()
            accounts.coalesce_profile_jobs(conn, job, now)
    return groups


def cleanup(conn, now=None, apply=False):
    leases = release_expired_leases(conn, now, apply=apply)
    duplicates = dedupe_profile_jobs(conn, now, apply=apply)
    if apply:
        conn.commit()
    return {'applied': bool(apply), 'expired_leases': leases, 'duplicate_profile_groups': duplicates}
