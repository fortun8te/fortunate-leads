"""Offline mock of Instagram for `igscrape selftest` and IGSCRAPE_MOCK=1 demo runs. Never touches the network.

Every username gets a copy of a real recorded `web_profile_info` response (NASA, recorded 2026-09-24) with
the username swapped in; names containing "doesnotexist" get Instagram's real 404 page.
"""
from __future__ import annotations

import asyncio
import json
import os
from functools import lru_cache
from pathlib import Path

import httpx

FIX = Path(__file__).resolve().parent / "selftest"


@lru_cache(maxsize=None)
def _fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _wpi_for(username: str) -> httpx.Response:
    if "doesnotexist" in username:
        f = _fixture("wpi_thisuserdoesnotexist_zq9x7.json")
        return httpx.Response(f["status"], headers={"content-type": f["content_type"]}, text=f["body"])
    f = _fixture("wpi_nasa.json")
    data = json.loads(f["body"])
    data["data"]["user"]["username"] = username
    return httpx.Response(200, headers={"content-type": "application/json"}, json=data)


def transport_factory(delay: float | None = None):
    if delay is None:
        delay = float(os.environ.get("IGSCRAPE_MOCK_DELAY", "0.05"))

    async def handler(request: httpx.Request) -> httpx.Response:
        if delay:
            await asyncio.sleep(delay)
        url = str(request.url)
        if "web_profile_info" in url:
            return _wpi_for(request.url.params.get("username", ""))
        if "/feed/user/" in url:
            return httpx.Response(200, json={"items": [], "num_results": 0, "status": "ok"})
        if "/info/" in url:
            return httpx.Response(404, text="not found")
        if url.startswith("https://www.instagram.com/"):
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html></html>")
        return httpx.Response(500)

    return lambda proxy: httpx.MockTransport(handler)


def enabled() -> bool:
    return os.environ.get("IGSCRAPE_MOCK", "").strip().lower() in ("1", "true", "yes", "on")
