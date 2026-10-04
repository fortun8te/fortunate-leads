"""SQLite connection lifecycle and caller-selected resource budgets.

Factories own configuration, never shared connection objects. Each request or
worker receives its own connection; transaction contexts close it on every exit.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import math
import sqlite3


def connect(path, *, timeout=15.0, cache_kib=32768, mmap_bytes=268435456):
    """Open a configured connection, preserving the legacy background defaults.

    WAL persists on the database file. Read its mode first so opening an already
    initialized database does not repeat a journal-mode transition on hot paths.
    Cache and mmap limits are per connection, independent of schema migrations.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError('timeout must be a finite nonnegative number of seconds')
    if type(cache_kib) is not int or cache_kib <= 0:
        raise ValueError('cache_kib must be a positive integer')
    if type(mmap_bytes) is not int or mmap_bytes < 0:
        raise ValueError('mmap_bytes must be a nonnegative integer')
    conn = sqlite3.connect(path, timeout=timeout)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f'PRAGMA busy_timeout={int(timeout * 1000)}')
        mode = conn.execute('PRAGMA journal_mode').fetchone()[0].lower()
        if mode not in ('wal', 'memory'):
            conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute(f'PRAGMA mmap_size={mmap_bytes}')
        # Large filtered sorts and facets spill to disk rather than grow RAM.
        conn.execute('PRAGMA temp_store=FILE')
        conn.execute(f'PRAGMA cache_size=-{cache_kib}')
        return conn
    except BaseException:
        conn.close()
        raise


@contextmanager
def connection(path, **options):
    """Yield a connection and close it, rolling back any unfinished writes."""
    conn = connect(path, **options)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(path, **options):
    """Commit successful work; roll back failed work; always close the connection."""
    conn = connect(path, **options)
    try:
        conn.execute('BEGIN')
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@dataclass(frozen=True)
class ConnectionFactory:
    """Immutable configuration for connections opened by an HTTP request or worker."""

    path: str
    timeout: float = 15.0
    cache_kib: int = 32768
    mmap_bytes: int = 268435456

    def __call__(self):
        return connect(self.path, timeout=self.timeout, cache_kib=self.cache_kib,
                       mmap_bytes=self.mmap_bytes)

    def connection(self):
        return connection(self.path, timeout=self.timeout, cache_kib=self.cache_kib,
                          mmap_bytes=self.mmap_bytes)

    def transaction(self):
        return transaction(self.path, timeout=self.timeout, cache_kib=self.cache_kib,
                           mmap_bytes=self.mmap_bytes)
