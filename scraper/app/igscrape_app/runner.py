"""Shared run engine for the CLI and the web UI: builds the igscraper config, runs ProfileScraper with a
progress callback, and exposes live progress (done/total, rate, healthy egresses, ETA)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import tempfile
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from igscraper import ProfileScraper, ScraperConfig
from igscraper.inputs import dedupe, target_key
from igscraper.output import render, write_stream

from . import mock
from .egress_config import load_yaml_egresses

HOME = Path(os.environ.get("IG_HOME") or (Path.home() / "ig-scraper")).expanduser()
DEFAULT_EGRESS_FILE = HOME / "egresses.yaml"
RESULTS_DIR = HOME / "results"


def parse_usernames(text: str) -> list[str]:
    """Split pasted text on newlines, commas, semicolons and spaces; drop comments and blanks."""
    out = []
    for line in text.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", line)            # "# comment" lines and trailing comments
        out += [t for t in re.split(r"[\s,;]+", line) if t]
    return out


def build_config(*, egress_file: str | None = None, concurrency: int | None = None, cache: bool = True,
                 include_about: bool = False, target_rate: float | None = None) -> tuple[ScraperConfig, str]:
    """Returns (config, where-the-egresses-came-from)."""
    cfg = ScraperConfig.from_env()
    source = "env" if cfg.egresses or cfg.proxies else "default (direct: this computer's own connection)"
    path = egress_file or os.environ.get("IGSCRAPE_EGRESSES") or (
        str(DEFAULT_EGRESS_FILE) if DEFAULT_EGRESS_FILE.exists() else None)
    if path:
        specs = load_yaml_egresses(path)
        if specs:
            cfg.egresses, cfg.proxies = specs, []
            source = str(Path(path).expanduser())
        elif egress_file:
            raise ValueError(f"{egress_file}: no active egresses (every line is commented out?)")
    cfg.concurrency = concurrency
    cfg.include_about = include_about
    cfg.target_rate = target_rate
    cfg.health_interval = 0 if not os.environ.get("IGSCRAPE_HEALTH_LOG") else 30.0
    if cache and not os.environ.get("IG_CACHE"):
        cfg.cache_path = str(HOME / "cache.sqlite")
    elif not cache:
        cfg.cache_path = None
    return cfg, source


@dataclass
class Progress:
    total: int = 0
    done: int = 0
    errors: int = 0
    cached: int = 0
    fresh_success: int = 0
    already_done: int = 0
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    healthy: int = 0
    egresses: int = 0
    egress_detail: list = field(default_factory=list)
    state: str = "idle"          # idle | running | done | stopped | failed
    message: str = ""

    @property
    def elapsed(self) -> float:
        return (self.finished or time.monotonic()) - self.started

    @property
    def per_min(self) -> float:
        e = self.elapsed
        return self.fresh_success / e * 60 if e > 0.5 else 0.0

    @property
    def eta_s(self) -> float | None:
        left = self.total - self.done
        if left <= 0:
            return 0.0
        r = (self.done - self.already_done) / self.elapsed * 60 if self.elapsed > 0.5 else 0
        return left / r * 60 if r > 0 else None

    def as_dict(self) -> dict:
        return {"state": self.state, "message": self.message, "total": self.total, "done": self.done,
                "ok": self.done - self.errors, "errors": self.errors, "cached": self.cached,
                "freshSuccess": self.fresh_success, "alreadyDone": self.already_done,
                "processedPerMinute": round((self.done - self.already_done) / max(self.elapsed, 0.5) * 60, 1),
                "elapsedS": round(self.elapsed, 1), "perMinute": round(self.per_min, 1),
                "etaS": None if self.eta_s is None else round(self.eta_s), "healthyEgresses": self.healthy,
                "egresses": self.egresses, "egressDetail": self.egress_detail}

    def line(self, width: int = 24) -> str:
        frac = self.done / self.total if self.total else 0
        n = int(frac * width)
        bar = "#" * n + "-" * (width - n)
        return (f"[{bar}] {self.done}/{self.total} done"
                + (f", {self.errors} failed" if self.errors else "")
                + f" | {self.per_min:.1f} fresh bios/min | egresses {self.healthy}/{self.egresses} healthy"
                + f" | ETA {fmt_duration(self.eta_s)}")


def fmt_duration(s: float | None) -> str:
    if s is None:
        return "--"
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


class Job:
    """One scrape run. `await job.run()`; poll `job.progress`; items (input order) in `job.items`."""

    def __init__(self, usernames: list[str], cfg: ScraperConfig, *, retain_items: bool = True):
        self.usernames = dedupe(usernames)
        self.cfg = replace(cfg)
        self.retain_items = retain_items
        self._spool = tempfile.TemporaryFile(mode="w+t", encoding="utf-8") if not retain_items and not cfg.cache_path else None
        if self.cfg.cache_path and not self.cfg.job:
            identity = {"targets": sorted(target_key(u) for u in self.usernames),
                        "includeAbout": self.cfg.include_about, "schema": 2}
            self.cfg.job = "profiles-" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
        self.progress = Progress(total=len(self.usernames))
        self.items: list[dict] = []
        self._partial: list[dict] = []
        self.scraper: ProfileScraper | None = None

    def _refresh_health(self) -> None:
        if not self.scraper:
            return
        h = self.scraper.health()
        self.progress.egresses = len(h)
        self.progress.healthy = sum(1 for e in h if e.get("state") == "healthy")
        self.progress.egress_detail = [{k: e.get(k) for k in ("egress", "state", "rpm", "ok", "blocks", "parkedForS")}
                                       for e in h]

    def _on_item(self, item: dict) -> None:
        if self.retain_items:
            self._partial.append(item)
        elif self._spool is not None:
            self._spool.write(json.dumps(item, ensure_ascii=False) + "\n")
        p = self.progress
        p.done += 1
        if "error" in item:
            p.errors += 1
        elif (item.get("_scrape") or {}).get("cacheHit"):
            p.cached += 1
        else:
            p.fresh_success += 1
        self._refresh_health()

    @property
    def partial_items(self) -> list[dict]:
        order = {target_key(raw): i for i, raw in enumerate(self.usernames)}
        return sorted(self.items or self._partial, key=lambda it: order.get(target_key(it.get("inputUrl")), len(order)))

    def _on_start(self, total: int, already_done: int) -> None:
        p = self.progress
        p.total, p.done, p.already_done = total, already_done, already_done
        s = self.scraper
        if s and self.cfg.job and not self.retain_items:
            p.errors = s.last_run_counts["errors"]
        if s and self.cfg.job and self.retain_items:
            pending = {i for i, _ in s.store.job_pending(self.cfg.job, self.cfg.retry_errors, max_age=self.cfg.cache_ttl)}
            pending.update(s.store.job_undelivered_indices(self.cfg.job))
            self._partial = [item for i, _, _, item in s.store.job_items(self.cfg.job) if item is not None and i not in pending]
            p.errors = sum(bool(item.get("error")) for item in self._partial)

    @contextmanager
    def output_source(self):
        """Repeatable export source; a read snapshot keeps CSV's two passes consistent."""
        if self.retain_items:
            yield lambda: iter(self.partial_items)
            return
        if self._spool is not None:
            self._spool.flush()
            def spool_rows():
                self._spool.seek(0)
                for line in self._spool:
                    yield json.loads(line)
            yield spool_rows
            return
        uri = Path(self.cfg.cache_path).expanduser().resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True)
        try:
            db.execute("BEGIN")
            positions = {target_key(raw): i for i, raw in enumerate(self.usernames)}
            order = [idx for idx, raw in sorted(db.execute(
                "SELECT idx, raw FROM job_items WHERE job=?", (self.cfg.job,)),
                key=lambda row: positions.get(target_key(row[1]), len(positions) + row[0]))]
            def rows():
                for idx in order:
                    row = db.execute("SELECT item FROM job_items WHERE job=? AND idx=? AND item IS NOT NULL",
                                     (self.cfg.job, idx)).fetchone()
                    if row is not None:
                        yield json.loads(row[0])
            yield rows
        finally:
            db.close()

    async def run(self) -> list[dict]:
        p = self.progress
        p.state, p.started = "running", time.monotonic()
        tf = mock.transport_factory() if mock.enabled() else None
        try:
            async with ProfileScraper(self.cfg, transport_factory=tf) as s:
                self.scraper = s
                self._refresh_health()
                c0 = s.stats.cache_hits
                self.items = await s.run(self.usernames, on_item=self._on_item, on_start=self._on_start,
                                         collect=self.retain_items)
                p.cached = s.stats.cache_hits - c0
                self._refresh_health()
            p.state = "done"
            if p.errors:
                p.message = f"{p.errors} profiles need review or retry; processing finished"
        except asyncio.CancelledError:
            p.state, p.message = "stopped", "stopped by user"
            raise
        except Exception as e:
            p.state, p.message = "failed", f"{type(e).__name__}: {e}"
            raise
        finally:
            p.finished = time.monotonic()
        return self.items


def validate_output_paths(json_path: str | None, csv_path: str | None) -> None:
    if json_path and csv_path and Path(json_path).expanduser().resolve() == Path(csv_path).expanduser().resolve():
        raise ValueError("JSON and CSV outputs need different file paths")


def write_outputs(items, json_path: str | None = None, csv_path: str | None = None) -> list[str]:
    validate_output_paths(json_path, csv_path)
    factory = items if callable(items) else lambda: iter(items)
    written = []
    for path, fmt in ((json_path, None), (csv_path, "csv")):
        if not path:
            continue
        f = fmt or ("csv" if path.lower().endswith(".csv") else "jsonl" if path.lower().endswith(".jsonl") else "json")
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=p.parent, delete=False) as tmp:
                temp_path = Path(tmp.name)
                write_stream(factory, tmp, f)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(temp_path, p)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        written.append(str(p.resolve()))
    return written
