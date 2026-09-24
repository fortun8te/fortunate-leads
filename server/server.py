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
import accounts  # noqa: E402
import db  # noqa: E402
import qualify  # noqa: E402
import rules  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'web'
EXT_ORIGIN = 'chrome-extension://fgdbghllamedgihmdcolaggnbhnakjnf'
CFG = {'db': str(ROOT / 'data' / 'leads.sqlite'), 'port': 8777}
STATUSES = ('good', 'maybe', 'no', 'contacted', 'client', 'known')
PIC_HOSTS = ('.cdninstagram.com', '.fbcdn.net')
PIC_MAX = 2 * 1024 * 1024
READ_PRIORITY = 10000
LEASE_MIN = 10
PROFILE_MAX_ATTEMPTS = 5   # a profile job whose lease keeps expiring (tab crash, hang) is parked as 'error' after this
BULK_MAX = 5000
SSL = ssl.create_default_context(cafile='/etc/ssl/cert.pem' if Path('/etc/ssl/cert.pem').is_file() else None)
BUDGET_MAX = {'list': 3000, 'profile': 300}


class Bad(Exception):
    pass


class Missing(Exception):
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

def ext_state(conn, row=None):
    """What one lane is told: paused = workspace pause or this account paused; budget = its own or the global one."""
    return {'paused': accounts.paused_for(conn, row), 'budget': accounts.budget_of(conn, row)}


def ext_next(conn, q, b):
    """Leases per lane (see accounts.py): never one job to two lanes, lists stick to their lane while it is healthy."""
    lane, ts, now = accounts.lane_of(q, b), db.now(), datetime.now(timezone.utc)
    kinds = [k for k in csv(q, 'kinds') if k in ('list', 'profile')] or ['list', 'profile']
    conn.execute('BEGIN IMMEDIATE')
    # asking for work means no login wall holds it any more
    row = accounts.touch(conn, lane, accounts.account_from(q, b), hold=None)
    accounts.release(conn, now)
    st = ext_state(conn, row)
    if st['paused']:  # cooldowns are the extension's job; the server only records them for display
        conn.commit()
        return dict(st, job=None, cooldown_until=None)
    kinds = accounts.kinds_for(conn, row, kinds, now)
    if not kinds:
        conn.commit()
        return dict(st, job=None)
    # expired leases are re-leased below; a profile read that never comes back after N leases is parked, not retried forever
    conn.execute("UPDATE jobs SET state='error', leased_until=NULL WHERE kind='profile' AND state='leased' AND leased_until<? "
                 "AND attempts>=?", (ts, PROFILE_MAX_ATTEMPTS))
    job = accounts.pick_job(conn, lane, kinds, now)
    if not job:
        conn.commit()
        return dict(st, job=None)
    conn.execute("UPDATE jobs SET state='leased', leased_until=?, attempts=attempts+1, lane=? WHERE id=?",
                 (iso(now + timedelta(minutes=LEASE_MIN)), lane, job['id']))
    accounts.took(conn, lane, job)
    if job['kind'] == 'list':
        conn.execute("UPDATE lists SET state='running', updated_at=? WHERE seed=? AND direction=?",
                     (ts, job['seed'], job['direction']))
        seed = conn.execute('SELECT coalesce(s.ig_id, p.ig_id) AS ig_id FROM seeds s LEFT JOIN people p ON p.handle=s.handle '
                            'WHERE s.handle=?', (job['seed'],)).fetchone()
        lst = conn.execute('SELECT cursor, received FROM lists WHERE seed=? AND direction=?', (job['seed'], job['direction'])).fetchone()
        out = {'id': job['id'], 'kind': 'list', 'seed': job['seed'], 'ig_id': seed and seed['ig_id'],
               'direction': job['direction'], 'cursor': lst and lst['cursor'], 'received': (lst and lst['received']) or 0}
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
    lane = accounts.lane_of(q, b)
    fresh = not job or conn.execute('INSERT OR IGNORE INTO pages(job_id, cursor, at, lane, users) VALUES(?,?,?,?,?)',
                                    (job['id'], b.get('next_cursor') or '', ts, lane, len(b.get('users') or []))).rowcount
    conn.execute('INSERT OR IGNORE INTO seeds(handle, added_at) VALUES(?,?)', (seed, ts))
    if b.get('ig_id'):
        conn.execute('UPDATE seeds SET ig_id=? WHERE handle=?', (str(b['ig_id']), seed))
    pids = []
    for u in b.get('users') or []:
        if u.get('handle'):
            pid = db.upsert_person(conn, {k: u.get(k) for k in ('ig_id', 'handle', 'name', 'pic_url', 'is_private', 'is_verified')}, ts)
            db.add_edge(conn, seed, pid, direction, ts)
            pids.append(pid)
    rules.sync(conn, pids)
    received = conn.execute('SELECT count(*) FROM edges WHERE seed=? AND direction=?', (seed, direction)).fetchone()[0]
    if not fresh:  # outbox retry of a page we already have: never move the cursor back
        conn.commit()
        return {'received': received, 'duplicate': True}
    done = bool(b.get('done'))
    total = b.get('total')
    if total is None:  # the seed's own profile (often captured passively) knows the list size
        row = conn.execute(f"SELECT {'followers' if direction == 'followers' else 'following'} FROM people WHERE handle=?", (seed,)).fetchone()
        total = row[0] if row else None
    conn.execute('INSERT INTO lists(seed, direction, state, cursor, received, total, updated_at) VALUES(?,?,?,?,?,?,?) '
                 'ON CONFLICT DO UPDATE SET state=excluded.state, cursor=excluded.cursor, received=excluded.received, '
                 'total=coalesce(excluded.total, total), error=NULL, updated_at=excluded.updated_at',
                 (seed, direction, 'done' if done else 'running', b.get('next_cursor'), received, total, ts))
    conn.execute("UPDATE jobs SET state=?, leased_until=NULL, attempts=0, lane=NULL WHERE kind='list' AND seed=? AND direction=? "
                 "AND state IN ('queued','leased')", ('done' if done else 'queued', seed, direction))
    conn.commit()
    return {'received': received}


def count_or_none(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, float) and v == v and abs(v) < 1e12:
        v = int(v)
    if isinstance(v, str) and re.fullmatch(r'\s*\d[\d,]*\s*', v):
        v = int(v.replace(',', ''))
    return v if isinstance(v, int) and 0 <= v < 10 ** 12 else None


def ext_profile(conn, q, b):
    p = dict(b.get('profile') or {}) if isinstance(b.get('profile'), dict) else {}
    if not p.get('handle') or not isinstance(p['handle'], str):
        raise Bad('profile.handle required')
    if not (isinstance(p.get('website'), str) and re.match(r'https?://[^\s]+$', p['website'].strip(), re.I)):
        p.pop('website', None)  # javascript:, data:, bare text: never stored, never rendered as a link
    else:
        p['website'] = p['website'].strip()
    for k in ('followers', 'following', 'posts'):
        if k in p:
            p[k] = count_or_none(p[k])
    ts = db.now()
    p['bio'] = p.get('bio') or ''
    p['bio_at'] = ts
    pid = db.upsert_person(conn, p, ts)
    rules.sync(conn, [pid])
    handle = db.norm_handle(p['handle'])
    if p.get('ig_id'):
        conn.execute('UPDATE seeds SET ig_id=? WHERE handle=?', (str(p['ig_id']), handle))
    conn.execute("UPDATE jobs SET state='done', leased_until=NULL WHERE kind='profile' AND (id=? OR handle=?) "
                 "AND state IN ('queued','leased')", (b.get('job_id'), handle))
    conn.commit()
    return {'id': pid}


def ext_error(conn, q, b):
    code, ts, lane = b.get('code'), db.now(), accounts.lane_of(q, b)
    # challenge/login: the extension holds itself (ext.state) until Michael resumes it in the popup. No global pause here:
    # the extension can't clear the server's `paused`, so setting it left scraping stuck after a popup Resume.
    # Per lane: a login wall hands that account's lists to the other lanes now; a list limit hands its list on.
    job = conn.execute("SELECT * FROM jobs WHERE id=? AND state IN ('queued','leased')", (b.get('job_id'),)).fetchone()
    fields = {'last_error': (b.get('message') or code or '')[:500] or None}
    if code in ('rate_limit', 'soft_block'):
        until = utc(b['retry_at']) if b.get('retry_at') else datetime.now(timezone.utc) + timedelta(minutes=15)
        db.set_setting(conn, 'cooldown', iso(until))
        fields['cooldown_until'] = iso(until)
        if job and job['kind'] == 'list':
            fields['list_cool_until'] = iso(until)
    if code in accounts.HOLDS:
        fields['hold'] = code
    accounts.touch(conn, lane, accounts.account_from(q, b), **fields)
    db.set_setting(conn, 'last_error', {'code': code, 'message': b.get('message'), 'at': ts, 'lane': lane})
    if job and code not in ('other', 'private', 'not_found'):
        # rate limits, soft blocks, login walls say nothing about this job: give the lease back to its attempt count,
        # so attempts = leases that ended in 'other' or expired (the ones that may mean the job itself is broken)
        conn.execute('UPDATE jobs SET attempts=max(attempts-1, 0) WHERE id=?', (job['id'],))
        job = conn.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
    if job:  # a late error for a job that already finished must not requeue it
        final = code in ('private', 'not_found') or (code == 'other' and job['attempts'] >= 5)
        conn.execute('UPDATE jobs SET state=?, leased_until=NULL, lane=NULL WHERE id=?',
                     ('done' if code in ('private', 'not_found') else 'error' if final else 'queued', job['id']))
        if job['kind'] == 'list':
            state = 'private' if code == 'private' else 'error' if final else 'queued'
            conn.execute('UPDATE lists SET state=?, error=?, updated_at=? WHERE seed=? AND direction=?',
                         (state, b.get('message') or code, ts, job['seed'], job['direction']))
        elif code == 'private':
            conn.execute('UPDATE people SET is_private=1, updated_at=? WHERE handle=?', (ts, job['handle']))
    if code in accounts.HOLDS:
        accounts.release_all(conn, lane)
    elif 'list_cool_until' in fields:
        accounts.release(conn, datetime.now(timezone.utc), only=lane)
    conn.commit()
    return {}


def clean_rate(r):
    if not isinstance(r, dict):
        return None
    out = {}
    for k in ('pages_hour', 'people_hour'):
        v = r.get(k)
        out[k] = round(float(v), 1) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None
    v = r.get('last_hit_at')
    try:
        out['last_hit_at'] = iso(utc(v)) if isinstance(v, str) and v else None
    except ValueError:
        out['last_hit_at'] = None
    return out


def iso_or_none(v):
    try:
        return iso(utc(v)) if isinstance(v, str) and v else None
    except ValueError:
        return None


def ext_heartbeat(conn, q, b):
    b = dict(b, rate=clean_rate(b.get('rate')))
    lane = accounts.lane_of(q, b)
    db.set_setting(conn, 'ext', dict(b, last_seen=db.now(), lane_id=lane))   # last beat of any lane (older readers)
    today = b.get('today') if isinstance(b.get('today'), dict) else {}

    def text(v, n=500):
        return v[:n] if isinstance(v, str) else None
    fields = {'version': text(b.get('version'), 40), 'state': text(b.get('state'), 20),
              'cooldown_until': iso_or_none(b.get('cooldown_until')), 'rate': json.dumps(b['rate']) if b['rate'] else None,
              'last_error': text(b.get('last_error')), 'activity': text(b.get('activity'), 200), 'text': text(b.get('text'), 200),
              'today': json.dumps({k: count_or_none(today.get(k)) or 0 for k in ('list', 'profile')})}
    if isinstance(b.get('cool'), dict):   # per-bucket cooldowns (3.4+): a list cooldown hands the list to another lane
        fields['list_cool_until'] = iso_or_none(b['cool'].get('list'))
    if 'hold' in b:   # 3.4+ report a login wall / security check here too; older builds only via /api/ext/error
        fields['hold'] = b['hold'] if b['hold'] in accounts.HOLDS else None
    row = accounts.touch(conn, lane, accounts.account_from(q, b), **fields)
    if row['hold'] or row['list_cool_until'] or row['paused']:
        accounts.release(conn, datetime.now(timezone.utc), only=lane)
    conn.commit()
    return ext_state(conn, row)


# ---------- UI endpoints ----------

LISTS = '(SELECT count(DISTINCT e.seed) FROM edges e WHERE e.person_id=p.id)'  # distinct seeds a person is linked to
PEOPLE_FROM = 'FROM people p LEFT JOIN verdicts v ON v.person_id=p.id LEFT JOIN marks m ON m.person_id=p.id'
LEAD_SQL = f'SELECT p.*, v.tier, v.score, v.role, v.reason, m.status, m.note, {LISTS} AS lists {PEOPLE_FROM}'
NOT_ME = 'p.handle NOT IN (SELECT handle FROM seeds WHERE is_me=1)'
# manual first, then rule tags, then auto; inside a source: role, niche, signal, size, source
TAG_ORDER = ("CASE t.source WHEN 'manual' THEN 0 WHEN 'rule' THEN 1 ELSE 2 END, "
             "CASE t.grp WHEN 'role' THEN 0 WHEN 'niche' THEN 1 WHEN 'signal' THEN 2 WHEN 'size' THEN 3 ELSE 4 END, t.tag")
MAP_TAGS = 4


def chunks(ids, n=900):
    ids = list(ids)
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def lead_rows(conn, rows):
    ids = [r['id'] for r in rows]
    if not ids:
        return []
    marks = ','.join('?' * len(ids))
    tags, via = {}, {}
    for t in conn.execute(f'SELECT * FROM tags t WHERE t.person_id IN ({marks}) ORDER BY {TAG_ORDER}', ids):
        tags.setdefault(t['person_id'], []).append({'tag': t['tag'], 'grp': t['grp'], 'source': t['source']})
    for e in conn.execute(f'SELECT DISTINCT person_id, seed FROM edges WHERE person_id IN ({marks}) ORDER BY seed', ids):
        via.setdefault(e['person_id'], []).append(e['seed'])
    return [{'id': r['id'], 'handle': r['handle'], 'name': r['name'], 'pic': f"/img/{r['id']}" if r['pic_file'] else None,
             'bio': r['bio'], 'website': r['website'], 'followers': r['followers'], 'following': r['following'],
             'posts': r['posts'], 'tier': r['tier'] or 'unread', 'score': r['score'], 'role': r['role'], 'reason': r['reason'],
             'tags': tags.get(r['id'], []), 'via': via.get(r['id'], []), 'lists': r['lists'], 'status': r['status']} for r in rows]


def csv(q, key):
    return [x.strip() for x in (q.get(key, [''])[0]).split(',') if x.strip()]


def qint(q, key):
    v = q.get(key, [''])[0].strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        raise Bad(f'{key} must be a whole number') from None


def lead_filter(q):
    """Shared by /api/leads, /api/tags (facets) and /api/map. -> (where clauses on p/v/m, args)."""
    where, args = [], []

    def within(sql, values):  # sql has one {} for the placeholders
        where.append(sql.format(','.join('?' * len(values))))
        args.extend(values)

    tiers = csv(q, 'tier')
    if tiers:
        within("coalesce(v.tier,'unread') IN ({})", tiers)
    for t in dict.fromkeys(csv(q, 'tags')):  # all of
        within('p.id IN (SELECT person_id FROM tags WHERE tag={})', [t])
    if csv(q, 'any'):  # at least one of
        within('p.id IN (SELECT person_id FROM tags WHERE tag IN ({}))', csv(q, 'any'))
    if csv(q, 'not'):  # none of
        within('p.id NOT IN (SELECT person_id FROM tags WHERE tag IN ({}))', csv(q, 'not'))
    statuses = csv(q, 'status')
    if not statuses:
        where.append("coalesce(m.status,'')!='no'")
    elif 'all' not in statuses:
        named = [s for s in statuses if s != 'none']
        if any(s not in STATUSES for s in named):
            raise Bad('bad status')
        conds = (['m.status IS NULL'] if 'none' in statuses else []) + (['m.status IN ({})'] if named else [])
        within('(' + ' OR '.join(conds) + ')', named)
    text = q.get('q', [''])[0].strip()
    if text:
        where.append('(p.handle LIKE ? OR p.name LIKE ? OR p.bio LIKE ?)')
        args += [f'%{text}%'] * 3
    min_lists = qint(q, 'min_lists') or 0
    if min_lists > 0:
        where.append(f'{LISTS}>=?')
        args.append(min_lists)
    has_bio = q.get('has_bio', [''])[0].strip()
    if has_bio in ('1', 'true'):
        where.append("coalesce(p.bio,'')!=''")
    elif has_bio in ('0', 'false'):
        where.append("coalesce(p.bio,'')=''")
    elif has_bio:
        raise Bad('has_bio must be 1 or 0')
    for s in dict.fromkeys(map(db.norm_handle, csv(q, 'seed'))):  # linked to every listed seed
        where.append('p.id IN (SELECT person_id FROM edges WHERE seed=?)')
        args.append(s)
    for key, op in (('followers_min', '>='), ('followers_max', '<=')):
        n = qint(q, key)
        if n is not None:
            where.append(f'p.followers {op} ?')
            args.append(n)
    return where, args


def api_leads(conn, q, b):
    where, args = lead_filter(q)
    order = {'recent': 'p.updated_at DESC', 'followers': 'p.followers IS NULL, p.followers DESC',
             'connected': 'lists DESC, p.followers IS NULL, p.followers DESC'}.get(
        q.get('sort', ['score'])[0], 'v.score IS NULL, v.score DESC, p.followers DESC')
    offset = max(0, qint(q, 'offset') or 0)
    limit = min(500, max(1, qint(q, 'limit') or 50))
    sql_where = ' WHERE ' + ' AND '.join([NOT_ME] + where)
    total = conn.execute(f'SELECT count(*) {PEOPLE_FROM}{sql_where}', args).fetchone()[0]
    rows = conn.execute(f'{LEAD_SQL}{sql_where} ORDER BY {order}, p.id LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
    return {'total': total, 'rows': lead_rows(conn, rows)}


def api_tags(conn, q, b):
    """Facets: per (tag, source) the people in the current filtered set (`count`) and overall (`total`). One grouped scan."""
    where, args = lead_filter(q)
    counts = dict(((r[0], r[1]), r[2]) for r in conn.execute(
        f"""WITH f AS MATERIALIZED (SELECT p.id {PEOPLE_FROM} WHERE {' AND '.join([NOT_ME] + where)})
        SELECT t.tag, t.source, count(*) FROM f JOIN tags t ON t.person_id=f.id GROUP BY t.tag, t.source""", args))
    out = [{'tag': r[0], 'grp': r[2], 'source': r[1], 'count': counts.get((r[0], r[1]), 0), 'total': r[3]}  # totals: covering index
           for r in conn.execute('SELECT tag, source, min(grp), count(*) FROM tags GROUP BY tag, source')]
    return sorted(out, key=lambda f: (-f['count'], -f['total'], f['tag'], f['source']))


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


KEEP = object()   # "leave this field as it is"


def set_status(conn, pids, status=KEEP, note=KEEP):
    """Upsert marks. KEEP leaves a field alone; None / '' clears it. A row with neither status nor note is removed."""
    ts = db.now()
    sets = [f'{col}=excluded.{col}' for col, v in (('status', status), ('note', note)) if v is not KEEP]
    if sets:
        rows = [(p, None if status is KEEP else status, None if note is KEEP else (note or None), ts) for p in pids]
        conn.executemany('INSERT INTO marks(person_id, status, note, updated_at) VALUES(?,?,?,?) ON CONFLICT(person_id) DO UPDATE SET '
                         + ', '.join(sets + ['updated_at=excluded.updated_at']), rows)
    for chunk in chunks(pids):
        conn.execute(f"DELETE FROM marks WHERE person_id IN ({','.join('?' * len(chunk))}) AND status IS NULL AND coalesce(note,'')=''", chunk)


def api_mark(conn, q, b, pid):
    """{"status"?: null|STATUS, "note"?: str|null}: an absent key is left alone, null clears (note '' clears too)."""
    person_row(conn, pid)
    status, note = b.get('status', KEEP), b.get('note', KEEP)
    if status is not KEEP and status is not None and status not in STATUSES:
        raise Bad('bad status')
    if note is not KEEP and note is not None and (not isinstance(note, str) or len(note) > 5000):
        raise Bad('note must be text')
    set_status(conn, [pid], status, note)
    conn.commit()
    return {}


def clean_tag(t):
    if not isinstance(t, str):
        raise Bad('tag must be a string')
    t = re.sub(r'\s+', ' ', t).strip()
    if not t or len(t) > 64 or ',' in t:
        raise Bad('tag must be 1-64 characters without commas')
    return t


def tag_group(conn, tag, default='signal'):
    row = conn.execute("SELECT grp FROM tags WHERE tag=? ORDER BY source='manual' DESC LIMIT 1", (tag,)).fetchone()
    return row[0] if row else default


def touch(conn, pids):
    """Bump updated_at: the qualify batch re-derives verdicts (manual role tags count) and the map rev changes."""
    ts = db.now()
    conn.executemany('UPDATE people SET updated_at=? WHERE id=?', [(ts, p) for p in dict.fromkeys(pids)])


def add_manual(conn, pids, tags):
    for t in tags:
        grp = tag_group(conn, t)
        conn.executemany("INSERT OR REPLACE INTO tags VALUES(?,?,?,'manual')", [(p, t, grp) for p in pids])


def api_tag_edit(conn, q, b, pid):
    person_row(conn, pid)
    add_manual(conn, [pid], [clean_tag(t) for t in b.get('add') or []])
    for t in b.get('remove') or []:
        if isinstance(t, str):
            conn.execute('DELETE FROM tags WHERE person_id=? AND tag=?', (pid, t))
    touch(conn, [pid])
    conn.commit()
    return {}


def api_tag_rename(conn, q, b):
    src, dst = clean_tag(b.get('from')), clean_tag(b.get('to'))
    if src == dst:
        return {'renamed': 0}
    pids = [r[0] for r in conn.execute("SELECT person_id FROM tags WHERE tag=? AND source='manual'", (src,))]
    grp = tag_group(conn, dst, tag_group(conn, src))
    # merge: someone who already has `to` (any source) keeps one tag, now manual
    conn.execute("INSERT INTO tags(person_id, tag, grp, source) SELECT person_id, ?, ?, 'manual' FROM tags WHERE tag=? AND source='manual' "
                 "ON CONFLICT(person_id, tag) DO UPDATE SET source='manual'", (dst, grp, src))
    conn.execute("DELETE FROM tags WHERE tag=? AND source='manual'", (src,))
    touch(conn, pids)
    conn.commit()
    return {'renamed': len(pids)}


def api_tag_delete(conn, q, b):
    tag = clean_tag(b.get('tag'))
    pids = [r[0] for r in conn.execute("SELECT person_id FROM tags WHERE tag=? AND source='manual'", (tag,))]
    conn.execute("DELETE FROM tags WHERE tag=? AND source='manual'", (tag,))
    touch(conn, pids)
    conn.commit()
    return {'deleted': len(pids)}


def api_bulk(conn, q, b):
    ids = b.get('ids')
    if not isinstance(ids, list) or len(ids) > BULK_MAX or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise Bad(f'ids must be a list of up to {BULK_MAX} ids')
    add = [clean_tag(t) for t in b.get('add') or []]
    remove = [t for t in b.get('remove') or [] if isinstance(t, str)]
    status = b.get('status')
    if 'status' in b and status is not None and status not in STATUSES:
        raise Bad('bad status')
    pids = [r[0] for chunk in chunks(dict.fromkeys(ids))
            for r in conn.execute(f"SELECT id FROM people WHERE id IN ({','.join('?' * len(chunk))})", chunk)]
    add_manual(conn, pids, add)
    conn.executemany('DELETE FROM tags WHERE person_id=? AND tag=?', [(p, t) for p in pids for t in remove])
    if 'status' in b:  # absent = leave marks alone; null = clear the status (a note is kept)
        set_status(conn, pids, status=status)
    touch(conn, pids)
    conn.commit()
    return {'updated': len(pids)}


# ---------- tag rules and saved views ----------

def rule_out(conn, r):
    hits = conn.execute("SELECT count(*) FROM tags WHERE tag=? AND source='rule'", (r['tag'],)).fetchone()[0]
    return {'id': r['id'], 'tag': r['tag'], 'grp': r['grp'], 'field': r['field'], 'match': r['match'], 'hits': hits}


def api_rules(conn, q, b):
    return [rule_out(conn, r) for r in conn.execute('SELECT * FROM tag_rules ORDER BY tag, id')]


def api_rule_add(conn, q, b):
    tag, field = clean_tag(b.get('tag')), b.get('field')
    match = b.get('match').strip() if isinstance(b.get('match'), str) else b.get('match')
    try:
        rules.compile_match(field, match)
    except ValueError as e:
        raise Bad(str(e)) from None
    grp = b.get('grp') if b.get('grp') in rules.GROUPS else tag_group(conn, tag)
    row = conn.execute('SELECT * FROM tag_rules WHERE tag=? AND field=? AND match=?', (tag, field, match)).fetchone()
    if row:
        return rule_out(conn, row)
    rule, started = {'tag': tag, 'grp': grp, 'field': field, 'match': match}, db.now()
    try:
        pids = rules.matching_ids(conn, rule)  # read-only scan: ingest keeps writing meanwhile
    except ValueError as e:
        raise Bad(str(e)) from None
    rid = conn.execute('INSERT INTO tag_rules(tag, grp, field, match, created_at) VALUES(?,?,?,?,?)',
                       (tag, grp, field, match, db.now())).lastrowid
    rules.write_rule_tags(conn, rule, pids)
    # people ingested while we scanned did not know this rule yet
    rules.sync(conn, [r[0] for r in conn.execute('SELECT id FROM people WHERE updated_at>=?', (started,))])
    conn.commit()
    return rule_out(conn, conn.execute('SELECT * FROM tag_rules WHERE id=?', (rid,)).fetchone())


def api_rule_preview(conn, q, b):
    """How many people a rule would tag, without saving anything. Same matcher and guards as create."""
    rule = {'field': q.get('field', [''])[0], 'match': q.get('match', [''])[0].strip()}
    try:
        rules.compile_match(rule['field'], rule['match'])
        return {'hits': len(rules.matching_ids(conn, rule, budget=rules.PREVIEW_BUDGET))}
    except ValueError as e:
        raise Bad(str(e)) from None


def api_rule_delete(conn, q, b, rid):
    row = conn.execute('SELECT * FROM tag_rules WHERE id=?', (rid,)).fetchone()
    if not row:
        return {'deleted': 0}
    keep = []
    for other in rules.load(conn):  # another rule may give the same tag: scan for it before taking the write lock
        if other['tag'] == row['tag'] and other['id'] != rid:
            try:
                keep.append((other, rules.matching_ids(conn, other)))
            except ValueError:
                pass
    conn.execute('DELETE FROM tag_rules WHERE id=?', (rid,))
    conn.execute("DELETE FROM tags WHERE tag=? AND source='rule'", (row['tag'],))
    for other, pids in keep:
        rules.write_rule_tags(conn, other, pids)
    conn.commit()
    return {'deleted': 1}


def api_views(conn, q, b):
    return [dict(r) for r in conn.execute('SELECT id, name, query FROM saved_views ORDER BY name COLLATE NOCASE, id')]


def api_view_save(conn, q, b):
    name, query = b.get('name'), b.get('query')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        raise Bad('name must be 1-80 characters')
    if not isinstance(query, str) or len(query) > 4000:
        raise Bad('query must be a URL query string')
    name, query = name.strip(), query.strip().lstrip('?')
    conn.execute('INSERT INTO saved_views(name, query, created_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET query=excluded.query',
                 (name, query, db.now()))
    conn.commit()
    return {'id': conn.execute('SELECT id FROM saved_views WHERE name=?', (name,)).fetchone()[0], 'name': name, 'query': query}


def api_view_delete(conn, q, b, vid):
    n = conn.execute('DELETE FROM saved_views WHERE id=?', (vid,)).rowcount
    conn.commit()
    return {'deleted': n}


def api_read(conn, q, b, pid):
    handle = person_row(conn, pid)['handle']
    if not conn.execute("UPDATE jobs SET priority=? WHERE kind='profile' AND handle=? AND state IN ('queued','leased')",
                        (READ_PRIORITY, handle)).rowcount:
        conn.execute("INSERT INTO jobs(kind, handle, priority, created_at) VALUES('profile',?,?,?)", (handle, READ_PRIORITY, db.now()))
    conn.commit()
    return {}


# ---------- map ----------

def data_rev(conn):
    r = conn.execute('SELECT (SELECT max(updated_at) FROM people), (SELECT max(updated_at) FROM verdicts), '
                     '(SELECT count(*) FROM edges), (SELECT count(*) FROM seeds), (SELECT max(updated_at) FROM marks), '
                     '(SELECT count(*) FROM marks), (SELECT count(*) FROM tags)').fetchone()
    return zlib.crc32('|'.join(map(str, r)).encode())


SEED_LINKS = [None]   # [(key, computed_at, links)]: replaced whole, never mutated, so readers can't race a writer
SEED_LINKS_TOP = 50
SEED_LINKS_MIN_AGE = 30   # s: while a list is streaming in, recompute the overlap at most this often (same db)


def seed_links(conn):
    key = (CFG['db'], *conn.execute('SELECT count(*), max(rowid) FROM edges').fetchone())
    cached, now = SEED_LINKS[0], datetime.now().timestamp()
    if cached and (cached[0] == key or (cached[0][0] == key[0] and now - cached[1] < SEED_LINKS_MIN_AGE)):
        return cached[2]
    rows = conn.execute("""WITH ps AS (SELECT DISTINCT seed, person_id FROM edges WHERE person_id IN
            (SELECT person_id FROM edges GROUP BY person_id HAVING count(DISTINCT seed)>1))
        SELECT a.seed AS a, b.seed AS b, count(*) AS shared FROM ps a JOIN ps b ON b.person_id=a.person_id AND b.seed>a.seed
        GROUP BY a.seed, b.seed ORDER BY shared DESC, a.seed, b.seed LIMIT ?""", (SEED_LINKS_TOP,)).fetchall()
    links = [{'source': f"s:{r['a']}", 'target': f"s:{r['b']}", 'shared': r['shared']} for r in rows]
    SEED_LINKS[0] = (key, now, links)
    return links


def api_map(conn, q, b):
    limit = min(3000, max(10, qint(q, 'limit') or 400))
    where, args = lead_filter(q)
    cond = ' AND '.join(['p.handle NOT IN (SELECT handle FROM seeds)'] + where)
    # one materialized pass over the filtered people; both picks below sort that set (cost follows the filter's size)
    base = ('SELECT p.id, p.handle, p.name, p.pic_file, p.followers, v.tier, v.score, v.reason, m.status, '
            'count(DISTINCT e.seed) AS degree FROM people p JOIN edges e ON e.person_id=p.id '
            f'LEFT JOIN verdicts v ON v.person_id=p.id LEFT JOIN marks m ON m.person_id=p.id WHERE {cond} GROUP BY p.id')
    by_score = 'ORDER BY score IS NULL, score DESC, degree DESC, id LIMIT ?'
    multi_n = 0 if q.get('scope', ['leads'])[0] == 'all' else limit * 3 // 5  # scope=leads: people in several lists first
    rows = conn.execute(f"""WITH b AS MATERIALIZED ({base})
        SELECT * FROM (SELECT 0 AS part, * FROM b WHERE degree>=2 ORDER BY degree DESC, score DESC, id LIMIT ?)
        UNION ALL SELECT * FROM (SELECT 1 AS part, * FROM b {by_score})""", (*args, multi_n, limit)).fetchall()
    multi = [r for r in rows if r['part'] == 0]
    seen = {r['id'] for r in multi}
    people = multi + [r for r in rows if r['part'] == 1 and r['id'] not in seen][:limit - len(multi)]
    seeds = conn.execute('SELECT s.handle, (SELECT count(*) FROM edges e WHERE e.seed=s.handle) AS degree, p.id AS pid, '
                         'p.pic_file, p.followers, v.tier, v.score, m.status, coalesce(sd.is_me, 0) AS is_me '
                         'FROM (SELECT handle FROM seeds UNION SELECT seed FROM edges) s LEFT JOIN seeds sd ON sd.handle=s.handle '
                         'LEFT JOIN people p ON p.handle=s.handle LEFT JOIN verdicts v ON v.person_id=p.id '
                         'LEFT JOIN marks m ON m.person_id=p.id').fetchall()
    node_of = {r['id']: f"p:{r['id']}" for r in people}
    node_of.update((s['pid'], f"s:{s['handle']}") for s in seeds if s['pid'])
    links, seeds_of, tags = [], {}, {}
    for chunk in chunks(node_of):
        marks = ','.join('?' * len(chunk))
        for e in conn.execute(f'SELECT * FROM edges WHERE person_id IN ({marks}) ORDER BY seed, direction', chunk):
            links.append({'source': f"s:{e['seed']}", 'target': node_of[e['person_id']], 'direction': e['direction']})
            if e['seed'] not in seeds_of.setdefault(e['person_id'], []):
                seeds_of[e['person_id']].append(e['seed'])
        for t in conn.execute(f'SELECT t.person_id, t.tag FROM tags t WHERE t.person_id IN ({marks}) ORDER BY t.person_id, {TAG_ORDER}', chunk):
            if len(tags.setdefault(t['person_id'], [])) < MAP_TAGS:
                tags[t['person_id']].append(t['tag'])
    nodes = [{'id': f"s:{s['handle']}", 'kind': 'seed', 'label': s['handle'], 'tier': s['tier'], 'score': s['score'],
              'pic': f"/img/{s['pid']}" if s['pic_file'] else None, 'degree': s['degree'], 'followers': s['followers'],
              'status': s['status'], 'lists': len(seeds_of.get(s['pid'], [])), 'tags': tags.get(s['pid'], []),
              'seeds': seeds_of.get(s['pid'], []), 'is_me': bool(s['is_me'])} for s in seeds]
    nodes += [{'id': f"p:{r['id']}", 'kind': 'lead', 'label': r['handle'], 'handle': r['handle'], 'name': r['name'],
               'tier': r['tier'] or 'unread', 'score': r['score'], 'reason': r['reason'], 'tags': tags.get(r['id'], []),
               'pic': f"/img/{r['id']}" if r['pic_file'] else None, 'degree': r['degree'], 'lists': r['degree'],
               'status': r['status'], 'followers': r['followers'], 'seeds': seeds_of.get(r['id'], [])} for r in people]
    return {'nodes': nodes, 'links': links, 'seed_links': seed_links(conn), 'rev': data_rev(conn)}


def ext_aggregate(conn, accts, now):
    """The old single-extension `ext` block, now summed over lanes (one lane: exactly what it reported)."""
    if not accts:
        ext = db.get_setting(conn, 'ext') or {}
        cooldowns = [c for c in (ext.get('cooldown_until'), db.get_setting(conn, 'cooldown')) if c and utc(c) > now]
        return {'online': bool(ext.get('last_seen')) and now - utc(ext['last_seen']) < timedelta(seconds=60),
                'version': ext.get('version'), 'state': ext.get('state'),
                'cooldown_until': iso(max(map(utc, cooldowns))) if cooldowns else None,
                'today': ext.get('today'), 'budget': db.get_setting(conn, 'budget'),
                'last_seen': ext.get('last_seen'), 'activity': ext.get('activity'), 'text': ext.get('text'),
                'rate': ext.get('rate'), 'last_error': ext.get('last_error') or (db.get_setting(conn, 'last_error') or {}).get('message')}
    live = [a for a in accts if a['online']] or accts
    many = len(accts) > 1
    running = [a for a in live if a['state'] == 'running']
    first = (running or sorted(live, key=lambda a: a['last_seen'] or '', reverse=True))[0]
    cools = [a['cooldown_until'] for a in live if a['cooldown_until']]
    all_cool = len(cools) == len(live) and cools
    rate = accounts.aggregate_rate(accts)
    errs = sorted((a for a in accts if a['last_error']), key=lambda a: a['last_seen'] or '')
    prefix = (lambda a, t: f"{a['name']}: {t}" if t and many else t)
    last_error = errs[-1]['last_error'] if errs else (db.get_setting(conn, 'last_error') or {}).get('message')
    return {'online': any(a['online'] for a in accts), 'version': max((a['version'] or '' for a in accts), default=None) or None,
            'state': 'running' if running else first['state'],
            'cooldown_until': min(cools) if all_cool else None,
            'today': {k: sum(a['today'][k] for a in accts) for k in ('list', 'profile')},
            'budget': db.get_setting(conn, 'budget'), 'last_seen': max(a['last_seen'] or '' for a in accts) or None,
            'activity': prefix(first, first['activity']), 'text': prefix(first, first['text']),
            'rate': None if not any(a['rate'] for a in live) else {k: rate[k] for k in ('pages_hour', 'people_hour', 'last_hit_at')},
            'last_error': prefix(errs[-1], last_error) if errs else last_error}


def api_scraper(conn, q, b):
    now = datetime.now(timezone.utc)
    accts = accounts.listing(conn, now)
    return {'ext': ext_aggregate(conn, accts, now), 'accounts': accts, 'rate': accounts.aggregate_rate(accts),
            'alerts': accounts.alerts(conn, now, accts),
            'paused': bool(db.get_setting(conn, 'paused')),
            'qualify': bool(db.get_setting(conn, 'qualify')), 'qualify_auto': bool(db.get_setting(conn, 'qualify_auto')),
            'soak': soak(conn, now),
            'people_today': conn.execute('SELECT count(*) FROM people WHERE first_seen>=?', (iso(now)[:10],)).fetchone()[0],
            'lists': [dict(r) for r in conn.execute('SELECT seed, direction, state, received, total, updated_at, error FROM lists '
                                                    'ORDER BY updated_at DESC')],
            'queue': dict.fromkeys(('list', 'profile'), 0) | dict(conn.execute(
                "SELECT kind, count(*) FROM jobs WHERE state IN ('queued','leased') GROUP BY kind").fetchall())}


def soak(conn, now):
    out = {}
    for label, hours in (('1h', 1), ('6h', 6)):
        since = iso(now - timedelta(hours=hours))
        out[label] = {'pages': conn.execute('SELECT count(*) FROM pages WHERE at>=?', (since,)).fetchone()[0],
                      'people': conn.execute('SELECT count(*) FROM edges WHERE first_seen>=?', (since,)).fetchone()[0],
                      'new_people': conn.execute('SELECT count(*) FROM people WHERE first_seen>=?', (since,)).fetchone()[0],
                      'profiles': conn.execute('SELECT count(*) FROM people WHERE bio_at>=?', (since,)).fetchone()[0]}
    return out


def api_qualify(conn, q, b):
    if not isinstance(b.get('on'), bool):
        raise Bad('on must be true or false')
    db.set_setting(conn, 'qualify', b['on'])
    if 'auto' in b:
        db.set_setting(conn, 'qualify_auto', bool(b['auto']))
    conn.commit()
    return {'qualify': b['on']}


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


def api_accounts(conn, q, b):
    now = datetime.now(timezone.utc)
    accts = accounts.listing(conn, now)
    return {'accounts': accts, 'alerts': accounts.alerts(conn, now, accts), 'rate': accounts.aggregate_rate(accts),
            'main_list_share': float(db.get_setting(conn, 'main_list_share') or 0)}


def api_account_edit(conn, q, b, lane):
    conn.execute('BEGIN IMMEDIATE')
    try:
        row = accounts.edit(conn, str(lane), b)
    except LookupError:
        conn.rollback()
        raise Missing('no such account') from None
    except ValueError as e:
        conn.rollback()
        raise Bad(str(e)) from None
    conn.commit()
    return {'account': accounts.out(conn, row, datetime.now(timezone.utc))}


def api_account_remove(conn, q, b, lane):
    conn.execute('BEGIN IMMEDIATE')
    n = accounts.remove(conn, str(lane))
    conn.commit()
    return {'removed': n}


def api_setup(conn, q, b):
    """What the add-account wizard shows: where the unpacked extension lives and which id Chrome gives it."""
    manifest = json.loads((ROOT / 'extension' / 'manifest.json').read_text())
    return {'repo': str(ROOT), 'extension_path': str(ROOT / 'extension'), 'extension_id': EXT_ORIGIN.split('//')[1],
            'extension_version': manifest.get('version'), 'server': f"http://127.0.0.1:{CFG['port']}",
            'lanes': conn.execute('SELECT count(*) FROM accounts').fetchone()[0]}


LANE = r'([A-Za-z0-9_-]{1,64})'
ROUTES = [
    ('GET', r'/api/accounts', api_accounts), ('POST', rf'/api/accounts/{LANE}', api_account_edit),
    ('POST', rf'/api/accounts/{LANE}/remove', api_account_remove), ('GET', r'/api/setup', api_setup),
    ('GET', r'/api/ext/next', ext_next), ('POST', r'/api/ext/list-page', ext_list_page),
    ('POST', r'/api/ext/profile', ext_profile), ('POST', r'/api/ext/error', ext_error),
    ('POST', r'/api/ext/heartbeat', ext_heartbeat),
    ('GET', r'/api/leads', api_leads), ('GET', r'/api/tags', api_tags), ('GET', r'/api/counts', api_counts),
    ('POST', r'/api/tags/rename', api_tag_rename), ('POST', r'/api/tags/delete', api_tag_delete),
    ('POST', r'/api/people/bulk', api_bulk),
    ('GET', r'/api/tag-rules', api_rules), ('POST', r'/api/tag-rules', api_rule_add), ('GET', r'/api/tag-rules/preview', api_rule_preview),
    ('POST', r'/api/tag-rules/(\d+)/delete', api_rule_delete),
    ('GET', r'/api/views', api_views), ('POST', r'/api/views', api_view_save), ('POST', r'/api/views/(\d+)/delete', api_view_delete),
    ('GET', r'/api/person/(\d+)', api_person), ('POST', r'/api/person/(\d+)/mark', api_mark),
    ('POST', r'/api/person/(\d+)/tags', api_tag_edit), ('POST', r'/api/person/(\d+)/read', api_read),
    ('GET', r'/api/map', api_map), ('GET', r'/api/scraper', api_scraper),
    ('POST', r'/api/scraper/seeds', api_seeds), ('POST', r'/api/scraper/pause', api_pause),
    ('POST', r'/api/scraper/budget', api_budget), ('POST', r'/api/settings/qualify', api_qualify),
]


SECURITY_HEADERS = {'X-Frame-Options': 'DENY', 'Content-Security-Policy': "frame-ancestors 'none'", 'X-Content-Type-Options': 'nosniff'}


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
                                            'Access-Control-Allow-Headers': 'Content-Type, X-FL'})

    def send(self, code, body, ctype='application/json', headers=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        if self.headers.get('Origin') == EXT_ORIGIN:
            self.send_header('Access-Control-Allow-Origin', EXT_ORIGIN)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
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
            # Extension GETs carry no Origin; a custom header can't be sent cross-site without a (refused) preflight.
            if origin != EXT_ORIGIN and not (origin is None and self.headers.get('X-FL') == '1'):
                return self.send(403, {'ok': False, 'error': 'origin'})
        elif origin and origin not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            return self.send(403, {'ok': False, 'error': 'origin'})
        elif method == 'POST' and not origin:  # browsers always send Origin on POST; a missing one is not our page
            return self.send(403, {'ok': False, 'error': 'origin required'})
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
                out = fn(conn, q, body, *(int(g) if g.isdigit() and '/accounts/' not in self.path else g for g in groups))
            finally:
                conn.close()
        except Missing as e:
            return self.send(404, {'ok': False, 'error': str(e)})
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
    rules.sync(conn, [r['id'] for r in rows])  # rule tags first, so the verdict sees them
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
    if not db.get_setting(conn, 'qualify'):
        return None
    t = datetime.now().timestamp()
    for k in [k for k, until in skip.items() if until <= t]:
        del skip[k]
    held = list(skip)[:900]  # recently failed: skip in SQL so they can't starve everyone behind them
    row = conn.execute("SELECT p.* FROM people p JOIN verdicts v ON v.person_id=p.id WHERE coalesce(p.bio,'')!='' "
                       "AND v.model='rules' AND v.updated_at=p.updated_at "
                       f"AND p.id NOT IN ({','.join('?' * len(held))}) ORDER BY v.prefilter DESC LIMIT 1", held).fetchone()
    p = dict(row) if row else None
    if not p:
        return None
    edges = edges_of(conn, p['id'])
    tags = [tuple(r) for r in conn.execute('SELECT tag, grp FROM tags WHERE person_id=?', (p['id'],))]
    try:
        v = qualify.llm_verdict(p, tags, edges)
    except Exception:  # never let one bad reply spin the worker on the same row: fall back like 'no model'
        traceback.print_exc()
        v = None
    if v is None:
        skip[p['id']] = datetime.now().timestamp() + 1800
        return False
    if conn.execute('UPDATE verdicts SET score=?, tier=?, role=?, reason=?, model=?, input_hash=? WHERE person_id=? AND updated_at=?',
                    (v['score'], v['tier'], v['role'], v['reason'], v.get('model') or 'llm', qualify.input_hash(p, edges),
                     p['id'], p['updated_at'])).rowcount:
        conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,?,'auto')", [(p['id'], t, g) for t, g in v.get('tags') or []])
    conn.commit()
    return True


def auto_qualify(conn):
    """Switch qualification on once every queued list has been collected (setting qualify_auto, default on)."""
    if db.get_setting(conn, 'qualify') or not db.get_setting(conn, 'qualify_auto'):
        return False
    lists = conn.execute("SELECT count(*), count(CASE WHEN state IN ('queued','running') THEN 1 END) FROM lists").fetchone()
    jobs = conn.execute("SELECT count(*) FROM jobs WHERE kind='list' AND state IN ('queued','leased')").fetchone()[0]
    if not lists[0] or lists[1] or jobs:
        return False
    db.set_setting(conn, 'qualify', True)
    conn.commit()
    return True


def plan_priority(lists, prefilter):
    """Lists count first, prefilter second; always below READ_PRIORITY (a read Michael asked for goes first)."""
    return min(lists or 0, 9) * 1000 + max(0, min(999, prefilter or 0))


def retag_if_changed(conn):
    """When the rule taxonomy changes (qualify.TAGS_VERSION), let the qualify batch re-derive everyone's auto tags.
    LLM verdicts survive: requalify keeps them while the input hash is unchanged."""
    version = getattr(qualify, 'TAGS_VERSION', None)
    if version is None or db.get_setting(conn, 'tags_version') == version:
        return False
    conn.execute("UPDATE verdicts SET updated_at=''")
    db.set_setting(conn, 'tags_version', version)
    conn.commit()
    return True


def plan_profiles(conn):
    auto_qualify(conn)
    if not db.get_setting(conn, 'qualify'):  # collection first: bios are read only once qualification is switched on
        return 0
    now = datetime.now(timezone.utc)
    accts = [a for a in accounts.listing(conn, now) if a['healthy'] and a['role'] in ('bios', 'both')]
    if accts:   # every lane that reads bios brings its own daily budget
        room = sum(max(0, a['budget']['profile'] - a['today']['profile']) for a in accts)
    else:
        ext = db.get_setting(conn, 'ext') or {}
        today = now.date().isoformat()
        used = (ext.get('today') or {}).get('profile', 0) if (ext.get('last_seen') or '').startswith(today) else 0
        room = db.get_setting(conn, 'budget')['profile'] - used
    active = conn.execute("SELECT count(*) FROM jobs WHERE kind='profile' AND state IN ('queued','leased')").fetchone()[0]
    need = room - active
    if need <= 0:
        return 0
    rows = conn.execute("""SELECT p.handle, v.prefilter, (SELECT count(DISTINCT seed) FROM edges e WHERE e.person_id=p.id) AS n
        FROM people p JOIN verdicts v ON v.person_id=p.id
        WHERE p.bio_at IS NULL AND coalesce(p.is_private,0)=0
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind='profile' AND j.handle=p.handle)
          AND p.handle NOT IN (SELECT handle FROM seeds)
          AND p.id NOT IN (SELECT person_id FROM marks WHERE status='no')
        ORDER BY n DESC, v.prefilter DESC, p.id LIMIT ?""", (need,)).fetchall()
    ts = db.now()
    conn.executemany("INSERT INTO jobs(kind, handle, priority, created_at) VALUES('profile',?,?,?)",
                     [(r['handle'], plan_priority(r['n'], r['prefilter']), ts) for r in rows])
    conn.commit()
    return len(rows)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None   # urllib then raises HTTPError(3xx): nothing is fetched from the redirect target


PIC_OPENER = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=SSL))


def fetch_pic(url):
    host = urlparse(url).hostname or ''
    if not url.startswith('https://') or not host.endswith(PIC_HOSTS):
        return None
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with PIC_OPENER.open(req, timeout=10) as r:
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
                     'ORDER BY p.updated_at DESC LIMIT 1').fetchone()
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
    conn = db.init(CFG['db'])
    retag_if_changed(conn)
    conn.close()
    start_workers(threading.Event())
    print(f'Fortunate Leads on http://127.0.0.1:{a.port}  db={CFG["db"]}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', a.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
