import argparse
import biofetch
import collections
import time
import json
import mimetypes
import os
import re
import socket
import ssl
import sys
import threading
import traceback
import urllib.request
import zlib
from collections import Counter, OrderedDict
from itertools import chain, combinations
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounts  # noqa: E402
import control  # noqa: E402
import db  # noqa: E402
import laya  # noqa: E402
import llm  # noqa: E402
import qualify  # noqa: E402
import rules  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'web'
EXT_ORIGIN = 'chrome-extension://fgdbghllamedgihmdcolaggnbhnakjnf'
CFG = {'db': str(ROOT / 'data' / 'leads.sqlite'), 'port': 8777}
# Pipeline: (unmarked) -> interested -> contacted -> talking -> client; 'no' = Not a fit (hidden by default).
STATUSES = ('interested', 'contacted', 'talking', 'client', 'no')
POSITIVE = ('interested', 'talking', 'client')   # what used to be good/client: positive few-shot, seed yield, snowball
POSITIVE_SQL = "('interested','talking','client')"
LEGACY_STATUS = {'good': 'interested'}           # older clients / saved views


def status_in(v):
    return LEGACY_STATUS.get(v, v)
PIC_HOSTS = ('.cdninstagram.com', '.fbcdn.net')
PIC_MAX = 2 * 1024 * 1024
READ_PRIORITY = 10000
LEASE_MIN = 10
PROFILE_MAX_ATTEMPTS = 5   # a profile job whose lease keeps expiring (tab crash, hang) is parked as 'error' after this
BULK_MAX = 5000
SSL = ssl.create_default_context(cafile='/etc/ssl/cert.pem' if Path('/etc/ssl/cert.pem').is_file() else None)
PLAN_BATCH = 200   # profile jobs kept queued at a time when the profile budget is unlimited


class Bad(Exception):
    pass


class NotFound(Bad):
    pass


def text_or_none(v):
    return v if isinstance(v, str) else None


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
    stopped = control.paused_kinds(conn)   # a stage paused on the control strip hands out none of its jobs
    st['stages'] = {'list': 'list' not in stopped, 'profile': 'profile' not in stopped}
    kinds = [k for k in accounts.kinds_for(conn, row, kinds, now) if k not in stopped]
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
    fresh = not job or job['state'] in ('queued', 'leased') and conn.execute(
        'INSERT OR IGNORE INTO pages(job_id, cursor, at, lane, users) VALUES(?,?,?,?,?)',
        (job['id'], b.get('next_cursor') or '', ts, lane, len(b.get('users') or []))).rowcount
    conn.execute('INSERT OR IGNORE INTO seeds(handle, added_at) VALUES(?,?)', (seed, ts))
    if b.get('ig_id'):
        conn.execute('UPDATE seeds SET ig_id=? WHERE handle=?', (str(b['ig_id']), seed))
    pids = []
    users = b.get('users') if isinstance(b.get('users'), list) else []
    for u in users:
        if isinstance(u, dict) and isinstance(u.get('handle'), str) and db.norm_handle(u['handle']):
            u = dict(u, name=text_or_none(u.get('name')), pic_url=text_or_none(u.get('pic_url')),
                     ig_id=u['ig_id'] if isinstance(u.get('ig_id'), (str, int)) and not isinstance(u.get('ig_id'), bool) else None)
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
    for k in ('name', 'bio', 'category', 'pic_url'):
        if k in p:
            p[k] = text_or_none(p[k])
    if not isinstance(p.get('ig_id'), (str, int)) or isinstance(p.get('ig_id'), bool):
        p.pop('ig_id', None)
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
        until = clean_iso(b.get('retry_at')) or iso(datetime.now(timezone.utc) + timedelta(minutes=15))
        db.set_setting(conn, 'cooldown', until)
        fields['cooldown_until'] = until
        if job and job['kind'] == 'list':
            fields['list_cool_until'] = until
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


def clean_iso(v):
    try:
        return iso(utc(v)) if isinstance(v, str) and v else None
    except ValueError:
        return None


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


def ext_heartbeat(conn, q, b):
    b = dict(b, rate=clean_rate(b.get('rate')), cooldown_until=clean_iso(b.get('cooldown_until')))
    lane = accounts.lane_of(q, b)
    db.set_setting(conn, 'ext', dict(b, last_seen=db.now(), lane_id=lane))   # last beat of any lane (older readers)
    today = b.get('today') if isinstance(b.get('today'), dict) else {}

    def text(v, n=500):
        return v[:n] if isinstance(v, str) else None
    fields = {'version': text(b.get('version'), 40), 'state': text(b.get('state'), 20),
              'cooldown_until': b['cooldown_until'], 'rate': json.dumps(b['rate']) if b['rate'] else None,
              'last_error': text(b.get('last_error')), 'activity': text(b.get('activity'), 200), 'text': text(b.get('text'), 200),
              'today': json.dumps({k: count_or_none(today.get(k)) or 0 for k in ('list', 'profile')})}
    if isinstance(b.get('cool'), dict):   # per-bucket cooldowns (3.4+): a list cooldown hands the list to another lane
        fields['list_cool_until'] = clean_iso(b['cool'].get('list'))
    if 'hold' in b:   # 3.4+ report a login wall / security check here too; older builds only via /api/ext/error
        fields['hold'] = b['hold'] if b['hold'] in accounts.HOLDS else None
    row = accounts.touch(conn, lane, accounts.account_from(q, b), **fields)
    if isinstance(b.get('ready'), dict):   # 3.7+: when each clock allows the next request (the control strip shows breaks)
        ready = db.get_setting(conn, 'ext_ready') or {}
        ready[lane] = {k: clean_iso(b['ready'].get(k)) for k in ('list', 'profile')}
        db.set_setting(conn, 'ext_ready', ready)
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
             'tags': tags.get(r['id'], []), 'via': via.get(r['id'], []), 'lists': r['lists'], 'status': r['status'],
             'note': r['note'] or None} for r in rows]


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


def lead_filter(q, status_default=True):
    """Shared by /api/leads, /api/counts, /api/tags (facets) and /api/map. -> (where clauses on p/v/m, args).
    status_default: without a status filter, leave out people marked no."""
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
        if status_default:
            where.append("coalesce(m.status,'')!='no'")
    elif 'all' not in statuses:
        named = [status_in(s) for s in statuses if s != 'none']
        if any(s not in STATUSES for s in named):
            raise Bad('bad status')
        conds = (['m.status IS NULL'] if 'none' in statuses else []) + (['m.status IN ({})'] if named else [])
        within('(' + ' OR '.join(conds) + ')', named)
    text = q.get('q', [''])[0].strip()
    if text:
        where.append("(p.handle LIKE ? ESCAPE '\\' OR p.name LIKE ? ESCAPE '\\' OR p.bio LIKE ? ESCAPE '\\' OR m.note LIKE ? ESCAPE '\\')")
        like = re.sub(r'([\\%_])', r'\\\1', text)
        args += [f'%{like}%'] * 4
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


TIER_RANK = "CASE coalesce(v.tier,'unread') WHEN 'hot' THEN 0 WHEN 'warm' THEN 1 WHEN 'cold' THEN 2 ELSE 3 END"
FIT = {'hot': 'strong', 'warm': 'good', 'cold': 'weak'}   # the UI's name for a tier; anything else is 'unread'
SORTS = {'recent': 'p.updated_at DESC', 'followers': 'p.followers IS NULL, p.followers DESC',
         'connected': 'lists DESC, p.followers IS NULL, p.followers DESC',
         'fit': f'{TIER_RANK}, lists DESC, v.score IS NULL, v.score DESC, p.followers IS NULL, p.followers DESC',
         'score': 'v.score IS NULL, v.score DESC, p.followers DESC'}


def api_leads(conn, q, b):
    where, args = lead_filter(q)
    sort = q.get('sort', ['score'])[0]
    if sort not in SORTS:
        raise Bad('sort must be one of ' + ', '.join(SORTS))
    order = SORTS[sort]
    offset = max(0, qint(q, 'offset') or 0)
    limit = min(500, max(1, qint(q, 'limit') or 50))
    sql_where = ' WHERE ' + ' AND '.join([NOT_ME] + where)
    total = conn.execute(f'SELECT count(*) {PEOPLE_FROM}{sql_where}', args).fetchone()[0]
    rows = conn.execute(f'{LEAD_SQL}{sql_where} ORDER BY {order}, p.id LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
    return {'total': total, 'rows': lead_rows(conn, rows)}


def api_tags(conn, q, b):
    return cached(conn, 'tags', q, lambda: tag_facets(conn, q))


def tag_facets(conn, q):
    """Facets: per (tag, source) the people in the current filtered set (`count`) and overall (`total`). One grouped scan."""
    where, args = lead_filter(q)
    counts = dict(((r[0], r[1]), r[2]) for r in conn.execute(
        f"""WITH f AS MATERIALIZED (SELECT p.id {PEOPLE_FROM} WHERE {' AND '.join([NOT_ME] + where)})
        SELECT t.tag, t.source, count(*) FROM f JOIN tags t ON t.person_id=f.id GROUP BY t.tag, t.source""", args))
    out = [{'tag': r[0], 'grp': r[2], 'source': r[1], 'count': counts.get((r[0], r[1]), 0), 'total': r[3]}  # totals: covering index
           for r in conn.execute('SELECT tag, source, min(grp), count(*) FROM tags GROUP BY tag, source')]
    return sorted(out, key=lambda f: (-f['count'], -f['total'], f['tag'], f['source']))


def api_counts(conn, q, b):
    return cached(conn, 'counts', q, lambda: counts(conn, q))


def counts(conn, q):
    """Tier and status counts inside the shared filter, each ignoring its own dimension (so the choices stay visible).
    none = unmarked, open = everyone but 'no'; total / with_bio: everyone in the database."""
    out = dict.fromkeys(('hot', 'warm', 'cold', 'unread', *STATUSES, 'none', 'open'), 0)
    where, args = lead_filter({k: v for k, v in q.items() if k != 'tier'})
    out.update(conn.execute(f"SELECT coalesce(v.tier,'unread'), count(*) {PEOPLE_FROM} WHERE {' AND '.join([NOT_ME] + where)} "
                            'GROUP BY 1', args).fetchall())
    where, args = lead_filter({k: v for k, v in q.items() if k != 'status'}, status_default=False)
    for status, n in conn.execute(f"SELECT m.status, count(*) {PEOPLE_FROM} WHERE {' AND '.join([NOT_ME] + where)} GROUP BY 1", args):
        out[status or 'none'] = out.get(status or 'none', 0) + n
        out['open'] += n if status != 'no' else 0
    out['total'], out['with_bio'] = conn.execute("SELECT count(*), count(nullif(bio,'')) FROM people").fetchone()
    return out


def person_row(conn, pid):
    row = conn.execute(LEAD_SQL + ' WHERE p.id=?', (pid,)).fetchone()
    if not row:
        raise NotFound('not found')
    return row


def api_person(conn, q, b, pid):
    row = person_row(conn, pid)
    v = conn.execute('SELECT * FROM verdicts WHERE person_id=?', (pid,)).fetchone()
    verdict = dict(v) if v else None
    if verdict:
        try:
            ev = json.loads(verdict.get('evidence') or '[]')
        except ValueError:
            ev = []
        verdict['evidence'] = [x for x in ev if isinstance(x, str)] if isinstance(ev, list) else []
    return dict(lead_rows(conn, [row])[0], edges=edges_of(conn, pid), verdict=verdict, note=row['note'])


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
    if sets:
        touch(conn, pids)   # status and note feed the qualifier: the person is re-qualified on the next batch


def api_mark(conn, q, b, pid):
    """{"status"?: null|STATUS, "note"?: str|null}: an absent key is left alone, null clears (note '' clears too)."""
    person_row(conn, pid)
    status, note = b.get('status', KEEP), b.get('note', KEEP)
    status = status_in(status) if isinstance(status, str) else status
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
    status = status_in(status) if isinstance(status, str) else status
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
    if '~' in handle:  # parked row of an account that gave up this handle: there is no profile to read under it
        raise Bad('this account no longer has a handle to read')
    if not conn.execute("UPDATE jobs SET priority=? WHERE kind='profile' AND handle=? AND state IN ('queued','leased')",
                        (READ_PRIORITY, handle)).rowcount:
        conn.execute("INSERT INTO jobs(kind, handle, priority, created_at) VALUES('profile',?,?,?)", (handle, READ_PRIORITY, db.now()))
    conn.commit()
    return {}


# ---------- map ----------

def data_rev(conn):
    """Changes whenever anything the lead list, tag facets or map show changes (llm_rev: LLM verdicts keep updated_at)."""
    r = conn.execute("SELECT (SELECT max(updated_at) FROM people), (SELECT max(updated_at) FROM verdicts), "
                     "(SELECT count(*) FROM edges), (SELECT count(*) FROM seeds), (SELECT max(updated_at) FROM marks), "
                     "(SELECT count(*) FROM marks), (SELECT count(*) FROM tags), (SELECT count(*) FROM tag_rules), "
                     "(SELECT value FROM settings WHERE key='llm_rev')").fetchone()
    return zlib.crc32('|'.join(map(str, r)).encode())


CACHE = OrderedDict()   # (endpoint, db, query, data_rev) -> response; small LRU for /api/tags and /api/map
CACHE_MAX = 32
CACHE_LOCK = threading.Lock()


def cached(conn, name, q, compute):
    key = (name, CFG['db'], tuple(sorted((k, tuple(v)) for k, v in q.items())), data_rev(conn))
    with CACHE_LOCK:
        if key in CACHE:
            CACHE.move_to_end(key)
            return CACHE[key]
    out = compute()
    with CACHE_LOCK:
        CACHE[key] = out
        while len(CACHE) > CACHE_MAX:
            CACHE.popitem(last=False)
    return out


def clear_caches():
    with CACHE_LOCK:
        CACHE.clear()
    SEED_LINKS[0] = None


SEED_LINKS = [None]   # [(key, computed_at, links)]: replaced whole, never mutated, so readers can't race a writer
SEED_LINKS_TOP = 50
SEED_LINKS_MIN_AGE = 30   # s: while a list is streaming in, recompute the overlap at most this often (same db)


def seed_links(conn):
    key = (CFG['db'], *conn.execute('SELECT count(*), max(rowid) FROM edges').fetchone())
    cached, now = SEED_LINKS[0], datetime.now().timestamp()
    if cached and (cached[0] == key or (cached[0][0] == key[0] and now - cached[1] < SEED_LINKS_MIN_AGE)):
        return cached[2]
    # one pass over the covering index; people linked to a single seed (most of them) never reach Python
    rows = conn.execute('SELECT group_concat(seed, char(10)) FROM edges GROUP BY person_id HAVING min(seed)<max(seed)')
    pairs = Counter(chain.from_iterable(combinations(sorted(set(r[0].split('\n'))), 2) for r in rows))
    top = sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0]))[:SEED_LINKS_TOP]
    links = [{'source': f's:{a}', 'target': f's:{b}', 'shared': n} for (a, b), n in top]
    SEED_LINKS[0] = (key, now, links)
    return links


def api_map(conn, q, b):
    # seed_links has its own (time-throttled) cache and is structural, so it stays outside the per-filter one
    return dict(cached(conn, 'map', q, lambda: map_graph(conn, q)), seed_links=seed_links(conn))


def map_graph(conn, q):
    limit = min(3000, max(10, qint(q, 'limit') or 400))
    where, args = lead_filter(q)
    cond = ' AND '.join(['p.handle NOT IN (SELECT handle FROM seeds)'] + where)
    # one materialized pass over the filtered people; both picks below sort that set (cost follows the filter's size)
    base = ('SELECT p.id, p.handle, p.name, p.pic_file, p.followers, v.tier, v.score, v.reason, m.status, m.note, '
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
                         'p.pic_file, p.followers, v.tier, v.score, m.status, m.note, coalesce(sd.is_me, 0) AS is_me '
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
              'seeds': seeds_of.get(s['pid'], []), 'is_me': bool(s['is_me']), 'pid': s['pid'], 'note': s['note'] or None} for s in seeds]
    nodes += [{'id': f"p:{r['id']}", 'kind': 'lead', 'label': r['handle'], 'handle': r['handle'], 'name': r['name'],
               'tier': r['tier'] or 'unread', 'fit': FIT.get(r['tier'], 'unread'), 'score': r['score'], 'reason': r['reason'],
               'tags': tags.get(r['id'], []),
               'pic': f"/img/{r['id']}" if r['pic_file'] else None, 'degree': r['degree'], 'lists': r['degree'],
               'status': r['status'], 'note': r['note'] or None, 'followers': r['followers'], 'seeds': seeds_of.get(r['id'], [])} for r in people]
    return {'nodes': nodes, 'links': links, 'rev': data_rev(conn)}


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
            'llm': api_llm(conn, q, b),
            'soak': soak(conn, now), 'progress': progress(conn, accts),
            'people_today': conn.execute('SELECT count(*) FROM people WHERE first_seen>=?', (iso(now)[:10],)).fetchone()[0],
            'lists': [dict(r) for r in conn.execute('SELECT seed, direction, state, received, total, updated_at, error FROM lists '
                                                    'ORDER BY updated_at DESC')],
            'queue': dict.fromkeys(('list', 'profile'), 0) | dict(conn.execute(
                "SELECT kind, count(*) FROM jobs WHERE state IN ('queued','leased') GROUP BY kind").fetchall())}


def eta_hours(left, per_hour):
    return round(left / per_hour, 2) if left and per_hour else (0 if not left else None)


def progress(conn, accts):
    """Plain numbers for the Scraper page: what is left, how fast it goes, when it is done."""
    rate = accounts.aggregate_rate(accts)
    lists_left = conn.execute("SELECT coalesce(sum(max(coalesce(total,0)-received,0)),0) FROM lists "
                              "WHERE state NOT IN ('done','private','error')").fetchone()[0]
    bios_left = conn.execute("SELECT count(*) FROM jobs WHERE kind='profile' AND state IN ('queued','leased')").fetchone()[0]
    bio_budget = (db.get_setting(conn, 'budget') or {}).get('profile') or 0
    online = rate.get('online') or 0
    q_left = conn.execute("SELECT count(*) FROM people p JOIN verdicts v ON v.person_id=p.id WHERE coalesce(p.bio,'')!='' "
                          "AND v.model='rules' AND (coalesce(v.prefilter,0)+coalesce(v.score,0))/2>=?",
                          (db.get_setting(conn, 'llm_min') or 0,)).fetchone()[0]
    q_rate = POOL[0].rate() if POOL[0] else None
    return {
        'lists': {'left': lists_left, 'per_hour': rate.get('people_hour'), 'eta_h': eta_hours(lists_left, rate.get('people_hour'))},
        'bios': {'left': bios_left, 'per_day': bio_budget * max(1, online),
                 'eta_h': eta_hours(bios_left, bio_budget * max(1, online) / 24) if online else None},
        'qualify': {'left': q_left, 'per_hour': round(q_rate) if q_rate else None, 'eta_h': eta_hours(q_left, q_rate),
                    'on': bool(db.get_setting(conn, 'qualify')), 'workers': db.get_setting(conn, 'llm_workers'),
                    'keys': len(llm.get().keys) if hasattr(llm, 'get') else None},
    }


def soak(conn, now):
    out = {}
    for label, hours in (('1h', 1), ('6h', 6)):
        since = iso(now - timedelta(hours=hours))
        out[label] = {'pages': conn.execute('SELECT count(*) FROM pages WHERE at>=?', (since,)).fetchone()[0],
                      'people': conn.execute('SELECT count(*) FROM edges WHERE first_seen>=?', (since,)).fetchone()[0],
                      'new_people': conn.execute('SELECT count(*) FROM people WHERE first_seen>=?', (since,)).fetchone()[0],
                      'profiles': conn.execute('SELECT count(*) FROM people WHERE bio_at>=?', (since,)).fetchone()[0]}
    return out


def api_biofetch_get(conn, q, b=None):
    return biofetch.public(conn)


def api_biofetch(conn, q, b):
    """{"on"?, "token"?, "ig_user_id"?, "gap"?}: bios from the Meta Graph API (business_discovery), off by default."""
    try:
        return biofetch.save(conn, b)
    except ValueError as e:
        raise Bad(str(e))


def api_qualify(conn, q, b):
    """{"on"?, "auto"?, "workers"?, "llm_min"?, "bio_min"?}: an absent key is left alone."""
    if 'on' in b and not isinstance(b['on'], bool):
        raise Bad('on must be true or false')
    for key, lo, hi in (('workers', 1, 16), ('llm_min', 0, 100), ('bio_min', 0, 100)):
        if key in b and (not isinstance(b[key], int) or isinstance(b[key], bool) or not lo <= b[key] <= hi):
            raise Bad(f'{key} must be a whole number {lo}-{hi}')
    if 'on' in b:
        db.set_setting(conn, 'qualify', b['on'])
    if 'auto' in b:
        db.set_setting(conn, 'qualify_auto', bool(b['auto']))
    if 'workers' in b:
        db.set_setting(conn, 'llm_workers', b['workers'])
    for key in ('llm_min', 'bio_min'):
        if key in b:
            db.set_setting(conn, key, b[key])
    conn.commit()
    return {'qualify': bool(db.get_setting(conn, 'qualify'))}


def api_llm(conn, q, b):
    """Providers (keys masked), cooldowns, requests today, last error; plus the Laya sidecar and the pool settings."""
    out = llm.get().status()
    out.update(workers=db.get_setting(conn, 'llm_workers'), llm_min=db.get_setting(conn, 'llm_min'),
               bio_min=db.get_setting(conn, 'bio_min'), laya={'url': laya.URL, 'up': laya.last_known()},
               config=str(llm.CONFIG.relative_to(ROOT)) if llm.CONFIG.is_relative_to(ROOT) else str(llm.CONFIG))
    auto = llm.read_config().get('auto_models') or {}
    out['auto_models'] = {k: auto.get(k) for k in ('stealth', 'free', 'at', 'error', 'new_stealth')}
    counts = {}
    for p in out['providers']:
        counts[p.get('state') or 'untested'] = counts.get(p.get('state') or 'untested', 0) + 1
    out['summary'] = counts   # e.g. {'ok': 2, 'spent': 2, 'error': 1}: spent keys are not broken, they return at 00:00 UTC
    out['verdicts'] = dict(conn.execute("SELECT CASE WHEN model IN ('rules','error') THEN model ELSE 'llm' END, count(*) FROM verdicts "
                                        'GROUP BY 1').fetchall())
    return out


def api_llm_health(conn, q, b):
    """Probes now: is the local OpenRouter proxy listening, does the Laya sidecar answer /health."""
    url = urlparse(llm.get().proxy)
    try:
        socket.create_connection((url.hostname, url.port or 80), timeout=1).close()
        proxy = True
    except OSError:
        proxy = False
    laya.reset()
    return {'proxy': {'url': f'{url.scheme}://{url.netloc}', 'up': proxy}, 'laya': {'url': laya.URL, 'up': laya.available()}}


def api_llm_key_add(conn, q, b):
    try:
        return {'id': llm.add_key(b.get('key'))}
    except ValueError as e:
        raise Bad(str(e)) from None


def api_llm_key_remove(conn, q, b, pid):
    try:
        llm.remove_key(pid)
    except LookupError:
        raise NotFound('no such key') from None
    except ValueError as e:
        raise Bad(str(e)) from None
    return {}


def api_llm_key_test(conn, q, b, pid):
    try:
        return llm.get().test(pid)
    except LookupError:
        raise NotFound('no such key') from None


def api_llm_models_refresh(conn, q, b):
    rec = llm.refresh_models(force=True)
    st = llm.get().status()
    return {'models': st['models'], 'auto': {k: rec.get(k) for k in ('stealth', 'free', 'at', 'error', 'new_stealth')}}


def api_llm_models(conn, q, b):
    try:
        llm.set_models(b.get('models'), b.get('daily_limit'))
    except ValueError as e:
        raise Bad(str(e)) from None
    st = llm.get().status()
    return {'models': st['models'], 'daily_limit': st['daily_limit']}


SNOWBALL_MAX = 50


def api_snowball(conn, q, b):
    """Opt-in: queue the `following` lists of good/client leads as new seeds (their people then get the same network signals)."""
    min_status = status_in(b.get('min_status', 'interested'))
    if min_status not in ('interested', 'client'):
        raise Bad('min_status must be interested or client')
    limit = b.get('limit', SNOWBALL_MAX)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise Bad('limit must be 1-500')
    statuses = POSITIVE if min_status == 'interested' else ('client',)
    rows = conn.execute(f"SELECT p.handle FROM people p JOIN marks m ON m.person_id=p.id WHERE m.status IN ({','.join('?' * len(statuses))}) "
                        "AND instr(p.handle, '~')=0 AND coalesce(p.is_private,0)=0 "
                        "AND NOT EXISTS (SELECT 1 FROM lists l WHERE l.seed=p.handle AND l.direction='following') "
                        "ORDER BY m.updated_at DESC LIMIT ?", (*statuses, limit)).fetchall()
    seeds = [r[0] for r in rows if db.queue_list(conn, r[0], 'following')]
    conn.commit()
    return {'queued': len(seeds), 'seeds': seeds}


def api_seeds(conn, q, b):
    dirs = [d for d in b.get('directions') or ['followers', 'following'] if d in ('followers', 'following')]
    added = [(h, d) for h in map(db.norm_handle, b.get('handles') or []) if h for d in dirs if db.queue_list(conn, h, d)]
    conn.commit()
    return {'queued': len(added)}


def api_pause(conn, q, b):
    db.set_setting(conn, 'paused', bool(b.get('paused')))
    conn.commit()
    return {}


def ai_left(conn):
    return conn.execute("SELECT count(*) FROM people p JOIN verdicts v ON v.person_id=p.id WHERE coalesce(p.bio,'')!='' "
                        "AND v.model='rules' AND (coalesce(v.prefilter,0)+coalesce(v.score,0))/2>=?",
                        (db.get_setting(conn, 'llm_min') or 0,)).fetchone()[0]


def api_control(conn, q, b):
    """What each stage (lists, bios, AI) and each account is doing right now, in plain sentences."""
    return control.snapshot(conn, ai_left(conn))


def api_control_set(conn, q, b):
    """{"stage": lists|bios|ai|all, "action": pause|resume} or {"account": lane, "action": ...} → the new snapshot."""
    try:
        control.apply(conn, b)
    except LookupError:
        raise NotFound('no such account') from None
    return api_control(conn, q, b)


def api_budget(conn, q, b):
    budget = db.get_setting(conn, 'budget')
    budget.update({k: max(0, min(cap, int(b[k]))) for k, cap in accounts.BUDGET_MAX.items() if b.get(k) is not None})
    db.set_setting(conn, 'budget', budget)
    conn.commit()
    return {}


def api_accounts(conn, q, b):
    now = datetime.now(timezone.utc)
    accts = accounts.listing(conn, now)
    return {'accounts': accts, 'alerts': accounts.alerts(conn, now, accts), 'rate': accounts.aggregate_rate(accts),
            'main_list_share': float(db.get_setting(conn, 'main_list_share') or 0)}


def api_account_settings(conn, q, b):
    """{"main_list_share": 0-1}: the share of list pages Michael's own account may take (0 = bios only)."""
    v = b.get('main_list_share')
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 1:
        raise Bad('main_list_share must be a number 0-1')
    db.set_setting(conn, 'main_list_share', round(float(v), 2))
    conn.commit()
    return {'main_list_share': round(float(v), 2)}


def api_account_edit(conn, q, b, lane):
    conn.execute('BEGIN IMMEDIATE')
    try:
        row = accounts.edit(conn, lane, b)
    except LookupError:
        conn.rollback()
        raise NotFound('no such account') from None
    except ValueError as e:
        conn.rollback()
        raise Bad(str(e)) from None
    conn.commit()
    return {'account': accounts.out(conn, row, datetime.now(timezone.utc))}


def api_account_remove(conn, q, b, lane):
    conn.execute('BEGIN IMMEDIATE')
    n = accounts.remove(conn, lane)
    conn.commit()
    return {'removed': n}


def api_setup(conn, q, b):
    """What the add-account wizard shows: where the unpacked extension lives and which id Chrome gives it."""
    manifest = json.loads((ROOT / 'extension' / 'manifest.json').read_text())
    return {'repo': str(ROOT), 'extension_path': str(ROOT / 'extension'), 'extension_id': EXT_ORIGIN.split('//')[1],
            'extension_version': manifest.get('version'), 'server': f"http://127.0.0.1:{CFG['port']}",
            'lanes': conn.execute('SELECT count(*) FROM accounts').fetchone()[0]}


LANE = r'(?P<lane>[A-Za-z0-9_-]{1,64})'   # named groups stay text; unnamed (\d+) groups become ints
KEY = r'(?P<key>proxy|[0-9a-f]{10})'
ROUTES = [
    ('GET', r'/api/accounts', api_accounts), ('POST', rf'/api/accounts/{LANE}', api_account_edit),
    ('POST', rf'/api/accounts/{LANE}/remove', api_account_remove), ('GET', r'/api/setup', api_setup),
    ('POST', r'/api/settings/accounts', api_account_settings),
    ('GET', r'/api/ext/next', ext_next), ('POST', r'/api/ext/list-page', ext_list_page),
    ('POST', r'/api/ext/profile', ext_profile), ('POST', r'/api/ext/error', ext_error),
    ('POST', r'/api/ext/heartbeat', ext_heartbeat),
    ('GET', r'/api/control', api_control), ('POST', r'/api/control', api_control_set),
    ('GET', r'/api/ext/control', api_control), ('POST', r'/api/ext/control', api_control_set),
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
    ('POST', r'/api/scraper/budget', api_budget), ('POST', r'/api/scraper/snowball', api_snowball),
    ('POST', r'/api/settings/qualify', api_qualify),
    ('GET', r'/api/settings/biofetch', api_biofetch_get), ('POST', r'/api/settings/biofetch', api_biofetch),
    ('GET', r'/api/llm', api_llm), ('GET', r'/api/llm/health', api_llm_health), ('POST', r'/api/llm/keys', api_llm_key_add),
    ('POST', rf'/api/llm/keys/{KEY}/remove', api_llm_key_remove), ('POST', rf'/api/llm/keys/{KEY}/test', api_llm_key_test),
    ('POST', r'/api/llm/models', api_llm_models), ('POST', r'/api/llm/models/refresh', api_llm_models_refresh),
]
import qual_api  # noqa: E402  Qualification page endpoints (web/frontend module)
ROUTES += qual_api.routes(sys.modules[__name__])


class Server(ThreadingHTTPServer):
    # socketserver's default listen backlog is 5: a browser opening ~10 asset connections at once overflowed it and
    # macOS answered the extra SYNs with a reset (ERR_CONNECTION_RESET / ERR_SOCKET_NOT_CONNECTED). curl, one at a time, never did.
    request_queue_size = 256
    daemon_threads = True
    allow_reuse_address = True


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
        self.drain()
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

    def drain(self):
        """Read a request body nobody consumed (early 403/404): closing a socket with unread bytes sends a TCP reset,
        which the browser reports as ERR_CONNECTION_RESET instead of the response."""
        if getattr(self, '_drained', False):
            return
        self._drained = True
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        if 0 < n <= 64 * 1024 * 1024:
            self.rfile.read(n)

    def route(self, method):
        self._drained = False
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
                named = set(match.re.groupindex.values())
                args = [g if i in named else int(g) for i, g in enumerate(match.groups(), 1)]
                return self.api(fn, parse_qs(url.query), args, method == 'POST' or '/ext/' in url.path)
        if method == 'GET' and url.path.startswith('/img/'):
            return self.image(url.path[5:])
        if method == 'GET' and not url.path.startswith('/api/'):
            return self.static(url.path)
        self.send(404, {'ok': False, 'error': 'not found'})

    def api(self, fn, q, args, with_ok):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n < 0 or n > 64 * 1024 * 1024:
                raise Bad('bad Content-Length')
            self._drained = True
            body = json.loads(self.rfile.read(n) or b'{}') if n else {}
            if not isinstance(body, dict):
                raise Bad('body must be a JSON object')
            conn = db.connect(CFG['db'])
            try:
                out = fn(conn, q, body, *args)
            finally:
                conn.close()
        except NotFound as e:
            return self.send(404, {'ok': False, 'error': str(e)})
        except (Bad, ValueError, KeyError, TypeError, OverflowError) as e:
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

def network_context(conn, pids, me=None):
    """Network signals per person for the prefilter and the LLM packet:
    seeds + direction, lists count, link to Michael (is_me seed), seed yield learned from marks (smoothed good+client / marked),
    and how many of the person's seeds are people Michael marked good/client."""
    pids = list(dict.fromkeys(pids))
    if not pids:
        return {}
    me = me if me is not None else me_handle(conn)
    yields = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT e.seed, count(DISTINCT CASE WHEN m.status IN {POSITIVE_SQL} THEN e.person_id END), count(DISTINCT e.person_id) "
        "FROM edges e JOIN marks m ON m.person_id=e.person_id WHERE m.status IS NOT NULL GROUP BY e.seed")}
    good_handles = {r[0] for r in conn.execute("SELECT p.handle FROM people p JOIN marks m ON m.person_id=p.id "
                                               "WHERE m.status IN " + POSITIVE_SQL)}
    out = {p: {'seeds': [], 'lists': 0, 'me': None, 'seed_yield': None, 'seed_marked': 0, 'client_seeds': 0} for p in pids}
    for chunk in chunks(pids):
        for e in conn.execute(f"SELECT person_id, seed, direction FROM edges WHERE person_id IN ({','.join('?' * len(chunk))}) "
                              'ORDER BY seed, direction', chunk):
            out[e['person_id']]['seeds'].append((e['seed'], e['direction']))
    for pid, n in out.items():
        seeds = {s for s, _ in n['seeds']}
        mine = {d for s, d in n['seeds'] if me and s == me}
        others = seeds - ({me} if me else set())
        n['lists'] = len(seeds)
        n['me'] = 'mutual' if len(mine) == 2 else 'follows' if 'followers' in mine else 'followed' if mine else None
        best = None
        for s in others:
            g, m = yields.get(s, (0, 0))
            if m:
                y = (g + 1) / (m + 4)   # prior 1 good in 4: a seed needs several marks before it moves anyone much
                if best is None or y > best[0]:
                    best = (y, m)
        if best:
            n['seed_yield'], n['seed_marked'] = round(best[0], 3), best[1]
        n['client_seeds'] = len(others & good_handles)
    return out


def laya_row(conn, pid):
    r = conn.execute('SELECT answers, fit FROM laya WHERE person_id=?', (pid,)).fetchone()
    if not r:
        return None, None
    try:
        return json.loads(r['answers'] or '{}'), r['fit']
    except ValueError:
        return None, None


def with_owner(conn, p):
    """Adds Michael's own judgement to a person dict: status, note and the tags he set by hand. The qualifier puts these in
    the prompt and in input_hash, so changing any of them re-runs the model for that person."""
    m = conn.execute('SELECT status, note FROM marks WHERE person_id=?', (p['id'],)).fetchone()
    p['status'], p['note'] = (m['status'], m['note']) if m else (None, None)
    p['manual_tags'] = sorted(r[0] for r in conn.execute("SELECT tag FROM tags WHERE person_id=? AND source='manual'", (p['id'],)))
    return p


def requalify(conn, p, me, net=None):
    with_owner(conn, p)
    edges = edges_of(conn, p['id'])
    answers, lfit = laya_row(conn, p['id'])
    pre = qualify.prefilter(p, sorted({e['seed'] for e in edges}), net, lfit)
    old = conn.execute('SELECT model, input_hash FROM verdicts WHERE person_id=?', (p['id'],)).fetchone()
    keep_llm = old and old['input_hash'] and old['input_hash'] == qualify.input_hash(p, edges)
    if not keep_llm:  # the LLM's extra auto tags stay as long as its verdict does
        conn.execute("DELETE FROM tags WHERE person_id=? AND source='auto'", (p['id'],))
    auto = qualify.rule_tags(p, edges, me)
    if answers:  # Laya tags only when it is very sure on its own (a rule tag that agrees is already there)
        have = {t for t, _ in auto} | {r[0] for r in conn.execute('SELECT tag FROM tags WHERE person_id=?', (p['id'],))}
        auto = auto + [(t, getattr(qualify, 'TAXONOMY', {}).get(t, 'signal')) for t in laya.tags(answers, have)]
    conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,?,'auto')", [(p['id'], t, g) for t, g in auto])
    if keep_llm:
        conn.execute('UPDATE verdicts SET prefilter=?, updated_at=? WHERE person_id=?', (pre, p['updated_at'], p['id']))
        return
    tags = [tuple(r) for r in conn.execute('SELECT tag, grp FROM tags WHERE person_id=?', (p['id'],))]
    v = qualify.rule_verdict(p, tags, net)
    conn.execute("INSERT OR REPLACE INTO verdicts(person_id, prefilter, score, tier, role, reason, model, input_hash, updated_at) "
                 "VALUES(?,?,?,?,?,?,'rules',NULL,?)", (p['id'], pre, v['score'], v['tier'], v['role'], v['reason'], p['updated_at']))


def qualify_batch(conn, limit=200):
    rows = conn.execute('SELECT p.* FROM people p LEFT JOIN verdicts v ON v.person_id=p.id '
                        'WHERE v.person_id IS NULL OR v.updated_at < p.updated_at LIMIT ?', (limit,)).fetchall()
    me = me_handle(conn)
    rules.sync(conn, [r['id'] for r in rows])  # rule tags first, so the verdict sees them
    nets = network_context(conn, [r['id'] for r in rows], me) if rows else {}
    for r in rows:
        try:
            requalify(conn, dict(r), me, nets.get(r['id']))
        except Exception:
            traceback.print_exc()
            conn.execute("INSERT OR REPLACE INTO verdicts(person_id, tier, model, updated_at) VALUES(?,'unread','error',?)",
                         (r['id'], r['updated_at']))
    conn.commit()
    return len(rows)


# ---------- Laya (optional soft signal) ----------

LAYA_BATCH = 64


def laya_hash(bio):
    """What a Laya answer was computed from: a bio edit of the same length still counts as a change."""
    return f"bio:{zlib.crc32(bio.encode()):08x}" if bio else 'nobio'


def laya_step(conn):
    """Score people without a (current) Laya answer; bios first, list-only people too. Silently idle when the sidecar is down."""
    if not laya.available():
        return False
    conn.create_function('laya_hash', 1, laya_hash, deterministic=True)
    rows = conn.execute("""SELECT p.id, p.handle, p.name, p.bio, p.category, p.website, p.followers, l.input_hash AS lh
        FROM people p LEFT JOIN laya l ON l.person_id=p.id LEFT JOIN verdicts v ON v.person_id=p.id
        WHERE instr(p.handle, '~')=0 AND p.handle NOT IN (SELECT handle FROM seeds WHERE is_me=1)
          AND (l.person_id IS NULL OR l.input_hash IS NOT laya_hash(p.bio))
        ORDER BY coalesce(p.bio,'')='' , v.prefilter DESC, p.id LIMIT ?""", (LAYA_BATCH,)).fetchall()
    if not rows:
        return False
    answers = laya.decide([dict(r) for r in rows])   # no DB lock is held during the call
    if not answers:
        return False
    ts = db.now()
    done = [r for r in rows if r['id'] in answers]
    conn.executemany('INSERT OR REPLACE INTO laya VALUES(?,?,?,?,?)',
                     [(r['id'], laya_hash(r['bio']), json.dumps(answers[r['id']]),
                       laya.fit(answers[r['id']]), ts) for r in done])
    # the qualify batch folds the new signal into prefilter (and very sure tags) on its next pass
    conn.executemany("UPDATE verdicts SET updated_at='' WHERE person_id=?", [(r['id'],) for r in done])
    conn.commit()
    return True


# ---------- LLM stage: bounded worker pool, few-shot from marks ----------

FEWSHOT_MAX = 8
FEWSHOT_CHANGE = 5    # re-run LLM verdicts when the good/client/no marks moved by this many (or 20 %)
FEWSHOT_RERUN = 35    # ... but only those at or near warm (45): a new example set will not lift a clear cold one


def fewshot(conn):
    """The few-shot example set, frozen until Michael's marks change a lot; then older LLM verdicts are re-run."""
    n = conn.execute("SELECT count(*) FROM marks WHERE status IN ('interested','talking','client','no')").fetchone()[0]
    cur = db.get_setting(conn, 'fewshot') or {}
    if cur and abs(n - cur.get('n', 0)) < max(FEWSHOT_CHANGE, cur.get('n', 0) // 5):
        return cur.get('examples') or []
    ex = []
    for label, statuses in (('good', POSITIVE_SQL), ('no', "('no')")):
        for r in conn.execute(f"SELECT p.handle, p.name, p.bio FROM marks m JOIN people p ON p.id=m.person_id WHERE m.status IN {statuses} "
                              f"AND coalesce(p.bio,'')!='' ORDER BY m.updated_at DESC LIMIT ?", (FEWSHOT_MAX,)):
            ex.append({'handle': r['handle'], 'name': r['name'], 'bio': (r['bio'] or '')[:200], 'label': label})
    version = qualify.prompt_version(ex) if hasattr(qualify, 'prompt_version') else None
    if cur and version and version != cur.get('version'):
        conn.execute("UPDATE verdicts SET model='rules' WHERE model NOT IN ('rules','error') AND prompt IS NOT ? AND coalesce(score,0)>=?",
                     (version, FEWSHOT_RERUN))
    db.set_setting(conn, 'fewshot', {'n': n, 'examples': ex, 'version': version})
    conn.commit()
    return ex


def llm_candidates(conn, limit, exclude):
    """Top candidates by combined signal (prefilter = list data + network + Laya; score = rules on the bio)."""
    held = list(exclude)[:900]
    return conn.execute("SELECT p.* FROM people p JOIN verdicts v ON v.person_id=p.id WHERE coalesce(p.bio,'')!='' "
                        "AND v.model='rules' AND v.updated_at=p.updated_at AND (coalesce(v.prefilter,0)+coalesce(v.score,0))/2>=? "
                        f"AND p.id NOT IN ({','.join('?' * len(held))}) "
                        "ORDER BY coalesce(v.prefilter,0)+coalesce(v.score,0) DESC, p.id LIMIT ?",
                        (db.get_setting(conn, 'llm_min'), *held, limit)).fetchall()


def run_llm(conn, rows, skip):
    """One model round for these people (no DB transaction is open during the call). -> number of verdicts written."""
    rows = [with_owner(conn, dict(r)) for r in rows]
    me = me_handle(conn)
    nets = network_context(conn, [p['id'] for p in rows], me)
    items = [{'person': p, 'edges': edges_of(conn, p['id']), 'net': nets.get(p['id']),
              'tags': [tuple(r) for r in conn.execute('SELECT tag, grp FROM tags WHERE person_id=?', (p['id'],))]} for p in rows]
    examples = fewshot(conn)
    conn.commit()
    try:
        many = getattr(qualify, 'llm_verdicts', None)
        vs = many(items, examples) if many else [qualify.llm_verdict(i['person'], i['tags'], i['edges']) for i in items]
    except Exception:  # never let one bad reply spin the worker on the same rows: fall back like 'no model'
        traceback.print_exc()
        vs = [None] * len(items)
    wrote = 0
    for it, v in zip(items, vs):
        p = it['person']
        if v is None:
            skip[p['id']] = datetime.now().timestamp() + 1800
            continue
        if conn.execute('UPDATE verdicts SET score=?, tier=?, role=?, reason=?, model=?, input_hash=?, prompt=?, evidence=? '
                        'WHERE person_id=? AND updated_at=?',
                        (v['score'], v['tier'], v['role'], v['reason'], v.get('model') or 'llm', qualify.input_hash(p, it['edges']),
                         v.get('prompt'), json.dumps(v.get('evidence') or []), p['id'], p['updated_at'])).rowcount:
            conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,?,'auto')", [(p['id'], t, g) for t, g in v.get('tags') or []])
            wrote += 1
    if wrote:
        db.set_setting(conn, 'llm_rev', (db.get_setting(conn, 'llm_rev') or 0) + 1)
    conn.commit()
    return wrote


def _expire(skip):
    t = datetime.now().timestamp()
    for k in [k for k, until in list(skip.items()) if until <= t]:
        skip.pop(k, None)


def llm_step(conn, skip):
    """Synchronous single step (tests, tools): one person. The server itself runs LLMPool."""
    if not db.get_setting(conn, 'qualify'):
        return None
    _expire(skip)
    rows = llm_candidates(conn, 1, skip)
    if not rows:
        return None
    return run_llm(conn, rows, skip) > 0


class LLMPool:
    """Bounded pool: at most `llm_workers` (setting, default 4) model calls in flight, each on its own DB connection.
    The dispatcher only reads; results are written by the worker that got them. HTTP handlers and ingest never wait on it."""

    def __init__(self, batch=None):
        self.lock = threading.Lock()
        self.inflight = set()
        self.running = 0
        self.skip = {}
        self.batch = batch
        self.done = collections.deque(maxlen=2000)   # (time, verdicts written) for the ETA

    def step(self, conn):
        if not db.get_setting(conn, 'qualify'):
            return False
        workers = max(1, min(16, int(db.get_setting(conn, 'llm_workers') or 4)))
        batch = self.batch or getattr(qualify, 'LLM_BATCH', 1)
        with self.lock:
            _expire(self.skip)
            free = workers - self.running
            exclude = set(self.inflight) | set(self.skip)
        if free <= 0:
            return False
        rows = llm_candidates(conn, free * batch, exclude)
        if not rows:
            return False
        groups = [rows[i:i + batch] for i in range(0, len(rows), batch)][:free]
        with self.lock:
            for g in groups:
                self.running += 1
                self.inflight.update(r['id'] for r in g)
        for g in groups:
            threading.Thread(target=self._work, args=(g,), daemon=True).start()
        return True

    def rate(self, window=900):
        """Verdicts per hour over the last `window` seconds (None until there is data)."""
        now = time.time()
        recent = [(t, n) for t, n in list(self.done) if now - t <= window]
        if not recent:
            return None
        span = max(60, now - recent[0][0])
        return sum(n for _, n in recent) * 3600 / span

    def _work(self, rows):
        conn = db.connect(CFG['db'])
        try:
            n = run_llm(conn, rows, self.skip)
            if n:
                self.done.append((time.time(), n))
        except Exception:
            traceback.print_exc()
        finally:
            conn.close()
            with self.lock:
                self.running -= 1
                self.inflight.difference_update(r['id'] for r in rows)

    def idle(self):
        with self.lock:
            return self.running == 0


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
    """Likely fit first (prefilter already weighs lists, seed yield, links to Michael and clients, Laya), lists count breaks
    ties; always below READ_PRIORITY (a read Michael asked for goes first)."""
    return max(0, min(100, prefilter or 0)) * 10 + min(lists or 0, 9)


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


EARLY_LISTS = 2   # while lists are still collecting, only people already in this many lists get a bio read


def plan_profiles(conn):
    """Keep the profile queue topped up to the bio budget of the lanes that read bios. Qualification on: everyone above
    `bio_min`, best prefilter first. Still collecting (qualify off, auto on): only the people already in several lists."""
    auto_qualify(conn)
    # Bio reads are scraping, not AI: they run whether Qualify is on or off. While lists are still collecting,
    # only people already in several lists get one.
    early = not db.get_setting(conn, 'qualify') and bool(conn.execute(
        "SELECT 1 FROM lists WHERE state IN ('queued','running') LIMIT 1").fetchone())
    now = datetime.now(timezone.utc)
    accts = [a for a in accounts.listing(conn, now) if a['healthy'] and a['role'] in ('bios', 'both')]
    if accts:   # every lane that reads bios brings its own daily budget (0 = no daily number)
        caps = [(a['budget']['profile'], a['today']['profile']) for a in accts]
    else:
        ext = db.get_setting(conn, 'ext') or {}
        used = (ext.get('today') or {}).get('profile', 0) if (ext.get('last_seen') or '').startswith(now.date().isoformat()) else 0
        caps = [(db.get_setting(conn, 'budget')['profile'], used)]
    room = PLAN_BATCH if any(cap == 0 for cap, _ in caps) else min(PLAN_BATCH, sum(max(0, cap - used) for cap, used in caps))
    active = conn.execute("SELECT count(*) FROM jobs WHERE kind='profile' AND state IN ('queued','leased')").fetchone()[0]
    need = room - active
    if need <= 0:
        return 0
    rows = conn.execute(f"""SELECT * FROM (SELECT p.handle, v.prefilter, {LISTS} AS n
        FROM people p JOIN verdicts v ON v.person_id=p.id
        WHERE p.bio_at IS NULL AND coalesce(p.is_private,0)=0 AND v.prefilter>=?
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind='profile' AND j.handle=p.handle)
          AND p.handle NOT IN (SELECT handle FROM seeds) AND instr(p.handle, '~')=0
          AND p.id NOT IN (SELECT person_id FROM marks WHERE status='no')) WHERE n>=?
        ORDER BY prefilter DESC, n DESC, handle LIMIT ?""", (db.get_setting(conn, 'bio_min'), EARLY_LISTS if early else 0, need)).fetchall()
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


POOL = [None]


def repair_step(conn):
    """Every 15 min: lists that stopped short or lost their job get queued again (db.repair_lists)."""
    out = db.repair_lists(conn)
    conn.commit()
    if any(out.values()):
        print('repair_lists', out, flush=True)
    return False


def models_step(conn):
    """Once a day: pick up OpenRouter's free and stealth models (llm.refresh_models; stealth ones go first)."""
    if not os.environ.get('FL_NO_ORSLOT'):   # tests stay offline
        llm.refresh_models()
        # hourly: probe providers that are not known-good, so Settings shows ok / spent / broken instead of 'untested'
        pool = llm.get()
        for p in pool.status()['providers']:
            if p['state'] not in ('ok', 'spent', 'broken'):
                pool.test(p['id'])
    return False


def start_workers(stop):
    pool = POOL[0] = LLMPool()
    loops = [(repair_step, 900, 900), (models_step, 3600, 3600), (qualify_batch, 0, 5), (pool.step, 1, 5), (laya_step, 0.2, 30), (plan_profiles, 15, 15), (pfp_step, 0.4, 10), (biofetch.step, 0.5, 10)]
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
    Server(('127.0.0.1', a.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
