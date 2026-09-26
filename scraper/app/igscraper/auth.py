"""Optional logged-in mode: sessionid cookie or Netscape cookies.txt (flag or env). Nothing is stored."""
from __future__ import annotations

from pathlib import Path


def load_netscape_cookies(path: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, name, value = parts[0], parts[5], parts[6]
        d = domain.lower().lstrip(".")
        if d == "instagram.com" or d.endswith(".instagram.com"):
            cookies[name] = value
    return cookies


def build_cookies(sessionid: str | None, cookies_file: str | None) -> dict[str, str]:
    cookies: dict[str, str] = {}
    if cookies_file:
        cookies.update(load_netscape_cookies(cookies_file))
    if sessionid:
        cookies["sessionid"] = sessionid
    return cookies


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())
