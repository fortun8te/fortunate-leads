from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from .config import ScraperConfig
from .output import infer_format, omit_fields, render
from .egress import load_egress_file, parse_specs
from .scraper import ProfileScraper, load_proxies
from .store import DEFAULT_DIR


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m igscraper",
                                description="Apify-compatible Instagram Profile Scraper. "
                                            "Use `python -m igscraper serve` for the HTTP API.")
    src = p.add_argument_group("input (Apify: usernames = usernames, profile URLs or profile IDs)")
    src.add_argument("--usernames", "-u", nargs="+", default=[], help="usernames, profile URLs or numeric IDs")
    src.add_argument("--usernames-file", help="text file, one username/URL/ID per line")
    src.add_argument("--input", help='Apify-style input JSON file: {"usernames": [...], "includeAboutSection": false}')
    src.add_argument("--include-about", action="store_true", help="Apify includeAboutSection (emits about=null; needs login upstream)")
    out = p.add_argument_group("output")
    out.add_argument("--out", "-o", help="output file (.json, .jsonl, .csv). Default: stdout")
    out.add_argument("--format", choices=["json", "jsonl", "csv"], help="override format inferred from --out")
    out.add_argument("--omit", default="", help="comma-separated top-level fields to drop, e.g. latestPosts,relatedProfiles")
    perf = p.add_argument_group("speed / robustness")
    perf.add_argument("--concurrency", "-c", type=int, default=None,
                      help="profiles in flight (default: auto = max(8, 4 x egresses), cap 64)")
    perf.add_argument("--rate", type=float, default=None,
                      help="FIXED requests/sec per egress (0 = unlimited). Default: adaptive limiter")
    perf.add_argument("--start-rpm", type=float, default=None, help="adaptive: start requests/min per egress (60)")
    perf.add_argument("--max-rpm", type=float, default=None, help="adaptive: ceiling requests/min per egress (90)")
    perf.add_argument("--target-rate", type=float, default=None,
                      help="profiles/min you need; warns if the healthy egresses can't reach it")
    perf.add_argument("--park-cooldown", type=float, default=None,
                      help="seconds an egress is parked after 401/429/login wall (default 900, doubles on repeat)")
    perf.add_argument("--max-park-wait", type=float, default=None,
                      help="fail remaining items if every egress is parked longer than this (default: pause and wait)")
    perf.add_argument("--health-interval", type=float, default=30.0, help="seconds between egress health logs (-v)")
    perf.add_argument("--burst", type=int, default=2)
    perf.add_argument("--retries", type=int, default=4)
    perf.add_argument("--timeout", type=float, default=20.0)
    perf.add_argument("--no-fallback", action="store_true", help="disable feed+HTML fallback")
    net = p.add_argument_group("egress: direct / proxies / relays (env IG_EGRESS, IG_EGRESS_FILE, PROXY_URL)")
    net.add_argument("--egress", action="append", default=[],
                     help="egress spec (repeatable): direct | http(s)/socks5 proxy URL | relay:https://host/?u={url} "
                          "| tor:127.0.0.1:9152:36 (36 isolated Tor circuits)")
    net.add_argument("--egress-file", help="text (one spec per line) or JSON {\"egresses\": [...]} file")
    net.add_argument("--proxy", action="append", default=[], help="proxy URL (repeatable): http://user:pass@host:port, socks5://...")
    net.add_argument("--proxy-file", help="file with one proxy per line (URL, host:port or host:port:user:pass)")
    net.add_argument("--proxy-max-failures", type=int, default=3)
    net.add_argument("--use-direct", action="store_true", help="also use this machine's IP alongside proxies")
    auth = p.add_argument_group("optional logged-in mode (also env IG_SESSIONID / IG_COOKIES_FILE)")
    auth.add_argument("--sessionid", help="Instagram sessionid cookie value")
    auth.add_argument("--cookies-file", help="Netscape cookies.txt exported from a logged-in browser")
    st = p.add_argument_group("cache / resumable jobs (sqlite)")
    st.add_argument("--cache", default=None, help="sqlite path (default ~/.igscraper/cache.sqlite, env IG_CACHE)")
    st.add_argument("--no-cache", action="store_true", help="disable the TTL cache and jobs")
    st.add_argument("--cache-ttl", type=float, default=24.0, help="hours a cached profile is reused (default 24)")
    st.add_argument("--job", help="resumable job name: re-running with the same name skips finished profiles")
    st.add_argument("--no-retry-errors", action="store_true", help="when resuming, don't retry failed items")
    p.add_argument("--stats", action="store_true", help="print run stats JSON to stderr")
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p


def _serve(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m igscraper serve")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)
    import uvicorn
    uvicorn.run("igscraper.api:app", host=a.host, port=a.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (OSError, ValueError, UnicodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stopped; use the same --job to resume saved profiles", file=sys.stderr)
        return 130


def _main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "tor":
        from .torctl import main as tor_main
        return tor_main(argv[1:])
    if argv and argv[0] == "serve":
        return _serve(argv[1:])
    a = _parser().parse_args(argv)
    # Only our own logger gets more verbose. Third-party DEBUG logs (h2/hpack/httpcore) print request headers,
    # including Cookie/sessionid, so they stay at WARNING whatever -v says.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("igscraper").setLevel(logging.WARNING - 10 * min(a.verbose, 2))
    for noisy in ("hpack", "h2", "httpcore", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    usernames = list(a.usernames)
    include_about = a.include_about
    if a.usernames_file:
        usernames += [l.strip() for l in Path(a.usernames_file).read_text().splitlines() if l.strip()]
    if a.input:
        inp = json.loads(Path(a.input).read_text())
        if not isinstance(inp, dict) or not isinstance(inp.get("usernames"), list):
            raise ValueError("input.usernames must be an array of strings")
        if "includeAboutSection" in inp and not isinstance(inp["includeAboutSection"], bool):
            raise ValueError("input.includeAboutSection must be true or false")
        usernames += inp["usernames"]
        include_about = include_about or inp.get("includeAboutSection", False)
    from .inputs import validate_inputs
    usernames = validate_inputs(usernames)
    if not usernames:
        print("no usernames given (use --usernames, --usernames-file or --input)", file=sys.stderr)
        return 2

    cfg = ScraperConfig.from_env()
    try:
        proxies = load_proxies(a.proxy, a.proxy_file)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if proxies:
        cfg.proxies = proxies
    specs = [e for x in a.egress for e in parse_specs(x)]
    if a.egress_file:
        specs += load_egress_file(a.egress_file)
    if specs:
        cfg.egresses = specs
    cfg.concurrency, cfg.rate_per_ip, cfg.burst = a.concurrency, a.rate, a.burst
    for attr, val in (("start_rpm", a.start_rpm), ("max_rpm", a.max_rpm), ("target_rate", a.target_rate),
                      ("proxy_cooldown", a.park_cooldown), ("max_park_wait", a.max_park_wait)):
        if val is not None:
            setattr(cfg, attr, val)
    cfg.health_interval = a.health_interval
    if a.no_cache:
        cfg.cache_path = None
    else:
        cfg.cache_path = a.cache or cfg.cache_path or str(DEFAULT_DIR / "cache.sqlite")
    cfg.cache_ttl = a.cache_ttl * 3600
    cfg.job = a.job
    cfg.retry_errors = not a.no_retry_errors
    if a.job and not cfg.cache_path:
        print("--job needs the sqlite cache (drop --no-cache)", file=sys.stderr)
        return 2
    cfg.max_retries, cfg.timeout, cfg.fallback = a.retries, a.timeout, not a.no_fallback
    cfg.proxy_max_failures, cfg.use_direct_with_proxies = a.proxy_max_failures, a.use_direct
    cfg.include_about = include_about
    if a.sessionid:
        cfg.sessionid = a.sessionid
    if a.cookies_file:
        cfg.cookies_file = a.cookies_file

    async def go():
        async with ProfileScraper(cfg) as s:
            t0 = time.perf_counter()
            items = await s.run(usernames)
            dt = time.perf_counter() - t0
            return items, s, dt

    items, s, dt = asyncio.run(go())
    fmt = infer_format(a.out, a.format)
    text = render(omit_fields(items, a.omit.split(",")), fmt)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    ok = sum(1 for i in items if "error" not in i)
    summary = {"profiles": len(items), "ok": ok, "errors": len(items) - ok, "seconds": round(dt, 2),
               "profilesPerMinute": round(len(items) / dt * 60, 1) if dt else None,
               "concurrency": s.concurrency, "limiter": "adaptive" if cfg.rate_per_ip is None else cfg.rate_per_ip,
               "loggedIn": s.logged_in, **s.stats.as_dict(), "egress": s.health()}
    print(json.dumps(summary) if a.stats else
          f"done: {ok}/{len(items)} ok in {dt:.1f}s ({summary['profilesPerMinute']} profiles/min)", file=sys.stderr)
    return 0 if ok else 1
