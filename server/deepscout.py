"""Leadscout: the last qualification step, a Hermes agent that vets the best leads on the open web.

The bulk AI step (free models + SearXNG) scores everyone with a bio. Only people it rates as a likely fit
(content_fit >= SCOUT_MIN) come here, so the agent's time goes to leads that might be real. The agent
(Hermes profile `leadscout`, see ~/.hermes-me/profiles/leadscout/SOUL.md) searches, reads their site and
answers with one JSON object. Positive verdicts affect fit only after a cited
quote is checked against the saved profile or a related public page.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import db
import qualify

SCOUT_MIN = 60          # bulk AI fit needed before an agent looks
TIMEOUT = 240           # seconds per lead
CMD = os.environ.get('LEADSCOUT_CMD', f"{Path.home() / '.local/bin/hermesme'} -p leadscout -t web -z")

# Which model the agent runs on (setting scout_model); the profile's fallback chain still applies after it.
MODELS = {
    'space-bunny': {'label': 'Space Bunny (OpenRouter stealth, free)', 'args': ['--provider', 'orslot', '-m', 'stealth/space-bunny-alpha']},
    'grok': {'label': 'Grok 4.6 (your SuperGrok plan)', 'args': ['--provider', 'xai-oauth', '-m', 'grok-4.6']},
}
STATE_DB = Path.home() / '.hermes-me/profiles/leadscout/state.db'

PROFILE_FIELDS = ('ig_id', 'handle', 'name', 'bio', 'website', 'followers', 'is_private')
SNAPSHOT = {field: field + '_at_check' for field in PROFILE_FIELDS}
FOLLOWER_BANDS = (1000, 10000, 50000, 300000, 1000000)


def _value(p, field):
    value = p.get(field) if isinstance(p, dict) else p[field]
    if field == 'followers' and isinstance(value, int):
        return next((cap for cap in FOLLOWER_BANDS if value < cap), FOLLOWER_BANDS[-1] + 1)
    return value


def _sql_value(alias, field):
    if field != 'followers':
        return f'{alias}.{field}'
    checks = ' '.join(f'WHEN {alias}.followers < {cap} THEN {cap}' for cap in FOLLOWER_BANDS)
    return f'(CASE WHEN {alias}.followers IS NULL THEN NULL {checks} ELSE {FOLLOWER_BANDS[-1] + 1} END)'


MATCH = ' AND '.join(f'd.{column} IS {_sql_value("p", field)}' for field, column in SNAPSHOT.items())
CHANGED = ' OR '.join(f'NOT (d.{column} IS {_sql_value("NEW", field)})' for field, column in SNAPSHOT.items())
SCHEMA = """CREATE TABLE IF NOT EXISTS deep_research(person_id INTEGER PRIMARY KEY, verdict TEXT, reachable INT,
  summary TEXT, tags TEXT, sources TEXT, raw TEXT, at TEXT NOT NULL, ig_id_at_check TEXT,
  handle_at_check TEXT, name_at_check TEXT, bio_at_check TEXT, website_at_check TEXT,
  followers_at_check INTEGER, is_private_at_check INTEGER, verified INTEGER NOT NULL DEFAULT 0,
  verification_reason TEXT, retry_after TEXT)"""
RUNS_SCHEMA = """CREATE TABLE IF NOT EXISTS deep_research_runs(id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL,
  at TEXT NOT NULL, model TEXT, outcome TEXT NOT NULL, raw TEXT, verification_reason TEXT)"""
RETRY_HOURS = 1
MAX_EVIDENCE = 4
POSITIVE_TERMS = re.compile(r'\b(founder|founded|owner|ceo|brand|products?|shop|store|selling|sells|'
                            r'skincare|cosmetics|clothing|apparel|supplements?|candles|jewelry|retailer|'
                            r'e-?commerce|direct.to.consumer)\b', re.I)
SHARED_HOSTS = {'instagram.com', 'linktr.ee', 'beacons.ai', 'stan.store', 'taplink.cc',
                'etsy.com', 'amazon.com', 'tiktok.com', 'facebook.com'}

VERDICT_TAG = {'strong': 'Scout: Strong', 'possible': 'Scout: Possible', 'no': 'Scout: No'}


def ensure(conn):
    conn.execute(SCHEMA)
    conn.execute(RUNS_SCHEMA)
    columns = {r[1] for r in conn.execute('PRAGMA table_info(deep_research)')}
    missing = [column for column in SNAPSHOT.values() if column not in columns]
    for column in missing:
        kind = 'INTEGER' if column in ('followers_at_check', 'is_private_at_check') else 'TEXT'
        conn.execute(f'ALTER TABLE deep_research ADD COLUMN {column} {kind}')
    added_verification = 'verified' not in columns
    for column, kind in (('verified', 'INTEGER NOT NULL DEFAULT 0'),
                         ('verification_reason', 'TEXT'), ('retry_after', 'TEXT')):
        if column not in columns:
            conn.execute(f'ALTER TABLE deep_research ADD COLUMN {column} {kind}')
    if missing or added_verification:
        # Older rows have no proof of which profile Hermes saw. Retain the research
        # record, but stop showing its verdict/tags until this profile is checked again.
        # Legacy Leadscout scores may already contain an unsupported promotion.
        # Requeue normal qualification instead of treating them as bulk scores.
        conn.execute("UPDATE verdicts SET model='rules',score=NULL,tier='unread',role=NULL,reason=NULL,"
                     "content_fit=NULL,input_hash=NULL,prompt=NULL,evidence=NULL,updated_at='' "
                     "WHERE model='leadscout' AND person_id IN (SELECT person_id FROM deep_research)")
        conn.execute("DELETE FROM tags WHERE source='auto' AND grp='scout' "
                     "AND person_id IN (SELECT person_id FROM deep_research)")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS leadscout_profile_changed
        AFTER UPDATE OF {','.join(PROFILE_FIELDS)} ON people
        WHEN EXISTS (SELECT 1 FROM deep_research d WHERE d.person_id=NEW.id AND ({CHANGED}))
        BEGIN
          UPDATE verdicts SET model='rules',score=NULL,tier='unread',role=NULL,reason=NULL,
            content_fit=NULL,input_hash=NULL,prompt=NULL,evidence=NULL,updated_at=''
            WHERE person_id=NEW.id AND model='leadscout';
          DELETE FROM tags WHERE person_id=NEW.id AND grp='scout' AND source='auto';
        END""")


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
    return ('Vet ' + ' | '.join(bits) + '\nReturn one JSON object with verdict (strong/possible/no), '
            'reachable (boolean), summary (string), tags (string array), sources (URL array), and evidence '
            '(array of {source, quote}). For strong/possible, cite a 10-240 character exact quote from '
            'profile.bio, profile.name, or a public page on the profile website or Instagram handle URL. '
            'Do not cite search snippets or unrelated pages as proof. Use evidence: [] for a no verdict.')


def run(p, model='space-bunny'):
    """Network only: one agent run. -> parsed dict or None."""
    base = shlex.split(CMD)
    args = base[:-1] + MODELS.get(model, MODELS['space-bunny'])['args'] + base[-1:]   # model flags before -z
    try:
        out = subprocess.run(args + [prompt(p)], capture_output=True, text=True, timeout=TIMEOUT,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    data = qualify.parse_json(out.stdout)
    return data if isinstance(data, dict) else None


def _host(url):
    try:
        parsed = urlsplit(url if '://' in url else 'https://' + url)
        host = (parsed.hostname or '').lower().removeprefix('www.')
        return host, parsed
    except ValueError:
        return '', None


def _related(url, p):
    host, parsed = _host(url)
    if not parsed or parsed.scheme not in ('http', 'https') or not host:
        return False
    site_host, site = _host(p.get('website') or '')
    if site_host and host == site_host:
        if host in SHARED_HOSTS:
            path = (site.path or '').lower().rstrip('/')
            cited = parsed.path.lower().rstrip('/')
            return bool(path and (cited == path or cited.startswith(path + '/')))
        return True
    handle = (p.get('handle') or '').lower()
    return host == 'instagram.com' and parsed.path.lower().rstrip('/') == '/' + handle


def _schema_error(data):
    if not isinstance(data, dict) or not isinstance(data.get('verdict'), str) or data['verdict'] not in VERDICT_TAG:
        return 'invalid verdict'
    if not isinstance(data.get('reachable'), bool):
        return 'reachable must be a boolean'
    if not isinstance(data.get('summary'), str) or not data['summary'].strip() or len(data['summary']) > 1000:
        return 'invalid summary'
    if not isinstance(data.get('tags'), list) or len(data['tags']) > 8 or not all(
            isinstance(t, str) and 0 < len(t) <= 60 for t in data['tags']):
        return 'invalid tags'
    if not isinstance(data.get('sources'), list) or len(data['sources']) > 8 or not all(
            isinstance(s, str) and len(s) <= 2000 for s in data['sources']):
        return 'invalid sources'
    evidence = data.get('evidence')
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE or not all(
            isinstance(e, dict) and set(e) == {'source', 'quote'}
            and isinstance(e['source'], str) and isinstance(e['quote'], str)
            and 10 <= len(e['quote'].strip()) <= 240 and len(e['source']) <= 2000 for e in evidence):
        return 'invalid evidence'
    if data['verdict'] in ('strong', 'possible') and (not data['reachable'] or not evidence):
        return 'positive verdict needs reachable quoted evidence'
    return None


def verify(p, data):
    """Check typed evidence without treating the agent's URL or prose as proof."""
    error = _schema_error(data)
    if error:
        return False, error
    if data['verdict'] == 'no':
        return True, None
    pages = {}
    relevant = False
    for item in data['evidence']:
        source, quote = item['source'], re.sub(r'\s+', ' ', item['quote']).strip()
        if source in ('profile.bio', 'profile.name'):
            corpus = re.sub(r'\s+', ' ', str(p.get(source.split('.')[1]) or '')).strip()
        elif _related(source, p):
            if source not in pages:
                if len(pages) >= 2:
                    return False, 'too many cited pages'
                try:
                    import qual_api
                    final_url, doc = qual_api.fetch(source)
                    if not _related(final_url, p):
                        return False, 'cited page redirected away from profile website'
                    title, description, body = qual_api.page_text(doc)
                    pages[source] = ' '.join((title, description, body))
                except (ValueError, OSError):
                    return False, 'cited page could not be verified'
            corpus = re.sub(r'\s+', ' ', pages[source]).strip()
        else:
            return False, 'citation is not tied to the profile'
        if quote not in corpus:
            return False, 'quote not found in cited source'
        relevant = relevant or bool(POSITIVE_TERMS.search(quote))
    if not relevant:
        return False, 'quote does not support a positive lead claim'
    return True, None


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
    row = _row(conn, pid)
    if row and row['verified'] and _fresh(conn, row):
        conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,'scout','auto')", [(pid, t) for t in tags_of(row)])
    elif row:
        conn.execute("DELETE FROM tags WHERE person_id=? AND grp='scout' AND source='auto'", (pid,))


def result(conn, pid):
    """Current verdict or visibly unverified historical read for the detail panel."""
    row = _row(conn, pid)
    if not row:
        return None
    return {'stale': not _fresh(conn, row), 'verified': bool(row['verified']),
            'verification_reason': row['verification_reason'], 'verdict': row['verdict'],
            'reachable': bool(row['reachable']), 'summary': row['summary'],
            'tags': json.loads(row['tags'] or '[]'), 'sources': json.loads(row['sources'] or '[]'), 'at': row['at']}


def _has_table(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deep_research'").fetchone())


def _row(conn, pid):
    return conn.execute('SELECT * FROM deep_research WHERE person_id=?', (pid,)).fetchone() if _has_table(conn) else None


def _fresh(conn, row, p=None):
    if p is None:
        p = conn.execute('SELECT * FROM people WHERE id=?', (row['person_id'],)).fetchone()
    return bool(p and all(SNAPSHOT[field] in row.keys() and row[SNAPSHOT[field]] == _value(p, field)
                          for field in PROFILE_FIELDS))


def fresh(conn, p):
    row = _row(conn, p['id'])
    return bool(row and row['verified'] and _fresh(conn, row, p))


def apply(conn, p, data, verification=None):
    """Keep every candidate; only verified answers can change fit or tags."""
    verified, reason = verification if verification is not None else verify(p, data)
    reachable = data.get('reachable') is True
    summary = re.sub(r'\s+', ' ', str(data.get('summary') or '')).strip()[:400]
    sources = list(dict.fromkeys(s for s in data.get('sources') or []
                                 if isinstance(s, str) and _related(s, p)))[:8]
    retry_after = (datetime.now(timezone.utc) + timedelta(hours=RETRY_HOURS)).isoformat() if not verified else None
    conn.execute('INSERT OR REPLACE INTO deep_research '
                 '(person_id,verdict,reachable,summary,tags,sources,raw,at,'
                 'ig_id_at_check,handle_at_check,name_at_check,bio_at_check,website_at_check,'
                 'followers_at_check,is_private_at_check,verified,verification_reason,retry_after) '
                 'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (p['id'], data.get('verdict'), int(reachable), summary, json.dumps(_clean_tags(data.get('tags'))),
                  json.dumps(sources), json.dumps(data, ensure_ascii=False)[:8000], db.now(),
                  *(_value(p, field) for field in PROFILE_FIELDS), int(verified), reason, retry_after))
    if verified:
        reapply(conn, p)
    conn.execute("DELETE FROM tags WHERE person_id=? AND grp='scout'", (p['id'],))
    if verified:
        retag(conn, p['id'])
    return verified


def reapply(conn, p, net=None):
    """Restore the saved scout's final verdict after a later rule pass on this person."""
    row = _row(conn, p['id'])
    if not row or not row['verified'] or not _fresh(conn, row, p):
        return False
    v = conn.execute('SELECT content_fit FROM verdicts WHERE person_id=?', (p['id'],)).fetchone()
    if not v:
        return False
    fit = v['content_fit'] if v and v['content_fit'] is not None else 50
    fit = 10 if row['verdict'] == 'no' or not row['reachable'] else max(fit, 85) if row['verdict'] == 'strong' else min(max(fit, 50), 75)
    if net is None:
        import server   # the server owns network context and blending
        net = server.network_context(conn, [p['id']]).get(p['id'])
    score = qualify.blend(fit, net)
    conn.execute("UPDATE verdicts SET model='leadscout', content_fit=?, score=?, tier=?, reason=?, "
                 "role=NULL, prompt=NULL, evidence=NULL WHERE person_id=?",
                 (fit, score, qualify._tier(score, bool((p.get('bio') or '').strip())), row['summary'] or None, p['id']))
    return True


def candidates(conn, limit, exclude):
    held = list(exclude)[:900]
    return [dict(r) for r in conn.execute(
        "SELECT p.* FROM people p JOIN verdicts v ON v.person_id=p.id "
        "WHERE v.model NOT IN ('rules','error','leadscout') AND coalesce(v.content_fit,0)>=? "
        f"AND NOT EXISTS (SELECT 1 FROM deep_research d WHERE d.person_id=p.id AND {MATCH} "
        "AND (d.verified=1 OR d.retry_after>?)) "
        f"AND p.id NOT IN ({','.join('?' * len(held))}) ORDER BY v.score DESC, p.id LIMIT ?",
        (SCOUT_MIN, db.now(), *held, limit))]


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
    waiting = conn.execute(
        "SELECT count(*) FROM people p JOIN verdicts v ON v.person_id=p.id "
        "WHERE v.model NOT IN ('rules','error','leadscout') AND coalesce(v.content_fit,0)>=? "
        f"AND NOT EXISTS (SELECT 1 FROM deep_research d WHERE d.person_id=p.id AND {MATCH} "
        "AND (d.verified=1 OR d.retry_after>?))",
        (SCOUT_MIN, db.now())).fetchone()[0]
    return {'on': db.get_setting(conn, 'scout') is not False, 'available': available(),
            'model': db.get_setting(conn, 'scout_model') or 'space-bunny', 'workers': db.get_setting(conn, 'scout_workers') or 3,
            'models': [{'id': k, 'label': v['label']} for k, v in MODELS.items()],
            'done_today': conn.execute("SELECT count(*) FROM deep_research WHERE at>=date('now')").fetchone()[0],
            'done': conn.execute('SELECT count(*) FROM deep_research').fetchone()[0],
            'waiting': waiting, 'usage': usage()}


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
            data = run(p, model)
            verification = verify(p, data) if data is not None else (False, 'no usable answer')
            conn = db.connect(self.db_path)
            try:
                ensure(conn)
                # The agent can spend minutes researching. Check the exact profile it saw
                # under the same short write transaction that saves its answer.
                conn.execute('BEGIN IMMEDIATE')
                current = conn.execute('SELECT * FROM people WHERE id=?', (p['id'],)).fetchone()
                stale = not current or any(_value(current, field) != _value(p, field) for field in PROFILE_FIELDS)
                outcome = 'stale' if stale else 'failed' if data is None else 'verified' if verification[0] else 'unverified'
                conn.execute('INSERT INTO deep_research_runs(person_id,at,model,outcome,raw,verification_reason) '
                             'VALUES(?,?,?,?,?,?)',
                             (p['id'], db.now(), model, outcome,
                              json.dumps(data, ensure_ascii=False)[:8000] if data is not None else None,
                              verification[1]))
                if stale:
                    conn.commit()
                    return
                if data is not None:
                    apply(conn, p, data, verification)
                conn.commit()
                if not verification[0]:
                    with self.lock:
                        self.failed[p['id']] = __import__('time').time() + RETRY_HOURS * 3600
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()
            with self.lock:
                self.failed[p['id']] = __import__('time').time() + 3600
        finally:
            with self.lock:
                self.inflight.discard(p['id'])
