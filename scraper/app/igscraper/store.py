"""SQLite-backed profile cache (TTL dedupe) and resumable job queue."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import fcntl
import hashlib
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Iterable
from .inputs import target_key

DEFAULT_DIR = Path(os.environ.get("IGSCRAPER_HOME", Path.home() / ".igscraper"))

# errors that will not change on a retry; everything else is retried when a job is resumed
FINAL_ERRORS = {"not_found", "invalid_input"}


def synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class Store:
    def __init__(self, path: str | os.PathLike):
        p = Path(path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        self.path = p
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(p), timeout=30, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA busy_timeout=30000")
        # WAL's first-open transition may report SQLITE_BUSY during a concurrent open.
        for attempt in range(100):
            try:
                self.db.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 99:
                    self.db.close()
                    raise
                time.sleep(.01)
        self.db.execute("PRAGMA synchronous=NORMAL")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("CREATE TABLE IF NOT EXISTS profiles(key TEXT PRIMARY KEY, item TEXT NOT NULL, fetched_at REAL NOT NULL)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS job_items(job TEXT NOT NULL, idx INTEGER NOT NULL,
                raw TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', item TEXT, updated REAL,
                delivered INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(job, idx))""")
            # Schema inspection and migration must share the write transaction.
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(job_items)")}
            if "delivered" not in columns:
                self.db.execute("ALTER TABLE job_items ADD COLUMN delivered INTEGER NOT NULL DEFAULT 1")
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            self.db.close()
            raise

    @synchronized
    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @contextmanager
    def job_guard(self, job: str):
        """One live runner per named job, across threads and processes.

        An OS file lock is released on cancellation, exceptions, or process death;
        unlike a timed lease it cannot expire during a legitimate long scrape.
        """
        directory = self.path.parent / (self.path.name + ".job-locks")
        directory.mkdir(parents=True, exist_ok=True)
        lock_path = directory / (hashlib.sha256(job.encode()).hexdigest() + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(f"job {job!r} is already running against this cache") from None
            yield
        finally:
            os.close(fd)

    # ------------------------------------------------------------------ cache
    @staticmethod
    def keys_for(item: dict) -> list[str]:
        ks = []
        if item.get("username"):
            ks.append("u:" + str(item["username"]).lower())
        if item.get("id"):
            ks.append("id:" + str(item["id"]))
        return ks

    @synchronized
    def cache_get(self, key: str, ttl: float) -> dict | None:
        row = self.db.execute("SELECT item, fetched_at FROM profiles WHERE key=?", (key,)).fetchone()
        age = time.time() - row[1] if row else None
        if age is None or age < 0 or age > ttl:
            return None
        return json.loads(row[0])

    @synchronized
    def cache_put(self, item: dict) -> None:
        if "error" in item or not isinstance(item.get("biography"), str):
            return
        blob, now = json.dumps(item, ensure_ascii=False), time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            keys = self.keys_for(item)
            # A fresh identity observation retires the old pair of aliases. A
            # renamed account must not stay cached under its former username,
            # and a reassigned username must invalidate the former ID's payload.
            # Match the saved blob too: another alias may already have been
            # claimed independently, and that newer observation must survive.
            for key in keys:
                previous = self.db.execute("SELECT item FROM profiles WHERE key=?", (key,)).fetchone()
                if previous is not None:
                    for alias in self.keys_for(json.loads(previous[0])):
                        if alias not in keys:
                            self.db.execute("DELETE FROM profiles WHERE key=? AND item=?", (alias, previous[0]))
            for k in keys:
                self.db.execute("INSERT OR REPLACE INTO profiles(key, item, fetched_at) VALUES(?,?,?)", (k, blob, now))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    @synchronized
    def cache_purge(self, ttl: float) -> int:
        cur = self.db.execute("DELETE FROM profiles WHERE fetched_at < ?", (time.time() - ttl,))
        return cur.rowcount

    # ------------------------------------------------------------------ jobs
    @synchronized
    def job_init(self, job: str, raws: Iterable[str]) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = self.db.execute("SELECT idx, raw FROM job_items WHERE job=? ORDER BY idx", (job,))
            known = set()
            nxt = 0
            for idx, raw in rows:
                known.add(target_key(raw))
                nxt = idx + 1
            for raw in raws:
                key = target_key(raw)
                if key not in known:
                    known.add(key)
                    self.db.execute("INSERT INTO job_items(job, idx, raw) VALUES(?,?,?)", (job, nxt, raw))
                    nxt += 1
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    @synchronized
    def job_items(self, job: str) -> list[tuple[int, str, str, dict | None]]:
        rows = self.db.execute("SELECT idx, raw, status, item FROM job_items WHERE job=? ORDER BY idx", (job,))
        return [(i, raw, st, json.loads(it) if it else None) for i, raw, st, it in rows.fetchall()]

    def job_iter_items(self, job: str, batch_size: int = 64):
        """Yield all saved results in durable input order, one payload page at a time.

        Includes unacknowledged results so an interrupted run can still export
        its saved output. Call after the runner stops for a stable full export;
        reads do not hold a transaction while the caller writes each item.
        """
        if not 1 <= batch_size <= 900:
            raise ValueError("batch_size must be between 1 and 900")
        after = -1
        while True:
            with self._lock:
                rows = self.db.execute(
                    "SELECT idx, item FROM job_items WHERE job=? AND idx>? "
                    "AND status IN ('done', 'error') AND item IS NOT NULL ORDER BY idx LIMIT ?",
                    (job, after, batch_size)).fetchall()
            if not rows:
                return
            for idx, item in rows:
                after = idx
                yield json.loads(item)

    @synchronized
    def job_order(self, job: str) -> list[tuple[int, str]]:
        return self.db.execute("SELECT idx, raw FROM job_items WHERE job=? ORDER BY idx", (job,)).fetchall()

    @synchronized
    def job_completed_count(self, job: str, pending: set[int]) -> tuple[int, int]:
        completed = errors = 0
        for idx, status in self.db.execute("SELECT idx, status FROM job_items WHERE job=?", (job,)):
            if idx not in pending and status != "pending":
                completed += 1
                errors += status == "error"
        return completed, errors

    def job_undelivered(self, job: str, ordered_indices: Iterable[int] | None = None,
                        batch_size: int = 64):
        """Yield saved, unacknowledged results with bounded JSON payload memory.

        ``ordered_indices`` lets a resumed run retain the caller's input order.
        Only one page of payloads is held while callbacks run; the runner may
        still retain O(N) small indices and input strings for ordering.
        """
        if not 1 <= batch_size <= 900:
            raise ValueError("batch_size must be between 1 and 900")
        if ordered_indices is None:
            after = -1
            while True:
                with self._lock:
                    rows = self.db.execute(
                        "SELECT idx, item FROM job_items WHERE job=? AND idx>? "
                        "AND delivered=0 AND item IS NOT NULL ORDER BY idx LIMIT ?",
                        (job, after, batch_size)).fetchall()
                if not rows:
                    return
                for idx, item in rows:
                    after = idx
                    yield idx, json.loads(item)
        else:
            indices = iter(ordered_indices)
            while True:
                page = []
                for _ in range(batch_size):
                    try:
                        page.append(next(indices))
                    except StopIteration:
                        break
                if not page:
                    return
                placeholders = ",".join("?" for _ in page)
                with self._lock:
                    rows = self.db.execute(
                        "SELECT idx, item FROM job_items WHERE job=? AND idx IN (" + placeholders + ") "
                        "AND delivered=0 AND item IS NOT NULL", (job, *page)).fetchall()
                payloads = dict(rows)
                for idx in page:
                    if idx in payloads:
                        yield idx, json.loads(payloads[idx])

    @synchronized
    def job_undelivered_indices(self, job: str) -> set[int]:
        return {idx for (idx,) in self.db.execute(
            "SELECT idx FROM job_items WHERE job=? AND delivered=0 AND item IS NOT NULL", (job,))}

    @synchronized
    def job_pending(self, job: str, retry_errors: bool = True, max_age: float | None = None) -> list[tuple[int, str]]:
        out = []
        now = time.time()
        cutoff = now - max_age if max_age is not None else None
        rows = self.db.execute("SELECT idx, raw, status, item, updated, delivered FROM job_items WHERE job=? ORDER BY idx", (job,))
        for idx, raw, st, blob, updated, delivered in rows:
            if st == "pending":
                out.append((idx, raw))
                continue
            item = json.loads(blob) if blob else None
            error = (item or {}).get("error")
            if error in FINAL_ERRORS:
                continue
            if st == "error":
                if retry_errors and delivered and not error in FINAL_ERRORS:
                    out.append((idx, raw))
                continue
            if not delivered:
                continue  # replay the saved item; never refetch before callback acknowledgement
            coverage = (item or {}).get("_scrape") or {}
            incomplete = st == "done" and (not isinstance((item or {}).get("biography"), str)
                or coverage.get("bioStatus") not in ("observed", "empty") or not coverage.get("observedAt"))
            invalid_time = updated is None or updated > now
            if incomplete or invalid_time or (cutoff is not None and updated < cutoff):
                out.append((idx, raw))
        return out

    @synchronized
    def job_done(self, job: str, idx: int, item: dict, *, delivered: bool = True) -> None:
        st = "error" if "error" in item else "done"
        self.db.execute("UPDATE job_items SET status=?, item=?, updated=?, delivered=? WHERE job=? AND idx=?",
                        (st, json.dumps(item, ensure_ascii=False), time.time(), int(delivered), job, idx))

    @synchronized
    def job_ack(self, job: str, idx: int) -> None:
        self.db.execute("UPDATE job_items SET delivered=1 WHERE job=? AND idx=?", (job, idx))

    @synchronized
    def job_summary(self, job: str) -> dict:
        rows = self.db.execute("SELECT status, COUNT(*) FROM job_items WHERE job=? GROUP BY status", (job,))
        return dict(rows.fetchall())
