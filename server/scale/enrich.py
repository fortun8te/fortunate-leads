"""Logged-out profile enrichment pool: one worker per egress unit, massively parallel.

Route (verified live through Tor on 2026-09-27, see docs/scaling-plan.md):
  GET https://i.instagram.com/api/v1/users/web_profile_info/?username=<handle>
  Android app identity (User-Agent + X-IG-App-ID 567067343352427) AND a browser TLS fingerprint
  (curl_cffi impersonate="chrome"). With Python's stock TLS the same request gets 429 on the first
  try; `www.instagram.com` with the web app id gets 401 "please wait" (instaloader #2726).
  Logged-out `users/{pk}/info/` answers 200 but only with username/pk/picture: useless for bios.
Fallback: some business profiles answer 400 "Asset asset://laser.provider/ig_business_category_
subvertical has been deleted" (every app version tried), a few answer {"status":"ok"} with no data.
Those go to the public profile HTML (www.instagram.com/<handle>/), which carries biography, pk and
og:description counts, but is ~0.8 MB and often a login redirect on a given exit (~3 tries/profile
in the 2026-09-27 smoke test), so it is only a fallback.

Worker policy per egress unit
  * own Pacer (start 10 req/min, cap 15; AIMD) - the 2026-09-24 measurement;
  * Tor: push-back (429 / login wall / 401 please-wait / 403) -> new circuit at once (new exit IP)
    and a short pause; the lease goes back untouched. Many swaps in a row -> real cooldown
    (Tor as a whole is saturated, stop feeding it);
  * non-rotatable egress (IPv6 bind, phone, SOCKS box): push-back -> Pacer cooldown (escalating);
  * a profile-level error (400 "Asset ... deleted", other) -> that handle backs off, the egress is fine.
Nothing here touches a logged-in session.
"""
import json
import random
import threading
import time

from scale import signals
from scale.pacing import profile_pacer

ANDROID_APP_ID = '567067343352427'
ANDROID_UAS = [
    'Instagram 361.0.0.46.88 Android (33/13; 420dpi; 1080x2400; samsung; SM-G991B; o1s; exynos2100; en_US; 674675155)',
    'Instagram 361.0.0.46.88 Android (34/14; 480dpi; 1080x2340; Google/google; Pixel 7; panther; panther; en_US; 674675155)',
    'Instagram 360.0.0.52.192 Android (31/12; 440dpi; 1080x2400; Xiaomi/Redmi; M2101K6G; sweet; qcom; en_GB; 672535977)',
    'Instagram 361.0.0.46.88 Android (33/13; 450dpi; 1080x2400; OnePlus; CPH2449; OP5958L1; qcom; en_US; 674675155)',
]


def profile_request(handle, ua=None):
    url = 'https://i.instagram.com/api/v1/users/web_profile_info/?username=' + handle
    return url, {'User-Agent': ua or ANDROID_UAS[0], 'X-IG-App-ID': ANDROID_APP_ID, 'Accept': '*/*',
                 'Accept-Language': 'en-US'}


def profile_html_request(handle):
    """Fallback for profiles the Android route cannot serve (400 "Asset ... has been deleted", seen
    on many business accounts 2026-09-27): the public profile page. ~0.8 MB and often a login
    redirect on a given exit, so it is only a fallback, never the main route."""
    return 'https://www.instagram.com/%s/' % handle, {
        'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'en-US,en;q=0.9'}


_ABBR = {'K': 1e3, 'M': 1e6, 'B': 1e9}


def _abbr(s):
    s = (s or '').replace(',', '').strip()
    try:
        if s[-1:].upper() in _ABBR:
            return int(float(s[:-1]) * _ABBR[s[-1].upper()])
        return int(float(s))
    except ValueError:
        return None


def _jstr(raw):
    try:
        return json.loads('"%s"' % raw)
    except ValueError:
        return raw


def parse_profile_html(text, handle):
    """Public profile HTML -> contract fields. Exact counts when the embedded JSON has them, else the
    og:description numbers (rounded, e.g. "6M"). Returns None for a login page or unrelated HTML."""
    import re
    t = text or ''
    og = re.search(r'<meta property="og:description" content="([^"]+)"', t)
    bio = re.search(r'"biography":"((?:[^"\\]|\\.)*)"', t)
    pk = re.search(r'"profilePage_(\d+)"', t)
    if not (og or bio) or not pk:
        return None
    counts = re.match(r'\s*([\d.,]+[KMB]?) Followers, ([\d.,]+[KMB]?) Following, ([\d.,]+[KMB]?) Posts', og.group(1)) \
        if og else None
    exact = re.search(r'"edge_followed_by":\{"count":(\d+)\}', t) or re.search(r'"follower_count":(\d+)', t)
    name = re.search(r'<meta property="og:title" content="([^"(]+?)\s*\(&#064;', t)
    ext = re.search(r'"external_url":"((?:[^"\\]|\\.)*)"', t)
    site = _jstr(ext.group(1)) if ext else None
    priv = re.search(r'"is_private":(true|false)', t)
    return {
        'ig_id': pk.group(1), 'handle': handle.lower(), 'name': (name.group(1).strip() if name else ''),
        'bio': _jstr(bio.group(1)) if bio else None,
        'website': site if isinstance(site, str) and site.lower().startswith(('http://', 'https://')) else None,
        'category': None,
        'followers': int(exact.group(1)) if exact else (_abbr(counts.group(1)) if counts else None),
        'following': _abbr(counts.group(2)) if counts else None,
        'posts': _abbr(counts.group(3)) if counts else None,
        'is_private': (priv.group(1) == 'true') if priv else None, 'is_verified': '"is_verified":true' in t,
        'is_business': None, 'pic_url': None, 'email': None,
    }


def _count(v):
    if isinstance(v, dict):
        v = v.get('count')
    if isinstance(v, str) and v.replace(',', '').isdigit():
        v = int(v.replace(',', ''))
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10 ** 12 else None


def parse_profile(j):
    """web_profile_info JSON -> the /api/ext/profile contract fields (same as core.js mapProfile)."""
    u = (j or {}).get('data', {}).get('user') if isinstance(j, dict) else None
    if not isinstance(u, dict) or not u.get('username'):
        return None
    links = [x.get('url') or x.get('lynx_url') for x in (u.get('bio_links') or []) if isinstance(x, dict)]
    site = u.get('external_url') or next((x for x in links if x), None)
    if not (isinstance(site, str) and site.lower().startswith(('http://', 'https://'))):
        site = None
    return {
        'ig_id': str(u.get('id') or u.get('pk') or '') or None,
        'handle': str(u['username']).lower(),
        'name': u.get('full_name') or '',
        'bio': u.get('biography') if isinstance(u.get('biography'), str) else None,
        'website': site,
        'category': u.get('category_name') or u.get('business_category_name') or u.get('category') or None,
        'followers': _count(u.get('edge_followed_by') or u.get('follower_count')),
        'following': _count(u.get('edge_follow') or u.get('following_count')),
        'posts': _count(u.get('edge_owner_to_timeline_media') or u.get('media_count')),
        'is_private': u['is_private'] if isinstance(u.get('is_private'), bool) else None,
        'is_verified': bool(u.get('is_verified')),
        'is_business': bool(u.get('is_business_account') or u.get('is_professional_account')),
        'pic_url': u.get('profile_pic_url_hd') or u.get('profile_pic_url') or None,
        'email': u.get('business_email') or None,
    }


class CurlTransport:
    """curl_cffi with a Chrome TLS fingerprint. Optional dependency: `pip install curl_cffi`
    in the worker's own venv (the server itself stays stdlib-only)."""

    def __init__(self, impersonate='chrome'):
        from curl_cffi import requests as cr  # noqa: deferred so the server never needs it
        self.cr, self.impersonate = cr, impersonate

    @staticmethod
    def proxy_url(egress):
        if egress.kind in ('tor', 'socks5'):
            auth = '%s:%s@' % (egress.username, egress.password or 'x') if egress.username else ''
            return 'socks5h://%s%s:%d' % (auth, egress.host, egress.port)
        if egress.kind == 'http':
            return 'http://%s:%d' % (egress.host, egress.port)
        return None

    def send(self, egress, method, url, headers=None, body=None, timeout=20):
        from scale.transport import Response, TransportError
        if egress is None:
            raise TransportError('no egress: direct connections are not allowed')
        kw = {'headers': headers or {}, 'impersonate': self.impersonate, 'timeout': timeout,
              'allow_redirects': False}
        p = self.proxy_url(egress)
        if p:
            kw['proxies'] = {'https': p, 'http': p}
        elif egress.kind == 'bind':
            kw['interface'] = egress.source_addr
        else:
            raise TransportError('unsupported egress')
        t0 = time.monotonic()
        try:
            r = self.cr.request(method, url, data=body, **kw)
        except Exception as e:  # curl errors -> transport trouble
            raise TransportError(str(e)[:200])
        hdrs = {k.lower(): v for k, v in r.headers.items()}
        loc = hdrs.get('location')
        final = loc if 300 <= r.status_code < 400 and loc else url
        return Response(r.status_code, hdrs, r.text, final, time.monotonic() - t0)


# ---- sinks ------------------------------------------------------------------
class JsonlSink:
    def __init__(self, path):
        self.f = open(path, 'a', encoding='utf-8')
        self.lock = threading.Lock()

    def write(self, profile):
        with self.lock:
            self.f.write(json.dumps(profile, ensure_ascii=False) + '\n')
            self.f.flush()


class FortunateDBSink:
    """Writes into leads.sqlite like server/biofetch.py does (bio_src='public_android'), finishes the
    person's queued profile job, re-applies tag rules. Honours the owner's Bios pause; a logged-in
    account's Instagram cooldown does not apply to this logged-out identity."""

    def __init__(self, path, src='public_android'):
        import db  # server module; server/ must be on sys.path
        self.db, self.src, self.path = db, src, path
        self.lock = threading.Lock()
        self.conn = db.connect(path)

    def paused(self):
        try:
            import control
            return bool(control.stage_paused(self.conn, 'bios'))
        except Exception:
            return True   # fail closed

    def write(self, profile):
        p = {k: v for k, v in profile.items() if k in self.db.PERSON_FIELDS}
        with self.lock:
            ts = self.db.now()
            p.update(bio_at=ts, bio_src=self.src)
            pid = self.db.upsert_person(self.conn, p, ts)
            self.conn.execute("UPDATE jobs SET state='done', leased_until=NULL WHERE kind='profile' AND handle=? "
                              "AND state IN ('queued','leased')", (p['handle'],))
            try:
                import rules
                rules.sync(self.conn, [pid])
            except Exception:
                pass
            self.conn.commit()


# ---- the pool -----------------------------------------------------------------
class EnrichmentPool:
    def __init__(self, store, egresses, transport, sink, metrics=None, registry=None, rng=None,
                 pacer_factory=None, clock=time.time, sleep=time.sleep, swap_limit=8, batch=64):
        self.store, self.transport, self.sink, self.metrics = store, transport, sink, metrics
        self.registry, self.rng = registry, rng or random.Random()
        self.clock, self.sleep = clock, sleep
        self.egresses = [e for e in egresses if not e.dedicated_to]   # never borrow an account's IP
        self.pacers = {e.id: (pacer_factory or (lambda: profile_pacer(rng=self.rng)))() for e in self.egresses}
        self.swaps = {e.id: 0 for e in self.egresses}
        self.swap_limit, self.batch = swap_limit, batch
        self.buf, self.buf_lock = [], threading.Lock()
        self.stop_evt = threading.Event()
        self.threads = []
        self.paused_fn = getattr(sink, 'paused', lambda: False)
        self.html_route = {}      # handle -> attempts on the HTML fallback route
        self.html_max = 8

    def _m(self, scope, ident, name):
        if self.metrics:
            self.metrics.inc(scope, ident, name, now=self.clock())

    def _next_handle(self):
        with self.buf_lock:
            if not self.buf:
                self.buf = self.store.lease(self.batch, now=self.clock())
            return self.buf.pop(0) if self.buf else None

    def step(self, egress):
        """One unit of work for one egress. Returns a short outcome string (tests/metrics)."""
        now = self.clock()
        if self.registry and not self.registry.ready_for_work(egress):
            return 'unverified'
        pacer = self.pacers[egress.id]
        if not pacer.try_acquire(now):
            return 'wait'
        item = self._next_handle()
        if item is None:
            pacer.next_at = now + 5    # nothing queued; do not burn the slot
            return 'idle'
        handle, _ = item
        html = handle in self.html_route
        if html:
            url, headers = profile_html_request(handle)
            self.html_route[handle] += 1
        else:
            url, headers = profile_request(handle, self.rng.choice(ANDROID_UAS))
        self._m('egress', egress.id, 'requests_html' if html else 'requests')
        try:
            r = self.transport.send(egress, 'GET', url, headers, timeout=40 if html else 25)
            if html and r.status == 200:
                code, reason = signals.OK, ''     # the HTML parser decides (login pages parse to None)
            else:
                code, reason = signals.classify(r.status, r.text, r.url)
            if code == signals.OTHER and r.status == 400 and 'asset' in (r.text or '').lower():
                code, reason = 'asset_deleted', 'asset_deleted'
        except OSError:
            code, reason, r = signals.NETWORK, 'transport', None
        if code == 'asset_deleted':
            # Instagram-side bug on this profile for the Android route: retry on the HTML route.
            self.html_route[handle] = 0
            self.store.release(handle)
            self._m('egress', egress.id, 'asset_deleted')
            return 'to_html'
        if html and code in signals.PUSHBACK and self.html_route[handle] >= self.html_max:
            self.html_route.pop(handle, None)
            self.store.fail(handle, now=now)
            self._m('egress', egress.id, 'html_gave_up')
            if egress.kind == 'tor':
                egress.rotate()
            return 'html_gave_up'
        if code == signals.OK:
            prof = None
            if html:
                prof = parse_profile_html(r.text, handle)
            else:
                try:
                    j = json.loads(r.text)
                except ValueError:
                    j = None
                if isinstance(j, dict) and 'data' not in j and j.get('status') == 'ok':
                    # {"status":"ok"} with no data (seen 2026-09-27 on a few brand handles): try the HTML route
                    self.html_route[handle] = 0
                    self.store.release(handle)
                    self._m('egress', egress.id, 'empty_ok')
                    return 'to_html'
                if isinstance(j, dict) and isinstance(j.get('data'), dict) and j['data'].get('user') is None:
                    self.store.done(handle, outcome='not_found', now=now)
                    self._m('egress', egress.id, 'not_found')
                    return 'not_found'
                prof = parse_profile(j)
            if not prof and html and self.html_route.get(handle, 0) < self.html_max:
                self.store.release(handle)          # a logged-out wall page, not a profile problem
                self._m('egress', egress.id, 'html_wall')
                if egress.kind == 'tor':
                    egress.rotate()
                return 'html_wall'
            if not prof:
                self.html_route.pop(handle, None)
                self.store.fail(handle, now=now)
                self._m('egress', egress.id, 'bad_body')
                return 'bad_body'
            self.html_route.pop(handle, None)
            self.sink.write(prof)
            self.store.done(handle, now=now, ig_id=prof.get('ig_id'))
            pacer.success(now)
            self.swaps[egress.id] = 0
            self._m('egress', egress.id, 'ok_html' if html else 'ok')
            self._m('pipeline', 'enrich', 'profiles')
            return 'ok'
        if code == signals.NOT_FOUND:
            self.store.done(handle, outcome='not_found', now=now)
            self._m('egress', egress.id, 'not_found')
            return 'not_found'
        if code in signals.PUSHBACK:
            self.store.release(handle)
            self._m('egress', egress.id, 'pushback_' + code)
            if egress.kind == 'tor':
                egress.rotate()
                self.swaps[egress.id] += 1
                self._m('egress', egress.id, 'circuit_swaps')
                if self.swaps[egress.id] >= self.swap_limit:
                    self.swaps[egress.id] = 0
                    pacer.pushback(now)
                    return 'tor_saturated'
                pacer.next_at = max(pacer.next_at, now + self.rng.uniform(2, 6))
                return 'swapped'
            retry = None
            if r is not None and r.header('retry-after', '').isdigit():
                retry = now + int(r.header('retry-after'))
            pacer.pushback(now, retry_at=retry)
            return 'cooldown'
        if code == signals.NETWORK:
            self.store.release(handle)
            self._m('egress', egress.id, 'network')
            pacer.soft_error(now, delay=15)
            if egress.kind == 'tor':
                egress.rotate()
            return 'network'
        # profile-level or unknown: the handle backs off, the egress keeps going
        self.store.fail(handle, now=now)
        self._m('egress', egress.id, 'error_' + str(reason)[:30])
        return 'error'

    def _worker(self, egress):
        while not self.stop_evt.is_set():
            if self.paused_fn():
                self.sleep(10)
                continue
            out = self.step(egress)
            if out in ('wait', 'idle', 'unverified'):
                wait = self.pacers[egress.id].ready_at(self.clock()) - self.clock()
                self.sleep(min(max(wait, 0.2), 10))

    def start(self):
        for e in self.egresses:
            t = threading.Thread(target=self._worker, args=(e,), name='enrich-' + e.id, daemon=True)
            t.start()
            self.threads.append(t)
        return self

    def stop(self, timeout=30):
        self.stop_evt.set()
        for t in self.threads:
            t.join(timeout)
