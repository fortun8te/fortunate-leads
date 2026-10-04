"""Disk-space guard: warn early, pause write-heavy work before the disk fills.

Free space is read from the volume that holds the database (one statvfs call, cached for a few
seconds). Three levels:

    ok        free >= warn        nothing to say
    low       free <  warn        a message is shown (GET /api/disk, doctor, log); nothing pauses
    critical  free <  pause       background write-heavy work and new collection leases pause

Defaults: warn at 10 GB, pause at 3 GB, resume at 4 GB (the gap keeps it from flapping when free
space hovers at the line). Override with FL_DISK_WARN_GB / FL_DISK_PAUSE_GB (decimal GB). The pause
line is never below 1.5 times the database plus its WAL, which is what a checkpoint or a
VACUUM INTO needs. Saving a lead, tagging or reading never pauses; neither does list or profile data
the extension already holds (the extension keeps and retries it).

Pausing is a hold, not an error: work resumes by itself once free space is back above the resume line.
"""
import os
from pathlib import Path
import shutil
import sys
import threading
import time

GB = 1000 ** 3
DEFAULT_WARN_GB = 10.0
DEFAULT_PAUSE_GB = 3.0
RESUME_MARGIN = 1.0 / 3      # resume once free >= pause * (1 + margin)
CACHE_SECONDS = 5.0
LEASE_WAIT_MS = 60000

# Background loops (see backend/app.py start_workers) that write a lot or grow the disk.
# 'repair' (small bookkeeping) and 'local-services' (starts the model) keep running.
WRITE_HEAVY = frozenset({'rules', 'processing-maintenance', 'external-ai', 'laya', 'local-processing',
                         'profile-planner', 'photos', 'biofetch', 'research', 'map-layout'})


def human(n):
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1000 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1000


def _env_gb(name, default):
    try:
        value = float(os.environ.get(name, ''))
    except ValueError:
        return default
    return value if value > 0 else default


class DiskGuard:
    def __init__(self, database, *, warn_bytes=None, pause_bytes=None, usage=None, ttl=CACHE_SECONDS,
                 clock=time.monotonic, log=None):
        self.database = Path(database)
        self.warn_bytes = int(warn_bytes if warn_bytes is not None else _env_gb('FL_DISK_WARN_GB', DEFAULT_WARN_GB) * GB)
        self.pause_bytes = int(pause_bytes if pause_bytes is not None else _env_gb('FL_DISK_PAUSE_GB', DEFAULT_PAUSE_GB) * GB)
        if self.pause_bytes >= self.warn_bytes:
            self.pause_bytes = self.warn_bytes // 2
        self._usage = usage or self._disk_usage
        self.ttl = ttl
        self._clock = clock
        self._log = log if log is not None else (lambda text: print(text, file=sys.stderr, flush=True))
        self._lock = threading.Lock()
        self._cached = None
        self._at = -1e9
        self._latched = False
        self._last_level = 'ok'
        self.skipped = {}

    def _disk_usage(self):
        path = self.database.parent
        while not path.exists() and path != path.parent:
            path = path.parent
        usage = shutil.disk_usage(path)
        return usage.total, usage.free

    def _database_bytes(self):
        total = 0
        for suffix in ('', '-wal'):
            try:
                total += os.stat(str(self.database) + suffix).st_size
            except OSError:
                pass
        return total

    def thresholds(self, database_bytes=None):
        database_bytes = self._database_bytes() if database_bytes is None else database_bytes
        pause = max(self.pause_bytes, int(database_bytes * 1.5))
        warn = max(self.warn_bytes, pause * 2 + 1)
        resume = int(pause * (1 + RESUME_MARGIN))
        return warn, pause, resume

    def state(self, *, force=False):
        """Current level and numbers. Cached for `ttl` seconds; `force` re-reads the disk."""
        with self._lock:
            now = self._clock()
            if not force and self._cached is not None and now - self._at < self.ttl:
                return dict(self._cached)
            try:
                total, free = self._usage()
            except OSError as exc:
                # Cannot tell: say so, and never pause work on a guess.
                self._cached = {'level': 'unknown', 'paused': False, 'message': None, 'error': str(exc)}
                self._at = now
                return dict(self._cached)
            database_bytes = self._database_bytes()
            warn, pause, resume = self.thresholds(database_bytes)
            if free < pause:
                level = 'critical'
                self._latched = True
            elif self._latched and free < resume:
                level = 'critical'
            else:
                self._latched = False
                level = 'low' if free < warn else 'ok'
            paused = level == 'critical'
            message = None
            if level == 'low':
                message = (f'Disk space is low: {human(free)} free. Collection and background work pause '
                           f'below {human(pause)}.')
            elif paused:
                message = (f'Disk is almost full: {human(free)} free. Collection and background work are paused '
                           f'until {human(resume)} is free. Free up space to resume.')
            self._cached = {'level': level, 'paused': paused, 'message': message, 'free_bytes': free,
                            'total_bytes': total, 'used_percent': round(100.0 * (total - free) / total, 1) if total else None,
                            'warn_bytes': warn, 'pause_bytes': pause, 'resume_bytes': resume,
                            'database_bytes': database_bytes, 'skipped': dict(self.skipped)}
            self._at = now
            if level != self._last_level:
                previous, self._last_level = self._last_level, level
                if level != 'ok':
                    self._log(f'disk: {message}')
                elif previous != 'ok':
                    self._log(f'disk: space recovered, {human(free)} free; paused work has resumed')
            return dict(self._cached)

    def level(self):
        return self.state()['level']

    def paused(self):
        return bool(self.state().get('paused'))

    def message(self):
        return self.state().get('message')

    def note_skip(self, name):
        with self._lock:
            self.skipped[name] = self.skipped.get(name, 0) + 1

    def require(self, needed_bytes):
        """For a big one-off write (backup, vacuum into, restore): enough room plus the pause line?"""
        state = self.state(force=True)
        if state['level'] == 'unknown':
            return True
        return state['free_bytes'] - needed_bytes >= state['pause_bytes']


_REGISTRY = {}
_REGISTRY_LOCK = threading.Lock()


def for_database(path):
    """One shared guard per database, so every caller sees the same cache and the same latch."""
    key = str(Path(path).resolve())
    with _REGISTRY_LOCK:
        if key not in _REGISTRY:
            _REGISTRY[key] = DiskGuard(key)
        return _REGISTRY[key]


def reset():
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


def guarded(name, step, database):
    """Wrap a background step: skipped (idle) while the disk is critical, unchanged otherwise."""
    if name not in WRITE_HEAVY:
        return step

    def run(conn):
        guard = for_database(database)
        if guard.paused():
            guard.note_skip(name)
            return False
        return step(conn)
    run.__name__ = getattr(step, '__name__', name)
    run.__wrapped__ = step
    return run


def lease_hold(database):
    """The reply a collector gets while the disk is critical, or None when it may take work."""
    guard = for_database(database)
    if not guard.paused():
        return None
    return {'granted': False, 'job': None, 'paused': True, 'stages': {'list': False, 'profile': False},
            'wait_ms': LEASE_WAIT_MS, 'reason': 'disk_low', 'message': guard.message()}


def routes(database):
    """GET /api/disk. `database` is a callable returning the current database path."""
    def disk(conn, query, body):
        return for_database(database()).state(force=True)
    return [('GET', '/api/disk', disk)]
