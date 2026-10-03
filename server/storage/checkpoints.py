"""Checkpoints storage ownership; writes remain in the caller transaction."""

from datetime import datetime, timedelta, timezone

from .clock import now, utc_now
from .evidence import list_run_complete
from .handles import norm_handle
from .settings import get_setting, set_setting


def start_list_run(conn, job_id, seed, direction):
    """Attach a run, carrying tracked prefixes only when continuing a saved cursor."""
    if conn.execute('SELECT 1 FROM list_runs WHERE job_id=?', (job_id,)).fetchone():
        return
    old = conn.execute('SELECT cursor,run_job_id FROM lists WHERE seed=? AND direction=?',
                       (seed, direction)).fetchone()
    if old and old['cursor'] and old['run_job_id'] is not None and old['run_job_id'] != job_id:
        prior = old['run_job_id']
        conn.execute('INSERT OR IGNORE INTO list_runs(job_id,first_page_seen,total,total_source) '
                     'SELECT ?,first_page_seen,total,total_source FROM list_runs WHERE job_id=?',
                     (job_id, prior))
        conn.execute('INSERT OR IGNORE INTO list_members SELECT ?,person_id,observed_at FROM list_members WHERE job_id=?',
                     (job_id, prior))
        conn.execute('INSERT OR IGNORE INTO list_page_requests SELECT ?,requested_cursor,next_cursor FROM list_page_requests WHERE job_id=?',
                     (job_id, prior))
    conn.execute('INSERT OR IGNORE INTO list_runs(job_id,member_count) VALUES(?, '
                 '(SELECT count(*) FROM list_members WHERE job_id=?))', (job_id, job_id))
    # Do not count historical edges as a tracked prefix. Legacy received remains visible
    # until a page arrives, but can never certify completion.
    if old and old['cursor']:
        conn.execute('UPDATE lists SET run_job_id=? WHERE seed=? AND direction=?', (job_id, seed, direction))


def queue_list(conn, seed, direction, priority=0, refresh=False, page_size=None, experiment_viewer_ig_id=None):
    seed = norm_handle(seed)
    if conn.execute("SELECT 1 FROM jobs WHERE seed=? AND direction=? AND collection_backend='mobile'", (seed, direction)).fetchone():
        raise ValueError('This list belongs to the mobile collector; its cursor cannot be reused by Chrome')
    if not seed or '~' in seed:
        raise ValueError('invalid Instagram seed handle')
    if direction not in ('followers', 'following'):
        raise ValueError('invalid list direction')
    if experiment_viewer_ig_id is not None:
        if not isinstance(experiment_viewer_ig_id, str) or not experiment_viewer_ig_id.isdigit() or direction != 'following' or page_size is None:
            raise ValueError('following experiment requires a numeric viewer ID and page size')
    if page_size is not None:
        follower_trial = direction == 'followers' and page_size in (25, 50) and experiment_viewer_ig_id is None
        following_trial = direction == 'following' and page_size in (50, 100, 200) and experiment_viewer_ig_id is not None
        if type(page_size) is not int or not (follower_trial or following_trial) or refresh:
            raise ValueError('experimental page size requires a fresh supported list and pinned viewer')
        if (conn.execute('SELECT 1 FROM seeds WHERE handle=?', (seed,)).fetchone()
                or conn.execute('SELECT 1 FROM lists WHERE seed=? AND direction=?', (seed, direction)).fetchone()
                or conn.execute('SELECT 1 FROM jobs WHERE seed=? AND direction=?', (seed, direction)).fetchone()
                or conn.execute('SELECT 1 FROM edges WHERE seed=? LIMIT 1', (seed,)).fetchone()):
            raise ValueError('experimental page size requires a new seed')
    ts = now()
    conn.execute('INSERT OR IGNORE INTO seeds(handle, added_at) VALUES(?,?)', (seed, ts))
    row = conn.execute('SELECT state FROM lists WHERE seed=? AND direction=?', (seed, direction)).fetchone()
    if conn.execute("SELECT 1 FROM jobs WHERE kind='list' AND seed=? AND direction=? AND state IN ('queued','leased')",
                    (seed, direction)).fetchone():
        return False  # duplicate requests never rewind or relabel work already in progress
    if refresh:
        conn.execute('DELETE FROM list_private_denials WHERE seed=? AND direction=?', (seed, direction))
    if row and row['state'] in ('done', 'private') and not refresh:
        return False
    if row and (refresh or row['state'] == 'partial'):
        conn.execute('UPDATE lists SET cursor=NULL, run_job_id=NULL, received=0, lane=NULL WHERE seed=? AND direction=?', (seed, direction))
    conn.execute('INSERT INTO lists(seed, direction, state, updated_at) VALUES(?,?,?,?) '
                 "ON CONFLICT DO UPDATE SET state='queued', error=NULL, updated_at=excluded.updated_at",
                 (seed, direction, 'queued', ts))
    if not conn.execute("SELECT 1 FROM jobs WHERE kind='list' AND seed=? AND direction=? AND state IN ('queued','leased')",
                        (seed, direction)).fetchone():
        job_id = conn.execute('INSERT INTO jobs(kind, seed, direction, priority, created_at, page_size, experiment_viewer_ig_id) VALUES(?,?,?,?,?,?,?)',
                              ('list', seed, direction, priority, ts, page_size, experiment_viewer_ig_id)).lastrowid
        start_list_run(conn, job_id, seed, direction)
    return True


REOPEN_MAX = 2          # bound automatic retries of unverified/short lists


SHORT_RATIO = 0.95      # done with received below this share of the known total = ended early


SHORT_MIN = 20          # ... and at least this many people missing


PARTIAL_RETRY_DELAY = timedelta(days=1)


PARTIAL_RETRY_BATCH = 2  # repair runs every 15 minutes; do not flood Instagram with old partials


def repair_lists(conn, dry=False):
    """Make sure every seed has both lists queued until they are really complete. Returns counts; caller commits.
    - a seed without a followers/following list row gets one (queued);
    - a list in state paused/error/queued/running without a live job gets a job again (cursor kept: it resumes);
    - a list marked done without a complete tracked run is reopened from the start;
    - recoverable terminal partials wait a day and retry in small batches, at most
      REOPEN_MAX times. Explicit Instagram caps and access denials stay parked.
    Cached profile totals never certify coverage."""
    ts = now()
    out = {'added': 0, 'requeued': 0, 'reopened': 0, 'seed_bios': 0, 'partial': 0}
    reopened = get_setting(conn, 'lists_reopened') or {}
    live = {(r[0].lower(), r[1]) for r in conn.execute(
        "SELECT seed, direction FROM jobs WHERE kind='list' AND state IN ('queued','leased')")}
    private_denials = {(r[0].lower(), r[1]) for r in conn.execute(
        'SELECT DISTINCT seed,direction FROM list_private_denials')}
    # a list without a total borrows it from the seed's profile (its followers / following count)
    if not dry:
        conn.execute("UPDATE lists SET total=(SELECT CASE lists.direction WHEN 'followers' THEN p.followers ELSE p.following END "
                     "FROM people p WHERE p.handle=lists.seed) WHERE total IS NULL")
    rows = {(r['seed'].lower(), r['direction']): r for r in conn.execute('SELECT * FROM lists')}
    # done lists nobody can judge yet: read the seed's profile first (its counts are the list totals)
    blind = {r['seed'] for r in rows.values() if r['state'] == 'done' and r['total'] is None}
    have = {r[0] for r in conn.execute("SELECT handle FROM jobs WHERE kind='profile' AND state IN ('queued','leased')")}
    have |= {r[0] for r in conn.execute('SELECT handle FROM people WHERE bio_at IS NOT NULL')}   # read once is enough
    out['seed_bios'] = len(blind - have)
    if not dry:
        for s in blind - have:
            conn.execute('INSERT INTO jobs(kind, handle, priority, created_at) VALUES(?,?,?,?)', ('profile', s, 10000, ts))
    todo = []
    partial_candidates = []
    trial_seeds = {r[0].lower() for r in conn.execute("SELECT DISTINCT seed FROM jobs WHERE experiment_viewer_ig_id IS NOT NULL OR collection_backend='mobile'")}
    for (s,) in conn.execute("SELECT handle FROM seeds WHERE instr(handle, '~')=0 AND NOT EXISTS "
                             "(SELECT 1 FROM collection_discovery d WHERE d.handle=seeds.handle AND d.state='queued')"):
        if s.lower() in trial_seeds:
            continue  # Trials stop at their recorded result; never restart with an ordinary page size.
        for d in ('followers', 'following'):
            r = rows.get((s.lower(), d))
            if r is None:
                out['added'] += 1
                todo.append((s, d, 'add'))
            elif r['state'] in ('paused', 'error', 'queued', 'running', None) and (s.lower(), d) not in live:
                out['requeued'] += 1
                todo.append((r['seed'], d, 'requeue'))
            elif r['state'] == 'done' and (s.lower(), d) not in live and (
                    r['cursor'] is not None or not list_run_complete(conn, r['run_job_id'])):
                if reopened.get(f'{s}|{d}', 0) < REOPEN_MAX:
                    out['reopened'] += 1
                    todo.append((r['seed'], d, 'reopen'))
                else:
                    out['partial'] += 1
                    todo.append((r['seed'], d, 'partial'))
            elif r['state'] == 'partial' and (s.lower(), d) not in live and (
                    (s.lower(), d) not in private_denials and r['released_why'] != 'private' and
                    not (r['error'] or '').startswith('Instagram limited this list;') and
                    reopened.get(f'{s}|{d}', 0) < REOPEN_MAX):
                try:
                    updated = datetime.fromisoformat(r['updated_at'])
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    continue  # uncertain age must not trigger automatic Instagram requests
                if utc_now() - updated >= PARTIAL_RETRY_DELAY:
                    partial_candidates.append((updated, r['seed'], d))
    for _, s, d in sorted(partial_candidates)[:PARTIAL_RETRY_BATCH]:
        out['reopened'] += 1
        todo.append((s, d, 'retry_partial'))
    if dry:
        return out
    for s, d, what in todo:
        if what == 'partial':
            conn.execute("UPDATE lists SET state='partial', error='Complete list coverage could not be verified after retries', updated_at=? WHERE seed=? AND direction=?", (ts, s, d))
            continue
        if what == 'add':
            conn.execute('INSERT OR IGNORE INTO lists(seed, direction, state, updated_at) VALUES(?,?,?,?)', (s, d, 'queued', ts))
        else:
            if what in ('reopen', 'retry_partial'):
                reopened[f'{s}|{d}'] = reopened.get(f'{s}|{d}', 0) + 1
                row = rows[(s.lower(), d)]
                # An interrupted cursor chain can continue with its tracked prefix.
                # A terminal short list or a cursor cycle needs a fresh first page.
                run = (conn.execute('SELECT first_page_seen FROM list_runs WHERE job_id=?',
                                    (row['run_job_id'],)).fetchone() if row['run_job_id'] is not None else None)
                tracked_prefix = bool(run and run[0])
                resume = (what == 'retry_partial' and row['cursor'] is not None and tracked_prefix and
                          not (row['error'] or '').startswith(('Instagram repeated its page cursor.',
                                                                'Earlier pages lack first-page tracking;',
                                                                'The page history does not prove')))
                if not resume:
                    conn.execute('UPDATE lists SET cursor=NULL,run_job_id=NULL,received=0 WHERE seed=? AND direction=?', (s, d))
            conn.execute("UPDATE lists SET state='queued', error=NULL, prev_lane=coalesce(lane,prev_lane), "
                         "lane=NULL, released_at=?, released_why='repair', updated_at=? WHERE seed=? AND direction=?",
                         (ts, ts, s, d))
        if (s.lower(), d) not in live:
            job_id = conn.execute('INSERT INTO jobs(kind, seed, direction, priority, created_at) VALUES(?,?,?,?,?)', ('list', s, d, 0, ts)).lastrowid
            start_list_run(conn, job_id, s, d)
            live.add((s.lower(), d))
    if out['reopened']:
        set_setting(conn, 'lists_reopened', reopened)
    return out
