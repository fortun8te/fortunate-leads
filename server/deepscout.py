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
import websearch

SCOUT_MIN = 60          # bulk AI fit needed before an agent looks
TIMEOUT = 240           # seconds per lead
CMD = os.environ.get('LEADSCOUT_CMD', f"{Path.home() / '.local/bin/hermesme'} -p leadscout -t web -z")

# Which model the agent runs on (setting scout_model); the profile's fallback chain still applies after it.
MODELS = {
    'space-bunny': {'label': 'Space Bunny (OpenRouter stealth, free)', 'args': ['--provider', 'orslot', '-m', 'stealth/space-bunny-alpha']},
    'grok': {'label': 'Grok 4.7 (your SuperGrok plan)', 'args': ['--provider', 'xai-oauth', '-m', 'grok-4.7', '--reasoning', 'medium']},
}
STATE_DB = Path.home() / '.hermes-me/profiles/leadscout/state.db'

SCHEMA = """CREATE TABLE IF NOT EXISTS deep_research(person_id INTEGER PRIMARY KEY, verdict TEXT, reachable INT,
  summary TEXT, tags TEXT, sources TEXT, raw TEXT, at TEXT NOT NULL)"""

VERDICT_TAG = {'strong': 'Scout: Strong', 'possible': 'Scout: Possible', 'no': 'Scout: No'}


def ensure(conn):
    conn.execute(SCHEMA)


def available():
    return not os.environ.get('FL_NO_ORSLOT') and Path(shlex.split(CMD)[0]).exists()


def prompt(p, research=None):
    """The lead plus the research already gathered (SearXNG results, their website), so the agent verifies and fills
    gaps instead of repeating the same searches: fewer tool turns per lead."""
    head = _vet_line(p)
    lines = websearch.lines(research) if research else []
    if not lines:
        return head
    return (head + '\n\nALREADY GATHERED (verify, do not repeat these searches; at most 2 new searches, only for gaps such as '
            'founder name, shop, market or size):\n' + '\n'.join(lines)[:3500])


def _vet_line(p):
    bits = [f"@{p['handle']}"]
    for label, key in (('name', 'name'), ('website', 'website'), ('bio', 'bio')):
        if p.get(key):
            value = re.sub(r'\s+', ' ', str(p[key]))[:300]
            bits.append(f'{label}: {value}')
    if isinstance(p.get('followers'), int):
        bits.append(f"followers: {p['followers']:,}")
    return 'Vet ' + ' | '.join(bits)


def run(p, model='space-bunny', research=None):
    """Network only: one agent run. -> parsed dict or None."""
    base = shlex.split(CMD)
    args = base[:-1] + MODELS.get(model, MODELS['space-bunny'])['args'] + base[-1:]   # model flags before -z
    try:
        out = subprocess.run(args + [prompt(p, research)], capture_output=True, text=True, timeout=TIMEOUT,
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
    v = conn.execute('SELECT content_fit, role FROM verdicts WHERE person_id=?', (p['id'],)).fetchone()
    fit = v['content_fit'] if v and v['content_fit'] is not None else 50
    fit = 10 if data['verdict'] == 'no' or not reachable else max(fit, 85) if data['verdict'] == 'strong' else min(max(fit, 50), 75)
    import server   # the server owns network context and blending
    net = server.network_context(conn, [p['id']]).get(p['id'])
    score = qualify.blend(fit, net, 'buyer' if data['verdict'] == 'strong' else v['role'] if v else None)
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


def usage(days=7):
    """Runs and tokens the agent used per model over the last `days`, from Hermes' own session records."""
    if not STATE_DB.exists():
        return []
    import sqlite3
    import time
    try:
        con = sqlite3.connect(f'file:{STATE_DB}?mode=ro', uri=True, timeout=2)
        rows = con.execute('SELECT model, billing_provider, count(*), coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0), '
                           'max(started_at) FROM sessions WHERE started_at>=? GROUP BY 1,2 ORDER BY 3 DESC',
                           (time.time() - days * 86400,)).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    return [{'model': m, 'provider': prov, 'runs': n, 'tokens_in': ti, 'tokens_out': to, 'last': last} for m, prov, n, ti, to, last in rows]


def status(conn):
    ensure(conn)
    return {'on': db.get_setting(conn, 'scout') is not False, 'available': available(),
            'model': db.get_setting(conn, 'scout_model') or 'space-bunny', 'workers': db.get_setting(conn, 'scout_workers') or 3,
            'models': [{'id': k, 'label': v['label']} for k, v in MODELS.items()],
            'done_today': conn.execute("SELECT count(*) FROM deep_research WHERE at>=date('now')").fetchone()[0],
            'done': conn.execute('SELECT count(*) FROM deep_research').fetchone()[0],
            'waiting': len(candidates(conn, 100000, set())), 'usage': usage()}


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
            conn0 = db.connect(self.db_path)
            try:
                model = db.get_setting(conn0, 'scout_model') or 'space-bunny'
            finally:
                conn0.close()
            research = None
            if websearch.available():   # Special owns fresh lookups; Bulk only reads this cache
                conn1 = db.connect(self.db_path)
                try:
                    websearch.ensure(conn1)
                    research = websearch.cached(conn1, p)
                    conn1.commit()
                finally:
                    conn1.close()
                if research is None:
                    research = websearch.lookup(p)
                    conn1 = db.connect(self.db_path)
                    try:
                        websearch.store(conn1, p, research)
                        conn1.commit()
                    finally:
                        conn1.close()
            data = run(p, model, research)
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
