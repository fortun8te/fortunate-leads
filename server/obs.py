"""Agent-first observability for the local server: structured events and crash forensics.

Stdlib only. Read docs/LOGGING.md; to see what happened run ops/diagnose.

What it keeps, under <data>/ (the folder holding leads.sqlite):
  logs/events.jsonl (+ .1 .. .4)  one JSON event per line, size-rotated
  logs/run.json                   pid + last-clean-exit marker, rewritten every heartbeat
  crash/<ts>.json                 last events + all thread stacks, written on a fatal error
  crash/faulthandler.log          native crashes and `kill -USR1 <pid>` thread dumps

Rules this module keeps:
  1. A caller pays one dict build and one deque append. Nothing blocks, nothing raises.
     A background thread writes; when it falls behind, new events are dropped and counted.
  2. Redaction happens at the boundary: secrets never reach the queue, the ring or the disk.
  3. Nothing is active until start() is called (server.main does it). Tests and CLIs are untouched.
"""
import atexit
import collections
import faulthandler
import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
import traceback

try:
    import resource
except ImportError:  # pragma: no cover - not on this Mac
    resource = None

LEVELS = {'debug': 10, 'info': 20, 'warn': 30, 'error': 40}
MAX_STR = 300
QUEUE_MAX = 8192
RING_MAX = 400
HIST_EDGES = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)   # ms upper edges; one more bucket above
_STOP = object()


# ---------------------------------------------------------------- redaction
# Keys whose value never leaves the process: credentials, cookies, and anything a person typed
# (notes, DMs, bios) or Instagram sent back (bodies, samples).
_DENY = re.compile(
    r'cookie|session|csrf|secret|passw|api[_-]?key|authoriz|bearer|credential|(?:^|_)token(?!s)|ds_user|ig_did|'
    r'x-ig|www-claim|^(?:note|notes|dm|dms|bio|text|body|html|content|prompt|caption|sample|reply|response|'
    r'headers?|message_text|email)$|(?:^|_)(?:note|dm)s?(?:_|$)', re.I)
_ALLOW = frozenset({'tokens_in', 'tokens_out', 'input_tokens', 'output_tokens'})
_key_cache = {}

_SUSPECT = re.compile(r'[=:?]|bearer|sk-|[A-Za-z0-9_-]{40}', re.I)
_KV = re.compile(
    r'(?i)\b(sessionid|csrftoken|ds_user_id|ig_did|ig_nrcb|datr|shbid|shbts|rur|mid|x-csrftoken|x-ig-www-claim|'
    r'authorization|set-cookie|cookie|password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|token)'
    r'(["\']?\s*[:=]\s*["\']?)(?:bearer\s+)?[^\s"\';,&]{3,}')
_BEARER = re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}')
_SK = re.compile(r'\bsk-[A-Za-z0-9_-]{6,}')
_URL_Q = re.compile(r'((?:https?:/)?/[^\s?#"\']+)\?[^\s"\']*')
_OPAQUE = re.compile(r'(?<![\w/.-])[A-Za-z0-9_-]{40,}(?![\w/-])')


def _denied(key):
    hit = _key_cache.get(key)
    if hit is None:
        hit = key not in _ALLOW and bool(_DENY.search(key))
        if len(_key_cache) < 4096:
            _key_cache[key] = hit
    return hit


def scrub(text):
    """Remove credentials from a string and bound its length."""
    if len(text) > 4 * MAX_STR:
        text = text[:4 * MAX_STR]
    if _SUSPECT.search(text):
        text = _KV.sub(lambda m: m.group(1) + '=[redacted]', text)
        text = _BEARER.sub('Bearer [redacted]', text)
        text = _SK.sub('[redacted]', text)
        text = _URL_Q.sub(r'\1?[query]', text)
        text = _OPAQUE.sub(lambda m: '[redacted:%dch]' % len(m.group(0)), text)
    if len(text) > MAX_STR:
        text = text[:MAX_STR] + '...(+%d)' % (len(text) - MAX_STR)
    return text


def clean(value, depth=0):
    """A JSON-safe, bounded, credential-free copy of a field value."""
    kind = type(value)
    if kind is str:
        return scrub(value)
    if value is None or kind is bool or kind is int:
        return value
    if kind is float:
        return round(value, 3) if value == value and value not in (float('inf'), float('-inf')) else None
    if depth < 2:
        if kind is dict:
            return {str(k)[:40]: ('[redacted]' if _denied(str(k)) else clean(v, depth + 1))
                    for k, v in list(value.items())[:24]}
        if kind in (list, tuple, set, frozenset):
            return [clean(v, depth + 1) for v in list(value)[:24]]
    try:
        return scrub(str(value))
    except Exception:
        return '[unprintable]'


def _iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t)) + '.%03dZ' % int((t % 1) * 1000)


# ---------------------------------------------------------------- state
class _State:
    def __init__(self):
        self.on = False
        self.min_level = LEVELS['info']
        self.dir = self.crash_dir = None
        self.q = collections.deque()
        self.ring = collections.deque(maxlen=RING_MAX)
        self.dropped = self.internal_errors = 0
        self.n_by_level = {'error': 0, 'warn': 0}
        self.writer = self.beat = None
        self.stop_evt = threading.Event()
        self.run_id = self.pid = None
        self.started = self.started_mono = 0.0
        self.exit_reason = None
        self.crashed = False
        self.crash_dump = None
        self.meta = {}
        self.fault_file = None
        self.loops = {}
        self.inflight = {}
        self.gauges = {}
        self.aggs = {}
        self.trans = {}
        self.throttles = {}
        self.lock = threading.Lock()
        self.saved = {}
        self.tick_s, self.hb_s, self.hang_s, self.stuck_s = 5.0, 60.0, 300.0, 30.0


S = _State()
_ctx = threading.local()
_ROUTE_COMPONENTS = frozenset(('db', 'state'))


def active():
    return S.on


# ---------------------------------------------------------------- the one hot function
def event(component, name, level='info', /, **fields):
    """Record one event. Never blocks, never raises; a no-op until start()."""
    if S.on:
        _emit(None, component, name, level, fields)


def event_at(when, component, name, level='info', /, **fields):
    """event() for something that happened earlier elsewhere (the extension's own clock)."""
    if S.on:
        _emit(when, component, name, level, fields)


def _emit(when, component, name, level, fields):
    try:
        lv = LEVELS.get(level, 20)
        if lv < S.min_level:
            return
        rec = {'t': when or time.time(), 'level': level, 'component': component, 'event': name}
        ctx = _ctx.__dict__
        if ctx:
            for key in ('request_id', 'lane', 'account', 'loop'):
                value = ctx.get(key)
                if value is not None and key not in fields:
                    rec[key] = value
            if component in _ROUTE_COMPONENTS and ctx.get('route') and 'route' not in fields:
                rec['route'] = ctx['route']
        for key, value in fields.items():
            if value is None:
                continue
            rec[key] = '[redacted]' if _denied(key) else clean(value)
        if lv >= 30:
            S.n_by_level['error' if lv >= 40 else 'warn'] += 1
        S.ring.append(rec)
        if len(S.q) >= QUEUE_MAX:
            S.dropped += 1
        else:
            S.q.append(rec)
    except Exception:
        S.internal_errors += 1


def debug(component, name, /, **fields):
    event(component, name, 'debug', **fields)


def info(component, name, /, **fields):
    event(component, name, 'info', **fields)


def warn(component, name, /, **fields):
    event(component, name, 'warn', **fields)


def error(component, name, /, **fields):
    event(component, name, 'error', **fields)


def bind(**fields):
    """Attach request_id/lane/account/route to every event this thread emits until unbind()."""
    ctx = _ctx.__dict__
    for key, value in fields.items():
        if value is not None:
            ctx[key] = value


def unbind(*keys):
    ctx = _ctx.__dict__
    if keys:
        for key in keys:
            ctx.pop(key, None)
    else:
        ctx.clear()


class span:
    """with span('app', 'initialize'): ...  -> one event with dur_ms (error level if it raises)."""

    def __init__(self, component, name, **fields):
        self.component, self.name, self.fields = component, name, fields

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        dur = round((time.perf_counter() - self.t0) * 1000, 1)
        if exc_type is None:
            event(self.component, self.name, dur_ms=dur, **self.fields)
        else:
            event(self.component, self.name, 'error', dur_ms=dur, exc_type=exc_type.__name__,
                  error=str(exc), **self.fields)
        return False


# ---------------------------------------------------------------- volume control
def throttle(key, seconds):
    """None if `key` fired less than `seconds` ago, else how many calls were suppressed since the last pass."""
    now = time.monotonic()
    with S.lock:
        row = S.throttles.get(key)
        if row is None:
            if len(S.throttles) > 2048:
                S.throttles.clear()
            S.throttles[key] = [now, 0]
            return 0
        if now - row[0] < seconds:
            row[1] += 1
            return None
        suppressed, row[0], row[1] = row[1], now, 0
        return suppressed


def transition(key, signature, component, name, /, level='info', refresh_s=600, **fields):
    """Emit only when `signature` changes (or every refresh_s while it holds). Returns True when it emitted.

    The event describes the NEW state and carries prev_repeats/prev_held_s for the state it replaced, so a
    cooldown that was refused 240 times shows as one line, not 240.
    """
    now = time.monotonic()
    with S.lock:
        row = S.trans.get(key)
        if row is None:
            if len(S.trans) > 512:
                S.trans.clear()
            S.trans[key] = row = {'sig': signature, 'n': 0, 'since': now, 'emit': now}
            extra = {'first': True}
        elif row['sig'] != signature:
            extra = {'prev_repeats': row['n'], 'prev_held_s': round(now - row['since'], 1)}
            row.update(sig=signature, n=0, since=now, emit=now)
        else:
            row['n'] += 1
            if now - row['emit'] < refresh_s:
                return False
            extra = {'still': True, 'repeats': row['n'], 'held_s': round(now - row['since'], 1)}
            row['emit'] = now
    event(component, name, level, **fields, **extra)
    return True


def transition_end(key):
    """Forget a transition state; returns how long and how often it held (None if it was not active)."""
    now = time.monotonic()
    with S.lock:
        row = S.trans.pop(key, None)
    if row is None:
        return None
    return {'repeats': row['n'] + 1, 'held_s': round(now - row['since'], 1)}


class _Agg:
    __slots__ = ('n', 'sum', 'max', 'hist', 'bytes', 'labels', 'every', 'due', 'tags')

    def __init__(self, every, tags):
        self.n, self.sum, self.max, self.bytes = 0, 0.0, 0.0, 0
        self.hist = [0] * (len(HIST_EDGES) + 1)
        self.labels = {}
        self.every = every
        self.due = time.monotonic() + every
        self.tags = tags


def _bucket(ms):
    for index, edge in enumerate(HIST_EDGES):
        if ms < edge:
            return index
    return len(HIST_EDGES)


def agg(component, name, key, ms=0.0, nbytes=0, label=None, every=60, **tags):
    """Fold a chatty event into a per-key summary emitted every `every` seconds (n, sum, max, histogram)."""
    if not S.on:
        return
    try:
        with S.lock:
            row = S.aggs.get((component, name, key))
            if row is None:
                if len(S.aggs) >= 256:
                    key = '(other)'
                    row = S.aggs.get((component, name, key))
                if row is None:
                    row = S.aggs[(component, name, key)] = _Agg(every, tags)
            row.n += 1
            row.sum += ms
            if ms > row.max:
                row.max = ms
            row.hist[_bucket(ms)] += 1
            row.bytes += nbytes
            if label is not None:
                row.labels[label] = row.labels.get(label, 0) + 1
    except Exception:
        S.internal_errors += 1


def quantile(hist, q, top):
    """Approximate quantile from the fixed histogram; the last bucket reports the observed maximum."""
    total = sum(hist)
    if not total:
        return 0
    want, seen = total * q, 0
    for index, count in enumerate(hist):
        seen += count
        if seen >= want:
            return min(HIST_EDGES[index], top) if index < len(HIST_EDGES) else top
    return top


def flush_aggs(force=False):
    now = time.monotonic()
    done = []
    with S.lock:
        for key, row in list(S.aggs.items()):
            if force or now >= row.due:
                done.append((key, row))
                del S.aggs[key]
    for (component, name, key), row in done:
        event(component, name, 'info', agg=True, key=key, n=row.n, sum_ms=round(row.sum, 1), max_ms=round(row.max, 1),
              p95_ms=quantile(row.hist, 0.95, round(row.max, 1)), hist=row.hist, bytes=row.bytes or None,
              labels=row.labels or None, **row.tags)


def gauge(name, fn):
    """Register a zero-argument callable returning a small dict, included in every heartbeat."""
    S.gauges[name] = fn


# ---------------------------------------------------------------- writer
class _Marker:
    def __init__(self):
        self.done = threading.Event()


class _Writer(threading.Thread):
    def __init__(self, path, max_bytes, files, poll):
        super().__init__(name='fortunate-obs-writer', daemon=True)
        self.path, self.max_bytes, self.files, self.poll = path, max_bytes, files, poll
        self.fd = None
        self.size = 0
        self.errors = 0
        self.reported_drops = 0
        self.open_failed_at = None
        self.rotations = 0

    def run(self):
        while True:
            try:
                stop = self.drain()
            except Exception:
                self.errors += 1
                stop = False
            if stop:
                break
            time.sleep(self.poll)
        self.close()

    def drain(self):
        lines, markers, stop = [], [], False
        q = S.q
        for _ in range(2000):
            try:
                item = q.popleft()
            except IndexError:
                break
            if item is _STOP:
                stop = True
                break
            if type(item) is _Marker:
                markers.append(item)
                break
            lines.append(self.render(item))
        dropped = S.dropped
        if dropped > self.reported_drops:
            lines.append(self.render({'t': time.time(), 'level': 'warn', 'component': 'obs', 'event': 'dropped',
                                      'n': dropped - self.reported_drops, 'total': dropped}))
            self.reported_drops = dropped
        if lines:
            self.write(lines)
        for marker in markers:
            marker.done.set()
        if stop:
            return True
        return False

    @staticmethod
    def render(rec):
        out = {'ts': _iso(rec['t']), 'level': rec['level'], 'component': rec['component'], 'event': rec['event']}
        for key, value in rec.items():
            if key not in ('t', 'level', 'component', 'event'):
                out[key] = value
        try:
            return json.dumps(out, separators=(',', ':'), default=str) + '\n'
        except Exception:
            return json.dumps({k: out[k] for k in ('ts', 'level', 'component', 'event')} | {'render_error': True}) + '\n'

    def open(self):
        if self.fd is not None:
            return True
        if self.open_failed_at is not None and time.monotonic() - self.open_failed_at < 5:
            return False
        try:
            self.fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            self.size = os.fstat(self.fd).st_size
            return True
        except OSError:
            self.open_failed_at = time.monotonic()
            self.errors += 1
            return False

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def rotate(self):
        self.close()
        try:
            for index in range(self.files - 1, 0, -1):
                source = self.path if index == 1 else '%s.%d' % (self.path, index - 1)
                if os.path.exists(source):
                    os.replace(source, '%s.%d' % (self.path, index))
            self.rotations += 1
        except OSError:
            self.errors += 1

    def write(self, lines):
        buf, size = [], 0
        for line in lines:
            data = line.encode('utf-8', 'replace')
            if not self.open():
                return
            if self.size + size + len(data) > self.max_bytes and (self.size + size) > 0:
                self.flush_buf(buf)
                buf, size = [], 0
                self.rotate()
                if not self.open():
                    return
            buf.append(data)
            size += len(data)
        self.flush_buf(buf)

    def flush_buf(self, buf):
        if not buf or self.fd is None:
            return
        data = b''.join(buf)
        try:
            view = memoryview(data)
            while view:
                written = os.write(self.fd, view)
                view = view[written:]
            self.size += len(data)
        except OSError:
            self.errors += 1
            self.close()


def flush(timeout=2.0):
    """Wait until everything queued so far is on disk. For shutdown and crash paths, never for requests."""
    if not S.on or S.writer is None or not S.writer.is_alive():
        return False
    marker = _Marker()
    S.q.append(marker)
    return marker.done.wait(timeout)


# ---------------------------------------------------------------- exceptions
def _frames(tb, limit=10):
    return ['%s:%d %s' % (os.path.basename(f.filename), f.lineno, f.name) for f in traceback.extract_tb(tb)[-limit:]]


def signature(exc_type, exc, tb):
    """Stable group key for an error: type, innermost function, normalised message (no line numbers)."""
    frames = traceback.extract_tb(tb)
    where = ''
    if frames:
        last = frames[-1]
        where = '@%s:%s' % (os.path.splitext(os.path.basename(last.filename))[0], last.name)
    message = re.sub(r'\d+', 'N', scrub(str(exc)))[:48]
    return '%s%s|%s' % (exc_type.__name__, where, message)


def log_exception(component='app', name='exception', **fields):
    """Call inside `except`: prints the traceback as before (stderr) and records one structured error event."""
    try:
        traceback.print_exc()
    except Exception:
        pass
    if not S.on:
        return
    exc_type, exc, tb = sys.exc_info()
    if exc_type is None:
        return
    try:
        sig = signature(exc_type, exc, tb)
        # A loop failing every 100 ms must not push the rest of the history out of the rotation: one line per
        # signature per 10 s, carrying how many it stood for.
        suppressed = throttle(('exc', component, sig), 10)
        if suppressed is None:
            return
        error(component, name, exc_type=exc_type.__name__, error=str(exc), sig=sig, stack=_frames(tb),
              suppressed=suppressed or None, **fields)
    except Exception:
        S.internal_errors += 1


def http_error():
    log_exception('http', 'exception')


def worker_error():
    log_exception('worker', 'step_error')


def thread_stacks(limit=8):
    names = {t.ident: t.name for t in threading.enumerate()}
    out = {}
    for ident, frame in sys._current_frames().items():
        stack = traceback.extract_stack(frame)[-limit:]
        out['%s#%s' % (names.get(ident, '?'), ident)] = [
            scrub('%s:%d %s' % (os.path.basename(f.filename), f.lineno, f.name)) for f in stack]
    return out


# ---------------------------------------------------------------- crash forensics
def dump_crash(kind, exc_info=None, **extra):
    """Write crash/<ts>.json with the last events, every thread's stack and the failing exception."""
    if not S.on or S.crash_dir is None:
        return None
    try:
        now = time.time()
        body = {'kind': kind, 'ts': _iso(now), 'run_id': S.run_id, 'pid': S.pid,
                'uptime_s': round(time.monotonic() - S.started_mono, 1), 'meta': S.meta,
                'last_events': [dict(ts=_iso(r['t']), **{k: v for k, v in r.items() if k != 't'})
                                for r in list(S.ring)[-RING_MAX:]],
                'threads': thread_stacks(), 'in_flight': _inflight_rows(time.monotonic()),
                'loops': _loop_rows(time.monotonic()), 'dropped_events': S.dropped}
        if exc_info and exc_info[0] is not None:
            exc_type, exc, tb = exc_info
            body['exception'] = {'type': exc_type.__name__, 'message': scrub(str(exc)),
                                 'sig': signature(exc_type, exc, tb),
                                 'stack': [scrub(line.rstrip()) for line in traceback.format_exception(exc_type, exc, tb)][-40:]}
        for key, value in extra.items():
            body[key] = clean(value)
        path = os.path.join(S.crash_dir, time.strftime('%Y%m%dT%H%M%S', time.gmtime(now)) + '-%03d.json' % int((now % 1) * 1000))
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(body, handle, separators=(',', ':'), default=str)
        os.replace(tmp, path)
        S.crash_dump = path
        _prune(S.crash_dir, 20)
        return path
    except Exception:
        S.internal_errors += 1
        return None


def _prune(directory, keep):
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith('.json'))
        for name in names[:-keep]:
            os.unlink(os.path.join(directory, name))
    except OSError:
        pass


def _excepthook(exc_type, exc, tb):
    try:
        if not issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
            S.crashed = True
            S.exit_reason = 'crash'
            error('proc', 'crash', exc_type=exc_type.__name__, error=str(exc), sig=signature(exc_type, exc, tb),
                  stack=_frames(tb), thread='main')
            path = dump_crash('uncaught_exception', (exc_type, exc, tb))
            if path:
                info('proc', 'crash_dump', path=path)
            flush(1.5)
            _write_marker('crashed')
    finally:
        S.saved['excepthook'](exc_type, exc, tb)


def _thread_excepthook(args):
    try:
        if args.exc_type is not SystemExit:
            tname = args.thread.name if args.thread else '?'
            error('thread', 'uncaught', thread=tname, exc_type=args.exc_type.__name__, error=str(args.exc_value),
                  sig=signature(args.exc_type, args.exc_value, args.exc_traceback), stack=_frames(args.exc_traceback))
            if throttle('thread-crash-dump', 60) is not None:
                dump_crash('thread_exception', (args.exc_type, args.exc_value, args.exc_traceback), thread=tname)
            flush(1.0)
    finally:
        S.saved['threading_excepthook'](args)


def _unraisable(unraisable):
    try:
        if throttle('unraisable', 30) is not None:
            warn('proc', 'unraisable', message=unraisable.err_msg,
                 exc_type=getattr(unraisable.exc_type, '__name__', None), error=str(unraisable.exc_value))
    finally:
        S.saved['unraisablehook'](unraisable)


def install_signal(signum, handler):
    """signal.signal() that first records which signal ended the run and when."""
    if not S.on:
        signal.signal(signum, handler)
        return

    def wrapper(number, frame):
        try:
            name = signal.Signals(number).name
            S.exit_reason = name
            warn('proc', 'signal', signal=name, uptime_s=round(time.monotonic() - S.started_mono, 1))
        except Exception:
            S.internal_errors += 1
        handler(number, frame)
    signal.signal(signum, wrapper)


def _exit_via_system_exit(code):
    def handler(number, frame):
        raise SystemExit(code)
    return handler


# ---------------------------------------------------------------- run marker, previous-run verdict
def _write_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, separators=(',', ':'))
    os.replace(tmp, path)


def _write_marker(status, reason=None):
    if S.dir is None or (S.crashed and status == 'running'):
        return
    try:
        _write_json(os.path.join(S.dir, 'run.json'), {
            'run_id': S.run_id, 'pid': S.pid, 'status': status, 'reason': reason or S.exit_reason,
            'started': _iso(S.started), 'last_alive': _iso(time.time()), 'crash_dump': S.crash_dump,
            'sha': S.meta.get('sha'), 'argv': S.meta.get('argv')})
    except Exception:
        S.internal_errors += 1


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def previous_run(directory, crash_directory):
    """Classify how the previous run ended: first | clean | crashed | killed | unknown."""
    try:
        with open(os.path.join(directory, 'run.json'), encoding='utf-8') as handle:
            prev = json.load(handle)
    except (OSError, ValueError):
        return {'outcome': 'first'}
    info_ = {'prev_run_id': prev.get('run_id'), 'prev_pid': prev.get('pid'), 'prev_started': prev.get('started'),
             'prev_last_alive': prev.get('last_alive'), 'exit_reason': prev.get('reason')}
    status = prev.get('status')
    if status == 'clean':
        return dict(info_, outcome='clean')
    if status == 'crashed':
        return dict(info_, outcome='crashed', crash_dump=prev.get('crash_dump'))
    dumps = []
    try:
        for name in sorted(os.listdir(crash_directory)):
            if name.endswith('.json'):
                with open(os.path.join(crash_directory, name), encoding='utf-8') as handle:
                    head = handle.read(400)
                if '"run_id":"%s"' % prev.get('run_id') in head.replace(' ', ''):
                    dumps.append(os.path.join(crash_directory, name))
    except (OSError, ValueError):
        pass
    if dumps:
        return dict(info_, outcome='crashed', exit_reason='crash', crash_dump=dumps[-1])
    fault = os.path.join(crash_directory, 'faulthandler.log')
    try:
        with open(fault, encoding='utf-8', errors='replace') as handle:
            if 'Fatal Python error' in handle.read(200000):
                return dict(info_, outcome='crashed', exit_reason='native crash (faulthandler)', crash_dump=fault)
    except OSError:
        pass
    pid = prev.get('pid')
    if isinstance(pid, int) and pid != os.getpid() and _pid_alive(pid):
        return dict(info_, outcome='unknown', exit_reason='previous pid still alive: two servers on one data folder?')
    return dict(info_, outcome='killed', exit_reason=prev.get('reason') or 'no shutdown record (SIGKILL, OOM, power loss or reboot)')


def _tail_lines(path, count):
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            handle.seek(max(0, end - 65536))
            chunk = handle.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return []
    return chunk[-count:]


def compact_line(line):
    """'12:00:03 http.req route=/api/x status=500' from a JSONL line, for tails and summaries."""
    try:
        row = json.loads(line)
    except ValueError:
        return line[:120]
    skip = ('ts', 'level', 'component', 'event', 'hist', 'stack')
    rest = ' '.join('%s=%s' % (k, v) for k, v in row.items() if k not in skip)
    return ('%s %s %s.%s %s' % (str(row.get('ts', ''))[11:23], row.get('level', '')[:4], row.get('component'),
                                row.get('event'), rest))[:200]


# ---------------------------------------------------------------- loops and requests (watchdog inputs)
class _Loop:
    __slots__ = ('name', 'steps', 'busy', 'errors', 'total', 'max', 'begin', 'last_end', 'ident', 'warned')

    def __init__(self, name):
        self.name, self.steps, self.busy, self.errors, self.total, self.max = name, 0, 0, 0, 0.0, 0.0
        self.begin = self.ident = self.warned = None
        self.last_end = time.monotonic()


def wrap_step(name, step):
    """Time a worker-loop step: counters for the heartbeat, a warning when one step takes over a second."""
    loop = S.loops.get(name)
    if loop is None:
        loop = S.loops[name] = _Loop(name)

    def timed(conn):
        if not S.on:
            return step(conn)
        _ctx.loop = name
        S.loops.setdefault(name, loop)
        t0 = time.perf_counter()
        loop.ident, loop.begin = threading.get_ident(), time.monotonic()
        if not loop.steps:
            info('worker', 'start', loop=name)
        try:
            result = step(conn)
        except BaseException:
            loop.errors += 1
            raise
        finally:
            dt = time.perf_counter() - t0
            loop.steps += 1
            loop.total += dt
            if dt > loop.max:
                loop.max = dt
            loop.begin, loop.last_end, loop.warned = None, time.monotonic(), None
            if dt >= 1.0 and throttle(('slow-step', name), 60) is not None:
                warn('worker', 'slow_step', loop=name, dur_ms=round(dt * 1000))
        if result:
            loop.busy += 1
        return result
    timed.__wrapped__ = step
    return timed


def _loop_rows(now):
    rows = {}
    for name, loop in list(S.loops.items()):
        rows[name] = {'steps': loop.steps, 'busy': loop.busy, 'errors': loop.errors,
                      'max_ms': round(loop.max * 1000), 'idle_s': round(now - loop.last_end, 1) if loop.begin is None else 0,
                      'in_step_s': round(now - loop.begin, 1) if loop.begin else 0}
    return rows


def request_enter(ticket):
    S.inflight[threading.get_ident()] = ticket


def request_exit():
    S.inflight.pop(threading.get_ident(), None)


def _inflight_rows(now):
    rows = []
    for ident, ticket in list(S.inflight.items()):
        rows.append({'thread': ident, 'route': getattr(ticket, 'route', '?'), 'method': getattr(ticket, 'method', '?'),
                     'request_id': getattr(ticket, 'rid', None), 'age_s': round(now - getattr(ticket, 'start', now), 1)})
    return rows


# ---------------------------------------------------------------- database
def _sql_sig(sql, _cache={}):
    sig = _cache.get(sql)
    if sig is None:
        text = re.sub(r"'(?:[^']|'')*'", '?', ' '.join(str(sql).split()))
        text = re.sub(r'\b\d+(?:\.\d+)?\b', '?', text)
        text = re.sub(r'\((?:\?,\s*)+\?\)', '(?...)', text)
        sig = text[:140]
        if len(_cache) < 2000:
            _cache[sql] = sig
    return sig


_DB_NOTE, _DB_SLOW, _DB_LOCK = 0.02, 0.25, 0.05
_perf = time.perf_counter


def _db_note(sql, dt):
    sig = _sql_sig(sql)
    agg('db', 'q', sig, dt * 1000, every=120)
    if sig.startswith(('BEGIN IMMEDIATE', 'BEGIN EXCLUSIVE')) and dt >= _DB_LOCK:
        if throttle(('lock', sig), 5) is not None:
            warn('db', 'lock_wait', dur_ms=round(dt * 1000), sql=sig)
    elif dt >= _DB_SLOW and throttle(('slow', sig), 10) is not None:
        warn('db', 'slow', dur_ms=round(dt * 1000), sql=sig)


def _db_failed(sql, dt, exc):
    text = str(exc).lower()
    busy = isinstance(exc, sqlite3.OperationalError) and ('locked' in text or 'busy' in text)
    if throttle(('dberr', _sql_sig(sql), busy), 5) is not None:
        event('db', 'busy' if busy else 'error', 'warn' if busy else 'error', dur_ms=round(dt * 1000),
              sql=_sql_sig(sql), error=str(exc))


class TimedConnection(sqlite3.Connection):
    """sqlite3.Connection that reports slow statements, lock waits and busy errors."""

    def execute(self, sql, *args):
        t0 = _perf()
        try:
            result = super().execute(sql, *args)
        except sqlite3.Error as exc:
            _db_failed(sql, _perf() - t0, exc)
            raise
        dt = _perf() - t0
        if dt >= _DB_NOTE:
            _db_note(sql, dt)
        return result

    def executemany(self, sql, *args):
        t0 = _perf()
        try:
            result = super().executemany(sql, *args)
        except sqlite3.Error as exc:
            _db_failed(sql, _perf() - t0, exc)
            raise
        dt = _perf() - t0
        if dt >= _DB_NOTE:
            _db_note(sql, dt)
        return result

    def commit(self):
        t0 = _perf()
        try:
            result = super().commit()
        except sqlite3.Error as exc:
            _db_failed('COMMIT', _perf() - t0, exc)
            raise
        dt = _perf() - t0
        if dt >= _DB_NOTE:
            _db_note('COMMIT', dt)
        return result


def connect_options():
    """kwargs for sqlite3.connect: the timing connection while logging is on, nothing otherwise."""
    return {'factory': TimedConnection} if S.on else {}


# ---------------------------------------------------------------- process vitals, heartbeat, watchdog
def _rss_mb():
    if resource is None:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1048576 if sys.platform == 'darwin' else 1024))


def _fds():
    try:
        return len(os.listdir('/dev/fd'))
    except OSError:
        return None


def _vitals():
    try:
        load = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        load = None
    return {'rss_peak_mb': _rss_mb(), 'threads': threading.active_count(), 'fds': _fds(), 'load1': load}


def heartbeat():
    now = time.monotonic()
    rows = _inflight_rows(now)
    oldest = max((r['age_s'] for r in rows), default=0)
    fields = dict(_vitals(), up_s=round(now - S.started_mono), queue=len(S.q), dropped=S.dropped,
                  http_active=len(rows), http_oldest_s=oldest, errors=S.n_by_level['error'],
                  warns=S.n_by_level['warn'], loops={n: [r['steps'], r['busy'], r['errors'], r['max_ms'], r['idle_s']]
                                                     for n, r in _loop_rows(now).items()})
    for name, fn in list(S.gauges.items()):
        try:
            value = fn()
            if isinstance(value, dict):
                fields[name] = value
        except Exception:
            S.internal_errors += 1
    event('proc', 'heartbeat', **fields)
    _write_marker('running')
    if S.hang_s and S.fault_file is not None:
        # Re-armed once a minute: if this thread stops ticking for hang_s, faulthandler dumps every stack.
        faulthandler.dump_traceback_later(S.hang_s, file=S.fault_file)


def _watch(now):
    for ident, ticket in list(S.inflight.items()):
        age = now - getattr(ticket, 'start', now)
        if age > S.stuck_s and not getattr(ticket, 'warned', False):
            ticket.warned = True
            frame = sys._current_frames().get(ident)
            stack = [scrub('%s:%d %s' % (os.path.basename(f.filename), f.lineno, f.name))
                     for f in traceback.extract_stack(frame)[-12:]] if frame else []
            warn('watchdog', 'stuck_request', route=ticket.route, method=ticket.method,
                 age_s=round(age, 1), request_id=ticket.rid, stack=stack)
    for loop in list(S.loops.values()):
        if loop.begin and now - loop.begin > S.stuck_s and loop.warned != loop.begin:
            loop.warned = loop.begin
            frame = sys._current_frames().get(loop.ident)
            stack = [scrub('%s:%d %s' % (os.path.basename(f.filename), f.lineno, f.name))
                     for f in traceback.extract_stack(frame)[-12:]] if frame else []
            warn('watchdog', 'stuck_loop', loop=loop.name, age_s=round(now - loop.begin, 1), stack=stack)


def _beat_loop():
    last_m, last_w = time.monotonic(), time.time()
    next_hb = last_m
    while not S.stop_evt.wait(S.tick_s):
        try:
            now_m, now_w = time.monotonic(), time.time()
            dm, dw = now_m - last_m, now_w - last_w
            if dw - dm > 20:
                info('proc', 'slept', slept_s=round(dw - dm))
            elif dm > S.tick_s * 4 + 5:
                warn('watchdog', 'stall', gap_s=round(dm, 1), detail='the watchdog thread did not run; process starved or frozen')
            last_m, last_w = now_m, now_w
            _watch(now_m)
            flush_aggs()
            if now_m >= next_hb:
                heartbeat()
                next_hb = now_m + S.hb_s
        except Exception:
            S.internal_errors += 1


# ---------------------------------------------------------------- lifecycle
def _git_sha(root):
    try:
        git = os.path.join(root, '.git')
        if os.path.isfile(git):
            with open(git, encoding='utf-8') as handle:
                git = os.path.join(root, handle.read().strip().split(':', 1)[1].strip())
        with open(os.path.join(git, 'HEAD'), encoding='utf-8') as handle:
            head = handle.read().strip()
        if head.startswith('ref:'):
            ref = head.split(None, 1)[1]
            common = git
            if os.path.exists(os.path.join(git, 'commondir')):
                with open(os.path.join(git, 'commondir'), encoding='utf-8') as handle:
                    common = os.path.normpath(os.path.join(git, handle.read().strip()))
            for base in (git, common):
                path = os.path.join(base, ref)
                if os.path.exists(path):
                    with open(path, encoding='utf-8') as handle:
                        return handle.read().strip()[:10]
            with open(os.path.join(common, 'packed-refs'), encoding='utf-8') as handle:
                for line in handle:
                    if line.strip().endswith(' ' + ref):
                        return line.split()[0][:10]
            return None
        return head[:10]
    except Exception:
        return None


def start(data_dir, *, root=None, name='server', subdir=None, max_bytes=None, files=None, level=None, tick_s=5.0,
          hb_s=60.0, hang_s=300.0, stuck_s=30.0, poll=0.2, watchdog=True, hooks=True, **meta):
    """Turn logging on for this process. Safe to call once; returns the previous-run verdict."""
    if S.on:
        return {}
    if os.environ.get('FL_LOG_OFF') == '1':
        return {}
    data_dir = str(data_dir)
    base = os.environ.get('FL_LOG_DIR') or data_dir
    S.dir = os.path.join(base, 'logs', subdir) if subdir else os.path.join(base, 'logs')
    S.crash_dir = os.path.join(base, 'crash', subdir) if subdir else os.path.join(base, 'crash')
    for directory in (S.dir, S.crash_dir):
        os.makedirs(directory, mode=0o700, exist_ok=True)
    max_bytes = max_bytes or int(float(os.environ.get('FL_LOG_MB', '10')) * 1048576)
    files = files or int(os.environ.get('FL_LOG_FILES', '5'))
    S.min_level = LEVELS.get(level or os.environ.get('FL_LOG_LEVEL', 'info'), 20)
    S.tick_s, S.hb_s, S.hang_s, S.stuck_s = tick_s, hb_s, hang_s, stuck_s
    verdict = previous_run(S.dir, S.crash_dir)
    verdict['last_events'] = [compact_line(line) for line in _tail_lines(os.path.join(S.dir, 'events.jsonl'), 12)]
    S.pid = os.getpid()
    S.started, S.started_mono = time.time(), time.monotonic()
    S.run_id = '%s-%s' % (time.strftime('%m%dT%H%M%S', time.gmtime(S.started)), os.urandom(2).hex())
    S.meta = dict(clean(meta), name=name, sha=_git_sha(root) if root else None, python=sys.version.split()[0],
                  argv=' '.join(sys.argv)[:200])
    S.exit_reason, S.crashed, S.crash_dump = None, False, None
    S.stop_evt.clear()
    S.q.clear()
    S.ring.clear()
    S.dropped = S.internal_errors = 0
    S.n_by_level = {'error': 0, 'warn': 0}
    fault_path = os.path.join(S.crash_dir, 'faulthandler.log')
    _archive_fault_log(fault_path)
    S.writer = _Writer(os.path.join(S.dir, 'events.jsonl'), max_bytes, files, poll)
    S.on = True
    S.writer.start()
    if hooks:
        _install_hooks(fault_path)
    _write_marker('running')
    atexit.register(_atexit)
    info('proc', 'start', pid=S.pid, run_id=S.run_id, **{k: v for k, v in S.meta.items() if v is not None})
    event('proc', 'previous_run', 'warn' if verdict.get('outcome') in ('crashed', 'killed', 'unknown') else 'info', **verdict)
    if watchdog:
        S.beat = threading.Thread(target=_beat_loop, name='fortunate-obs-watchdog', daemon=True)
        S.beat.start()
        heartbeat()
    return verdict


def _archive_fault_log(path):
    """Move a non-empty faulthandler log from the previous run into a named dump, so this run starts clean."""
    try:
        if os.path.getsize(path) > 0:
            os.replace(path, os.path.join(S.crash_dir, time.strftime('%Y%m%dT%H%M%S', time.gmtime()) + '-faulthandler.txt'))
            _prune_txt(S.crash_dir, 10)
    except OSError:
        pass


def _prune_txt(directory, keep):
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith('-faulthandler.txt'))
        for name in names[:-keep]:
            os.unlink(os.path.join(directory, name))
    except OSError:
        pass


def _install_hooks(fault_path):
    S.saved['excepthook'] = sys.excepthook
    S.saved['threading_excepthook'] = threading.excepthook
    S.saved['unraisablehook'] = sys.unraisablehook
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    sys.unraisablehook = _unraisable
    try:
        S.fault_file = open(fault_path, 'a', encoding='utf-8')
        S.saved['faulthandler'] = faulthandler.is_enabled()
        faulthandler.enable(file=S.fault_file, all_threads=True)
        if hasattr(signal, 'SIGUSR1'):
            faulthandler.register(signal.SIGUSR1, file=S.fault_file, all_threads=True)
    except Exception:
        S.fault_file = None
    if threading.current_thread() is threading.main_thread():
        for number, handler, default in ((signal.SIGINT, signal.default_int_handler, signal.default_int_handler),
                                         (signal.SIGTERM, _exit_via_system_exit(0), signal.SIG_DFL),
                                         (getattr(signal, 'SIGHUP', None), _exit_via_system_exit(129), signal.SIG_DFL)):
            if number is not None and signal.getsignal(number) == default:
                S.saved.setdefault('signals', []).append(number)
                install_signal(number, handler)


def _atexit():
    stop('crash' if S.crashed else None)


def stop(reason=None):
    """Orderly shutdown: final aggregates, exit marker, writer drained. Idempotent."""
    if not S.on:
        return
    try:
        flush_aggs(force=True)
        reason = reason or S.exit_reason or 'normal'
        event('proc', 'exit', 'error' if S.crashed else 'info', reason=reason,
              up_s=round(time.monotonic() - S.started_mono), clean=not S.crashed, **_vitals())
        S.stop_evt.set()
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:
            pass
        if not S.crashed:
            _write_marker('clean', reason)
        flush(2.0)
        S.q.append(_STOP)
        if S.writer is not None:
            S.writer.join(2.0)
    finally:
        S.on = False
        _restore_hooks()
        try:
            atexit.unregister(_atexit)
        except Exception:
            pass


def _restore_hooks():
    if 'excepthook' in S.saved:
        sys.excepthook = S.saved.pop('excepthook')
        threading.excepthook = S.saved.pop('threading_excepthook')
        sys.unraisablehook = S.saved.pop('unraisablehook')
        try:
            if hasattr(signal, 'SIGUSR1'):
                faulthandler.unregister(signal.SIGUSR1)
            faulthandler.disable()
            if S.saved.pop('faulthandler', False):
                faulthandler.enable()
        except Exception:
            pass
        if S.fault_file is not None:
            try:
                S.fault_file.close()
            except OSError:
                pass
            S.fault_file = None
        for number in S.saved.pop('signals', []):
            try:
                signal.signal(number, signal.default_int_handler if number == signal.SIGINT else signal.SIG_DFL)
            except Exception:
                pass
    S.loops.clear()
    S.inflight.clear()
    S.gauges.clear()
    S.aggs.clear()
    S.trans.clear()
    S.throttles.clear()
