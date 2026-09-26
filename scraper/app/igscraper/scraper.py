"""Orchestration: Apify-compatible input -> list of Apify-shaped profile items."""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import logging
import time

import httpx
from urllib.parse import urlencode
from typing import Any, Awaitable, Callable, Iterable

from . import mapper
from .auth import build_cookies, cookie_header
from .config import ScraperConfig
from .inputs import Target, dedupe, parse_target, target_key
from .egress import EgressSpec, normalise_proxy
from .net import (AdaptiveLimiter, BadResponse, EgressPool, Fetcher, LoginWall, NotFound, RateLimited, SchemaError, ScrapeError,
                  Stats, load_proxy_file)
from .store import Store

log = logging.getLogger("igscraper")

API = "https://i.instagram.com/api/v1"
WEB_API = "https://www.instagram.com/api/v1"

# Logged-out: the Instagram Android app identity. Verified 2026-09-24 that web_profile_info answers
# this way from a datacenter IP while the browser identity (app id 936619743392459) gets 401.
ANDROID_UA = ("Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2400; samsung; SM-G991B; o1s; "
              "exynos2100; en_US; 458229237)")
ANDROID_APP_ID = "567067343352427"
# Threads' app id: web_profile_info answers for profiles that 400 under the IG app id, but returns
# Threads-flavoured data (no posts, no category, Threads following count), so it only enriches the fallback.
# Threads-web app id 238260118697367 returns 400 NodeInvalidTypeException since 2026-09-24; the Barcelona
# (Threads Android) identity still answers web_profile_info for profiles hit by the subvertical-400 bug.
BARCELONA_APP_ID = "3419628305025917"
BARCELONA_UA = ("Barcelona 289.0.0.77.109 Android (33/13; 420dpi; 1080x2400; samsung; SM-G991B; o1s; "
                "exynos2100; en_US; 489720145)")
THREADS_APP_ID = BARCELONA_APP_ID      # backwards-compatible name
GQL_URL = "https://www.instagram.com/graphql/query"
GQL_PROFILE_DOC_ID = "27937681195819736"   # PolarisProfilePageContentQuery, by user id, works logged-out
GQL_PV = {"__relay_internal__pv__PolarisCannesGuardianExperienceEnabledrelayprovider": True,
          "__relay_internal__pv__PolarisCASB976ProfileEnabledrelayprovider": False,
          "__relay_internal__pv__PolarisRepostsConsumptionEnabledrelayprovider": False,
          "__relay_internal__pv__PolarisWebSchoolsEnabledrelayprovider": False}
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/128.0.0.0 Safari/537.36")
WEB_APP_ID = "936619743392459"
# Instagram server-renders the profile (counts + bio) for crawlers.
CRAWLER_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

ItemCallback = Callable[[dict], Awaitable[None] | None]


class ProfileScraper:
    def __init__(self, config: ScraperConfig | None = None, transport_factory=None):
        self.cfg = config or ScraperConfig()
        self._transport_factory = transport_factory   # tests: callable(proxy|None) -> httpx transport
        self.stats = Stats()
        self.last_run_counts: dict = {}
        self.cookies = build_cookies(self.cfg.sessionid, self.cfg.cookies_file)
        self.logged_in = "sessionid" in self.cookies
        self._pool: EgressPool | None = None
        self._fetch: Fetcher | None = None

    # ------------------------------------------------------------------ lifecycle
    def egress_specs(self) -> list[EgressSpec]:
        specs = list(self.cfg.egresses)
        specs += [EgressSpec("proxy", p) for p in (normalise_proxy(x) for x in self.cfg.proxies) if p]
        if not specs or (self.cfg.use_direct_with_proxies and not any(e.kind == "direct" for e in specs)):
            specs.append(EgressSpec("direct"))
        return specs

    def _limiter(self, spec: EgressSpec) -> AdaptiveLimiter:
        r = self.cfg.rate_per_ip
        if r is None:
            return AdaptiveLimiter(mode="adaptive", start_rpm=spec.start_rpm or self.cfg.start_rpm,
                                   max_rpm=spec.max_rpm or self.cfg.max_rpm, burst=self.cfg.burst)
        if r <= 0:
            return AdaptiveLimiter(mode="unlimited")
        return AdaptiveLimiter(mode="fixed", fixed_rps=r, burst=self.cfg.burst)

    async def __aenter__(self) -> "ProfileScraper":
        specs = self.egress_specs()
        self._pool = EgressPool(
            specs, limiter_factory=self._limiter, timeout=self.cfg.timeout, http2=self.cfg.http2,
            max_failures=self.cfg.proxy_max_failures, park_cooldown=self.cfg.proxy_cooldown,
            park_max=self.cfg.park_max, max_park_wait=self.cfg.max_park_wait,
            rate_cooldown=self.cfg.rate_limit_cooldown,
            max_inflight_per_egress=self.cfg.max_inflight_per_egress,
            transport_factory=self._transport_factory,
        )
        self._fetch = Fetcher(self._pool, max_retries=self.cfg.max_retries, backoff_base=self.cfg.backoff_base,
                              backoff_max=self.cfg.backoff_max, stats=self.stats)
        self.store = Store(self.cfg.cache_path) if self.cfg.cache_path else None
        if self.cfg.job and not self.store:
            raise ValueError("a resumable job needs cache_path (sqlite file)")
        self.check_target_rate()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._pool:
            await self._pool.aclose()
        if getattr(self, "store", None):
            self.store.close()

    @property
    def concurrency(self) -> int:
        if self.cfg.concurrency:
            return max(1, self.cfg.concurrency)
        n = len(self._pool.egresses) if self._pool else 1
        return min(64, max(8, 4 * n))

    def check_target_rate(self) -> bool:
        """Warn when the healthy egresses' combined ceiling can't reach --target-rate (profiles/min)."""
        if not self.cfg.target_rate or not self._pool:
            return True
        done = sum(v for k, v in self.stats.paths.items() if k not in ("cache", "error"))
        rpp = max(1.0, self.stats.requests / done) if done >= 20 else None   # measured once we have data
        cap = self._pool.capacity_profiles_per_min(req_per_profile=rpp)
        healthy = sum(1 for h in self._pool.health() if h["state"] == "healthy")
        if cap < self.cfg.target_rate:
            log.warning("target rate %.0f profiles/min is NOT reachable: %d healthy egress(es) give at most "
                        "~%.0f profiles/min (per-egress cap %.0f req/min, ~%.1f req/profile). Add egresses.",
                        self.cfg.target_rate, healthy, cap, self.cfg.max_rpm, rpp or 1.5)
            return False
        return True

    @property
    def proxy_report(self) -> list[dict]:
        return [{"proxy": e.name, "ok": e.ok, "failed": e.bad, "retired": e.retired}
                for e in (self._pool.egresses if self._pool else [])]

    def health(self) -> list[dict]:
        return self._pool.health() if self._pool else []

    def healthy_count(self) -> int:
        """Number of egresses currently usable (not parked, not retired)."""
        return self._pool.healthy_count() if self._pool else 0

    async def _health_loop(self) -> None:
        last_ok = True
        while True:
            await asyncio.sleep(self.cfg.health_interval)
            for h in self.health():
                log.info("egress %-28s %-8s rpm=%-6s ok=%-5d failed=%-4d blocks=%d parked=%ss last=%s",
                         h["egress"], h["state"], h["rpm"], h["ok"], h["failed"], h["blocks"], h["parkedForS"],
                         h["last"])
            ok = self.check_target_rate()
            if ok and not last_ok:
                log.info("target rate reachable again")
            last_ok = ok

    # ------------------------------------------------------------------ headers
    def _api_headers(self) -> dict:
        if self.logged_in:
            h = {"User-Agent": WEB_UA, "X-IG-App-ID": WEB_APP_ID, "X-Requested-With": "XMLHttpRequest",
                 "Referer": "https://www.instagram.com/", "Accept": "*/*",
                 "Cookie": cookie_header(self.cookies)}
            if "csrftoken" in self.cookies:
                h["X-CSRFToken"] = self.cookies["csrftoken"]
            return h
        return {"User-Agent": ANDROID_UA, "X-IG-App-ID": ANDROID_APP_ID, "Accept": "*/*",
                "Accept-Language": "en-US"}

    def _api_base(self) -> str:
        return WEB_API if self.logged_in else API

    # ------------------------------------------------------------------ endpoints
    async def fetch_web_profile_info(self, username: str, app_id: str | None = None,
                                     retries: int | None = None) -> dict:
        headers = self._api_headers()
        base = self._api_base()
        if app_id:
            ua = BARCELONA_UA if app_id == BARCELONA_APP_ID else ANDROID_UA
            headers = {"User-Agent": ua, "X-IG-App-ID": app_id, "Accept": "*/*", "Accept-Language": "en-US"}
            base = API
        r = await self._fetch.get(f"{base}/users/web_profile_info/?username={username}", headers, expect_json=True,
                                  endpoint="wpi_threads" if app_id else "wpi", retries=retries)
        data = r.json()
        if not isinstance(data, dict) or data.get("status") == "fail" or data.get("errors"):
            raise BadResponse("web_profile_info returned an upstream failure envelope")
        user = (data.get("data") or {}).get("user")
        if not user:
            raise BadResponse("web_profile_info returned no user; absence is not confirmed")
        if not isinstance(user, dict) or not user.get("username") or not user.get("id"):
            raise BadResponse("web_profile_info returned an incomplete identity")
        if str(user["username"]).casefold() != username.casefold():
            raise BadResponse("web_profile_info identity does not match the requested username")
        return user

    async def fetch_feed(self, username: str, retries: int | None = None) -> dict:
        r = await self._fetch.get(f"{self._api_base()}/feed/user/{username}/username/?count=12",
                                  self._api_headers(), expect_json=True, endpoint="feed",
                                  retries=retries)
        data = r.json()
        if not isinstance(data, dict) or data.get("status") == "fail":
            raise BadResponse("feed returned an upstream failure envelope")
        user = data.get("user") or ((data.get("items") or [{}])[0].get("user") or {})
        if user.get("username") and str(user["username"]).casefold() != username.casefold():
            raise BadResponse("feed identity does not match the requested username")
        return data

    async def _gql_token_pair(self, retries: int | None = None) -> dict:
        """csrftoken + LSD from ONE logged-out homepage GET; reused for 30 min."""
        tok = getattr(self, "_gql_tokens", None)
        if tok and time.monotonic() - tok["ts"] < 1800:
            return tok
        r = await self._fetch.get("https://www.instagram.com/", {"User-Agent": WEB_UA, "Accept": "text/html",
                                                                "Accept-Language": "en-US,en;q=0.9"},
                                  expect_json=False, endpoint="home", retries=retries)
        txt = r.text
        m = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', txt)
        csrf = r.cookies.get("csrftoken")
        if not csrf:
            mc = re.search(r'"csrf_token":"([^"]+)"', txt)
            csrf = mc.group(1) if mc else None
        if not m or not csrf:
            raise BadResponse("homepage without LSD/csrf token (GraphQL fallback unavailable)")
        self._gql_tokens = {"lsd": m.group(1), "csrf": csrf, "ts": time.monotonic()}
        return self._gql_tokens

    async def fetch_gql_profile(self, user_id: str, retries: int | None = None) -> dict:
        """PolarisProfilePageContentQuery by id (~5 KB): followers, following, bio, bio links, external url."""
        tok = await self._gql_token_pair(retries)
        variables = {"id": str(user_id), "render_surface": "PROFILE", "enable_integrity_filters": True, **GQL_PV}
        body = urlencode({"doc_id": GQL_PROFILE_DOC_ID, "lsd": tok["lsd"], "server_timestamps": "true",
                          "variables": json.dumps(variables, separators=(",", ":")),
                          "fb_api_req_friendly_name": "PolarisProfilePageContentQuery"})
        h = {"User-Agent": WEB_UA, "X-CSRFToken": tok["csrf"], "X-FB-LSD": tok["lsd"],
             "X-FB-Friendly-Name": "PolarisProfilePageContentQuery", "X-Requested-With": "XMLHttpRequest",
             "Content-Type": "application/x-www-form-urlencoded", "Origin": "https://www.instagram.com",
             "Referer": "https://www.instagram.com/", "Accept": "*/*", "Cookie": f"csrftoken={tok['csrf']}"}
        r = await self._fetch.get(GQL_URL, h, expect_json=True, endpoint="gql_profile", retries=retries,
                                  method="POST", content=body)
        user = ((r.json() or {}).get("data") or {}).get("user")
        if not user:
            self._gql_tokens = None          # stale tokens are the usual cause; refresh next time
            raise BadResponse("GraphQL profile query returned no user")
        if str(user.get("pk") or user.get("id") or "") != str(user_id):
            raise BadResponse("GraphQL identity does not match the requested profile")
        return user

    async def fetch_profile_html(self, username: str, retries: int | None = None) -> dict:
        r = await self._fetch.get(f"https://www.instagram.com/{username}/",
                                  {"User-Agent": CRAWLER_UA, "Accept-Language": "en-US"}, expect_json=False,
                                  endpoint="html", retries=retries)
        user = mapper.parse_profile_html(r.text, username=username)
        if user.get("username") and str(user["username"]).casefold() != username.casefold():
            raise BadResponse("HTML identity does not match the requested username")
        return user

    async def resolve_user_id(self, user_id: str) -> str:
        r = await self._fetch.get(f"{API}/users/{user_id}/info/",
                                  {"User-Agent": ANDROID_UA, "Accept": "*/*"}, expect_json=True, endpoint="info")
        uname = ((r.json() or {}).get("user") or {}).get("username")
        if not uname:
            raise NotFound(f"no profile with id {user_id}")
        return uname

    # ------------------------------------------------------------------ one profile
    async def scrape_target(self, target: Target, include_about: bool | None = None) -> dict:
        about = self.cfg.include_about if include_about is None else include_about
        username = target.username
        resolved_id = False
        try:
            if not username:
                try:
                    username = await self.resolve_user_id(target.user_id)
                    resolved_id = True
                except NotFound:
                    if target.raw.strip().isdigit():       # could be a numeric *username*
                        username = target.raw.strip()
                    else:
                        raise
            try:
                user = await self.fetch_web_profile_info(username)
                if resolved_id and mapper.instagram_id(user) != target.user_id:
                    raise BadResponse("resolved profile id does not match the requested id")
                if self.cfg.fallback and not isinstance(user.get("biography"), str):
                    raise SchemaError("web_profile_info did not expose the Instagram biography")
                item = mapper.with_coverage(mapper.map_web_profile_info(user, target.input_url, about), "web_profile_info")
                self.stats.paths["error" if item.get("error") else "web_profile_info"] += 1
                return item
            except (SchemaError, LoginWall, RateLimited) as primary:
                if not self.cfg.fallback:
                    raise
                # Throttled (not a schema bug): only fall back if some egress is still healthy, and then
                # with a single attempt per endpoint, so a blocked run doesn't multiply its own traffic.
                throttled = not isinstance(primary, SchemaError)
                if throttled and self._pool.healthy_count() == 0:
                    raise
                fb_retries = 0 if throttled else None
                log.info("%s: web_profile_info failed (%s: %s); using feed+html fallback",
                         username, type(primary).__name__, str(primary)[:120])
                # Fallback across rate buckets (feed / GraphQL are separate from web_profile_info):
                #   feed (12 posts + id) and Barcelona-identity wpi (highlights, IGTV, related) in parallel,
                #   then GraphQL profile by id (followers, following, bio, links); SSR HTML only if GraphQL fails.
                feed, thr = await asyncio.gather(
                    self.fetch_feed(username, fb_retries),
                    self.fetch_web_profile_info(username, app_id=BARCELONA_APP_ID, retries=fb_retries),
                    return_exceptions=True)
                feed_ok = isinstance(feed, dict) and bool(feed.get("user") or feed.get("items"))
                thr = thr if isinstance(thr, dict) else None
                fu = (feed.get("user") or ((feed.get("items") or [{}])[0].get("user")) or {}) if feed_ok else {}
                uid = mapper.instagram_id(fu) or mapper.instagram_id(thr)
                gql = page = None
                if uid and self.cfg.gql_fallback:
                    try:
                        gql = await self.fetch_gql_profile(uid, fb_retries)
                    except (ScrapeError, httpx.HTTPError) as e:
                        log.info("%s: GraphQL profile fallback failed (%s)", username, str(e)[:120])
                if gql is None or not isinstance(gql.get("biography"), str):
                    page = await asyncio.gather(self.fetch_profile_html(username, fb_retries),
                                                return_exceptions=True)
                    page = page[0]
                page_ok = isinstance(page, dict) and bool(page.get("username"))
                if page_ok and not mapper.same_profile(page, uid, username, html=True):
                    log.warning("%s: HTML profile identity disagrees with feed/Threads; ignoring HTML", username)
                    page_ok = False
                if not feed_ok and not page_ok and gql is None and thr is None:
                    if isinstance(feed, dict) and feed.get("status") == "ok" and not feed.get("items") \
                            and isinstance(page, dict) and not page.get("username"):
                        raise BadResponse("empty feed and no SSR profile; absence is not confirmed")
                    err = feed if isinstance(feed, Exception) else page if isinstance(page, Exception) else primary
                    raise err
                source = ("fallback_feed_gql" if gql and isinstance(gql.get("biography"), str)
                          else "fallback_feed_html")
                item = mapper.map_fallback(target.input_url, feed if feed_ok else None,
                                           page if page_ok else None, about, threads_user=thr, gql_user=gql)
                if resolved_id and item.get("id") != target.user_id:
                    raise BadResponse("fallback profile id does not match the requested id")
                # A bio without an Instagram id cannot be imported by the durable bridge as a verified profile.
                observed = bool(item.get("id")) and (
                    (mapper.same_profile(gql, item["id"], item["username"]) and
                     isinstance((gql or {}).get("biography"), str)) or
                    (page_ok and mapper.same_profile(page, item["id"], item["username"], html=True) and
                     isinstance(page.get("biography"), str)))
                item = mapper.with_coverage(item, source, bio_observed=observed)
                self.stats.paths["error" if item.get("error") else source] += 1
                return item
        except ScrapeError as e:
            self.stats.paths["error"] += 1
            return error_item(target, username, e.code, str(e))
        except Exception as e:  # never crash the run for one profile
            self.stats.paths["error"] += 1
            log.exception("unexpected error for %s", target.raw)
            return error_item(target, username, "unexpected_error", repr(e))

    # ------------------------------------------------------------------ many profiles
    def _cache_lookup(self, raw: str, include_about: bool = False) -> dict | None:
        if not self.store:
            return None
        try:
            t = parse_target(raw)
        except ValueError:
            return None
        key = f"u:{t.username}" if t.username else f"id:{t.user_id}"
        hit = self.store.cache_get(key, self.cfg.cache_ttl)
        if hit is not None:
            coverage = hit.get("_scrape") or {}
            if (hit.get("error") or not isinstance(hit.get("biography"), str)
                    or coverage.get("bioStatus") not in ("observed", "empty") or not coverage.get("observedAt")):
                return None
            if include_about and "about" not in hit:
                return None                      # cached without the about section -> refetch
            hit = dict(hit)
            if not include_about:
                hit.pop("about", None)
            hit["inputUrl"] = t.input_url
            hit["_scrape"] = {**hit.get("_scrape", {}), "cacheHit": True}
        return hit

    async def run(self, usernames: Iterable[str], on_item: ItemCallback | None = None, *,
                  include_about: bool | None = None, concurrency: int | None = None,
                  on_start: Callable[[int, int], Any] | None = None,
                  return_partial_on_cancel: bool = False, collect: bool = True) -> list[dict]:
        # The complete run, including callback acknowledgement, is one named-job
        # ownership interval. The OS releases the lock if this process dies.
        if self.cfg.job:
            with self.store.job_guard(self.cfg.job):
                return await self._run_unlocked(
                    usernames, on_item, include_about=include_about, concurrency=concurrency,
                    on_start=on_start, return_partial_on_cancel=return_partial_on_cancel,
                    collect=collect)
        return await self._run_unlocked(
            usernames, on_item, include_about=include_about, concurrency=concurrency,
            on_start=on_start, return_partial_on_cancel=return_partial_on_cancel,
            collect=collect)

    async def _run_unlocked(self, usernames: Iterable[str], on_item: ItemCallback | None = None, *,
                  include_about: bool | None = None, concurrency: int | None = None,
                  on_start: Callable[[int, int], Any] | None = None,
                  return_partial_on_cancel: bool = False, collect: bool = True) -> list[dict]:
        """Scrape all inputs; returns items in input order.

        on_start(total, already_done) is called once before any work (already_done counts items finished by
        a previous run of the same job). After the call, ``self.last_run_counts`` holds
        {"total", "alreadyDone", "done", "errors"} and is updated live while running.
        return_partial_on_cancel=True: if the run task is cancelled, return the items finished so far
        (input order) instead of raising CancelledError.
        collect=False: streaming mode for huge jobs - items only go to on_item (and the job store);
        result payload memory stays bounded and the returned list is empty. Input strings, order
        indices, and pending/replay indices still use O(N) metadata memory.
        """
        raws = dedupe(list(usernames))
        if (self.cfg.include_about if include_about is None else include_about) and not self.logged_in \
                and not getattr(self, "_about_warned", False):
            self._about_warned = True
            log.warning("includeAboutSection needs a logged-in session (IG_SESSIONID / --sessionid / --cookies-file): "
                        "'about' will be null. Parser ready in igscraper/about.py; the session fetch is not "
                        "verified live yet.")
        if self.cfg.job:
            self.store.job_init(self.cfg.job, raws)
            stored_order = self.store.job_order(self.cfg.job)
            requested = {target_key(raw): pos for pos, raw in enumerate(raws)}
            order = [idx for idx, raw in sorted(stored_order,
                     key=lambda row: (requested.get(target_key(row[1]), len(raws) + row[0]), row[0]))]
            todo = self.store.job_pending(self.cfg.job, self.cfg.retry_errors, max_age=self.cfg.cache_ttl)
            replay_idx = self.store.job_undelivered_indices(self.cfg.job) if on_item else set()
            outstanding = {i for i, _ in todo} | replay_idx
            if collect:
                done = {i: item for i, _, _, item in self.store.job_items(self.cfg.job)
                        if item is not None and i not in outstanding}
                already_done = len(done)
                already_errors = sum(bool(item.get("error")) for item in done.values())
            else:
                done = {}
                already_done, already_errors = self.store.job_completed_count(self.cfg.job, outstanding)
            log.info("job %s: %d items, %d already done, %d to do", self.cfg.job, len(order), already_done, len(todo))
        else:
            order = list(range(len(raws)))
            done = {}
            todo = list(enumerate(raws))
            replay_idx = set()
            already_done = already_errors = 0
        results: dict[int, dict] = done
        sem = asyncio.Semaphore(max(1, concurrency) if concurrency else self.concurrency)
        counts = {"total": len(order), "alreadyDone": already_done, "done": already_done,
                  "errors": already_errors, "freshSuccess": 0, "cached": 0}
        self.last_run_counts = counts
        if on_start:
            res = on_start(counts["total"], counts["alreadyDone"])
            if inspect.isawaitable(res):
                await res

        async def work(i: int, raw: str) -> None:
            await self._pace()
            item = self._cache_lookup(raw, self.cfg.include_about if include_about is None else include_about)
            if item is not None:
                counts["cached"] += 1
                self.stats.cache_hits += 1
                self.stats.paths["cache"] += 1
            else:
                async with sem:
                    try:
                        target = parse_target(raw)
                    except ValueError as e:
                        item = {"inputUrl": raw, "username": None, "error": "invalid_input",
                                "errorDescription": str(e), "requestErrorMessages": [str(e)]}
                    else:
                        item = await self.scrape_target(target, include_about)
                if not item.get("error"):
                    counts["freshSuccess"] += 1
                if self.store:
                    self.store.cache_put(item)
            if self.cfg.job:
                self.store.job_done(self.cfg.job, i, item, delivered=on_item is None)
            if collect:
                results[i] = item
            counts["done"] += 1
            if item.get("error"):
                counts["errors"] += 1
            if on_item:
                res = on_item(item)
                if inspect.isawaitable(res):
                    await res
                if self.cfg.job:
                    self.store.job_ack(self.cfg.job, i)

        async def replay_saved() -> None:
            if not replay_idx:
                return
            for i, item in self.store.job_undelivered(
                    self.cfg.job, (i for i in order if i in replay_idx)):
                if collect:
                    results[i] = item
                res = on_item(item)
                if inspect.isawaitable(res):
                    await res
                self.store.job_ack(self.cfg.job, i)
                counts["done"] += 1
                counts["errors"] += bool(item.get("error"))

        health = asyncio.create_task(self._health_loop()) if self.cfg.health_interval > 0 else None
        # BUG-7: bounded worker pool pulling from the queue (not one task per input)
        queue = iter(todo)
        n_workers = max(1, concurrency) if concurrency else self.concurrency

        async def worker() -> None:
            for i, r in queue:
                await work(i, r)

        tasks = []
        try:
            await replay_saved()
            tasks = [asyncio.ensure_future(worker()) for _ in range(min(n_workers, len(todo)))]
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if not return_partial_on_cancel:
                raise
            log.warning("run cancelled: returning %d/%d finished items", len(results), len(order))
        except BaseException:
            # A failed callback/store write must not leave sibling workers writing after teardown.
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            if health:
                health.cancel()
                await asyncio.gather(health, return_exceptions=True)
        return [results[i] for i in order if i in results] if collect else []

    async def _pace(self) -> None:
        """BUG-9: global pacing - with target_rate set, profiles start at most target_rate per minute in total,
        so each IP stays well under its ceiling instead of every egress running at max."""
        rate = self.cfg.target_rate
        if not rate or rate <= 0:
            return
        now = time.monotonic()
        slot = max(getattr(self, "_next_slot", now), now)
        self._next_slot = slot + 60.0 / rate
        if slot > now:
            await asyncio.sleep(slot - now)


def error_item(target: Target, username: str | None, code: str, desc: str) -> dict:
    return {"inputUrl": target.input_url, "username": username or target.username, "error": code,
            "errorDescription": desc[:500], "requestErrorMessages": [f"{code}: {desc[:500]}"]}


# ---------------------------------------------------------------------- convenience API
def config_from_input(run_input: dict[str, Any], base: ScraperConfig | None = None) -> ScraperConfig:
    cfg = base or ScraperConfig.from_env()
    if "includeAboutSection" in run_input:
        cfg.include_about = bool(run_input["includeAboutSection"])
    return cfg


async def scrape_profiles(usernames: Iterable[str], config: ScraperConfig | None = None,
                          on_item: ItemCallback | None = None) -> list[dict]:
    async with ProfileScraper(config) as s:
        return await s.run(usernames, on_item)


async def run_input(run_input: dict[str, Any], config: ScraperConfig | None = None) -> list[dict]:
    """Run with an Apify-shaped input object: {"usernames": [...], "includeAboutSection": false}."""
    if not isinstance(run_input.get("usernames"), list):
        raise ValueError("input.usernames must be an array of strings")
    cfg = config_from_input(run_input, config)
    return await scrape_profiles(run_input["usernames"], cfg)


def scrape(usernames: Iterable[str], **config_kwargs) -> list[dict]:
    """Synchronous helper: ``igscraper.scrape(["nasa", "natgeo"], concurrency=8)``."""
    cfg = ScraperConfig.from_env(**config_kwargs)
    return asyncio.run(scrape_profiles(usernames, cfg))


def load_proxies(cli_proxies: list[str] | None, proxy_file: str | None) -> list[str]:
    """Parse --proxy values and a --proxy-file. Unparsable entries raise ValueError (never silently fall back
    to the machine's own IP)."""
    from pathlib import Path
    bad, out = [], []
    lines = list(cli_proxies or [])
    if proxy_file:
        lines += [l for l in Path(proxy_file).read_text().splitlines() if l.strip() and not l.strip().startswith("#")]
    for x in lines:
        p = normalise_proxy(x)
        (out if p else bad).append(p or x)
    if bad:
        from .net import redact
        raise ValueError(f"unparsable proxy entries: {[redact('http://' + b) if '@' in b else b for b in bad]} "
                         "(use scheme://[user:pass@]host:port, host:port or host:port:user:pass)")
    return out


def _coerce_egress(egress) -> list[EgressSpec]:
    from .egress import parse_specs
    if egress is None:
        return []
    if isinstance(egress, (str, EgressSpec)):
        egress = [egress]
    out: list[EgressSpec] = []
    for e in egress:
        out += [e] if isinstance(e, EgressSpec) else parse_specs(str(e))
    return out


async def fetch_profiles(usernames: Iterable[str], egress=None, *, on_item: ItemCallback | None = None,
                         include_about: bool = False, **config) -> list[dict]:
    """Library entry point for coordinators / node agents.

        items = await fetch_profiles(["nasa", "natgeo"], egress=["tor:127.0.0.1:9152:8", "direct"],
                                     cache_path=None, max_retries=2)

    ``egress``: None (this machine's IP), a spec string, an EgressSpec, or a list of them.
    ``config``: any ScraperConfig field. Returns Apify-shaped items in input order; never raises for a
    single bad profile (failed items carry ``error``/``errorDescription``/``requestErrorMessages``).
    """
    cfg = ScraperConfig(**config)
    cfg.egresses = _coerce_egress(egress) or cfg.egresses
    cfg.include_about = include_about
    async with ProfileScraper(cfg) as s:
        return await s.run(usernames, on_item)


def fetch_profiles_sync(usernames: Iterable[str], egress=None, **kw) -> list[dict]:
    return asyncio.run(fetch_profiles(usernames, egress, **kw))
