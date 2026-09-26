"""`python -m igscraper tor up|status` - the free default egress: ONE local tor process, many circuits.

SocksPort 9152 with IsolateSOCKSAuth + IPv6Traffic PreferIPv6: every SOCKS username (c0, c1, ... cN) gets its own
isolated circuit; multiple circuits may share an exit IP. IPv6 exits were far less walled than IPv4 ones in our tests (11/11 vs 2/6).
Use it with:  --egress tor:127.0.0.1:9152:36   (36 circuits, adaptive 10->15 req/min each; ~150 profiles/min)
"""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

TOR_PORT = 9152
TOR_CIRCUITS = 36
FREE_EGRESS = f"tor:127.0.0.1:{TOR_PORT}:{TOR_CIRCUITS}"


def install_hint() -> str:
    if platform.system() == "Darwin":
        return "tor is not installed. Install it with:  brew install tor"
    if platform.system() == "Windows":
        return "tor is not installed. Install the Tor Expert Bundle from https://www.torproject.org/download/tor/"
    return "tor is not installed. Install it with:  sudo apt install tor   (or: brew install tor)"


def port_open(port: int = TOR_PORT, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def torrc_text(data_dir: Path, port: int = TOR_PORT) -> str:
    return (f"DataDirectory {data_dir}\n"
            f"SocksPort 127.0.0.1:{port} IsolateSOCKSAuth IPv6Traffic PreferIPv6\n"
            "MaxCircuitDirtiness 600\nNumEntryGuards 8\nRunAsDaemon 0\n"
            f"Log notice file {data_dir.parent / 'tor.log'}\n")


def tor_up(port: int = TOR_PORT, home: Path | None = None, wait: float = 90.0, tor_bin: str | None = None) -> int:
    if port_open(port):
        print(f"tor already listening on 127.0.0.1:{port}. Use:  --egress tor:127.0.0.1:{port}:{TOR_CIRCUITS}")
        return 0
    tor = tor_bin or shutil.which("tor")
    if not tor:
        print(install_hint(), file=sys.stderr)
        return 3
    home = home or Path(os.environ.get("IGSCRAPER_HOME", Path.home() / ".igscraper")) / "tor"
    data = home / "data"
    data.mkdir(parents=True, exist_ok=True)
    os.chmod(data, 0o700)
    rc = home / "torrc"
    rc.write_text(torrc_text(data, port))
    subprocess.Popen([tor, "-f", str(rc)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait:
        if port_open(port):
            print(f"tor started (torrc {rc}); SOCKS on 127.0.0.1:{port}. Bootstrapping circuits can take ~30 s.\n"
                  f"Use:  python -m igscraper --egress tor:127.0.0.1:{port}:{TOR_CIRCUITS} -u nasa")
            return 0
        time.sleep(0.5)
    print(f"tor did not open port {port} within {wait:.0f}s; see {home / 'tor.log'}", file=sys.stderr)
    return 4


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "up":
        return tor_up()
    if cmd == "status":
        ok = port_open()
        print(f"tor on 127.0.0.1:{TOR_PORT}: {'UP' if ok else 'DOWN'}" + ("" if ok or shutil.which("tor")
                                                                              else f" ({install_hint()})"))
        return 0 if ok else 1
    if cmd == "torrc":
        print(torrc_text(Path("~/.igscraper/tor/data").expanduser()), end="")
        return 0
    print("usage: python -m igscraper tor up|status|torrc", file=sys.stderr)
    return 2
