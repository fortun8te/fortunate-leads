#!/usr/bin/env python3
"""Proves the server hands out scrape work, without touching the real queue.

Copies data/leads.sqlite (sqlite online backup, safe while the server runs) into a temp dir, starts a second server on
a free port against the copy (FL_NO_ORSLOT=1: no model calls), then for every lane in the copy plus a fresh lane:
heartbeat + /api/ext/next?kinds=list,profile and ?kinds=list, exactly as the extension does. Exit 0 when every lane that
may work gets a job. The real DB is only read.

    python3 ops/selftest-scrape.py [--db data/leads.sqlite]
"""
import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORIGIN = 'chrome-extension://fgdbghllamedgihmdcolaggnbhnakjnf'


def call(port, path, body=None):
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=None if body is None else json.dumps(body).encode(),
                                 headers={'Origin': ORIGIN, 'Content-Type': 'application/json'},
                                 method='GET' if body is None else 'POST')
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=str(ROOT / 'data' / 'leads.sqlite'))
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix='fl-selftest-'))
    copy = tmp / 'copy.sqlite'
    src = sqlite3.connect(f'file:{a.db}?mode=ro', uri=True)
    dst = sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.row_factory = sqlite3.Row
    lanes = [dict(r) for r in dst.execute('SELECT lane_id, handle, role, is_main, paused, hold FROM accounts')]
    q = dict(dst.execute("SELECT kind, count(*) FROM jobs WHERE state IN ('queued','leased') GROUP BY kind").fetchall())
    dst.close()
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    env = dict(os.environ, FL_NO_ORSLOT='1')
    srv = subprocess.Popen([sys.executable, str(ROOT / 'server' / 'server.py'), '--db', str(copy), '--port', str(port)],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    bad = 0
    try:
        for _ in range(50):
            try:
                call(port, '/api/scraper')
                break
            except OSError:
                time.sleep(0.2)
        print(f'queue in copy: {q}')
        for ln in lanes + [{'lane_id': 'selftest_fresh', 'handle': None, 'role': 'both', 'is_main': 0, 'paused': 0, 'hold': None}]:
            acct = {'ig_id': '1', 'handle': ln['handle']} if ln['handle'] else {'ig_id': '1'}
            hb = call(port, '/api/ext/heartbeat', {'lane_id': ln['lane_id'], 'version': 'selftest', 'state': 'idle', 'account': acct})
            both = call(port, f"/api/ext/next?kinds=list,profile&lane={ln['lane_id']}")
            lst = call(port, f"/api/ext/next?kinds=list&lane={ln['lane_id']}")
            may = not hb.get('paused') and not ln['hold']
            got = [j['job'] and j['job']['kind'] for j in (both, lst)]
            ok = not may or all(got)
            bad += not ok
            print(f"{'OK  ' if ok else 'FAIL'} lane {ln['lane_id']} @{ln['handle']} role={ln['role']} main={ln['is_main']} "
                  f"paused={hb.get('paused')} hold={ln['hold']} -> list,profile: {got[0]}  list: {got[1]}")
    finally:
        srv.terminate()
        srv.wait(5)
        shutil.rmtree(tmp, ignore_errors=True)
    print('PASS: work is handed out' if not bad else f'FAIL: {bad} lane(s) got no work')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
