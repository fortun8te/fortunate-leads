"""Egress definitions: direct connection, http/https/socks proxy, or HTTP relay (Cloudflare-Worker style).

Spec formats (CLI --egress, env IG_EGRESS (comma-separated), --egress-file / env IG_EGRESS_FILE):

    direct
    http://user:pass@host:port            socks5://127.0.0.1:9050        host:port[:user:pass]
    relay:https://relay.example.workers.dev/?u={url}      ({url} = percent-encoded target URL)
    tor:127.0.0.1:9152:36                 36 Tor circuits (IsolateSOCKSAuth); a blocked circuit is swapped
                                          for a fresh one after a short rest instead of a 15-min park

--egress-file may be plain text (one spec per line, '#' comments) or JSON:

    {"egresses": [
       {"name": "home-mac", "type": "proxy", "url": "socks5://10.0.0.5:1080"},
       {"type": "relay", "url": "https://r1.example.workers.dev/?u={url}", "headers": {"x-relay-key": "..."}},
       {"type": "direct", "startRpm": 30, "maxRpm": 60}
    ]}
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse


@dataclass
class EgressSpec:
    kind: str                      # "direct" | "proxy" | "relay"
    url: str | None = None         # proxy URL or relay template
    name: str | None = None
    headers: dict = field(default_factory=dict)   # extra headers sent to a relay (e.g. an auth key)
    start_rpm: float | None = None
    max_rpm: float | None = None
    tor: bool = False              # Tor SOCKS circuit: on a block, switch to a fresh circuit instead of parking
    rotation: int = 0

    def rotated(self) -> "EgressSpec":
        """New SOCKS username -> Tor (IsolateSOCKSAuth) isolates a new circuit; its exit is not guaranteed unique."""
        import secrets
        u = urlparse(self.url or "")
        user = f"{(self.name or 'tor')}-{secrets.token_hex(4)}"
        url = f"{u.scheme}://{user}:x@{u.hostname}:{u.port}"
        return EgressSpec(self.kind, url, self.name, dict(self.headers), self.start_rpm, self.max_rpm, True,
                          self.rotation + 1)
    @property
    def label(self) -> str:
        if self.name:
            return self.name + (f"#{self.rotation}" if self.tor and self.rotation else "")
        if self.kind == "direct":
            return "direct"
        u = urlparse(self.url or "")
        if self.kind == "relay":
            return f"relay:{u.scheme}://{u.hostname}"
        return f"{u.scheme}://{u.hostname}:{u.port}"      # never includes credentials

    def target_url(self, url: str) -> str:
        if self.kind != "relay":
            return url
        t = self.url or ""
        if "{url}" in t:
            return t.replace("{url}", quote(url, safe=""))
        if "<encoded target>" in t:
            return t.replace("<encoded target>", quote(url, safe=""))
        return t + quote(url, safe="")


TOR_PORTS = {9050, 9150, 9152}


def looks_like_tor(url: str | None) -> bool:
    """socks5 proxy on a standard Tor SocksPort (9050/9150/9152) -> treat as Tor (rotate circuits on blocks).
    Circuit rotation uses a fresh SOCKS username, which gives a new circuit when the SocksPort has
    IsolateSOCKSAuth (Tor's default isolation flags include it) - the equivalent of NEWNYM for that stream."""
    if not url:
        return False
    u = urlparse(url)
    try:
        return u.scheme.lower().startswith("socks5") and u.port in TOR_PORTS
    except ValueError:
        return False


def normalise_proxy(line: str) -> str | None:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if "://" in s:
        return s
    parts = s.split(":")
    if len(parts) == 2:                                   # host:port
        return f"http://{parts[0]}:{parts[1]}"
    if len(parts) == 4:                                   # host:port:user:pass
        return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    if "@" in s:
        return "http://" + s
    return None


def parse_spec(s: str) -> EgressSpec | None:
    s = s.strip()
    if not s or s.startswith("#"):
        return None
    if s.lower() == "direct":
        return EgressSpec("direct")
    if s.lower().startswith("relay:"):
        return EgressSpec("relay", s[len("relay:"):])
    if s.lower().startswith("tor:"):
        return expand_tor(s)[0]
    p = normalise_proxy(s)
    if not p:
        return None
    if looks_like_tor(p):
        from .net import TOR_MAX_RPM, TOR_START_RPM
        return EgressSpec("proxy", p, start_rpm=TOR_START_RPM, max_rpm=TOR_MAX_RPM, tor=True)
    return EgressSpec("proxy", p)


def _from_obj(o: dict) -> EgressSpec:
    kind = (o.get("type") or o.get("kind") or ("proxy" if o.get("url") else "direct")).lower()
    if kind not in ("direct", "proxy", "relay"):
        raise ValueError(f"unknown egress type {kind!r}")
    url = o.get("url")
    if kind == "proxy":
        url = normalise_proxy(url or "")
        if not url:
            raise ValueError(f"bad proxy url in {o!r}")
    if kind == "relay" and not url:
        raise ValueError("relay egress needs a url template")
    tor = bool(o["tor"]) if "tor" in o else (kind == "proxy" and looks_like_tor(url))
    from .net import TOR_MAX_RPM, TOR_START_RPM
    return EgressSpec(kind, url, o.get("name"), dict(o.get("headers") or {}),
                      o.get("startRpm") or (TOR_START_RPM if tor else None),
                      o.get("maxRpm") or (TOR_MAX_RPM if tor else None), tor)


def expand_tor(s: str) -> list[EgressSpec]:
    """'tor:127.0.0.1:9152:16' -> 16 isolated-circuit SOCKS egresses (needs IsolateSOCKSAuth on that port)."""
    parts = s.split(":")[1:]
    host = parts[0] if parts and parts[0] else "127.0.0.1"
    port = int(parts[1]) if len(parts) > 1 and parts[1] else 9050
    n = int(parts[2]) if len(parts) > 2 and parts[2] else 1
    from .net import TOR_MAX_RPM, TOR_START_RPM
    return [EgressSpec("proxy", f"socks5://c{i}-0:x@{host}:{port}", f"tor{i}", start_rpm=TOR_START_RPM,
                       max_rpm=TOR_MAX_RPM, tor=True) for i in range(n)]


def parse_specs(s: str) -> list[EgressSpec]:
    s = s.strip()
    if s.lower().startswith("tor:"):
        return expand_tor(s)
    e = parse_spec(s)
    return [e] if e else []


def load_egress_file(path: str) -> list[EgressSpec]:
    """Text (one spec per line), JSON ({"egresses": [...]} or a list) or YAML (.yaml/.yml; needs PyYAML).

    Object types: direct | proxy | relay | tor (host, port, circuits) | mac-node (host, port=8899 -> http proxy).
    """
    text = Path(path).read_text(encoding="utf-8")
    st = text.lstrip()
    data = None
    if str(path).lower().endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError as e:
            raise ValueError(f"{path}: YAML egress files need PyYAML (pip install pyyaml) - "
                             "or use the JSON format") from e
        data = yaml.safe_load(text) or {}
    elif st.startswith("{") or st.startswith("["):
        data = json.loads(text)
    if data is not None:
        items = list((data.get("egresses") or []) if isinstance(data, dict) else data)
        if isinstance(data, dict) and isinstance(data.get("tor"), dict):     # preset:  tor: {circuits: 36}
            t = data["tor"]
            items.append({"type": "tor", "host": t.get("host", "127.0.0.1"), "port": t.get("port", 9152),
                          "circuits": t.get("circuits", 36)})
        out: list[EgressSpec] = []
        for o in items:
            if isinstance(o, dict) and (o.get("type") or "").lower() == "tor":
                out += expand_tor(f"tor:{o.get('host', '127.0.0.1')}:{o.get('port', 9050)}:{o.get('circuits', 1)}")
            elif isinstance(o, dict) and (o.get("type") or "").lower() in ("mac-node", "mac_node", "node"):
                if not o.get("host"):
                    raise ValueError(f"mac-node egress needs a host: {o!r}")
                out.append(EgressSpec("proxy", f"http://{o['host']}:{o.get('port', 8899)}",
                                      o.get("name") or f"node-{o['host']}", dict(o.get("headers") or {}),
                                      o.get("startRpm"), o.get("maxRpm")))
            elif isinstance(o, dict):
                out.append(_from_obj(o))
            else:
                out += parse_specs(str(o))
        return out
    return [e for l in text.splitlines() for e in parse_specs(l)]


def egresses_from_env() -> list[EgressSpec]:
    out: list[EgressSpec] = []
    if os.environ.get("IG_EGRESS_FILE"):
        out += load_egress_file(os.environ["IG_EGRESS_FILE"])
    for s in (os.environ.get("IG_EGRESS") or "").split(","):
        out += parse_specs(s)
    return out
