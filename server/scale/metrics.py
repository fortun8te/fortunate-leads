"""Rolling per-account / per-egress counters: requests, successes, push-backs by code, profiles.

`snapshot()` is JSON for the UI/CLI; `prometheus()` is text exposition if you want Grafana.
"""
import collections
import threading
import time

WINDOW = 3600.0


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = collections.defaultdict(collections.deque)   # (scope, id, name) -> deque[t]
        self.totals = collections.Counter()
        self.last = {}

    def inc(self, scope, ident, name, now=None, n=1):
        now = time.time() if now is None else now
        key = (scope, str(ident), name)
        with self.lock:
            d = self.events[key]
            for _ in range(n):
                d.append(now)
            self.totals[key] += n
            self.last[key] = now
            while d and now - d[0] > WINDOW:
                d.popleft()

    def rate(self, scope, ident, name, seconds=60.0, now=None):
        now = time.time() if now is None else now
        with self.lock:
            return sum(1 for t in self.events.get((scope, str(ident), name), ()) if now - t <= seconds)

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        out = {}
        with self.lock:
            for (scope, ident, name), d in self.events.items():
                while d and now - d[0] > WINDOW:
                    d.popleft()
                row = out.setdefault(scope, {}).setdefault(ident, {})
                row[name] = {'total': self.totals[(scope, ident, name)],
                             'last_min': sum(1 for t in d if now - t <= 60), 'last_hour': len(d),
                             'last_at': self.last.get((scope, ident, name))}
        return out

    def prometheus(self, now=None):
        lines = ['# TYPE fl_scale_events_total counter', '# TYPE fl_scale_events_hour gauge']
        for scope, ids in sorted(self.snapshot(now).items()):
            for ident, names in sorted(ids.items()):
                for name, v in sorted(names.items()):
                    lab = 'scope="%s",id="%s",event="%s"' % (scope, ident.replace('"', ''), name)
                    lines.append('fl_scale_events_total{%s} %d' % (lab, v['total']))
                    lines.append('fl_scale_events_hour{%s} %d' % (lab, v['last_hour']))
        return '\n'.join(lines) + '\n'
