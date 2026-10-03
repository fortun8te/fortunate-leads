"""Opt-in reopening of saved Chrome profiles on startup or collection resume.

Chrome loads the persistent profile without a foreground window. The extension
owns Instagram tab creation, authentication checks and all request pacing.
"""
import json
import logging
import re
import secrets
import time
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import accounts
import control
import db

CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
_CONFIG = 'browser-startup.json'
_LOCK = threading.Lock()
_START_LOCK = threading.Lock()
_INTENT = 'collection_startup'
_PENDING = ('opening', 'waiting')
_TIMEOUT = 90


def read_config(repo):
    path = Path(repo) / 'data' / _CONFIG
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('enabled') is not True:
        return []
    profiles = data.get('profiles')
    if not isinstance(profiles, list) or not 1 <= len(profiles) <= 8:
        raise ValueError('Choose between one and eight saved Chrome profiles.')
    directories, lanes = set(), set()
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError('Invalid saved Chrome profile.')
        directory, lane, identity = (profile.get(key) for key in ('directory', 'lane_id', 'ig_id'))
        if (not isinstance(directory, str) or not re.fullmatch(r'Default|Profile [1-9][0-9]*', directory)
                or not isinstance(lane, str) or not accounts.LANE_RX.fullmatch(lane)
                or not isinstance(identity, str) or not re.fullmatch(r'[0-9]{1,30}', identity)
                or directory in directories or lane in lanes):
            raise ValueError('Each saved Chrome profile must have its own verified account.')
        directories.add(directory)
        lanes.add(lane)
    return profiles


def connectable(conn, profile):
    row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (profile['lane_id'],)).fetchone()
    return bool(row and str(row['ig_id']) == profile['ig_id'] and row['collection_backend'] == 'chrome'
                and not row['hold'] and not accounts.collection_protected(conn, row) and accounts.isolation_allows(conn, row))


def wanted(conn, profile):
    if all(control.stage_paused(conn, stage) for stage in ('lists', 'bios')):
        return False
    row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (profile['lane_id'],)).fetchone()
    if not row or row['paused'] or not connectable(conn, profile):
        return False
    stages = ('lists', 'bios') if row['role'] == 'both' else (row['role'],)
    return any(not control.stage_paused(conn, stage) for stage in stages)


def launch_saved(repo, conn, *, popen=subprocess.Popen, now=None, profiles=None, request_id=None):
    """One bounded attempt, never a watchdog. Does not change collection intent."""
    profiles = read_config(repo) if profiles is None else profiles
    result = {'requested': 0, 'skipped': 0, 'failed': 0}
    timestamp = (now or datetime.now(timezone.utc)).timestamp()
    for profile in profiles:
        # A second startup process must not dispatch the same profile twice.
        conn.execute('BEGIN IMMEDIATE')
        try:
            intent = db.get_setting(conn, _INTENT) or {}
            allowed = (connectable(conn, profile) and intent.get('id') == request_id and intent.get('state') in _PENDING
                       if request_id else wanted(conn, profile))
            if not allowed:
                result['skipped'] += 1
                continue
            key = 'browser_startup:' + profile['lane_id']
            previous = db.get_setting(conn, key) or {}
            tried = previous.get('requested_at', 0) if isinstance(previous, dict) else 0
            if (request_id and previous.get('request_id') == request_id or not request_id
                    and isinstance(tried, (float, int)) and 0 <= timestamp - tried < 60):
                result['skipped'] += 1
                continue
            db.set_setting(conn, key, {'requested_at': timestamp, 'request_id': request_id, 'state': 'opening'})
        finally:
            conn.commit()
        # Direct executable dispatch preserves --profile-directory when Chrome
        # is already running. No URL means no new foreground tab or login bypass.
        try:
            process = popen([str(CHROME), '--profile-directory=' + profile['directory'], '--no-startup-window'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
        except (OSError, subprocess.SubprocessError) as exc:
            result['failed'] += 1
            db.set_setting(conn, key, {'requested_at': timestamp, 'request_id': request_id, 'state': 'failed',
                                      'message': 'Chrome could not open this profile: ' + str(exc)[:160]})
            conn.commit()
            continue
        # Reap the short dispatch process without waiting for a cold Chrome to exit.
        threading.Thread(target=process.wait, daemon=True).start()
        result['requested'] += 1
    return result


def can_launch(repo, conn):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    return bool(sys.platform == 'darwin' and path
                and Path(path).resolve() == (Path(repo).resolve() / 'data/leads.sqlite').resolve())


def schedule_saved(repo, conn):
    """Only the canonical live database can reopen local applications."""
    repo = Path(repo).resolve()
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if (sys.platform != 'darwin' or not path or
            Path(path).resolve() != (repo / 'data/leads.sqlite').resolve() or
            not (repo / 'data' / _CONFIG).exists() or not _LOCK.acquire(blocking=False)):
        return False
    def run():
        try:
            fresh = db.connect(path)
            try:
                launch_saved(repo, fresh)
            finally:
                fresh.close()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            logging.warning('Saved Chrome profiles could not be reopened: %s', exc)
        finally:
            _LOCK.release()
    threading.Thread(target=run, name='saved-chrome-profiles', daemon=True).start()
    return True


def snapshot(conn):
    intent = db.get_setting(conn, _INTENT)
    if not isinstance(intent, dict):
        return None
    result = {key: intent.get(key) for key in ('id', 'state', 'message', 'accounts', 'requested_at', 'deadline_at')}
    if result['state'] in _PENDING and time.time() >= intent['deadline_at']:
        result.update(state='failed', message='Chrome did not reconnect in time. Try connecting again.')
    return result


def cancel(conn):
    intent = db.get_setting(conn, _INTENT) or {}
    if intent.get('state') in _PENDING:
        intent.update(state='cancelled', message='Connection attempt stopped. Collection stays paused.')
        db.set_setting(conn, _INTENT, intent)
        conn.commit()


def _warning_signature(conn):
    warning = db.get_setting(conn, 'instagram_scraping_warning') or {}
    return sorted([str(item.get('lane')) + ':' + str(item.get('at'))
                   for item in [warning, *warning.get('pending', {}).values()] if item])


def _selected(repo, conn, body):
    profiles = read_config(repo)
    requested = body.get('accounts') if body.get('action') == 'resume_selected_accounts' else None
    isolation = db.get_setting(conn, 'instagram_collection_isolation') or {}
    if requested is None and isolation:
        requested = [{'lane_id': lane, 'ig_id': identity} for lane, identity in isolation['accounts'].items()]
    if requested is not None:
        if not isinstance(requested, list) or len(requested) not in (1, 2) or any(not isinstance(item, dict) for item in requested):
            raise ValueError('Choose one or two saved Instagram accounts.')
        selected = {(item.get('lane_id'), item.get('ig_id')) for item in requested}
        if len(selected) != len(requested) or isolation and not selected.issubset(set(isolation['accounts'].items())):
            raise ValueError('Connect only previously selected accounts.')
        profiles = [profile for profile in profiles if (profile['lane_id'], profile['ig_id']) in selected]
        if len(profiles) != len(selected):
            raise ValueError('A selected account has no saved Chrome profile. Check account setup.')
    else:
        profiles = [profile for profile in profiles if (row := conn.execute(
            'SELECT * FROM accounts WHERE lane_id=? AND paused=0', (profile['lane_id'],)).fetchone())
            and not accounts.collection_protected(conn, row)]
    if not profiles:
        raise ValueError('No saved Chrome profiles are configured. Check account setup.')
    if any(not connectable(conn, profile) for profile in profiles):
        raise ValueError('A selected account needs attention. Its security hold has not been cleared.')
    return profiles


def begin(repo, conn, body, *, only_if_offline=False):
    """Save explicit intent before a bounded worker opens exact configured profiles."""
    now = datetime.now(timezone.utc)
    existing = db.get_setting(conn, _INTENT) or {}
    pending = existing.get('state') in _PENDING and existing.get('deadline_at', 0) > now.timestamp()
    requested = body.get('accounts')
    if not pending and only_if_offline and isinstance(requested, list) and len(requested) in (1, 2) and all(isinstance(item, dict) for item in requested):
        rows = [conn.execute('SELECT last_seen FROM accounts WHERE lane_id=?', (item.get('lane_id'),)).fetchone() for item in requested]
        if all(row and row['last_seen'] and now - accounts.utc(row['last_seen']) < accounts.ONLINE_FOR for row in rows):
            return False
    profiles = _selected(repo, conn, body)
    if not pending and only_if_offline and requested is None:
        rows = [conn.execute('SELECT last_seen FROM accounts WHERE lane_id=?', (p['lane_id'],)).fetchone() for p in profiles]
        if all(row and row['last_seen'] and now - accounts.utc(row['last_seen']) < accounts.ONLINE_FOR for row in rows):
            return False
    if pending:
        selected = [{'lane_id': p['lane_id'], 'ig_id': p['ig_id']} for p in profiles]
        if selected != existing['accounts']:
            raise ValueError('Stop the current connection attempt before choosing other accounts.')
        if body['action'] != 'connect_accounts':
            existing['command'] = body
            db.set_setting(conn, _INTENT, existing)
            conn.commit()
        return True
    if control.shared_collection_wait(conn, now) or db.get_setting(conn, 'instagram_request_attention'):
        raise ValueError('Instagram is on a protective pause. Collection stays paused.')
    intent = {'id': secrets.token_hex(12), 'state': 'opening', 'message': 'Opening your saved Chrome profiles…',
              'accounts': [{'lane_id': p['lane_id'], 'ig_id': p['ig_id']} for p in profiles],
              'profiles': profiles, 'requested_at': now.timestamp(), 'deadline_at': now.timestamp() + _TIMEOUT,
              'command': body, 'warnings': _warning_signature(conn)}
    db.set_setting(conn, _INTENT, intent)
    conn.commit()
    if not schedule(repo, conn):
        intent.update(state='failed', message='Could not start the local Chrome connection. Try again.')
        db.set_setting(conn, _INTENT, intent)
        conn.commit()
        raise ValueError(intent['message'])
    return True


def run_pending(repo, conn, *, popen=subprocess.Popen, clock=time.time, sleep=time.sleep):
    """One finite connection attempt; only a fresh matching heartbeat can finish it."""
    intent = db.get_setting(conn, _INTENT) or {}
    if intent.get('state') not in _PENDING:
        return
    request_id = intent['id']
    if clock() >= intent['deadline_at']:
        intent.update(state='failed', message='Connection attempt expired. Try connecting again.')
        db.set_setting(conn, _INTENT, intent); conn.commit()
        return
    launches = launch_saved(repo, conn, popen=popen, profiles=intent['profiles'], request_id=request_id)
    while True:
        conn.execute('BEGIN IMMEDIATE')
        current = db.get_setting(conn, _INTENT) or {}
        if current.get('id') != request_id or current.get('state') not in _PENDING:
            conn.rollback()
            return
        error = None
        if launches['failed']:
            error = 'Chrome could not open a saved profile. Check that Chrome is installed and try again.'
        elif _warning_signature(conn) != intent['warnings'] or any(not connectable(conn, p) for p in intent['profiles']):
            error = 'An account needs attention. Collection has not been restarted.'
        elif control.shared_collection_wait(conn, datetime.now(timezone.utc)) or db.get_setting(conn, 'instagram_request_attention'):
            error = 'Instagram is on a protective pause. Collection has not been restarted.'
        rows = [conn.execute('SELECT * FROM accounts WHERE lane_id=?', (p['lane_id'],)).fetchone() for p in intent['profiles']]
        ready = all(row and row['last_seen'] and accounts.utc(row['last_seen']).timestamp() >= intent['requested_at']
                    and clock() - accounts.utc(row['last_seen']).timestamp() < accounts.ONLINE_FOR.total_seconds()
                    and row['state'] != 'network_wait' for row in rows)
        if not error and clock() >= intent['deadline_at']:
            error = 'Saved accounts did not reconnect. Check their Instagram sign-in and try again.'
        if not error and ready:
            try:
                if current['command']['action'] != 'connect_accounts':
                    control.apply(conn, current['command'])
                    state, message = 'started', 'Accounts connected. Collection is enabled.'
                else:
                    state, message = 'connected', 'Your saved accounts are connected.'
            except (ValueError, LookupError) as exc:
                error = str(exc)
            else:
                if not conn.in_transaction:
                    conn.execute('BEGIN IMMEDIATE')
                current = db.get_setting(conn, _INTENT) or {}
                if current.get('id') == request_id and current.get('state') in _PENDING:
                    current.update(state=state, message=message)
                    db.set_setting(conn, _INTENT, current)
                conn.commit()
                return
        if error or clock() >= intent['deadline_at']:
            current.update(state='failed', message=error or 'Saved accounts did not reconnect. Check their Instagram sign-in and try again.')
            db.set_setting(conn, _INTENT, current); conn.commit()
            return
        current.update(state='waiting', message='Waiting for your accounts to connect…')
        db.set_setting(conn, _INTENT, current); conn.commit()
        sleep(1)


def schedule(repo, conn):
    intent = db.get_setting(conn, _INTENT) or {}
    if intent.get('state') not in _PENDING:
        return schedule_saved(repo, conn)
    repo = Path(repo).resolve()
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if sys.platform != 'darwin' or not path or Path(path).resolve() != (repo / 'data/leads.sqlite').resolve():
        return False
    if not _START_LOCK.acquire(blocking=False):
        return True
    def run():
        fresh = None
        try:
            fresh = db.connect(path)
            run_pending(repo, fresh)
        except Exception as exc:
            logging.warning('Chrome connection attempt failed: %s', exc)
            if fresh:
                fresh.rollback()
                current = db.get_setting(fresh, _INTENT) or {}
                if current.get('id') == intent.get('id') and current.get('state') in _PENDING:
                    current.update(state='failed', message='Could not finish connecting Chrome. Try again.')
                    db.set_setting(fresh, _INTENT, current); fresh.commit()
        finally:
            current = db.get_setting(fresh, _INTENT) if fresh else None
            follow_up = bool(current and current.get('id') != intent.get('id') and current.get('state') in _PENDING)
            if fresh:
                fresh.close()
            _START_LOCK.release()
            if follow_up:
                next_conn = db.connect(path)
                try:
                    schedule(repo, next_conn)
                finally:
                    next_conn.close()
    threading.Thread(target=run, name='connect-saved-accounts', daemon=True).start()
    return True
