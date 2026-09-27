"""Conservative shared budget for background local AI; never touches user apps."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time


class Deferred(RuntimeError):
    def __init__(self, message, retry_after=1):
        super().__init__(message)
        self.retry_after = max(1.0, float(retry_after))


def _probe():
    if sys.platform != 'darwin':
        return {'memory_free_percent': None, 'thermal_limited': None,
                'error': 'Local AI resource monitoring is unavailable on this system.'}
    try:
        memory = subprocess.check_output(['/usr/bin/memory_pressure', '-Q'], text=True,
                                         stderr=subprocess.DEVNULL, timeout=2)
        match = re.search(r'System-wide memory free percentage:\s*(\d+)%', memory)
        if not match:
            raise ValueError('Missing memory pressure reading')
        thermal = subprocess.check_output(['/usr/bin/pmset', '-g', 'therm'], text=True,
                                          stderr=subprocess.DEVNULL, timeout=2)
        limits = re.findall(r'CPU_(?:Speed_Limit|Scheduler_Limit)\s*=\s*(\d+)', thermal)
        limited = any(int(value) < 90 for value in limits)
        # Normal pmset output explicitly says no thermal warning; otherwise
        # require either explicit CPU limits or an explicit normal pressure.
        normal = 'No thermal warning level has been recorded' in thermal
        level = re.search(r'Thermal(?:PressureLevel|Level)\s*=\s*(\d+)', thermal)
        if level:
            limited = limited or int(level.group(1)) > 0
        if not (normal or limits or level):
            raise ValueError('Missing thermal pressure reading')
        return {'memory_free_percent': int(match.group(1)), 'thermal_limited': limited, 'error': None}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'memory_free_percent': None, 'thermal_limited': None,
                'error': 'Waiting for a reliable Mac resource reading.'}


class Governor:
    def __init__(self, probe=_probe, clock=time.monotonic, lock_path=None):
        self.probe, self.clock = probe, clock
        self.lock_path = lock_path
        self._mutex = threading.Lock()
        self._monitor = threading.RLock()
        self._sample = None
        self._sample_at = float('-inf')
        self._pressured = False
        self._recovery_at = None
        self._next_at = 0.0
        self._active = None
        self._uncertain = set()

    def set_uncertain(self, stage, uncertain):
        """Keep local capacity occupied after a request times out at the client."""
        with self._monitor:
            if uncertain:
                self._uncertain.add(stage)
            else:
                self._uncertain.discard(stage)

    def state(self, force=False, startup=False):
        with self._monitor:
            now = self.clock()
            if force or self._sample is None or now - self._sample_at >= 10:
                self._sample = self.probe()
                self._sample_at = now
                free = self._sample.get('memory_free_percent')
                unhealthy = (self._sample.get('error') or free is None or free < 25
                             or self._sample.get('thermal_limited') is not False)
                if unhealthy:
                    self._pressured = True
                    self._recovery_at = None
                elif self._pressured:
                    if free >= 35:
                        if self._recovery_at is None:
                            self._recovery_at = now
                        elif now - self._recovery_at >= 30:
                            self._pressured = False
                            self._recovery_at = None
                    else:
                        self._recovery_at = None
            sample = dict(self._sample)
            free = sample.get('memory_free_percent')
            reason = sample.get('error')
            if not reason and sample.get('thermal_limited'):
                reason = 'Local AI is waiting for your Mac to cool down.'
            if not reason and self._pressured:
                reason = 'Local AI is waiting for more memory and a stable recovery.'
            if not reason and startup and (free is None or free < 35):
                reason = 'Model startup needs more free memory. Your saved work is safe.'
            if not reason and self._uncertain:
                reason = 'Waiting for the local engine to confirm it has stopped.'
            if not reason and self._active:
                reason = 'Another local AI check is running.'
            if not reason and now < self._next_at:
                reason = 'Brief pause to keep your Mac responsive.'
            retry_after = (10 if self._pressured or sample.get('error') or sample.get('thermal_limited')
                           or (startup and (free is None or free < 35)) else
                           1 if self._active else max(1, self._next_at - now))
            sample.update(retry_after=retry_after, allowed=not bool(reason), reason=reason or '', busy=bool(self._active or self._uncertain),
                          activity_unknown=bool(self._uncertain), active_stage=self._active, retry_in=max(0, self._next_at - now),
                          recovering=self._pressured)
            return sample

    @contextmanager
    def lease(self, stage, startup=False):
        if not self._mutex.acquire(blocking=False):
            raise Deferred('Another local AI check is running.')
        lock = None
        started = None
        try:
            if self.lock_path:
                self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                lock = self.lock_path.open('a')
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise Deferred('Another local AI check is running.')
            state = self.state(startup=startup)
            if not state['allowed']:
                raise Deferred(state['reason'], state['retry_after'])
            # Cross-process rest window uses wall time, whereas local timing is
            # monotonic. Clamp stale/future values so clock changes cannot stall AI.
            if self.lock_path:
                try:
                    rest = json.loads(self.lock_path.with_suffix('.json').read_text()).get('rest_until', 0)
                    remaining = min(30, max(0, float(rest) - time.time()))
                except (OSError, ValueError, TypeError, AttributeError):
                    remaining = 0
                if remaining > 0:
                    self._next_at = self.clock() + remaining
                    raise Deferred('Brief pause to keep your Mac responsive.', remaining)
            started = self.clock()
            self._active = stage
            yield
        finally:
            if started is not None:
                # At most half duty cycle for typical short checks; capped rest
                # avoids multi-minute waits after a failed startup/transport call.
                rest = 0 if startup else max(2, min(30, self.clock() - started))
                self._next_at = self.clock() + rest
                self._active = None
                if self.lock_path:
                    path = self.lock_path.with_suffix('.json')
                    temp = path.with_suffix('.tmp')
                    try:
                        temp.write_text(json.dumps({'rest_until': time.time() + rest}))
                        temp.replace(path)
                    except OSError:
                        pass
            if lock:
                lock.close()
            self._mutex.release()


_governor = Governor(lock_path=Path.home() / 'Library/Application Support/Fortunate Leads/local-ai.lock')
state = _governor.state
lease = _governor.lease

set_uncertain = _governor.set_uncertain
