"""Bios without the Instagram session: the official Instagram Graph API `business_discovery` field.

Off by default. Needs a Meta app, an Instagram professional account linked to a Facebook Page, and a user access token
with instagram_basic + pages_read_engagement (Settings -> Bios via Meta API). One Graph call per handle; only business and
creator accounts answer (personal accounts return an error and are marked tried, the extension still reads them).

Pacing: at most one call per `gap` seconds (default 1.5 s, about 40/min). Meta's published budget is
4800 x impressions per 24 h for the professional account; the worker slows down when the `x-business-use-case-usage`
header passes 75 % and stops on any throttling code, doubling the pause 10 min -> 4 h. It never touches the IG session.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import db
import control

GRAPH = 'https://graph.facebook.com/v23.0/'
FIELDS = 'username,name,biography,website,followers_count,follows_count,media_count,profile_picture_url,id'
THROTTLE = {4, 17, 32, 613, 80002, 80001}
NOT_FOUND = {110, 100}  # 2207013 etc.: not a business/creator account, or no such user
DEFAULT = {'on': False, 'token': '', 'ig_user_id': '', 'gap': 1.5}


def settings(conn):
    return {**DEFAULT, **(db.get_setting(conn, 'biofetch') or {})}


def state(conn):
    return db.get_setting(conn, 'biofetch_state') or {}


def public(conn):
    s, st = settings(conn), state(conn)
    return {'on': bool(s['on']), 'ready': bool(s['token'] and s['ig_user_id']), 'ig_user_id': s['ig_user_id'],
            'token': ('…' + s['token'][-4:]) if s['token'] else '', 'gap': s['gap'], **st}


def save(conn, b):
    s = settings(conn)
    if 'on' in b:
        if not isinstance(b['on'], bool):
            raise ValueError('on must be true or false')
        s['on'] = b['on']
    for k in ('token', 'ig_user_id'):
        if k in b:
            if not isinstance(b[k], str):
                raise ValueError(f'{k} must be text')
            s[k] = b[k].strip()
    if 'gap' in b:
        if not isinstance(b['gap'], (int, float)) or isinstance(b['gap'], bool) or not 1.2 <= b['gap'] <= 600:
            raise ValueError('gap must be 1.2-600 seconds')
        s['gap'] = float(b['gap'])
    db.set_setting(conn, 'biofetch', s)
    return public(conn)


def _get(url, timeout=20):
    """-> (status, json body, headers). Network errors -> (0, {}, {})."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
            return r.status, json.loads(r.read() or b'{}'), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b'{}')
        except ValueError:
            body = {}
        return e.code, body, dict(e.headers or {})
    except (OSError, ValueError):
        return 0, {}, {}


FETCH = [_get]  # tests swap this


def usage_pct(headers):
    """Highest percentage in x-business-use-case-usage / x-app-usage, 0 when absent."""
    top = 0
    for k, v in headers.items():
        if k.lower() not in ('x-business-use-case-usage', 'x-app-usage'):
            continue
        try:
            data = json.loads(v)
        except ValueError:
            continue
        items = [x for lst in data.values() for x in lst] if k.lower() == 'x-business-use-case-usage' else [data]
        for it in items:
            for f in ('call_count', 'total_cputime', 'total_time'):
                if isinstance(it.get(f), (int, float)):
                    top = max(top, it[f])
    return top


def pick(conn):
    return conn.execute("""SELECT p.handle FROM people p LEFT JOIN verdicts v ON v.person_id=p.id
        WHERE p.bio_at IS NULL AND p.bd_at IS NULL AND coalesce(p.is_private,0)=0 AND instr(p.handle,'~')=0
          AND p.handle NOT IN (SELECT handle FROM seeds)
        ORDER BY coalesce(v.prefilter,0) DESC, p.id LIMIT 1""").fetchone()


def _cool(conn, st, why, now):
    mins = min(240, max(10, (st.get('backoff_min') or 5) * 2))
    st.update(backoff_min=mins, until=(now + timedelta(minutes=mins)).isoformat(timespec='seconds'), last_error=why)
    db.set_setting(conn, 'biofetch_state', st)


def step(conn, now=None):
    """One handle. Returns True when it made a call (the worker then waits `gap`)."""
    if control.stage_paused(conn, 'bios'):
        return False
    s, st = settings(conn), state(conn)
    now = now or datetime.now(timezone.utc)
    if not (s['on'] and s['token'] and s['ig_user_id']):
        return False
    if st.get('until') and datetime.fromisoformat(st['until']) > now:
        return False
    last = st.get('last_at')
    if last and (now - datetime.fromisoformat(last)).total_seconds() < s['gap']:
        return False
    row = pick(conn)
    if not row:
        return False
    h = row['handle']
    q = urllib.parse.urlencode({'fields': f'business_discovery.username({h}){{{FIELDS}}}', 'access_token': s['token']})
    status, body, headers = FETCH[0](GRAPH + urllib.parse.quote(s['ig_user_id']) + '?' + q)
    ts = db.now()
    day = now.date().isoformat()
    if st.get('day') != day:
        st.update(day=day, hits=0, misses=0, calls=0)
    st['calls'] = st.get('calls', 0) + 1
    st['last_at'] = now.isoformat(timespec='seconds')
    err = body.get('error') if isinstance(body, dict) else None
    bd = body.get('business_discovery') if isinstance(body, dict) else None
    if status == 200 and isinstance(bd, dict):
        p = {'handle': h, 'bio': bd.get('biography') or '', 'name': bd.get('name'), 'followers': bd.get('followers_count'),
             'following': bd.get('follows_count'), 'posts': bd.get('media_count'), 'is_business': 1,
             'bio_at': ts, 'bio_src': 'meta_bd', 'pic_url': bd.get('profile_picture_url')}
        w = bd.get('website')
        if isinstance(w, str) and w.strip().lower().startswith(('http://', 'https://')):
            p['website'] = w.strip()
        pid = db.upsert_person(conn, p, ts)
        conn.execute('UPDATE people SET bd_at=? WHERE id=?', (ts, pid))
        conn.execute("UPDATE jobs SET state='done', leased_until=NULL WHERE kind='profile' AND handle=? "
                     "AND state IN ('queued','leased')", (h,))
        try:
            import rules
            rules.sync(conn, [pid])
        except Exception:  # tagging is best effort here; the next rules pass catches up
            pass
        st['hits'] = st.get('hits', 0) + 1
        st['backoff_min'] = 0
        st.pop('until', None)
        if usage_pct(headers) >= 75:
            _cool(conn, st, 'Meta usage above 75 %', now)
    elif err and (err.get('code') in THROTTLE or status == 429):
        _cool(conn, st, f"Meta limit ({err.get('code')})", now)
    elif err and err.get('code') == 190:
        s['on'] = False
        db.set_setting(conn, 'biofetch', s)
        st['last_error'] = 'Token expired or invalid: switched off'
    elif err and err.get('code') in NOT_FOUND:
        conn.execute('UPDATE people SET bd_at=? WHERE handle=?', (ts, h))
        st['misses'] = st.get('misses', 0) + 1
    else:
        _cool(conn, st, f'HTTP {status}' + (f" ({err.get('message', '')[:120]})" if err else ''), now)
    db.set_setting(conn, 'biofetch_state', st)
    conn.commit()
    return True
