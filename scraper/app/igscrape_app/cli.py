"""`igscrape`: the simple command line.

    igscrape nasa natgeo                         -> results JSON + CSV in the current folder
    igscrape -f usernames.txt -o out.json --csv out.csv
    igscrape ui                                  -> local web page (paste usernames, run, download)
    igscrape selftest                            -> offline check that the install works
    igscrape egresses                            -> show which connections (egresses) will be used
    igscrape advanced --help                     -> every igscraper option (power users)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from . import __version__

SUBCOMMANDS = ("ui", "selftest", "egresses", "advanced", "version")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="igscrape",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Scrape public Instagram profiles into Apify-identical JSON / CSV.",
        epilog="""examples:
  igscrape nasa natgeo
  igscrape -f usernames.txt -o out.json --csv out.csv
  igscrape ui                 open the web page (paste usernames, click Run, download)
  igscrape selftest           offline install check (no Instagram requests)
  igscrape egresses           show which connections will be used
  igscrape advanced --help    all low-level igscraper options

Usernames may be plain names, @handles or profile URLs; separate them with spaces, commas or new lines.
Re-running the same list is fast: profiles fetched in the last 24 h come from the local cache.""")
    p.add_argument("usernames", nargs="*", help="usernames, @handles or profile URLs")
    p.add_argument("-f", "--file", action="append", default=[], metavar="FILE",
                   help="text file with usernames (one per line); '-' reads from stdin. Repeatable")
    p.add_argument("-o", "--out", metavar="FILE", help="write JSON here (.json, or .jsonl for one item per line)")
    p.add_argument("--csv", metavar="FILE", help="also write a CSV file (Apify column layout)")
    p.add_argument("--stdout", action="store_true", help="print the JSON to the screen instead of saving files")
    p.add_argument("-e", "--egresses", metavar="FILE",
                   help="egress config (default: ~/ig-scraper/egresses.yaml if it exists)")
    p.add_argument("-c", "--concurrency", type=int, help="profiles in flight (default: automatic)")
    p.add_argument("--target-rate", type=float, metavar="N", help="profiles/min you want; warns if not reachable")
    p.add_argument("--about", action="store_true", help="Apify includeAboutSection")
    p.add_argument("--no-cache", action="store_true", help="always fetch fresh (ignore the 24 h cache)")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress line")
    p.add_argument("--version", action="version", version=f"igscrape {__version__}")
    return p


class _LineLogHandler(logging.Handler):
    """Log lines that don't garble the live progress line."""

    def __init__(self, ticker):
        super().__init__(logging.WARNING)
        self.ticker = ticker
        self.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    def emit(self, record):
        self.ticker.clear()
        sys.stderr.write(self.format(record) + "\n")
        self.ticker.draw()


class _Ticker:
    def __init__(self, job, quiet: bool):
        self.job, self.quiet = job, quiet
        self.tty = sys.stderr.isatty()
        self._last_plain = 0.0
        self._shown = False

    def clear(self):
        if self.tty and self._shown:
            sys.stderr.write("\r\033[K")

    def draw(self, final: bool = False):
        if self.quiet:
            return
        line = self.job.progress.line()
        if self.tty:
            try:
                cols = os.get_terminal_size(sys.stderr.fileno()).columns
            except OSError:
                cols = 120
            sys.stderr.write("\r\033[K" + line[: max(20, cols - 1)] + ("\n" if final else ""))
            self._shown = not final
        elif final or time.monotonic() - self._last_plain >= 10:
            self._last_plain = time.monotonic()
            sys.stderr.write(line + "\n")
        sys.stderr.flush()

    async def loop(self):
        while True:
            self.draw()
            await asyncio.sleep(0.5)


def _read_inputs(a) -> list[str]:
    from .runner import parse_usernames
    names = parse_usernames(" ".join(a.usernames))
    for f in a.file:
        text = sys.stdin.read() if f == "-" else Path(f).expanduser().read_text(encoding="utf-8-sig")
        names += parse_usernames(text)
    from igscraper.inputs import validate_inputs
    return validate_inputs(names)


def _default_paths() -> tuple[str, str]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"ig-results-{stamp}.json", f"ig-results-{stamp}.csv"


def run_cli(argv: list[str]) -> int:
    a = _parser().parse_args(argv)
    try:
        names = _read_inputs(a)
    except (OSError, ValueError, UnicodeError) as e:
        print(f"error reading inputs: {e}", file=sys.stderr)
        return 2
    if not names:
        _parser().print_usage(sys.stderr)
        print("error: no usernames given. Example:  igscrape nasa natgeo   or   igscrape -f usernames.txt",
              file=sys.stderr)
        return 2

    from .runner import Job, build_config, write_outputs, validate_output_paths
    json_path, csv_path = a.out, a.csv
    if not a.stdout and not json_path and not csv_path:
        json_path, csv_path = _default_paths()
    try:
        validate_output_paths(json_path, csv_path)
        if a.concurrency is not None and a.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if a.target_rate is not None:
            import math
            if not math.isfinite(a.target_rate) or a.target_rate <= 0:
                raise ValueError("target rate must be a finite positive number")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        cfg, source = build_config(egress_file=a.egresses, concurrency=a.concurrency, cache=not a.no_cache,
                                   include_about=a.about, target_rate=a.target_rate)
    except (OSError, ValueError) as e:
        print(f"error in egress config: {e}", file=sys.stderr)
        return 2

    job = Job(names, cfg, retain_items=False)
    ticker = _Ticker(job, a.quiet)
    logging.basicConfig(level=logging.WARNING, handlers=[_LineLogHandler(ticker)], force=True)
    if not a.quiet:
        from . import mock
        print(f"scraping {job.progress.total} profile(s); egresses from {source}"
              + ("  [MOCK MODE: offline fake data]" if mock.enabled() else ""), file=sys.stderr)

    async def go():
        t = asyncio.create_task(ticker.loop())
        try:
            return await job.run()
        finally:
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)

    interrupted = False
    try:
        items = asyncio.run(go())
    except KeyboardInterrupt:
        interrupted = True
        items = job.partial_items
    except Exception as e:
        ticker.draw(final=True)
        print(f"error: {e}", file=sys.stderr)
        return 1
    ticker.draw(final=True)

    p = job.progress
    try:
        with job.output_source() as rows:
            count, ok, errs = 0, 0, {}
            for item in rows():
                count += 1
                if "error" not in item:
                    ok += 1
                else:
                    reason = str(item["error"])
                    errs[reason] = errs.get(reason, 0) + 1
            if a.stdout:
                from igscraper.output import write_stream
                write_stream(rows, sys.stdout, "jsonl" if (a.out or "").endswith(".jsonl") else "json")
                written = []
            else:
                written = write_outputs(rows, json_path, csv_path)
    except (OSError, ValueError) as e:
        print(f"error saving results: {e}", file=sys.stderr)
        return 1
    if interrupted:
        resume = "re-run the same command to resume" if cfg.cache_path else "caching was disabled; re-running fetches again"
        print(f"stopped early: saved {count} of {p.total} profiles ({resume})", file=sys.stderr)
    print(f"done: {ok} ok, {count - ok} failed, {p.elapsed:.1f}s ({p.per_min:.1f} fresh bios/min)"
          + (f", {p.cached} from cache" if p.cached else ""), file=sys.stderr)
    for w in written:
        print(f"saved: {w}", file=sys.stderr)
    if errs:
        print("failures by reason: " + ", ".join(f"{k}={v}" for k, v in sorted(errs.items())), file=sys.stderr)
    return 130 if interrupted else (0 if ok else 1)


def cmd_egresses(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="igscrape egresses", description="Show the egresses a run would use.")
    ap.add_argument("-e", "--egresses", metavar="FILE")
    a = ap.parse_args(argv)
    from igscraper import ProfileScraper
    from .egress_config import describe
    from .runner import build_config
    try:
        cfg, source = build_config(egress_file=a.egresses)
    except (OSError, ValueError) as e:
        print(f"error in egress config: {e}", file=sys.stderr)
        return 2
    specs = ProfileScraper(cfg).egress_specs()
    print(f"source: {source}")
    for line in describe(specs):
        print("  " + line)
    return 0


def cmd_selftest(argv: list[str]) -> int:
    """Offline end-to-end check: fake Instagram responses -> real igscraper pipeline -> JSON + CSV."""
    import json
    import tempfile
    from igscraper.output import render
    from . import mock
    from .runner import Job, build_config
    os.environ["IGSCRAPE_MOCK"] = "1"
    os.environ.setdefault("IGSCRAPE_MOCK_DELAY", "0.01")
    names = ["nasa", "@natgeo", "https://www.instagram.com/example_one/", "thisuserdoesnotexist_zq9x7"]
    cfg, _ = build_config(cache=False, egress_file=argv[0] if argv else None)
    job = Job(names, cfg)
    items = asyncio.run(job.run())
    checks = {
        "4 items returned": len(items) == 4,
        "nasa mapped (followersCount, latestPosts)": items[0].get("username") == "nasa"
            and isinstance(items[0].get("followersCount"), int) and bool(items[0].get("latestPosts")),
        "@handle and URL accepted": items[1].get("username") == "natgeo" and items[2].get("username") == "example_one",
        "missing profile reported as error": items[3].get("error") == "not_found",
        "progress counted every profile": job.progress.done == 4 and job.progress.errors == 1,
    }
    csv_text = render(items, "csv")
    checks["CSV has Apify columns"] = csv_text.splitlines()[0].count("latestPosts/0/") > 0
    with tempfile.TemporaryDirectory() as d:
        Path(d, "x.json").write_text(render(items, "json"))
        checks["JSON round-trips"] = len(json.loads(Path(d, "x.json").read_text())) == 4
    for k, v in checks.items():
        print(("  ok   " if v else "  FAIL ") + k)
    good = all(checks.values())
    print("selftest passed: igscrape is installed correctly" if good else "selftest FAILED", file=sys.stderr)
    return 0 if good else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in SUBCOMMANDS:
        cmd, rest = argv[0], argv[1:]
        if cmd == "ui":
            from .ui import main as ui_main
            return ui_main(rest)
        if cmd == "selftest":
            return cmd_selftest(rest)
        if cmd == "egresses":
            return cmd_egresses(rest)
        if cmd == "version":
            import igscraper
            print(f"igscrape {__version__} (igscraper {getattr(igscraper, '__version__', '?')}, "
                  f"python {sys.version.split()[0]})")
            return 0
        if cmd == "advanced":
            from igscraper.cli import main as raw_main
            return raw_main(rest)
    if argv and argv[0] == "--":
        argv = argv[1:]
    return run_cli(argv)
