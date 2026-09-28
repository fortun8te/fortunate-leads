"""Optional, explicit-session mobile list collector. No login or fallback routes.

The transport uses an instagrapi-prepared requests Session, not its retrying
private_request or paginating convenience helpers. One permit means one GET.
"""
import json
import os
from pathlib import Path
import re
import stat
import time
from email.utils import parsedate_to_datetime
import uuid
from datetime import datetime, timedelta, timezone
import urllib.parse
import urllib.request

import db

BACKEND = 'mobile'
VERSION = 'mobile-0.1'
INSTAGRAPI_VERSION = '3.0.14'
FLAG = 'mobile_backend_enabled'
ID = re.compile(r'\d{1,30}')


class Stopped(RuntimeError):
    pass


class ResponseWarning(Stopped):
    def __init__(self, code, reason, status=0, retry_after=None, uncertain=False):
        super().__init__(reason)
        self.code, self.reason, self.status = code, reason, status
        self.retry_after, self.uncertain = retry_after, uncertain


def default_outbox(lane):
    if not isinstance(lane, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', lane):
        raise ValueError('Lane must contain only letters, digits, underscores or hyphens')
    return Path.home() / 'Library/Application Support/Fortunate Leads/mobile' / lane / 'outbox.json'


def validate_client(conn, lane, backend, viewer_id=None, allow_disabled=False):
    if backend not in ('chrome', 'mobile'):
        raise ValueError('Unknown collection backend')
    row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()
    if backend == 'chrome':
        if row and row['collection_backend'] != 'chrome':
            raise ValueError('This lane is assigned to the mobile collector')
        return row
    if not allow_disabled and db.get_setting(conn, FLAG, False) is not True:
        raise ValueError('Mobile collector is disabled')
    if (not row or row['collection_backend'] != 'mobile' or row['is_main']
            or not isinstance(viewer_id, str) or not ID.fullmatch(viewer_id)
            or row['ig_id'] != viewer_id):
        raise ValueError('Mobile collection requires its assigned alternate and matching session identity')
    return row


def configure_lane(conn, lane, viewer_id, enabled=False, disable=False):
    """Explicit offline configuration only; never clears pauses or warning holds."""
    if enabled is not True and disable is not True:
        raise ValueError('Explicit enable is required')
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()
    if not row or row['is_main'] or not viewer_id or row['ig_id'] != viewer_id:
        raise ValueError('Choose a known alternate with matching Instagram ID')
    if not (db.get_setting(conn, 'paused', False) or db.get_setting(conn, 'paused_lists', False)):
        raise ValueError('Pause collection before assigning a backend')
    if (db.get_setting(conn, 'instagram_request_gate') or {}).get('active'):
        raise ValueError('An Instagram request is still outstanding')
    if conn.execute("SELECT 1 FROM jobs WHERE state='leased' AND lane=?", (lane,)).fetchone():
        raise ValueError('This lane still owns leased work')
    backend = 'chrome' if disable else BACKEND
    conn.execute('UPDATE accounts SET collection_backend=? WHERE lane_id=?', (backend, lane))
    db.set_setting(conn, FLAG, bool(conn.execute("SELECT 1 FROM accounts WHERE collection_backend='mobile'").fetchone()))
    return {'enabled': not disable, 'backend': backend, 'lane': lane, 'viewer_ig_id': viewer_id}


def queue_mobile_list(conn, lane, viewer_ig_id, seed, direction, count=200):
    """Only untouched targets initially: never adopt or overwrite Chrome history."""
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    row = validate_client(conn, lane, BACKEND, viewer_ig_id)
    if row['role'] not in ('lists', 'both'):
        raise ValueError('This account is not assigned list work')
    seed = db.norm_handle(seed)
    if not re.fullmatch(r'[a-z0-9._]{1,30}', seed or '') or direction not in ('followers', 'following'):
        raise ValueError('Valid target and list direction required')
    if type(count) is not int or count not in (25, 50, 100, 200):
        raise ValueError('Mobile page count must be 25, 50, 100 or 200')
    person = conn.execute('SELECT ig_id FROM people WHERE handle=?', (seed,)).fetchone()
    if not person or not isinstance(person['ig_id'], str) or not ID.fullmatch(person['ig_id']):
        raise ValueError('Target must have a saved numeric Instagram ID')
    if conn.execute('SELECT 1 FROM accounts WHERE ig_id=? OR handle=?', (person['ig_id'], seed)).fetchone():
        raise ValueError('Collector and main accounts are excluded as mobile targets')
    if (conn.execute('SELECT 1 FROM seeds WHERE handle=?', (seed,)).fetchone()
            or conn.execute('SELECT 1 FROM lists WHERE seed=?', (seed,)).fetchone()
            or conn.execute("SELECT 1 FROM jobs WHERE kind='list' AND seed=?", (seed,)).fetchone()
            or conn.execute('SELECT 1 FROM edges WHERE seed=? LIMIT 1', (seed,)).fetchone()):
        raise ValueError('Mobile collection initially requires an untouched target')
    stamp = db.now()
    conn.execute('INSERT INTO seeds(handle,ig_id,added_at) VALUES(?,?,?)', (seed, person['ig_id'], stamp))
    conn.execute("INSERT INTO lists(seed,direction,state,lane,updated_at) VALUES(?,?,'queued',?,?)",
                 (seed, direction, lane, stamp))
    job_id = conn.execute("INSERT INTO jobs(kind,seed,direction,state,created_at,page_size,collection_backend,backend_lane,backend_viewer_ig_id) "
                          "VALUES('list',?,?,'queued',?,?,'mobile',?,?)",
                          (seed, direction, stamp, count, lane, viewer_ig_id)).lastrowid
    db.start_list_run(conn, job_id, seed, direction)
    return {'id': job_id, 'job_id': job_id, 'seed': seed, 'direction': direction, 'count': count, 'backend': BACKEND}


def session_identity(client):
    """Only compare local identity fields; this must never trigger an API lookup."""
    cookie = client.cookie_dict.get('ds_user_id')
    authorization = (client.authorization_data or {}).get('ds_user_id')
    identities = {str(v) for v in (cookie, authorization, client.user_id) if v is not None}
    if len(identities) != 1 or not ID.fullmatch(next(iter(identities), '')):
        raise Stopped('Session identity is missing or inconsistent')
    return identities.pop()


def validate_saved_session(saved):
    required = {'uuids': ('phone_id', 'uuid', 'client_session_id', 'advertising_id', 'android_device_id', 'request_id', 'tray_session_id'),
                'device_settings': ('app_version', 'android_version', 'android_release', 'dpi', 'resolution', 'manufacturer', 'device', 'model', 'cpu', 'version_code', 'bloks_versioning_id')}
    if not isinstance(saved, dict) or any(not isinstance(saved.get(section), dict) or
            any(not saved[section].get(key) for key in keys) for section, keys in required.items()):
        raise Stopped('A complete saved mobile session and device identity are required; browser cookies are unsupported')
    for key in ('ds_user_id', 'sessionid'):
        values = {str(saved[section][key]) for section in ('cookies', 'authorization_data')
                  if isinstance(saved.get(section), dict) and saved[section].get(key)}
        if len(values) != 1 or (key == 'ds_user_id' and not ID.fullmatch(next(iter(values), ''))):
            raise Stopped('Saved mobile session authentication is missing or inconsistent')


def load_client(settings_path):
    """Lazy optional dependency. Loading settings never logs in or saves secrets."""
    import importlib.metadata
    if importlib.metadata.version('instagrapi') != INSTAGRAPI_VERSION:
        raise Stopped('This prototype requires instagrapi ' + INSTAGRAPI_VERSION)
    from instagrapi import Client
    from requests.adapters import HTTPAdapter
    path = Path(settings_path).expanduser()
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise Stopped('Session settings must be a private file readable only by its owner')
    saved = json.loads(path.read_text())
    validate_saved_session(saved)
    client = Client(session_retry_total=0, private_transport='requests')
    client.load_settings(str(path))
    session_identity(client)
    # Saved settings may restore curl and retries; reset after loading them.
    client.set_retry_config(private_transport='requests', session_retry_total=0)
    client.private.trust_env = False
    client.private.proxies.clear()
    client.private.verify = True
    client.private.mount('https://', HTTPAdapter(max_retries=0))
    client.private.mount('http://', HTTPAdapter(max_retries=0))
    return client


def one_page(client, job, viewer_id):
    if session_identity(client) != viewer_id:
        raise Stopped('Session identity no longer matches the assigned alternate')
    if job.get('collection_backend') != BACKEND or job.get('backend_viewer_ig_id') != viewer_id:
        raise Stopped('Job is not bound to this mobile session')
    target, direction, count = str(job.get('ig_id') or ''), job.get('direction'), job.get('page_size')
    if not ID.fullmatch(target) or direction not in ('followers', 'following') or type(count) is not int or count not in (25, 50, 100, 200):
        raise Stopped('Invalid mobile list request')
    cursor = job.get('cursor')
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
        raise Stopped('Invalid mobile cursor')
    params = {'count': count, 'rank_token': client.rank_token}
    if direction == 'followers':
        params.update(search_surface='follow_list_page', query='', enable_groups='true')
    if cursor:
        params['max_id'] = cursor
    headers = dict(client.base_headers)
    if client.authorization:
        headers['Authorization'] = client.authorization
    response = None
    try:
        # No private_request: it retries and may resolve challenges automatically.
        response = client.private.get('https://i.instagram.com/api/v1/friendships/' + target + '/' + direction + '/',
            params=params, headers=headers, timeout=(5, 20), allow_redirects=False, stream=True, proxies={})
        status = response.status_code
        retry_after = response.headers.get('Retry-After')
        if status == 429:
            raise ResponseWarning('rate_limit', 'mobile_http_429', status, retry_after)
        if status in (401, 403):
            raise ResponseWarning('login', 'mobile_auth_required', status)
        if 300 <= status < 400:
            raise ResponseWarning('soft_block', 'mobile_redirect', status)
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            raw.extend(chunk)
            if len(raw) > 8_000_000:
                raise ResponseWarning('other', 'mobile_response_too_large', status)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ResponseWarning('soft_block', 'mobile_non_json_response', status) from None
        if not isinstance(payload, dict):
            raise ResponseWarning('other', 'mobile_invalid_response', status)
        message = (str(payload.get('message', '')) + ' ' + str(payload.get('error_type', ''))).lower()
        if payload.get('challenge') or payload.get('two_factor_required') is True or any(v in message for v in ('challenge', 'checkpoint', 'two_factor')):
            raise ResponseWarning('challenge', 'mobile_challenge', status)
        if 'login_required' in message or payload.get('require_login') is True:
            raise ResponseWarning('login', 'mobile_login_required', status)
        if payload.get('spam') is True or any(v in message for v in ('feedback_required', 'please wait', 'sentry_block', 'rate_limit')):
            raise ResponseWarning('soft_block', 'mobile_feedback_required', status, retry_after)
        if status != 200 or payload.get('status') != 'ok' or not isinstance(payload.get('users'), list):
            raise ResponseWarning('other', 'mobile_invalid_page', status)
        users = []
        for user in payload['users']:
            if not isinstance(user, dict) or not ID.fullmatch(str(user.get('pk') or '')) or not re.fullmatch(r'[a-zA-Z0-9._]{1,30}', str(user.get('username') or '')):
                raise ResponseWarning('other', 'mobile_invalid_user', status)
            mapped = {'ig_id': str(user['pk']), 'handle': user['username']}
            for source, dest in (('full_name', 'name'), ('profile_pic_url', 'pic_url'), ('is_private', 'is_private'), ('is_verified', 'is_verified')):
                if source in user:
                    mapped[dest] = user[source]
            users.append(mapped)
        next_cursor = payload.get('next_max_id') or None
        if next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 4096):
            raise ResponseWarning('other', 'mobile_invalid_cursor', status)
        more = payload.get('has_more')
        if more is not None and type(more) is not bool:
            raise ResponseWarning('other', 'mobile_invalid_end', status)
        if more is True and not next_cursor:
            raise ResponseWarning('other', 'mobile_missing_cursor', status)
        if 'should_limit_list_of_followers' in payload and type(payload['should_limit_list_of_followers']) is not bool:
            raise ResponseWarning('other', 'mobile_invalid_limit', status)
        if more is False and next_cursor:
            raise ResponseWarning('other', 'mobile_conflicting_end', status)
        return {'users': users, 'requested_cursor': cursor, 'next_cursor': next_cursor,
                'has_more': more, 'done': more is False and next_cursor is None,
                'limited': payload.get('should_limit_list_of_followers') is True,
                'requested_count': count, 'http_status': status, 'total_source': 'unknown'}
    except (ResponseWarning, Stopped):
        raise
    except Exception:
        # Transport errors can leave an outstanding upstream request. Keep the
        # shared permit until server expiry raises its existing attention hold.
        raise ResponseWarning('other', 'mobile_transport_uncertain', uncertain=True) from None
    finally:
        if response is not None:
            response.close()


class LocalAPI:
    def __init__(self, lane, viewer_id, url='http://127.0.0.1:8777'):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password or parsed.path not in ('', '/'):
            raise ValueError('Collector API must be loopback HTTP')
        self.url, self.lane, self.viewer_id = url.rstrip('/'), lane, viewer_id
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise Stopped('Local API redirected the request')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, path, body=None):
        identity = {'backend': BACKEND, 'lane_id': self.lane, 'account': {'ig_id': self.viewer_id}, 'version': VERSION}
        if body is None:
            path += ('&' if '?' in path else '?') + urllib.parse.urlencode({
                'backend': BACKEND, 'lane': self.lane, 'ig_id': self.viewer_id, 'version': VERSION})
        else:
            body = dict(body, **identity)
        request = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                                         headers={'X-FL': '1', 'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=15) as response:
            data = json.loads(response.read(8_000_001))
        if not isinstance(data, dict):
            raise Stopped('Invalid local API response')
        return data


def _save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out)
    temporary.replace(path)


def run_once(api, client, outbox_path):
    """At most one upstream page. Persist its outcome before local ingestion."""
    path = Path(outbox_path)
    job_path = path.with_suffix('.job.json')
    viewer = session_identity(client)
    if viewer != api.viewer_id:
        raise Stopped('Session identity does not match the configured lane')
    if path.exists():
        pending = json.loads(path.read_text())
        if pending.get('lane') != api.lane or pending.get('viewer') != viewer:
            raise Stopped('Outbox belongs to another source identity')
        if pending.get('path') not in ('/api/ext/list-page', '/api/ext/error') or not isinstance(pending.get('body'), dict):
            raise Stopped('Invalid saved collector outcome')
        result = api.call(pending['path'], pending['body'])
        if result.get('stale') or result.get('ok') is False or (pending['path'].endswith('list-page') and type(result.get('received')) is not int):
            raise Stopped('Saved outcome was not acknowledged; no new Instagram request made')
        if not pending.get('uncertain'):
            api.call('/api/ext/request', {'action': 'release', 'token': pending['permit']})
        path.unlink()
        job_path.unlink(missing_ok=True)
        return dict(pending.get('metrics', {}), **{'state': 'stopped' if pending['path'].endswith('error') else 'saved', 'replayed': True, 'upstream_requests': 0,
                'received': result.get('received'), 'returned_count': len(pending['body'].get('users', []))})
    state = api.call('/api/mobile/state')
    if state.get('backend') != BACKEND or state.get('backend_cursor_isolated') is not True:
        raise Stopped('Server does not confirm mobile cursor isolation')
    if state.get('paused') is not False or state.get('cooldown_until') or state.get('stages', {}).get('list') is not True:
        return {'state': 'waiting', 'upstream_requests': 0}
    if job_path.exists():
        cached = json.loads(job_path.read_text())
        if cached.get('lane') != api.lane or cached.get('viewer') != viewer:
            raise Stopped('Saved lease belongs to another source identity')
        job = cached['job']
    else:
        next_job = api.call('/api/ext/next?kinds=list')
        if next_job.get('paused') is not False or next_job.get('cooldown_until') or next_job.get('stages', {}).get('list') is not True:
            return {'state': 'waiting', 'upstream_requests': 0}
        job = next_job.get('job')
    if not job:
        return {'state': 'idle', 'upstream_requests': 0}
    if (job.get('collection_backend') != BACKEND or job.get('backend_lane') != api.lane
            or job.get('backend_viewer_ig_id') != viewer or not job.get('lease_token')):
        raise Stopped('Server returned an unbound job or Chrome cursor')
    _save(job_path, {'lane': api.lane, 'viewer': viewer, 'job': job})
    permit = api.call('/api/ext/request', {'action': 'acquire', 'kind': 'list', 'job_id': job['id'], 'lease_token': job['lease_token']})
    if permit.get('stale'):
        job_path.unlink(missing_ok=True)
        return {'state': 'stopped', 'upstream_requests': 0}
    if permit.get('granted') is not True:
        return {'state': 'waiting', 'upstream_requests': 0}
    if not permit.get('token'):
        raise Stopped('Missing request permit token')
    base = {'job_id': job['id'], 'lease_token': job['lease_token'], 'seed': job['seed'], 'ig_id': job['ig_id'], 'direction': job['direction']}
    started = time.monotonic()
    try:
        page = one_page(client, job, viewer)
        body, endpoint, uncertain = dict(base, **page, captured_at=db.now()), '/api/ext/list-page', False
    except ResponseWarning as warning:
        body = dict(base, code=warning.code, reason=warning.reason, http_status=warning.status,
                    kind='list', event_id=uuid.uuid4().hex, message=warning.reason)
        if warning.retry_after:
            try:
                value = str(warning.retry_after).strip()
                deadline = (datetime.now(timezone.utc) + timedelta(seconds=int(value))) if value.isdigit() else parsedate_to_datetime(value)
                body['retry_after'] = deadline.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError, OverflowError):
                # An unrepresentable explicit deadline must not shorten a hold.
                body['retry_after'] = datetime.max.replace(tzinfo=timezone.utc).isoformat()
        endpoint, uncertain = '/api/ext/error', warning.uncertain
    except Stopped:
        api.call('/api/ext/request', {'action': 'release', 'token': permit['token']})
        raise
    metrics = {'job_id': job['id'], 'page_size': job['page_size'], 'elapsed_ms': round((time.monotonic() - started) * 1000),
               'distinct_rows': len({u['ig_id'] for u in body.get('users', [])})}
    pending = {'metrics': metrics, 'lane': api.lane, 'viewer': viewer, 'path': endpoint, 'body': body, 'permit': permit['token'], 'uncertain': uncertain}
    _save(path, pending)
    result = run_once(api, client, path)
    return dict(result, upstream_requests=1, returned_count=len(body.get('users', [])),
                state='stopped' if endpoint.endswith('error') else 'saved')
