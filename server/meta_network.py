"""Shared outbound checks for optional Meta workers, without granting request permission."""
import sqlite3
from datetime import datetime, timezone

import control
import db

META_DOMAINS = ('instagram.com', 'instagr.am', 'facebook.com', 'fb.com', 'fb.me',
                'meta.com', 'threads.net', 'threads.com', 'messenger.com',
                'cdninstagram.com', 'fbcdn.net', 'fbsbx.com', 'facebook.net')


def is_meta_host(host):
    host = (host or '').lower().rstrip('.')
    return any(host == domain or host.endswith('.' + domain) for domain in META_DOMAINS)


def blocked(conn, now=None, stage=None):
    """Fail closed on unreadable settings; no worker bypasses a shared warning."""
    now = now or datetime.now(timezone.utc)
    try:
        if stage and control.stage_paused(conn, stage):
            return True
        if all(control.stage_paused(conn, s) for s in ('lists', 'bios')):
            return True
        value = db.get_setting(conn, 'cooldown')
        if value is None or value == '':
            return False
        until = control.utc(value) if isinstance(value, str) else None
        return until is None or until > now
    except (ValueError, TypeError, OverflowError, sqlite3.Error):
        return True
