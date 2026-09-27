"""Private, bounded accounting for provider attempts. No request or response content is stored."""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / 'data' / 'llm_usage.sqlite'
LOCK = threading.Lock()
EVENT_DAYS = 90


def _connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    db = sqlite3.connect(path, timeout=5)
    db.execute('PRAGMA busy_timeout=5000')
    db.execute('''CREATE TABLE IF NOT EXISTS attempts (
        id INTEGER PRIMARY KEY, at TEXT NOT NULL, provider TEXT NOT NULL,
        model TEXT NOT NULL, purpose TEXT NOT NULL, batch_size INTEGER NOT NULL,
        success INTEGER NOT NULL, status TEXT NOT NULL, latency_ms INTEGER NOT NULL,
        input_tokens INTEGER, output_tokens INTEGER
    )''')
    db.execute('CREATE INDEX IF NOT EXISTS attempts_at ON attempts(at)')
    db.execute('''CREATE TABLE IF NOT EXISTS days (
        day TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
        purpose TEXT NOT NULL, success INTEGER NOT NULL, status TEXT NOT NULL,
        requests INTEGER NOT NULL, items INTEGER NOT NULL, latency_ms INTEGER NOT NULL,
        input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
        token_reports INTEGER NOT NULL, first_at TEXT NOT NULL, last_at TEXT NOT NULL,
        PRIMARY KEY(day, provider, model, purpose, success, status)
    )''')
    return db


def _int_or_none(value):
    return value if type(value) is int and 0 <= value <= 10**10 else None


def record(provider, model, purpose, batch_size, success, status, latency_ms,
           input_tokens=None, output_tokens=None, path=PATH):
    """Record one network attempt, including failures; token totals only use provider-reported values."""
    if provider != 'proxy' and (not isinstance(provider, str) or len(provider) != 10):
        raise ValueError('provider must be a nonsecret identifier')
    if purpose not in ('qualification', 'website_summary', 'provider_test'):
        raise ValueError('unknown usage purpose')
    if status not in ('ok', 'http_error', 'transport_error', 'invalid_reply'):
        raise ValueError('unknown usage status')
    batch_size = min(1000, max(1, int(batch_size)))
    latency_ms = min(3_600_000, max(0, int(latency_ms)))
    input_tokens, output_tokens = _int_or_none(input_tokens), _int_or_none(output_tokens)
    at = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    day = at[:10]
    with LOCK:
        db = _connect(path)
        try:
            db.execute('''INSERT INTO attempts(at,provider,model,purpose,batch_size,success,status,latency_ms,input_tokens,output_tokens)
                          VALUES(?,?,?,?,?,?,?,?,?,?)''',
                       (at, provider, model, purpose, batch_size, int(bool(success)), status,
                        latency_ms, input_tokens, output_tokens))
            db.execute('''INSERT INTO days VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(day,provider,model,purpose,success,status) DO UPDATE SET
                            requests=requests+1, items=items+excluded.items,
                            latency_ms=latency_ms+excluded.latency_ms,
                            input_tokens=input_tokens+excluded.input_tokens,
                            output_tokens=output_tokens+excluded.output_tokens,
                            token_reports=token_reports+excluded.token_reports,
                            last_at=excluded.last_at''',
                       (day, provider, model, purpose, int(bool(success)), status, 1, batch_size,
                        latency_ms, input_tokens or 0, output_tokens or 0,
                        int(input_tokens is not None or output_tokens is not None), at, at))
            # Daily aggregates preserve longer history while individual events stay bounded.
            db.execute('DELETE FROM attempts WHERE at < ?',
                       ((datetime.now(timezone.utc) - timedelta(days=EVENT_DAYS)).isoformat(),))
            db.commit()
        finally:
            db.close()
    os.chmod(path, 0o600)


def summary(path=PATH, days=30, purpose='qualification'):
    """Read-only totals; coverage starts with the first recorded attempt, never inferred from verdict rows."""
    if purpose not in ('qualification', 'website_summary', 'provider_test', 'all'):
        raise ValueError('unknown usage purpose')
    path = Path(path)
    if not path.exists():
        return {'recording_since': None, 'window_days': days, 'purpose': purpose,
                'requests': 0, 'successes': 0,
                'failed_attempts': 0, 'items_attempted': 0, 'input_tokens_reported': 0,
                'output_tokens_reported': 0, 'token_reports': 0, 'by_provider_model': []}
    since = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date().isoformat()
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
    try:
        where = '' if purpose == 'all' else ' AND purpose=?'
        params = (since,) if purpose == 'all' else (since, purpose)
        first = db.execute('SELECT min(first_at) FROM days' + ('' if purpose == 'all' else ' WHERE purpose=?'),
                           () if purpose == 'all' else (purpose,)).fetchone()[0]
        rows = db.execute('''SELECT provider,model,purpose,success,sum(requests),sum(items),
                                  sum(input_tokens),sum(output_tokens),sum(token_reports),sum(latency_ms)
                           FROM days WHERE day >= ?''' + where + ''' GROUP BY provider,model,purpose,success
                           ORDER BY sum(requests) DESC''', params).fetchall()
    finally:
        db.close()
    detail = [{'provider': p, 'model': m, 'purpose': purpose, 'success': bool(ok),
               'requests': n, 'items_attempted': items, 'input_tokens_reported': inp,
               'output_tokens_reported': out, 'token_reports': reports,
               'average_latency_ms': round(ms / n) if n else 0}
              for p, m, purpose, ok, n, items, inp, out, reports, ms in rows]
    return {'recording_since': first, 'window_days': days, 'purpose': purpose,
            'requests': sum(x['requests'] for x in detail),
            'successes': sum(x['requests'] for x in detail if x['success']),
            'failed_attempts': sum(x['requests'] for x in detail if not x['success']),
            'items_attempted': sum(x['items_attempted'] for x in detail),
            'input_tokens_reported': sum(x['input_tokens_reported'] for x in detail),
            'output_tokens_reported': sum(x['output_tokens_reported'] for x in detail),
            'token_reports': sum(x['token_reports'] for x in detail),
            'by_provider_model': detail}
