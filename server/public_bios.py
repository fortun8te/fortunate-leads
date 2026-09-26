"""Optional logged-out profile collector using the workspace's durable profile queue."""
import hashlib
import os
from pathlib import Path
import subprocess
import time
import uuid
from datetime import timedelta

import control
import db
import rules

ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_ERRORS = {'rate_limited', 'rate_limit', 'login_required', 'no_proxies_left', 'upstream_error', 'network_error', 'timeout', 'transport_error'}

PERMANENT_ERRORS = {'not_found', 'invalid_input'}

DEFAULTS = {'on': False, 'concurrency': 16, 'scope': 'queued', 'egress_file': 'data/scraper-egresses.yaml'}
_process = None
_restart_after = 0


def init(conn):
    conn.executescript('''CREATE TABLE IF NOT EXISTS public_bio_results(
      id INTEGER PRIMARY KEY, job_id INTEGER, handle TEXT, at TEXT, outcome TEXT,
      cache_hit INTEGER, source TEXT, observed_at TEXT, error TEXT);
      CREATE INDEX IF NOT EXISTS public_bio_results_at ON public_bio_results(at);
      CREATE TABLE IF NOT EXISTS public_bio_retries(job_id INTEGER PRIMARY KEY, next_at TEXT, failures INTEGER DEFAULT 0);''')
    conn.commit()


def settings(conn):
    return {**DEFAULTS, **(db.get_setting(conn, 'public_bios') or {})}


def config_path(cfg, root=ROOT):
    path = (Path(root) / cfg['egress_file']).resolve()
    if not path.is_relative_to((Path(root) / 'data').resolve()):
        raise ValueError('egress_file must be inside data/')
    return path


def configure(conn, body):
    cfg = settings(conn)
    if 'on' in body:
        if not isinstance(body['on'], bool):
            raise ValueError('on must be true or false')
        cfg['on'] = body['on']
    if 'concurrency' in body:
        value = body['concurrency']
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 32:
            raise ValueError('concurrency must be an integer from 1 to 32')
        cfg['concurrency'] = value
    if 'scope' in body:
        if body['scope'] not in ('queued', 'missing'):
            raise ValueError('scope must be queued or missing')
        cfg['scope'] = body['scope']
    if 'egress_file' in body:
        cfg['egress_file'] = str(body['egress_file'])
    config_path(cfg)
    db.set_setting(conn, 'public_bios', cfg)
    conn.commit()
    return status(conn)


def enabled(conn):
    return settings(conn)['on'] and not control.stage_paused(conn, 'bios')


def plan(conn, capacity=256):
    """Top up a bounded runnable queue, independent of scoring/budget. Explicit jobs always retain priority.

    Automatic discovery skips people marked no. Seeds are always included. Historical error jobs
    are not recreated endlessly; an explicit retry can put those back into the queue.
    """
    conn.execute('BEGIN IMMEDIATE')
    if not enabled(conn) or settings(conn)['scope'] != 'missing':
        conn.commit()
        return 0
    # Deferred profiles remain durable, but cannot occupy all runnable slots and
    # prevent untouched profiles from making progress during their cooldown.
    active = conn.execute("""SELECT count(*) FROM jobs j
      LEFT JOIN public_bio_retries r ON r.job_id=j.id
      WHERE j.kind='profile' AND (j.state='leased' OR
        (j.state='queued' AND (r.next_at IS NULL OR r.next_at<=?)))""", (db.now(),)).fetchone()[0]
    room = max(0, capacity - active)
    candidates = conn.execute("""SELECT handle FROM (
      SELECT s.handle, 0 AS source_order FROM seeds s LEFT JOIN people p ON p.handle=s.handle
      WHERE p.bio_at IS NULL AND instr(s.handle,'~')=0
      AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind='profile' AND j.handle=s.handle)
      UNION ALL SELECT p.handle, 1 AS source_order FROM people p LEFT JOIN marks m ON m.person_id=p.id
      WHERE p.bio_at IS NULL AND instr(p.handle,'~')=0 AND coalesce(m.status,'')!='no'
      AND NOT EXISTS (SELECT 1 FROM seeds s WHERE s.handle=p.handle)
      AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind='profile' AND j.handle=p.handle)
      ) ORDER BY source_order,handle LIMIT ?""", (room,)).fetchall()
    conn.executemany("INSERT INTO jobs(kind,handle,created_at) VALUES('profile',?,?)", [(r[0], db.now()) for r in candidates])
    conn.commit()
    return len(candidates)


def outstanding(conn):
    if settings(conn)['scope'] != 'missing':
        return conn.execute("SELECT count(*) FROM jobs WHERE kind='profile' AND state IN ('queued','leased')").fetchone()[0]
    # Include future unqueued work; an ETA based only on the current 256 slots would be misleading.
    return conn.execute("""SELECT count(*) FROM (
      SELECT p.handle FROM people p LEFT JOIN marks m ON m.person_id=p.id WHERE p.bio_at IS NULL AND instr(p.handle,'~')=0
      AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind='profile' AND j.handle=p.handle AND j.state IN ('error','done'))
      AND (coalesce(m.status,'')!='no' OR EXISTS (SELECT 1 FROM seeds s WHERE s.handle=p.handle))
      UNION SELECT s.handle FROM seeds s LEFT JOIN people p ON p.handle=s.handle WHERE p.bio_at IS NULL AND instr(s.handle,'~')=0
      AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind='profile' AND j.handle=s.handle AND j.state IN ('error','done'))
      UNION SELECT handle FROM jobs WHERE kind='profile' AND state IN ('queued','leased'))""").fetchone()[0]


def lease(conn, lane):
    conn.execute('BEGIN IMMEDIATE')
    if not enabled(conn):
        conn.commit()
        return None
    ts = db.now()
    conn.execute("UPDATE jobs SET state='error',leased_until=NULL,lane=NULL,lease_token=NULL "
                 "WHERE kind='profile' AND attempts>=5 AND (state='queued' OR (state='leased' AND leased_until<?))", (ts,))
    row = conn.execute("""SELECT j.* FROM jobs j LEFT JOIN public_bio_retries r ON r.job_id=j.id
      WHERE j.kind='profile' AND (j.state='queued' OR (j.state='leased' AND j.leased_until<?))
      AND (r.next_at IS NULL OR r.next_at<=?) AND j.attempts<5
      ORDER BY j.priority DESC,j.id LIMIT 1""", (ts, ts)).fetchone()
    if row is None:
        conn.commit()
        return None
    token = uuid.uuid4().hex
    until = (db.utc_now() + timedelta(minutes=5)).isoformat()
    conn.execute("UPDATE jobs SET state='leased', lane=?, lease_token=?, leased_until=?, attempts=attempts+1 WHERE id=?",
                 (lane, token, until, row['id']))
    identity = conn.execute('SELECT ig_id FROM people WHERE handle=? UNION ALL '
                            'SELECT ig_id FROM seeds WHERE handle=?',
                            (row['handle'], row['handle'])).fetchall()
    expected_id = next((str(r[0]) for r in identity if r[0]), None)
    conn.commit()
    return {**dict(row), 'lane': lane, 'lease_token': token, 'expected_ig_id': expected_id}


def renew(conn, lane):
    conn.execute("UPDATE jobs SET leased_until=? WHERE state='leased' AND lane=? AND leased_until>?",
                 ((db.utc_now() + timedelta(minutes=5)).isoformat(), lane, db.now()))
    conn.commit()


def release(conn, lane):
    conn.execute("UPDATE jobs SET state='queued', leased_until=NULL, lane=NULL, lease_token=NULL, attempts=max(0,attempts-1) "
                 "WHERE state='leased' AND lane=?", (lane,))
    conn.commit()


def import_item(conn, job, item):
    """Only the current lease holder may write. Missing bio never erases prior evidence."""
    try:
        return _import_item(conn, job, item)
    except Exception:
        # The shared worker connection also persists heartbeats and cooldowns.
        # None of those commits may publish a partly applied profile import.
        conn.rollback()
        raise


def _import_item(conn, job, item):
    conn.execute('BEGIN IMMEDIATE')
    owned = conn.execute("SELECT attempts FROM jobs WHERE id=? AND kind='profile' AND state='leased' AND lane=? AND lease_token=? AND leased_until>? AND handle=?",
                         (job['id'], job['lane'], job['lease_token'], db.now(), job['handle'])).fetchone()
    if not owned:
        conn.rollback()
        return False
    meta = item.get('_scrape') if isinstance(item.get('_scrape'), dict) else {}
    handle = db.norm_handle(item.get('username'))
    error = item.get('error')
    # A renamed account needs a stable id; an unrelated response must never replace this queued identity.
    old = conn.execute('SELECT ig_id,bio_at FROM people WHERE handle=?', (job['handle'],)).fetchone()
    seed = conn.execute('SELECT ig_id FROM seeds WHERE handle=?', (job['handle'],)).fetchone()
    raw_id = item.get('id')
    stable_id = str(raw_id) if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) and str(raw_id).isdigit() and int(raw_id) > 0 else None
    prior_by_id = conn.execute('SELECT bio_at FROM people WHERE ig_id=?', (stable_id,)).fetchone() if stable_id else None
    prior_bio_at = control.utc(prior_by_id['bio_at'] if prior_by_id else old['bio_at'] if old else None)
    expected_id = job.get('expected_ig_id') or (old['ig_id'] if old and old['ig_id'] else None) or (seed['ig_id'] if seed else None)
    identity_ok = bool(stable_id and handle and
                       (not expected_id or stable_id == str(expected_id)) and
                       (handle == db.norm_handle(job['handle']) or expected_id == stable_id))
    observed_raw = meta.get('observedAt')
    observed_at = control.utc(observed_raw) if isinstance(observed_raw, str) else None
    time_ok = bool(observed_at and observed_at <= db.utc_now())
    bio_ok = (meta.get('bioStatus') in ('observed', 'empty') and isinstance(item.get('biography'), str)
              and time_ok)
    if not identity_ok and not error:
        error, bio_ok = 'identity_mismatch', False
    elif not bio_ok:
        error = error or 'incomplete_profile'
    ts = db.now()
    known_at = prior_bio_at
    # Cache observation time is the source time, never the import time. Older snapshots cannot
    # regress any profile fields or move an account back to an old handle.
    stale = bool(known_at and observed_at and observed_at <= known_at)
    if not time_ok and not error:
        error, bio_ok = 'incomplete_profile', False
    expired_cache = bool(meta.get('cacheHit') and observed_at and
                         observed_at < db.utc_now() - timedelta(hours=24) and not known_at)
    if expired_cache:
        error, bio_ok = 'incomplete_profile', False
    if identity_ok and stable_id and time_ok and not stale and not expired_cache:
        mapping = {'id': 'ig_id', 'fullName': 'name', 'profilePicUrl': 'pic_url', 'private': 'is_private',
                   'verified': 'is_verified', 'externalUrl': 'website', 'businessCategoryName': 'category',
                   'followersCount': 'followers', 'followsCount': 'following', 'postsCount': 'posts',
                   'isBusinessAccount': 'is_business'}
        p = {'ig_id': stable_id}
        for src, dst in mapping.items():
            if src == 'id':
                continue
            value = item.get(src)
            if value is None:
                continue
            if dst in ('is_private', 'is_verified', 'is_business'):
                if not isinstance(value, bool):
                    continue
            elif dst in ('followers', 'following', 'posts'):
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    continue
            elif not isinstance(value, str):
                continue
            p[dst] = value
        p['handle'] = handle
        if bio_ok and not error:
            p.update(bio=item['biography'], bio_at=observed_at.isoformat(), bio_src='public:' + str(meta.get('source') or 'unknown'))
        pid = db.upsert_person(conn, p, observed_at.isoformat())
        rules.sync(conn, [pid])
        conn.execute('UPDATE seeds SET ig_id=? WHERE handle=?', (stable_id, handle))
    elif not stable_id and not error:
        error = 'incomplete_profile'
    saved = conn.execute('SELECT ig_id,bio_at FROM people WHERE ig_id=?', (stable_id,)).fetchone() if stable_id else None
    success = bool(not error and bio_ok and saved and saved['bio_at'])
    # An older observation can complete a job using an existing bio without saving
    # anything. Keep that successful callback, but exclude it from fresh-save speed.
    saved_this_result = bool(success and not stale and saved['bio_at'] == observed_at.isoformat()
                             and (not prior_bio_at or observed_at > prior_bio_at))
    deferred = error in ENVIRONMENT_ERRORS or (error == 'error' and str(item.get('errorDescription', '')).startswith('network error after retries:'))
    outcome = ('success' if saved_this_result else 'reused') if success else 'deferred' if deferred else 'error'
    conn.execute('INSERT INTO public_bio_results(job_id,handle,at,outcome,cache_hit,source,observed_at,error) VALUES(?,?,?,?,?,?,?,?)',
                 (job['id'], job['handle'], ts, outcome, int(bool(meta.get('cacheHit'))), str(meta.get('source') or '')[:80],
                  observed_at.isoformat() if observed_at else None, str(error)[:80] if error else None))
    if success:
        state = 'done'
        conn.execute('DELETE FROM public_bio_retries WHERE job_id=?', (job['id'],))
    else:
        prior = conn.execute('SELECT failures FROM public_bio_retries WHERE job_id=?', (job['id'],)).fetchone()
        failures = (prior[0] if prior else 0) + (0 if deferred else 1)
        state = 'error' if error in PERMANENT_ERRORS or failures >= 5 or (not deferred and owned['attempts'] >= 5) else 'queued'
        due = (db.utc_now() + timedelta(seconds=min(3600, 60 * 2 ** max(0, failures - 1)))).isoformat()
        if deferred:
            conn.execute('UPDATE jobs SET attempts=max(0,attempts-1) WHERE id=?', (job['id'],))
        conn.execute('INSERT OR REPLACE INTO public_bio_retries VALUES(?,?,?)', (job['id'], due, failures))
    conn.execute('UPDATE jobs SET state=?,leased_until=NULL,lane=NULL,lease_token=NULL WHERE id=?', (state, job['id']))
    conn.commit()
    return success


def _egress_key(eg, index, by_endpoint=False):
    # Persistent identity without exposing proxy credentials in database settings or the UI.
    from urllib.parse import urlsplit
    endpoint = urlsplit(eg.spec.url or '')
    identity = (None if by_endpoint else getattr(eg.spec, 'name', None)) or (str(endpoint.hostname) + ':' + str(endpoint.port) + endpoint.path)
    return hashlib.sha256((str(eg.spec.kind) + ':' + identity).encode()).hexdigest()[:24]


def save_cooldowns(conn, pool):
    mono, epoch = time.monotonic(), time.time()
    previous = db.get_setting(conn, 'public_bios_cooldowns') or {}
    # A configuration edit can temporarily remove a lane. Keep its outstanding server wait
    # so re-adding it cannot bypass Retry-After. Expired absent lanes can be discarded.
    saved = {}
    for key, item in previous.items():
        if not isinstance(item, dict):
            continue
        mono_at, mono_until = item.get('monoAt'), item.get('monoUntil')
        if isinstance(mono_at, (int, float)) and isinstance(mono_until, (int, float)) and mono >= mono_at:
            outstanding = mono_until > mono
        else:
            outstanding = isinstance(item.get('until'), (int, float)) and item['until'] > epoch
        if outstanding:
            saved[key] = item
    for index, eg in enumerate(pool.egresses):
        wait = max(eg.parked_until - mono, getattr(eg, 'retired_until', 0) - mono, 0)
        key = _egress_key(eg, index)
        prior = saved.get(key)
        if prior:
            # A concurrent persistence hook or a newly loaded pool cannot shorten
            # a server deadline already written by this worker.
            wait = max(wait, _cooldown_remaining(prior, epoch, mono))
        # Persist the learned limiter rate and strike history after a wait ends too.
        saved[key] = {'until': epoch + wait, 'remaining': wait, 'monoAt': mono,
                      'monoUntil': mono + wait, 'endpointKey': _egress_key(eg, index, by_endpoint=True),
                      'strikes': eg.strikes,
                      'rateStrikes': eg.rate_strikes, 'rpm': getattr(eg.limiter, 'rpm', None)}
    db.set_setting(conn, 'public_bios_cooldowns', saved)
    conn.commit()


def _cooldown_remaining(item, epoch, mono):
    until = item.get('until', 0)
    remaining = max(0, until - epoch) if isinstance(until, (int, float)) else 0
    duration = item.get('remaining')
    if isinstance(duration, (int, float)) and duration >= 0:
        # A backward wall clock jump must not lengthen the original pause.
        remaining = min(remaining, duration)
        mono_at, mono_until = item.get('monoAt'), item.get('monoUntil')
        if isinstance(mono_at, (int, float)) and isinstance(mono_until, (int, float)) and mono >= mono_at:
            # Across an ordinary process restart, monotonic time survives wall clock jumps.
            remaining = min(duration, max(0, mono_until - mono))
    return remaining


def restore_cooldowns(conn, pool):
    saved = db.get_setting(conn, 'public_bios_cooldowns') or {}
    epoch, mono = time.time(), time.monotonic()

    current_keys = {_egress_key(eg, index) for index, eg in enumerate(pool.egresses)}
    # Older workers may have used another hash. We cannot identify its lane, so honor
    # an outstanding legacy server deadline across all current lanes rather than lose it.
    legacy_wait = max((_cooldown_remaining(item, epoch, mono) for key, item in saved.items()
                       if key not in current_keys and isinstance(item, dict)
                       and not item.get('endpointKey')), default=0)
    # A rename can retain a cooldown if this endpoint identifies exactly one old and one
    # current lane. Multiple lanes on one proxy endpoint must keep their named identities.
    endpoint_counts = {}
    for eg in pool.egresses:
        key = _egress_key(eg, 0, by_endpoint=True)
        endpoint_counts[key] = endpoint_counts.get(key, 0) + 1
    old_endpoint_counts = {}
    for item in saved.values():
        if isinstance(item, dict) and item.get('endpointKey'):
            key = item['endpointKey']
            old_endpoint_counts[key] = old_endpoint_counts.get(key, 0) + 1
    for index, eg in enumerate(pool.egresses):
        item = saved.get(_egress_key(eg, index), {})
        endpoint_key = _egress_key(eg, index, by_endpoint=True)
        if not item and endpoint_counts[endpoint_key] == old_endpoint_counts.get(endpoint_key) == 1:
            item = next(value for value in saved.values()
                        if isinstance(value, dict) and value.get('endpointKey') == endpoint_key)
        if not isinstance(item, dict):
            continue
        remaining = max(_cooldown_remaining(item, epoch, mono), legacy_wait)
        if remaining:
            eg.parked_until = max(eg.parked_until, mono + remaining)
        eg.strikes = max(eg.strikes, item.get('strikes', 0))
        eg.rate_strikes = max(eg.rate_strikes, item.get('rateStrikes', 0))
        rpm = item.get('rpm')
        if isinstance(rpm, (int, float)) and rpm > 0 and getattr(eg.limiter, 'mode', 'adaptive') == 'adaptive':
            eg.limiter.rpm = min(eg.limiter.rpm, rpm)
        elif remaining and rpm is None:
            # Compatibility with records written before limiter state was saved.
            eg.limiter.on_block()


def status(conn, root=ROOT):
    cfg = settings(conn)
    path = config_path(cfg, root)
    missing = []
    if not path.is_file():
        missing.append('egress configuration')
    if not (Path(root) / 'data/scraper-venv/bin/python').is_file():
        missing.append('Python environment')
    ready = not missing
    now = db.utc_now()
    since = (now - timedelta(seconds=60)).isoformat()
    five_minutes_ago = (now - timedelta(minutes=5)).isoformat()
    counts = dict(conn.execute('SELECT outcome,count(*) FROM public_bio_results GROUP BY outcome'))
    until = now.isoformat()
    fresh = conn.execute("SELECT count(*) FROM public_bio_results WHERE outcome='success' AND cache_hit=0 AND at>=? AND at<=?", (since, until)).fetchone()[0]
    recent = conn.execute("""SELECT count(*), min(at), max(at) FROM public_bio_results
      WHERE outcome='success' AND cache_hit=0 AND at>=? AND at<=?""", (five_minutes_ago, until)).fetchone()
    fresh_five_minutes = recent[0]
    five_minute_rate = fresh_five_minutes / 5
    cached = conn.execute("SELECT count(*) FROM public_bio_results WHERE outcome IN ('success','reused') AND cache_hit=1").fetchone()[0]
    left = outstanding(conn)
    terminal = conn.execute("SELECT count(DISTINCT handle) FROM jobs WHERE kind='profile' AND state='error'").fetchone()[0]
    heartbeat = db.get_setting(conn, 'public_bios_heartbeat') or {}
    heartbeat_age = time.time() - heartbeat.get('epoch', 0)
    alive = 0 <= heartbeat_age < 10
    running = alive and enabled(conn)
    # A new burst is not a trend. Require several real saves spread across at least two
    # minutes, a save in the last minute, and a currently usable egress before estimating.
    observed_span = (control.utc(recent[2]) - control.utc(recent[1])).total_seconds() if recent[1] and recent[2] else 0
    progressing = running and heartbeat.get('healthyEgresses', 1) > 0 and fresh > 0
    eta_rate = five_minute_rate if progressing and fresh_five_minutes >= 5 and observed_span >= 120 else None
    eta_seconds = round(left / eta_rate * 60) if left and eta_rate else None
    return {**cfg, 'ready': ready, 'setupMissing': missing, 'configPath': str(path), 'running': running,
            'state': 'off' if not cfg['on'] else 'setup_needed' if not ready else 'paused' if control.stage_paused(conn, 'bios') else 'running' if running else 'starting' if cfg['on'] and ready else 'off',
            'successes': counts.get('success', 0) + counts.get('reused', 0), 'errors': counts.get('error', 0), 'cached': cached,
            'freshPerMinute': fresh, 'freshFiveMinutes': fresh_five_minutes,
            'etaRatePerMinute': eta_rate, 'remaining': left, 'terminalErrors': terminal,
            'deferred': counts.get('deferred', 0), 'etaSeconds': eta_seconds,
            'cooldowns': db.get_setting(conn, 'public_bios_cooldowns') or {}, 'worker': heartbeat, 'lastError': db.get_setting(conn, 'public_bios_error')}


def tick(db_path, root=ROOT):
    """Called by one server background loop. Never spawns a collector unless explicitly enabled."""
    global _process, _restart_after
    conn = db.connect(db_path)
    try:
        cfg = settings(conn)
        want = enabled(conn)
        if _process is not None and _process.poll() is not None:
            _process = None
            _restart_after = time.monotonic() + 10
        if not want:
            if _process is not None:
                _process.terminate()  # worker handles SIGTERM, cancels network operations and releases leases
            return
        if _process is not None or time.monotonic() < _restart_after:
            return
        heartbeat = db.get_setting(conn, 'public_bios_heartbeat') or {}
        if 0 <= time.time() - heartbeat.get('epoch', 0) < 10:
            return  # another server already owns a live worker for this database
        python = Path(root) / 'data/scraper-venv/bin/python'
        if not python.exists() or not config_path(cfg, root).exists():
            db.set_setting(conn, 'public_bios_error', 'Collector setup missing: Python environment or egress configuration.')
            conn.commit()
            return
        env = {k: v for k, v in os.environ.items() if k not in ('IG_SESSIONID', 'IG_COOKIES_FILE', 'PROXY_URL', 'IG_EGRESSES', 'IG_CACHE')}
        _process = subprocess.Popen([str(python), str(Path(root) / 'scraper/workspace_worker.py'), '--db', str(Path(db_path).resolve())],
                                    cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        conn.close()
