"""Opt-in reopening of saved Chrome profiles on startup or collection resume.

Chrome loads the persistent profile without a foreground window. The extension
owns Instagram tab creation, authentication checks and all request pacing.
"""
import json
import logging
import re
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


def wanted(conn, profile):
    if all(control.stage_paused(conn, stage) for stage in ('lists', 'bios')):
        return False
    row = conn.execute('SELECT * FROM accounts WHERE lane_id=?', (profile['lane_id'],)).fetchone()
    if not row or row['paused'] or str(row['ig_id']) != profile['ig_id'] or row['collection_backend'] != 'chrome':
        return False
    stages = ('lists', 'bios') if row['role'] == 'both' else (row['role'],)
    return any(not control.stage_paused(conn, stage) for stage in stages)


def launch_saved(repo, conn, *, popen=subprocess.Popen, now=None):
    """One bounded attempt, never a watchdog. Does not change collection intent."""
    profiles = read_config(repo)
    result = {'requested': 0, 'skipped': 0}
    timestamp = (now or datetime.now(timezone.utc)).timestamp()
    for profile in profiles:
        # A second startup process must not dispatch the same profile twice.
        conn.execute('BEGIN IMMEDIATE')
        try:
            if not wanted(conn, profile):
                result['skipped'] += 1
                continue
            key = 'browser_startup:' + profile['lane_id']
            previous = db.get_setting(conn, key) or {}
            tried = previous.get('requested_at', 0) if isinstance(previous, dict) else 0
            if isinstance(tried, (float, int)) and 0 <= timestamp - tried < 60:
                result['skipped'] += 1
                continue
            db.set_setting(conn, key, {'requested_at': timestamp})
        finally:
            conn.commit()
        # Direct executable dispatch preserves --profile-directory when Chrome
        # is already running. No URL means no new foreground tab or login bypass.
        process = popen([str(CHROME), '--profile-directory=' + profile['directory'], '--no-startup-window'],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True)
        # Reap the short dispatch process without waiting for a cold Chrome to exit.
        threading.Thread(target=process.wait, daemon=True).start()
        result['requested'] += 1
    return result


def schedule(repo, conn):
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
