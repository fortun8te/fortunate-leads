"""Minimal HTTP API. POST /run with Apify input JSON -> dataset items (JSON array).

    {"usernames": ["nasa", "https://www.instagram.com/natgeo/"], "includeAboutSection": false}

Query params: ?format=json|jsonl|csv  &concurrency=N  &omit=latestPosts,relatedProfiles

Async runs with live progress (for UIs):
    POST   /runs                 same body -> {"id": "..."} (202); runs in the background
    GET    /runs/{id}            {id,status,done,total,errors,perMinute,healthyEgresses,etaS}
    GET    /runs/{id}/items      ?format=json|jsonl|csv&omit=...  (items finished so far, input order)
    DELETE /runs/{id}            cancel; finished items stay available
Server-wide egress/cookie/cache config comes from env (IG_EGRESS, IG_EGRESS_FILE, PROXY_URL, IG_SESSIONID,
IG_COOKIES_FILE, IG_CACHE). All requests share ONE scraper, so the per-egress rate limits, parking and cache
apply across concurrent API calls (N parallel calls do not multiply the traffic sent from one IP).

Legacy development API: /runs metadata/results live only in process memory and are lost on restart.
Use the fortunate-leads durable queue for production collection and resume.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import ScraperConfig
from .output import omit_fields, render
from .scraper import ProfileScraper

_shared: dict = {"scraper": None, "factory": None}
_lock = asyncio.Lock()
_runs: dict[str, dict] = {}
MAX_RUNS = 100


async def get_scraper() -> ProfileScraper:
    async with _lock:
        if _shared["scraper"] is None or _shared["factory"] is not ProfileScraper:
            if _shared["scraper"] is not None:
                await _shared["scraper"].__aexit__(None, None, None)
            s = ProfileScraper(ScraperConfig.from_env())
            await s.__aenter__()
            _shared.update(scraper=s, factory=ProfileScraper)
        return _shared["scraper"]


async def close_scraper() -> None:
    pending = []
    for r in _runs.values():
        if r["task"] and not r["task"].done():
            r["task"].cancel()
            pending.append(r["task"])
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if _shared["scraper"] is not None:
        await _shared["scraper"].__aexit__(None, None, None)
        _shared.update(scraper=None, factory=None)


@asynccontextmanager
async def lifespan(_app):
    yield
    await close_scraper()


app = FastAPI(title="igscraper", version="1.2.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "runPersistence": "memory_only", "productionQueue": "fortunate-leads"}


@app.get("/egress")
async def egress() -> dict:
    """Live per-egress health: state (healthy/parked/retired), current rate, ok/failed/blocks."""
    s = _shared["scraper"]
    return {"egress": s.health() if s else [], "stats": s.stats.as_dict() if s else None}


@app.post("/run")
async def run(payload: dict = Body(...), format: str = Query("json"), concurrency: int | None = Query(None),
              omit: str = Query("")):
    usernames = payload.get("usernames")
    if not isinstance(usernames, list) or not all(isinstance(u, str) for u in usernames):
        raise HTTPException(422, "input.usernames must be an array of strings")
    if format not in ("json", "jsonl", "csv"):
        raise HTTPException(422, "format must be json, jsonl or csv")
    s = await get_scraper()
    items = await s.run(usernames, include_about=bool(payload.get("includeAboutSection", False)),
                        concurrency=max(1, min(concurrency, 256)) if concurrency else None)
    items = omit_fields(items, omit.split(","))
    if format == "json":
        return JSONResponse(items)
    media = "text/csv" if format == "csv" else "application/x-ndjson"
    return PlainTextResponse(render(items, format), media_type=media)


def _validate(payload: dict) -> list[str]:
    usernames = payload.get("usernames")
    if not isinstance(usernames, list) or not all(isinstance(u, str) for u in usernames):
        raise HTTPException(422, "input.usernames must be an array of strings")
    return usernames


def _get_run(run_id: str) -> dict:
    r = _runs.get(run_id)
    if r is None:
        raise HTTPException(404, "unknown run id")
    return r


def _ordered(r: dict) -> list[dict]:
    return [r["items"][k] for k in sorted(r["items"])]


@app.post("/runs", status_code=202)
async def start_run(payload: dict = Body(...), concurrency: int | None = Query(None)):
    usernames = _validate(payload)
    s = await get_scraper()
    run_id = uuid.uuid4().hex[:12]
    from .inputs import dedupe, parse_target
    raws = dedupe(usernames)
    index: dict[str, int] = {}
    for i, raw in enumerate(raws):
        index.setdefault(raw, i)
        try:
            index.setdefault(parse_target(raw).input_url, i)
        except ValueError:
            pass
    r = {"id": run_id, "status": "running", "total": len(raws), "errors": 0, "items": {}, "started": time.time(),
         "finished": None, "task": None, "error": None}

    def on_item(item: dict) -> None:
        # items carry their inputUrl; map back to input position for stable ordering
        key = index.get(item.get("inputUrl"), len(r["items"]) + 10**9)
        r["items"][key] = item
        if item.get("error"):
            r["errors"] += 1

    async def go() -> None:
        try:
            items = await s.run(raws, on_item, include_about=bool(payload.get("includeAboutSection", False)),
                                concurrency=max(1, min(concurrency, 256)) if concurrency else None)
            r["items"] = dict(enumerate(items))
            r["status"] = "succeeded"
        except asyncio.CancelledError:
            r["status"] = "cancelled"
        except Exception as e:   # never lose the run record
            r["status"], r["error"] = "failed", f"{type(e).__name__}: {e}"
        finally:
            r["finished"] = time.time()

    if len(_runs) >= MAX_RUNS:   # drop oldest finished runs
        for k in [k for k, v in _runs.items() if v["finished"]][: len(_runs) - MAX_RUNS + 1]:
            _runs.pop(k, None)
    _runs[run_id] = r
    r["task"] = asyncio.create_task(go())
    return {"id": run_id, "total": len(raws)}


@app.get("/runs/{run_id}")
async def run_status(run_id: str) -> dict:
    r = _get_run(run_id)
    done = len(r["items"])
    elapsed = max(1e-6, (r["finished"] or time.time()) - r["started"])
    per_min = done / elapsed * 60
    items = list(r["items"].values())
    cached = sum(not i.get("error") and bool(i.get("_scrape", {}).get("cacheHit")) for i in items)
    fresh = sum(not i.get("error") and not i.get("_scrape", {}).get("cacheHit")
                and i.get("_scrape", {}).get("bioStatus") in ("observed", "empty") for i in items)
    remaining = r["total"] - done
    s = _shared["scraper"]
    return {"id": r["id"], "status": r["status"], "done": done, "total": r["total"], "errors": r["errors"],
            "perMinute": round(fresh / elapsed * 60, 1), "freshSuccess": fresh, "cached": cached,
            "processedPerMinute": round(per_min, 1), "runPersistence": "memory_only",
            "healthyEgresses": s.healthy_count() if s else 0,
            "etaS": 0 if remaining <= 0 else (round(remaining / per_min * 60) if per_min > 0 else None),
            "error": r["error"]}


@app.get("/runs/{run_id}/items")
async def run_items(run_id: str, format: str = Query("json"), omit: str = Query("")):
    if format not in ("json", "jsonl", "csv"):
        raise HTTPException(422, "format must be json, jsonl or csv")
    items = omit_fields(_ordered(_get_run(run_id)), omit.split(","))
    if format == "json":
        return JSONResponse(items)
    media = "text/csv" if format == "csv" else "application/x-ndjson"
    return PlainTextResponse(render(items, format), media_type=media)


@app.delete("/runs/{run_id}")
async def cancel_run(run_id: str) -> dict:
    r = _get_run(run_id)
    if r["task"] and not r["task"].done():
        r["task"].cancel()
        try:
            await r["task"]
        except BaseException:
            pass
    return {"id": run_id, "status": r["status"], "done": len(r["items"])}
