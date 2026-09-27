import argparse
import biofetch
import deepscout
import websearch
from concurrent.futures import ThreadPoolExecutor
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import socket
import ssl
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounts  # noqa: E402
import control  # noqa: E402
import connection_graph  # noqa: E402
import collection_suggestions  # noqa: E402
import db  # noqa: E402
import owner  # noqa: E402
import owner_relationships  # noqa: E402
import tag_projection  # noqa: E402
import owner_notes  # noqa: E402
import engine_start  # noqa: E402
import meta_network  # noqa: E402
import laya  # noqa: E402
import llm  # noqa: E402
import usage_ledger  # noqa: E402
import qualify  # noqa: E402
import rules  # noqa: E402
import workflows  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'web'
EXT_ORIGIN = 'chrome-extension://fgdbghllamedgihmdcolaggnbhnakjnf'
CFG = {'db': str(ROOT / 'data' / 'leads.sqlite'), 'port': 8777}
# Pipeline: (unmarked) -> interested -> contacted -> talking -> client; 'no' = Not a fit (hidden by default).
STATUSES = ('interested', 'contacted', 'talking', 'spoke_before', 'client', 'no')
POSITIVE = ('interested', 'talking', 'client')   # what used to be good/client: positive few-shot, seed yield, snowball
POSITIVE_SQL = "('interested','talking','client')"
LEGACY_STATUS = {'good': 'interested'}           # older clients / saved views


def status_in(v):
    return LEGACY_STATUS.get(v, v)
PIC_HOSTS = ('.cdninstagram.com', '.fbcdn.net')
PIC_MAX = 2 * 1024 * 1024
READ_PRIORITY = 10000
LEASE_MIN = 10
PROFILE_MAX_ATTEMPTS = 8   # expired profile leases get delayed retries, then stop after this many attempts
QUALIFY_MAX_ATTEMPTS = 5
SSL = ssl.create_default_context(cafile='/etc/ssl/cert.pem' if Path('/etc/ssl/cert.pem').is_file() else None)
PLAN_BATCH = 200   # profile jobs kept queued at a time when the profile budget is unlimited


class Bad(Exception):
    pass


class NotFound(Bad):
    pass


class Conflict(Exception):
    def __init__(self, current):
        super().__init__("This record changed. Review the latest note before retrying.")
        self.current = current


def text_or_none(v):
    return v if isinstance(v, str) else None


def utc(s):
    d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def iso(d):
    return d.isoformat(timespec='microseconds')


def pfp_dir():
    return Path(CFG['db']).resolve().parent / 'pfp'


OWNER_HANDLE = owner_relationships.OWNER


def me_handle(conn):
    return owner_relationships.owner_handle(conn)


def edges_of(conn, pid):
    return [dict(r) for r in conn.execute('SELECT seed, direction, observed_at FROM current_edges WHERE person_id=? ORDER BY seed, direction', (pid,))]


def edge_history_of(conn, pid):
    """All discovered links with their latest evidence; never use this for scoring."""
    return [dict(r) for r in conn.execute('SELECT e.seed,e.direction,e.first_seen,v.observed_at,v.checked_at,'
            "CASE WHEN v.active=1 THEN 'observed' WHEN v.active=0 THEN 'absent' ELSE 'unverified' END AS state "
            'FROM edges e LEFT JOIN edge_evidence v ON v.seed=e.seed AND v.person_id=e.person_id '
            'AND v.direction=e.direction WHERE e.person_id=? ORDER BY e.seed,e.direction', (pid,))]


# ---------- extension endpoints ----------

def workspace_cooldown(conn, now):
    """A persisted Instagram warning pauses collection across all browser accounts."""
    raw = db.get_setting(conn, 'cooldown')
    value = clean_iso(raw)
    if raw is not None and raw != '' and value is None:
        raise Bad('Stored safety hold is invalid. Collection remains stopped until it is repaired.')
    until = utc(value) if value else None
    return value if until and until > now else None


def permit_capable(version):
    parts = str(version or '').split('.')
    return len(parts) == 3 and all(p.isdigit() for p in parts) and tuple(map(int, parts)) >= (3, 9, 16)


def ext_state(conn, row=None):
    """What one lane is told: paused = workspace pause or this account paused; budget = its own or the global one."""
    cooling = workspace_cooldown(conn, datetime.now(timezone.utc))
    upgrade = row is None or not permit_capable(row['version'])
    return {'paused': accounts.paused_for(conn, row), 'budget': accounts.budget_of(conn, row),
            'stages': {k: not cooling and not upgrade and k not in control.paused_kinds(conn) for k in ('list', 'profile')},
            **({'upgrade_required': True, 'minimum_version': '3.9.16', 'message': 'Reload the extension in this Chrome profile before collecting.'} if upgrade else {}),
            **({'cooldown_until': cooling} if cooling else {})}


def ext_request(conn, q, b):
    """One workspace-wide request permit; release only accepts its original lane/token."""
    lane = accounts.lane_of(q, b)
    now = datetime.now(timezone.utc)
    if b.get('action') == 'release':
        token = b.get('token')
        if not isinstance(token, str) or not token:
            raise Bad('request token required')
        return accounts.request_permit(conn, lane, token=token, now=now)
    if b.get('action') != 'acquire' or b.get('kind') not in ('list', 'profile'):
        raise Bad('request action and kind required')
    conn.execute('BEGIN IMMEDIATE')
    row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()
    if row is None or not permit_capable(row['version']) or accounts.paused_for(conn, row) or workspace_cooldown(conn, now) or b['kind'] in control.paused_kinds(conn):
        conn.rollback()
        return {'granted': False, 'wait_ms': 15000}
    job = conn.execute('SELECT * FROM jobs WHERE id=?', (b.get('job_id'),)).fetchone()
    if job is None or stale_lease(conn, job, q, b) or job['kind'] != b['kind'] or not job['leased_until'] or utc(job['leased_until']) <= now:
        conn.rollback()
        return {'granted': False, 'stale': True, 'wait_ms': 15000}
    if (job['kind'] == 'profile' and accounts.main_bios_reserved(conn, row) or
            job['kind'] == 'list' and job['direction'] == 'followers'
            and accounts.follower_route_wait(conn, row, now)):
        conn.execute("UPDATE jobs SET state='queued',lane=NULL,leased_until=NULL,lease_token=NULL,"
                     "attempts=max(attempts-1,0) WHERE id=?", (job['id'],))
        conn.commit()
        return {'granted': False, 'stale': True, 'wait_ms': 15000}
    # Keep a valid job alive while its account waits fairly for the shared request slot.
    conn.execute('UPDATE jobs SET leased_until=? WHERE id=?',
                 (iso(now + timedelta(minutes=LEASE_MIN)), job['id']))
    return dict(accounts.request_permit(conn, lane, kind=b['kind'], now=now), lease_renewed=True)


def ext_next(conn, q, b):
    """Leases per lane (see accounts.py): never one job to two lanes, lists stick to their lane while it is healthy."""
    lane, ts, now = accounts.lane_of(q, b), db.now(), datetime.now(timezone.utc)
    kinds = [k for k in csv(q, 'kinds') if k in ('list', 'profile')] or ['list', 'profile']
    conn.execute('BEGIN IMMEDIATE')
    # asking for work means no login wall holds it any more
    row = accounts.touch(conn, lane, accounts.account_from(q, b), hold=None,
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
    accounts.reopen_private_for_viewer(conn, row, now)
    st = ext_state(conn, row)
    if st.get('upgrade_required'):
        conn.commit()
        return dict(st, job=None)

    if st['paused']:
        conn.commit()
        return dict(st, job=None, cooldown_until=st.get('cooldown_until'))
    if st.get('cooldown_until'):
        conn.commit()
        return dict(st, job=None)
    stopped = control.paused_kinds(conn)   # a stage paused on the control strip hands out none of its jobs
    st['stages'] = {'list': 'list' not in stopped, 'profile': 'profile' not in stopped}
    kinds = [k for k in accounts.kinds_for(conn, row, kinds, now) if k not in stopped]
    if not kinds:
        conn.commit()
        return dict(st, job=None)
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
               'page_size': job['page_size']}
    else:
        p = conn.execute('SELECT ig_id FROM people WHERE handle=?', (job['handle'],)).fetchone()
        out = {'id': job['id'], 'kind': 'profile', 'handle': job['handle'], 'ig_id': p and p['ig_id']}
    conn.commit()
    out['lease_token'] = token
    return dict(st, job=out)


def check_seed_identity(conn, handle, ig_id):
    """A seed's history belongs to one Instagram account: refuse a different ID for a known handle."""
    known = {str(r[0]) for r in conn.execute(
        'SELECT ig_id FROM seeds WHERE handle=? AND ig_id IS NOT NULL '
        'UNION SELECT ig_id FROM people WHERE handle=? AND ig_id IS NOT NULL', (handle, handle)) if r[0]}
    if known - {ig_id}:
        raise Bad('seed account identity changed; existing relationship history cannot be reassigned')


def stale_lease(conn, job, q, b):
    """Old outbox messages cannot alter a job after another lane/lease has taken it."""
    if not job:
        return bool(b.get('job_id'))
    if job['state'] != 'leased':
        return True
    lane = accounts.lane_of(q, b)
    if job['lane'] and job['lane'] != lane:
        return True
    return bool(job['lease_token'] and b.get('lease_token') != job['lease_token'])


def ext_list_page(conn, q, b):
    """Import one list page atomically: any rejection or failure leaves no partial writes behind."""
    try:
        return _ext_list_page(conn, q, b)
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def _ext_list_page(conn, q, b):
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
        check_seed_identity(conn, seed, seed_ig_id)
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
    if stale_lease(conn, job, q, b) or ('requested_cursor' in b and
            request_key != ((old and old['cursor']) or '')):
        conn.commit()
        return {'received': received, 'stale': True}
    if job and job['page_size'] and b.get('requested_count') != job['page_size']:
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
    refresh_network(conn, set(pids) | changed | prior_ids, prior_network)
    # A positive observation can restore a previous relationship without adding a new edge row.
    if pids or changed:
        clear_caches()
    if job:
        conn.execute('INSERT INTO collector_events(event_id,at,lane,job_id,kind,direction,outcome,'
                     'http_status,requested_count,returned_count,new_links) VALUES(NULL,?,?,?,?,?,?,?,?,?,?)',
                     (ts, lane, job['id'], 'list', direction, 'page',
                      metric_int(b.get('http_status'), 100, 599), metric_int(b.get('requested_count'), 1, 100),
                      len(users), new_links))
    conn.commit()
    return {'received': received, 'stalled': True, 'partial': True} if stalled else {'received': received}


def count_or_none(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, float) and v == v and abs(v) < 1e12:
        v = int(v)
    if isinstance(v, str) and re.fullmatch(r'\s*\d[\d,]*\s*', v):
        v = int(v.replace(',', ''))
    return v if isinstance(v, int) and 0 <= v < 10 ** 12 else None


def metric_int(value, low, high):
    return value if type(value) is int and low <= value <= high else None


def ext_profile(conn, q, b):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    job = conn.execute('SELECT * FROM jobs WHERE id=?', (b.get('job_id'),)).fetchone()
    if b.get('job_id') and stale_lease(conn, job, q, b):
        conn.commit()
        return {'stale': True}
    p = dict(b.get('profile') or {}) if isinstance(b.get('profile'), dict) else {}
    if not p.get('handle') or not isinstance(p['handle'], str):
        raise Bad('profile.handle required')
    p['handle'] = db.norm_handle(p['handle'])
    if not p['handle'] or '~' in p['handle']:
        raise Bad('invalid profile.handle')
    if job and db.norm_handle(p['handle']) != db.norm_handle(job['handle']):
        identity = conn.execute('SELECT ig_id FROM people WHERE handle=?', (job['handle'],)).fetchone()
        if not (identity and identity['ig_id'] and str(p.get('ig_id')) == identity['ig_id']):
            raise Bad('profile does not match its leased identity')
    if not (isinstance(p.get('website'), str) and re.match(r'https?://[^\s]+$', p['website'].strip(), re.I)):
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
    ts = db.now()
    complete = isinstance(p.get('bio'), str)
    if complete:
        p['bio_at'] = ts
        p['bio_src'] = 'extension'
    else:
        p.pop('bio_at', None)
    handle = db.norm_handle(p['handle'])
    if p.get('ig_id') and conn.execute('SELECT 1 FROM seeds WHERE handle=?', (handle,)).fetchone():
        check_seed_identity(conn, handle, str(p['ig_id']))
    pid = db.upsert_person(conn, p, ts)
    rules.sync(conn, [pid])
    if p.get('ig_id'):
        conn.execute('UPDATE seeds SET ig_id=? WHERE handle=?', (str(p['ig_id']), handle))
    if complete:
        conn.execute("UPDATE jobs SET state='done', leased_until=NULL WHERE kind='profile' AND handle=? "
                     "AND (state='queued' OR (state='leased' AND id=?))", (handle, b.get('job_id')))
    conn.commit()
    return {'id': pid}


def ext_error(conn, q, b):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    code, ts, lane = b.get('code'), db.now(), accounts.lane_of(q, b)
    event_id = b.get('event_id') if isinstance(b.get('event_id'), str) and 0 < len(b['event_id']) <= 100 else None
    if event_id and conn.execute('SELECT 1 FROM collector_events WHERE event_id=?', (event_id,)).fetchone():
        conn.commit()
        return {'duplicate': True}
    # Security and login warnings also stop other accounts sharing this workspace.
    job = conn.execute("SELECT * FROM jobs WHERE id=? AND state IN ('queued','leased')", (b.get('job_id'),)).fetchone()
    stale = bool(b.get('job_id') and stale_lease(conn, job, q, b))
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
    conn.execute('INSERT INTO collector_events(event_id,at,lane,job_id,kind,direction,outcome,reason,http_status) '
                 'VALUES(?,?,?,?,?,?,?,?,?)',
                 (event_id, ts, lane, job['id'] if job else None, kind, direction, str(code or 'other')[:40],
                  str(b.get('reason') or '')[:100] or None, metric_int(b.get('http_status'), 0, 599)))
    home_redirect = (code == 'other' and b.get('reason') == 'list_html_home_redirect'
                     and ((reported_job and reported_job['kind'] == 'list')
                          or (not b.get('job_id') and b.get('kind') == 'list')))
    if stale:
        job = None  # account-level waits still apply, but the old callback cannot change a released job
        if code not in ('rate_limit', 'soft_block', 'login', 'challenge') and not home_redirect:
            conn.commit()
            return {'stale': True}
    fields = {'last_error': (b.get('message') or code or '')[:500] or None}
    if code in ('rate_limit', 'soft_block', 'login', 'challenge') or home_redirect:
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
    accounts.touch(conn, lane, accounts.account_from(q, b), **fields)
    db.set_setting(conn, 'last_error', {'code': code, 'message': b.get('message'), 'at': ts, 'lane': lane})
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
            hits = (job['limit_hits'] or 0) + 1
            delay = min(30 * 2 ** min(hits - 1, 4), 360) if code == 'rate_limit' else min(15 * 2 ** min(hits - 1, 3), 120)
            retry = datetime.now(timezone.utc) + timedelta(minutes=delay)
            if code == 'rate_limit':
                reported = utc(clean_iso(b.get('retry_after'))) if clean_iso(b.get('retry_after')) else None
                if reported and reported > retry:
                    retry = reported
            if job['kind'] == 'profile':
                conn.execute("UPDATE jobs SET retry_not_before=?, limit_hits=max(limit_hits, ?) "
                             "WHERE kind='profile' AND handle=? AND state='queued'",
                             (iso(retry), hits, job['handle']))
            else:
                conn.execute('UPDATE jobs SET retry_not_before=?, limit_hits=? WHERE id=?',
                             (iso(retry), hits, job['id']))
        elif code == 'other' and not final:
            # Keep a malformed or temporarily failing target from occupying the
            # whole account through repeated local backoffs.
            retry = iso(datetime.now(timezone.utc) + timedelta(minutes=min(2 ** min(job['attempts'], 4), 15)))
            if job['kind'] == 'profile':
                conn.execute("UPDATE jobs SET retry_not_before=? WHERE kind='profile' AND handle=? AND state='queued'",
                             (retry, job['handle']))
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
    conn.commit()
    return {'stale': True} if stale else {}


def clean_iso(v):
    try:
        return iso(utc(v)) if isinstance(v, str) and v else None
    except ValueError:
        return None


def clean_rate(r):
    if not isinstance(r, dict):
        return None
    out = {}
    for k in ('pages_hour', 'people_hour'):
        v = r.get(k)
        out[k] = round(float(v), 1) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None
    v = r.get('last_hit_at')
    try:
        out['last_hit_at'] = iso(utc(v)) if isinstance(v, str) and v else None
    except ValueError:
        out['last_hit_at'] = None
    return out


def ext_heartbeat(conn, q, b):
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
    if 'hold' in b:   # 3.4+ report a login wall / security check here too; older builds only via /api/ext/error
        fields['hold'] = b['hold'] if b['hold'] in accounts.HOLDS else None
    row = accounts.touch(conn, lane, accounts.account_from(q, b), **fields)
    if isinstance(b.get('ready'), dict):   # 3.7+: when each clock allows the next request (the control strip shows breaks)
        ready = db.get_setting(conn, 'ext_ready') or {}
        ready[lane] = {k: clean_iso(b['ready'].get(k)) for k in ('list', 'profile')}
        db.set_setting(conn, 'ext_ready', ready)
    if row['hold'] or accounts.list_wait_until(row, datetime.now(timezone.utc)) or row['paused']:
        accounts.release(conn, datetime.now(timezone.utc), only=lane)
    conn.commit()
    return ext_state(conn, row)


# ---------- UI endpoints ----------

LISTS = '(SELECT count(DISTINCT e.seed) FROM current_edges e WHERE e.person_id=p.id)'  # observed links only
PEOPLE_FROM = 'FROM people p LEFT JOIN verdicts v ON v.person_id=p.id LEFT JOIN marks m ON m.person_id=p.id'
LEAD_SQL = f"SELECT p.*, v.tier, v.score, v.content_fit, v.role, v.reason, m.status, m.note, coalesce(m.updated_at, '') AS mark_rev, {LISTS} AS lists {PEOPLE_FROM}"
NOT_ME = "p.handle NOT IN (SELECT handle FROM seeds WHERE is_me=1) AND p.handle!='fortun8te' COLLATE NOCASE"
# manual first, then rule tags, then auto; inside a source: role, niche, signal, size, source
TAG_ORDER = ("CASE t.source WHEN 'manual' THEN 0 WHEN 'rule' THEN 1 ELSE 2 END, "
             "CASE t.grp WHEN 'role' THEN 0 WHEN 'niche' THEN 1 WHEN 'signal' THEN 2 WHEN 'size' THEN 3 ELSE 4 END, t.tag")
MAP_TAGS = 4
# The map colours each person by the judgement in their full tag set (kept in step with GOOD_TAGS / BAD_TAGS in web/app.js).
JUDGE_BAD = {'Too big', 'Other market', 'Scout: No', 'Not reachable', 'Creator', 'Coach', 'Agency', 'Personal', 'SaaS', 'Freelancer'}
JUDGE_GOOD = {'Scout: Strong', 'Scout: Possible', 'AI: Top fit', 'AI: Decision maker', 'Founder', 'Brand', 'Store', 'Shopify', 'Shop Link', 'DTC'}


def judge(tagset):
    return 'bad' if tagset & JUDGE_BAD else 'good' if tagset & JUDGE_GOOD else None


def chunks(ids, n=900):
    ids = list(ids)
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def lead_rows(conn, rows):
    ids = [r['id'] for r in rows]
    if not ids:
        return []
    marks = ','.join('?' * len(ids))
    nets = network_context(conn, ids)
    tags, via, history_via = {}, {}, {}
    followups = {r['person_id']: {k: r[k] for k in ('due_on', 'note', 'completed_at', 'updated_at')} for r in conn.execute(f'SELECT * FROM followups WHERE person_id IN ({marks})', ids)}
    for t in conn.execute(f'SELECT * FROM ({tag_projection.relation()}) t WHERE t.person_id IN ({marks}) ORDER BY {TAG_ORDER}', ids):
        tags.setdefault(t['person_id'], []).append({'tag': t['tag'], 'grp': t['grp'], 'source': t['source']})
    for e in conn.execute(f'SELECT DISTINCT person_id, seed FROM current_edges WHERE person_id IN ({marks}) ORDER BY seed', ids):
        via.setdefault(e['person_id'], []).append(e['seed'])
    for e in conn.execute(f'SELECT DISTINCT person_id, seed FROM edges WHERE person_id IN ({marks}) ORDER BY seed', ids):
        history_via.setdefault(e['person_id'], []).append(e['seed'])
    result = [{'id': r['id'], 'handle': r['handle'], 'name': r['name'], 'pic': f"/img/{r['id']}" if r['pic_file'] else None,
             'bio': r['bio'], 'website': r['website'], 'followers': r['followers'], 'following': r['following'],
             'posts': r['posts'], 'tier': r['tier'] or 'unread', 'score': r['score'],
             'business_fit': round(r['content_fit']) if r['content_fit'] is not None else None,
             'connection_strength': qualify.network_strength(nets[r['id']]),
             'relationship': nets[r['id']]['me'],
             'role': r['role'], 'reason': r['reason'],
             'tags': tags.get(r['id'], []), 'via': via.get(r['id'], []), 'lists': r['lists'],
             'history_via': history_via.get(r['id'], []), 'history_lists': len(history_via.get(r['id'], [])),
             'status': r['status'], 'mark_rev': r['mark_rev'],
             'note': r['note'] or None, 'bio_at': r['bio_at'], 'bio_src': r['bio_src'], 'follow_up': followups.get(r['id'])} for r in rows]
    owner.hydrate(conn, result)
    owner_links = owner_relationships.facts(conn, ids)
    for person in result:
        person.update(owner_links[person['id']])
        person['manual_tags'] = [t['tag'] for t in person['tags'] if t['source'] == 'manual']
        person['owner_status'] = owner.owner_status(person)
        person['reachable'] = True if person['owner_status'] in ('client', 'talking') else None
        person['owner_conflict'] = owner.owner_conflict(person)
        person['tags'] = owner.visible_tags(person, person['tags'])
        person.update(owner.owner_recommendation(person, person))
    return result


def csv(q, key):
    return [x.strip() for x in (q.get(key, [''])[0]).split(',') if x.strip()]


def qint(q, key):
    v = q.get(key, [''])[0].strip()
    if not v:
        return None
    try:
        n = int(v)
    except ValueError:
        raise Bad(f'{key} must be a whole number') from None
    if not -(2 ** 63) <= n < 2 ** 63:
        raise Bad(f'{key} is too large')
    return n


def lead_filter(q, status_default=True):
    """Shared by /api/leads, /api/counts, /api/tags (facets) and /api/map. -> (where clauses on p/v/m, args).
    status_default: without a status filter, leave out people marked no."""
    where, args = [], []

    def within(sql, values):  # sql has one {} for the placeholders
        where.append(sql.format(','.join('?' * len(values))))
        args.extend(values)

    relationship = q.get('relationship', [''])[0]
    if relationship:
        try:
            clauses, values = owner_relationships.filter_sql(relationship)
        except ValueError as error:
            raise Bad(str(error)) from None
        where.extend(clauses)
        args.extend(values)

    tiers = csv(q, 'tier')
    if tiers:
        if any(t not in ('hot', 'warm', 'cold', 'unread') for t in tiers):
            raise Bad('bad tier')
        within("coalesce(v.tier,'unread') IN ({})", tiers)
    effective_tags = 'SELECT t.person_id FROM (' + tag_projection.relation() + ') t WHERE '
    for t in dict.fromkeys(csv(q, 'tags')):  # all of
        within('p.id IN (' + effective_tags + 't.tag={})', [t])
    if csv(q, 'any'):  # at least one of
        within('p.id IN (' + effective_tags + 't.tag IN ({}))', csv(q, 'any'))
    if csv(q, 'not'):  # none of
        within('p.id NOT IN (' + effective_tags + 't.tag IN ({}))', csv(q, 'not'))
    statuses = csv(q, 'status')
    if any(status_in(s) not in (*STATUSES, 'none', 'all') for s in statuses):
        raise Bad('bad status')
    if not statuses:
        if status_default:
            where.append("coalesce(m.status,'')!='no'")
    elif 'all' not in statuses:
        named = [status_in(s) for s in statuses if s != 'none']
        if any(s not in STATUSES for s in named):
            raise Bad('bad status')
        conds = (['m.status IS NULL'] if 'none' in statuses else []) + (['m.status IN ({})'] if named else [])
        within('(' + ' OR '.join(conds) + ')', named)
    text = q.get('q', [''])[0].strip()
    if text:
        where.append("(p.handle LIKE ? ESCAPE '\\' OR p.name LIKE ? ESCAPE '\\' OR p.bio LIKE ? ESCAPE '\\' OR m.note LIKE ? ESCAPE '\\')")
        like = re.sub(r'([\\%_])', r'\\\1', text)
        args += [f'%{like}%'] * 4
    min_lists = qint(q, 'min_lists') or 0
    if min_lists < 0:
        raise Bad('min_lists must be nonnegative')
    if min_lists > 0:
        where.append(f'{LISTS}>=?')
        args.append(min_lists)
    has_bio = q.get('has_bio', [''])[0].strip()
    if has_bio in ('1', 'true'):
        where.append("coalesce(p.bio,'')!=''")
    elif has_bio in ('0', 'false'):
        where.append("coalesce(p.bio,'')=''")
    elif has_bio:
        raise Bad('has_bio must be 1 or 0')
    for s in dict.fromkeys(map(db.norm_handle, csv(q, 'seed'))):  # discovered through every listed seed, including history
        where.append('p.id IN (SELECT person_id FROM edges WHERE seed=?)')
        args.append(s)
    for key, op in (('followers_min', '>='), ('followers_max', '<=')):
        n = qint(q, key)
        if n is not None:
            if n < 0:
                raise Bad(f'{key} must be nonnegative')
            where.append(f'p.followers {op} ?')
            args.append(n)
    workflows.filters(q, where, args)
    return where, args


SORTS = {'follow_up': '(SELECT f.due_on FROM followups f WHERE f.person_id=p.id AND f.completed_at IS NULL) IS NULL, (SELECT f.due_on FROM followups f WHERE f.person_id=p.id AND f.completed_at IS NULL)', 'recent': 'p.updated_at DESC', 'followers': 'p.followers IS NULL, p.followers DESC',
         'connected': 'lists DESC, p.followers IS NULL, p.followers DESC',
         'fit': "CASE WHEN v.tier='unread' THEN 1 ELSE 0 END, v.content_fit IS NULL, v.content_fit DESC, lists DESC, v.score IS NULL, v.score DESC",
         'score': 'v.score IS NULL, v.score DESC, p.followers DESC'}


@contextmanager
def read_snapshot(conn):
    """Keep the revision, totals and related records in one SQLite read snapshot.

    SAVEPOINT also preserves a caller's existing transaction, including test fixtures.
    """
    conn.execute('SAVEPOINT lead_read')
    try:
        yield
    finally:
        conn.execute('RELEASE SAVEPOINT lead_read')


def api_leads(conn, q, b):
    where, args = lead_filter(q)
    sort = q.get('sort', ['score'])[0]
    if sort not in SORTS:
        raise Bad('sort must be one of ' + ', '.join(SORTS))
    order = SORTS[sort]
    offset = max(0, qint(q, 'offset') or 0)
    limit = min(500, max(1, qint(q, 'limit') or 50))
    sql_where = ' WHERE ' + ' AND '.join([NOT_ME] + where)
    with read_snapshot(conn):
        rev = data_rev(conn)
        total = conn.execute(f'SELECT count(*) {PEOPLE_FROM}{sql_where}', args).fetchone()[0]
        rows = conn.execute(f'{LEAD_SQL}{sql_where} ORDER BY {order}, p.id LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
        next_offset = offset + len(rows)
        return {'total': total, 'rows': lead_rows(conn, rows), 'rev': rev,
                'next_offset': next_offset, 'has_more': next_offset < total}


def api_tags(conn, q, b):
    return cached(conn, 'tags', q, lambda: tag_facets(conn, q))


def tag_facets(conn, q):
    """Per-tag counts for the filtered set and overall, including current fit labels."""
    where, args = lead_filter(q)
    counts, totals = {}, {}
    # Group each branch separately so SQLite can retain the stored-tag covering
    # index instead of materializing the entire tag relation before filtering.
    for branch, projected in enumerate(tag_projection.parts()):
        selected = f"SELECT p.id {PEOPLE_FROM} WHERE {' AND '.join([NOT_ME] + where)}"
        count_sql = (f"WITH f AS MATERIALIZED ({selected}) SELECT t.tag,t.source,count(*) "
                     f"FROM f JOIN ({projected}) t ON t.person_id=f.id GROUP BY t.tag,t.source"
                     if branch == 0 else
                     f"SELECT t.tag,t.source,count(*) FROM ({projected}) t "
                     f"WHERE t.person_id IN ({selected}) GROUP BY t.tag,t.source")
        for tag, source, n in conn.execute(count_sql, args):
            counts[tag, source] = counts.get((tag, source), 0) + n
        for tag, source, grp, n in conn.execute(
            f'SELECT t.tag,t.source,min(t.grp),count(*) FROM ({projected}) t GROUP BY t.tag,t.source'):
            previous = totals.get((tag, source), (grp, 0))
            totals[tag, source] = (grp, previous[1] + n)
    out = [{'tag': tag, 'grp': grp, 'source': source, 'count': counts.get((tag, source), 0), 'total': total}
           for (tag, source), (grp, total) in totals.items()]
    return sorted(out, key=lambda f: (-f['count'], -f['total'], f['tag'], f['source']))


def api_counts(conn, q, b):
    return cached(conn, 'counts', q, lambda: counts(conn, q))


def counts(conn, q):
    """Tier and status counts inside the shared filter, each ignoring its own dimension (so the choices stay visible).
    none = unmarked, open = everyone but 'no'; total / with_bio: everyone in the database."""
    out = dict.fromkeys(('hot', 'warm', 'cold', 'unread', *STATUSES, 'none', 'open'), 0)
    where, args = lead_filter({k: v for k, v in q.items() if k != 'tier'})
    out.update(conn.execute(f"SELECT coalesce(v.tier,'unread'), count(*) {PEOPLE_FROM} WHERE {' AND '.join([NOT_ME] + where)} "
                            'GROUP BY 1', args).fetchall())
    where, args = lead_filter({k: v for k, v in q.items() if k != 'status'}, status_default=False)
    for status, n in conn.execute(f"SELECT m.status, count(*) {PEOPLE_FROM} WHERE {' AND '.join([NOT_ME] + where)} GROUP BY 1", args):
        out[status or 'none'] = out.get(status or 'none', 0) + n
        out['open'] += n if status != 'no' else 0
    out['total'], out['with_bio'] = conn.execute("SELECT count(*), count(nullif(bio,'')) FROM people").fetchone()
    return out


def person_row(conn, pid):
    row = conn.execute(LEAD_SQL + ' WHERE p.id=?', (pid,)).fetchone()
    if not row:
        raise NotFound('not found')
    return row


def api_person(conn, q, b, pid):
    row = person_row(conn, pid)
    v = conn.execute('SELECT * FROM verdicts WHERE person_id=?', (pid,)).fetchone()
    verdict = dict(v) if v else None
    if verdict:
        try:
            ev = json.loads(verdict.get('evidence') or '[]')
        except ValueError:
            ev = []
        verdict['evidence'] = [x for x in ev if isinstance(x, str)] if isinstance(ev, list) else []
    owner_facts = with_owner(conn, dict(row))
    if verdict:
        verdict = owner.owner_recommendation(owner_facts, verdict)
    # Active work takes precedence over history; otherwise show the latest request for this profile.
    job = conn.execute("SELECT state FROM jobs WHERE kind='profile' AND handle=? AND state!='cancelled' "
                       "ORDER BY (state IN ('queued','leased')) DESC, id DESC LIMIT 1", (row['handle'],)).fetchone()
    pending = bool(job and job['state'] in ('queued', 'leased'))
    profile_read = {'state': {'leased': 'reading', 'error': 'failed'}.get(job['state'], job['state'])} if job else None
    return dict(lead_rows(conn, [row])[0], edges=edges_of(conn, pid), edge_history=edge_history_of(conn, pid),
                verdict=verdict, note=row['note'], site=qual_api.site_row(conn, pid),
                activity=workflows.history(conn, pid), profile_read_pending=pending, profile_read=profile_read,
                scout=deepscout.result(conn, pid), note_interpretation=owner_notes.result(conn, pid))


KEEP = object()   # "leave this field as it is"


def set_status(conn, pids, status=KEEP, note=KEEP, relationships=KEEP, familiarity=KEEP):
    """Upsert marks. KEEP leaves a field alone; None / '' clears it. A row with neither status nor note is removed."""
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    pids = list(dict.fromkeys(pids))
    affected = set()
    context_changed = relationships is not KEEP or familiarity is not KEEP
    if status is not KEEP or context_changed:
        for chunk in chunks(pids):
            placeholders = ','.join('?' * len(chunk))
            affected.update(r[0] for r in conn.execute(
                f'SELECT DISTINCT person_id FROM current_edges WHERE seed IN (SELECT seed FROM current_edges WHERE person_id IN ({placeholders}) '
                f'UNION SELECT handle FROM people WHERE id IN ({placeholders}))', (*chunk, *chunk)))
    affected.difference_update(pids)
    ts = db.now()
    for pid in dict.fromkeys(pids):
        old = conn.execute('SELECT status,note FROM marks WHERE person_id=?', (pid,)).fetchone()
        if context_changed:
            existing = conn.execute('SELECT * FROM owner_context WHERE person_id=?', (pid,)).fetchone()
            before_context = {'relationships': owner.relationships({'status': old['status'] if old else None,
                               'relationships': json.loads(existing['relationships']) if existing else []}),
                              'familiarity': existing['familiarity'] if existing else None}
            after_context = {'relationships': before_context['relationships'] if relationships is KEEP else relationships,
                             'familiarity': before_context['familiarity'] if familiarity is KEEP else familiarity}
            conn.execute('INSERT OR REPLACE INTO owner_context VALUES(?,?,?,?)',
                         (pid, json.dumps(after_context['relationships']), after_context['familiarity'], ts))
            if before_context != after_context:
                workflows.event(conn, pid, 'relationship', before=before_context, after=after_context)
            if relationships is not KEEP and 'client' not in relationships:
                conn.execute("UPDATE marks SET status=NULL,updated_at=? WHERE person_id=? AND status='client'", (ts, pid))
                for legacy_tag in conn.execute("SELECT tag FROM tags WHERE person_id=? AND source='manual' AND lower(trim(tag))='client'", (pid,)):
                    workflows.event(conn, pid, 'tag', body='Relationship updated', before=legacy_tag['tag'])
                conn.execute("DELETE FROM tags WHERE person_id=? AND source='manual' AND lower(trim(tag))='client'", (pid,))
        # A pipeline change must not erase the recorded client history.
        legacy_client = bool(old and old['status'] == 'client') or bool(conn.execute(
            "SELECT 1 FROM tags WHERE person_id=? AND source='manual' AND lower(trim(tag))='client'", (pid,)).fetchone())
        if relationships is KEEP and (status == 'client' or (status is not KEEP and legacy_client)):
            existing = conn.execute('SELECT * FROM owner_context WHERE person_id=?', (pid,)).fetchone()
            saved = owner.normalize_relationships((json.loads(existing['relationships']) if existing else []) + ['client'])
            conn.execute('INSERT OR REPLACE INTO owner_context VALUES(?,?,?,?)',
                         (pid, json.dumps(saved), existing['familiarity'] if existing else None, ts))

        if status is not KEEP and status != 'client':
            legacy = conn.execute("SELECT tag FROM tags WHERE person_id=? AND source='manual' "
                                  "AND lower(trim(tag))='client'", (pid,)).fetchall()
            for tag in legacy:
                workflows.event(conn, pid, 'tag', body='Relationship updated', before=tag['tag'])
            conn.execute("DELETE FROM tags WHERE person_id=? AND source='manual' AND lower(trim(tag))='client'", (pid,))
        for kind, value in (('status', status), ('note', note)):
            before = old[kind] if old else None
            if value is not KEEP and (value or None) != (before or None):
                workflows.event(conn, pid, kind, before=before, after=value or None)
    sets = [f'{col}=excluded.{col}' for col, v in (('status', status), ('note', note)) if v is not KEEP]
    if sets:
        rows = [(p, None if status is KEEP else status, None if note is KEEP else (note or None), ts) for p in pids]
        conn.executemany('INSERT INTO marks(person_id, status, note, updated_at) VALUES(?,?,?,?) ON CONFLICT(person_id) DO UPDATE SET '
                         + ', '.join(sets + ['updated_at=excluded.updated_at']), rows)
    for chunk in chunks(pids):
        conn.execute(f"DELETE FROM marks WHERE person_id IN ({','.join('?' * len(chunk))}) AND status IS NULL AND coalesce(note,'')=''", chunk)
    if sets or context_changed:
        touch(conn, pids)   # status and note feed the qualifier: the person is re-qualified on the next batch
        for pid in pids:
            owner_notes.invalidate(conn, pid)
    # A mark changes the yield of shared seeds, and a client's own seed becomes a stronger link.
    # One seed can hold tens of thousands of people: queue their local re-rank for the background
    # drain instead of doing it inside this request's write lock.
    db.mark_network_dirty(conn, affected)


def api_mark(conn, q, b, pid):
    """Optional if_match/mark_rev compares the last read mark before applying a partial edit."""
    status, note = b.get('status', KEEP), b.get('note', KEEP)
    relationships, familiarity = b.get('relationships', KEEP), b.get('familiarity', KEEP)
    if relationships is not KEEP:
        try:
            relationships = owner.normalize_relationships(relationships)
        except ValueError as exc:
            raise Bad(str(exc)) from exc
    if familiarity is not KEEP and familiarity is not None and (not isinstance(familiarity, str) or familiarity not in owner.FAMILIARITIES):
        raise Bad('Choose a known familiarity')
    status = status_in(status) if isinstance(status, str) else status
    if status is not KEEP and status is not None and status not in STATUSES:
        raise Bad('bad status')
    if note is not KEEP and note is not None and (not isinstance(note, str) or len(note) > 5000):
        raise Bad('note must be text')
    expected = b.get('if_match', b.get('mark_rev', KEEP))
    if expected is not KEEP and not isinstance(expected, str):
        raise Bad('mark revision must be text')
    if 'if_match' in b and 'mark_rev' in b and b['if_match'] != b['mark_rev']:
        raise Bad('mark revisions disagree')
    # Acquire the writer lock before reading: two tabs cannot both pass the same comparison.
    conn.execute('BEGIN IMMEDIATE')
    try:
        row = person_row(conn, pid)
        current = {key: row[key] for key in ('status', 'note', 'mark_rev')}
        current['id'] = pid
        owner.hydrate(conn, [current])
        if expected is not KEEP and expected != current['mark_rev']:
            raise Conflict(current)
        set_status(conn, [pid], status, note, relationships, familiarity)
        row = person_row(conn, pid)
        result = {key: row[key] for key in ('status', 'note', 'mark_rev')}
        result['id'] = pid
        owner.hydrate(conn, [result])
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def clean_tag(t):
    if not isinstance(t, str):
        raise Bad('tag must be a string')
    t = re.sub(r'\s+', ' ', t).strip()
    if not t or len(t) > 64 or ',' in t:
        raise Bad('tag must be 1-64 characters without commas')
    return t


def manual_tag(t):
    tag = clean_tag(t)
    if tag.casefold() == 'client':
        raise Bad('Use the Client relationship status instead of adding a Client label.')
    return tag


def tag_group(conn, tag, default='signal'):
    row = conn.execute("SELECT grp FROM tags WHERE tag=? ORDER BY source='manual' DESC LIMIT 1", (tag,)).fetchone()
    return row[0] if row else default


def touch(conn, pids):
    """Bump updated_at: the qualify batch re-derives verdicts (manual role tags count) and the map rev changes."""
    ts = db.now()
    conn.executemany('UPDATE people SET updated_at=? WHERE id=?', [(ts, p) for p in dict.fromkeys(pids)])


def add_manual(conn, pids, tags):
    for t in tags:
        grp = tag_group(conn, t)
        conn.executemany("INSERT OR REPLACE INTO tags VALUES(?,?,?,'manual')", [(p, t, grp) for p in pids])


def api_tag_edit(conn, q, b, pid):
    for key in ('add', 'remove'):
        if key in b and (not isinstance(b[key], list) or not all(isinstance(t, str) for t in b[key])):
            raise Bad(f'{key} must be a list of tags')
    additions = [clean_tag(t) for t in b.get('add') or []]
    relation_names = {label.casefold(): key for key, label in owner.RELATIONSHIPS.items()}
    conn.execute('BEGIN IMMEDIATE')
    try:
        person = with_owner(conn, dict(person_row(conn, pid)))
        selected = set(owner.relationships(person))
        relation_edit = False
        for t in additions:
            if t.casefold() in relation_names:
                selected.add(relation_names[t.casefold()])
                relation_edit = True
            else:
                add_manual(conn, [pid], [t])
        for t in b.get('remove') or []:
            conn.execute("DELETE FROM tags WHERE person_id=? AND tag=? AND source='manual'", (pid, t))
            if t.casefold() in relation_names:
                key = relation_names[t.casefold()]
                selected.discard(key)
                if key == 'worked_with':
                    selected.discard('client')
                relation_edit = True
        rules.sync(conn, [pid])
        if relation_edit:
            set_status(conn, [pid], relationships=owner.normalize_relationships(list(selected)))
        touch(conn, [pid])
        result = {'relationships': owner.normalize_relationships(list(selected))} if relation_edit else {}
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def api_tag_rename(conn, q, b):
    src, dst = clean_tag(b.get('from')), clean_tag(b.get('to'))
    fixed = {label.casefold() for label in owner.RELATIONSHIPS.values()}
    if src.casefold() in fixed or dst.casefold() in fixed:
        raise Bad('Edit relationships on the profile')
    dst = manual_tag(dst)
    if src == dst:
        return {'renamed': 0}
    pids = [r[0] for r in conn.execute("SELECT person_id FROM tags WHERE tag=? AND source='manual'", (src,))]
    grp = tag_group(conn, dst, tag_group(conn, src))
    # merge: someone who already has `to` (any source) keeps one tag, now manual
    conn.execute("INSERT INTO tags(person_id, tag, grp, source) SELECT person_id, ?, ?, 'manual' FROM tags WHERE tag=? AND source='manual' "
                 "ON CONFLICT(person_id, tag) DO UPDATE SET source='manual'", (dst, grp, src))
    conn.execute("DELETE FROM tags WHERE tag=? AND source='manual'", (src,))
    touch(conn, pids)
    conn.commit()
    return {'renamed': len(pids)}


def api_tag_delete(conn, q, b):
    tag = clean_tag(b.get('tag'))
    if tag.casefold() in {label.casefold() for label in owner.RELATIONSHIPS.values()}:
        raise Bad('Edit relationships on the profile')
    pids = [r[0] for r in conn.execute("SELECT person_id FROM tags WHERE tag=? AND source='manual'", (tag,))]
    conn.execute("DELETE FROM tags WHERE tag=? AND source='manual'", (tag,))
    touch(conn, pids)
    conn.commit()
    return {'deleted': len(pids)}


# ---------- tag rules ----------

def rule_out(conn, r):
    hits = conn.execute("SELECT count(*) FROM tags WHERE tag=? AND source='rule'", (r['tag'],)).fetchone()[0]
    return {'id': r['id'], 'tag': r['tag'], 'grp': r['grp'], 'field': r['field'], 'match': r['match'], 'hits': hits}


def api_rules(conn, q, b):
    return [rule_out(conn, r) for r in conn.execute('SELECT * FROM tag_rules ORDER BY tag, id')]


def api_rule_add(conn, q, b):
    tag, field = clean_tag(b.get('tag')), b.get('field')
    match = b.get('match').strip() if isinstance(b.get('match'), str) else b.get('match')
    try:
        rules.compile_match(field, match)
    except ValueError as e:
        raise Bad(str(e)) from None
    grp = b.get('grp') if b.get('grp') in rules.GROUPS else tag_group(conn, tag)
    row = conn.execute('SELECT * FROM tag_rules WHERE tag=? AND field=? AND match=?', (tag, field, match)).fetchone()
    if row:
        return rule_out(conn, row)
    rule, started = {'tag': tag, 'grp': grp, 'field': field, 'match': match}, db.now()
    try:
        pids = rules.matching_ids(conn, rule)  # read-only scan: ingest keeps writing meanwhile
    except ValueError as e:
        raise Bad(str(e)) from None
    rid = conn.execute('INSERT INTO tag_rules(tag, grp, field, match, created_at) VALUES(?,?,?,?,?)',
                       (tag, grp, field, match, db.now())).lastrowid
    rules.write_rule_tags(conn, rule, pids)
    # people ingested while we scanned did not know this rule yet
    rules.sync(conn, [r[0] for r in conn.execute('SELECT id FROM people WHERE updated_at>=?', (started,))])
    conn.commit()
    return rule_out(conn, conn.execute('SELECT * FROM tag_rules WHERE id=?', (rid,)).fetchone())


def api_rule_preview(conn, q, b):
    """How many people a rule would tag, without saving anything. Same matcher and guards as create."""
    rule = {'field': q.get('field', [''])[0], 'match': q.get('match', [''])[0].strip()}
    try:
        rules.compile_match(rule['field'], rule['match'])
        return {'hits': len(rules.matching_ids(conn, rule, budget=rules.PREVIEW_BUDGET))}
    except ValueError as e:
        raise Bad(str(e)) from None


def api_rule_delete(conn, q, b, rid):
    row = conn.execute('SELECT * FROM tag_rules WHERE id=?', (rid,)).fetchone()
    if not row:
        return {'deleted': 0}
    keep = []
    for other in rules.load(conn):  # another rule may give the same tag: scan for it before taking the write lock
        if other['tag'] == row['tag'] and other['id'] != rid:
            try:
                keep.append((other, rules.matching_ids(conn, other)))
            except ValueError:
                pass
    conn.execute('DELETE FROM tag_rules WHERE id=?', (rid,))
    conn.execute("DELETE FROM tags WHERE tag=? AND source='rule'", (row['tag'],))
    for other, pids in keep:
        rules.write_rule_tags(conn, other, pids)
    conn.commit()
    return {'deleted': 1}


def api_read(conn, q, b, pid):
    handle = person_row(conn, pid)['handle']
    if '~' in handle:  # parked row of an account that gave up this handle: there is no profile to read under it
        raise Bad('this account no longer has a handle to read')
    if not conn.execute("UPDATE jobs SET priority=? WHERE kind='profile' AND handle=? AND state IN ('queued','leased')",
                        (READ_PRIORITY, handle)).rowcount:
        conn.execute("INSERT INTO jobs(kind, handle, priority, created_at) VALUES('profile',?,?,?)", (handle, READ_PRIORITY, db.now()))
    conn.commit()
    return {}


# ---------- map ----------

def data_rev(conn):
    """Durable revision advances on every edit affecting leads, facets or the map (triggers in db.init)."""
    return db.get_setting(conn, 'lead_data_rev', 0)


CACHE = OrderedDict()   # (endpoint, db, query, data_rev) -> response; small LRU for /api/tags and /api/map
CACHE_MAX = 32
CACHE_LOCK = threading.Lock()


def cached(conn, name, q, compute):
    # Uncommitted revisions may be reused after rollback; never publish those results.
    if conn.in_transaction:
        return compute()
    # CFG is process-wide; the actual connection can point to another workspace.
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    database = path or ('memory', id(conn))
    with read_snapshot(conn):
        key = (name, database, datetime.now().date().isoformat(), tuple(sorted((k, tuple(v)) for k, v in q.items())), data_rev(conn))
        with CACHE_LOCK:
            if key in CACHE:
                CACHE.move_to_end(key)
                return CACHE[key]
        out = compute()
        with CACHE_LOCK:
            CACHE[key] = out
            while len(CACHE) > CACHE_MAX:
                CACHE.popitem(last=False)
        return out


def clear_caches():
    with CACHE_LOCK:
        CACHE.clear()
    SEED_LINKS[0] = None


SEED_LINKS = [None]   # [(key, computed_at, links)]: replaced whole, never mutated, so readers can't race a writer
SEED_LINKS_TOP = 50


def seed_links(conn, cacheable=None):
    # A caller's uncommitted revision can be reused after rollback. API reads
    # pass an explicit committed-snapshot flag from before cached() opens one.
    if cacheable is None:
        cacheable = not conn.in_transaction
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    # Overlap means people currently observed in both source lists. The
    # transactionally maintained distinct membership table avoids rejoining
    # every edge to evidence on each exact-revision refresh.
    members_ready = db.get_setting(conn, 'map_seed_member_v1', False) and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_member_%'").fetchone()[0] == 6 and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_seed_degree_%'").fetchone()[0] == 2
    membership_rev = db.get_setting(conn, 'map_membership_rev', None)
    revision_ready = members_ready and membership_rev is not None and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_overlap_rev_%'").fetchone()[0] == 3
    revision = ('membership', membership_rev) if revision_ready else ('all', data_rev(conn))
    key = (path or ('memory', id(conn)), revision)
    cached = SEED_LINKS[0] if cacheable else None
    if cached and cached[0] == key:
        return cached[2]
    # Aggregate overlap in SQLite and return only the top pairs. This avoids
    # grouping every person's source list into strings or materializing every
    # member in Python on each exact-revision refresh.
    membership = 'map_seed_member' if members_ready else '(SELECT DISTINCT person_id,seed FROM current_edges)'
    # Without this hint SQLite scans the seed-first secondary index, making
    # each person lookup jump across the entire membership table.
    outer = 'map_seed_member a NOT INDEXED' if members_ready else f'{membership} a'
    top = conn.execute(f'SELECT a.seed,b.seed,count(*) AS shared FROM {outer} '
                       f'JOIN {membership} b ON b.person_id=a.person_id AND b.seed>a.seed '
                       'GROUP BY a.seed,b.seed ORDER BY shared DESC,a.seed,b.seed LIMIT ?', (SEED_LINKS_TOP,))
    links = [{'source': f's:{a}', 'target': f's:{b}', 'shared': n} for a, b, n in top]
    if cacheable:
        SEED_LINKS[0] = (key, datetime.now().timestamp(), links)
    return links


def api_map(conn, q, b):
    # cached() reads the revision and computes the complete response in one
    # snapshot. Capturing the caller's state first keeps rollback-only reads
    # out of both caches while allowing committed UI polls to reuse the result.
    committed = not conn.in_transaction
    return cached(conn, 'map', q, lambda: dict(map_graph(conn, q), seed_links=seed_links(conn, cacheable=committed)))


def api_connections(conn, q, b):
    """Inspect observed connections independently of qualification and map display caps."""
    limit = qint(q, 'limit') if 'limit' in q else 20
    if limit is None or not 1 <= limit <= 100:
        raise Bad('limit must be between 1 and 100')
    try:
        return connection_graph.compare(conn, q.get('source', [''])[0], q.get('target', [''])[0], limit)
    except ValueError as exc:
        raise Bad(str(exc)) from exc


def map_graph(conn, q):
    limit = min(10000, max(10, qint(q, 'limit') or 400))
    # The general lead seed filter intentionally includes discovery history. On the map,
    # a seed filter must mean an observed connection to that seed.
    where, args = lead_filter({k: v for k, v in q.items() if k != 'seed'})
    for seed in dict.fromkeys(map(db.norm_handle, csv(q, 'seed'))):
        where.append('p.id IN (SELECT person_id FROM current_edges WHERE seed=?)')
        args.append(seed)
    # Older databases, or an interrupted backfill without the ready marker,
    # retain the exact read-through query until db.init repairs the summary.
    people_ready = db.get_setting(conn, 'map_people_present_v1', False) and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_people_%'").fetchone()[0] == 3
    summary_ready = people_ready and db.get_setting(conn, 'map_person_degree_v1', False) and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_degree_%'").fetchone()[0] == 6
    if summary_ready:
        where = [part.replace(LISTS, 'd.degree') for part in where]
    sources_ready = db.get_setting(conn, 'map_source_handles_v1', False) and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_sources_%'").fetchone()[0] == 6
    rank_ready = summary_ready and sources_ready and people_ready and db.get_setting(conn, 'map_rank_v1', False) and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_rank_%'").fetchone()[0] == 6
    excluded = ('p.handle NOT IN (SELECT handle FROM map_source_handles)' if sources_ready else
                'p.handle NOT IN (SELECT handle FROM seeds UNION SELECT seed FROM edges)')
    cond = ' AND '.join([excluded] + where)
    # Materialize only ranking fields for the full match set. Display fields
    # are fetched after the limit; otherwise millions of names, notes and
    # reasons are copied into SQLite's temporary sort table on every refresh.
    if summary_ready:
        base = ('SELECT p.id, v.score, d.degree FROM map_person_degree d JOIN people p ON p.id=d.person_id '
                f'LEFT JOIN verdicts v ON v.person_id=p.id LEFT JOIN marks m ON m.person_id=p.id WHERE {cond}')
    else:
        base = ('SELECT p.id, v.score, count(DISTINCT e.seed) AS degree '
                'FROM people p JOIN current_edges e ON e.person_id=p.id '
                f'LEFT JOIN verdicts v ON v.person_id=p.id LEFT JOIN marks m ON m.person_id=p.id WHERE {cond} GROUP BY p.id')
    by_score = 'ORDER BY score IS NULL, score DESC, degree DESC, id LIMIT ?'
    multi_n = 0 if q.get('scope', ['leads'])[0] == 'all' else limit * 3 // 5  # scope=leads: people in several lists first
    # The common unfiltered overview can follow two exact ranking indexes and
    # stop after its display limit. Other filters still use the general query.
    # The UI always adds its local date; it changes only follow-up filters.
    simple = rank_ready and set(q) <= {'scope', 'limit', 'today'}
    if simple:
        source_cond = 'd.hidden=0 AND ' + excluded
        source_join = 'FROM map_person_degree d JOIN people p ON p.id=d.person_id '
        total = conn.execute('SELECT count(*) FROM map_person_degree WHERE hidden=0').fetchone()[0]
        # Force the tiny source registry outermost; SQLite otherwise sometimes
        # scans every ranked person before joining its handle.
        total -= conn.execute('SELECT count(*) FROM map_source_handles h CROSS JOIN people p '
                              'CROSS JOIN map_person_degree d WHERE p.handle=h.handle '
                              'AND d.person_id=p.id AND d.hidden=0').fetchone()[0]
        rows = []
        if multi_n:
            rows.extend(conn.execute('SELECT 0 AS part,d.person_id AS id,d.score,d.degree '
                                     f'{source_join} WHERE {source_cond} AND d.degree>=2 '
                                     'ORDER BY d.degree DESC,d.score DESC,d.person_id LIMIT ?', (multi_n,)).fetchall())
        rows.extend(conn.execute('SELECT 1 AS part,d.person_id AS id,d.score,d.degree '
                                 f'{source_join} WHERE {source_cond} '
                                 'ORDER BY d.score IS NULL,d.score DESC,d.degree DESC,d.person_id LIMIT ?',
                                 (limit,)).fetchall())
    else:
        rows = conn.execute(f"""WITH b AS MATERIALIZED ({base})
            SELECT (SELECT count(*) FROM b) AS total, picked.* FROM (
              SELECT * FROM (SELECT 0 AS part, * FROM b WHERE degree>=2 ORDER BY degree DESC, score DESC, id LIMIT ?)
              UNION ALL SELECT * FROM (SELECT 1 AS part, * FROM b {by_score})
            ) picked""", (*args, multi_n, limit)).fetchall()
        total = rows[0]['total'] if rows else 0
    multi = [r for r in rows if r['part'] == 0]
    seen = {r['id'] for r in multi}
    picked = multi + [r for r in rows if r['part'] == 1 and r['id'] not in seen][:limit - len(multi)]
    display = {}
    for chunk in chunks([r['id'] for r in picked]):
        marks = ','.join('?' * len(chunk))
        for row in conn.execute('SELECT p.id,p.handle,p.name,p.pic_file,p.followers,'
                                'v.tier,v.score,v.content_fit,v.reason,m.status,m.note '
                                'FROM people p LEFT JOIN verdicts v ON v.person_id=p.id '
                                'LEFT JOIN marks m ON m.person_id=p.id '
                                f'WHERE p.id IN ({marks})', chunk):
            display[row['id']] = row
    people = [dict(display[r['id']], degree=r['degree']) for r in picked]
    members_ready = db.get_setting(conn, 'map_seed_member_v1', False) and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_member_%'").fetchone()[0] == 6 and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_seed_degree_%'").fetchone()[0] == 2
    source_from = 'map_source_handles' if sources_ready else '(SELECT handle FROM seeds UNION SELECT seed FROM edges)'
    seed_degree = ('coalesce(md.degree,0)' if members_ready else
                   '(SELECT count(DISTINCT e.person_id) FROM current_edges e WHERE e.seed=s.handle)')
    degree_join = 'LEFT JOIN map_seed_degree md ON md.seed=s.handle ' if members_ready else ''
    seeds = conn.execute(f'SELECT s.handle, {seed_degree} AS degree, p.id AS pid, '
                         'p.pic_file, p.followers, v.tier, v.score, m.status, m.note, coalesce(sd.is_me, 0) AS is_me '
                         f'FROM {source_from} s LEFT JOIN seeds sd ON sd.handle=s.handle '
                         f'{degree_join}LEFT JOIN people p ON p.handle=s.handle LEFT JOIN verdicts v ON v.person_id=p.id '
                         'LEFT JOIN marks m ON m.person_id=p.id').fetchall()
    node_of = {r['id']: f"p:{r['id']}" for r in people}
    node_of.update((s['pid'], f"s:{s['handle']}") for s in seeds if s['pid'])
    links, seeds_of, tags, alltags = [], {}, {}, {}
    map_owners = {r['id']: dict(r) for r in people}
    map_owners.update({s['pid']: dict(s, id=s['pid']) for s in seeds if s['pid']})
    raw_tags = {}
    for chunk in chunks(node_of):
        marks = ','.join('?' * len(chunk))
        for e in conn.execute(f'SELECT e.*,v.active,v.observed_at,v.checked_at FROM edges e '
                              'LEFT JOIN edge_evidence v ON v.seed=e.seed AND v.person_id=e.person_id AND v.direction=e.direction '
                              f'WHERE e.person_id IN ({marks}) ORDER BY e.seed,e.direction', chunk):
            links.append({'source': f"s:{e['seed']}", 'target': node_of[e['person_id']], 'direction': e['direction'],
                          'state': 'observed' if e['active'] == 1 else 'absent' if e['active'] == 0 else 'unverified',
                          'observed_at': e['observed_at'], 'checked_at': e['checked_at']})
            if e['active'] == 1 and e['seed'] not in seeds_of.setdefault(e['person_id'], []):
                seeds_of[e['person_id']].append(e['seed'])
        for t in conn.execute(f'SELECT t.* FROM ({tag_projection.relation()}) t WHERE t.person_id IN ({marks}) ORDER BY t.person_id, {TAG_ORDER}', chunk):
            raw_tags.setdefault(t['person_id'], []).append(dict(t))
    owner.hydrate(conn, list(map_owners.values()))
    for pid, person in map_owners.items():
        raw = raw_tags.get(pid, [])
        person['manual_tags'] = [t['tag'] for t in raw if t['source'] == 'manual']
        visible = owner.visible_tags(person, raw)
        alltags[pid] = {t['tag'] for t in visible}
        tags[pid] = [t['tag'] for t in visible[:MAP_TAGS]]
    nodes = [{'id': f"s:{s['handle']}", 'kind': 'seed', 'label': s['handle'], 'tier': s['tier'], 'score': s['score'],
              'pic': f"/img/{s['pid']}" if s['pic_file'] else None, 'degree': s['degree'], 'followers': s['followers'],
              'status': s['status'], 'lists': len(seeds_of.get(s['pid'], [])), 'tags': tags.get(s['pid'], []),
              'seeds': seeds_of.get(s['pid'], []), 'is_me': bool(s['is_me']), 'pid': s['pid'], 'note': s['note'] or None} for s in seeds]
    nets = network_context(conn, [r['id'] for r in people]) if people else {}
    nodes += [{'id': f"p:{r['id']}", 'kind': 'lead', 'label': r['handle'], 'handle': r['handle'], 'name': r['name'],
               'tier': r['tier'] or 'unread', 'fit': ('strong' if r['content_fit'] >= 70 else 'good' if r['content_fit'] >= 45 else 'weak') if r['content_fit'] is not None else 'unread',
               'score': r['score'], 'business_fit': round(r['content_fit']) if r['content_fit'] is not None else None,
               'connection_strength': qualify.network_strength(nets[r['id']]), 'reason': r['reason'],
               'relationship': nets[r['id']]['me'],
               'tags': tags.get(r['id'], []), 'judge': judge(alltags.get(r['id'], set())),
               'pic': f"/img/{r['id']}" if r['pic_file'] else None, 'degree': r['degree'], 'lists': r['degree'],
               'status': r['status'], 'note': r['note'] or None, 'followers': r['followers'], 'seeds': seeds_of.get(r['id'], [])} for r in people]
    owner_links = owner_relationships.facts(conn, [r['id'] for r in people] + [s['pid'] for s in seeds if s['pid']])
    for node in nodes:
        pid = node.get('pid') if node['kind'] == 'seed' else int(node['id'].split(':', 1)[1])
        node.update(owner_links.get(pid, {'owner_relationship': None, 'relationship_owner': OWNER_HANDLE, 'relationship_evidence': []}))
        facts = map_owners.get(pid, {})
        node['relationships'] = owner.relationships(facts)
        node['familiarity'] = facts.get('familiarity')
        node['owner_status'] = owner.owner_status(facts)
        node.update(owner.owner_recommendation(facts, node))
    return {'nodes': nodes, 'links': links, 'total': total, 'limit': limit, 'rev': data_rev(conn)}


def ext_aggregate(conn, accts, now):
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


def api_scraper(conn, q, b):
    now = datetime.now(timezone.utc)
    accts = accounts.listing(conn, now)
    lists, coverage = list_coverage(conn)
    stages = control.snapshot(conn, ai_left(conn))['stages']
    return {'ext': ext_aggregate(conn, accts, now), 'accounts': accts, 'rate': accounts.aggregate_rate(accts),
            'alerts': accounts.alerts(conn, now, accts),
            'paused': bool(db.get_setting(conn, 'paused')),
            'qualify': bool(db.get_setting(conn, 'qualify')), 'qualify_auto': bool(db.get_setting(conn, 'qualify_auto')),
            'local_laya': bool(db.get_setting(conn, 'local_laya')),
            'llm': api_llm(conn, q, b),
            'soak': soak(conn, now), 'progress': progress(conn, accts),
            'coverage': {'lists': coverage, 'local': local_coverage(conn)},
            'stages': stages,
            'people_today': conn.execute('SELECT count(*) FROM people WHERE first_seen>=?', (iso(now)[:10],)).fetchone()[0],
            'lists': lists,
            'queue': dict.fromkeys(('list', 'profile'), 0) | dict(conn.execute(
                "SELECT kind, count(*) FROM jobs WHERE state IN ('queued','leased') GROUP BY kind").fetchall())}


def api_scraper_status(conn, q, b):
    """Small poll for the navigation strip; the full scraper report is for its page."""
    now = datetime.now(timezone.utc)
    accts = accounts.listing(conn, now, include_lists=False)
    stages = control.snapshot(conn, ai_left(conn))['stages']
    return {'ext': ext_aggregate(conn, accts, now), 'accounts': accts,
            'rate': accounts.aggregate_rate(accts),
            'alerts': accounts.alerts(conn, now, accts),
            'paused': bool(db.get_setting(conn, 'paused')),
            'qualify': bool(db.get_setting(conn, 'qualify')),
            'qualify_auto': bool(db.get_setting(conn, 'qualify_auto')),
            'local_laya': bool(db.get_setting(conn, 'local_laya')),
            'stages': stages,
            'queue': dict.fromkeys(('list', 'profile'), 0) | dict(conn.execute(
                "SELECT kind, count(*) FROM jobs WHERE state IN ('queued','leased') GROUP BY kind").fetchall())}


def eta_hours(left, per_hour):
    return round(left / per_hour, 2) if left and per_hour else (0 if not left else None)


RATE_WINDOW = timedelta(hours=6)   # measured throughput includes pacing breaks and cooldowns
OBSERVED_RATE_WINDOW = timedelta(minutes=1)


def measured_rate(conn, sql, now):
    """Recent hourly pace; do not project an ETA after an hour without saved work."""
    since = iso(now - RATE_WINDOW)
    n, first, last = conn.execute(sql, (since,)).fetchone()
    if not n or not first or not last or now - utc(last) >= timedelta(hours=1):
        return None
    hours = max(0.25, (now - utc(first)).total_seconds() / 3600)
    return n / hours


def observed_per_minute(conn, sql, now):
    """Count persisted work in the exact trailing minute; None means no history to measure."""
    since = iso(now - OBSERVED_RATE_WINDOW)
    recent, all_time = conn.execute(sql, (since,)).fetchone()
    return recent if all_time else None


def eta_with_budget(left, per_hour, per_request, lanes, kind, now):
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


def progress(conn, accts):
    """Plain numbers for the Scraper page: what is left, how fast it really goes (measured), when it is done."""
    now = datetime.now(timezone.utc)
    lanes = [a for a in accts if not a['paused']]
    list_lanes = [a for a in lanes if (a['role'] or 'both') in ('lists', 'both')]
    bio_lanes = [a for a in lanes if (a['role'] or 'both') in ('bios', 'both')]
    # Unknown list sizes fall back to the seed's follower/following count, so the total is an estimate.
    lists_left, unknown = conn.execute(
        "SELECT coalesce(sum(max(coalesce(l.total, CASE l.direction WHEN 'followers' THEN p.followers ELSE p.following END, 0)"
        " - l.received, 0)), 0), count(CASE WHEN l.total IS NULL THEN 1 END) FROM lists l "
        "LEFT JOIN people p ON p.handle=l.seed WHERE l.state NOT IN ('done','private','error','partial')").fetchone()
    incomplete_lists, incomplete_left, capped_lists = conn.execute(
        "SELECT count(*), coalesce(sum(max(coalesce(l.total, CASE l.direction WHEN 'followers' THEN p.followers "
        "ELSE p.following END, l.received) - l.received, 0)), 0), "
        "count(CASE WHEN l.state='partial' AND l.error LIKE 'Instagram limited this list;%' THEN 1 END) "
        "FROM lists l LEFT JOIN people p ON p.handle=l.seed WHERE l.state IN ('partial','error')").fetchone()
    pages_h = measured_rate(conn, 'SELECT count(*), min(at), max(at) FROM pages WHERE at>=?', now)
    people_h = measured_rate(conn, 'SELECT coalesce(sum(users), 0), min(at), max(at) FROM pages WHERE at>=?', now)
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
    bios_h = measured_rate(conn, 'SELECT count(*), min(bio_at), max(bio_at) FROM people WHERE bio_at>=?', now)
    lists_min = observed_per_minute(conn, 'SELECT coalesce(sum(users),0), (SELECT count(*) FROM pages) FROM pages WHERE at>=?', now)
    bios_min = observed_per_minute(conn, 'SELECT count(*), (SELECT count(*) FROM people WHERE bio_at IS NOT NULL) FROM people WHERE bio_at>=?', now)
    q_left = conn.execute(f"SELECT count(*) FROM people p JOIN verdicts v ON v.person_id=p.id WHERE {NOT_ME} AND coalesce(p.bio,'')!='' "
                          "AND v.model='rules' AND v.updated_at=p.updated_at AND (coalesce(v.prefilter,0)+coalesce(v.score,0))/2>=?",
                          (db.get_setting(conn, 'llm_min') or 0,)).fetchone()[0]
    q_rate = measured_rate(conn, 'SELECT count(*), min(scored_at), max(scored_at) FROM ai_scoring_events WHERE scored_at>=?', now)
    q_hour = conn.execute('SELECT count(*) FROM ai_scoring_events WHERE scored_at>=?',
                          (iso(now - timedelta(hours=1)),)).fetchone()[0]
    bio_budget = (db.get_setting(conn, 'budget') or {}).get('profile') or 0
    return {
        'lists': {'left': lists_left, 'estimate': bool(unknown),
                  'incomplete_lists': incomplete_lists, 'incomplete_left': incomplete_left,
                  'capped_lists': capped_lists, 'per_hour': round(people_h) if people_h else None,
                  'per_minute': lists_min,
                  'eta_h': eta_with_budget(lists_left, people_h, per_page, list_lanes, 'list', now)},
        'bios': {'left': bios_left, 'queued': queued, 'per_hour': round(bios_h) if bios_h else None,
                 'per_minute': bios_min,
                 'per_day': sum(a['budget'].get('profile') or 0 for a in bio_lanes) or bio_budget * max(1, len(bio_lanes)),
                 'eta_h': eta_with_budget(bios_left, bios_h, 1, bio_lanes, 'profile', now),
                 'estimate': not bios_h},
        'qualify': {'left': q_left, 'per_hour': q_hour, 'per_minute': conn.execute(
                    'SELECT count(*) FROM ai_scoring_events WHERE scored_at>=?',
                    (iso(now - timedelta(minutes=1)),)).fetchone()[0],
                    'eta_h': eta_hours(q_left, q_rate),
                    'on': bool(db.get_setting(conn, 'qualify')), 'workers': db.get_setting(conn, 'llm_workers'),
                    'keys': len(llm.get().keys) if hasattr(llm, 'get') else None},
    }


def list_coverage(conn):
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
            reason = list_completion_reason(row['error']) or 'Only part of this list was saved.'
        elif row['state'] in ('private', 'error'):
            completion = 'blocked'
            reason = list_completion_reason(row['error']) or ('Access to this list was denied.' if row['state'] == 'private' else 'Collection stopped with an error.')
        elif row['state'] == 'running':
            completion, reason = 'collecting', None
        else:
            completion, reason = 'waiting', None
        item = {'seed': row['seed'], 'direction': row['direction'], 'state': row['state'],
                'received': row['received'], 'total': row['total'], 'updated_at': row['updated_at'],
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


def list_completion_reason(error):
    """Keep a readable reason; raw error samples may contain an entire HTML page."""
    if not error:
        return None
    if error.startswith('Instagram limited this list;'):
        return 'Instagram limited this list.'
    if 'list_html_home_redirect' in error:
        return 'Instagram returned its home page instead of list data.'
    return error.split(' | ', 1)[0][:240]


_local_coverage_lock = threading.Lock()
_local_coverage_cache = {}


def local_coverage(conn):
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
    with _local_coverage_lock:
        cached = _local_coverage_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 30:
            eligible, pending, sampled_at = cached[1:]
        else:
            eligible = conn.execute(f"SELECT count(*) FROM people p WHERE instr(p.handle,'~')=0 AND {NOT_ME}").fetchone()[0]
            pending = conn.execute('SELECT count(*) FROM laya_queue').fetchone()[0]
            sampled_at = db.now()
            _local_coverage_cache.clear()
            _local_coverage_cache[cache_key] = (time.monotonic(), eligible, pending, sampled_at)
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


def soak(conn, now):
    out = {}
    for label, hours in (('1h', 1), ('6h', 6)):
        since = iso(now - timedelta(hours=hours))
        out[label] = {'pages': conn.execute('SELECT count(*) FROM pages WHERE at>=?', (since,)).fetchone()[0],
                      'people': conn.execute('SELECT count(*) FROM edges WHERE first_seen>=?', (since,)).fetchone()[0],
                      'new_people': conn.execute('SELECT count(*) FROM people WHERE first_seen>=?', (since,)).fetchone()[0],
                      'profiles': conn.execute('SELECT count(*) FROM people WHERE bio_at>=?', (since,)).fetchone()[0]}
    return out


def api_biofetch_get(conn, q, b=None):
    return biofetch.public(conn)


def api_biofetch(conn, q, b):
    """{"on"?, "token"?, "ig_user_id"?, "gap"?}: bios from the Meta Graph API (business_discovery), off by default."""
    try:
        return biofetch.save(conn, b)
    except ValueError as e:
        raise Bad(str(e))


def api_qualify(conn, q, b):
    """{"on"?, "auto"?, "local_laya"?, "workers"?, "llm_min"?, "bio_min"?}: absent keys stay unchanged."""
    if 'on' in b and not isinstance(b['on'], bool):
        raise Bad('on must be true or false')
    if 'auto' in b and not isinstance(b['auto'], bool):
        raise Bad('auto must be true or false')
    if 'local_laya' in b and not isinstance(b['local_laya'], bool):
        raise Bad('local_laya must be true or false')
    if b.get('local_laya', db.get_setting(conn, 'local_laya')) and b.get('on', db.get_setting(conn, 'qualify')):
        raise Bad('turn external AI off to use local-only Laya')
    if b.get('auto') and b.get('local_laya', db.get_setting(conn, 'local_laya')):
        raise Bad('automatic external AI cannot run in local-only mode')
    for key, lo, hi in (('workers', 1, 32), ('llm_min', 0, 100), ('bio_min', 0, 100)):
        if key in b and (not isinstance(b[key], int) or isinstance(b[key], bool) or not lo <= b[key] <= hi):
            raise Bad(f'{key} must be a whole number {lo}-{hi}')
    if 'on' in b:
        # All AI switches share the control strip's durable off behavior.
        control.set_stage(conn, 'ai', pause=not b['on'])
    if 'auto' in b:
        # An explicit request to schedule later activation is separate from switching off now.
        db.set_setting(conn, 'qualify_auto', b['auto'])
    if 'local_laya' in b:
        db.set_setting(conn, 'local_laya', b['local_laya'])
        if b['local_laya']:
            db.set_setting(conn, 'qualify_auto', False)
    if 'workers' in b:
        db.set_setting(conn, 'llm_workers', b['workers'])
    for key in ('llm_min', 'bio_min'):
        if key in b:
            db.set_setting(conn, key, b[key])
    conn.commit()
    result = {'qualify': bool(db.get_setting(conn, 'qualify'))}
    if 'local_laya' in b:
        result['local_laya'] = bool(db.get_setting(conn, 'local_laya'))
    return result


def api_llm(conn, q, b):
    """Providers (keys masked), cooldowns, requests today, last error; plus the Laya sidecar and the pool settings."""
    out = llm.get().status()
    out.update(workers=db.get_setting(conn, 'llm_workers'), llm_min=db.get_setting(conn, 'llm_min'),
               bio_min=db.get_setting(conn, 'bio_min'), laya={'url': laya.URL, 'up': laya.last_known()},
               config=str(llm.CONFIG.relative_to(ROOT)) if llm.CONFIG.is_relative_to(ROOT) else str(llm.CONFIG))
    auto = llm.read_config().get('auto_models') or {}
    out['auto_models'] = {k: auto.get(k) for k in ('stealth', 'free', 'at', 'error', 'new_stealth')}
    counts = {}
    for p in out['providers']:
        counts[p.get('state') or 'untested'] = counts.get(p.get('state') or 'untested', 0) + 1
    out['summary'] = counts   # e.g. {'ok': 2, 'spent': 2, 'error': 1}: spent keys are not broken, they return at 00:00 UTC
    out['verdicts'] = dict(conn.execute("SELECT CASE WHEN model IN ('rules','error') THEN model ELSE 'llm' END, count(*) FROM verdicts "
                                        'GROUP BY 1').fetchall())
    out['usage'] = llm_usage_report()
    return out


def llm_usage_report(days=30, purpose='qualification'):
    pool = llm.get()
    path = pool.usage_path or usage_ledger.PATH
    gap = pool.usage_gap or usage_ledger.read_gap(path)
    try:
        report = usage_ledger.summary(path, days, purpose)
    except (OSError, ValueError, sqlite3.Error):
        return {'available': False, 'error': 'usage_ledger_unavailable',
                'accounting_gap': gap, 'window_days': days, 'purpose': purpose}
    first = report['recording_since']
    covered_window = bool(first) and datetime.fromisoformat(first) <= datetime.now(timezone.utc) - timedelta(days=days)
    return dict(report, available=True, complete=gap is None and covered_window, accounting_gap=gap)


def api_llm_usage(conn, q, b):
    raw = (q.get('days') or ['30'])[0]
    if not raw.isdigit() or not 1 <= int(raw) <= 365:
        raise Bad('days must be 1-365')
    purpose = (q.get('purpose') or ['qualification'])[0]
    if purpose not in ('qualification', 'website_summary', 'provider_test', 'all'):
        raise Bad('unknown usage purpose')
    return llm_usage_report(int(raw), purpose)


def api_llm_health(conn, q, b):
    """Probes now: is the local OpenRouter proxy listening, does the Laya sidecar answer /health."""
    url = urlparse(llm.get().proxy)
    try:
        socket.create_connection((url.hostname, url.port or 80), timeout=1).close()
        proxy = True
    except OSError:
        proxy = False
    laya.reset()
    return {'proxy': {'url': f'{url.scheme}://{url.netloc}', 'up': proxy}, 'laya': {'url': laya.URL, 'up': laya.available()}}


def api_llm_key_add(conn, q, b):
    try:
        return {'id': llm.add_key(b.get('key'))}
    except ValueError as e:
        raise Bad(str(e)) from None


def api_llm_key_remove(conn, q, b, pid):
    try:
        llm.remove_key(pid)
    except LookupError:
        raise NotFound('no such key') from None
    except ValueError as e:
        raise Bad(str(e)) from None
    return {}


def api_llm_key_test(conn, q, b, pid):
    try:
        return llm.get().test(pid)
    except LookupError:
        raise NotFound('no such key') from None


def api_scout(conn, q, b=None):
    return deepscout.status(conn)


def api_scout_set(conn, q, b):
    if 'on' in b:
        if not isinstance(b['on'], bool):
            raise Bad('on must be true or false')
        db.set_setting(conn, 'scout', b['on'])
    if 'model' in b:
        if b['model'] not in deepscout.MODELS:
            raise Bad('unknown model')
        db.set_setting(conn, 'scout_model', b['model'])
    if 'workers' in b:
        if isinstance(b['workers'], bool) or not isinstance(b['workers'], int) or not 1 <= b['workers'] <= 8:
            raise Bad('workers must be 1-8')
        db.set_setting(conn, 'scout_workers', b['workers'])
    conn.commit()
    return deepscout.status(conn)


def api_llm_models_refresh(conn, q, b):
    rec = llm.refresh_models(force=True)
    st = llm.get().status()
    return {'models': st['models'], 'auto': {k: rec.get(k) for k in ('stealth', 'free', 'at', 'error', 'new_stealth')}}


def api_llm_models(conn, q, b):
    try:
        llm.set_models(b.get('models'), b.get('daily_limit'))
    except ValueError as e:
        raise Bad(str(e)) from None
    st = llm.get().status()
    return {'models': st['models'], 'daily_limit': st['daily_limit']}


SNOWBALL_MAX = 50


def api_collection_suggestions(conn, q, b):
    return collection_suggestions.suggest(conn, qint(q, 'limit') or 6)


def api_snowball(conn, q, b):
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


def api_seeds(conn, q, b):
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


def api_pause(conn, q, b):
    if not isinstance(b.get('paused'), bool):
        raise Bad('paused must be an explicit boolean')
    if b['paused'] is False:
        require_collection_resume(conn)
    db.set_setting(conn, 'paused', b['paused'])
    conn.commit()
    return {}


def ai_left(conn):
    return conn.execute(f"SELECT count(*) FROM people p JOIN verdicts v ON v.person_id=p.id WHERE {NOT_ME} AND coalesce(p.bio,'')!='' "
                        "AND v.model='rules' AND v.updated_at=p.updated_at AND (coalesce(v.prefilter,0)+coalesce(v.score,0))/2>=?",
                        (db.get_setting(conn, 'llm_min') or 0,)).fetchone()[0]


def api_control(conn, q, b):
    """What each stage (lists, bios, AI) and each account is doing right now, in plain sentences."""
    return control.snapshot(conn, ai_left(conn))


def require_collection_resume(conn):
    # Serialize the check with the subsequent resume so a warning cannot land between them.
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    try:
        if workspace_cooldown(conn, datetime.now(timezone.utc)):
            raise Bad('Instagram is on a shared safety hold. Collection remains paused.')
    except Exception:
        conn.rollback()
        raise


def api_control_set(conn, q, b):
    """{"stage": collection|lists|bios|ai|all, "action": pause|resume} or {"account": lane, "action": ...} → the new snapshot."""
    if (b.get('action') == 'start_all' or
            b.get('action') == 'resume' and (b.get('stage') in ('collection', 'lists', 'bios', 'all') or
                                          b.get('stage') is None and isinstance(b.get('account'), str))):
        require_collection_resume(conn)
    try:
        control.apply(conn, b)
    except LookupError:
        conn.rollback()
        raise NotFound('no such account') from None
    except Exception:
        conn.rollback()
        raise
    return api_control(conn, q, b)


def api_engine_start(conn, q, b):
    """Start local services. Collection keeps its current pause and safety state."""
    try:
        account_count = conn.execute('SELECT count(*) FROM accounts').fetchone()[0]
        return engine_start.start(ROOT, account_count)
    except engine_start.EngineStartError as exc:
        raise Bad(str(exc)) from None


def api_budget(conn, q, b):
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


def api_accounts(conn, q, b):
    now = datetime.now(timezone.utc)
    accts = accounts.listing(conn, now)
    return {'accounts': accts, 'alerts': accounts.alerts(conn, now, accts), 'rate': accounts.aggregate_rate(accts),
            'main_list_share': float(db.get_setting(conn, 'main_list_share') or 0)}


def api_account_settings(conn, q, b):
    """{"main_list_share": 0-1}: the share of list pages Michael's own account may take (0 = bios only)."""
    v = b.get('main_list_share')
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 1:
        raise Bad('main_list_share must be a number 0-1')
    db.set_setting(conn, 'main_list_share', round(float(v), 2))
    conn.commit()
    return {'main_list_share': round(float(v), 2)}


def api_account_edit(conn, q, b, lane):
    conn.execute('BEGIN IMMEDIATE')
    if b.get('paused') is False:
        require_collection_resume(conn)
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


def api_account_remove(conn, q, b, lane):
    conn.execute('BEGIN IMMEDIATE')
    n = accounts.remove(conn, lane)
    conn.commit()
    return {'removed': n}


def api_setup(conn, q, b):
    """What the add-account wizard shows: where the unpacked extension lives and which id Chrome gives it."""
    manifest = json.loads((ROOT / 'extension' / 'manifest.json').read_text())
    return {'repo': str(ROOT), 'extension_path': str(ROOT / 'extension'), 'extension_id': EXT_ORIGIN.split('//')[1],
            'extension_version': manifest.get('version'), 'server': f"http://127.0.0.1:{CFG['port']}",
            'lanes': conn.execute('SELECT count(*) FROM accounts').fetchone()[0]}


LANE = r'(?P<lane>[A-Za-z0-9_-]{1,64})'   # named groups stay text; unnamed (\d+) groups become ints
KEY = r'(?P<key>proxy|[0-9a-f]{10})'
ROUTES = [
    ('GET', r'/api/accounts', api_accounts), ('POST', rf'/api/accounts/{LANE}', api_account_edit),
    ('POST', rf'/api/accounts/{LANE}/remove', api_account_remove), ('GET', r'/api/setup', api_setup),
    ('POST', r'/api/settings/accounts', api_account_settings),
    ('GET', r'/api/ext/next', ext_next), ('POST', r'/api/ext/list-page', ext_list_page),
    ('POST', r'/api/ext/profile', ext_profile), ('POST', r'/api/ext/error', ext_error),
    ('POST', r'/api/ext/request', ext_request), ('POST', r'/api/ext/heartbeat', ext_heartbeat),
    ('GET', r'/api/control', api_control), ('POST', r'/api/control', api_control_set),
    ('POST', r'/api/engine/start', api_engine_start),
    ('GET', r'/api/ext/control', api_control), ('POST', r'/api/ext/control', api_control_set),
    ('GET', r'/api/leads', api_leads), ('GET', r'/api/tags', api_tags), ('GET', r'/api/counts', api_counts),
    ('POST', r'/api/tags/rename', api_tag_rename), ('POST', r'/api/tags/delete', api_tag_delete),
    ('GET', r'/api/tag-rules', api_rules), ('POST', r'/api/tag-rules', api_rule_add), ('GET', r'/api/tag-rules/preview', api_rule_preview),
    ('POST', r'/api/tag-rules/(\d+)/delete', api_rule_delete),
    ('GET', r'/api/person/(\d+)', api_person), ('POST', r'/api/person/(\d+)/mark', api_mark),
    ('POST', r'/api/person/(\d+)/tags', api_tag_edit), ('POST', r'/api/person/(\d+)/read', api_read),
    ('GET', r'/api/map', api_map), ('GET', r'/api/connections', api_connections),
    ('GET', r'/api/scraper/status', api_scraper_status), ('GET', r'/api/scraper', api_scraper),
    ('GET', r'/api/scraper/suggestions', api_collection_suggestions),
    ('POST', r'/api/scraper/seeds', api_seeds), ('POST', r'/api/scraper/pause', api_pause),
    ('POST', r'/api/scraper/budget', api_budget), ('POST', r'/api/scraper/snowball', api_snowball),
    ('POST', r'/api/settings/qualify', api_qualify),
    ('GET', r'/api/settings/biofetch', api_biofetch_get), ('POST', r'/api/settings/biofetch', api_biofetch),
    ('GET', r'/api/llm', api_llm), ('GET', r'/api/llm/usage', api_llm_usage),
    ('GET', r'/api/llm/health', api_llm_health), ('POST', r'/api/llm/keys', api_llm_key_add),
    ('POST', rf'/api/llm/keys/{KEY}/remove', api_llm_key_remove), ('POST', rf'/api/llm/keys/{KEY}/test', api_llm_key_test),
    ('GET', r'/api/scout', api_scout), ('POST', r'/api/settings/scout', api_scout_set),
    ('POST', r'/api/llm/models', api_llm_models), ('POST', r'/api/llm/models/refresh', api_llm_models_refresh),
]
import qual_api  # noqa: E402  Qualification page endpoints (web/frontend module)
ROUTES += qual_api.routes(sys.modules[__name__])
ROUTES += workflows.routes(sys.modules[__name__])


class Server(ThreadingHTTPServer):
    # socketserver's default listen backlog is 5: a browser opening ~10 asset connections at once overflowed it and
    # macOS answered the extra SYNs with a reset (ERR_CONNECTION_RESET / ERR_SOCKET_NOT_CONNECTED). curl, one at a time, never did.
    request_queue_size = 256
    daemon_threads = True
    allow_reuse_address = True


SECURITY_HEADERS = {'X-Frame-Options': 'DENY', 'Content-Security-Policy': "frame-ancestors 'none'", 'X-Content-Type-Options': 'nosniff'}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.route('GET')

    def do_POST(self):
        self.route('POST')

    def do_OPTIONS(self):
        if self.headers.get('Origin') != EXT_ORIGIN:
            return self.send(403, b'', 'text/plain')
        self.send(204, b'', 'text/plain', {'Access-Control-Allow-Methods': 'GET, POST',
                                            'Access-Control-Allow-Headers': 'Content-Type, X-FL'})

    def send(self, code, body, ctype='application/json', headers=None):
        self.drain()
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        if self.headers.get('Origin') == EXT_ORIGIN:
            self.send_header('Access-Control-Allow-Origin', EXT_ORIGIN)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def drain(self):
        """Read a request body nobody consumed (early 403/404): closing a socket with unread bytes sends a TCP reset,
        which the browser reports as ERR_CONNECTION_RESET instead of the response."""
        if getattr(self, '_drained', False):
            return
        self._drained = True
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        if 0 < n <= 64 * 1024 * 1024:
            self.rfile.read(n)

    def route(self, method):
        self._drained = False
        url = urlparse(self.path)
        port = CFG['port']
        origin = self.headers.get('Origin')
        if self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}'):
            return self.send(403, {'ok': False, 'error': 'host'})
        if url.path.startswith('/api/ext/'):
            # Extension GETs carry no Origin; a custom header can't be sent cross-site without a (refused) preflight.
            if origin != EXT_ORIGIN and not (origin is None and self.headers.get('X-FL') == '1'):
                return self.send(403, {'ok': False, 'error': 'origin'})
        elif origin and origin not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            return self.send(403, {'ok': False, 'error': 'origin'})
        elif method == 'POST' and not origin:  # browsers always send Origin on POST; a missing one is not our page
            return self.send(403, {'ok': False, 'error': 'origin required'})
        for m, rx, fn in ROUTES:
            match = re.fullmatch(rx, url.path)
            if m == method and match:
                named = set(match.re.groupindex.values())
                args = [g if i in named else int(g) for i, g in enumerate(match.groups(), 1)]
                return self.api(fn, parse_qs(url.query), args, method == 'POST' or '/ext/' in url.path)
        if method == 'GET' and url.path.startswith('/img/'):
            return self.image(url.path[5:])
        if method == 'GET' and url.path == '/api/local-font/areal':
            return self.local_font()
        if method == 'GET' and not url.path.startswith('/api/'):
            return self.static(url.path)
        self.send(404, {'ok': False, 'error': 'not found'})

    def api(self, fn, q, args, with_ok):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n < 0 or n > 64 * 1024 * 1024:
                raise Bad('bad Content-Length')
            self._drained = True
            body = json.loads(self.rfile.read(n) or b'{}') if n else {}
            if not isinstance(body, dict):
                raise Bad('body must be a JSON object')
            conn = db.connect(CFG['db'])
            try:
                out = fn(conn, q, body, *args)
            finally:
                conn.close()
        except Conflict as e:
            return self.send(409, dict(e.current, ok=False, error=str(e), current=e.current))
        except NotFound as e:
            return self.send(404, {'ok': False, 'error': str(e)})
        except (Bad, ValueError, KeyError, TypeError, OverflowError) as e:
            return self.send(400, {'ok': False, 'error': str(e)})
        except Exception:
            traceback.print_exc()
            return self.send(500, {'ok': False, 'error': 'Internal server error'})
        self.send(200, dict(out, ok=True) if with_ok else out)

    def image(self, pid):
        if not re.fullmatch(r'[0-9]+', pid):
            return self.send(404, {'ok': False, 'error': 'no image'})
        f = pfp_dir() / f'{pid}.jpg'
        if not f.is_file():
            return self.send(404, {'ok': False, 'error': 'no image'})
        data = f.read_bytes()
        if not valid_pic(data):
            return self.send(404, {'ok': False, 'error': 'no image'})
        ctype = 'image/png' if data[:4] == b'\x89PNG' else 'image/webp' if data[8:12] == b'WEBP' else 'image/jpeg'
        self.send(200, data, ctype, {'Cache-Control': 'no-cache'})

    def local_font(self):
        # Use the owner's installed font without distributing it in the public repository.
        font = Path.home() / 'Library' / 'Fonts' / 'ABCArealSuperfamilyVariable.ttf'
        if not font.is_file():
            return self.send(404, b'Font not installed', 'text/plain')
        self.send(200, font.read_bytes(), 'font/ttf', {'Cache-Control': 'private, max-age=86400'})

    def static(self, path):
        f = (WEB / (path.lstrip('/') or 'index.html')).resolve()
        if f.is_dir():
            f = f / 'index.html'
        if not f.is_relative_to(WEB.resolve()) or not f.is_file():
            return self.send(404, b'not found', 'text/plain')
        self.send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or 'application/octet-stream',
                  {'Cache-Control': 'no-cache'})


# ---------- background workers ----------

def network_context(conn, pids, me=None):
    """Network signals per person for the prefilter and the LLM packet:
    seeds + direction, lists count, link to Michael (is_me seed), seed yield learned from marks (smoothed good+client / marked),
    and how many of the person's seeds are people Michael marked good/client."""
    pids = list(dict.fromkeys(pids))
    if not pids:
        return {}
    me = me if me is not None else me_handle(conn)
    out = {p: {'seeds': [], 'lists': 0, 'me': None, 'seed_yield': None, 'seed_marked': 0, 'client_seeds': 0, 'followers': None}
           for p in pids}
    for chunk in chunks(pids):
        for r in conn.execute(f"SELECT id, followers FROM people WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            out[r['id']]['followers'] = r['followers']
        for e in conn.execute(f"SELECT person_id, seed, direction FROM current_edges WHERE person_id IN ({','.join('?' * len(chunk))}) "
                              'ORDER BY seed, direction', chunk):
            out[e['person_id']]['seeds'].append((e['seed'], e['direction']))
    # The old aggregation read every observed edge for every list page, even
    # though the page usually touches only a few seeds. Limit the aggregate to
    # the seeds of these people; a marked person outside them cannot affect the
    # result. Chunking also stays within SQLite's variable limit.
    relevant = sorted({seed for n in out.values() for seed, _ in n['seeds']})
    yields = {}
    good_handles = set()
    known_handles = set()
    members_ready = db.get_setting(conn, 'map_seed_member_v1', False) and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_member_%'").fetchone()[0] == 6 and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_seed_degree_%'").fetchone()[0] == 2
    for chunk in chunks(relevant):
        slots = ','.join('?' * len(chunk))
        membership = 'map_seed_member' if members_ready else 'current_edges'
        distinct = '' if members_ready else 'DISTINCT '
        for r in conn.execute(
                f"SELECT e.seed, count({distinct}CASE WHEN m.status IN {POSITIVE_SQL} THEN e.person_id END), "
                f"count({distinct}e.person_id) FROM {membership} e JOIN ({owner.feedback_marks_sql()}) m ON m.person_id=e.person_id "
                f"WHERE m.status IS NOT NULL AND e.seed IN ({slots}) GROUP BY e.seed", chunk):
            yields[r[0]] = (r[1], r[2])
        good_handles.update(r[0] for r in conn.execute(
            f"SELECT p.handle FROM people p JOIN ({owner.feedback_marks_sql()}) m ON m.person_id=p.id "
            f"WHERE m.status IN {POSITIVE_SQL} AND p.handle IN ({slots})", chunk))
        known_handles.update(r[0] for r in conn.execute(
            f"SELECT p.handle FROM people p JOIN owner_context oc ON oc.person_id=p.id "
            f"WHERE oc.relationships!='[]' AND p.handle IN ({slots})", chunk))
    for pid, n in out.items():
        seeds = {s for s, _ in n['seeds']}
        mine = {d for s, d in n['seeds'] if me and s == me}
        others = seeds - ({me} if me else set())
        n['lists'] = len(seeds)
        n['me'] = 'mutual' if len(mine) == 2 else 'follows' if 'followers' in mine else 'followed' if mine else None
        best = None
        for s in others:
            g, m = yields.get(s, (0, 0))
            if m:
                y = (g + 1) / (m + 4)   # prior 1 good in 4: a seed needs several marks before it moves anyone much
                if best is None or y > best[0]:
                    best = (y, m)
        if best:
            n['seed_yield'], n['seed_marked'] = round(best[0], 3), best[1]
        n['client_seeds'] = len(others & good_handles)
        n['known_seeds'] = len((others & known_handles) - good_handles)
    return out


def network_snapshot(conn, pids, me=None):
    """Capture the evidence used by an existing verdict before a graph/mark change."""
    pids = list(dict.fromkeys(pids))
    nets = network_context(conn, pids, me)
    out = {}
    for pid in pids:
        row = conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()
        if row:
            out[pid] = {'person': with_owner(conn, dict(row)), 'edges': edges_of(conn, pid), 'net': nets[pid]}
    return out


def refresh_network(conn, pids, prior=None, me=None, nets=None):
    """Refresh connection claims and reblend already known fit without classifying anyone.

    Content hashes allow queued changes to reblend without a prior snapshot.
    `prior` can validate older graph-dependent verdict hashes before migration.
    Changed content and unknown content fit remain for an enabled qualification pass.
    """
    pids = list(dict.fromkeys(pids))
    if not pids:
        return 0
    me = me if me is not None else me_handle(conn)
    if nets is None or not all(pid in nets for pid in pids):
        nets = network_context(conn, pids, me)
    changed = 0
    for pid in pids:
        row = conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()
        if not row:
            continue
        p = with_owner(conn, dict(row))
        edges = edges_of(conn, pid)
        # Automatic source tags describe observed connections. Manual knowledge
        # is independent evidence and is never removed by a list refresh.
        source = [(tag, grp) for tag, grp in qualify.rule_tags(p, edges, me) if grp == 'source']
        old_source = {(r['tag'], r['grp']) for r in conn.execute(
            "SELECT tag, grp FROM tags WHERE person_id=? AND source='auto' AND grp='source'", (pid,))}
        if old_source != set(source):
            conn.execute("DELETE FROM tags WHERE person_id=? AND source='auto' AND grp='source'", (pid,))
            conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,?,'auto')", [(pid, tag, grp) for tag, grp in source])
            changed += 1
        old = conn.execute('SELECT * FROM verdicts WHERE person_id=?', (pid,)).fetchone()
        before = (prior or {}).get(pid)
        if not old:
            continue
        if old['content_fit'] is None:
            if old['score'] is not None:
                conn.execute("UPDATE verdicts SET score=NULL,tier='unread',prefilter=NULL WHERE person_id=?", (pid,))
                changed += 1
            continue
        hashed = qualify.input_hash(p, edges, nets[pid])
        valid = old['input_hash'] == hashed
        if not valid and before:
            previous = before['person']
            same_content = qualify.input_hash(previous, before['edges'], before['net']) == hashed
            valid = same_content and old['updated_at'] == previous['updated_at'] and (
                old['model'] == 'rules' or old['input_hash'] == qualify.legacy_input_hash(
                    previous, before['edges'], before['net']))
        if not valid:
            continue
        score = owner.owner_recommendation(p, {'score': qualify.blend(old['content_fit'], nets[pid])})['score']
        _, lfit = laya_row(conn, pid)
        pre = qualify.prefilter(p, sorted({e['seed'] for e in edges}), nets[pid], lfit)
        if (score, pre, hashed, p['updated_at']) != (old['score'], old['prefilter'], old['input_hash'], old['updated_at']):
            conn.execute('UPDATE verdicts SET score=?, tier=?, prefilter=?, input_hash=?, updated_at=? WHERE person_id=?',
                         (score, qualify._tier(score, bool((p.get('bio') or '').strip())), pre, hashed, p['updated_at'], pid))
            changed += 1
    return changed


def drain_network_dirty(conn, limit=200):
    """Acknowledge only the exact queued revision that was refreshed."""
    rows = conn.execute('SELECT person_id,change_id FROM network_dirty ORDER BY change_id,person_id LIMIT ?',
                        (limit,)).fetchall()
    if not rows:
        return 0
    refresh_network(conn, [r['person_id'] for r in rows])
    conn.executemany('DELETE FROM network_dirty WHERE person_id=? AND change_id=?',
                     [(r['person_id'], r['change_id']) for r in rows])
    conn.commit()
    return len(rows)


def laya_row(conn, pid):
    r = conn.execute('''SELECT l.answers, l.fit, l.input_hash, p.handle, p.name, p.bio,
                       p.category, p.website, p.followers
                       FROM laya l JOIN people p ON p.id=l.person_id WHERE l.person_id=?''', (pid,)).fetchone()
    if not r:
        return None, None
    fields = (r[k] for k in ('handle', 'name', 'bio', 'category', 'website', 'followers'))
    if r['input_hash'] != laya_hash(*fields):
        return None, None
    try:
        answers = json.loads(r['answers'] or '{}')
        if not laya.valid_answers(answers):
            return None, None
        return answers, laya.fit(answers)
    except (TypeError, ValueError):
        return None, None


def with_owner(conn, p):
    """Adds Michael's own judgement to a person dict: status, note and the tags he set by hand. The qualifier puts these in
    the prompt and in input_hash, so changing any of them re-runs the model for that person."""
    m = conn.execute('SELECT status, note FROM marks WHERE person_id=?', (p['id'],)).fetchone()
    p['status'], p['note'] = (m['status'], m['note']) if m else (None, None)
    p['manual_tags'] = sorted(r[0] for r in conn.execute("SELECT tag FROM tags WHERE person_id=? AND source='manual'", (p['id'],)))
    owner.hydrate(conn, [p])
    return p


def requalify(conn, p, me, net=None):
    # Same rule as background_qualify: rule scoring is free and only Stop all holds it.
    if not db.get_setting(conn, 'local_laya') and all(control.stage_paused(conn, s) for s in ('lists', 'bios', 'ai')):
        return refresh_network(conn, [p['id']], me=me)
    with_owner(conn, p)
    # Older auto tags treated any follow or @mention as a personal acquaintance.
    conn.execute("DELETE FROM tags WHERE person_id=? AND tag='knows you' AND source='auto'", (p['id'],))
    edges = edges_of(conn, p['id'])
    _, lfit = laya_row(conn, p['id'])
    pre = qualify.prefilter(p, sorted({e['seed'] for e in edges}), net, lfit)
    old = conn.execute('SELECT model, input_hash FROM verdicts WHERE person_id=?', (p['id'],)).fetchone()
    scout_current = bool(old and old['model'] == 'leadscout' and deepscout.fresh(conn, p))
    keep_llm = old and old['input_hash'] and old['input_hash'] == qualify.input_hash(p, edges, net) and (
        old['model'] != 'leadscout' or scout_current)
    if not keep_llm:  # the LLM's extra auto tags stay as long as its verdict does
        conn.execute("DELETE FROM tags WHERE person_id=? AND source='auto'", (p['id'],))
    auto = qualify.rule_tags(p, edges, me)
    # Laya probabilities are uncalibrated and have no separate tag provenance.
    # Use the fit as a ranking hint only; rule and LLM tags keep their own evidence.
    conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,?,'auto')", [(p['id'], t, g) for t, g in auto])
    deepscout.retag(conn, p['id'])   # the agent's findings outlive rule passes
    if keep_llm:
        refresh_network(conn, [p['id']], me=me, nets={p['id']: net} if net is not None else None)
        # The verdict is current for this profile revision even when the reblend changed nothing;
        # without this the batch would pick the same person up again on every pass.
        conn.execute('UPDATE verdicts SET updated_at=? WHERE person_id=? AND updated_at<?',
                     (p['updated_at'], p['id'], p['updated_at']))
        return
    tags = [tuple(r) for r in conn.execute('SELECT tag, grp FROM tags WHERE person_id=?', (p['id'],))]
    v = qualify.rule_verdict(p, tags, net)
    conn.execute("INSERT OR REPLACE INTO verdicts(person_id, prefilter, score, tier, role, reason, model, input_hash, updated_at, content_fit) "
                 "VALUES(?,?,?,?,?,?,'rules',?,?,?)", (p['id'], pre, v['score'], v['tier'], v['role'], v['reason'], qualify.input_hash(p, edges, net), p['updated_at'], v['content_fit']))
    if scout_current:
        deepscout.reapply(conn, p, net)


def qualify_batch(conn, limit=32):
    """Keep each rule write transaction short enough for concurrent extension and Laya writers."""
    rows = conn.execute('SELECT p.* FROM people p LEFT JOIN verdicts v ON v.person_id=p.id '
                        "WHERE v.person_id IS NULL OR (coalesce(v.model,'')!='error' AND v.updated_at < p.updated_at) "
                        "OR (v.model='error' AND (coalesce(v.input_hash,'')!=p.updated_at "
                        "OR (v.reason LIKE 'retry %' AND v.updated_at<=?))) LIMIT ?",
                        (db.now(), limit)).fetchall()
    me = me_handle(conn)
    # Reserve the writer before rule reads and per-person savepoints. A scraper
    # commit between a savepoint's first read and its first write otherwise
    # makes SQLite reject the upgrade with SQLITE_BUSY_SNAPSHOT.
    if rows and not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    # One bulk rule sync is much cheaper than querying rules and tags for every
    # person. If it fails, roll back the whole sync and isolate the bad person
    # below so the rest of the batch can still make progress.
    rule_sync_failed = False
    if rows:
        conn.execute('SAVEPOINT qualify_rules')
        try:
            rules.sync(conn, [r['id'] for r in rows])
            conn.execute('RELEASE SAVEPOINT qualify_rules')
        except Exception:
            traceback.print_exc()
            conn.execute('ROLLBACK TO SAVEPOINT qualify_rules')
            conn.execute('RELEASE SAVEPOINT qualify_rules')
            rule_sync_failed = True
    nets = network_context(conn, [r['id'] for r in rows], me) if rows else {}
    for r in rows:
        conn.execute('SAVEPOINT qualify_person')
        try:
            if rule_sync_failed:
                rules.sync(conn, [r['id']])
            requalify(conn, dict(r), me, nets.get(r['id']))
            conn.execute('RELEASE SAVEPOINT qualify_person')
        except Exception:
            traceback.print_exc()
            conn.execute('ROLLBACK TO SAVEPOINT qualify_person')
            conn.execute('RELEASE SAVEPOINT qualify_person')
            previous = conn.execute('SELECT model,input_hash,reason FROM verdicts WHERE person_id=?', (r['id'],)).fetchone()
            attempt = 1
            if previous and previous['model'] == 'error' and previous['input_hash'] == r['updated_at']:
                match = re.fullmatch(r'(?:retry|failed) (\d+)/\d+', previous['reason'] or '')
                if match:
                    attempt = int(match[1]) + 1
            final = attempt >= QUALIFY_MAX_ATTEMPTS
            delay = min(10 * 3 ** (attempt - 1), 900)
            next_at = r['updated_at'] if final else iso(datetime.now(timezone.utc) + timedelta(seconds=delay))
            conn.execute("INSERT OR REPLACE INTO verdicts(person_id,tier,model,input_hash,reason,updated_at) "
                         "VALUES(?,'unread','error',?,?,?)",
                         (r['id'], r['updated_at'], f"{'failed' if final else 'retry'} {attempt}/{QUALIFY_MAX_ATTEMPTS}", next_at))
    conn.commit()
    return len(rows)


# ---------- Laya (optional soft signal) ----------

LAYA_BATCH = 64
LAYA_REBUILD_BATCH = 5000


def laya_hash(*fields):
    """Invalidate when any field supplied to Laya changes."""
    payload = [laya.cache_signature(), fields]
    return 'profile:' + __import__('hashlib').sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()[:24]


def rebuild_laya_queue(conn, signature):
    """Reconcile one ID range and publish the queue only after the final range.

    Profile and Laya triggers maintain ranges already visited. A durable cursor
    lets a restart resume without holding the SQLite write lock for a full scan.
    """
    conn.execute('BEGIN IMMEDIATE')
    try:
        if db.get_setting(conn, 'laya_queue_signature') == signature:
            conn.commit()
            return True
        state = db.get_setting(conn, 'laya_queue_rebuild')
        cursor = state['cursor'] if isinstance(state, dict) and state.get('signature') == signature else None
        if cursor is None:
            ids = [r[0] for r in conn.execute('SELECT id FROM people ORDER BY id LIMIT ?', (LAYA_REBUILD_BATCH,))]
        else:
            ids = [r[0] for r in conn.execute('SELECT id FROM people WHERE id>? ORDER BY id LIMIT ?',
                                               (cursor, LAYA_REBUILD_BATCH))]
        if ids:
            end = ids[-1]
            bound = ('person_id<=?' if cursor is None else 'person_id>? AND person_id<=?')
            args = (end,) if cursor is None else (cursor, end)
            conn.execute('DELETE FROM laya_queue WHERE ' + bound, args)
            person_bound = ('p.id<=?' if cursor is None else 'p.id>? AND p.id<=?')
            conn.execute(f"""INSERT INTO laya_queue(person_id,bio_blank,prefilter)
                SELECT p.id,coalesce(p.bio,'')='',v.prefilter
                FROM people p LEFT JOIN laya l ON l.person_id=p.id
                LEFT JOIN verdicts v ON v.person_id=p.id
                WHERE {person_bound} AND instr(p.handle,'~')=0 AND p.handle!='fortun8te' COLLATE NOCASE AND NOT EXISTS
                  (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle)
                  AND (l.person_id IS NULL OR l.input_hash IS NOT
                    laya_hash(p.handle,p.name,p.bio,p.category,p.website,p.followers))""", args)
            cursor = end
        if len(ids) < LAYA_REBUILD_BATCH:
            db.set_setting(conn, 'laya_queue_signature', signature)
            conn.execute("DELETE FROM settings WHERE key='laya_queue_rebuild'")
            complete = True
        else:
            db.set_setting(conn, 'laya_queue_rebuild', {'signature': signature, 'cursor': cursor})
            complete = False
        conn.commit()
        return complete
    except Exception:
        conn.rollback()
        raise


def laya_allowed(conn):
    return bool(db.get_setting(conn, 'local_laya')) or not control.stage_paused(conn, 'ai')


def laya_step(conn):
    """Score people without a (current) Laya answer; bios first, list-only people too. Silently idle when the sidecar is down."""
    if not laya_allowed(conn) or not laya.available():
        return False
    caller_transaction = conn.in_transaction
    conn.create_function('laya_hash', 6, laya_hash, deterministic=True)
    signature = laya.cache_signature()
    if not caller_transaction and db.get_setting(conn, 'laya_queue_signature') != signature:
        if not rebuild_laya_queue(conn, signature):
            return True  # next worker pass continues the bounded rebuild
    if caller_transaction:
        # A caller's uncommitted profile edits must be visible, but its transaction
        # must not be committed or held behind a queue rebuild during a model call.
        rows = conn.execute(f"""SELECT p.id,p.handle,p.name,p.bio,p.category,p.website,p.followers,l.input_hash AS lh
            FROM people p LEFT JOIN laya l ON l.person_id=p.id LEFT JOIN verdicts v ON v.person_id=p.id
            WHERE instr(p.handle,'~')=0 AND {NOT_ME}
              AND (l.person_id IS NULL OR l.input_hash IS NOT
                laya_hash(p.handle,p.name,p.bio,p.category,p.website,p.followers))
            ORDER BY coalesce(p.bio,'')='',v.prefilter DESC,p.id LIMIT ?""", (LAYA_BATCH,)).fetchall()
    else:
        rows = conn.execute(f"""SELECT p.id,p.handle,p.name,p.bio,p.category,p.website,p.followers,l.input_hash AS lh
            FROM laya_queue q JOIN people p ON p.id=q.person_id
            LEFT JOIN laya l ON l.person_id=p.id
            WHERE {NOT_ME}
            ORDER BY q.bio_blank,q.prefilter DESC,q.person_id LIMIT ?""", (LAYA_BATCH,)).fetchall()
    # A profile can change back to an already cached hash. Drop that one queue
    # entry rather than sending the same profile to the sidecar again.
    current = [r['id'] for r in rows if r['lh'] == laya_hash(*(r[k] for k in
               ('handle','name','bio','category','website','followers')))]
    if current and not caller_transaction:
        conn.executemany('DELETE FROM laya_queue WHERE person_id=?', [(pid,) for pid in current])
        conn.commit()
        current_ids = set(current)
        rows = [r for r in rows if r['id'] not in current_ids]
    if not rows:
        return False
    if not laya_allowed(conn):
        return False
    answers = laya.decide([dict(r) for r in rows])   # no DB lock is held during the call
    if not answers:
        return False
    if not laya_allowed(conn):
        return False
    if set(answers) != {r['id'] for r in rows} or not all(laya.valid_answers(a) for a in answers.values()):
        return False
    ts = db.now()
    done = []
    for r in rows:
        current = conn.execute('SELECT handle,name,bio,category,website,followers FROM people WHERE id=?', (r['id'],)).fetchone()
        if current and laya_hash(*current) == laya_hash(*(r[k] for k in ('handle','name','bio','category','website','followers'))):
            done.append(r)
    if not done:
        return False
    conn.executemany('INSERT OR REPLACE INTO laya VALUES(?,?,?,?,?)',
                     [(r['id'], laya_hash(*(r[k] for k in ('handle','name','bio','category','website','followers'))), json.dumps(answers[r['id']]),
                       laya.fit(answers[r['id']]), ts) for r in done])
    conn.executemany('DELETE FROM laya_queue WHERE person_id=?', [(r['id'],) for r in done])
    # The qualify batch folds the new ranking signal into prefilter on its next pass.
    conn.executemany("UPDATE verdicts SET updated_at='' WHERE person_id=?", [(r['id'],) for r in done])
    if not caller_transaction:
        conn.commit()
    return True


# ---------- LLM stage: bounded worker pool, few-shot from marks ----------

FEWSHOT_MAX = 8
FEWSHOT_CHANGE = 5    # re-run LLM verdicts when the good/client/no marks moved by this many (or 20 %)
FEWSHOT_RERUN = 35    # ... but only those at or near warm (45): a new example set will not lift a clear cold one
FEWSHOT_TAG_MAX = getattr(qualify, 'FEWSHOT_TAG_MAX', 6)


def feedback_example(conn, pid):
    """A small, identity-stable example from Michael's mark and manual tags only."""
    r = conn.execute(f"""SELECT p.id,p.handle,p.name,p.bio,m.status AS status FROM people p
        LEFT JOIN ({owner.feedback_marks_sql()}) m ON m.person_id=p.id WHERE p.id=?
        AND (m.status IN ('interested','talking','client','no') OR
             (m.status IS NOT 'no' AND EXISTS(SELECT 1 FROM tags t WHERE t.person_id=p.id
                AND t.source='manual' AND t.tag='client' COLLATE NOCASE)))
        AND coalesce(p.bio,'')!='' AND instr(p.handle,'~')=0 AND p.handle!='fortun8te' COLLATE NOCASE
        AND NOT EXISTS (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle)""", (pid,)).fetchone()
    if not r:
        return None
    tags = [re.sub(r'\s+', ' ', t[0]).strip()[:64] for t in conn.execute(
        "SELECT tag FROM tags WHERE person_id=? AND source='manual' ORDER BY tag LIMIT ?",
        (pid, FEWSHOT_TAG_MAX))]
    return {'person_id': r['id'], 'handle': r['handle'], 'name': (r['name'] or '')[:80],
            'bio': r['bio'][:200], 'label': 'no' if r['status'] == 'no' else 'good',
            'status': r['status'], 'feedback_source': 'status_mark' if r['status'] in (*POSITIVE, 'no') else 'manual_client_tag',
            'manual_tags': [t for t in tags if t and not (r['status'] == 'no' and t.casefold() == 'client')]}


def fewshot(conn):
    """Use bounded owner examples for future calls; batch broad re-runs after several new marks."""
    n = conn.execute(f"SELECT count(*) FROM ({owner.feedback_marks_sql()}) m WHERE m.status IN ('interested','talking','client','no')").fetchone()[0]
    cur = db.get_setting(conn, 'fewshot') or {}
    prior = cur.get('examples') or []
    selected_n = cur.get('n', 0)
    count_due = bool(cur) and abs(n - selected_n) >= max(FEWSHOT_CHANGE, selected_n // 5)
    old_format = any('person_id' not in e or 'feedback_source' not in e for e in prior)
    latest_client = conn.execute(f"""SELECT p.id FROM ({owner.feedback_marks_sql()}) m JOIN people p ON p.id=m.person_id
        WHERE m.status='client' AND coalesce(p.bio,'')!='' AND instr(p.handle,'~')=0
          AND p.handle!='fortun8te' COLLATE NOCASE
          AND NOT EXISTS (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle)
        ORDER BY m.updated_at DESC,p.id DESC LIMIT 1""").fetchone()
    tagged_client = conn.execute(f"""SELECT p.id FROM tags t JOIN people p ON p.id=t.person_id
        LEFT JOIN ({owner.feedback_marks_sql()}) m ON m.person_id=p.id
        WHERE t.source='manual' AND t.tag='client' COLLATE NOCASE AND m.status IS NOT 'no'
          AND coalesce(p.bio,'')!='' AND instr(p.handle,'~')=0 AND p.handle!='fortun8te' COLLATE NOCASE
          AND NOT EXISTS (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle)
        ORDER BY CASE WHEN m.status IN ('interested','talking','client') THEN 1 ELSE 0 END,
                 p.updated_at DESC,p.id DESC LIMIT 1""").fetchone()
    prior_ids = {e.get('person_id') for e in prior}
    new_client = any(row and row[0] not in prior_ids for row in (latest_client, tagged_client))
    rebuild = not cur or count_due or old_format or new_client
    ex = []
    if not rebuild:
        ex = [e for e in (feedback_example(conn, p['person_id']) for p in prior) if e]
        if len(ex) != len(prior) or any(a['label'] != b['label'] for a, b in zip(ex, prior)):
            rebuild = True
    if rebuild:
        ex = []
        marked_ids = [r[0] for r in conn.execute(f"""SELECT p.id FROM ({owner.feedback_marks_sql()}) m JOIN people p ON p.id=m.person_id
                WHERE m.status IN {POSITIVE_SQL} AND coalesce(p.bio,'')!='' AND instr(p.handle,'~')=0
                  AND p.handle!='fortun8te' COLLATE NOCASE
                  AND NOT EXISTS (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle)
                ORDER BY CASE m.status WHEN 'client' THEN 0 WHEN 'talking' THEN 1 ELSE 2 END,
                         m.updated_at DESC,p.id DESC LIMIT ?""", (FEWSHOT_MAX,))]
        tagged_ids = [r[0] for r in conn.execute(f"""SELECT p.id FROM tags t JOIN people p ON p.id=t.person_id
            LEFT JOIN ({owner.feedback_marks_sql()}) m ON m.person_id=p.id
            WHERE t.source='manual' AND t.tag='client' COLLATE NOCASE AND m.status IS NOT 'no'
              AND coalesce(p.bio,'')!='' AND instr(p.handle,'~')=0 AND p.handle!='fortun8te' COLLATE NOCASE
              AND NOT EXISTS (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle)
            ORDER BY CASE WHEN m.status IN ('interested','talking','client') THEN 1 ELSE 0 END,
                     p.updated_at DESC,p.id DESC LIMIT ?""", (FEWSHOT_MAX,))]
        good_ids = marked_ids[:FEWSHOT_MAX]
        extra_tagged = [pid for pid in tagged_ids if pid not in good_ids]
        if extra_tagged and len(good_ids) == FEWSHOT_MAX:
            good_ids.pop()  # one slot for Michael's manual Client tag, even when marks fill the cap
        good_ids.extend(extra_tagged[:FEWSHOT_MAX - len(good_ids)])
        ex.extend(e for e in (feedback_example(conn, pid) for pid in good_ids) if e)
        no_ids = [r[0] for r in conn.execute(f"""SELECT p.id FROM ({owner.feedback_marks_sql()}) m JOIN people p ON p.id=m.person_id
            WHERE m.status='no' AND coalesce(p.bio,'')!='' AND instr(p.handle,'~')=0
              AND p.handle!='fortun8te' COLLATE NOCASE
              AND NOT EXISTS (SELECT 1 FROM seeds WHERE is_me=1 AND handle=p.handle)
            ORDER BY m.updated_at DESC,p.id DESC LIMIT ?""", (FEWSHOT_MAX,))]
        ex.extend(e for e in (feedback_example(conn, pid) for pid in no_ids) if e)
    version = qualify.prompt_version(ex) if hasattr(qualify, 'prompt_version') else None
    if ex == prior and version == cur.get('version') and not count_due:
        return ex
    # A rubric/schema revision still invalidates warm verdicts. Owner note/tag
    # edits update future prompts without turning every old verdict into a job.
    base_prompt_changed = bool(version and cur.get('version') and
                               version.rsplit(':', 1)[0] != cur['version'].rsplit(':', 1)[0])
    if cur and (count_due or base_prompt_changed) and version and version != cur.get('version'):
        conn.execute("UPDATE verdicts SET model='rules' WHERE model NOT IN ('rules','error','leadscout') AND prompt IS NOT ? AND coalesce(score,0)>=?",
                     (version, FEWSHOT_RERUN))
    db.set_setting(conn, 'fewshot', {'n': n if rebuild else selected_n, 'examples': ex, 'version': version})
    conn.commit()
    return ex


def llm_candidates(conn, limit, exclude):
    """Top candidates by combined signal (prefilter = list data + network + Laya; score = rules on the bio)."""
    held = list(exclude)[:900]
    return conn.execute(f"SELECT p.* FROM people p JOIN verdicts v ON v.person_id=p.id WHERE {NOT_ME} AND coalesce(p.bio,'')!='' "
                        "AND v.model='rules' AND v.updated_at=p.updated_at AND (coalesce(v.prefilter,0)+coalesce(v.score,0))/2>=? "
                        f"AND p.id NOT IN ({','.join('?' * len(held))}) "
                        "ORDER BY coalesce(v.prefilter,0)+coalesce(v.score,0) DESC, p.id LIMIT ?",
                        (db.get_setting(conn, 'llm_min'), *held, limit)).fetchall()


# All model workers share the same small research pool. A pool per model batch
# queued 32+ lookups behind six search slots and spent their deadlines waiting.
RESEARCH_POOL = ThreadPoolExecutor(max_workers=websearch.PARALLEL, thread_name_prefix='research')


def safe_research_lookup(person):
    try:
        return websearch.lookup(person)
    except Exception as exc:
        print(f'research failed for person {person.get("id")}: {exc}', file=sys.stderr)
        return {'results': [], 'site': ''}


def research(conn, items):
    """Attach web research (SearXNG + their website) to each item's person before the model call.
    Cached per person; missing lookups run in parallel with no DB transaction open. Search down -> no research."""
    if not items or not websearch.available():
        return
    websearch.ensure(conn)
    found, todo = {}, []
    for it in items:
        hit = websearch.cached(conn, it['person'])
        if hit is None:
            todo.append(it['person'])
        else:
            found[it['person']['id']] = hit
    conn.commit()
    if todo:
        for p, got in zip(todo, RESEARCH_POOL.map(safe_research_lookup, todo)):
            found[p['id']] = got
        for p in todo:
            websearch.store(conn, p, found[p['id']])
        conn.commit()
    for it in items:
        got = found.get(it['person']['id'])
        # A copy: the input hash and the stored profile never see the research fields.
        it['person'] = dict(it['person'], web_lines=websearch.lines(got), web_text=websearch.text(got),
                            web_site=(got or {}).get('site') or '')


def run_llm(conn, rows, skip):
    """One model round for these people (no DB transaction is open during the call). -> number of verdicts written."""
    if control.stage_paused(conn, 'ai'):
        return 0
    rows = [with_owner(conn, dict(r)) for r in rows]
    me = me_handle(conn)
    nets = network_context(conn, [p['id'] for p in rows], me)
    items = []
    for p in rows:
        edges = edges_of(conn, p['id'])
        fresh_auto = qualify.rule_tags(p, edges, me)
        retained = [tuple(r) for r in conn.execute("SELECT tag, grp FROM tags WHERE person_id=? AND source!='auto'", (p['id'],))]
        items.append({'person': p, 'edges': edges, 'net': nets.get(p['id']),
                      'tags': retained + fresh_auto, 'fresh_auto': fresh_auto})
    examples = fewshot(conn)
    conn.commit()
    if control.stage_paused(conn, 'ai'):
        return 0
    research(conn, items)
    if control.stage_paused(conn, 'ai'):
        return 0
    try:
        many = getattr(qualify, 'llm_verdicts', None)
        vs = many(items, examples) if many else [qualify.llm_verdict(i['person'], i['tags'], i['edges']) for i in items]
    except Exception:  # never let one bad reply spin the worker on the same rows: fall back like 'no model'
        traceback.print_exc()
        vs = [None] * len(items)
    if control.stage_paused(conn, 'ai'):
        return 0
    wrote = 0
    for it, v in zip(items, vs):
        p = it['person']
        if v is None:
            skip[p['id']] = datetime.now().timestamp() + 1800
            continue
        latest = conn.execute('SELECT * FROM people WHERE id=?', (p['id'],)).fetchone()
        if latest is None or latest['updated_at'] != p['updated_at']:
            continue
        latest_p = with_owner(conn, dict(latest))
        latest_net = network_context(conn, [p['id']], me).get(p['id'])
        if qualify.input_hash(latest_p, edges_of(conn, p['id']), latest_net) != qualify.input_hash(p, it['edges'], it['net']):
            continue
        if conn.execute('UPDATE verdicts SET score=?, tier=?, role=?, reason=?, model=?, input_hash=?, prompt=?, evidence=?, content_fit=? '
                        "WHERE person_id=? AND updated_at=? AND model!='leadscout'",
                        (v['score'], v['tier'], v['role'], v['reason'], v.get('model') or 'llm', qualify.input_hash(p, it['edges'], it['net']),
                         v.get('prompt'), json.dumps(v.get('evidence') or []),
                         v.get('content_fit', min(getattr(qualify, 'ROLE_CAP', {}).get(v['role'], 100), v['fit']) if v.get('fit') is not None else None),
                         p['id'], p['updated_at'])).rowcount:
            conn.execute('INSERT INTO ai_scoring_events(person_id,scored_at) VALUES(?,?)',
                         (p['id'], db.now()))
            conn.execute("DELETE FROM tags WHERE person_id=? AND source='auto'", (p['id'],))
            conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,?,'auto')",
                             [(p['id'], t, g) for t, g in it['fresh_auto'] + (v.get('tags') or [])])
            deepscout.retag(conn, p['id'])
            wrote += 1
    if wrote:
        db.set_setting(conn, 'llm_rev', (db.get_setting(conn, 'llm_rev') or 0) + 1)
    conn.commit()
    return wrote


def _expire(skip):
    t = datetime.now().timestamp()
    for k in [k for k, until in list(skip.items()) if until <= t]:
        skip.pop(k, None)


def llm_step(conn, skip):
    """Synchronous single step (tests, tools): one person. The server itself runs LLMPool."""
    if not db.get_setting(conn, 'qualify'):
        return None
    _expire(skip)
    rows = llm_candidates(conn, 1, skip)
    if not rows:
        return None
    return run_llm(conn, rows, skip) > 0


class LLMPool:
    """Bounded pool: at most `llm_workers` (setting, default 4) model calls in flight, each on its own DB connection.
    The dispatcher only reads; results are written by the worker that got them. HTTP handlers and ingest never wait on it."""

    def __init__(self, batch=None):
        self.lock = threading.Lock()
        self.inflight = set()
        self.running = 0
        self.skip = {}
        self.batch = batch

    def step(self, conn):
        if not db.get_setting(conn, 'qualify'):
            return False
        workers = max(1, min(32, int(db.get_setting(conn, 'llm_workers') or 4)))
        batch = self.batch or getattr(qualify, 'LLM_BATCH', 1)
        with self.lock:
            _expire(self.skip)
            free = workers - self.running
            exclude = set(self.inflight) | set(self.skip)
        if free <= 0:
            return False
        rows = llm_candidates(conn, free * batch, exclude)
        if not rows:
            return False
        groups = [rows[i:i + batch] for i in range(0, len(rows), batch)][:free]
        with self.lock:
            for g in groups:
                self.running += 1
                self.inflight.update(r['id'] for r in g)
        for g in groups:
            threading.Thread(target=self._work, args=(g,), daemon=True).start()
        return True

    def _work(self, rows):
        conn = db.connect(CFG['db'])
        try:
            run_llm(conn, rows, self.skip)
        except Exception:
            traceback.print_exc()
        finally:
            conn.close()
            with self.lock:
                self.running -= 1
                self.inflight.difference_update(r['id'] for r in rows)

    def idle(self):
        with self.lock:
            return self.running == 0


def auto_qualify(conn):
    """Switch AI on only after collection is complete or cannot be continued automatically."""
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


def retag_if_changed(conn):
    """When the rule taxonomy changes (qualify.TAGS_VERSION), let the qualify batch re-derive everyone's auto tags.
    LLM verdicts survive: requalify keeps them while the input hash is unchanged."""
    # "knows you" is an owner-only claim now; retire the old automatic copies even when the version is current.
    if conn.execute("DELETE FROM tags WHERE tag='knows you' AND source='auto'").rowcount:
        conn.commit()
    version = getattr(qualify, 'TAGS_VERSION', None)
    if version is None or db.get_setting(conn, 'tags_version') == version:
        return False
    conn.execute("UPDATE verdicts SET updated_at=''")
    db.set_setting(conn, 'tags_version', version)
    conn.commit()
    return True


def refresh_laya_prefilter_if_changed(conn):
    """Queue only Laya-scored verdicts after a prefilter policy change.

    The version write and invalidation share one transaction, so an interrupted
    startup can safely retry. The normal qualify worker reblends in batches.
    """
    version = qualify.PREFILTER_VERSION
    if db.get_setting(conn, 'laya_prefilter_version') == version:
        return 0
    changed = conn.execute("UPDATE verdicts SET updated_at='' WHERE person_id IN "
                           "(SELECT person_id FROM laya)").rowcount
    db.set_setting(conn, 'laya_prefilter_version', version)
    conn.commit()
    return changed


EARLY_LISTS = 2   # while lists are still collecting, only people already in this many lists get a bio read


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
        degree_join, degree = '', LISTS
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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None   # urllib then raises HTTPError(3xx): nothing is fetched from the redirect target


PIC_OPENER = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=SSL))


def valid_pic(data):
    if not data or len(data) > PIC_MAX:
        return False
    if data[:3] == b'\xff\xd8\xff':
        return data.endswith(b'\xff\xd9')
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return len(data) >= 20 and data[-8:-4] == b'IEND'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return len(data) >= 20 and int.from_bytes(data[4:8], 'little') == len(data) - 8
    return False


def valid_pic_file(path):
    try:
        size = path.stat().st_size
        if size < 12 or size > PIC_MAX:
            return False
        with path.open('rb') as f:
            head = f.read(12)
            f.seek(-12, os.SEEK_END)
            tail = f.read(12)
    except OSError:
        return False
    return ((head[:3] == b'\xff\xd8\xff' and tail[-2:] == b'\xff\xd9')
            or (head[:8] == b'\x89PNG\r\n\x1a\n' and tail[4:8] == b'IEND')
            or (head[:4] == b'RIFF' and head[8:12] == b'WEBP'
                and int.from_bytes(head[4:8], 'little') == size - 8))


def fetch_pic(url):
    if not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ''
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != 'https' or not host.endswith(PIC_HOSTS) or parsed.username or parsed.password or port not in (None, 443):
        return None
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with PIC_OPENER.open(req, timeout=10) as r:
            if not (urlparse(r.geturl()).hostname or '').endswith(PIC_HOSTS):
                return None
            data = r.read(PIC_MAX + 1)
    except (OSError, ValueError):
        return None
    return data if valid_pic(data) else None


_pfp_check_id = 0


def repair_pfp_cache(conn, limit=32):
    """Reconcile a bounded local slice, including orphaned photos, without HTTP.

    The returned change count and cursor allow an explicit one-pass recovery.
    This also runs during collection pauses and network holds.
    """
    global _pfp_check_id
    rows = conn.execute('SELECT id, pic_file FROM people WHERE id > ? ORDER BY id LIMIT ?',
                        (_pfp_check_id, max(1, min(int(limit), 1000)))).fetchall()
    if not rows:
        _pfp_check_id = 0
        return 0
    repaired = 0
    directory = pfp_dir()
    for row in rows:
        _pfp_check_id = row['id']
        filename = f"{row['id']}.jpg"
        valid = valid_pic_file(directory / filename)
        if valid and row['pic_file'] != filename:
            # Reuse existing bytes; do not create thousands of network refreshes.
            repaired += conn.execute('UPDATE people SET pic_file=? WHERE id=? AND pic_file IS ?',
                                     (filename, row['id'], row['pic_file'])).rowcount
        elif not valid and row['pic_file']:
            repaired += conn.execute('UPDATE people SET pic_file=NULL, pic_refresh=0 '
                                     'WHERE id=? AND pic_file=?',
                                     (row['id'], row['pic_file'])).rowcount
    if repaired:
        conn.commit()
    return repaired


def pfp_step(conn):
    repaired = repair_pfp_cache(conn)
    if meta_network.blocked(conn):
        return bool(repaired)
    r = conn.execute('SELECT p.id, p.pic_url, p.pic_file FROM people p '
                     'WHERE p.pic_url IS NOT NULL AND (p.pic_file IS NULL OR p.pic_refresh=1) '
                     'ORDER BY (p.pic_file IS NULL) DESC, p.updated_at DESC LIMIT 1').fetchone()
    if not r:
        return bool(repaired)
    if meta_network.blocked(conn):
        return bool(repaired)
    data = fetch_pic(r['pic_url'])
    directory = pfp_dir()
    if data:
        directory.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix=f".{r['id']}.", suffix='.tmp', delete=False) as tmp:
                name = tmp.name
                tmp.write(data)
            os.replace(name, directory / f"{r['id']}.jpg")
        finally:
            if name and os.path.exists(name):
                os.unlink(name)
    # A failed refresh must not hide an existing valid photo. Compare the URL
    # before clearing refresh state so an in-flight URL update is not lost.
    filename = f"{r['id']}.jpg" if data or valid_pic_file(directory / f"{r['id']}.jpg") else ''
    updated = conn.execute('UPDATE people SET pic_file=?, pic_refresh=0 WHERE id=? AND pic_url=?',
                           (filename, r['id'], r['pic_url'])).rowcount
    if not updated:
        conn.execute('UPDATE people SET pic_refresh=1 WHERE id=? AND pic_url IS NOT NULL', (r['id'],))
    conn.commit()
    return True


def worker(stop, step, busy_wait, idle_wait):
    conn = None
    try:
        while not stop.is_set():
            try:
                if conn is None:
                    conn = db.connect(CFG['db'])
                busy = step(conn)
            except Exception:
                traceback.print_exc()
                busy = False
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        traceback.print_exc()
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = None
            stop.wait(busy_wait if busy else idle_wait)
    finally:
        if conn is not None:
            conn.close()


POOL = [None]


def repair_step(conn):
    """Every 15 min: lists that stopped short or lost their job get queued again (db.repair_lists)."""
    out = db.repair_lists(conn)
    conn.commit()
    if any(out.values()):
        print('repair_lists', out, flush=True)
    return False


def models_step(conn):
    """Once a day: pick up OpenRouter's free and stealth models (llm.refresh_models; stealth ones go first)."""
    if not db.get_setting(conn, 'qualify'):
        return False
    if not os.environ.get('FL_NO_ORSLOT'):   # tests stay offline
        llm.refresh_models()
        # hourly: probe providers that are not known-good, so Settings shows ok / spent / broken instead of 'untested'
        pool = llm.get()
        for p in pool.status()['providers']:
            if p['state'] not in ('ok', 'spent', 'broken'):
                pool.test(p['id'])
    return False


def background_qualify(conn):
    refreshed = drain_network_dirty(conn)
    # Local processing stays independent of Instagram collection and external model calls.
    if not db.get_setting(conn, 'local_laya') and all(control.stage_paused(conn, s) for s in ('lists', 'bios', 'ai')):
        return bool(refreshed)
    return qualify_batch(conn) or bool(refreshed)


def start_workers(stop):
    pool = POOL[0] = LLMPool()
    scouts = deepscout.ScoutPool(CFG['db'])
    loops = [(repair_step, 900, 900), (models_step, 3600, 3600), (background_qualify, 0, 5), (pool.step, 1, 5), (laya_step, 0.2, 30), (owner_notes.step, 1, 30), (plan_profiles, 15, 15), (pfp_step, 0.4, 10), (biofetch.step, 0.5, 10), (scouts.step, 2, 10)]
    for args in loops:
        threading.Thread(target=worker, args=(stop, *args), daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=CFG['db'])
    ap.add_argument('--port', type=int, default=CFG['port'])
    a = ap.parse_args()
    CFG.update(db=str(Path(a.db).resolve()), port=a.port)
    Path(CFG['db']).parent.mkdir(parents=True, exist_ok=True)
    conn = db.init(CFG['db'])
    deepscout.ensure(conn)
    owner_notes.ensure(conn)
    conn.commit()
    retag_if_changed(conn)
    refresh_laya_prefilter_if_changed(conn)
    conn.close()
    start_workers(threading.Event())
    print(f'Fortunate Leads on http://127.0.0.1:{a.port}  db={CFG["db"]}', flush=True)
    Server(('127.0.0.1', a.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
