"""Bounded profile queue planning with existing collection budgets."""

from datetime import datetime, timedelta, timezone

import accounts
import db
import processing_modes

from .common import LISTS_READ_THROUGH, PLAN_BATCH, iso

EARLY_LISTS = 2

def auto_qualify(conn):
    """Switch AI on only after collection is complete or cannot be continued automatically."""
    if db.get_setting(conn, 'processing_mode') in processing_modes.MODES:
        return False  # Explicit modes never schedule paid/external activation.
    if db.get_setting(conn, 'qualify') or not db.get_setting(conn, 'qualify_auto'):
        return False
    if conn.execute("SELECT 1 FROM jobs WHERE kind='list' AND state IN ('queued','leased') LIMIT 1").fetchone():
        return False
    lists = conn.execute('SELECT seed,direction,state,error,released_why,run_job_id,cursor FROM lists').fetchall()
    if not lists:
        return False
    # repair_lists creates both directions for every seed; do not switch on in
    # the interval before its next pass has created a missing list.
    present = {(row['seed'].lower(), row['direction']) for row in lists}
    if any((seed.lower(), direction) not in present
           for (seed,) in conn.execute("SELECT handle FROM seeds WHERE instr(handle,'~')=0")
           for direction in ('followers', 'following')):
        return False
    reopened = db.get_setting(conn, 'lists_reopened') or {}
    for row in lists:
        if row['state'] == 'done':
            if row['cursor'] is not None or not db.list_run_complete(conn, row['run_job_id']):
                return False
        elif row['state'] == 'private':
            continue
        elif row['state'] == 'partial':
            # Instagram caps and final access denials cannot yield full coverage.
            # Other partials stay recoverable until the bounded repair budget is used.
            terminal = (row['released_why'] == 'private' or
                        (row['error'] or '').startswith('Instagram limited this list;') or
                        reopened.get(f"{row['seed'].lower()}|{row['direction']}", 0) >= db.REOPEN_MAX)
            if not terminal:
                return False
        else:
            return False
    db.set_setting(conn, 'qualify', True)
    conn.commit()
    return True


def plan_priority(lists, prefilter):
    """Likely fit first (prefilter already weighs lists, seed yield, links to Michael and clients, Laya), lists count breaks
    ties; always below READ_PRIORITY (a read Michael asked for goes first)."""
    return max(0, min(100, prefilter or 0)) * 10 + min(lists or 0, 9)


def plan_profiles(conn):
    """Keep the profile queue topped up to the bio budget of the lanes that read bios. Qualification on: everyone above
    `bio_min`, best prefilter first. Still collecting (qualify off, auto on): only the people already in several lists."""
    auto_qualify(conn)
    # Recover jobs parked by older releases after five expired leases. The new
    # ceiling is eight, so this is a one-time requeue per existing job.
    conn.execute("UPDATE jobs SET state='queued',retry_not_before=? WHERE kind='profile' AND state='error' "
                 "AND attempts=5 AND retry_not_before IS NULL",
                 (iso(datetime.now(timezone.utc) + timedelta(minutes=15)),))
    # Bio reads are scraping, not AI: they run whether Qualify is on or off. While lists are still collecting,
    # only people already in several lists get one.
    early = not db.get_setting(conn, 'qualify') and bool(conn.execute(
        "SELECT 1 FROM lists WHERE state IN ('queued','running') LIMIT 1").fetchone())
    now = datetime.now(timezone.utc)
    accts = [a for a in accounts.listing(conn, now) if a['healthy'] and a['role'] in ('bios', 'both')]
    if accts:   # every lane that reads bios brings its own daily budget (0 = no daily number)
        caps = [(a['budget']['profile'], a['today']['profile']) for a in accts]
    else:
        ext = db.get_setting(conn, 'ext') or {}
        used = (ext.get('today') or {}).get('profile', 0) if (ext.get('last_seen') or '').startswith(now.date().isoformat()) else 0
        caps = [(db.get_setting(conn, 'budget')['profile'], used)]
    room = PLAN_BATCH if any(cap == 0 for cap, _ in caps) else min(PLAN_BATCH, sum(max(0, cap - used) for cap, used in caps))
    active = conn.execute("SELECT count(*) FROM jobs WHERE kind='profile' AND state IN ('queued','leased')").fetchone()[0]
    need = room - active
    if need <= 0:
        conn.commit()
        return 0
    # The map's transactionally maintained degree has the same distinct-current-seed
    # meaning as LISTS. Keep the exact read-through query if its backfill was interrupted.
    degree_ready = db.get_setting(conn, 'map_person_degree_v1', False) and db.get_setting(
        conn, 'map_people_present_v1', False) and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_degree_%'").fetchone()[0] == 6
    degree_ready = degree_ready and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_people_%'").fetchone()[0] == 3
    if degree_ready:
        degree_join = ('JOIN' if early else 'LEFT JOIN') + ' map_person_degree d ON d.person_id=p.id'
        degree = 'd.degree' if early else 'coalesce(d.degree,0)'
    else:
        degree_join, degree = '', LISTS_READ_THROUGH
    rows = conn.execute(f"""SELECT * FROM (SELECT p.handle, v.prefilter, {degree} AS n
        FROM people p JOIN verdicts v ON v.person_id=p.id {degree_join}
        WHERE p.bio_at IS NULL AND coalesce(p.is_private,0)=0 AND v.prefilter>=?
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind='profile' AND j.handle=p.handle)
          AND p.handle NOT IN (SELECT handle FROM seeds) AND p.handle!='fortun8te' COLLATE NOCASE AND instr(p.handle, '~')=0
          AND p.id NOT IN (SELECT person_id FROM marks WHERE status='no')) WHERE n>=?
        ORDER BY prefilter DESC, n DESC, handle LIMIT ?""", (db.get_setting(conn, 'bio_min'), EARLY_LISTS if early else 0, need)).fetchall()
    ts = db.now()
    conn.executemany("INSERT INTO jobs(kind, handle, priority, created_at) VALUES('profile',?,?,?)",
                     [(r['handle'], plan_priority(r['n'], r['prefilter']), ts) for r in rows])
    conn.commit()
    return len(rows)
