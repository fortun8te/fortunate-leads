#!/usr/bin/env python3
"""Runs the real server/server.py (same handlers, same SQL, same main()) on the simulator's clock.

Two things differ from `python3 server/server.py --db X --port N`, both needed for compressed time:
  1. Clock: every request may carry `X-Sim-Now: <epoch ms>`; `datetime.now()` inside server.py/db.py then returns that
     simulated time (monotonic). Leases (10 min), soak windows, heartbeat freshness and `today` all follow the
     simulation instead of wall time. Without the header the real clock is used.
  2. Background workers: instead of wall-clock threads (qualify every 1 s, profile planner every 15 s), the driver
     calls GET /__sim/tick on the simulated clock, which runs the same qualify_batch() and plan_profiles() once.
     llm_step (needs the local OpenRouter proxy) and pfp_step (needs the Instagram CDN) are not run.
Usage: python3 tests/e2e/sim_server.py --db /tmp/x.sqlite --port 18777
"""
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = Path(os.environ.get('FL_SERVER_DIR') or ROOT / 'server')  # override to test a patched copy
sys.path.insert(0, str(SERVER_DIR))
import db  # noqa: E402
import server  # noqa: E402
import accounts  # noqa: E402
import control  # noqa: E402
import pipeline_log  # noqa: E402

LOCK = threading.Lock()
VNOW = {'ms': None}


class SimDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        ms = VNOW['ms']
        if ms is None:
            return datetime.now(tz)
        d = datetime.fromtimestamp(ms / 1000, timezone.utc)
        return d.astimezone(tz) if tz else datetime.fromtimestamp(ms / 1000)


# Request ownership, operator review and evidence must share the same clock as
# the simulated heartbeat. Real time would make healthy virtual accounts stale.
for mod in (server, db, accounts, control, pipeline_log):
    mod.datetime = SimDateTime

server.start_workers = lambda stop: None
_route = server.Handler.route


def route(self, method):
    v = self.headers.get('X-Sim-Now')
    if v:
        with LOCK:
            VNOW['ms'] = max(VNOW['ms'] or 0, int(v))
    if self.path.startswith('/__sim/tick'):
        conn = db.connect(server.CFG['db'])
        try:
            out = {'qualified': server.qualify_batch(conn, 5000), 'planned': server.plan_profiles(conn)}
        finally:
            conn.close()
        return self.send(200, out)
    return _route(self, method)


server.Handler.route = route

if __name__ == '__main__':
    sys.argv[0] = str(SERVER_DIR / 'server.py')
    server.main()
