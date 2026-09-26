"""Load egresses.yaml (friendly format) and hand it to igscraper's own egress loader.

The YAML is converted to igscraper's JSON egress-file format and parsed by `igscraper.egress.load_egress_file`,
so every egress type igscraper supports works here, with the same validation.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from igscraper.egress import EgressSpec, load_egress_file, parse_specs

_KEYMAP = {"start_rpm": "startRpm", "max_rpm": "maxRpm", "kind": "type"}


def _norm(entry):
    if isinstance(entry, str):
        entry = entry.strip()
        if entry.lower().startswith("tor:"):
            parts = entry.split(":")
            if len(parts) > 4 or (len(parts) > 3 and not _valid_circuits(parts[3])):
                raise ValueError("invalid Tor circuit count")
        if entry and not parse_specs(entry):
            raise ValueError("invalid egress text line")
        return entry
    if not isinstance(entry, dict):
        raise ValueError("egress entry must be a text line or a mapping")
    out = {_KEYMAP.get(k, k): v for k, v in entry.items()}
    t = str(out.get("type") or "").lower()
    if t == "tor" and not _valid_circuits(out.get("circuits", 1)):
        raise ValueError("invalid Tor circuit count")
    if t in ("mac", "remote-mac", "mac-node", "node"):       # a remote Mac node = an HTTP proxy on that Mac
        out["type"] = "proxy"
        if not out.get("url") and out.get("host"):
            out["url"] = f"http://{out['host']}:{out.get('port', 8899)}"
    elif t in ("socks5", "socks", "http", "https"):
        out["type"] = "proxy"
    if out.get("type") == "proxy" and not out.get("url") and out.get("host"):
        if not out.get("port"):
            raise ValueError("proxy egress with a host needs a port")
        scheme = t if t in ("socks5", "http", "https") else "http"
        out["url"] = f"{scheme}://{out['host']}:{out['port']}"
    for k in ("host", "port") if out.get("type") != "tor" else ():
        out.pop(k, None)
    return out


def _valid_circuits(value) -> bool:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return False
    return not isinstance(value, bool) and str(value) == str(count) and 1 <= count <= 256


def _check_identities(specs: list[EgressSpec]) -> list[EgressSpec]:
    """Cooldown storage keys lanes by name, or by endpoint when unnamed."""
    seen = set()
    for spec in specs:
        endpoint = urlparse(spec.url or "")
        if spec.kind != "direct" and not endpoint.hostname:
            raise ValueError("egress URL needs a host")
        if spec.kind == "proxy" and not endpoint.port:
            raise ValueError("proxy URL needs a port")
        if spec.kind == "proxy" and endpoint.scheme not in ("http", "https", "socks5"):
            raise ValueError("unsupported proxy URL scheme")
        identity = spec.name or f"{endpoint.hostname}:{endpoint.port}{endpoint.path}"
        key = (spec.kind, identity)
        if key in seen:
            raise ValueError("duplicate egress identity; give each lane a unique name or endpoint")
        seen.add(key)
    return specs


def load_yaml_egresses(path: str | os.PathLike) -> list[EgressSpec]:
    p = Path(path).expanduser()
    text = p.read_text(encoding="utf-8")
    st = text.lstrip()
    if p.suffix.lower() not in (".yaml", ".yml") or st.startswith("{"):
        try:
            return _check_identities(load_egress_file(str(p)))
        except (ValueError, TypeError) as e:
            raise ValueError(f"{p}: invalid egress configuration") from None
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise SystemExit("PyYAML is missing: re-run install.sh") from e
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ValueError(f"{p}: not valid YAML") from None
    if data is None:
        return []
    if isinstance(data, dict):
        if set(data) != {"egresses"}:
            raise ValueError(f"{p}: expected an egresses list")
        items = data["egresses"]
    else:
        items = data
    if not isinstance(items, list):
        raise ValueError(f"{p}: egresses must be a list")
    try:
        items = [_norm(x) for x in items if x not in (None, "")]
    except (ValueError, TypeError):
        raise ValueError(f"{p}: invalid egress entry") from None
    if not items:
        return []
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"egresses": items}, f)
        tmp = f.name
    try:
        try:
            return _check_identities(load_egress_file(tmp))
        except (ValueError, TypeError):
            raise ValueError(f"{p}: invalid egress configuration or duplicate identity") from None
    finally:
        os.unlink(tmp)


def describe(specs: list[EgressSpec]) -> list[str]:
    out = []
    for s in specs:
        extra = []
        if s.start_rpm or s.max_rpm:
            extra.append(f"rpm {s.start_rpm or '-'}..{s.max_rpm or '-'}")
        if s.tor:
            extra.append("tor circuit")
        where = ""
        if s.name and s.url:                       # show the endpoint too, never the credentials
            u = urlparse(s.url)
            where = f" -> {u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
        out.append(f"{s.kind:6s} {s.label}{where}" + (f"  ({', '.join(extra)})" if extra else ""))
    return out
