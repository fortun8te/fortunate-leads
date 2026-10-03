"""Explicit ownership, admission and shutdown of application background work."""

import threading
import time
import traceback
from pathlib import Path

import db
import edge_benchmark_api
import engine_start
import laya
import local_model
import processing_modes
import resource_budget

from .common import WorkerDelay


class LocalServices:
    """Coalesce requested local-service changes outside request transactions."""

    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.requested = threading.Event()
        self.state = {'state': 'idle', 'error': None}
        self.stop = threading.Event()
        self.thread = None
        self.lifecycle_lock = threading.RLock()

    def schedule(self, conn):
        with self.lifecycle_lock:
            return self._schedule(conn)

    def _schedule(self, conn):
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        if (self.stop.is_set() or not self.config.host_operations_allowed or not path
                or Path(path).resolve() != Path(self.config.db).resolve()):
            return False
        self.requested.set()
        if not self.lock.acquire(blocking=False):
            return False
        previous = self.thread
        thread = threading.Thread(target=self._run, name='local-service-start', daemon=True)
        self.thread = thread
        try:
            thread.start()
        except BaseException as exc:
            self.thread = previous
            self.state.update(state='failed', error=str(exc))
            self.lock.release()
            raise
        return True

    def _run(self):
        try:
            while self.requested.is_set() and not self.stop.is_set():
                self.requested.clear()
                self.state.update(state='starting', error=None)
                try:
                    engine_start.start(self.config.root, 0)
                    laya.reset()
                    self.state.update(state='ready', error=None)
                except engine_start.EngineStartError as exc:
                    self.state.update(state='failed', error=str(exc))
        finally:
            self.lock.release()
            if self.requested.is_set() and not self.stop.is_set():
                conn = db.connect(self.config.db)
                try:
                    self.schedule(conn)
                finally:
                    conn.close()

    def step(self, conn, notes_pending):
        if not self.config.host_operations_allowed:
            return False
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        if not path or Path(path).resolve() != Path(self.config.db).resolve():
            return False
        mode = processing_modes.snapshot(conn)
        laya_wanted = processing_modes.begin_work(conn, 'laya') is not None
        if not laya_wanted and (laya.available() or laya.runtime_status().get('busy')):
            self.schedule(conn)
        paused = (mode.get('paused', False) or not mode['capabilities']['local_qualification']
                  or not mode['engines']['k2']['enabled'])
        local_model.maintain_service(paused=paused)
        if not paused and (local_model.is_remote() or resource_budget.state(startup=True)['allowed']):
            import processing_state
            has_work = bool(processing_state.next_pending(conn)) or notes_pending(conn)
            if has_work and not local_model.ready() and not self.lock.locked():
                self.schedule(conn)
        return False

    def close(self, timeout=5):
        with self.lifecycle_lock:
            self.stop.set()
            self.requested.clear()
            thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return thread is None or not thread.is_alive()


class WorkerSupervisor:
    """Each loop owns one SQLite connection and releases its transaction per step."""

    def __init__(self, connection_factory, benchmark_probe=None,
                 log_error=traceback.print_exc):
        self.connect = connection_factory
        self.benchmark_probe = benchmark_probe or (lambda conn: edge_benchmark_api.active(conn))
        self.log_error = log_error
        self.stop = threading.Event()
        self.threads = []
        self.lock = threading.RLock()
        self.active = set()
        self.closed = False

    def run(self, stop, step, busy_wait, idle_wait):
        conn = None
        try:
            while not stop.is_set() and not self.stop.is_set():
                busy = False
                token = None
                try:
                    if conn is None:
                        conn = self.connect()
                    with edge_benchmark_api.LOCK:
                        if not self.benchmark_probe(conn):
                            token = object()
                            with self.lock:
                                self.active.add(token)
                    if token is not None:
                        busy = step(conn)
                    # A forgotten commit cannot leak into the next iteration.
                    # Domain steps commit intentional writes themselves.
                    if conn.in_transaction:
                        conn.rollback()
                except Exception:
                    self.log_error()
                    busy = False
                    if conn is not None:
                        try:
                            conn.rollback()
                        except Exception:
                            self.log_error()
                            try:
                                conn.close()
                            except Exception:
                                pass
                            conn = None
                finally:
                    if token is not None:
                        with self.lock:
                            self.active.discard(token)
                delay = busy.seconds if isinstance(busy, WorkerDelay) else busy_wait if busy else idle_wait
                stop.wait(delay)
        finally:
            if conn is not None:
                conn.close()

    def start(self, name, step, busy_wait, idle_wait):
        with self.lock:
            if self.closed:
                raise RuntimeError('Background runtime is closed')
            thread = threading.Thread(target=self.run, args=(self.stop, step, busy_wait, idle_wait),
                                      name='fortunate-' + name, daemon=True)
            self.threads.append(thread)
            try:
                thread.start()
            except BaseException:
                if thread.ident is None:
                    self.threads.remove(thread)
                raise
        return thread

    def busy(self):
        with self.lock:
            return bool(self.active)

    def close(self, timeout=5):
        with self.lock:
            self.closed = True
            self.stop.set()
            threads = list(self.threads)
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        return [thread.name for thread in threads if thread.is_alive()]
