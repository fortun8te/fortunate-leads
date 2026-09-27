"""Bounded local-inference telemetry. No prompts, notes or generated text."""
import math
import time

import db


def ensure(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS local_attempts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INTEGER NOT NULL,
        at REAL NOT NULL, duration REAL NOT NULL, status TEXT NOT NULL,
        input_tokens INTEGER, output_tokens INTEGER, repair INTEGER NOT NULL DEFAULT 0)''')
    conn.execute('CREATE INDEX IF NOT EXISTS local_attempts_at ON local_attempts(at)')


def active(conn, person_id=None):
    db.set_setting(conn, 'local_active_review', {'person_id': person_id, 'started_at': time.time()} if person_id else None)


def record(conn, person_id, result, duration, repair=False):
    metrics = result.get('runtime_metrics') or {}
    def tokens(key):
        value = metrics.get(key)
        return value if type(value) is int and 0 <= value <= 100000 else None
    elapsed = max(0, min(float(duration), 300))
    row = conn.execute('''INSERT INTO local_attempts(person_id,at,duration,status,input_tokens,output_tokens,repair)
        VALUES(?,?,?,?,?,?,?)''', (person_id, time.time(), elapsed, result.get('status', 'unverified'),
                                  tokens('prompt_tokens'), tokens('completion_tokens'), int(repair)))
    conn.execute('DELETE FROM local_attempts WHERE id<=?', (row.lastrowid - 10000,))
    active(conn)


def snapshot(conn, pending, paused=False, seeding=False, waiting=False, now=None):
    now = time.time() if now is None else now
    rows = conn.execute('SELECT at,duration,status,output_tokens FROM local_attempts WHERE at>=? ORDER BY at', (now-1800,)).fetchall()
    good = [r for r in rows if r['status'] in ('complete', 'needs_research')]
    # Include model rest and idle time. Only validated results count toward throughput.
    span = max(60, now - rows[0]['at'] + rows[0]['duration']) if rows else 60
    recent = bool(rows and now - rows[-1]['at'] < 120)
    rate = len(good) * 60 / span if recent else 0
    eta = math.ceil(pending / rate * 60) if len(good) >= 5 and rate and not (paused or seeding or waiting) else None
    running = db.get_setting(conn, 'local_active_review')
    if running and now - running.get('started_at', 0) < 90 and not paused:
        person = conn.execute('SELECT handle FROM people WHERE id=?', (running.get('person_id'),)).fetchone()
        running = dict(running, handle=person[0] if person else None)
    else:
        running = None
    return {'active': running, 'per_minute': round(rate, 2), 'eta_seconds': eta,
            'window_minutes': 30, 'attempts': len(rows), 'successful': len(good),
            'unverified': sum(r['status'] == 'unverified' for r in rows),
            'median_seconds': sorted(r['duration'] for r in rows)[len(rows)//2] if rows else None,
            'output_tokens': sum(r['output_tokens'] or 0 for r in rows),
            'eta_reason': 'paused' if paused else 'waiting_for_mac' if waiting else 'finding_work' if seeding else 'measuring' if eta is None and pending else None}
