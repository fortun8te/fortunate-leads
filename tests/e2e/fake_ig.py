#!/usr/bin/env python3
"""Fake Instagram web API for the end-to-end simulator (stdlib only).

Serves the endpoints the extension uses, with Instagram's real response shapes:
  GET /api/v1/friendships/{pk}/followers/?count=25&max_id=..   (~18-25 per page, opaque QVFE.. cursors)
  GET /api/v1/friendships/{pk}/following/?count=50&max_id=..   (50 per page, numeric cursors)
  GET /api/v1/users/{pk}/info/                                   (full user object: bio, links, counts)
  GET /{username}/                                              (profile page HTML with the embedded user JSON that
                                                                  extension/bridge.js scans; used for seed lookups)
  GET /api/v1/users/web_profile_info/?username=X                 (retired for scripts: always 429, must never be called)
  GET /accounts/login/                                           (HTML login page, target of the login redirect)

Failures are injected per request kind ('list' | 'profile' | 'page') by request number, set through
POST /__sim/config {"seeds": [...], "schedule": {"list": {"25": "429"}, ...}}. Failure types:
  429, 429_retry (Retry-After: 3600), please_wait (200 + "Please wait a few minutes"), soft_block (200, users:[] +
  has_more), login_redirect (302 -> HTML login page), login_html (200 HTML at the same URL), for_prefix (valid page
  behind `for (;;);`), checkpoint (400 checkpoint_required), slow (valid, 20 s simulated latency), hang (never answers
  before the client's abort), freeze (the tab itself stops responding), 500, useragent (400 "useragent mismatch").

Time is simulated by the driver: responses carry X-Sim-Latency (virtual ms) and X-Sim-Hang instead of really sleeping,
so hours of pacing run in seconds. The driver reads GET /__sim/served (unique users served per list in usable
responses) and GET /__sim/log to check the pipeline end to end.
"""
import argparse
import base64
import json
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_ID = '936619743392459'
POOL = 40000
USER_PK0 = 41_000_000_000
ADJ = ('sun', 'oak', 'salt', 'moss', 'clay', 'blue', 'wild', 'north', 'gold', 'linen', 'amber', 'stone', 'fern', 'honey')
NOUN = ('studio', 'goods', 'labs', 'co', 'supply', 'house', 'skin', 'roast', 'thread', 'made', 'works', 'shop', 'club')
BIOS = ('Founder @{b} | DTC skincare | shipping US + EU', 'Growth at {b}. Ecom, paid social, creative testing.',
        'Photographer. Coffee. Amsterdam.', 'Co-founder {b} - small batch candles - link below',
        'Marketing lead @{b} | Shopify brand, 7 figures', 'Just vibes', 'Head of brand {b} | apparel | NYC',
        'Designer, packaging and product visuals', '')

LOCK = threading.Lock()
STATE = {'seeds': {}, 'by_name': {}, 'schedule': {}, 'counters': {'list': 0, 'profile': 0, 'page': 0, 'other': 0},
         'served': {}, 'pages_good': {}, 'log': [], 'members': {}}


def user(i):
    i = int(i)
    name = f"{ADJ[i % len(ADJ)]}.{NOUN[(i // len(ADJ)) % len(NOUN)]}{i}"
    return {'pk': str(USER_PK0 + i), 'pk_id': str(USER_PK0 + i), 'id': str(USER_PK0 + i), 'username': name,
            'full_name': name.replace('.', ' ').title(), 'is_private': i % 7 == 0, 'is_verified': i % 97 == 0,
            'profile_pic_url': f'https://fake-ig.invalid/pfp/{USER_PK0 + i}.jpg', 'profile_pic_id': f'{i}_1',
            'has_anonymous_profile_picture': False, 'latest_reel_media': 0, 'third_party_downloads_enabled': 0}


def full_user(u, followers, following, extra=None):
    i = int(u['pk']) - USER_PK0 if int(u['pk']) >= USER_PK0 else int(u['pk']) % 1000
    brand = u['username'].split('.')[0] + 'brand'
    bio = BIOS[i % len(BIOS)].format(b=brand)
    out = dict(u, biography=bio, external_url=f'https://{brand}.com' if i % 3 == 0 else '',
               bio_links=[{'url': f'https://{brand}.shop'}] if i % 5 == 0 else [], category='Shopping & retail' if i % 4 == 0 else None,
               follower_count=followers, following_count=following, media_count=40 + i % 300,
               is_business=i % 4 == 0, account_type=2 if i % 4 == 0 else 1,
               hd_profile_pic_url_info={'url': u['profile_pic_url']})
    out.update(extra or {})
    return out


def members(seed, direction):
    key = f"{seed['username']}/{direction}"
    if key not in STATE['members']:
        size = seed['lists'].get(direction, 0)
        STATE['members'][key] = random.Random(key).sample(range(POOL), min(size, POOL))
    return STATE['members'][key]


def enc_cursor(seed, direction, off):
    if direction == 'following':
        return str(off)
    return 'QVFE' + base64.urlsafe_b64encode(f"{off}|{seed['pk']}".encode()).decode().rstrip('=')


def dec_cursor(seed, direction, cur):
    if not cur:
        return 0
    if direction == 'following':
        return int(cur)
    raw = base64.urlsafe_b64decode(cur[4:] + '=' * (-len(cur[4:]) % 4)).decode()
    off, pk = raw.split('|')
    if pk != seed['pk']:
        raise ValueError('cursor for another user')
    return int(off)


def list_page(seed, direction, q):
    """-> (status, body dict, served user indexes)"""
    if seed.get('private'):
        return 400, {'message': 'Not authorized to view user', 'status': 'fail'}, []
    count = int(q.get('count', ['50'])[0] or 50)
    off = dec_cursor(seed, direction, q.get('max_id', [''])[0])
    mem = members(seed, direction)
    if direction == 'followers' and seed.get('capped'):
        chunk = mem[:49]
        return 200, {'users': [user(i) for i in chunk], 'big_list': False, 'page_size': len(chunk), 'has_more': False,
                     'should_limit_list_of_followers': True, 'use_clickable_see_more': False, 'status': 'ok'}, chunk
    if direction == 'followers':  # Instagram caps follower pages at ~25 whatever `count` says, and varies the size
        size = min(count, 25) - random.Random(f"{seed['pk']}:{off}").choice((0, 0, 0, 1, 2, 3, 5, 7))
    else:
        size = min(count, 50)
    chunk = mem[off:off + size]
    body = {'users': [user(i) for i in chunk], 'big_list': len(mem) > 50, 'page_size': size, 'status': 'ok'}
    if direction == 'followers':
        body['should_limit_list_of_followers'] = False
    if off + size < len(mem):
        body.update(next_max_id=enc_cursor(seed, direction, off + size), has_more=True)
    elif int(seed['pk']) % 2:  # tail: sometimes has_more:false, sometimes neither key (seen live)
        body['has_more'] = False
    return 200, body, chunk


def pick_seed_by_pk(pk):
    return STATE['seeds'].get(str(pk))


def full_by_name(name):
    """-> full user object (v1 shape) for a seed or pool user handle, or None."""
    seed = STATE['by_name'].get(name.lower())
    if seed:
        return full_user(seed_user(seed), seed['followers'], seed['following'])
    m = re.fullmatch(r'[a-z]+\.[a-z]+(\d+)', name.lower())
    if not m or int(m.group(1)) >= POOL or user(m.group(1))['username'] != name.lower():
        return None
    i = int(m.group(1))
    return full_user(user(i), 100 + i * 37 % 90000, 50 + i * 13 % 2000)


def graphql_user(f):
    return {'id': f['pk'], 'username': f['username'], 'full_name': f['full_name'], 'biography': f['biography'],
            'external_url': f['external_url'] or None, 'category_name': f['category'], 'is_private': f['is_private'],
            'is_verified': f['is_verified'], 'is_business_account': f['is_business'], 'profile_pic_url': f['profile_pic_url'],
            'edge_followed_by': {'count': f['follower_count']}, 'edge_follow': {'count': f['following_count']},
            'edge_owner_to_timeline_media': {'count': f['media_count']}}


def profile_html(f):
    blob = json.dumps({'require': [['ScheduledServerJS', 'handle', None, [{'__bbox': {'result': {'data': {'user': graphql_user(f)}}}}]]]})
    return ('<!DOCTYPE html><html lang="en"><head><title>' + f['full_name'] + ' (@' + f['username'] + ') &bull; Instagram photos and videos'
            '</title></head><body><div id="root"></div><script type="application/json" data-sjs>' + blob + '</script></body></html>')


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def reply(self, status, body, ctype='application/json; charset=utf-8', headers=None, prefix=''):
        data = (prefix + (body if isinstance(body, str) else json.dumps(body))).encode()
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(n) or b'{}')
        if u.path == '/__sim/config':
            with LOCK:
                STATE['seeds'] = {str(s['pk']): s for s in body.get('seeds', [])}
                STATE['by_name'] = {s['username'].lower(): s for s in body.get('seeds', [])}
                STATE['schedule'] = {k: {int(n): t for n, t in v.items()} for k, v in (body.get('schedule') or {}).items()}
                # lanes mode: one fake account per simulated Chrome profile (X-Sim-Account), each with its own failures
                STATE['accounts'] = {str(a['ig_id']): dict(a, schedule={k: {int(n): t for n, t in v.items()}
                                                                        for k, v in (a.get('schedule') or {}).items()})
                                     for a in body.get('accounts') or []}
                STATE['acct_counters'] = {}
                for k in STATE['counters']:
                    STATE['counters'][k] = 0
                STATE['served'].clear(); STATE['pages_good'].clear(); STATE['log'].clear(); STATE['members'].clear()
            return self.reply(200, {'ok': True})
        self.reply(404, {'message': 'not found', 'status': 'fail'})

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == '/__sim/served':
            with LOCK:
                out = {k: {'served': sorted(v), 'pages_good': STATE['pages_good'].get(k, 0),
                           'size': len(STATE['members'].get(k, []))} for k, v in STATE['served'].items()}
            return self.reply(200, out)
        if u.path == '/__sim/log':
            with LOCK:
                return self.reply(200, {'log': STATE['log'], 'counters': STATE['counters']})
        acct = STATE.get('accounts', {}).get(self.headers.get('X-Sim-Account') or '')
        if u.path == '/':
            viewer = ('<script type="application/json" data-sjs>' + json.dumps({'viewer': {'user': {
                'id': acct['ig_id'], 'username': acct['handle'], 'full_name': acct['handle']}}}) + '</script>') if acct else ''
            return self.reply(200, '<!DOCTYPE html><html lang="en"><head><title>Instagram</title></head><body><main>Feed</main>'
                              + viewer + '</body></html>', 'text/html; charset=utf-8', {'X-Sim-Latency': 900})
        if u.path.startswith('/accounts/login'):
            return self.reply(200, '<!DOCTYPE html><html lang="en"><head><title>Login &bull; Instagram</title></head><body>'
                              '<form id="loginForm"><input name="username"><input name="password" type="password">'
                              '<button>Log in</button></form></body></html>', 'text/html; charset=utf-8')
        m_list = re.fullmatch(r'/api/v1/friendships/(\d+)/(followers|following)/', u.path)
        m_info = re.fullmatch(r'/api/v1/users/(\d+)/info/', u.path)
        m_page = re.fullmatch(r'/([A-Za-z0-9._]{1,30})/', u.path)
        kind = 'list' if m_list else 'profile' if m_info else 'page' if m_page else 'other'
        with LOCK:
            STATE['counters'][kind] += 1
            n = STATE['counters'][kind]
            inject = STATE['schedule'].get(kind, {}).get(n)
            if acct:   # its own request numbers and failures
                key = (acct['ig_id'], kind)
                STATE['acct_counters'][key] = STATE['acct_counters'].get(key, 0) + 1
                inject = acct['schedule'].get(kind, {}).get(STATE['acct_counters'][key])
        entry = {'kind': kind, 'n': n, 'path': u.path, 'max_id': q.get('max_id', [None])[0], 'inject': inject,
                 'sim_now': int(self.headers.get('X-Sim-Now') or 0), 'account': acct and acct['ig_id']}
        if u.path == '/api/v1/users/web_profile_info/':  # retired for scripts (RESEARCH.md): 429 on the first call
            entry['status'] = 429
            self.log(entry)
            return self.reply(429, {'message': 'Please wait a few minutes before you try again.', 'status': 'fail'})
        if self.headers.get('X-IG-App-ID') != APP_ID and kind in ('list', 'profile'):
            entry['status'] = 400
            self.log(entry)
            return self.reply(400, {'message': 'useragent mismatch', 'status': 'fail'})
        latency = random.Random(f'{kind}{n}').randint(300, 1500)
        hdr = {'X-Sim-Latency': latency, 'X-Sim-Inject': inject or '', 'x-ig-set-www-claim': 'hmac.AR3sim'}
        if inject and self.injected(inject, kind, u, hdr, entry):
            return
        try:
            if m_list:
                seed = pick_seed_by_pk(m_list.group(1))
                if not seed:
                    return self.done(entry, 404, {'message': 'User not found', 'status': 'fail'}, hdr)
                status, body, chunk = list_page(seed, m_list.group(2), q)
                good = status == 200 and inject != 'hang' and inject != 'freeze'
                if good:
                    key = f"{seed['username']}/{m_list.group(2)}"
                    with LOCK:
                        STATE['served'].setdefault(key, set()).update(user(i)['pk'] for i in chunk)
                        STATE['pages_good'][key] = STATE['pages_good'].get(key, 0) + 1
                    entry['users'] = len(chunk)
                return self.done(entry, status, body, hdr, prefix='for (;;);' if inject == 'for_prefix' else '')
            if m_info:
                pk = m_info.group(1)
                seed = pick_seed_by_pk(pk)
                if seed:
                    return self.done(entry, 200, {'user': full_user(seed_user(seed), seed['followers'], seed['following']),
                                                  'status': 'ok'}, hdr, prefix='for (;;);' if inject == 'for_prefix' else '')
                i = int(pk) - USER_PK0
                if not 0 <= i < POOL:
                    return self.done(entry, 404, {'message': 'User not found', 'status': 'fail'}, hdr)
                return self.done(entry, 200, {'user': full_user(user(i), 100 + i * 37 % 90000, 50 + i * 13 % 2000),
                                              'status': 'ok'}, hdr, prefix='for (;;);' if inject == 'for_prefix' else '')
            if kind == 'page':
                f = full_by_name(m_page.group(1))
                if not f:
                    return self.done(entry, 404, '<!DOCTYPE html><html><head><title>Page not found &bull; Instagram</title></head>'
                                     "<body>Sorry, this page isn't available.</body></html>", hdr, ctype='text/html; charset=utf-8')
                return self.done(entry, 200, profile_html(f), hdr, ctype='text/html; charset=utf-8')
        except (ValueError, KeyError) as e:
            return self.done(entry, 400, {'message': f'Invalid request: {e}', 'status': 'fail'}, hdr)
        return self.done(entry, 404, {'message': 'Page not found', 'status': 'fail'}, hdr)

    def injected(self, inject, kind, u, hdr, entry):
        if inject == '429':
            return self.done(entry, 429, {'message': 'Please wait a few minutes before you try again.', 'status': 'fail'}, hdr)
        if inject == '429_retry':
            return self.done(entry, 429, '', dict(hdr, **{'Retry-After': 3600}), ctype='text/html; charset=utf-8')
        if inject == 'please_wait':
            return self.done(entry, 200, {'message': 'Please wait a few minutes before you try again.', 'require_login': False,
                                          'spam': False, 'status': 'fail'}, hdr)
        if inject == 'soft_block':
            return self.done(entry, 200, {'users': [], 'big_list': True, 'page_size': 25, 'next_max_id': 'QVFEsoftblock',
                                          'has_more': True, 'status': 'ok'}, hdr)
        if inject == 'login_redirect':
            return self.done(entry, 302, '', dict(hdr, Location='/accounts/login/?next=' + u.path), ctype='text/html')
        if inject == 'login_html':
            return self.done(entry, 200, '<!DOCTYPE html><html><head><title>Instagram</title></head><body>'
                             '<div>Log in to see photos and videos from your friends.</div><input name="password">'
                             '</body></html>', hdr, ctype='text/html; charset=utf-8')
        if inject == 'checkpoint':
            return self.done(entry, 400, {'message': 'checkpoint_required', 'checkpoint_url':
                                          'https://www.instagram.com/challenge/?next=/api/v1/', 'lock': False,
                                          'flow_render_type': 0, 'status': 'fail'}, hdr)
        if inject == 'useragent':
            return self.done(entry, 400, {'message': 'useragent mismatch', 'status': 'fail'}, hdr)
        if inject == '500':
            return self.done(entry, 500, '<html><body>Something went wrong</body></html>', hdr, ctype='text/html')
        if inject == 'slow':
            time.sleep(0.15)  # a little real slowness too, so the driver really waits on the socket
            hdr['X-Sim-Latency'] = 20000
        if inject in ('hang', 'freeze'):
            hdr['X-Sim-Hang'] = 'abort' if inject == 'hang' else 'freeze'
        return None  # fall through to a normal (possibly delayed) response

    def done(self, entry, status, body, hdr, ctype='application/json; charset=utf-8', prefix=''):
        entry['status'] = status
        self.log(entry)
        self.reply(status, body, ctype, hdr, prefix)
        return True

    def log(self, entry):
        with LOCK:
            STATE['log'].append(entry)


def seed_user(seed):
    return {'pk': str(seed['pk']), 'pk_id': str(seed['pk']), 'id': str(seed['pk']), 'username': seed['username'],
            'full_name': seed['username'].replace('.', ' ').title(), 'is_private': bool(seed.get('private')),
            'is_verified': bool(seed.get('verified')), 'profile_pic_url': f"https://fake-ig.invalid/pfp/{seed['pk']}.jpg"}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # clients that hang up early (aborted fetches) are normal here
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8791)
    a = ap.parse_args()
    srv = Server(('127.0.0.1', a.port), H)
    print(f'fake instagram on http://127.0.0.1:{a.port}', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
