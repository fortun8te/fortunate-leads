"""HTTP layer: multi-egress pool (direct / proxy / relay) with adaptive per-egress rate limiting, parking of
blocked egresses (exponential cooldown), retirement of dead proxies, and retry with backoff + jitter."""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .egress import EgressSpec, normalise_proxy  # noqa: F401  (re-exported)

log = logging.getLogger("igscraper")

# Measured on this box's datacenter IP, 2026-09-24 (bench/): ~91 req/min (49 profiles/min) held 8 min with
# zero throttling; ~1,200 req/min got the first 401 after 5,007 requests / 262 s. Defaults stay well below.
DEFAULT_START_RPM = 45.0     # per egress IP; ramps on 200s
DEFAULT_MAX_RPM = 60.0       # BUG-1: stay at/below the measured safe per-IP rate (~90 held 8 min, but no margin)
TOR_START_RPM = 10.0         # per Tor circuit (many circuits share few exits)
TOR_MAX_RPM = 15.0           # raw test 2026-09-24: one exit held 24 req/min for 10 min without a block
REPROBE_AFTER = 600.0        # a retired proxy/relay is re-probed after this long (BUG-5)
DEFAULT_MIN_RPM = 15.0
REQUESTS_PER_PROFILE = 1.5   # measured average: 1.4-1.8 depending on how many profiles need the fallback


class ScrapeError(Exception):
    code = "error"


class NotFound(ScrapeError):
    code = "not_found"


class SchemaError(ScrapeError):
    """Instagram returned a server-side 400 for this profile on this endpoint (not retryable there)."""
    code = "endpoint_schema_error"


class LoginWall(ScrapeError):
    code = "login_required"


class RateLimited(ScrapeError):
    code = "rate_limited"


class UpstreamError(ScrapeError):
    code = "upstream_error"


class BadResponse(ScrapeError):
    code = "bad_response"


class NoEgress(ScrapeError):
    code = "no_proxies_left"


# ----------------------------------------------------------------------------- rate limiting
class TokenBucket:
    """Fixed-rate bucket (kept for callers that want a plain limiter)."""
    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.capacity = max(1, burst)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.rate <= 0:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class AdaptiveLimiter:
    """Token bucket whose rate ramps up slowly on success and halves on a block.

    mode "adaptive": rpm starts at start_rpm, +ramp_step every ramp_every successes, capped at max_rpm.
    mode "fixed": constant rate (``--rate`` given explicitly). mode "unlimited": no limit (rate 0).
    """
    def __init__(self, *, mode: str = "adaptive", start_rpm: float = DEFAULT_START_RPM,
                 max_rpm: float = DEFAULT_MAX_RPM, min_rpm: float = DEFAULT_MIN_RPM, fixed_rps: float = 0.0,
                 burst: int = 2, ramp_every: int = 20, ramp_step: float = 3.0):
        self.mode = mode
        self.start_rpm, self.max_rpm = start_rpm, max(max_rpm, start_rpm)
        self.min_rpm = min(min_rpm, start_rpm / 2, self.max_rpm)
        self.rpm = fixed_rps * 60 if mode == "fixed" else start_rpm
        self.capacity = max(1, burst)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self.ramp_every, self.ramp_step = ramp_every, ramp_step
        self._streak = 0

    @property
    def unlimited(self) -> bool:
        return self.mode == "unlimited" or self.rpm <= 0

    def _refill(self, now: float) -> None:
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rpm / 60.0)
        self.updated = now

    def wait_time(self, now: float) -> float:
        if self.unlimited:
            return 0.0
        self._refill(now)
        return 0.0 if self.tokens >= 1 else (1 - self.tokens) * 60.0 / self.rpm

    def consume(self, now: float) -> None:
        if not self.unlimited:
            self._refill(now)
            self.tokens -= 1

    def on_success(self) -> None:
        if self.mode != "adaptive":
            return
        self._streak += 1
        if self._streak >= self.ramp_every:
            self._streak = 0
            self.rpm = min(self.max_rpm, self.rpm + self.ramp_step)

    def on_block(self) -> None:
        self._streak = 0
        if self.mode == "adaptive":
            self.rpm = min(self.rpm, max(self.min_rpm, self.rpm * 0.5))

    @property
    def ceiling_rpm(self) -> float:
        if self.unlimited:
            return math.inf
        return self.max_rpm if self.mode == "adaptive" else self.rpm


# ----------------------------------------------------------------------------- proxies (compat helpers)
def load_proxy_file(path: str) -> list[str]:
    return [p for p in (normalise_proxy(l) for l in Path(path).read_text().splitlines()) if p]


def redact(proxy: str | None) -> str:
    return EgressSpec("proxy", proxy).label if proxy else "direct"


# ----------------------------------------------------------------------------- egress pool
@dataclass
class Egress:
    spec: EgressSpec
    client: httpx.AsyncClient
    limiter: AdaptiveLimiter
    failures: int = 0            # consecutive network failures since the last success (drives retirement)
    strikes: int = 0             # blocks, drives the exponential park cooldown; decays after clean successes
    retired: bool = False
    parked_until: float = 0.0
    ok: int = 0
    bad: int = 0
    blocks: int = 0
    clean_since_strike: int = 0
    last_event: str = ""
    clean_since_rate: int = 0
    rate_strikes: int = 0        # consecutive 429s (short, doubling backoff); reset after clean successes
    tor_streak: int = 0          # Tor rotations without a success in between (escalates the rotation rest)
    inflight: int = 0
    old_client: object = None
    retired_until: float = 0.0
    last_fail_at: float = -1e9

    @property
    def proxy(self) -> str | None:        # compat: proxy URL / relay template, None for direct
        return self.spec.url

    @property
    def name(self) -> str:
        return self.spec.label

    def state(self, now: float) -> str:
        if self.retired:
            return "retired"
        return "parked" if self.parked_until > now else "healthy"


class EgressPool:
    def __init__(self, specs: list[EgressSpec], *, limiter_factory, timeout: float, http2: bool,
                 max_failures: int, park_cooldown: float, park_max: float, max_park_wait: float,
                 transport_factory=None, strike_reset: int = 50, tor_rotate_cooldown: float = 10.0,
                 rate_cooldown: float = 45.0, rate_reset: int = 10, max_inflight_per_egress: int = 2):
        self.timeout, self.http2, self.transport_factory = timeout, http2, transport_factory
        self.max_inflight_per_egress = max(1, int(max_inflight_per_egress))
        self.tor_rotate_cooldown = tor_rotate_cooldown
        self.rate_cooldown, self.rate_reset = rate_cooldown, rate_reset
        self._closing: list = []
        self._close_tasks: dict = {}
        self._client_inflight: Counter = Counter()
        self._client_drained: dict = {}
        self._available = asyncio.Event()
        self._closed = False
        self._shutdown_task = None
        self.max_failures = max_failures
        self.park_cooldown = park_cooldown
        self.park_max = park_max
        self.max_park_wait = max_park_wait
        self.strike_reset = strike_reset
        self.egresses: list[Egress] = []
        for spec in specs or [EgressSpec("direct")]:
            self.egresses.append(Egress(spec, self._client(spec), limiter_factory(spec)))
        self._last_pick: dict[int, float] = {}
        self._last_pause_log = -1e9

    def _client(self, spec: EgressSpec) -> httpx.AsyncClient:
        if self.transport_factory:
            extra = {"transport": self.transport_factory(spec.url if spec.kind != "direct" else None)}
        elif spec.kind == "proxy":
            extra = {"proxy": spec.url}
        else:
            extra = {}
        return httpx.AsyncClient(**extra, http2=self.http2, timeout=self.timeout, follow_redirects=False,
                                 # Tor starts at one request every 6s; httpx's default 5s expiry
                                 # discards connections between ordinary requests on the same circuit.
                                 limits=httpx.Limits(max_connections=64, max_keepalive_connections=32,
                                                   keepalive_expiry=60.0))

    def _rotate_tor(self, eg: Egress, reason: str) -> float:
        if self._closed:
            return 0.0
        old = eg.client
        eg.spec = eg.spec.rotated()
        eg.client = self._client(eg.spec)
        # A read timeout limits idle reads, not total response duration. A fixed
        # grace timer can abort a request that is still making progress.
        eg.old_client = old
        self._close_later(old)
        # A new isolated circuit can reuse an exit IP; rest and retain server cooldowns. If rotations keep getting blocked without a
        # success in between (e.g. Tor without IsolateSOCKSAuth, or the whole exit pool is walled) the rest
        # doubles, capped at 5 min.
        rest = min(300.0, self.tor_rotate_cooldown * (2 ** eg.tor_streak)) if self.tor_rotate_cooldown else 0.0
        eg.tor_streak += 1
        eg.parked_until = max(eg.parked_until, time.monotonic() + rest)
        eg.failures = 0
        log.info("tor egress %s (%s): switched to a new circuit %s, resting %.0fs", eg.name, reason, eg.spec.label,
                 rest)
        return rest

    # -- selection
    async def acquire(self) -> Egress:
        await asyncio.sleep(0)             # yield once so timers (health log) and other tasks get to run
        while True:
            if self._closed:
                raise NoEgress("egress pool is closed")
            self._available.clear()
            now = time.monotonic()
            for e in self.egresses:            # BUG-5: retired proxies get a re-probe after REPROBE_AFTER
                if e.retired and e.retired_until and e.retired_until <= now:
                    e.retired, e.retired_until = False, 0.0
                    e.failures = max(0, self.max_failures - 1)     # one more failure retires it again
                    log.info("re-probing retired egress %s", e.name)
            live = [e for e in self.egresses if not e.retired]
            if not live:
                reprobes = [e.retired_until for e in self.egresses if e.retired_until > now]
                wait = min(reprobes) - now if reprobes else None
                if wait is None or (self.max_park_wait is not None and wait > self.max_park_wait):
                    raise NoEgress("all proxies/egresses retired; waiting for re-probe")
                await self._wait_available(min(5.0, max(0.001, wait)))
                continue
            unparked = [e for e in live if e.parked_until <= now]
            avail = [e for e in unparked if e.inflight < self.max_inflight_per_egress]
            if unparked and not avail:
                await self._wait_available(0.5)
                continue
            if not avail:
                # A retired lane can be ready for re-probe before any parked lane
                # recovers. Include it when choosing how long queued work waits.
                nxt = min([e.parked_until for e in live] +
                          [e.retired_until for e in self.egresses if e.retired and e.retired_until > now])
                if self.max_park_wait is not None and nxt - now > self.max_park_wait:
                    raise NoEgress(f"all {len(live)} egress(es) parked after blocks; next one free in "
                                   f"{nxt - now:.0f}s (> max wait {self.max_park_wait:.0f}s)")
                # BUG-4: pause (never fail queued items) until the earliest egress is free again
                if now - self._last_pause_log > 60:
                    self._last_pause_log = now
                    log.warning("all %d egress(es) parked: pausing %.0fs until the next one is free",
                                len(live), nxt - now)
                await self._wait_available(min(5.0, max(0.001, nxt - now)))
                continue
            # the egress that can send soonest; ties -> least recently used (spreads load = sum of rates)
            best = min(avail, key=lambda e: (e.limiter.wait_time(now), self._last_pick.get(id(e), 0.0)))
            w = best.limiter.wait_time(now)
            if w <= 0:
                best.inflight += 1
                self._client_inflight[best.client] += 1
                self._client_drained.setdefault(best.client, asyncio.Event()).clear()
                best.limiter.consume(now)
                self._last_pick[id(best)] = now
                return best
            await self._wait_available(min(w, 0.5))

    async def _wait_available(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._available.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def release(self, eg: Egress, client=None) -> None:
        eg.inflight = max(0, eg.inflight - 1)
        client = eg.client if client is None else client
        if self._client_inflight[client] > 0:
            self._client_inflight[client] -= 1
            if not self._client_inflight[client]:
                del self._client_inflight[client]
                self._client_drained[client].set()
        self._available.set()

    # -- outcomes
    def success(self, eg: Egress) -> None:
        eg.failures = 0
        eg.ok += 1
        eg.last_event = "ok"
        eg.limiter.on_success()
        eg.tor_streak = 0
        if eg.rate_strikes:
            eg.clean_since_rate += 1
            if eg.clean_since_rate >= self.rate_reset:
                eg.rate_strikes, eg.clean_since_rate = 0, 0
        if eg.strikes:
            eg.clean_since_strike += 1
            if eg.clean_since_strike >= self.strike_reset:
                eg.strikes, eg.clean_since_strike = 0, 0

    def block(self, eg: Egress, reason: str, severity: str = "block", retry_after: float | None = None) -> float:
        """Take an egress out of rotation after Instagram pushed back. Returns the rest in seconds.

        severity="rate" (HTTP 429): short backoff (rate_cooldown, doubling per consecutive 429, capped at
        park_cooldown; a larger Retry-After wins) and the egress rate halves.
        severity="block" (401 "please wait", 403, login redirect/wall): long park (park_cooldown, doubling per
        repeat, capped at park_max).
        Tor egresses rotate circuits, which may reuse exit IPs; server cooldowns still apply.
        Blocks never retire an egress (the IP recovers); only consecutive network failures do."""
        eg.bad += 1
        eg.blocks += 1
        eg.last_event = reason
        now = time.monotonic()
        if eg.parked_until > now:
            # BUG-6: requests that were in flight when the egress got walled -> same block event, no new strike
            if retry_after is not None:
                eg.parked_until = max(eg.parked_until, now + retry_after)
            return eg.parked_until - now
        if eg.spec.tor:
            if severity == "rate":
                eg.limiter.on_block()
            rest = self._rotate_tor(eg, reason)
            if retry_after is not None:
                rest = max(rest, retry_after)
                eg.parked_until = max(eg.parked_until, now + rest)
            return rest
        if severity == "rate":
            eg.rate_strikes += 1
            eg.clean_since_rate = 0
            cd = min(self.park_cooldown, self.rate_cooldown * (2 ** (eg.rate_strikes - 1)))
            if retry_after:
                cd = max(cd, float(retry_after))
            eg.parked_until = max(eg.parked_until, time.monotonic() + cd)
            eg.limiter.on_block()
            log.warning("egress %s rate-limited: backing off %.0fs (%s; 429 #%d, rate now %.0f rpm)",
                        eg.name, cd, reason, eg.rate_strikes, eg.limiter.rpm)
            return cd
        eg.strikes += 1
        eg.clean_since_strike = 0
        cd = min(self.park_max, self.park_cooldown * (2 ** (eg.strikes - 1)))
        if retry_after is not None:
            cd = max(cd, retry_after)
        eg.parked_until = time.monotonic() + cd
        eg.last_event = reason
        eg.limiter.on_block()
        if True:
            log.warning("egress %s parked for %.0fs (%s; strike %d, rate now %.0f rpm)",
                        eg.name, cd, reason, eg.strikes, eg.limiter.rpm)
        return cd

    def network_failure(self, eg: Egress, reason: str, started: float | None = None) -> None:
        eg.bad += 1
        eg.last_event = reason
        now = time.monotonic()
        if started is not None and started < eg.last_fail_at:
            return                  # BUG-5: was in flight when another failure landed -> same event, not "consecutive"
        eg.last_fail_at = now
        eg.failures += 1
        if eg.spec.tor and not eg.retired:
            if eg.failures >= self.max_failures:      # a dead/slow circuit: build a new one, don't retire
                self._rotate_tor(eg, reason)
            return
        # transient network error: the request is retried at once on another egress; this one only rests
        # (briefly, growing) when failures repeat back-to-back. Retired after max_failures in a row.
        if eg.failures >= 2:
            eg.parked_until = max(eg.parked_until, time.monotonic() + min(30.0, 2.0 * (eg.failures - 1)))
        self._maybe_retire(eg, reason)

    def transient(self, eg: Egress, reason: str, retry_after: float | None = None) -> None:       # 5xx: short rest, not a block
        eg.bad += 1
        eg.last_event = reason
        eg.parked_until = max(eg.parked_until, time.monotonic() + max(min(self.park_cooldown, 5.0), retry_after or 0))

    # backwards-compatible alias
    def failure(self, eg: Egress, reason: str, *a, **k) -> None:
        self.network_failure(eg, reason)

    def _maybe_retire(self, eg: Egress, reason: str) -> None:
        if not eg.retired and eg.spec.kind != "direct" and eg.failures >= self.max_failures:
            eg.retired = True
            eg.retired_until = time.monotonic() + REPROBE_AFTER
            log.warning("retiring egress %s after %d consecutive failures (%s)", eg.name, eg.failures, reason)

    # -- reporting
    def health(self) -> list[dict]:
        now = time.monotonic()
        return [{"egress": e.name, "kind": e.spec.kind, "state": e.state(now),
                 "rpm": None if e.limiter.unlimited else round(e.limiter.rpm, 1),
                 "ok": e.ok, "failed": e.bad, "blocks": e.blocks, "strikes": e.strikes,
                 "parkedForS": round(e.parked_until - now) if e.parked_until > now else 0,
                 "inflight": e.inflight, "maxInflight": self.max_inflight_per_egress,
                 "capacityEstimateOnly": True, "last": e.last_event} for e in self.egresses]

    def healthy_count(self) -> int:
        now = time.monotonic()
        return sum(1 for e in self.egresses if not e.retired and e.parked_until <= now)

    def capacity_profiles_per_min(self, include_parked: bool = False, req_per_profile: float | None = None) -> float:
        now = time.monotonic()
        tot = 0.0
        for e in self.egresses:
            if e.retired or (not include_parked and e.parked_until > now):
                continue
            tot += e.limiter.ceiling_rpm
        return tot / (req_per_profile or REQUESTS_PER_PROFILE)

    def _close_later(self, client) -> None:
        if client in self._closing:
            return
        self._closing.append(client)

        async def _close():
            if self._client_inflight.get(client, 0):
                await self._client_drained[client].wait()
            try:
                if not client.is_closed:
                    await client.aclose()
            finally:
                self._closing.remove(client)
                self._close_tasks.pop(client, None)
                self._client_drained.pop(client, None)
                self._client_inflight.pop(client, None)
                for eg in self.egresses:
                    if eg.old_client is client:
                        eg.old_client = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:               # no loop: aclose() will handle it
            return
        self._close_tasks[client] = loop.create_task(_close())

    async def aclose(self) -> None:
        if self._shutdown_task is None:
            self._closed = True
            self._available.set()
            self._shutdown_task = asyncio.create_task(self._shutdown())
        # A cancelled caller must not strand partially closed clients. Another
        # caller may await the same cleanup, including after cancellation.
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self) -> None:
        # Explicit pool shutdown may abort active requests; routine rotation never does.
        clients = list(dict.fromkeys([e.client for e in self.egresses] + self._closing))
        tasks = list(self._close_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)
        self._closing.clear()
        self._close_tasks.clear()
        self._client_drained.clear()
        self._client_inflight.clear()


# ----------------------------------------------------------------------------- fetch with retry
@dataclass
class Stats:
    requests: int = 0
    statuses: Counter = field(default_factory=Counter)
    retries: int = 0
    paths: Counter = field(default_factory=Counter)
    started: float = field(default_factory=time.monotonic)
    first_throttle: dict | None = None   # first 429 / 401 / 403 / login wall seen in this run
    cache_hits: int = 0

    def note(self, status, egress: str) -> None:
        self.requests += 1
        self.statuses[status] += 1
        if self.first_throttle is None and status in (429, 401, 403, "login_wall_html"):
            self.first_throttle = {"status": status, "request_no": self.requests, "egress": egress,
                                   "after_seconds": round(time.monotonic() - self.started, 2)}

    def as_dict(self) -> dict:
        return {"requests": self.requests, "retries": self.retries, "statuses": dict(self.statuses),
                "paths": dict(self.paths), "cacheHits": self.cache_hits, "firstThrottle": self.first_throttle}


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """HTTP Retry-After supports both delta seconds and an HTTP-date; never cap server waits."""
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return max(0.0, (deadline - (now or datetime.now(timezone.utc))).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _is_instagram_json(r: httpx.Response) -> bool:
    if "json" not in r.headers.get("content-type", ""):
        return False
    try:
        j = r.json()
    except ValueError:
        return False
    return isinstance(j, dict) and ("status" in j or "message" in j)


def _envelope_error(data: object) -> ScrapeError | None:
    """Classify explicit Instagram failure envelopes before recording a successful request."""
    if not isinstance(data, dict):
        return BadResponse("JSON response was not an object")
    message = str(data.get("message") or data.get("error_type") or data.get("error") or "").lower()
    if (data.get("challenge") or data.get("checkpoint_url") or
            any(mark in message for mark in ("login_required", "login required", "challenge_required",
                                               "checkpoint_required", "please log in"))):
        return LoginWall("Instagram login or challenge envelope")
    if any(mark in message for mark in ("rate limit", "too many requests", "please wait a few minutes")):
        return RateLimited("Instagram rate-limit envelope")
    if data.get("status") in ("fail", "error") or data.get("errors") or data.get("error"):
        return BadResponse("Instagram failure envelope")
    return None


def _confirmed_not_found(r: httpx.Response) -> bool:
    """A bare 404 (often from a relay) does not prove that a profile is absent."""
    if not _is_instagram_json(r):
        return False
    data = r.json()
    message = str(data.get("message") or "").lower()
    return data.get("status") == "fail" and any(
        mark in message for mark in ("not found", "does not exist", "doesn't exist", "user not found"))


def _looks_login_wall(r: httpx.Response) -> bool:
    if r.status_code in (401, 403):
        return True
    if r.status_code in (301, 302) and "/accounts/login" in r.headers.get("location", ""):
        return True
    return False


class Fetcher:
    def __init__(self, pool: EgressPool, *, max_retries: int, backoff_base: float, backoff_max: float, stats: Stats):
        self.pool = pool
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.stats = stats

    def _backoff(self, attempt: int, retry_after: str | None = None) -> float:
        delay = retry_after_seconds(retry_after)
        if delay is not None:
            return delay
        base = min(self.backoff_max, self.backoff_base * (2 ** attempt))
        return base * random.uniform(0.5, 1.5)

    async def get(self, url: str, headers: dict, *, expect_json: bool, endpoint: str = "default",
                  retries: int | None = None, method: str = "GET", content: str | None = None) -> httpx.Response:
        """GET with retries. Blocks (401/403/429/login wall) park the egress and the retry goes to another
        healthy egress; network errors count toward retiring a proxy/relay; 5xx back off briefly."""
        last: Exception | None = None
        max_retries = self.max_retries if retries is None else retries
        for attempt in range(max_retries + 1):
            eg = await self.pool.acquire()
            h = {**headers, **eg.spec.headers} if eg.spec.headers else headers
            if eg.spec.kind == "relay" and ("Cookie" in h or "X-CSRFToken" in h):
                # never hand the Instagram session to a third-party relay host
                h = {k: v for k, v in h.items() if k not in ("Cookie", "X-CSRFToken")}
                if not getattr(self, "_relay_cookie_warned", False):
                    self._relay_cookie_warned = True
                    log.warning("relay egress %s: session cookie NOT forwarded to the relay host "
                                "(requests via relays are anonymous)", eg.name)
            retry_after = None
            started = time.monotonic()
            client = eg.client
            try:
                if method == "GET":
                    r = await asyncio.wait_for(client.get(eg.spec.target_url(url), headers=h), timeout=self.pool.timeout)
                else:
                    r = await asyncio.wait_for(client.request(method, eg.spec.target_url(url), headers=h,
                                                              content=content), timeout=self.pool.timeout)
            except (httpx.TransportError, httpx.ProxyError, asyncio.TimeoutError) as e:
                self.pool.release(eg, client)
                self.stats.note("network_error", eg.name)
                if eg.client is client:
                    self.pool.network_failure(eg, f"network: {type(e).__name__}", started)
                last = e
            except BaseException:
                self.pool.release(eg, client)
                raise
            else:
                self.pool.release(eg, client)
                client.cookies.clear()          # stay anonymous: never replay cookies IG sets on responses
                sc = r.status_code
                ctype = r.headers.get("content-type", "")
                server_wait = retry_after_seconds(r.headers.get("retry-after"))
                html_wall = sc == 200 and expect_json and "json" not in ctype and "javascript" not in ctype
                self.stats.note("login_wall_html" if html_wall else sc, eg.name)
                envelope_error = None
                if sc == 200 and not html_wall:
                    if expect_json:
                        try:
                            data = r.json()
                        except ValueError:
                            if eg.client is client:
                                self.pool.transient(eg, "malformed json", retry_after=server_wait)
                            last = BadResponse("malformed JSON body (HTTP 200)")
                            if attempt < max_retries:
                                self.stats.retries += 1
                                await asyncio.sleep(self._backoff(attempt))
                            continue
                        envelope_error = _envelope_error(data)
                    if envelope_error is None:
                        if eg.client is client:
                            self.pool.success(eg)
                        return r
                if envelope_error is not None:
                    if isinstance(envelope_error, (LoginWall, RateLimited)):
                        if eg.client is client or (server_wait is not None and server_wait > 0):
                            if eg.client is not client:
                                eg.parked_until = max(eg.parked_until, time.monotonic() + server_wait)
                            self.pool.block(eg, "Instagram failure envelope",
                                            severity="rate" if isinstance(envelope_error, RateLimited) else "block",
                                            retry_after=server_wait)
                    elif server_wait is not None and server_wait > 0:
                        self.pool.transient(eg, "Instagram failure envelope", retry_after=server_wait)
                    last = envelope_error
                elif sc == 404:
                    if _confirmed_not_found(r):
                        if eg.client is client:
                            self.pool.success(eg)
                        raise NotFound("profile not found (Instagram 404 envelope)")
                    if server_wait is not None and server_wait > 0:
                        self.pool.transient(eg, "unconfirmed 404", retry_after=server_wait)
                    last = BadResponse("HTTP 404 without confirmed Instagram not-found envelope")
                elif sc == 400 and not _is_instagram_json(r):
                    # BUG-3: a 400 that is not Instagram JSON (proxy/relay error page, HTTP/1.1 rejection) is an
                    # egress failure: retry the username elsewhere, count toward retiring that egress.
                    self.stats.statuses["egress_400"] += 1
                    if eg.client is client:
                        self.pool.network_failure(eg, "non-Instagram 400", started)
                    last = BadResponse(f"HTTP 400 from egress (not Instagram): {r.text[:120]}")
                elif sc == 400:
                    if eg.client is client:
                        self.pool.success(eg)
                    body = r.text[:300]
                    if "has been deleted" in body or "schema" in body:
                        raise SchemaError(body)
                    if "useragent mismatch" in body:
                        raise ScrapeError(f"HTTP 400: {body}")
                    raise ScrapeError(f"HTTP 400: {body}")
                elif sc == 429:
                    retry_after = r.headers.get("retry-after")
                    ra = server_wait
                    if eg.client is client or (ra is not None and ra > 0):
                        if eg.client is not client:
                            # Preserve a late server deadline without rotating the replacement
                            # circuit or adding another strike. The normal hook persists it.
                            eg.parked_until = max(eg.parked_until, time.monotonic() + ra)
                        self.pool.block(eg, "http 429", severity="rate", retry_after=ra)
                    last = RateLimited("HTTP 429")
                    retry_after = None   # honoured by parking THIS egress; the retry goes elsewhere now
                elif sc >= 500:
                    retry_after = r.headers.get("retry-after")
                    if eg.client is client or (server_wait is not None and server_wait > 0):
                        self.pool.transient(eg, f"http {sc}", retry_after=server_wait)
                    last = UpstreamError(f"HTTP {sc}")
                elif html_wall or _looks_login_wall(r):
                    if eg.client is client or (server_wait is not None and server_wait > 0):
                        if eg.client is not client:
                            eg.parked_until = max(eg.parked_until, time.monotonic() + server_wait)
                        self.pool.block(eg, f"login wall {sc}", retry_after=server_wait)
                    msg = r.text[:200] if "json" in ctype else f"HTTP {sc}"
                    last = LoginWall(f"HTTP {sc}: {msg}")
                else:
                    raise ScrapeError(f"unexpected HTTP {sc}")
            if attempt < max_retries:                # no pointless sleep after the final attempt
                self.stats.retries += 1
                if isinstance(last, (RateLimited, LoginWall, UpstreamError, httpx.TransportError, httpx.ProxyError)):
                    # the failing egress is parked/resting; retry quickly on the next available one
                    await asyncio.sleep(min(self.backoff_base * 0.25, 0.5) * random.uniform(0.5, 1.5))
                else:
                    await asyncio.sleep(self._backoff(attempt, retry_after))
        assert last is not None
        if isinstance(last, ScrapeError):
            raise last
        raise ScrapeError(f"network error after retries: {last!r}")
