"""Input normalisation: usernames, @handles, profile URLs and numeric profile IDs.

Mirrors Apify's input contract: ``{"usernames": [...], "includeAboutSection": false}``
where every entry may be a username, a profile URL, or a numeric profile ID.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_RESERVED = {"p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct", "about", "developer", "_u"}
_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am", "www.instagr.am"}


@dataclass(frozen=True)
class Target:
    raw: str                      # exactly what the user passed
    username: str | None          # resolved handle, if known up-front
    user_id: str | None           # numeric ID, if the input looked like one
    input_url: str                # value emitted as ``inputUrl``


def parse_target(raw: str) -> Target:
    s = ("" if raw is None else str(raw)).strip()
    if not s:
        raise ValueError("empty username")
    if s.startswith("@"):
        s = s[1:]
    low = s.lower()
    if "instagram.com" in low or "instagr.am" in low or ":" in s or "/" in s or "?" in s or "#" in s:
        url = s if low.startswith(("http://", "https://")) else "https://" + s
        try:
            pu = urlsplit(url)
            host = pu.hostname
            valid_authority = pu.username is None and pu.password is None and pu.port is None
        except ValueError:
            raise ValueError(f"not an Instagram URL: {raw}") from None
        if pu.scheme.lower() not in ("http", "https") or host not in _HOSTS or not valid_authority:
            raise ValueError(f"not an Instagram URL: {raw}")
        parts = pu.path.split("/")
        if len(parts) < 2 or not parts[1]:
            raise ValueError(f"no username in URL: {raw}")
        if parts[1].lower() == "_u" and len(parts) in (3, 4) and (len(parts) == 3 or not parts[3]):
            handle = unquote(parts[2])
            numeric_id = True
        else:
            if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2]):
                raise ValueError(f"not a profile URL: {raw}")
            handle = unquote(parts[1])
            numeric_id = False
        if handle.lower() in _RESERVED or (numeric_id and not handle.isascii()):
            raise ValueError(f"not a profile URL: {raw}")
        if not _USERNAME_RE.fullmatch(handle):
            raise ValueError(f"invalid username in URL: {raw}")
        if numeric_id:
            if not handle.isdigit():
                raise ValueError(f"invalid numeric profile ID: {raw}")
            return Target(raw, None, handle, f"https://www.instagram.com/_u/{handle}")
        return Target(raw, handle.lower(), None, f"https://www.instagram.com/{handle.lower()}")
    if s.isascii() and s.isdigit():
        # Apify accepts profile IDs (> 2). Resolved to a username at run time.
        return Target(raw, None, s, f"https://www.instagram.com/_u/{s}")
    if not _USERNAME_RE.fullmatch(s) or s.lower() in _RESERVED:
        raise ValueError(f"invalid username: {raw}")
    return Target(raw, s.lower(), None, f"https://www.instagram.com/{s.lower()}")


def target_key(raw) -> str:
    """Identity of an input: '@nasa', 'nasa' and 'https://m.instagram.com/NASA?hl=en' share one key."""
    s = ("" if raw is None else str(raw)).strip()
    try:
        t = parse_target(s)
    except ValueError:
        return "raw:" + s.lower()
    return f"u:{t.username}" if t.username else f"id:{t.user_id}"


def dedupe(raws: list) -> list[str]:
    seen, out = set(), []
    for r in raws:
        s = ("" if r is None else str(r)).strip()
        if not s:
            continue
        k = target_key(s)
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out


def validate_inputs(raws) -> list[str]:
    """Validate CLI inputs before any network work, preserving the first spelling."""
    if isinstance(raws, (str, bytes)):
        raise ValueError("usernames must be an array of strings")
    out, seen = [], set()
    for index, raw in enumerate(raws, 1):
        if not isinstance(raw, str):
            raise ValueError(f"username {index} must be a string")
        value = raw.strip()
        parse_target(value)
        key = target_key(value)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out
