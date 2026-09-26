"""Leadscout: the last qualification step, a Hermes agent that vets the best leads on the open web.

The bulk AI step (free models + SearXNG) scores everyone with a bio. Only people it rates as a likely fit
(content_fit >= SCOUT_MIN) come here, so the agent's time goes to leads that might be real. The agent
(Hermes profile `leadscout`, see ~/.hermes-me/profiles/leadscout/SOUL.md) searches, reads their site and
answers with one JSON object. Its verdict has the final say on fit, its summary becomes the reason and its
findings become tags (group 'scout'), which survive later rule and model passes.

LEADSCOUT_CMD overrides the command (default: hermesme -p leadscout -t web -z). Settings: scout (on/off, default on;
runs only while AI scoring is on) and scout_workers (agents at once, default 3).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import traceback
from pathlib import Path

import db
import qualify

SCOUT_MIN = 60          # bulk AI fit needed before an agent looks
TIMEOUT = 240           # seconds per lead
CMD = os.environ.get('LEADSCOUT_CMD', f"{Path.home() / '.local/bin/hermesme'} -p leadscout -t web -z")

SCHEMA = """CREATE TABLE IF NOT EXISTS deep_research(person_id INTEGER PRIMARY KEY, verdict TEXT, reachable INT,
  summary TEXT, tags TEXT, sources TEXT, raw TEXT, at TEXT NOT NULL)"""

VERDICT_TAG = {'strong': 'Scout: Strong', 'possible': 'Scout: Possible', 'no': 'Scout: No'}


def ensure(conn):
    conn.execute(SCHEMA)


def available():
    return not os.environ.get('FL_NO_ORSLOT') and Path(shlex.split(CMD)[0]).exists()


def prompt(p):
    bits = [f"@{p['handle']}"]
    for label, key in (('name', 'name'), ('website', 'website'), ('bio', 'bio')):
        if p.get(key):
            value = re.sub(r'\s+', ' ', str(p[key]))[:300]
            bits.append(f'{label}: {value}')
    if isinstance(p.get('followers'), int):
        bits.append(f"followers: {p['followers']:,}")
    return 'Vet ' + ' | '.join(bits)


def run(p):
    """Network only: one agent run. -> parsed dict or None."""
    try:
        out = subprocess.run(shlex.split(CMD) + [prompt(p)], capture_output=True, text=True, timeout=TIMEOUT,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    data = qualify.parse_json(out.stdout)
    if not isinstance(data, dict) or data.get('verdict') not in VERDICT_TAG:
        return None
    return data


def _clean_tags(tags):
    out = []
    for t in tags if isinstance(tags, list) else []:
        t = re.sub(r'\s+', ' ', str(t)).strip()[:30]
        if t and t.lower() not in {x.lower() for x in out}:
            out.append(t)
    return out[:8]


def tags_of(row):
    """The tags a scout result puts on a person (verdict first)."""
    extra = json.loads(row['tags'] or '[]')
    if not row['reachable']:
        extra = ['Not reachable'] + extra
    return [VERDICT_TAG[row['verdict']]] + extra


def retag(conn, pid):
    """Put the scout tags back after a rule or model pass rewrote the automatic tags."""
    row = conn.execute('SELECT * FROM deep_research WHERE person_id=?', (pid,)).fetchone() if _has_table(conn) else None
    if row:
        conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,'scout','auto')", [(pid, t) for t in tags_of(row)])


def result(conn, pid):
    """The agent's verdict for the detail panel, or None."""
    row = conn.execute('SELECT * FROM deep_research WHERE person_id=?', (pid,)).fetchone() if _has_table(conn) else None
    if not row:
        return None
    return {'verdict': row['verdict'], 'reachable': bool(row['reachable']), 'summary': row['summary'],
            'tags': json.loads(row['tags'] or '[]'), 'sources': json.loads(row['sources'] or '[]'), 'at': row['at']}


def _has_table(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deep_research'").fetchone())


def apply(conn, p, data):
    """Store the result, give it the final say on fit, and tag the person."""
    reachable = data.get('reachable') is not False
    summary = re.sub(r'\s+', ' ', str(data.get('summary') or '')).strip()[:400]
    sources = [s for s in data.get('sources') or [] if isinstance(s, str) and s.startswith('http')][:8]
    conn.execute('INSERT OR REPLACE INTO deep_research VALUES(?,?,?,?,?,?,?,?)',
                 (p['id'], data['verdict'], int(reachable), summary, json.dumps(_clean_tags(data.get('tags'))),
                  json.dumps(sources), json.dumps(data, ensure_ascii=False)[:8000], db.now()))
    v = conn.execute('SELECT content_fit FROM verdicts WHERE person_id=?', (p['id'],)).fetchone()
    fit = v['content_fit'] if v and v['content_fit'] is not None else 50
    fit = 10 if data['verdict'] == 'no' or not reachable else max(fit, 85) if data['verdict'] == 'strong' else min(max(fit, 50), 75)
    import server   # the server owns network context and blending
    net = server.network_context(conn, [p['id']]).get(p['id'])
    score = qualify.blend(fit, net)
    conn.execute("UPDATE verdicts SET model='leadscout', content_fit=?, score=?, tier=?, reason=? WHERE person_id=?",
                 (fit, score, qualify._tier(score, True), summary or None, p['id']))
    conn.execute("DELETE FROM tags WHERE person_id=? AND grp='scout'", (p['id'],))
    retag(conn, p['id'])


def candidates(conn, limit, exclude):
    held = list(exclude)[:900]
    return [dict(r) for r in conn.execute(
        "SELECT p.* FROM people p JOIN verdicts v ON v.person_id=p.id "
        "WHERE v.model NOT IN ('rules','error','leadscout') AND coalesce(v.content_fit,0)>=? "
        "AND NOT EXISTS (SELECT 1 FROM deep_research d WHERE d.person_id=p.id) "
        f"AND p.id NOT IN ({','.join('?' * len(held))}) ORDER BY v.score DESC, p.id LIMIT ?",
        (SCOUT_MIN, *held, limit))]


class ScoutPool:
    """At most scout_workers agents at once, each on its own DB connection; failures back off for an hour."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.inflight, self.failed = set(), {}

    def step(self, conn):
        if not db.get_setting(conn, 'qualify') or db.get_setting(conn, 'scout') is False or not available():
            return False
        ensure(conn)
        workers = max(1, min(8, int(db.get_setting(conn, 'scout_workers') or 3)))
        now = __import__('time').time()
        with self.lock:
            self.failed = {k: t for k, t in self.failed.items() if t > now}
            free = workers - len(self.inflight)
            exclude = self.inflight | set(self.failed)
        if free <= 0:
            return False
        rows = candidates(conn, free, exclude)
        conn.commit()
        with self.lock:
            self.inflight.update(r['id'] for r in rows)
        for r in rows:
            threading.Thread(target=self._work, args=(r,), daemon=True).start()
        return bool(rows)

    def _work(self, p):
        try:
            data = run(p)
            conn = db.connect(self.db_path)
            try:
                if data is None:
                    raise ValueError('no usable answer')
                apply(conn, p, data)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()
            with self.lock:
                self.failed[p['id']] = __import__('time').time() + 3600
        finally:
            with self.lock:
                self.inflight.discard(p['id'])
