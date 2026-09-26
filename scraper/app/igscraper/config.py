from __future__ import annotations

import os
from dataclasses import dataclass, field

from .egress import EgressSpec, egresses_from_env
from .net import DEFAULT_MAX_RPM, DEFAULT_START_RPM


@dataclass
class ScraperConfig:
    concurrency: int | None = None       # profiles in flight; None = auto (max(8, 4 x egresses), cap 64)
    max_inflight_per_egress: int = 2      # bound simultaneous HTTP requests per lane
    max_retries: int = 4                 # per HTTP request, on 429/5xx/login-wall/network errors
    timeout: float = 20.0                # httpx per-phase timeout (connect/read/write/pool), not a total deadline
    # Per-egress limiter. None = adaptive (start_rpm -> ramps to max_rpm on 200s, halves + parks on blocks);
    # 0 = unlimited; > 0 = fixed requests/second per egress.
    rate_per_ip: float | None = None
    start_rpm: float = DEFAULT_START_RPM  # requests/min per egress at start (~40 profiles/min)
    max_rpm: float = DEFAULT_MAX_RPM      # adaptive ceiling (~60 profiles/min)
    burst: int = 2
    backoff_base: float = 1.0            # seconds; exponential backoff with +/-50% jitter
    backoff_max: float = 30.0
    proxies: list[str] = field(default_factory=list)          # proxy URLs (compat; become proxy egresses)
    egresses: list[EgressSpec] = field(default_factory=list)  # direct / proxy / relay egresses
    proxy_max_failures: int = 3          # consecutive failures before a proxy/relay is retired
    proxy_cooldown: float = 900.0        # park time after a 401 "please wait"/403/login wall (doubles per repeat)
    rate_limit_cooldown: float = 45.0    # short backoff after a 429 (rate halves; doubles per consecutive 429,
                                         # capped at proxy_cooldown; a larger Retry-After is honoured)
    park_max: float = 4 * 3600.0         # cap for the exponential park
    max_park_wait: float | None = None   # None = pause until an egress is free (never fail queued items);
                                         # a number = fail fast (no_proxies_left) if all are parked longer
    use_direct_with_proxies: bool = False  # also use the box's own IP alongside proxies/relays
    sessionid: str | None = None         # optional logged-in mode (never hard-code; flag/env only)
    cookies_file: str | None = None      # Netscape cookies.txt
    include_about: bool = False          # Apify "includeAboutSection" (emits about=null; needs login upstream)
    fallback: bool = True                # feed + SSR-HTML (+Threads-app) fallback when web_profile_info fails
    gql_fallback: bool = False           # GraphQL PolarisProfilePageContentQuery in the fallback. Off by default:
                                         # worked for the per-IP lane, but returned the HTML login page from our
                                         # client on 2026-09-24 22:25 (unverified) -> the SSR-HTML fallback is used
    http2: bool = True
    cache_path: str | None = None        # sqlite file for TTL cache + jobs; None = no cache
    cache_ttl: float = 24 * 3600.0
    job: str | None = None               # resumable job name (needs cache_path)
    retry_errors: bool = True            # when resuming a job, retry items that failed transiently
    target_rate: float | None = None     # profiles/min wanted; warns if healthy egresses can't reach it
    health_interval: float = 30.0        # seconds between per-egress health log lines (0 = off)

    @classmethod
    def from_env(cls, **overrides) -> "ScraperConfig":
        cfg = cls()
        env_proxy = os.environ.get("PROXY_URL")
        if env_proxy:
            cfg.proxies = [p.strip() for p in env_proxy.split(",") if p.strip()]
        cfg.egresses = egresses_from_env()
        cfg.sessionid = os.environ.get("IG_SESSIONID") or None
        cfg.cookies_file = os.environ.get("IG_COOKIES_FILE") or None
        cache = os.environ.get("IG_CACHE", "").strip()
        if cache and cache.lower() not in ("off", "none", "0", "false", "no"):
            cfg.cache_path = cache
        for k, v in overrides.items():
            if v is not None:
                setattr(cfg, k, v)
        return cfg
