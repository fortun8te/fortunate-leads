"""Small runtime ledger for engine work, independent of persisted intent."""
from contextlib import contextmanager
import threading

import processing_modes

lock = threading.RLock()
_active = {'k2': 0, 'laya': 0}


@contextmanager
def work(conn, ticket):
    """Admit work and acknowledge its completion under the control lock.

    A stopping engine stays active until the caller unwinds, including errors.
    Callers still validate their ticket inside their final write transaction.
    """
    engine = processing_modes.stage_engine(ticket.stage) if ticket else None
    admitted = False
    with lock:
        if engine and processing_modes.result_current(conn, ticket):
            _active[engine] += 1
            admitted = True
    try:
        yield admitted
    finally:
        if admitted:
            with lock:
                _active[engine] -= 1


def snapshot(processing, runtimes=None, starting=False):
    runtimes = runtimes or {}
    result = {}
    with lock:
        for engine, stage in (('k2', 'local_qualification'), ('laya', 'laya')):
            runtime = runtimes.get(engine, {})
            enabled = processing['engines'][engine]['enabled']
            allowed = enabled and processing['capabilities'][stage]
            wanted = allowed and not processing['paused']
            active = bool(_active[engine] or runtime.get('busy'))
            ready = bool(runtime.get('ready'))
            loaded = ready and runtime.get('managed_local', True)
            uncertain = bool(runtime.get('activity_unknown'))
            active = active or uncertain
            resources = runtime.get('resources', {})
            waiting = resources.get('reason') or runtime.get('error') or ''
            # An enabled idle model is waiting for work. Running means an actual
            # request is still outstanding, never just that a queue is nonempty.
            state = ('stopping' if active and not wanted else 'running' if active else
                     'stopping' if not wanted and (starting or loaded) else 'off' if not wanted else
                     'starting' if starting and not ready else 'waiting')
            reason = (runtime.get('error') or 'Waiting for the engine to confirm it has stopped.' if uncertain else
                      'Finishing the current request.' if state == 'stopping' and active else
                      'Unloading the local engine.' if state == 'stopping' else
                      'AI is paused.' if processing['paused'] else
                      'Choose RLAI or RLEAI to use this engine.' if not processing['capabilities'][stage] else
                      'Engine disabled.' if not enabled else
                      'Processing a request.' if active else
                      'Starting engine.' if state == 'starting' else
                      waiting or ('Waiting for work.' if ready else 'Waiting for the engine to become ready.'))
            result[engine] = dict(enabled=enabled, allowed=allowed, state=state,
                active=active, ready=ready, reason=reason,
                activity_unknown=uncertain,
                stop_acknowledged=not wanted and not active and not starting and not loaded)
    return {'processing': processing, 'paused': processing['paused'], 'engines': result,
            'stop_acknowledged': all(e['stop_acknowledged'] for e in result.values())}


def has_active(engine):
    with lock:
        return bool(_active[engine])
