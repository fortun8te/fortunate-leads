"""CollectionService owns one workspace and its request dependencies."""

import json
import re
import secrets
import time
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import accounts
import edge_benchmark_api
import mobile_collector
import control
import collection_suggestions
import collection_progress
import db
import owner
import engine_start
import browser_startup
import onboarding
import processing_modes
import laya
import llm
import rules
from .common import (
    status_in,
    Bad,
    NotFound,
    text_or_none,
    utc,
    iso,
    workspace_cooldown,
    count_or_none,
    metric_int,
    clean_iso,
    clean_rate,
    chunks,
    csv,
    qint,
    EXT_ORIGIN,
    POSITIVE,
    LEASE_MIN,
    PROFILE_MAX_ATTEMPTS,
    FOLLOWING_TRIAL_PAGE_LIMIT,
    NOT_ME,
    RATE_WINDOW,
    OBSERVED_RATE_WINDOW,
    SNOWBALL_MAX,
)
from .evidence import me_handle, network_snapshot
from .qualification import external_candidates_sql


class CollectionService:

    def __init__(self, config, cache, qualification, processing, local_services):
        self.config = config
        self.cache = cache
        self.qualification = qualification
        self.processing = processing
        self.local_services = local_services
        self.background_probe = None
        self.host_operations_allowed = config.host_operations_allowed
        self.local_coverage_lock = threading.Lock()
        self.local_coverage_cache = {}

    def permit_capable(self, version):
        parts = str(version or '').split('.')
        return len(parts) == 3 and all(p.isdigit() for p in parts) and tuple(map(int, parts)) >= (3, 9, 17)

    def collector_request(self, conn, q, b, job=None, allow_disabled=False):
        """Check a client's assigned backend before it may touch identity or results."""
        supplied = q.get('backend', ['chrome'])[0]
        backend = b.get('backend', supplied)
        if 'backend' in b and 'backend' in q and backend != supplied:
            raise Bad('conflicting collector backends')
        lane = accounts.lane_of(q, b)
        viewer = (accounts.account_from(q, b) or {}).get('ig_id')
        try:
            mobile_collector.validate_client(conn, lane, backend, viewer, allow_disabled=allow_disabled)
        except ValueError as exc:
            raise Bad(str(exc)) from None
        if job is None and b.get('job_id') is not None:
            job = conn.execute('SELECT * FROM jobs WHERE id=?', (b['job_id'],)).fetchone()
        if job is not None:
            if job['collection_backend'] != backend:
                raise Bad('job belongs to a different collector backend')
            if backend == 'mobile' and (job['kind'] != 'list' or job['backend_lane'] != lane
                                        or job['backend_viewer_ig_id'] != viewer):
                raise Bad('mobile job belongs to a different lane or viewer')
        if (backend == 'mobile' and allow_disabled and b.get('action') != 'release'
                and db.get_setting(conn, 'mobile_backend_enabled', False) is not True
                and (job is None or self.stale_lease(conn, job, q, b))):
            raise Bad('disabled mobile backend accepts only its outstanding matching result')
        return backend

    def collector_capable(self, row):
        return bool(row and (row['collection_backend'] == 'mobile' or self.permit_capable(row['version'])))

    def ext_state(self, conn, row=None):
        """What one lane is told: paused = workspace pause or this account paused; budget = its own or the global one."""
        cooling = workspace_cooldown(conn, datetime.now(timezone.utc))
        reviewed = db.get_setting(conn, 'instagram_scraping_warning_reviewed') or {}
        mobile = row is not None and row['collection_backend'] == 'mobile'
        upgrade = not self.collector_capable(row)
        return {'paused': accounts.paused_for(conn, row), 'budget': accounts.budget_of(conn, row),
                **({'scraping_warning_reviewed_at': reviewed[row['lane_id']]} if row is not None and row['lane_id'] in reviewed else {}),
                'stages': {k: not cooling and not upgrade and k not in control.paused_kinds(conn)
                           and (not mobile or (k == 'list' and not row['hold'])) for k in ('list', 'profile')},
                **({'backend': 'mobile', 'backend_cursor_isolated': True} if mobile else {}),
                **({'upgrade_required': True, 'minimum_version': '3.9.17', 'message': 'Reload the extension in this Chrome profile before collecting.'} if upgrade else {}),
                **({'cooldown_until': cooling} if cooling else {})}

    def benchmark_next(self, conn, q, b):
        try:
            return edge_benchmark_api.next_task(conn, q, b, background_probe=self.background_probe)
        except ValueError as exc:
            raise Bad(str(exc)) from None

    def benchmark_permit(self, conn, q, b):
        try:
            return edge_benchmark_api.permit(conn, q, b, background_probe=self.background_probe)
        except ValueError as exc:
            raise Bad(str(exc)) from None

    def benchmark_result(self, conn, q, b):
        try:
            return edge_benchmark_api.result(conn, q, b)
        except ValueError as exc:
            raise Bad(str(exc)) from None

    def ext_request(self, conn, q, b):
        """One workspace-wide request permit; release only accepts its original lane/token."""
        lane = accounts.lane_of(q, b)
        now = datetime.now(timezone.utc)
        if b.get('action') == 'release':
            self.collector_request(conn, q, b, allow_disabled=True)
            token = b.get('token')
            if not isinstance(token, str) or not token:
                raise Bad('request token required')
            return accounts.request_permit(conn, lane, token=token, now=now)
        if b.get('action') != 'acquire' or b.get('kind') not in ('list', 'profile'):
            raise Bad('request action and kind required')
        conn.execute('BEGIN IMMEDIATE')
        if edge_benchmark_api.active(conn):
            conn.rollback()
            return {'granted': False, 'job': None, 'paused': True, 'stages': {'list': False, 'profile': False}, 'wait_ms': 15000, 'reason': 'benchmark_exclusive'}
        backend = self.collector_request(conn, q, b)
        row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()
        if not self.collector_capable(row) or accounts.paused_for(conn, row) or workspace_cooldown(conn, now) or b['kind'] in control.paused_kinds(conn):
            conn.rollback()
            return {'granted': False, 'wait_ms': 15000}
        job = conn.execute('SELECT * FROM jobs WHERE id=?', (b.get('job_id'),)).fetchone()
        if job is None or self.stale_lease(conn, job, q, b) or job['kind'] != b['kind'] or not job['leased_until'] or utc(job['leased_until']) <= now:
            conn.rollback()
            return {'granted': False, 'stale': True, 'wait_ms': 15000}
        if (job['kind'] == 'profile' and accounts.main_bios_reserved(conn, row) or
                job['kind'] == 'list' and job['direction'] == 'followers'
                and (accounts.follower_route_wait(conn, row, now) or db.get_setting(conn, 'follower_lists') is False)):
            conn.execute("UPDATE jobs SET state='queued',lane=NULL,leased_until=NULL,lease_token=NULL,"
                         "attempts=max(attempts-1,0) WHERE id=?", (job['id'],))
            conn.commit()
            return {'granted': False, 'stale': True, 'wait_ms': 15000}
        if job['experiment_viewer_ig_id']:
            version = str(row['version'] or '').split('.')
            viewer = accounts.account_from(q, b) or {}
            if (row['is_main'] or row['ig_id'] != job['experiment_viewer_ig_id']
                    or viewer.get('ig_id') != job['experiment_viewer_ig_id']
                    or len(version) != 3 or not all(p.isdigit() for p in version)
                    or tuple(map(int, version)) < (3, 9, 21)):
                conn.rollback()
                return {'granted': False, 'stale': True, 'wait_ms': 15000}
        # Keep a valid job alive while its account waits fairly for the shared request slot.
        conn.execute('UPDATE jobs SET leased_until=? WHERE id=?',
                     (iso(now + timedelta(minutes=LEASE_MIN)), job['id']))
        mobile_clock = 'mobile_request_after:' + str(row['ig_id'])
        if backend == 'mobile':
            deadline = db.get_setting(conn, mobile_clock)
            if deadline:
                parsed = clean_iso(deadline)
                if parsed is None:
                    raise Bad('Mobile request timing is invalid; collection remains stopped')
                remaining = (utc(parsed) - now).total_seconds()
                if remaining > 0:
                    conn.commit()
                    return {'granted': False, 'wait_ms': max(1000, int(remaining * 1000)), 'lease_renewed': True}
        permit = accounts.request_permit(conn, lane, kind=b['kind'], now=now, commit=backend != 'mobile')
        if backend == 'mobile':
            # Charge a granted mobile attempt conservatively in the same transaction
            # as its permit. Native clients cannot reset this via stale heartbeats.
            if permit.get('granted'):
                db.set_setting(conn, mobile_clock, iso(now + timedelta(seconds=12)))
                today = accounts.jload(row['today'], {}) or {}
                if not row['last_seen'] or accounts.local_day(utc(row['last_seen'])) != accounts.local_day(now):
                    today = {}
                today[b['kind']] = today.get(b['kind'], 0) + 1
                accounts.touch(conn, lane, today=json.dumps(today))
            conn.commit()
        return dict(permit, lease_renewed=True)

    def ext_next(self, conn, q, b):
        """Leases per lane (see accounts.py): never one job to two lanes, lists stick to their lane while it is healthy."""
        lane, ts, now = accounts.lane_of(q, b), db.now(), datetime.now(timezone.utc)
        kinds = [k for k in csv(q, 'kinds') if k in ('list', 'profile')] or ['list', 'profile']
        conn.execute('BEGIN IMMEDIATE')
        if edge_benchmark_api.active(conn):
            conn.rollback()
            return {'granted': False, 'job': None, 'paused': True, 'stages': {'list': False, 'profile': False}, 'wait_ms': 15000, 'reason': 'benchmark_exclusive'}
        backend = self.collector_request(conn, q, b)
        if backend == 'mobile':
            kinds = [kind for kind in kinds if kind == 'list']
        # A queue poll cannot clear an observed login/security page. A later
        # heartbeat must confirm the tab is usable before this lane takes work.
        previous = conn.execute('SELECT state,hold FROM accounts WHERE lane_id=?', (lane,)).fetchone()
        tab_hold = previous['hold'] if previous and previous['state'] in ('tab_login', 'tab_challenge') else None
        row = accounts.touch(conn, lane, accounts.account_from(q, b),
                             **({'hold': tab_hold} if backend == 'chrome' else {}),
                             **({'version': b.get('version') or q['version'][0]} if b.get('version') or q.get('version') else {}))
        # An expired tab lease is a transient failure. Handle it before account
        # handoff, which otherwise clears an offline lane's expired lease and loses
        # its retry count.
        expired = conn.execute("SELECT id,attempts FROM jobs WHERE kind='profile' AND state='leased' AND leased_until<?",
                               (ts,)).fetchall()
        for stuck in expired:
            attempts = stuck['attempts']
            final = attempts >= PROFILE_MAX_ATTEMPTS
            retry = None if final else iso(now + timedelta(minutes=min(2 ** max(attempts - 1, 0), 60)))
            conn.execute("UPDATE jobs SET state=?,leased_until=NULL,lane=NULL,lease_token=NULL,retry_not_before=? WHERE id=?",
                         ('error' if final else 'queued', retry, stuck['id']))
        accounts.release(conn, now)
        if backend == 'chrome':
            accounts.reopen_private_for_viewer(conn, row, now)
        st = self.ext_state(conn, row)
        if st.get('upgrade_required'):
            conn.commit()
            return dict(st, job=None)

        if st['paused'] or row['hold']:
            conn.commit()
            return dict(st, job=None, cooldown_until=st.get('cooldown_until'))
        if st.get('cooldown_until'):
            conn.commit()
            return dict(st, job=None)
        stopped = control.paused_kinds(conn)   # a stage paused on the control strip hands out none of its jobs
        st['stages'] = {'list': 'list' not in stopped, 'profile': backend == 'chrome' and 'profile' not in stopped}
        kinds = [k for k in accounts.kinds_for(conn, row, kinds, now) if k not in stopped]
        if not kinds:
            conn.commit()
            return dict(st, job=None)
        if 'list' in kinds and backend == 'chrome':
            collection_suggestions.queue_when_idle(conn, row, now)
        job = accounts.pick_job(conn, lane, kinds, now, allow_page_size=True)
        if not job:
            conn.commit()
            return dict(st, job=None)
        token = secrets.token_hex(16)
        conn.execute("UPDATE jobs SET state='leased', leased_until=?, attempts=attempts+1, lane=?, lease_token=?, viewer_ig_id=? WHERE id=?",
                     (iso(now + timedelta(minutes=LEASE_MIN)), lane, token, row['ig_id'], job['id']))
        accounts.took(conn, lane, job)
        if job['kind'] == 'list':
            db.start_list_run(conn, job['id'], job['seed'], job['direction'])
            conn.execute("UPDATE lists SET state='running', updated_at=? WHERE seed=? AND direction=?",
                         (ts, job['seed'], job['direction']))
            seed = conn.execute('SELECT coalesce(s.ig_id, p.ig_id) AS ig_id FROM seeds s LEFT JOIN people p ON p.handle=s.handle '
                                'WHERE s.handle=?', (job['seed'],)).fetchone()
            lst = conn.execute('SELECT cursor, received FROM lists WHERE seed=? AND direction=?', (job['seed'], job['direction'])).fetchone()
            out = {'id': job['id'], 'kind': 'list', 'seed': job['seed'], 'ig_id': seed and seed['ig_id'],
                   'direction': job['direction'], 'cursor': lst and lst['cursor'], 'received': (lst and lst['received']) or 0,
                   'page_size': job['page_size'], 'experiment_viewer_ig_id': job['experiment_viewer_ig_id']}
        else:
            p = conn.execute('SELECT ig_id FROM people WHERE handle=?', (job['handle'],)).fetchone()
            out = {'id': job['id'], 'kind': 'profile', 'handle': job['handle'], 'ig_id': p and p['ig_id']}
        conn.commit()
        out['lease_token'] = token
        if backend == 'mobile':
            out.update(collection_backend='mobile', viewer_ig_id=row['ig_id'],
                       backend_lane=job['backend_lane'], backend_viewer_ig_id=job['backend_viewer_ig_id'])
        return dict(st, job=out)

    def check_seed_identity(self, conn, handle, ig_id):
        """A seed's history belongs to one Instagram account: refuse a different ID for a known handle."""
        known = {str(r[0]) for r in conn.execute(
            'SELECT ig_id FROM seeds WHERE handle=? AND ig_id IS NOT NULL '
            'UNION SELECT ig_id FROM people WHERE handle=? AND ig_id IS NOT NULL', (handle, handle)) if r[0]}
        if known - {ig_id}:
            raise Bad('seed account identity changed; existing relationship history cannot be reassigned')

    def stale_lease(self, conn, job, q, b):
        """Old outbox messages cannot alter a job after another lane/lease has taken it."""
        if not job:
            return bool(b.get('job_id'))
        if job['state'] != 'leased':
            return True
        lane = accounts.lane_of(q, b)
        if job['lane'] and job['lane'] != lane:
            return True
        return bool(job['lease_token'] and b.get('lease_token') != job['lease_token'])

    def trial_event_identity_matches(self, conn, job, q, b):
        """Read before accounts.touch: mismatched identities cannot stop a cohort."""
        viewer = (accounts.account_from(q, b) or {}).get('ig_id')
        registered = conn.execute('SELECT ig_id FROM accounts WHERE lane_id=?',
                                  (accounts.lane_of(q, b),)).fetchone()
        return bool(viewer and registered and registered['ig_id'] == viewer
                    and (not job or not job['viewer_ig_id'] or job['viewer_ig_id'] == viewer))

    def stop_following_trial(self, conn, reason, ts, job_ids=None):
        """Park the latest pinned cohort, preserving all saved pages and real holds."""
        if job_ids is None:
            trial = db.get_setting(conn, 'page_experiment_latest') or {}
            if not isinstance(trial, dict) or trial.get('direction') != 'following':
                return []
            job_ids = trial.get('job_ids') or []
        if not isinstance(job_ids, (list, tuple)):
            return []
        stopped = []
        for job_id in dict.fromkeys(i for i in job_ids if type(i) is int and i > 0):
            job = conn.execute("SELECT * FROM jobs WHERE id=? AND kind='list' AND direction='following' "
                               "AND experiment_viewer_ig_id IS NOT NULL", (job_id,)).fetchone()
            if not job:
                continue
            lst = conn.execute('SELECT * FROM lists WHERE seed=? AND direction=?',
                               (job['seed'], job['direction'])).fetchone()
            if not lst or (job['state'] == 'done' and db.list_run_complete(conn, job_id)):
                continue
            if lst['released_why'] == 'trial_stopped' and job['state'] == 'error':
                continue
            conn.execute("UPDATE jobs SET state='error',leased_until=NULL,lane=NULL,lease_token=NULL,"
                         "retry_not_before=NULL WHERE id=?", (job_id,))
            conn.execute("UPDATE lists SET state=?,error=?,prev_lane=coalesce(lane,prev_lane),lane=NULL,"
                         "released_at=?,released_why='trial_stopped',updated_at=? WHERE seed=? AND direction=?",
                         ('partial' if lst['received'] else 'paused',
                          'Following page trial stopped: ' + str(reason)[:400], ts, ts, job['seed'], job['direction']))
            stopped.append(job_id)
        return stopped

    def ext_list_page(self, conn, q, b):
        """Import one list page atomically: any rejection or failure leaves no partial writes behind."""
        try:
            return self._ext_list_page(conn, q, b)
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise

    def _ext_list_page(self, conn, q, b):
        # Hold the write transaction from lease validation to import: handoff cannot race this result.
        job_id = b.get('job_id')
        if job_id is not None and (isinstance(job_id, bool) or not isinstance(job_id, (int, str))):
            raise Bad('invalid job_id')
        seed_ig_id = b.get('ig_id')
        if seed_ig_id is not None:
            if isinstance(seed_ig_id, bool) or not isinstance(seed_ig_id, (str, int)) or not str(seed_ig_id).strip():
                raise Bad('invalid seed ig_id')
            seed_ig_id = str(seed_ig_id)
        conn.execute('BEGIN IMMEDIATE')
        job = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        backend = self.collector_request(conn, q, b, job=job, allow_disabled=True)
        if backend == 'mobile' and job is None:
            raise Bad('mobile list result requires a bound job')
        seed = db.norm_handle(b.get('seed') or (job and job['seed']))
        direction = b.get('direction') or (job and job['direction'])
        if not seed or direction not in ('followers', 'following'):
            raise Bad('seed and direction required')
        old = conn.execute('SELECT * FROM lists WHERE seed=? AND direction=?', (seed, direction)).fetchone()
        received = (old and old['received']) or 0
        if job_id is not None and not job:
            raise Bad('unknown list job')
        if job and (job['kind'] != 'list' or seed != job['seed'] or direction != job['direction']):
            raise Bad('page does not match its list job')
        if seed_ig_id is not None:
            self.check_seed_identity(conn, seed, seed_ig_id)
        for field in ('requested_cursor', 'next_cursor'):
            if b.get(field) is not None and not isinstance(b[field], str):
                raise Bad(f'{field} must be a string or null')
        for field in ('done', 'limited'):
            if field in b and not isinstance(b[field], bool):
                raise Bad(f'{field} must be a boolean')
        if b.get('has_more') is not None and not isinstance(b['has_more'], bool):
            raise Bad('has_more must be a boolean or null')
        if b.get('has_more') is True and b.get('done') and not b.get('limited'):
            raise Bad('page promises more but was marked done')
        if b.get('has_more') is False and b.get('next_cursor'):
            raise Bad('page reports an end but retains a cursor')
        if not isinstance(b.get('users', []), list):
            raise Bad('users must be a list')
        requested = b.get('requested_cursor') if 'requested_cursor' in b else (old and old['cursor'])
        request_key = requested or ''
        if 'requested_cursor' in b and job and conn.execute(
                'SELECT 1 FROM list_page_requests WHERE job_id=? AND requested_cursor=?',
                (job['id'], request_key)).fetchone():
            conn.commit()
            return {'received': received, 'duplicate': True}
        if self.stale_lease(conn, job, q, b) or ('requested_cursor' in b and
                request_key != ((old and old['cursor']) or '')):
            conn.commit()
            return {'received': received, 'stale': True}
        if job and job['experiment_viewer_ig_id']:
            viewer = accounts.account_from(q, b) or {}
            lane_row = conn.execute('SELECT ig_id,is_main FROM accounts WHERE lane_id=?', (accounts.lane_of(q, b),)).fetchone()
            if (viewer.get('ig_id') != job['experiment_viewer_ig_id'] or not lane_row or lane_row['is_main']
                    or lane_row['ig_id'] != job['experiment_viewer_ig_id']):
                raise Bad('experiment viewer changed during list run')
        if job and job['page_size'] and (type(b.get('requested_count')) is not int or b.get('requested_count') != job['page_size']):
            raise Bad('page size changed during list run')
        valid = [u for u in (b.get('users') or []) if isinstance(u, dict) and isinstance(u.get('handle'), str)
                 and db.norm_handle(u['handle']) and '~' not in db.norm_handle(u['handle'])]
        page_key = db.list_page_key(seed, direction, [dict(u, ig_id=u['ig_id'] if isinstance(u.get('ig_id'), (str, int))
                                                            and not isinstance(u.get('ig_id'), bool) else None) for u in valid],
                                    b.get('next_cursor') or None, job['id'] if job else None,
                                    request_key if job and 'requested_cursor' in b else None)
        if job and 'requested_cursor' not in b and not conn.execute(
                'INSERT OR IGNORE INTO pages(job_id,cursor,at,lane,users) VALUES(?,?,?,?,?)',
                (job['id'], 'next:' + (b.get('next_cursor') or ''), db.now(), accounts.lane_of(q, b),
                 len(b.get('users') or []))).rowcount:
            conn.commit()   # an older client replayed the same page
            return {'received': received, 'duplicate': True}
        if not job and not conn.execute('INSERT OR IGNORE INTO ingested_list_pages(page_key,seed,direction,observed_at) '
                                        'VALUES(?,?,?,?)', (page_key, seed, direction, db.now())).rowcount:
            conn.commit()   # an unmanaged import replayed: nothing new to record
            return {'received': received, 'duplicate': True}
        # Capture prior evidence before page imports can add edges or rename a seed.
        # Snapshot seed peers only when a marked member may enter/leave; their seed
        # yield affects their score even though their own edge does not change.
        incoming = [u for u in (b.get('users') or []) if isinstance(u, dict)]
        existing = set()
        rename_members = set()
        for u in incoming:
            handle = db.norm_handle(u.get('handle'))
            if not handle:
                continue
            identity = str(u['ig_id']) if isinstance(u.get('ig_id'), (str, int)) and not isinstance(u.get('ig_id'), bool) else None
            found = (conn.execute('SELECT id,handle FROM people WHERE ig_id=?', (identity,)).fetchone()
                     if identity else None)
            if not found:
                found = conn.execute('SELECT id,handle FROM people WHERE handle=?', (handle,)).fetchone()
            if found:
                existing.add(found['id'])
                if identity and found['handle'] != handle:
                    rename_members.update(r[0] for r in conn.execute(
                        'SELECT DISTINCT person_id FROM current_edges WHERE seed IN (?,?)', (found['handle'], handle)))
        old_active = {r[0] for r in conn.execute(
            'SELECT person_id FROM current_edges WHERE seed=? AND direction=?', (seed, direction))} if job and b.get('done') else set()
        # A normal page only adds positive observations. The full prefix is needed
        # once, when a claimed completed run can establish negative evidence.
        run_members = {r[0] for r in conn.execute('SELECT person_id FROM list_members WHERE job_id=?',
                                                  (job['id'],))} if old_active else set()
        maybe_removed = old_active - run_members - existing
        marked = set()
        for chunk in chunks(list(existing | maybe_removed)):
            marked.update(r[0] for r in conn.execute(
                f"SELECT person_id FROM marks WHERE status IS NOT NULL AND person_id IN ({','.join('?' * len(chunk))})", chunk))
        marked_added = any(pid in marked and not conn.execute(
            'SELECT 1 FROM current_edges WHERE seed=? AND direction=? AND person_id=?', (seed, direction, pid)).fetchone()
            for pid in existing)
        prior_ids = existing | maybe_removed | rename_members
        if marked_added or bool(maybe_removed & marked):
            prior_ids.update(r[0] for r in conn.execute('SELECT DISTINCT person_id FROM current_edges WHERE seed=?', (seed,)))
        prior_network = network_snapshot(conn, prior_ids)
        ts = db.now()
        lane = accounts.lane_of(q, b)
        next_cursor = b.get('next_cursor') or None
        # A repeated output cursor is a pagination failure, not a duplicate page.
        # Import the returned people before parking the run and retain the request cursor.
        stalled = bool(next_cursor and (next_cursor == requested or (job and conn.execute(
            'SELECT 1 FROM list_page_requests WHERE job_id=? AND requested_cursor=?',
            (job['id'], next_cursor)).fetchone())))
        if job:
            db.start_list_run(conn, job['id'], seed, direction)
            previous_members = conn.execute('SELECT member_count FROM list_runs WHERE job_id=?', (job['id'],)).fetchone()[0]
            valid_users = all(isinstance(u, dict) and isinstance(u.get('handle'), str)
                              and db.norm_handle(u['handle']) and '~' not in db.norm_handle(u['handle'])
                              for u in b.get('users', []))
            if 'requested_cursor' in b and valid_users:
                conn.execute('INSERT OR IGNORE INTO list_page_requests VALUES(?,?,?)', (job['id'], request_key, next_cursor))
            conn.execute('INSERT OR IGNORE INTO pages(job_id,cursor,at,lane,users) VALUES(?,?,?,?,?)',
                         (job['id'], 'request:' + request_key, ts, lane, len(b.get('users') or [])))
            if 'requested_cursor' in b and not requested:
                conn.execute('UPDATE list_runs SET first_page_seen=1 WHERE job_id=?', (job['id'],))
            fresh_total = count_or_none(b.get('total'))
            if isinstance(b.get('total'), float) and not b['total'].is_integer():
                fresh_total = None
            if b.get('total_source') == 'current_run' and fresh_total is not None:
                conn.execute("UPDATE list_runs SET total=?,total_source='current_run' WHERE job_id=?", (fresh_total, job['id']))
        conn.execute('INSERT OR IGNORE INTO seeds(handle, added_at) VALUES(?,?)', (seed, ts))
        if seed_ig_id is not None:
            conn.execute('UPDATE seeds SET ig_id=? WHERE handle=?', (seed_ig_id, seed))
        pids, flipped, new_links = [], set(), 0
        users = b.get('users') if isinstance(b.get('users'), list) else []
        for u in users:
            if (isinstance(u, dict) and isinstance(u.get('handle'), str) and db.norm_handle(u['handle'])
                    and '~' not in db.norm_handle(u['handle'])):
                u = dict(u, name=text_or_none(u.get('name')), pic_url=text_or_none(u.get('pic_url')),
                         ig_id=u['ig_id'] if isinstance(u.get('ig_id'), (str, int)) and not isinstance(u.get('ig_id'), bool) else None)
                pid = db.upsert_person(conn, {k: u.get(k) for k in ('ig_id', 'handle', 'name', 'pic_url', 'is_private', 'is_verified')}, ts)
                new_links += int(db.add_edge(conn, seed, pid, direction, ts, flipped=flipped))
                db.observe_edge(conn, seed, pid, direction, page_key, job['id'] if job else None, ts)
                pids.append(pid)
                if job:
                    conn.execute('INSERT INTO list_members VALUES(?,?,?) '
                                 'ON CONFLICT(job_id,person_id) DO UPDATE SET observed_at=excluded.observed_at',
                                 (job['id'], pid, ts))
        db.dirty_seed_members(conn, *flipped)   # once per seed, not once per new member
        rules.sync(conn, pids)
        received = conn.execute('SELECT member_count FROM list_runs WHERE job_id=?',
                                (job['id'],)).fetchone()[0] if job else len(set(pids))
        limited = bool(b.get('limited'))
        missing_end = not next_cursor and not b.get('done') and not limited
        done = bool(b.get('done')) or limited or stalled or missing_end
        total = count_or_none(b.get('total'))
        if total is None:
            row = conn.execute(f"SELECT {'followers' if direction == 'followers' else 'following'} FROM people WHERE handle=?", (seed,)).fetchone()
            total = row[0] if row else (old and old['total'])
        error = 'Instagram limited this list; coverage is partial.' if limited else None
        if stalled:
            error = 'Instagram repeated its page cursor. Collected profiles were saved; retry this partial list to continue.'
        run = conn.execute('SELECT * FROM list_runs WHERE job_id=?', (job['id'],)).fetchone() if job else None
        if missing_end:
            error = error or 'Instagram returned no next page without confirming the end; coverage is partial.'
        if not run or not run['first_page_seen']:
            error = error or 'Earlier pages lack first-page tracking; retry this list from the start to verify coverage.'
        verified_total = run['total'] if run and run['total_source'] == 'current_run' else None
        if verified_total is not None:
            total = verified_total
        elif done:
            error = error or 'No current collection count was available; coverage is partial.'
        if done and next_cursor and not stalled:
            error = error or 'Instagram reported an end with a remaining cursor; coverage is partial.'
        if done and verified_total is not None and received < verified_total:
            error = error or f'Instagram ended the list after {received} of {verified_total} profiles; coverage is partial.'
        if done and job and not error and not db.list_run_complete(conn, job['id']):
            error = 'The page history does not prove complete coverage; retry this list from the start.'
        state = 'partial' if done and error else ('done' if done else 'running')
        conn.execute('INSERT INTO lists(seed,direction,state,cursor,received,total,error,updated_at,run_job_id) VALUES(?,?,?,?,?,?,?,?,?) '
                     'ON CONFLICT DO UPDATE SET state=excluded.state,cursor=excluded.cursor,received=excluded.received, '
                     'total=coalesce(excluded.total,total),error=excluded.error,updated_at=excluded.updated_at,run_job_id=excluded.run_job_id',
                     (seed,direction,state,requested if stalled else next_cursor,received,total,error,ts,job['id'] if job else None))
        if job:
            conn.execute("UPDATE jobs SET state=?, leased_until=NULL, attempts=0, lane=NULL, lease_token=NULL WHERE id=?",
                         ('error' if stalled else state if done else 'queued', job['id']))
        changed = db.complete_list_snapshot(conn, seed, direction, job['id'], ts) if job and state == 'done' else set()
        if changed:
            rules.sync(conn, changed)
        self.qualification.refresh_network(conn, set(pids) | changed | prior_ids, prior_network)
        # A positive observation can restore a previous relationship without adding a new edge row.
        if pids or changed:
            self.cache.clear()
        if job:
            conn.execute('INSERT INTO collector_events(event_id,at,lane,job_id,kind,direction,outcome,'
                         'http_status,requested_count,returned_count,new_links,saved_entries) VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?)',
                         (ts, lane, job['id'], 'list', direction, 'page',
                          metric_int(b.get('http_status'), 100, 599), metric_int(b.get('requested_count'), 1, 200),
                          len(users), new_links, max(0, received - previous_members)))
        trial_stopped = False
        if job and error and self.trial_event_identity_matches(conn, job, q, b):
            trial_stopped = bool(self.stop_following_trial(conn, error, ts))
        elif job and job['experiment_viewer_ig_id'] and not done and conn.execute(
                "SELECT count(*) FROM collector_events WHERE job_id=? AND outcome='page'",
                (job['id'],)).fetchone()[0] >= FOLLOWING_TRIAL_PAGE_LIMIT:
            trial_stopped = bool(self.stop_following_trial(
                conn, 'saved page limit reached; coverage remains partial.', ts, [job['id']]))
        conn.commit()
        result = {'received': received}
        if stalled:
            result.update(stalled=True, partial=True)
        if trial_stopped:
            result.update(trial_stopped=True, partial=True)
        return result

    def ext_profile(self, conn, q, b):
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        if self.collector_request(conn, q, b) == 'mobile':
            raise Bad('mobile backend currently supports explicit list jobs only')
        job = conn.execute('SELECT * FROM jobs WHERE id=?', (b.get('job_id'),)).fetchone()
        if b.get('job_id') and self.stale_lease(conn, job, q, b):
            conn.commit()
            return {'stale': True}
        p = dict(b.get('profile') or {}) if isinstance(b.get('profile'), dict) else {}
        if not p.get('handle') or not isinstance(p['handle'], str):
            raise Bad('profile.handle required')
        p['handle'] = db.norm_handle(p['handle'])
        if not p['handle'] or '~' in p['handle']:
            raise Bad('invalid profile.handle')
        if job and job['target_ig_id'] and db.normalize_ig_id(p.get('ig_id')) != job['target_ig_id']:
            raise Bad('profile does not match its leased account identity')
        if job and db.norm_handle(p['handle']) != db.norm_handle(job['handle']):
            identity = conn.execute('SELECT ig_id FROM people WHERE handle=?', (job['handle'],)).fetchone()
            if not (identity and identity['ig_id'] and str(p.get('ig_id')) == identity['ig_id']):
                raise Bad('profile does not match its leased identity')
        if ('website' in p and isinstance(p.get('bio'), str)
                and (p['website'] is None or isinstance(p['website'], str) and not p['website'].strip())):
            # A complete read can explicitly remove a link. Omitted fields and
            # partial reads remain unknown; unsafe URLs never replace saved links.
            p['website'] = ''
        elif not (isinstance(p.get('website'), str) and re.match(r'https?://[^\s]+$', p['website'].strip(), re.I)):
            p.pop('website', None)  # javascript:, data:, bare text: never stored, never rendered as a link
        else:
            p['website'] = p['website'].strip()
        for k in ('followers', 'following', 'posts'):
            if k in p:
                p[k] = count_or_none(p[k])
        for k in ('name', 'bio', 'category', 'pic_url'):
            if k in p:
                p[k] = text_or_none(p[k])
        if not isinstance(p.get('ig_id'), (str, int)) or isinstance(p.get('ig_id'), bool):
            p.pop('ig_id', None)
        received_ts = db.now()
        received_at = utc(received_ts)
        captured_at = b.get('captured_at')
        if 'captured_at' not in b:
            ts = received_ts  # compatibility with older extension versions
        else:
            try:
                captured = datetime.fromisoformat(captured_at.replace('Z', '+00:00'))
                if captured.tzinfo is None or captured > received_at + timedelta(minutes=5):
                    raise ValueError()
                ts = iso(min(captured.astimezone(timezone.utc), received_at))
            except (AttributeError, TypeError, ValueError, OverflowError):
                raise Bad('captured_at must be a valid timezone-aware capture time')
        complete = isinstance(p.get('bio'), str)
        if complete:
            p['bio_at'] = ts
            p['bio_src'] = 'extension'
        else:
            p.pop('bio_at', None)
            p.pop('bio_src', None)
        handle = db.norm_handle(p['handle'])
        if p.get('ig_id') and conn.execute('SELECT 1 FROM seeds WHERE handle=?', (handle,)).fetchone():
            self.check_seed_identity(conn, handle, str(p['ig_id']))
        pid = db.upsert_person(conn, p, ts)
        rules.sync(conn, [pid])
        saved = conn.execute('SELECT handle,ig_id,bio_at FROM people WHERE id=?', (pid,)).fetchone()
        if p.get('ig_id') and saved['handle'] == handle:
            conn.execute('UPDATE seeds SET ig_id=? WHERE handle=?', (str(p['ig_id']), handle))
        if complete and saved['bio_at'] == ts and saved['handle'] == handle:
            pending = conn.execute("SELECT id,created_at FROM jobs WHERE kind='profile' AND handle=? "
                                   "AND (state='queued' OR (state='leased' AND id=?))",
                                   (handle, b.get('job_id'))).fetchall()
            conn.executemany("UPDATE jobs SET state='done',leased_until=NULL WHERE id=?",
                             [(row['id'],) for row in pending if 'captured_at' not in b or row['created_at'] is None
                              or db._profile_time(row['created_at']) <= db._profile_time(ts)])
        route = b.get('route') if b.get('route') in ('profile_page', 'info', 'passive') else None
        event_id = b.get('event_id')
        if route and isinstance(event_id, str) and 0 < len(event_id) <= 100:
            # The extension keeps this id in its outbox, so a retry counts once.
            reason = 'saved' if complete and saved['bio_at'] == ts and saved['handle'] == handle else 'stale_capture' if complete else 'partial_profile'
            conn.execute('INSERT OR IGNORE INTO collector_events(event_id,at,lane,job_id,kind,outcome,reason,route) '
                         'VALUES(?,?,?,?,?,?,?,?)',
                         ('profile:' + event_id, received_ts, accounts.lane_of(q, b), job['id'] if job else None,
                          'profile', 'profile', reason, route))
        conn.commit()
        return {'id': pid}

    def scoped_follower_redirect(self, conn, job, b, now):
        """Opt-in recovery for one verified route, never clearing an existing hold.

        The typed HTML-home classification comes from the list response parser.
        Missing response/lease evidence or any simultaneous warning retains the
        default workspace policy. The existing route and target waits still apply.
        """
        if (db.get_setting(conn, 'follower_redirect_recovery') != 'route'
                or not job or job['state'] != 'leased' or job['kind'] != 'list'
                or job['direction'] != 'followers' or not job['viewer_ig_id']
                or b.get('code') != 'other' or b.get('reason') != 'list_html_home_redirect'
                or b.get('http_status') != 200
                or b.get('retry_after') is not None or b.get('retry_at') is not None):
            return False
        try:
            if not job['leased_until'] or utc(job['leased_until']) <= now:
                return False
            raw_hold = db.get_setting(conn, 'cooldown')
            if raw_hold and clean_iso(raw_hold) is None:
                return False
            for row in conn.execute('SELECT hold,cooldown_until,list_cool_until,profile_cool_until FROM accounts'):
                if row['hold'] or any(accounts.later(row[field], now) for field in
                                      ('cooldown_until', 'list_cool_until', 'profile_cool_until')):
                    return False
        except (TypeError, ValueError, AttributeError):
            return False
        return True

    def record_scraping_warning(self, conn, lane, ig_id=None):
        message = 'Collection stopped: Instagram showed a scraping warning. Review the affected account before collecting again.'
        current = db.get_setting(conn, 'instagram_scraping_warning')
        row = conn.execute('SELECT ig_id FROM accounts WHERE lane_id=?', (lane,)).fetchone()
        warning = {'kind': 'scraping_warning', 'lane': lane, 'ig_id': ig_id or (row['ig_id'] if row else None),
                   'at': db.now(), 'message': message, 'review_ready': False}
        if current and current['lane'] != lane:
            current.setdefault('pending', {})[lane] = warning
        else:
            warning['pending'] = (current or {}).get('pending', {})
            current = warning
        db.set_setting(conn, 'instagram_scraping_warning', current)
        db.set_setting(conn, 'instagram_collection_isolation', None)
        db.set_setting(conn, 'paused_lists', True)
        db.set_setting(conn, 'paused_bios', True)

    def scraping_warning_url(self, value):
        try:
            parsed = urlparse(value)
            return (parsed.scheme == 'https' and parsed.hostname in ('instagram.com', 'www.instagram.com', 'i.instagram.com')
                    and parsed.path.rstrip('/') == '/accounts/scraping_warning')
        except (ValueError, TypeError):
            return False

    def ext_error(self, conn, q, b):
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        self.collector_request(conn, q, b, allow_disabled=True)
        code, ts, lane = b.get('code'), db.now(), accounts.lane_of(q, b)
        event_id = b.get('event_id') if isinstance(b.get('event_id'), str) and 0 < len(b['event_id']) <= 100 else None
        if event_id and conn.execute('SELECT 1 FROM collector_events WHERE event_id=?', (event_id,)).fetchone():
            conn.commit()
            return {'duplicate': True}
        warning = b.get('reason') == 'scraping_warning' or self.scraping_warning_url(b.get('url')) or any(
            self.scraping_warning_url(url) for url in re.findall(r'https://[^\s|]+', str(b.get('message') or '')))
        if warning:
            code = 'challenge'
            self.record_scraping_warning(conn, lane, (accounts.account_from(q, b) or {}).get('ig_id'))
        # Security and login warnings also stop other accounts sharing this workspace.
        job = conn.execute("SELECT * FROM jobs WHERE id=? AND state IN ('queued','leased')", (b.get('job_id'),)).fetchone()
        stale = bool(b.get('job_id') and self.stale_lease(conn, job, q, b))
        # Check the registered identity before accounts.touch can update it. A stale
        # lease or a different viewer cannot stop a newly running trial cohort.
        trial_warning = not stale and self.trial_event_identity_matches(conn, job, q, b)
        was_list = bool(job and job['kind'] == 'list')
        reported_job = job or conn.execute('SELECT kind FROM jobs WHERE id=?', (b.get('job_id'),)).fetchone()
        if code == 'private' and was_list and not stale:
            viewer_id = job['viewer_ig_id']
            if not viewer_id or b.get('reason') != 'profile_private_wall':
                # Only a matching profile-page wall can rule out this viewer. A JSON
                # error alone may be a temporary Instagram restriction.
                code = 'soft_block'
        kind = job['kind'] if job else b.get('kind')
        if kind not in ('list', 'profile'):
            kind = None
        direction = job['direction'] if was_list else (b.get('direction') if kind == 'list' else None)
        route = b.get('route') if b.get('route') in ('profile_page', 'info', 'passive') else None
        conn.execute('INSERT INTO collector_events(event_id,at,lane,job_id,kind,direction,outcome,reason,http_status,route) '
                     'VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (event_id, ts, lane, job['id'] if job else None, kind, direction, str(code or 'other')[:40],
                      str(b.get('reason') or '')[:100] or None, metric_int(b.get('http_status'), 0, 599), route))
        home_redirect = (code == 'other' and b.get('reason') == 'list_html_home_redirect'
                         and ((reported_job and reported_job['kind'] == 'list')
                              or (not b.get('job_id') and b.get('kind') == 'list')))
        if stale:
            job = None  # account-level waits still apply, but the old callback cannot change a released job
            if code not in ('rate_limit', 'soft_block', 'login', 'challenge') and not home_redirect:
                conn.commit()
                return {'stale': True}
        fields = {'last_error': (b.get('message') or code or '')[:500] or None}
        route_recovery = home_redirect and not stale and self.scoped_follower_redirect(
            conn, job, b, datetime.now(timezone.utc))
        if (code in ('rate_limit', 'soft_block', 'login', 'challenge') or home_redirect) and db.get_setting(conn, 'instagram_collection_isolation'):
            db.set_setting(conn, 'instagram_collection_isolation', None)
            db.set_setting(conn, 'paused_lists', True)
            db.set_setting(conn, 'paused_bios', True)
        if code in ('rate_limit', 'soft_block', 'login', 'challenge') or (home_redirect and not route_recovery):
            now = datetime.now(timezone.utc)
            # Keep the longest known wait. A second account's shorter warning must
            # never reopen collection while the first one is still cooling.
            deadlines = [now + timedelta(minutes=30 if home_redirect else 15)]
            for value in (b.get('retry_at'), b.get('retry_after'), db.get_setting(conn, 'cooldown')):
                clean = clean_iso(value)
                if clean:
                    deadlines.append(utc(clean))
            until = iso(max(deadlines))
            db.set_setting(conn, 'cooldown', until)
        if code in ('login', 'challenge'):
            # The timed wait must not silently restart collection after a security warning.
            db.set_setting(conn, 'paused_lists', True)
            db.set_setting(conn, 'paused_bios', True)
        if code in ('rate_limit', 'soft_block'):
            fields['cooldown_until'] = until
            if was_list:
                fields['list_cool_until'] = until
            else:
                fields['profile_cool_until'] = until
        if code in accounts.HOLDS:
            fields['hold'] = code
        if home_redirect and was_list and job and job['direction'] == 'followers':
            current = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()
            route_until = accounts.follower_route_wait(conn, current, datetime.now(timezone.utc)) if current else None
            if route_until:
                fields['list_endpoint_until'] = route_until
                if route_recovery:
                    # Every lane rests this endpoint; ordinary profile pages and
                    # following remain subject to the unchanged shared hold.
                    db.set_setting(conn, 'followers_route_until', route_until)
        accounts.touch(conn, lane, accounts.account_from(q, b), **fields)
        db.set_setting(conn, 'last_error', {'code': code, 'message': b.get('message'), 'at': ts, 'lane': lane})
        profile_siblings = accounts.profile_job_siblings(conn, job) if job and job['kind'] == 'profile' else []
        if job and code not in ('other', 'not_found'):
            # rate limits, soft blocks, login walls say nothing about this job: give the lease back to its attempt count,
            # so attempts = leases that ended in 'other' or expired (the ones that may mean the job itself is broken)
            conn.execute('UPDATE jobs SET attempts=max(attempts-1, 0) WHERE id=?', (job['id'],))
            job = conn.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
        if job:  # a late error for a job that already finished must not requeue it
            if code == 'private' and was_list:
                conn.execute('INSERT OR IGNORE INTO list_private_denials(seed,direction,viewer_ig_id,denied_at) VALUES(?,?,?,?)',
                             (job['seed'], job['direction'], viewer_id, ts))
                candidates = conn.execute('SELECT * FROM accounts').fetchall()
                # A cooling or offline account may still follow this private target later.
                # Its temporary availability must not turn an access denial into a final result.
                remaining = any(accounts.viewer_may_access_list(conn, a, job['seed'], job['direction'])
                                for a in candidates)
                final = not remaining
            else:
                # A home-page HTML redirect does not prove that this list is unavailable.
                # Keep trying this target after its delay while other lists can run.
                max_attempts = PROFILE_MAX_ATTEMPTS if job['kind'] == 'profile' else 5
                final = code in ('private', 'not_found') or (code == 'other' and b.get('reason') != 'list_html_home_redirect'
                                                           and job['attempts'] >= max_attempts)
            collected = (conn.execute('SELECT received FROM lists WHERE seed=? AND direction=?',
                                      (job['seed'], job['direction'])).fetchone() if was_list else None)
            job_state = ('partial' if was_list and final and collected and collected['received'] else
                         'done' if code in ('private', 'not_found') and final else
                         'error' if final else 'queued')
            conn.execute('UPDATE jobs SET state=?, leased_until=NULL, lane=NULL, lease_token=NULL WHERE id=?',
                         (job_state, job['id']))
            if code in ('rate_limit', 'soft_block'):
                # A hot job must not bounce immediately through every signed-in account.
                # An explicit Instagram Retry-After wins; the lane's *computed* cooldown
                # is separate, so one account does not freeze a target for all accounts.
                hits = max([job['limit_hits'] or 0] + [r['limit_hits'] or 0 for r in profile_siblings]) + 1
                delay = min(30 * 2 ** min(hits - 1, 4), 360) if code == 'rate_limit' else min(15 * 2 ** min(hits - 1, 3), 120)
                retry = datetime.now(timezone.utc) + timedelta(minutes=delay)
                if code == 'rate_limit':
                    reported = utc(clean_iso(b.get('retry_after'))) if clean_iso(b.get('retry_after')) else None
                    if reported and reported > retry:
                        retry = reported
                if job['kind'] == 'profile':
                    previous = [r['retry_not_before'] for r in profile_siblings if r['retry_not_before']]
                    retry = max([iso(retry)] + previous, key=utc)
                    conn.executemany("UPDATE jobs SET retry_not_before=?,limit_hits=max(limit_hits,?),"
                                     "attempts=max(attempts-CASE WHEN state='leased' THEN 1 ELSE 0 END,0),"
                                     "state='queued',leased_until=NULL,lane=NULL,lease_token=NULL "
                                     "WHERE id=? AND state IN ('queued','leased')",
                                     [(retry, hits, r['id']) for r in profile_siblings])
                else:
                    conn.execute('UPDATE jobs SET retry_not_before=?, limit_hits=? WHERE id=?',
                                 (iso(retry), hits, job['id']))
            elif code == 'other' and not final:
                # Keep a malformed or temporarily failing target from occupying the
                # whole account through repeated local backoffs.
                retry = iso(datetime.now(timezone.utc) + timedelta(minutes=min(2 ** min(job['attempts'], 4), 15)))
                if job['kind'] == 'profile':
                    previous = [r['retry_not_before'] for r in profile_siblings if r['retry_not_before']]
                    retry = max([retry] + previous, key=utc)
                    conn.executemany("UPDATE jobs SET retry_not_before=?,"
                                     "attempts=max(attempts-CASE WHEN state='leased' THEN 1 ELSE 0 END,0),"
                                     "state='queued',leased_until=NULL,lane=NULL,lease_token=NULL "
                                     "WHERE id=? AND state IN ('queued','leased')",
                                     [(retry, r['id']) for r in profile_siblings])
                else:
                    if fields.get('list_endpoint_until'):
                        retry = max(retry, fields['list_endpoint_until'], key=utc)
                    conn.execute('UPDATE jobs SET retry_not_before=? WHERE id=?', (retry, job['id']))
            if job['kind'] == 'list':
                state = ('partial' if final and collected and collected['received'] else
                         'private' if code == 'private' and final else 'error' if final else 'queued')
                if code == 'private':
                    conn.execute('UPDATE lists SET state=?, error=?, lane=NULL, prev_lane=?, released_at=?, released_why=?, updated_at=? WHERE seed=? AND direction=?',
                                 (state, b.get('message') or code, lane, ts, 'private', ts, job['seed'], job['direction']))
                else:
                    # Keep ownership until release_all/release records the actual
                    # handoff cause and source lane. Clearing it here lost that proof.
                    conn.execute('UPDATE lists SET state=?, error=?, updated_at=? WHERE seed=? AND direction=?',
                                 (state, b.get('message') or code, ts, job['seed'], job['direction']))
            elif code == 'private':
                conn.execute('UPDATE people SET is_private=1, updated_at=? WHERE handle=?', (ts, job['handle']))
        if code in accounts.HOLDS:
            accounts.release_all(conn, lane)
        elif 'list_cool_until' in fields:
            accounts.release(conn, datetime.now(timezone.utc), only=lane)
        if profile_siblings:
            accounts.coalesce_profile_jobs(conn, job)
        if trial_warning:
            self.stop_following_trial(conn, b.get('reason') or b.get('message') or code or 'collection error', ts)
        conn.commit()
        return {'stale': True} if stale else {}

    def ext_heartbeat(self, conn, q, b):
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        backend = self.collector_request(conn, q, b)
        b = dict(b, rate=clean_rate(b.get('rate')), cooldown_until=clean_iso(b.get('cooldown_until')))
        lane = accounts.lane_of(q, b)
        db.set_setting(conn, 'ext', dict(b, last_seen=db.now(), lane_id=lane))   # last beat of any lane (older readers)
        today = b.get('today') if isinstance(b.get('today'), dict) else {}

        def text(v, n=500):
            return v[:n] if isinstance(v, str) else None
        fields = {'version': text(b.get('version'), 40), 'state': text(b.get('state'), 20),
                  'identity_day': b.get('day'),
                  'cooldown_until': b['cooldown_until'], 'rate': json.dumps(b['rate']) if b['rate'] else None,
                  'last_error': text(b.get('last_error')), 'activity': text(b.get('activity'), 200), 'text': text(b.get('text'), 200),
                  'today': json.dumps({k: count_or_none(today.get(k)) or 0 for k in ('list', 'profile')})}
        if isinstance(b.get('cool'), dict):   # per-bucket cooldowns (3.4+): a list cooldown hands the list to another lane
            fields['list_cool_until'] = clean_iso(b['cool'].get('list'))
            fields['profile_cool_until'] = clean_iso(b['cool'].get('profile'))
        if 'list_endpoint_until' in b:
            fields['list_endpoint_until'] = clean_iso(b.get('list_endpoint_until'))
        if 'hold' in b and (backend == 'chrome' or b['hold'] in accounts.HOLDS):
            fields['hold'] = b['hold'] if b['hold'] in accounts.HOLDS else None
        if db.get_setting(conn, 'instagram_collection_isolation'):
            previous = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()
            identity = (accounts.account_from(q, b) or {}).get('ig_id')
            now = datetime.now(timezone.utc)
            for key in accounts.IDENTITY_WAITS:
                incoming = fields.get(key)
                old = previous[key] if previous and previous['ig_id'] == identity else None
                if incoming and accounts.later(incoming, now) and (not accounts.later(old, now) or utc(incoming) > utc(old)):
                    # A cooldown can arrive before its queued error report. Stop at
                    # the first new observation without shortening the saved wait.
                    db.set_setting(conn, 'instagram_collection_isolation', None)
                    db.set_setting(conn, 'paused_lists', True)
                    db.set_setting(conn, 'paused_bios', True)
                    break
        if db.get_setting(conn, 'instagram_collection_isolation') and b.get('hold') in accounts.HOLDS:
            identity = (accounts.account_from(q, b) or {}).get('ig_id')
            if not accounts.warning_for(conn, {'lane_id': lane, 'ig_id': identity}):
                db.set_setting(conn, 'instagram_collection_isolation', None)
                db.set_setting(conn, 'paused_lists', True)
                db.set_setting(conn, 'paused_bios', True)
        if backend == 'chrome':
            # Older extensions send this precise status text but no tab field.
            tab = b.get('tab') if 'tab' in b else {
                'Instagram tab is on the login page': 'tab_login',
                'Instagram tab shows a security check': 'tab_challenge',
            }.get(text(b.get('text'), 200))
            warning = db.get_setting(conn, 'instagram_scraping_warning')
            affected = warning if isinstance(warning, dict) and warning.get('lane') == lane else (warning or {}).get('pending', {}).get(lane)
            if affected:
                affected['review_ready'] = bool(affected.get('ig_id')) and (accounts.account_from(q, b) or {}).get('ig_id') == affected['ig_id'] and tab == 'ok' and tuple(int(v) for v in str(b.get('version') or '0').split('.') if v.isdigit()) >= (3, 9, 30)
                db.set_setting(conn, 'instagram_scraping_warning', warning)
            if tab == 'tab_scraping_warning':
                self.record_scraping_warning(conn, lane, (accounts.account_from(q, b) or {}).get('ig_id'))
                tab = 'tab_challenge'
            if tab in ('tab_login', 'tab_challenge'):
                if db.get_setting(conn, 'instagram_collection_isolation'):
                    db.set_setting(conn, 'instagram_collection_isolation', None)
                    db.set_setting(conn, 'paused_lists', True)
                    db.set_setting(conn, 'paused_bios', True)
                fields['hold'] = 'challenge' if tab == 'tab_challenge' or fields.get('hold') == 'challenge' else 'login'
                fields['state'] = tab
        row = accounts.touch(conn, lane, accounts.account_from(q, b), **fields)
        if isinstance(b.get('ready'), dict):   # 3.7+: when each clock allows the next request (the control strip shows breaks)
            ready = db.get_setting(conn, 'ext_ready') or {}
            ready[lane] = {k: clean_iso(b['ready'].get(k)) for k in ('list', 'profile')}
            db.set_setting(conn, 'ext_ready', ready)
        if row['hold'] or accounts.list_wait_until(row, datetime.now(timezone.utc)) or row['paused']:
            accounts.release(conn, datetime.now(timezone.utc), only=lane)
        conn.commit()
        return self.ext_state(conn, row)

    def ext_aggregate(self, conn, accts, now):
        """The old single-extension `ext` block, now summed over lanes (one lane: exactly what it reported)."""
        shared_wait = workspace_cooldown(conn, now)
        if not accts:
            ext = db.get_setting(conn, 'ext') or {}
            cooldowns = [c for c in (ext.get('cooldown_until'), shared_wait) if c and utc(c) > now]
            return {'online': bool(ext.get('last_seen')) and now - utc(ext['last_seen']) < timedelta(seconds=60),
                    'version': ext.get('version'), 'state': ext.get('state'),
                    'cooldown_until': iso(max(map(utc, cooldowns))) if cooldowns else None,
                    'today': ext.get('today'), 'budget': db.get_setting(conn, 'budget'),
                    'last_seen': ext.get('last_seen'), 'activity': ext.get('activity'), 'text': ext.get('text'),
                    'rate': ext.get('rate'), 'last_error': ext.get('last_error') or (db.get_setting(conn, 'last_error') or {}).get('message')}
        live = [a for a in accts if a['online']] or accts
        many = len(accts) > 1
        running = [a for a in live if a['state'] == 'running']
        first = (running or sorted(live, key=lambda a: a['last_seen'] or '', reverse=True))[0]
        cools = [a['cooldown_until'] for a in live if a['cooldown_until']]
        all_cool = len(cools) == len(live) and cools
        wait_until = min(cools) if all_cool else None
        if shared_wait and (not wait_until or utc(shared_wait) > utc(wait_until)):
            wait_until = shared_wait
        rate = accounts.aggregate_rate(accts)
        errs = sorted((a for a in accts if a['last_error']), key=lambda a: a['last_seen'] or '')
        prefix = (lambda a, t: f"{a['name']}: {t}" if t and many else t)
        last_error = errs[-1]['last_error'] if errs else (db.get_setting(conn, 'last_error') or {}).get('message')
        return {'online': any(a['online'] for a in accts), 'version': max((a['version'] or '' for a in accts), default=None) or None,
                'state': 'running' if running else first['state'],
                'cooldown_until': wait_until,
                'today': {k: sum(a['today'][k] for a in accts) for k in ('list', 'profile')},
                'budget': db.get_setting(conn, 'budget'), 'last_seen': max(a['last_seen'] or '' for a in accts) or None,
                'activity': prefix(first, first['activity']), 'text': prefix(first, first['text']),
                'rate': None if not any(a['rate'] for a in live) else {k: rate[k] for k in ('pages_hour', 'people_hour', 'last_hit_at')},
                'last_error': prefix(errs[-1], last_error) if errs else last_error}

    def api_scraper(self, conn, q, b):
        now = datetime.now(timezone.utc)
        accts = accounts.listing(conn, now)
        lists, coverage = self.list_coverage(conn)
        control_state = self.api_control(conn, {}, {})
        stages = control_state['stages']
        return {'processing': processing_modes.snapshot(conn), 'local_processing': self.processing.api_local_processing(conn, {}, {}),
                'ext': self.ext_aggregate(conn, accts, now), 'accounts': accts, 'rate': accounts.aggregate_rate(accts),
                'alerts': accounts.alerts(conn, now, accts),
                'paused': bool(db.get_setting(conn, 'paused')),
                'qualify': bool(db.get_setting(conn, 'qualify')), 'qualify_auto': bool(db.get_setting(conn, 'qualify_auto')),
                'local_laya': bool(db.get_setting(conn, 'local_laya')),
                'llm': self.processing.api_llm(conn, q, b),
                'soak': self.soak(conn, now), 'progress': self.progress(conn, accts),
                'coverage': {'lists': coverage, 'local': self.local_coverage(conn)},
                'collection': collection_progress.summary(conn, lists, accts, now),
                'stages': stages, 'control': control_state,
                'people_today': conn.execute('SELECT count(*) FROM people WHERE first_seen>=?', (iso(now)[:10],)).fetchone()[0],
                'lists': lists,
                'queue': control.queue_counts(conn)}

    def api_scraper_status(self, conn, q, b):
        """Small poll for the navigation strip; the full scraper report is for its page."""
        now = datetime.now(timezone.utc)
        accts = accounts.listing(conn, now, include_lists=False)
        control_state = self.api_control(conn, {}, {})
        stages = control_state['stages']
        return {'processing': processing_modes.snapshot(conn), 'ext': self.ext_aggregate(conn, accts, now), 'accounts': accts,
                'rate': accounts.aggregate_rate(accts),
                'alerts': accounts.alerts(conn, now, accts),
                'paused': bool(db.get_setting(conn, 'paused')),
                'qualify': bool(db.get_setting(conn, 'qualify')),
                'qualify_auto': bool(db.get_setting(conn, 'qualify_auto')),
                'local_laya': bool(db.get_setting(conn, 'local_laya')),
                'stages': stages, 'control': control_state,
                'queue': control.queue_counts(conn)}

    def eta_hours(self, left, per_hour):
        return round(left / per_hour, 2) if left and per_hour else (0 if not left else None)

    def measured_rate(self, conn, sql, now):
        """Recent hourly pace; do not project an ETA after an hour without saved work."""
        since = iso(now - RATE_WINDOW)
        n, first, last = conn.execute(sql, (since,)).fetchone()
        if not n or not first or not last or now - utc(last) >= timedelta(hours=1):
            return None
        hours = max(0.25, (now - utc(first)).total_seconds() / 3600)
        return n / hours

    def observed_per_minute(self, conn, sql, now):
        """Count persisted work in the exact trailing minute; None means no history to measure."""
        since = iso(now - OBSERVED_RATE_WINDOW)
        recent, all_time = conn.execute(sql, (since,)).fetchone()
        return recent if all_time else None

    def eta_with_budget(self, left, per_hour, per_request, lanes, kind, now):
        """Hours to finish at the measured pace, never faster than what today's and later daily budgets allow.
        per_request: items one budgeted request yields (people per list page, 1 for a bio)."""
        if not left:
            return 0
        if not per_hour:
            return None
        hours = left / per_hour
        daily = [a['budget'].get(kind) or 0 for a in lanes]
        if lanes and all(daily):   # 0 = no daily number: the pace alone decides
            per_day = sum(daily) * per_request
            today = sum(max(0, d - (a['today'].get(kind) or 0)) for d, a in zip(daily, lanes)) * per_request
            if left > today:
                midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                to_midnight = (midnight - now).total_seconds() / 3600
                hours = max(hours, to_midnight + (left - today) / per_day * 24)
        return round(hours, 2)

    def progress(self, conn, accts):
        """Plain numbers for the Scraper page: what is left, how fast it really goes (measured), when it is done."""
        now = datetime.now(timezone.utc)
        lanes = [a for a in accts if not a['paused']]
        list_lanes = [a for a in lanes if (a['role'] or 'both') in ('lists', 'both')]
        bio_lanes = [a for a in lanes if (a['role'] or 'both') in ('bios', 'both')]
        # Operational progress follows the chosen direction; saved coverage still includes every list.
        following = db.get_setting(conn, 'follower_lists') is False
        scope_filter = " AND l.direction='following'" if following else ''
        page_source = ("pages JOIN jobs j ON j.id=pages.job_id AND j.direction='following'" if following else 'pages')
        # Unknown list sizes fall back to the seed's follower/following count, so the total is an estimate.
        lists_left, unknown = conn.execute(
            "SELECT coalesce(sum(max(coalesce(l.total, CASE l.direction WHEN 'followers' THEN p.followers ELSE p.following END, 0)"
            " - l.received, 0)), 0), count(CASE WHEN l.total IS NULL THEN 1 END) FROM lists l "
            "LEFT JOIN people p ON p.handle=l.seed WHERE l.state NOT IN ('done','private','error','partial')" + scope_filter).fetchone()
        incomplete_lists, incomplete_left, capped_lists = conn.execute(
            "SELECT count(*), coalesce(sum(max(coalesce(l.total, CASE l.direction WHEN 'followers' THEN p.followers "
            "ELSE p.following END, l.received) - l.received, 0)), 0), "
            "count(CASE WHEN l.state='partial' AND l.error LIKE 'Instagram limited this list;%' THEN 1 END) "
            "FROM lists l LEFT JOIN people p ON p.handle=l.seed WHERE l.state IN ('partial','error')" + scope_filter).fetchone()
        pages_h = self.measured_rate(conn, f'SELECT count(*), min(at), max(at) FROM {page_source} WHERE at>=?', now)
        people_h = self.measured_rate(conn, f'SELECT coalesce(sum(users), 0), min(at), max(at) FROM {page_source} WHERE at>=?', now)
        per_page = people_h / pages_h if pages_h and people_h else 25
        bio_min = db.get_setting(conn, 'bio_min') or 0
        queued = conn.execute("SELECT count(*) FROM jobs WHERE kind='profile' AND state IN ('queued','leased')").fetchone()[0]
        # The planner queues bios in batches; count everyone it will still plan, not just the current batch.
        unplanned = conn.execute(
            f"SELECT count(*) FROM people p JOIN verdicts v ON v.person_id=p.id WHERE {NOT_ME} AND p.bio_at IS NULL "
            "AND coalesce(p.is_private,0)=0 AND v.prefilter>=? AND instr(p.handle,'~')=0 "
            "AND p.handle NOT IN (SELECT handle FROM seeds) "
            "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind='profile' AND j.handle=p.handle)", (bio_min,)).fetchone()[0]
        bios_left = queued + unplanned
        bios_failed = conn.execute("SELECT count(*) FROM people p WHERE p.bio_at IS NULL "
            "AND EXISTS(SELECT 1 FROM jobs j WHERE j.kind='profile' AND j.handle=p.handle AND j.state='error') "
            "AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.kind='profile' AND j.handle=p.handle AND j.state IN ('queued','leased'))").fetchone()[0]
        bios_h = self.measured_rate(conn, 'SELECT count(*), min(bio_at), max(bio_at) FROM people WHERE bio_at>=?', now)
        lists_min = self.observed_per_minute(conn, f'SELECT coalesce(sum(users),0), (SELECT count(*) FROM {page_source}) FROM {page_source} WHERE at>=?', now)
        bios_min = self.observed_per_minute(conn, 'SELECT count(*), (SELECT count(*) FROM people WHERE bio_at IS NOT NULL) FROM people WHERE bio_at>=?', now)
        q_left = self.ai_left(conn)
        q_rate = self.measured_rate(conn, 'SELECT count(*), min(scored_at), max(scored_at) FROM ai_scoring_events WHERE scored_at>=?', now)
        q_hour = conn.execute('SELECT count(*) FROM ai_scoring_events WHERE scored_at>=?',
                              (iso(now - timedelta(hours=1)),)).fetchone()[0]
        bio_budget = (db.get_setting(conn, 'budget') or {}).get('profile') or 0
        return {
            'lists': {'left': lists_left, 'estimate': bool(unknown),
                      'incomplete_lists': incomplete_lists, 'incomplete_left': incomplete_left,
                      'capped_lists': capped_lists, 'per_hour': round(people_h) if people_h else None,
                      'per_minute': lists_min,
                      'eta_h': self.eta_with_budget(lists_left, people_h, per_page, list_lanes, 'list', now)},
            'bios': {'left': bios_left, 'queued': queued, 'failed': bios_failed, 'unresolved': bios_left + bios_failed,
                     'per_hour': round(bios_h) if bios_h else None,
                     'per_minute': bios_min,
                     'per_day': sum(a['budget'].get('profile') or 0 for a in bio_lanes) or bio_budget * max(1, len(bio_lanes)),
                     'eta_h': self.eta_with_budget(bios_left, bios_h, 1, bio_lanes, 'profile', now),
                     'estimate': not bios_h},
            'qualify': {'left': q_left, 'per_hour': q_hour, 'per_minute': conn.execute(
                        'SELECT count(*) FROM ai_scoring_events WHERE scored_at>=?',
                        (iso(now - timedelta(minutes=1)),)).fetchone()[0],
                        'eta_h': self.eta_hours(q_left, q_rate),
                        'on': bool(db.get_setting(conn, 'qualify')), 'workers': db.get_setting(conn, 'llm_workers'),
                        'keys': len(llm.get().keys) if hasattr(llm, 'get') else None},
        }

    def list_coverage(self, conn):
        """Current attempt entries and target provenance, using indexed joins rather than an edge scan.

        Entries in different seed lists can name the same person. A prior run's positive
        edges remain saved when a short list is retried, but they do not certify the
        current attempt and must not inflate its numerator.
        """
        rows = conn.execute("""SELECT l.seed,l.direction,l.state,l.cursor,l.received,l.total,l.error,l.updated_at,
            l.run_job_id,l.released_why,r.member_count,r.first_page_seen,r.total AS run_total,r.total_source,
            CASE l.direction WHEN 'followers' THEN p.followers ELSE p.following END AS profile_total
            FROM lists l LEFT JOIN list_runs r ON r.job_id=l.run_job_id
            LEFT JOIN people p ON p.handle=l.seed ORDER BY l.updated_at DESC""").fetchall()
        out = []
        total = {'saved_entries': 0, 'tracked_saved_entries': 0, 'expected_entries': 0,
                 'saved_entries_known_targets': 0, 'saved_entries_unknown_targets': 0,
                 'missing_entries': 0, 'known_targets': 0, 'unknown_targets': 0,
                 'current_run_targets': 0, 'estimated_targets': 0,
                 'complete_lists': 0, 'partial_lists': 0, 'blocked_lists': 0,
                 'active_lists': 0, 'total_lists': len(rows)}
        for row in rows:
            tracked = row['member_count'] if row['run_job_id'] is not None else None
            saved = tracked if tracked is not None else max(0, row['received'] or 0)
            current_total = (row['run_total'] if row['total_source'] == 'current_run'
                             and isinstance(row['run_total'], int) and row['run_total'] >= 0 else None)
            estimate = next((v for v in (row['total'], row['profile_total'])
                             if isinstance(v, int) and v >= 0), None)
            expected = current_total if current_total is not None else estimate
            source = 'current_run' if current_total is not None else 'estimate' if estimate is not None else 'unknown'
            proved = (row['state'] == 'done' and row['cursor'] is None and row['run_job_id'] is not None
                      and db.list_run_complete(conn, row['run_job_id']))
            if proved:
                completion = 'complete'
                reason = None
            elif row['state'] == 'done':
                completion = 'unverified'
                reason = 'Completion proof is missing; this list needs review.'
            elif row['state'] == 'partial':
                completion = 'partial'
                reason = self.list_completion_reason(row['error']) or 'Only part of this list was saved.'
            elif row['state'] in ('private', 'error', 'paused'):
                completion = 'blocked'
                reason = self.list_completion_reason(row['error']) or {'private': 'Access to this list was denied.', 'paused': 'This list is stopped. Review it before retrying.'}.get(row['state'], 'Collection stopped with an error.')
            elif row['state'] == 'running':
                completion, reason = 'collecting', None
            else:
                completion, reason = 'waiting', None
            item = {'seed': row['seed'], 'direction': row['direction'], 'state': row['state'],
                    'received': row['received'], 'total': row['total'], 'updated_at': row['updated_at'],
                    'run_job_id': row['run_job_id'],
                    'error': row['error'], 'saved_entries': saved, 'saved_current_run': tracked,
                    'saved_source': 'tracked_run' if tracked is not None else 'legacy_unverified',
                    'expected': expected, 'expected_source': source,
                    'missing': max(expected - saved, 0) if expected is not None else None,
                    'completion': completion, 'completion_reason': reason}
            out.append(item)
            total['saved_entries'] += saved
            if tracked is not None:
                total['tracked_saved_entries'] += tracked
            if expected is None:
                total['unknown_targets'] += 1
                total['saved_entries_unknown_targets'] += saved
            else:
                total['known_targets'] += 1
                total['current_run_targets' if source == 'current_run' else 'estimated_targets'] += 1
                total['expected_entries'] += expected
                total['saved_entries_known_targets'] += saved
                if completion != 'complete':
                    total['missing_entries'] += max(expected - saved, 0)
            total[{'complete': 'complete_lists', 'partial': 'partial_lists',
                   'unverified': 'partial_lists', 'blocked': 'blocked_lists',
                   'collecting': 'active_lists', 'waiting': 'active_lists'}[completion]] += 1
        return out, total

    def list_completion_reason(self, error):
        """Keep a readable reason; raw error samples may contain an entire HTML page."""
        if not error:
            return None
        if error.startswith('Instagram limited this list;'):
            return 'Instagram limited this list.'
        if 'list_html_home_redirect' in error:
            return 'Instagram returned its home page instead of list data.'
        return error.split(' | ', 1)[0][:240]

    def local_coverage(self, conn):
        """Count current local profile reviews from the durable, signature-checked work queue.

        Queue rebuilds run in bounded worker batches. Until one finishes, old cache
        rows cannot prove how many profiles still need the current model policy.
        """
        enabled = bool(db.get_setting(conn, 'local_laya'))
        if db.get_setting(conn, 'laya_queue_signature') != laya.cache_signature():
            return {'enabled': enabled, 'eligible_profiles': None, 'processed_profiles': None,
                    'pending_profiles': None, 'state': 'rebuilding',
                    'reason': 'Checking which saved profiles need local review.'}
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        cache_key = (path, laya.cache_signature())
        with self.local_coverage_lock:
            cached = self.local_coverage_cache.get(cache_key) if path else None
            if cached and time.monotonic() - cached[0] < 30:
                eligible, pending, sampled_at = cached[1:]
            else:
                eligible = conn.execute(f"SELECT count(*) FROM people p WHERE instr(p.handle,'~')=0 AND {NOT_ME}").fetchone()[0]
                pending = conn.execute('SELECT count(*) FROM laya_queue').fetchone()[0]
                sampled_at = db.now()
                self.local_coverage_cache.clear()
                if path:
                    self.local_coverage_cache[cache_key] = (time.monotonic(), eligible, pending, sampled_at)
        processed = max(eligible - pending, 0)
        health = laya.last_known()
        state = ('off' if not enabled else 'complete' if pending == 0 else
                 'running' if health is True else 'waiting' if health is False else 'pending')
        reason = ('Local review is off.' if not enabled else
                  'All saved profiles have a current local review.' if pending == 0 else
                  'Reviewing saved profiles on this computer.' if state == 'running' else
                  'Waiting for the local reader.' if state == 'waiting' else
                  'Saved profiles still need local review.')
        return {'enabled': enabled, 'eligible_profiles': eligible, 'processed_profiles': processed,
                'pending_profiles': pending, 'state': state, 'reason': reason, 'sampled_at': sampled_at}

    def soak(self, conn, now):
        out = {}
        for label, hours in (('1h', 1), ('6h', 6)):
            since = iso(now - timedelta(hours=hours))
            out[label] = {'pages': conn.execute('SELECT count(*) FROM pages WHERE at>=?', (since,)).fetchone()[0],
                          'people': conn.execute('SELECT count(*) FROM edges WHERE first_seen>=?', (since,)).fetchone()[0],
                          'new_people': conn.execute('SELECT count(*) FROM people WHERE first_seen>=?', (since,)).fetchone()[0],
                          'profiles': conn.execute('SELECT count(*) FROM people WHERE bio_at>=?', (since,)).fetchone()[0]}
        return out

    def api_collection_suggestions(self, conn, q, b):
        if 'enabled' in b:
            collection_suggestions.set_enabled(conn, b['enabled'])
        if 'hide' in b:
            collection_suggestions.hide(conn, b['hide'])
        if b:
            conn.commit()
        return collection_suggestions.suggest(conn, qint(q, 'limit') or 6)

    def api_snowball(self, conn, q, b):
        """Opt-in: queue the `following` lists of good/client leads as new seeds (their people then get the same network signals)."""
        min_status = status_in(b.get('min_status', 'interested'))
        if min_status not in ('interested', 'client'):
            raise Bad('min_status must be interested or client')
        limit = b.get('limit', SNOWBALL_MAX)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise Bad('limit must be 1-500')
        statuses = POSITIVE if min_status == 'interested' else ('client',)
        rows = conn.execute(f"SELECT p.handle FROM people p JOIN ({owner.feedback_marks_sql()}) m ON m.person_id=p.id WHERE m.status IN ({','.join('?' * len(statuses))}) "
                            "AND instr(p.handle, '~')=0 AND coalesce(p.is_private,0)=0 "
                            "AND NOT EXISTS (SELECT 1 FROM lists l WHERE l.seed=p.handle AND l.direction='following') "
                            "ORDER BY m.updated_at DESC LIMIT ?", (*statuses, limit)).fetchall()
        seeds = [r[0] for r in rows if db.queue_list(conn, r[0], 'following')]
        conn.commit()
        return {'queued': len(seeds), 'seeds': seeds}

    def api_seeds(self, conn, q, b):
        raw_handles = b.get('handles')
        raw_dirs = b.get('directions', ['followers', 'following'])
        if 'refresh' in b and not isinstance(b['refresh'], bool):
            raise Bad('refresh must be true or false')
        if (not isinstance(raw_handles, list) or not isinstance(raw_dirs, list)
                or not raw_dirs or any(d not in ('followers', 'following') for d in raw_dirs)):
            raise Bad('handles and directions must be lists of Instagram accounts and list directions')
        handles = [db.norm_handle(h) for h in raw_handles]
        if any(not h or '~' in h for h in handles):
            raise Bad('invalid Instagram handle or profile URL')
        added = [(h, d) for h in dict.fromkeys(handles) for d in dict.fromkeys(raw_dirs)
                 if db.queue_list(conn, h, d, refresh=b.get('refresh', False))]
        conn.commit()
        return {'queued': len(added)}

    def api_pause(self, conn, q, b):
        if not isinstance(b.get('paused'), bool):
            raise Bad('paused must be an explicit boolean')
        if b['paused'] is False:
            self.require_collection_resume(conn)
        db.set_setting(conn, 'paused', b['paused'])
        conn.commit()
        return {}

    def ai_left(self, conn):
        return conn.execute('SELECT count(*) ' + external_candidates_sql(),
            (time.time(), db.get_setting(conn, 'llm_min') or 0)).fetchone()[0]

    def api_control(self, conn, q, b):
        """What each stage (lists, bios, AI) and each account is doing right now, in plain sentences."""
        return dict(control.snapshot(conn, self.ai_left(conn)), collection_startup=browser_startup.snapshot(conn))

    def require_collection_resume(self, conn):
        # Serialize the check with the subsequent resume so a warning cannot land between them.
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        try:
            if workspace_cooldown(conn, datetime.now(timezone.utc)):
                raise Bad('Instagram is on a shared safety hold. Collection remains paused.')
        except Exception:
            conn.rollback()
            raise

    def api_control_set(self, conn, q, b):
        """Collection controls, including bounded connection of saved Chrome profiles."""
        try:
            if b.get('action') == 'connect_accounts' and not self.host_operations_allowed:
                raise Bad('Accounts cannot connect from this saved-data workspace.')
            if b.get('action') == 'acknowledge_scraping_warning' and (browser_startup.snapshot(conn) or {}).get('state') in ('opening', 'waiting'):
                raise Bad('Stop the connection attempt before reviewing the warning.')
            if b.get('action') == 'pause' and b.get('stage') != 'ai':
                browser_startup.cancel(conn)
            if b.get('action') == 'connect_accounts':
                browser_startup.begin(self.config.root, conn, b)
                return self.api_control(conn, q, b)
            if b.get('action') == 'resume_selected_accounts' and self.host_operations_allowed:
                if browser_startup.begin(self.config.root, conn, b, only_if_offline=True):
                    return self.api_control(conn, q, b)
            normal_start = (b.get('action') == 'start_all' or b.get('action') == 'resume'
                            and b.get('stage') in ('collection', 'lists', 'bios', 'all'))
            if normal_start or b.get('action') == 'resume' and b.get('stage') is None and isinstance(b.get('account'), str):
                self.require_collection_resume(conn)
            if (normal_start and self.host_operations_allowed
                    and browser_startup.can_launch(self.config.root, conn)
                    and browser_startup.read_config(self.config.root)):
                if browser_startup.begin(self.config.root, conn, b, only_if_offline=True):
                    return self.api_control(conn, q, b)
            if b.get('action') == 'stop_benchmark':
                with edge_benchmark_api.LOCK:
                    control.apply(conn, b)
            else:
                control.apply(conn, b)
        except LookupError:
            conn.rollback()
            raise NotFound('no such account') from None
        except ValueError as exc:
            conn.rollback()
            raise Bad(str(exc)) from None
        except Exception:
            conn.rollback()
            raise
        if b.get('stage') in ('ai', 'all'):
            self.local_services.schedule(conn)
        if (self.host_operations_allowed and b.get('action') in ('resume', 'start_all', 'resume_selected_accounts')
                and b.get('stage') != 'ai'):
            browser_startup.schedule(self.config.root, conn)
        return self.api_control(conn, q, b)

    def api_engine_start(self, conn, q, b):
        """Start local services. Collection keeps its current pause and safety state."""
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        if (not self.host_operations_allowed or not path
                or Path(path).resolve() != Path(self.config.db).resolve()):
            raise Bad('Local services cannot start from this saved-data workspace.')
        try:
            account_count = conn.execute('SELECT count(*) FROM accounts').fetchone()[0]
            return engine_start.start(self.config.root, account_count)
        except engine_start.EngineStartError as exc:
            raise Bad(str(exc)) from None

    def api_budget(self, conn, q, b):
        budget = db.get_setting(conn, 'budget')
        changes = {}
        for key, cap in accounts.BUDGET_MAX.items():
            if key not in b:
                continue
            value = b[key]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= cap:
                raise Bad(f'{key} budget must be an integer from 0 to {cap}')
            changes[key] = value
        budget.update(changes)
        db.set_setting(conn, 'budget', budget)
        conn.commit()
        return {}

    def api_accounts(self, conn, q, b):
        now = datetime.now(timezone.utc)
        accts = accounts.listing(conn, now)
        return {'accounts': accts, 'alerts': accounts.alerts(conn, now, accts), 'rate': accounts.aggregate_rate(accts),
                'main_list_share': float(db.get_setting(conn, 'main_list_share') or 0)}

    def api_account_settings(self, conn, q, b):
        """{"main_list_share": 0-1}: the share of list pages Michael's own account may take (0 = bios only)."""
        v = b.get('main_list_share')
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 1:
            raise Bad('main_list_share must be a number 0-1')
        db.set_setting(conn, 'main_list_share', round(float(v), 2))
        conn.commit()
        return {'main_list_share': round(float(v), 2)}

    def api_account_edit(self, conn, q, b, lane):
        conn.execute('BEGIN IMMEDIATE')
        if b.get('paused') is False:
            self.require_collection_resume(conn)
        try:
            row = accounts.edit(conn, lane, b)
        except LookupError:
            conn.rollback()
            raise NotFound('no such account') from None
        except ValueError as e:
            conn.rollback()
            raise Bad(str(e)) from None
        conn.commit()
        return {'account': accounts.out(conn, row, datetime.now(timezone.utc))}

    def api_account_remove(self, conn, q, b, lane):
        conn.execute('BEGIN IMMEDIATE')
        n = accounts.remove(conn, lane)
        conn.commit()
        return {'removed': n}

    def api_mobile_state(self, conn, q, b):
        if self.collector_request(conn, q, b) != 'mobile':
            raise Bad('mobile backend must be explicitly selected')
        row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (accounts.lane_of(q, b),)).fetchone()
        return dict(self.ext_state(conn, row), account={key: row[key] for key in
                    ('lane_id', 'ig_id', 'collection_backend', 'paused', 'hold')})

    def api_mobile_queue(self, conn, q, b):
        conn.execute('BEGIN IMMEDIATE')
        if self.collector_request(conn, q, b) != 'mobile':
            raise Bad('mobile backend must be explicitly selected')
        lane = accounts.lane_of(q, b)
        viewer = (accounts.account_from(q, b) or {}).get('ig_id')
        try:
            queued = mobile_collector.queue_mobile_list(conn, lane, viewer, b.get('seed'),
                                                        b.get('direction'), count=b.get('count', 200))
        except ValueError as exc:
            raise Bad(str(exc)) from None
        conn.commit()
        return {'backend': 'mobile', 'backend_cursor_isolated': True,
                'job': dict(queued) if hasattr(queued, 'keys') else {'id': queued}}

    def api_setup(self, conn, q, b):
        """What the add-account wizard shows: where the unpacked extension lives and which id Chrome gives it."""
        manifest = json.loads((self.config.root / 'extension' / 'manifest.json').read_text())
        accts = accounts.listing(conn, datetime.now(timezone.utc), include_lists=False)
        main = next((a for a in accts if a.get('is_main')), None)
        instagram = onboarding.instagram_setup(main, me_handle(conn))
        benchmark = edge_benchmark_api.config(conn)
        instagram['collection_blocker'] = ('Review and stop the unfinished benchmark before continuing collection.'
                                           if benchmark else None)
        if benchmark:
            lane = next((a for a in accts if a['lane_id'] == benchmark.get('lane_id')), {})
            instagram['benchmark'] = {'lane_id': benchmark.get('lane_id'), 'handle': lane.get('handle'),
                                      'requires_account_tab_check': True}
        return {'instagram': instagram, 'repo': str(self.config.root), 'extension_path': str(self.config.root / 'extension'), 'extension_id': EXT_ORIGIN.split('//')[1],
                'extension_version': manifest.get('version'), 'server': f"http://127.0.0.1:{self.config['port']}",
                'lanes': conn.execute('SELECT count(*) FROM accounts').fetchone()[0]}

    def api_onboarding(self, conn, q, b):
        """Get-started checklist plus the one-handle flow. POST {"skip": "ai"|"backups"} hides an optional step."""
        if b:
            step = b.get('skip')
            if step not in onboarding.OPTIONAL or not isinstance(b.get('on', True), bool):
                raise Bad('skip must be one of ' + ', '.join(onboarding.OPTIONAL))
            skipped = set(db.get_setting(conn, 'onboarding_skipped') or [])
            skipped = skipped | {step} if b.get('on', True) else skipped - {step}
            db.set_setting(conn, 'onboarding_skipped', sorted(skipped))
            conn.commit()
        now = datetime.now(timezone.utc)
        accts = accounts.listing(conn, now, include_lists=False)
        steps = onboarding.checks(accts, self.processing.api_llm(conn, {}, {}), onboarding.newest_backup(self.config['db']),
                                  onboarding.backup_agent_loaded(), set(db.get_setting(conn, 'onboarding_skipped') or []),
                                  {'extension_version': json.loads((self.config.root / 'extension' / 'manifest.json').read_text()).get('version')})
        hold = workspace_cooldown(conn, now)
        controls = self.api_control(conn, {}, {})
        return {**onboarding.summarize(steps), 'steps': steps, 'control': controls,
                'flow': onboarding.flow(conn, hold, controls)}

    def api_start(self, conn, q, b):
        """Queue the chosen lists and resume collection at the normal safe pace."""
        handles = b.get('handles') if isinstance(b.get('handles'), list) else [b.get('handle')]
        directions = b.get('directions', ['followers', 'following'])
        queued = self.api_seeds(conn, q, {'handles': handles, 'directions': directions})['queued']
        started, starting, note, controls = False, False, None, None
        isolation = db.get_setting(conn, 'instagram_collection_isolation') or {}
        command = ({'action': 'resume_selected_accounts', 'accounts': [
            {'lane_id': lane, 'ig_id': identity} for lane, identity in isolation['accounts'].items()]}
            if isolation else {'stage': 'collection', 'action': 'resume'})
        try:
            controls = self.api_control_set(conn, q, command)
            starting = (controls.get('collection_startup') or {}).get('state') in ('opening', 'waiting')
            selected_stages = isolation.get('collection_stages', ['lists', 'bios'])
            stages = [stage for stage in controls.get('stages', []) if stage['id'] in selected_stages]
            started = not starting and len(stages) == len(selected_stages) and all(not stage['paused'] for stage in stages)
        except (Bad, ValueError) as exc:
            note = str(exc)
        return {'queued': queued, 'started': started, 'starting': starting, 'note': note,
                'directions': list(dict.fromkeys(directions)),
                'control': controls if controls is not None else self.api_control(conn, {}, {})}
