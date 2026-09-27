"""Cumulative processing modes and generation checks for asynchronous AI work.

Changing mode does not delete historical answers. Readers must use ``allows``
to decide which answers can influence the current ranking. Workers capture a
WorkTicket before inference and check it inside their final write transaction.
"""
from dataclasses import dataclass
import json

import db

MODES = ('R', 'RLAI', 'RLEAI')
STAGES = ('scraping', 'rules', 'laya', 'notes', 'local_qualification', 'external', 'deep_dive')
AI_STAGES = frozenset(('laya', 'notes', 'local_qualification', 'external', 'deep_dive'))
_KEYS = ('processing_mode', 'processing_generation', 'qualify', 'local_laya', 'scout', 'processing_paused')


def snapshot(conn):
    """Read one consistent, bounded settings snapshot without writing on GET."""
    values = {key: json.loads(value) for key, value in conn.execute(
        'SELECT key,value FROM settings WHERE key IN (?,?,?,?,?,?)', _KEYS)}
    explicit = values.get('processing_mode')
    mode = explicit if explicit in MODES else (
        'RLEAI' if values.get('qualify', False) else
        'RLAI' if values.get('local_laya', False) else 'R')
    generation = values.get('processing_generation', 0)
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        generation = 0
    local = mode != 'R'
    external = mode == 'RLEAI'
    return {'mode': mode, 'generation': generation, 'paused': bool(values.get('processing_paused', False)), 'capabilities': {
        'scraping': True, 'rules': True, 'laya': local, 'notes': local,
        'local_qualification': local, 'external': external,
        'deep_dive': external and bool(values.get('scout', False)),
    }}


def current_mode(conn):
    return snapshot(conn)['mode']


def allows(conn, stage):
    if stage not in STAGES:
        raise ValueError('Unknown processing stage: ' + str(stage))
    return snapshot(conn)['capabilities'][stage]


@dataclass(frozen=True)
class WorkTicket:
    stage: str
    mode: str
    generation: int


def begin_work(conn, stage):
    if stage not in STAGES:
        raise ValueError('Unknown processing stage: ' + str(stage))
    state = snapshot(conn)
    if not state['capabilities'][stage] or (stage in AI_STAGES and state['paused']):
        return None
    return WorkTicket(stage, state['mode'], state['generation'])


def result_current(conn, ticket):
    """Check under BEGIN IMMEDIATE, then save before releasing that transaction.

    This read alone does not acquire a writer lock. Checking outside the save
    transaction would allow a mode change between the check and the write.
    """
    if not isinstance(ticket, WorkTicket):
        return False
    state = snapshot(conn)
    return (state['mode'] == ticket.mode and state['generation'] == ticket.generation
            and state['capabilities'].get(ticket.stage, False)
            and not (ticket.stage in AI_STAGES and state['paused']))


def set_mode(conn, mode):
    """Set the mode atomically, leaving commit ownership with the caller.

    Legacy settings are mirrors for older callers. Explicit mode wins on reads.
    Choosing external AI never also opts into optional deep research.
    """
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError('mode must be R, RLAI or RLEAI')
    started = not conn.in_transaction
    if started:
        conn.execute('BEGIN IMMEDIATE')
    conn.execute('SAVEPOINT processing_mode_change')
    try:
        previous = snapshot(conn)
        migrating = conn.execute("SELECT 1 FROM settings WHERE key='processing_mode'").fetchone() is None
        generation = previous['generation'] + int(previous['mode'] != mode or migrating)
        for key, value in (
            ('processing_mode', mode), ('processing_generation', generation),
            ('local_laya', mode != 'R'), ('qualify', mode == 'RLEAI'),
            ('qualify_auto', False),
        ):
            db.set_setting(conn, key, value)
        if mode != 'RLEAI':
            db.set_setting(conn, 'scout', False)
        import processing_state
        transition = processing_state.invalidate_mode(conn, 'legacy' if migrating else previous['mode'], mode)
        conn.execute('RELEASE SAVEPOINT processing_mode_change')
    except Exception:
        conn.execute('ROLLBACK TO SAVEPOINT processing_mode_change')
        conn.execute('RELEASE SAVEPOINT processing_mode_change')
        if started:
            conn.rollback()
        raise
    return dict(snapshot(conn), transition=transition)


def set_paused(conn, paused):
    """Pause background AI without changing its mode, caches or scraping.

    Incrementing the shared generation invalidates in-flight responses, including
    a pause followed by a quick resume. Caller owns the final commit.
    """
    if not isinstance(paused, bool):
        raise ValueError('paused must be true or false')
    started = not conn.in_transaction
    if started:
        conn.execute('BEGIN IMMEDIATE')
    conn.execute('SAVEPOINT processing_pause_change')
    try:
        previous = snapshot(conn)
        if previous['paused'] != paused:
            db.set_setting(conn, 'processing_generation', previous['generation'] + 1)
            db.set_setting(conn, 'processing_paused', paused)
        conn.execute('RELEASE SAVEPOINT processing_pause_change')
    except Exception:
        conn.execute('ROLLBACK TO SAVEPOINT processing_pause_change')
        conn.execute('RELEASE SAVEPOINT processing_pause_change')
        if started:
            conn.rollback()
        raise
    return snapshot(conn)
