"""Bounded response caching owned by one application, with shared cache misses.

SQLite snapshots remain owned by the calling thread. Neither connections nor
uncommitted results cross thread boundaries.
"""
from collections import OrderedDict
from concurrent.futures import Future, TimeoutError
from datetime import datetime
import json
import threading


class CacheBusy(Exception):
    """A matching read is still running; the client may retry shortly."""


class CacheStore:
    def __init__(self, max_entries=32, wait_timeout=5.0):
        if max_entries < 1 or wait_timeout <= 0:
            raise ValueError('Cache limits must be positive')
        self.max_entries = max_entries
        self.wait_timeout = wait_timeout
        self.values = OrderedDict()
        self.lock = threading.RLock()
        self.seed_links = [None]
        self._pending = {}
        self._generation = 0

    def clear(self):
        with self.lock:
            self._generation += 1
            self.values.clear()
            self.seed_links[0] = None
            # Existing waiters retain their Future; newer reads do fresh work.
            self._pending.clear()

    def cached(self, conn, name, query, compute):
        if conn.in_transaction:
            return compute()
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        # In-memory databases have no stable cross-connection identity. Bypass
        # caching rather than risk a recycled Python object ID returning old data.
        if not path:
            return compute()
        conn.execute('SAVEPOINT response_cache_read')
        try:
            row = conn.execute("SELECT value FROM settings WHERE key='lead_data_rev'").fetchone()
            revision = json.loads(row[0]) if row else 0
            key = (name, path, datetime.now().date().isoformat(),
                   tuple(sorted((k, tuple(v)) for k, v in query.items())), revision)
            return self._get_or_compute(key, compute)
        finally:
            conn.execute('RELEASE SAVEPOINT response_cache_read')

    def _get_or_compute(self, key, compute):
        with self.lock:
            if key in self.values:
                self.values.move_to_end(key)
                return self.values[key]
            pending = self._pending.get(key)
            leader = pending is None
            generation = self._generation
            if leader:
                # Bound bookkeeping even when callers request distinct queries.
                if len(self._pending) >= self.max_entries:
                    raise CacheBusy('The workspace is busy. Try again shortly.')
                pending = Future()
                self._pending[key] = pending
        if not leader:
            try:
                return pending.result(timeout=self.wait_timeout)
            except TimeoutError:
                raise CacheBusy('This view is still loading. Try again shortly.') from None
        try:
            value = compute()
        except BaseException as exc:
            with self.lock:
                if self._pending.get(key) is pending:
                    del self._pending[key]
                pending.set_exception(exc)
            raise
        with self.lock:
            if generation == self._generation:
                self.values[key] = value
                self.values.move_to_end(key)
                while len(self.values) > self.max_entries:
                    self.values.popitem(last=False)
            if self._pending.get(key) is pending:
                del self._pending[key]
            pending.set_result(value)
        return value
