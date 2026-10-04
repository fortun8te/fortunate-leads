"""Exclusive benchmark dispatch using the existing account and request safeguards."""
import json
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import accounts
import control
import db
import edge_benchmark as ledger

KEY = 'raw_edge_benchmark'
LOCK = threading.RLock()
PACE_SECONDS = 12
BREAK_EVERY = 40
BREAK_SECONDS = 180
BACKGROUND_PROBE = None  # Server installs a read-only in-process activity callback.


def config(conn):
    value = db.get_setting(conn, KEY) or {}
    return value if isinstance(value, dict) and value.get('enabled') is True else None


def active(conn):
    return config(conn) is not None


def _meta(path):
    return ledger.state(path)


def _identity(conn, q, body, cfg):
    lane = accounts.lane_of(q, body)
    viewer = (accounts.account_from(q, body) or {}).get('ig_id')
    transport = body.get('transport') or (q.get('transport') or [''])[0]
    row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (lane,)).fetchone()
    if (not row or row['is_main'] or viewer != cfg['viewer_id'] or row['ig_id'] != viewer
            or lane != cfg['lane_id'] or transport not in ('chrome', 'mobile')):
        raise ValueError('Benchmark requires its pinned alternate and transport')
    return row, transport


def _blocked(conn, row, now, direction='following'):
    if db.get_setting(conn, 'instagram_request_attention') or row['hold']:
        return 'attention', 15000
    if accounts.paused_for(conn, row) or 'list' in control.paused_kinds(conn):
        return 'paused', 15000
    for value in (db.get_setting(conn, 'cooldown'), row['cooldown_until'], row['list_cool_until']):
        if value:
            try:
                wait = (accounts.utc(value) - now).total_seconds()
            except (ValueError, TypeError, AttributeError):
                return 'invalid_hold', 15000
            if wait > 0:
                return 'cooldown', max(1000, int(wait * 1000))
    if direction == 'followers' and accounts.follower_route_wait(conn, row, now):
        return 'cooldown', 15000
    if not accounts.role_allows(row, 'list'):
        return 'local_role', 15000
    if accounts.identity_handoff_pending(conn, row, now):
        return 'local_handoff', 15000
    if not accounts.identity_owner(conn, row, now):
        return 'local_identity_owner', 15000
    if accounts.identity_cooling(conn, row, 'list', now):
        return 'provider_cooldown', 15000
    if not accounts.request_budget_left(conn, row, 'list', now):
        return 'local_budget', 15000
    if 'list' not in accounts.kinds_for(conn, row, ['list'], now):
        return 'budget_or_account', 15000
    ready = (db.get_setting(conn, 'ext_ready') or {}).get(row['lane_id'], {}).get('list')
    if ready:
        try:
            wait = (accounts.utc(ready) - now).total_seconds()
            if wait > 0:
                return 'local_pacing', max(1000, int(wait * 1000))
        except (ValueError, TypeError, AttributeError):
            return 'invalid_pacing', 15000
    return None, 0


def _wait_bucket(reason):
    if reason in ('pacing', 'local_pacing', 'local_window'):
        return 'pacing'
    if reason in ('db', 'permit', 'local_policy', 'provider'):
        return reason
    if reason in ('cooldown', 'provider_cooldown', 'local_cooldown', 'local_endpoint_hold'):
        return 'provider'
    return 'local_policy'


def _wait(cfg, reason, now):
    prior = cfg.get('wait')
    totals = cfg.setdefault('waits', {})
    if prior:
        duration = max(0, now - prior['at'])
        bucket = _wait_bucket(prior['reason'])
        totals[bucket] = totals.get(bucket, 0) + duration
    cfg['wait'] = {'reason': reason, 'at': now} if reason else None


def _save(conn, cfg):
    db.set_setting(conn, KEY, cfg)
    conn.commit()


def _stopped(cfg, reason):
    return {'enabled': True, 'task': None, 'stopped': True, 'reason': reason, 'wait_ms': 15000}


def _inflight_wait(conn, cfg, row):
    inflight = cfg.get('inflight')
    if not inflight:
        return None
    if time.time() - inflight['at'] > accounts.REQUEST_LEASE_SECONDS:
        ledger.stop(cfg['path'], 'Unacknowledged transport outcome; no retry allowed')
        db.set_setting(conn, 'paused_lists', True)
        db.set_setting(conn, 'paused_bios', True)
        db.set_setting(conn, 'instagram_request_attention', {'lane': row['lane_id'], 'at': db.now(), 'message': 'Benchmark request did not confirm completion.'})
        conn.commit()
        return _stopped(cfg, 'uncertain_transport')
    return {'enabled': True, 'task': None, 'wait_ms': 1000, 'reason': 'inflight'}


def _client_wait(conn, cfg, q, body, transport):
    # Chrome's window/cooldown applies to mobile arms of the same account.
    value = body.get('client_wait_ms', (q.get('client_wait_ms') or [None])[0])
    if transport != 'chrome' or value is None:
        return
    try:
        milliseconds = float(value)
    except (ValueError, TypeError):
        raise ValueError('Invalid client wait') from None
    if not math.isfinite(milliseconds) or milliseconds < 0:
        raise ValueError('Invalid client wait')
    reason = body.get('client_wait_reason') or (q.get('client_wait_reason') or ['local_pacing'])[0]
    if reason not in ('local_pacing', 'local_window', 'local_cooldown', 'local_endpoint_hold'):
        raise ValueError('Invalid client wait reason')
    if milliseconds:
        until = time.time() + milliseconds / 1000
        if until > cfg.get('client_until', 0):
            cfg.update(client_until=until, client_reason=reason)
            db.set_setting(conn, KEY, cfg)
            conn.commit()


def _pace(cfg, now):
    if cfg.get('client_until', 0) > now:
        return cfg.get('client_reason', 'local_pacing'), max(1, int((cfg['client_until'] - now) * 1000))
    if cfg.get('next_at', 0) > now:
        return 'local_pacing', max(1, int((cfg['next_at'] - now) * 1000))
    return None, 0


def _background_wait(conn, now, background_probe=None):
    if conn.execute("SELECT 1 FROM jobs WHERE kind='profile' AND state='leased' AND leased_until>? LIMIT 1",
                    (accounts.iso(now),)).fetchone():
        return 'local_profile_drain', 1000
    probe = BACKGROUND_PROBE if background_probe is None else background_probe
    if not callable(probe):
        return 'local_background_unverified', 1000
    try:
        busy = probe()
    except Exception:
        return 'local_background_unverified', 1000
    if not isinstance(busy, (list, tuple, set)):
        return 'local_background_unverified', 1000
    if busy:
        return 'local_background_drain', 1000
    return None, 0


def next_task(conn, q, body, *, background_probe=None):
    with LOCK:
        cfg = config(conn)
        if not cfg:
            return {'enabled': False, 'task': None}
        # Other accounts also park ordinary collection while the cohort is armed.
        lane = accounts.lane_of(q, body)
        if lane != cfg['lane_id']:
            return {'enabled': True, 'task': None, 'wait_ms': 15000, 'reason': 'benchmark_other_viewer'}
        row, transport = _identity(conn, q, body, cfg)
        now = datetime.now(timezone.utc)
        meta = _meta(cfg['path'])
        if meta.get('viewer_safety_hold'):
            return _stopped(cfg, 'viewer_safety_hold')
        if meta['state'] in ('stopped', 'complete', 'completed'):
            return _stopped(cfg, meta.get('stop_reason') or meta['state'])
        _client_wait(conn, cfg, q, body, transport)
        inflight = _inflight_wait(conn, cfg, row)
        if inflight:
            return inflight
        task = ledger.next_task(cfg['path'], row['lane_id'], row['ig_id'], transport=transport)
        if not task:
            meta = _meta(cfg['path'])
            if meta['state'] in ('stopped', 'complete', 'completed'):
                return _stopped(cfg, meta.get('stop_reason') or meta['state'])
            current = meta.get('current_task')
            if current and not current['warmup'] and meta.get('phase') != 'measured':
                return _stopped(cfg, 'warmup_complete')
            return {'enabled': True, 'task': None, 'wait_ms': 1000, 'reason': 'other_transport'}
        if cfg.get('phase', 'warmup') == 'warmup' and not task['warmup']:
            return _stopped(cfg, 'warmup_complete')
        reason, wait = _blocked(conn, row, now, task['direction'])
        if not reason:
            reason, wait = _pace(cfg, now.timestamp())
        if not reason:
            reason, wait = _background_wait(conn, now, background_probe)
        if reason:
            _wait(cfg, reason, time.time())
            _save(conn, cfg)
            return {'enabled': True, 'task': None, 'wait_ms': wait, 'reason': reason}
        return {'enabled': True, 'task': task, 'wait_ms': 0}


def permit(conn, q, body, *, background_probe=None):
    with LOCK:
        cfg = config(conn)
        if not cfg:
            return {'enabled': False, 'granted': False}
        row, transport = _identity(conn, q, body, cfg)
        request_id = body.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id):
            raise ValueError('Stable request ID required')
        _client_wait(conn, cfg, q, body, transport)
        inflight = _inflight_wait(conn, cfg, row)
        if inflight:
            # Lost permit replies must not cause a duplicate upstream request.
            return dict(inflight, granted=False)
        now = datetime.now(timezone.utc)
        task = ledger.next_task(cfg['path'], row['lane_id'], row['ig_id'], transport=transport)
        if not task or task['task_id'] != body.get('task_id'):
            return {'granted': False, 'stopped': True, 'reason': 'task_changed'}
        if cfg.get('phase', 'warmup') == 'warmup' and not task['warmup']:
            return {'granted': False, 'stopped': True, 'reason': 'warmup_complete'}
        fingerprints = body.get('fingerprints')
        if (not isinstance(fingerprints, dict) or not fingerprints or
                any(not isinstance(k, str) or not isinstance(v, str) or
                    not re.fullmatch(r'[0-9a-f]{64}', v) for k, v in fingerprints.items())):
            raise ValueError('Non-secret session/device SHA256 fingerprint required')
        pinned = cfg.get('fingerprints', {}).get(transport)
        if pinned is not None and pinned != fingerprints:
            ledger.stop(cfg['path'], 'session_or_device_changed')
            db.set_setting(conn, 'paused_lists', True)
            db.set_setting(conn, 'paused_bios', True)
            db.set_setting(conn, 'instagram_request_attention', {'lane': row['lane_id'], 'at': db.now(),
                'message': 'Benchmark session or device fingerprint changed.'})
            conn.commit()
            return {'granted': False, 'stopped': True, 'reason': 'session_or_device_changed'}
        reason, wait = _blocked(conn, row, now, task['direction'])
        if not reason:
            reason, wait = _pace(cfg, now.timestamp())
        if not reason:
            reason, wait = _background_wait(conn, now, background_probe)
        if reason:
            _wait(cfg, reason, now.timestamp())
            _save(conn, cfg)
            return {'granted': False, 'wait_ms': max(1000, wait), 'reason': reason}
        conn.execute('BEGIN IMMEDIATE')
        granted = accounts.request_permit(conn, row['lane_id'], kind='list', now=now, commit=False)
        if not granted.get('granted'):
            _wait(cfg, 'permit', now.timestamp())
            db.set_setting(conn, KEY, cfg)
            conn.commit()
            return dict(granted, reason='permit')
        try:
            started = ledger.begin_request(cfg['path'], request_id, task['task_id'], row['ig_id'],
                                           started_at=now.timestamp(), fingerprints=fingerprints)
            if not started.get('send_allowed', True):
                conn.rollback()
                return {'granted': False, 'stopped': True, 'reason': 'request_already_recorded'}
            _wait(cfg, None, now.timestamp())
            cfg.setdefault('fingerprints', {})[transport] = fingerprints
            cfg['inflight'] = {'request_id': request_id, 'task_id': task['task_id'], 'token': granted['token'],
                               'transport': transport, 'at': now.timestamp(), 'expires_at': granted['expires_at'], 'waits': cfg.pop('waits', {})}
            cfg['attempts'] = cfg.get('attempts', 0) + 1
            cfg['next_at'] = now.timestamp() + (BREAK_SECONDS if cfg['attempts'] % BREAK_EVERY == 0 else PACE_SECONDS)
            today = accounts.jload(row['today'], {}) or {}
            if not row['last_seen'] or accounts.local_day(accounts.utc(row['last_seen'])) != accounts.local_day(now):
                today = {}
            today['list'] = today.get('list', 0) + 1
            accounts.touch(conn, row['lane_id'], today=json.dumps(today))
            db.set_setting(conn, KEY, cfg)
            conn.commit()
        except BaseException:
            conn.rollback()
            ledger.stop(cfg['path'], 'Permit transaction failed; no replay request allowed')
            raise
        return dict(granted, request_id=request_id)


def _ack(request_id, **values):
    return dict(ok=True, acknowledged=True, ack=True, request_id=request_id, **values)


def result(conn, q, body):
    with LOCK:
        cfg = db.get_setting(conn, KEY)
        if not isinstance(cfg, dict) or not cfg.get('path'):
            raise ValueError('Benchmark is not armed; preserve the local outbox')
        row, transport = _identity(conn, q, body, cfg)
        active_request = cfg.get('inflight')
        if not active_request:
            last = cfg.get('last_ack') or {}
            if last.get('request_id') == body.get('request_id') and last.get('transport') == transport and last.get('token') == body.get('token'):
                return _ack(body['request_id'], duplicate=True, stopped=bool(last.get('stopped')),
                            stop_scope=last.get('stop_scope'), viewer_safety_hold=bool(last.get('viewer_safety_hold')))
            raise ValueError('No matching outstanding benchmark request')
        if (active_request['request_id'] != body.get('request_id') or active_request['task_id'] != body.get('task_id')
                or active_request['token'] != body.get('token') or active_request['transport'] != transport):
            raise ValueError('Result does not match its benchmark permit')
        status = body.get('status', 'other')
        warning = body.get('terminal_warning')
        if isinstance(warning, bool):
            warning = status if warning else None
        completed = body.get('transport_completed')
        # Chrome reports uncertainty instead of an explicit completion field.
        if completed is None:
            completed = body.get('uncertain') is not True and type(body.get('actual_http_requests')) is int and body['actual_http_requests'] in (0, 1)
        uncertain = body.get('uncertain') is True or body.get('actual_http_requests') is None or completed is not True
        if status != 'ok' or uncertain:
            warning = warning or ('uncertain_transport' if uncertain else status)
        reasons = body.get('reason_flags') or []
        if not isinstance(reasons, list) or len(reasons) > 32 or any(not isinstance(v, str) or not re.fullmatch(r'[a-z0-9_]{1,80}', v) for v in reasons):
            raise ValueError('Only sanitized reason labels are accepted')
        failure_reason = body.get('failure_reason')
        if failure_reason is not None and (not isinstance(failure_reason, str) or not re.fullmatch(r'[a-z0-9_]{1,80}', failure_reason)):
            raise ValueError('Only a sanitized failure reason is accepted')
        started = time.monotonic()
        recorded = ledger.finish_request(cfg['path'], body['request_id'], rows=body.get('rows') or [],
            next_cursor=body.get('next_cursor'), has_more=body.get('has_more'), status=status,
            terminal_warning=warning, waits={key: sum(value for reason, value in active_request.get('waits', {}).items() if _wait_bucket(reason) == key) for key in ('pacing', 'permit', 'provider', 'local_policy', 'db')},
            actual_http_requests=body.get('actual_http_requests'), http_status=body.get('http_status'),
            duration_ms=body.get('duration_ms'), uncertain=uncertain, transport_completed=completed is True,
            requested_count=body.get('requested_count'), returned_count=body.get('returned_count'),
            device_fingerprint=body.get('device_fingerprint'),
            raw_returned_count=body.get('raw_returned_count', body.get('returned_count')),
            reason_flags=reasons, failure_reason=failure_reason,
            target_limited=body.get('target_limited') is True, reported_has_more=body.get('reported_has_more'))
        warning = recorded.get('terminal_warning') or warning
        scope = recorded.get('stop_scope')
        viewer_hold = recorded.get('viewer_safety_hold') is True
        # Provider/auth warnings always preserve identity-level safety, even if an arm stopped.
        safety_status = status in ('rate_limit', 'soft_block', 'challenge', 'auth', 'login')
        stopped = bool(uncertain or viewer_hold or safety_status or scope == 'cohort' or (warning and scope is None))
        if stopped:
            db.set_setting(conn, 'paused_lists', True)
            db.set_setting(conn, 'paused_bios', True)
            db.set_setting(conn, 'instagram_request_attention', {'lane': row['lane_id'], 'at': db.now(),
                'message': 'Benchmark stopped: ' + str(warning)[:150]})
            if status in ('challenge', 'auth', 'login'):
                accounts.touch(conn, row['lane_id'], hold='challenge' if status == 'challenge' else 'login')
            retry_after = body.get('retry_after')
            if retry_after:
                try:
                    value = str(retry_after).strip()
                    if value.isdigit():
                        until = datetime.now(timezone.utc) + timedelta(seconds=int(value))
                    else:
                        try:
                            until = accounts.utc(value)
                        except (ValueError, TypeError):
                            until = parsedate_to_datetime(value)
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=timezone.utc)
                    old = db.get_setting(conn, 'cooldown')
                    if not old or accounts.utc(old) < until:
                        db.set_setting(conn, 'cooldown', accounts.iso(until))
                except (ValueError, TypeError, AttributeError, OverflowError):
                    pass  # Attention hold still requires explicit inspection.
        if not uncertain:
            accounts.request_permit(conn, row['lane_id'], token=active_request['token'], commit=False)
        cfg['last_ack'] = {'request_id': body['request_id'], 'transport': transport, 'token': body['token'], 'stopped': stopped, 'stop_scope': scope, 'viewer_safety_hold': viewer_hold or safety_status}
        cfg['inflight'] = None
        cfg['db_seconds'] = cfg.get('db_seconds', 0) + time.monotonic() - started
        db.set_setting(conn, KEY, cfg)
        conn.commit()
        return _ack(body['request_id'], result=recorded, stopped=stopped, stop_scope=scope,
                    viewer_safety_hold=viewer_hold or safety_status)
