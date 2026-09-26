"""Reference implementation for Apify's `includeAboutSection` ("About this account").

NOT used by igscraper yet; the builder can move it into igscraper/. Kept here (tests/parity) because the parity lane
must not edit igscraper/. See docs/parity-deep-dive.md §1.

Instagram surface
-----------------
The data behind "About this account" comes from a Bloks app, not from web_profile_info:

* mobile:  POST https://i.instagram.com/api/v1/bloks/apps/com.instagram.interactions.about_this_account/
           form: referer_type=ProfileMore, target_user_id=<pk>, bk_client_context={"bloks_version":<v>,"styles_id":"instagram"},
                 bloks_versioning_id=<v>, _uuid=<device uuid>   (instagrapi `user_about_v1`)
* web:     POST https://www.instagram.com/async/wbloks/fetch/?appid=com.bloks.www.ig.about_this_account&type=app&__bkv=<v>
           form: params={"referer_type":"ProfileMore","target_user_id":"<pk>"} + fb_dtsg/lsd/jazoest + csrftoken cookie
           response is prefixed with "for (;;);"

Every open-source client found (instagrapi, uosint, ig-profile-finder, weyn-tools, ig-checker, CreatorScan) sends a
logged-in session (Authorization bearer / sessionid cookie / fb_dtsg). On the web the dialog is only reachable when
logged in. Treat it as LOGIN-REQUIRED: one extra request per profile on a *session* egress, which is why Apify sells it
as a paid add-on ($0.007/profile vs $0.0026 base, per the 2026-09-02 run report).

What the response contains (as parsed by the clients above): text rows "Date joined" -> "August 2013",
"Account based in" -> "United States" (also in a Bloks state keyed IG_ABOUT_THIS_ACCOUNT:about_this_account_country),
"Former usernames" -> count (the list itself is a second Bloks call), "Verified" -> date, when verified.

Apify output shape (README NASA sample, keys alphabetical):
    {"accounts_with_shared_followers": null, "country": "United States", "date_joined": "August 2013",
     "date_joined_as_timestamp": 1375315200, "date_verified": null, "date_verified_as_timestamp": null,
     "former_usernames": 0, "id": 528817151, "is_verified": true, "username": "nasa"}
Note `id` is an int (unlike every other id in the item) and `date_joined_as_timestamp` is the first day of the month,
00:00 UTC.
"""
from __future__ import annotations

import calendar
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

MOBILE_URL = "https://i.instagram.com/api/v1/bloks/apps/com.instagram.interactions.about_this_account/"
WEB_URL = "https://www.instagram.com/async/wbloks/fetch/?appid=com.bloks.www.ig.about_this_account&type=app&__bkv={bkv}"
ABOUT_KEYS = ["accounts_with_shared_followers", "country", "date_joined", "date_joined_as_timestamp", "date_verified",
              "date_verified_as_timestamp", "former_usernames", "id", "is_verified", "username"]
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')


def mobile_request(user_id: str | int, bloks_version: str) -> dict:
    """Arguments for httpx: client.post(**mobile_request(pk, v), headers=<logged-in mobile headers>)."""
    ctx = json.dumps({"bloks_version": bloks_version, "styles_id": "instagram"})
    return {"url": MOBILE_URL, "data": {"referer_type": "ProfileMore", "target_user_id": str(user_id),
                                        "bk_client_context": ctx, "bloks_versioning_id": bloks_version,
                                        "_uuid": str(uuid.uuid4())}}


def _strings(payload: Any) -> list[str]:
    """All string literals in document order, including ones embedded in Bloks lispy source strings."""
    out: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            if '"' in o and ("(" in o or "{" in o):          # lispy / embedded JSON: pull literals out of it
                out.extend(json.loads(f'"{s}"') for s in _STR.findall(o))
            else:
                out.append(o)
    walk(payload)
    return [s.strip() for s in out if s and s.strip()]


def _state(payload: Any, key_suffix: str) -> str | None:
    found: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if str(o.get("key", "")).endswith(key_suffix) and isinstance(o.get("initial"), str):
                found.append(o["initial"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(payload)
    return found[0] if found else None


def _after(texts: list[str], label: str) -> str | None:
    for i, t in enumerate(texts):
        if t.lower() == label.lower() and i + 1 < len(texts):
            return texts[i + 1]
    return None


def month_ts(text: str | None) -> int | None:
    """'August 2013' -> 1375315200 (first of month, UTC), as in Apify's sample."""
    if not text:
        return None
    m = re.search(r"([A-Za-z]+)\s+(\d{4})", text)
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    return int(datetime(int(m.group(2)), _MONTHS[m.group(1).lower()], 1, tzinfo=timezone.utc).timestamp())


def parse_about_response(text: str) -> dict:
    """Bloks response text -> {country, date_joined, date_verified, former_usernames} (values may be None)."""
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):]
    payload = json.loads(text)
    texts = _strings(payload)
    country = _state(payload, "about_this_account_country") or _after(texts, "Account based in")
    if country in ("Not shared", ""):
        country = None
    former = None
    for i, t in enumerate(texts):
        m = re.fullmatch(r"Former usernames\s*(\d+)?", t, re.I)
        if m:
            former = int(m.group(1)) if m.group(1) else next((int(x) for x in texts[i + 1:i + 3] if x.isdigit()), None)
            break
    verified = _after(texts, "Verified")
    if verified and not month_ts(verified):
        verified = None
    return {"country": country, "date_joined": _after(texts, "Date joined"), "date_verified": verified,
            "former_usernames": former if former is not None else 0}


def map_about(parsed: dict, user_id: str | int, username: str, is_verified: bool) -> dict:
    """Parsed About data + profile basics -> Apify `about` object (exact key order/types of the README sample)."""
    about = {
        "accounts_with_shared_followers": None,       # viewer-session dependent; Apify emits null
        "country": parsed.get("country"),
        "date_joined": parsed.get("date_joined"),
        "date_joined_as_timestamp": month_ts(parsed.get("date_joined")),
        "date_verified": parsed.get("date_verified"),
        "date_verified_as_timestamp": month_ts(parsed.get("date_verified")),
        "former_usernames": int(parsed.get("former_usernames") or 0),
        "id": int(user_id),
        "is_verified": bool(is_verified),
        "username": username,
    }
    assert list(about) == ABOUT_KEYS
    return about
