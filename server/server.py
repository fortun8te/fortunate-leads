import argparse
import json
import mimetypes
import re
import ssl
import sys
import threading
import traceback
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import qualify  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'web'
EXT_ORIGIN = 'chrome-extension://fgdbghllamedgihmdcolaggnbhnakjnf'
CFG = {'db': str(ROOT / 'data' / 'leads.sqlite'), 'port': 8766}
STATUSES = ('good', 'maybe', 'no', 'contacted', 'client', 'known')
PIC_HOSTS = ('.cdninstagram.com', '.fbcdn.net')
PIC_MAX = 2 * 1024 * 1024
READ_PRIORITY = 10000
SSL = ssl.create_default_context(cafile='/etc/ssl/cert.pem' if Path('/etc/ssl/cert.pem').is_file() else None)
BUDGET_MAX = {'list': 600, 'profile': 300}


class Bad(Exception):
    pass


def utc(s):
    d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def iso(d):
    return d.isoformat(timespec='microseconds')


def pfp_dir():
    return Path(CFG['db']).resolve().parent / 'pfp'


def me_handle(conn):
    row = conn.execute('SELECT handle FROM seeds WHERE is_me=1').fetchone()
    return row[0] if row else None


def edges_of(conn, pid):
    return [dict(r) for r in conn.execute('SELECT seed, direction FROM edges WHERE person_id=? ORDER BY seed, direction', (pid,))]


# ---------- extension endpoints ----------

def ext_state(conn):
    return {'paused': bool(db.get_setting(conn, 'paused')), 'budget': db.get_setting(conn, 'budget')}


def ext_next(conn, q, b):
    st, ts = ext_state(conn), db.now()
    cooldown = db.get_setting(conn, 'cooldown')
    kinds = [k for k in csv(q, 'kinds') if k in ('list', 'profile')] or ['list', 'profile']
    if st['paused'] or (cooldown and cooldown > ts):
        return dict(st, job=None, cooldown_until=cooldown if cooldown and cooldown > ts else None)
    conn.execute('BEGIN IMMEDIATE')
    job = conn.execute(f"SELECT * FROM jobs WHERE (state='queued' OR (state='leased' AND leased_until<?)) "
                       f"AND kind IN ({','.join('?' * len(kinds))}) ORDER BY kind='list' DESC, priority DESC, id LIMIT 1",
                       (ts, *kinds)).fetchone()
    if not job:
        conn.commit()
        return dict(st, job=None)
    conn.execute("UPDATE jobs SET state='leased', leased_until=?, attempts=attempts+1 WHERE id=?",
                 (iso(datetime.now(timezone.utc) + timedelta(minutes=10)), job['id']))
    if job['kind'] == 'list':
        conn.execute("UPDATE lists SET state='running', updated_at=? WHERE seed=? AND direction=?",
                     (ts, job['seed'], job['direction']))
        seed = conn.execute('SELECT ig_id FROM seeds WHERE handle=?', (job['seed'],)).fetchone()
        lst = conn.execute('SELECT cursor FROM lists WHERE seed=? AND direction=?', (job['seed'], job['direction'])).fetchone()
        out = {'id': job['id'], 'kind': 'list', 'seed': job['seed'], 'ig_id': seed and seed['ig_id'],
               'direction': job['direction'], 'cursor': lst and lst['cursor']}
    else:
        p = conn.execute('SELECT ig_id FROM people WHERE handle=?', (job['handle'],)).fetchone()
        out = {'id': job['id'], 'kind': 'profile', 'handle': job['handle'], 'ig_id': p and p['ig_id']}
    conn.commit()
    return dict(st, job=out)


def ext_list_page(conn, q, b):
    job = conn.execute('SELECT * FROM jobs WHERE id=?', (b.get('job_id'),)).fetchone()
    seed = db.norm_handle(b.get('seed') or (job and job['seed']))
    direction = b.get('direction') or (job and job['direction'])
    if not seed or direction not in ('followers', 'following'):
        raise Bad('seed and direction required')
    ts = db.now()
    fresh = not job or conn.execute('INSERT OR IGNORE INTO pages VALUES(?,?)', (job['id'], b.get('next_cursor') or '')).rowcount
    conn.execute('INSERT OR IGNORE INTO seeds(handle, added_at) VALUES(?,?)', (seed, ts))
    if b.get('ig_id'):
        conn.execute('UPDATE seeds SET ig_id=? WHERE handle=?', (str(b['ig_id']), seed))
    for u in b.get('users') or []:
        if u.get('handle'):
            pid = db.upsert_person(conn, {k: u.get(k) for k in ('ig_id', 'handle', 'name', 'pic_url', 'is_private', 'is_verified')}, ts)
            db.add_edge(conn, seed, pid, direction, ts)
    received = conn.execute('SELECT count(*) FROM edges WHERE seed=? AND direction=?', (seed, direction)).fetchone()[0]
    if not fresh:  # outbox retry of a page we already have: never move the cursor back
        conn.commit()
        return {'received': received, 'duplicate': True}
    done = bool(b.get('done'))
    conn.execute('INSERT INTO lists(seed, direction, state, cursor, received, total, updated_at) VALUES(?,?,?,?,?,?,?) '
                 'ON CONFLICT DO UPDATE SET state=excluded.state, cursor=excluded.cursor, received=excluded.received, '
                 'total=coalesce(excluded.total, total), error=NULL, updated_at=excluded.updated_at',
                 (seed, direction, 'done' if done else 'running', b.get('next_cursor'), received, b.get('total'), ts))
    conn.execute("UPDATE jobs SET state=?, leased_until=NULL, attempts=0 WHERE kind='list' AND seed=? AND direction=? "
                 "AND state IN ('queued','leased')", ('done' if done else 'queued', seed, direction))
    conn.commit()
    return {'received': received}


def ext_profile(conn, q, b):
    p = dict(b.get('profile') or {})
    if not p.get('handle'):
        raise Bad('profile.handle required')
    ts = db.now()
    p['bio'] = p.get('bio') or ''
    p['bio_at'] = ts
    pid = db.upsert_person(conn, p, ts)
    handle = db.norm_handle(p['handle'])
    if p.get('ig_id'):
        conn.execute('UPDATE seeds SET ig_id=? WHERE handle=?', (str(p['ig_id']), handle))
    conn.execute("UPDATE jobs SET state='done', leased_until=NULL WHERE (id=? OR (kind='profile' AND handle=? "
                 "AND state IN ('queued','leased')))", (b.get('job_id'), handle))
    conn.commit()
    return {'id': pid}


def ext_error(conn, q, b):
    code, ts = b.get('code'), db.now()
    if code in ('challenge', 'login'):  # needs Michael; the UI resume clears it
        db.set_setting(conn, 'paused', True)
    if code in ('rate_limit', 'soft_block') or b.get('retry_at'):
        until = utc(b['retry_at']) if b.get('retry_at') else datetime.now(timezone.utc) + timedelta(minutes=15)
        db.set_setting(conn, 'cooldown', iso(until))
    db.set_setting(conn, 'last_error', {'code': code, 'message': b.get('message'), 'at': ts})
    job = conn.execute('SELECT * FROM jobs WHERE id=?', (b.get('job_id'),)).fetchone()
    if job:
        final = code in ('private', 'not_found') or (code == 'other' and job['attempts'] >= 5)
        conn.execute('UPDATE jobs SET state=?, leased_until=NULL WHERE id=?',
                     ('done' if code in ('private', 'not_found') else 'error' if final else 'queued', job['id']))
        if job['kind'] == 'list':
            state = 'private' if code == 'private' else 'error' if final else 'queued'
            conn.execute('UPDATE lists SET state=?, error=?, updated_at=? WHERE seed=? AND direction=?',
                         (state, b.get('message') or code, ts, job['seed'], job['direction']))
        elif code == 'private':
            conn.execute('UPDATE people SET is_private=1, updated_at=? WHERE handle=?', (ts, job['handle']))
    conn.commit()
    return {}


def ext_heartbeat(conn, q, b):
    db.set_setting(conn, 'ext', dict(b, last_seen=db.now()))
    conn.commit()
    return ext_state(conn)


# ---------- UI endpoints ----------

LEAD_SQL = ('SELECT p.*, v.tier, v.score, v.role, v.reason, m.status, m.note FROM people p '
            'LEFT JOIN verdicts v ON v.person_id=p.id LEFT JOIN marks m ON m.person_id=p.id')


def lead_rows(conn, rows):
    ids = [r['id'] for r in rows]
    if not ids:
        return []
    marks = ','.join('?' * len(ids))
    tags, via = {}, {}
    for t in conn.execute(f'SELECT * FROM tags WHERE person_id IN ({marks}) ORDER BY grp, tag', ids):
        tags.setdefault(t['person_id'], []).append({'tag': t['tag'], 'grp': t['grp'], 'source': t['source']})
    for e in conn.execute(f'SELECT DISTINCT person_id, seed FROM edges WHERE person_id IN ({marks}) ORDER BY seed', ids):
        via.setdefault(e['person_id'], []).append(e['seed'])
    return [{'id': r['id'], 'handle': r['handle'], 'name': r['name'], 'pic': f"/img/{r['id']}" if r['pic_file'] else None,
             'bio': r['bio'], 'website': r['website'], 'followers': r['followers'], 'following': r['following'],
             'tier': r['tier'] or 'unread', 'score': r['score'], 'role': r['role'], 'reason': r['reason'],
             'tags': tags.get(r['id'], []), 'via': via.get(r['id'], []), 'status': r['status']} for r in rows]


def csv(q, key):
    return [x for x in (q.get(key, [''])[0]).split(',') if x.strip()]


def api_leads(conn, q, b):
    where, args = ["p.handle NOT IN (SELECT handle FROM seeds WHERE is_me=1)"], []
    tiers = csv(q, 'tier')
    if tiers:
        where.append(f"coalesce(v.tier,'unread') IN ({','.join('?' * len(tiers))})")
        args += tiers
    for t in csv(q, 'tags'):
        where.append('p.id IN (SELECT person_id FROM tags WHERE tag=?)')
        args.append(t)
    status = q.get('status', [''])[0]
    if status:
        where.append('m.status=?')
        args.append(status)
    else:
        where.append("coalesce(m.status,'')!='no'")
    text = q.get('q', [''])[0].strip()
    if text:
        where.append('(p.handle LIKE ? OR p.name LIKE ? OR p.bio LIKE ?)')
        args += [f'%{text}%'] * 3
    order = {'recent': 'p.updated_at DESC', 'followers': 'p.followers IS NULL, p.followers DESC'}.get(
        q.get('sort', ['score'])[0], 'v.score IS NULL, v.score DESC, p.followers DESC')
    offset = max(0, int(q.get('offset', ['0'])[0]))
    limit = min(500, max(1, int(q.get('limit', ['50'])[0])))
    sql_where = ' WHERE ' + ' AND '.join(where)
    total = conn.execute('SELECT count(*) FROM people p LEFT JOIN verdicts v ON v.person_id=p.id '
                         'LEFT JOIN marks m ON m.person_id=p.id' + sql_where, args).fetchone()[0]
    rows = conn.execute(f'{LEAD_SQL}{sql_where} ORDER BY {order}, p.id LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
    return {'total': total, 'rows': lead_rows(conn, rows)}


def api_tags(conn, q, b):
    return [dict(r) for r in conn.execute('SELECT tag, grp, count(*) AS count FROM tags GROUP BY tag, grp ORDER BY count DESC, tag')]


def api_counts(conn, q, b):
    out = dict.fromkeys(('hot', 'warm', 'cold', 'unread', 'good', 'maybe', 'contacted'), 0)
    out.update(conn.execute("SELECT coalesce(v.tier,'unread'), count(*) FROM people p "
                            "LEFT JOIN verdicts v ON v.person_id=p.id GROUP BY 1").fetchall())
    out.update(conn.execute('SELECT status, count(*) FROM marks WHERE status IS NOT NULL GROUP BY 1').fetchall())
    out['total'], out['with_bio'] = conn.execute("SELECT count(*), count(nullif(bio,'')) FROM people").fetchone()
    return out


def person_row(conn, pid):
    row = conn.execute(LEAD_SQL + ' WHERE p.id=?', (pid,)).fetchone()
    if not row:
        raise Bad('not found')
    return row


def api_person(conn, q, b, pid):
    row = person_row(conn, pid)
    v = conn.execute('SELECT * FROM verdicts WHERE person_id=?', (pid,)).fetchone()
    return dict(lead_rows(conn, [row])[0], edges=edges_of(conn, pid), verdict=dict(v) if v else None, note=row['note'])


def api_mark(conn, q, b, pid):
    person_row(conn, pid)
    status, note = b.get('status'), b.get('note')
    if status is not None and status not in STATUSES:
        raise Bad('bad status')
    if status is None and not note:
        conn.execute('DELETE FROM marks WHERE person_id=?', (pid,))
    else:
        conn.execute('INSERT INTO marks VALUES(?,?,?,?) ON CONFLICT DO UPDATE SET status=excluded.status, '
                     'note=coalesce(excluded.note, note), updated_at=excluded.updated_at', (pid, status, note, db.now()))
    conn.commit()
    return {}


def api_tag_edit(conn, q, b, pid):
    person_row(conn, pid)
    for t in b.get('add') or []:
        grp = conn.execute('SELECT grp FROM tags WHERE tag=? LIMIT 1', (t,)).fetchone()
        conn.execute("INSERT OR REPLACE INTO tags VALUES(?,?,?,'manual')", (pid, t, grp[0] if grp else 'signal'))
    for t in b.get('remove') or []:
        conn.execute('DELETE FROM tags WHERE person_id=? AND tag=?', (pid, t))
    conn.execute('UPDATE people SET updated_at=? WHERE id=?', (db.now(), pid))
    conn.commit()
    return {}


def api_read(conn, q, b, pid):
    handle = person_row(conn, pid)['handle']
    if not conn.execute("UPDATE jobs SET priority=? WHERE kind='profile' AND handle=? AND state IN ('queued','leased')",
                        (READ_PRIORITY, handle)).rowcount:
        conn.execute("INSERT INTO jobs(kind, handle, priority, created_at) VALUES('profile',?,?,?)", (handle, READ_PRIORITY, db.now()))
    conn.commit()
    return {}


def data_rev(conn):
    r = conn.execute('SELECT (SELECT max(updated_at) FROM people), (SELECT max(updated_at) FROM verdicts), '
                     '(SELECT count(*) FROM edges), (SELECT count(*) FROM seeds), (SELECT max(updated_at) FROM marks), '
                     '(SELECT count(*) FROM marks)').fetchone()
    return zlib.crc32('|'.join(map(str, r)).encode())


def api_map(conn, q, b):
    limit = min(3000, max(10, int(q.get('limit', ['400'])[0])))
    base = ('SELECT p.id, p.handle, p.pic_file, v.tier, v.score, count(DISTINCT e.seed) AS degree FROM people p '
            'JOIN edges e ON e.person_id=p.id LEFT JOIN verdicts v ON v.person_id=p.id LEFT JOIN marks m ON m.person_id=p.id '
            "WHERE coalesce(m.status,'')!='no' AND p.handle NOT IN (SELECT handle FROM seeds) GROUP BY p.id")
    by_score = 'ORDER BY v.score IS NULL, v.score DESC, degree DESC LIMIT ?'
    if q.get('scope', ['leads'])[0] == 'all':
        people = conn.execute(f'{base} {by_score}', (limit,)).fetchall()
    else:
        multi = conn.execute(f'{base} HAVING degree>=2 ORDER BY degree DESC, v.score DESC LIMIT ?', (limit * 3 // 5,)).fetchall()
        seen = {r['id'] for r in multi}
        people = multi + [r for r in conn.execute(f'{base} {by_score}', (limit,)) if r['id'] not in seen][:limit - len(multi)]
    seeds = conn.execute('SELECT s.handle, (SELECT count(*) FROM edges e WHERE e.seed=s.handle) AS degree, p.id AS pid, '
                         'p.pic_file, v.tier, v.score FROM (SELECT handle FROM seeds UNION SELECT seed FROM edges) s '
                         'LEFT JOIN people p ON p.handle=s.handle LEFT JOIN verdicts v ON v.person_id=p.id').fetchall()
    nodes = [{'id': f"s:{s['handle']}", 'kind': 'seed', 'label': s['handle'], 'tier': s['tier'], 'score': s['score'],
              'pic': f"/img/{s['pid']}" if s['pic_file'] else None, 'degree': s['degree']} for s in seeds]
    nodes += [{'id': f"p:{r['id']}", 'kind': 'lead', 'label': r['handle'], 'tier': r['tier'] or 'unread', 'score': r['score'],
               'pic': f"/img/{r['id']}" if r['pic_file'] else None, 'degree': r['degree']} for r in people]
    node_of = {r['id']: f"p:{r['id']}" for r in people}
    node_of.update((s['pid'], f"s:{s['handle']}") for s in seeds if s['pid'])
    ids = list(node_of)
    links = []
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        links += [{'source': f"s:{e['seed']}", 'target': node_of[e['person_id']], 'direction': e['direction']}
                  for e in conn.execute(f"SELECT * FROM edges WHERE person_id IN ({','.join('?' * len(chunk))})", chunk)]
    return {'nodes': nodes, 'links': links, 'rev': data_rev(conn)}


def api_scraper(conn, q, b):
    ext = db.get_setting(conn, 'ext') or {}
    now = datetime.now(timezone.utc)
    cooldowns = [c for c in (ext.get('cooldown_until'), db.get_setting(conn, 'cooldown')) if c and utc(c) > now]
    return {'ext': {'online': bool(ext.get('last_seen')) and now - utc(ext['last_seen']) < timedelta(seconds=60),
                    'version': ext.get('version'), 'state': ext.get('state'),
                    'cooldown_until': iso(max(map(utc, cooldowns))) if cooldowns else None,
                    'today': ext.get('today'), 'budget': db.get_setting(conn, 'budget'),
                    'last_seen': ext.get('last_seen'),
                    'last_error': ext.get('last_error') or (db.get_setting(conn, 'last_error') or {}).get('message')},
            'paused': bool(db.get_setting(conn, 'paused')),
            'people_today': conn.execute('SELECT count(*) FROM people WHERE first_seen>=?', (iso(now)[:10],)).fetchone()[0],
            'lists': [dict(r) for r in conn.execute('SELECT seed, direction, state, received, total, updated_at, error FROM lists '
                                                    'ORDER BY updated_at DESC')],
            'queue': dict.fromkeys(('list', 'profile'), 0) | dict(conn.execute(
                "SELECT kind, count(*) FROM jobs WHERE state IN ('queued','leased') GROUP BY kind").fetchall())}


def api_seeds(conn, q, b):
    dirs = [d for d in b.get('directions') or ['followers', 'following'] if d in ('followers', 'following')]
    added = [(h, d) for h in map(db.norm_handle, b.get('handles') or []) if h for d in dirs if db.queue_list(conn, h, d)]
    conn.commit()
    return {'queued': len(added)}


def api_pause(conn, q, b):
    db.set_setting(conn, 'paused', bool(b.get('paused')))
    conn.commit()
    return {}


def api_budget(conn, q, b):
    budget = db.get_setting(conn, 'budget')
    budget.update({k: max(0, min(cap, int(b[k]))) for k, cap in BUDGET_MAX.items() if b.get(k) is not None})
    db.set_setting(conn, 'budget', budget)
    conn.commit()
    return {}


ROUTES = [
    ('GET', r'/api/ext/next', ext_next), ('POST', r'/api/ext/list-page', ext_list_page),
    ('POST', r'/api/ext/profile', ext_profile), ('POST', r'/api/ext/error', ext_error),
    ('POST', r'/api/ext/heartbeat', ext_heartbeat),
    ('GET', r'/api/leads', api_leads), ('GET', r'/api/tags', api_tags), ('GET', r'/api/counts', api_counts),
    ('GET', r'/api/person/(\d+)', api_person), ('POST', r'/api/person/(\d+)/mark', api_mark),
    ('POST', r'/api/person/(\d+)/tags', api_tag_edit), ('POST', r'/api/person/(\d+)/read', api_read),
    ('GET', r'/api/map', api_map), ('GET', r'/api/scraper', api_scraper),
    ('POST', r'/api/scraper/seeds', api_seeds), ('POST', r'/api/scraper/pause', api_pause),
    ('POST', r'/api/scraper/budget', api_budget),
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.route('GET')

    def do_POST(self):
        self.route('POST')

    def do_OPTIONS(self):
        if self.headers.get('Origin') != EXT_ORIGIN:
            return self.send(403, b'', 'text/plain')
        self.send(204, b'', 'text/plain', {'Access-Control-Allow-Methods': 'GET, POST',
                                            'Access-Control-Allow-Headers': 'Content-Type'})

    def send(self, code, body, ctype='application/json', headers=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        if self.headers.get('Origin') == EXT_ORIGIN:
            self.send_header('Access-Control-Allow-Origin', EXT_ORIGIN)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def route(self, method):
        url = urlparse(self.path)
        port = CFG['port']
        origin = self.headers.get('Origin')
        if self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}'):
            return self.send(403, {'ok': False, 'error': 'host'})
        if url.path.startswith('/api/ext/'):
            if origin != EXT_ORIGIN:
                return self.send(403, {'ok': False, 'error': 'origin'})
        elif origin and origin not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            return self.send(403, {'ok': False, 'error': 'origin'})
        for m, rx, fn in ROUTES:
            match = re.fullmatch(rx, url.path)
            if m == method and match:
                return self.api(fn, parse_qs(url.query), match.groups(), method == 'POST' or '/ext/' in url.path)
        if method == 'GET' and url.path.startswith('/img/'):
            return self.image(url.path[5:])
        if method == 'GET' and not url.path.startswith('/api/'):
            return self.static(url.path)
        self.send(404, {'ok': False, 'error': 'not found'})

    def api(self, fn, q, groups, with_ok):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n) or b'{}') if n else {}
            conn = db.connect(CFG['db'])
            try:
                out = fn(conn, q, body, *map(int, groups))
            finally:
                conn.close()
        except (Bad, ValueError, KeyError, TypeError) as e:
            return self.send(400, {'ok': False, 'error': str(e)})
        except Exception as e:
            traceback.print_exc()
            return self.send(500, {'ok': False, 'error': str(e)})
        self.send(200, dict(out, ok=True) if with_ok else out)

    def image(self, pid):
        f = pfp_dir() / f'{pid}.jpg'
        if not pid.isdigit() or not f.is_file():
            return self.send(404, {'ok': False, 'error': 'no image'})
        data = f.read_bytes()
        ctype = 'image/png' if data[:4] == b'\x89PNG' else 'image/webp' if data[8:12] == b'WEBP' else 'image/jpeg'
        self.send(200, data, ctype, {'Cache-Control': 'max-age=86400'})

    def static(self, path):
        f = (WEB / (path.lstrip('/') or 'index.html')).resolve()
        if f.is_dir():
            f = f / 'index.html'
        if not f.is_relative_to(WEB.resolve()) or not f.is_file():
            return self.send(404, b'not found', 'text/plain')
        self.send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or 'application/octet-stream',
                  {'Cache-Control': 'no-cache'})


# ---------- background workers ----------

def requalify(conn, p, me):
    edges = edges_of(conn, p['id'])
    pre = qualify.prefilter(p, sorted({e['seed'] for e in edges}))
    old = conn.execute('SELECT model, input_hash FROM verdicts WHERE person_id=?', (p['id'],)).fetchone()
    keep_llm = old and old['input_hash'] and old['input_hash'] == qualify.input_hash(p, edges)
    if not keep_llm:  # the LLM's extra auto tags stay as long as its verdict does
        conn.execute("DELETE FROM tags WHERE person_id=? AND source='auto'", (p['id'],))
    conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,?,'auto')", [(p['id'], t, g) for t, g in qualify.rule_tags(p, edges, me)])
    if keep_llm:
        conn.execute('UPDATE verdicts SET prefilter=?, updated_at=? WHERE person_id=?', (pre, p['updated_at'], p['id']))
        return
    tags = [tuple(r) for r in conn.execute('SELECT tag, grp FROM tags WHERE person_id=?', (p['id'],))]
    v = qualify.rule_verdict(p, tags)
    conn.execute("INSERT OR REPLACE INTO verdicts VALUES(?,?,?,?,?,?,'rules',NULL,?)",
                 (p['id'], pre, v['score'], v['tier'], v['role'], v['reason'], p['updated_at']))


def qualify_batch(conn, limit=200):
    rows = conn.execute('SELECT p.* FROM people p LEFT JOIN verdicts v ON v.person_id=p.id '
                        'WHERE v.person_id IS NULL OR v.updated_at < p.updated_at LIMIT ?', (limit,)).fetchall()
    me = me_handle(conn)
    for r in rows:
        try:
            requalify(conn, dict(r), me)
        except Exception:
            traceback.print_exc()
            conn.execute("INSERT OR REPLACE INTO verdicts(person_id, tier, model, updated_at) VALUES(?,'unread','error',?)",
                         (r['id'], r['updated_at']))
    conn.commit()
    return len(rows)


def llm_step(conn, skip):
    rows = conn.execute("SELECT p.* FROM people p JOIN verdicts v ON v.person_id=p.id WHERE coalesce(p.bio,'')!='' "
                        "AND v.model='rules' AND v.updated_at=p.updated_at ORDER BY v.prefilter DESC LIMIT 50").fetchall()
    p = next((dict(r) for r in rows if skip.get(r['id'], 0) < datetime.now().timestamp()), None)
    if not p:
        return None
    edges = edges_of(conn, p['id'])
    tags = [tuple(r) for r in conn.execute('SELECT tag, grp FROM tags WHERE person_id=?', (p['id'],))]
    v = qualify.llm_verdict(p, tags, edges)
    if v is None:
        skip[p['id']] = datetime.now().timestamp() + 1800
        return False
    if conn.execute('UPDATE verdicts SET score=?, tier=?, role=?, reason=?, model=?, input_hash=? WHERE person_id=? AND updated_at=?',
                    (v['score'], v['tier'], v['role'], v['reason'], v.get('model') or 'llm', qualify.input_hash(p, edges),
                     p['id'], p['updated_at'])).rowcount:
        conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,?,'auto')", [(p['id'], t, g) for t, g in v.get('tags') or []])
    conn.commit()
    return True


def plan_profiles(conn):
    budget = db.get_setting(conn, 'budget')
    ext = db.get_setting(conn, 'ext') or {}
    today = datetime.now(timezone.utc).date().isoformat()
    used = (ext.get('today') or {}).get('profile', 0) if (ext.get('last_seen') or '').startswith(today) else 0
    active = conn.execute("SELECT count(*) FROM jobs WHERE kind='profile' AND state IN ('queued','leased')").fetchone()[0]
    need = budget['profile'] - used - active
    if need <= 0:
        return 0
    rows = conn.execute("""SELECT p.handle, v.prefilter, (SELECT count(DISTINCT seed) FROM edges e WHERE e.person_id=p.id) AS n
        FROM people p JOIN verdicts v ON v.person_id=p.id
        WHERE p.bio_at IS NULL AND coalesce(p.is_private,0)=0
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind='profile' AND j.handle=p.handle)
          AND p.handle NOT IN (SELECT handle FROM seeds)
          AND p.id NOT IN (SELECT person_id FROM marks WHERE status='no')
        ORDER BY n>=2 DESC, v.prefilter DESC, n DESC LIMIT ?""", (need,)).fetchall()
    ts = db.now()
    conn.executemany("INSERT INTO jobs(kind, handle, priority, created_at) VALUES('profile',?,?,?)",
                     [(r['handle'], (r['prefilter'] or 0) + (1000 if r['n'] >= 2 else 0), ts) for r in rows])
    conn.commit()
    return len(rows)


def fetch_pic(url):
    host = urlparse(url).hostname or ''
    if not url.startswith('https://') or not host.endswith(PIC_HOSTS):
        return None
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10, context=SSL) as r:
            if not (urlparse(r.geturl()).hostname or '').endswith(PIC_HOSTS):
                return None
            data = r.read(PIC_MAX + 1)
    except (OSError, ValueError):
        return None
    ok = data[:3] == b'\xff\xd8\xff' or data[:8] == b'\x89PNG\r\n\x1a\n' or (data[:4] == b'RIFF' and data[8:12] == b'WEBP')
    return data if ok and len(data) <= PIC_MAX else None


def pfp_step(conn):
    r = conn.execute('SELECT p.id, p.pic_url FROM people p LEFT JOIN verdicts v ON v.person_id=p.id '
                     'WHERE p.pic_url IS NOT NULL AND p.pic_file IS NULL '
                     'ORDER BY v.score IS NULL, v.score DESC, p.first_seen DESC LIMIT 1').fetchone()
    if not r:
        return False
    data = fetch_pic(r['pic_url'])
    if data:
        pfp_dir().mkdir(parents=True, exist_ok=True)
        (pfp_dir() / f"{r['id']}.jpg").write_bytes(data)
    conn.execute('UPDATE people SET pic_file=? WHERE id=?', (f"{r['id']}.jpg" if data else '', r['id']))
    conn.commit()
    return True


def worker(stop, step, busy_wait, idle_wait):
    conn = db.connect(CFG['db'])
    while not stop.is_set():
        try:
            busy = step(conn)
        except Exception:
            traceback.print_exc()
            busy = False
        stop.wait(busy_wait if busy else idle_wait)


def start_workers(stop):
    skip = {}
    loops = [(qualify_batch, 0, 1), (lambda c: llm_step(c, skip), 3, 10), (plan_profiles, 15, 15), (pfp_step, 0.4, 10)]
    for args in loops:
        threading.Thread(target=worker, args=(stop, *args), daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=CFG['db'])
    ap.add_argument('--port', type=int, default=CFG['port'])
    a = ap.parse_args()
    CFG.update(db=str(Path(a.db).resolve()), port=a.port)
    Path(CFG['db']).parent.mkdir(parents=True, exist_ok=True)
    db.init(CFG['db']).close()
    start_workers(threading.Event())
    print(f'Fortunate Leads on http://127.0.0.1:{a.port}  db={CFG["db"]}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', a.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
