"""Qualification page API (owned by the frontend work; server.py only registers ROUTES from here).

GET  /api/qual?view=all|ai|rules&q=&sort=recent|score&offset=&limit=
     -> {"summary": {...}, "total", "rows": [lead row + verdict details + site read]}
POST /api/qual/{id}/deeper
     -> {"ok", "bio_queued": bool, "site": site read | null, "note": str}
     Queues a fresh bio read and, when the person has a website / link in bio, fetches it here (public hosts only,
     8 s timeout, 400 KB cap, at most 3 redirects, one hop out of a link hub) and summarises it with the qualify LLM helper.
"""
import codecs
import hashlib
import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import db
import control
import llm
import qualify

FETCH_TIMEOUT = 8
FETCH_CAP = 400 * 1024
SITE_CACHE_AGE = timedelta(hours=24)
HUBS = ('linktr.ee', 'beacons.ai', 'lnk.bio', 'linkin.bio', 'taplink.cc', 'stan.store', 'bio.link', 'campsite.bio',
        'hoo.be', 'komi.io', 'linkbio.co', 'solo.to', 'msha.ke', 'snipfeed.co', 'many.link', 'tap.bio', 'allmylinks.com')
SOCIAL = ('instagram.com', 'tiktok.com', 'youtube.com', 'youtu.be', 'facebook.com', 'fb.me', 'twitter.com', 'x.com',
          'spotify.com', 'apple.com', 'threads.net', 'pinterest.com', 'snapchat.com', 'linkedin.com', 'wa.me', 'whatsapp.com',
          't.me', 'discord.gg', 'twitch.tv', 'patreon.com', 'onlyfans.com', 'amazon.com', 'amzn.to') + HUBS

SCHEMA = """CREATE TABLE IF NOT EXISTS site_reads(person_id INTEGER PRIMARY KEY, url TEXT, final_url TEXT, title TEXT,
  summary TEXT, signals TEXT, error TEXT, model TEXT, at TEXT, content_hash TEXT)"""
EVIDENCE_SCHEMA = """CREATE TABLE IF NOT EXISTS site_evidence(person_id INTEGER, tag TEXT, url TEXT,
  PRIMARY KEY(person_id,tag))"""


def _ensure(conn):
    conn.execute(SCHEMA)
    if 'content_hash' not in {r[1] for r in conn.execute('PRAGMA table_info(site_reads)')}:
        conn.execute('ALTER TABLE site_reads ADD COLUMN content_hash TEXT')
    conn.execute(EVIDENCE_SCHEMA)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='lead_rev_site_evidence_delete'").fetchone():
        db.add_rev_triggers(conn, ('site_reads', 'site_evidence'))


# ---------- safe fetch ----------

def _public(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split('%')[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return bool(infos)


def _check(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ('http', 'https') or not u.hostname or u.port not in (None, 80, 443):
        raise ValueError('only normal web links can be read')
    if not _public(u.hostname):
        raise ValueError('that address is not a public website')
    return u


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check(newurl)   # every hop must be a public web address too
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_Redirects, urllib.request.HTTPSHandler(context=llm._ssl()))


def fetch(url):
    """-> (final_url, html text). Raises ValueError with a plain message."""
    _check(url)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
                                               '(KHTML, like Gecko) Version/17.0 Safari/605.1.15', 'Accept': 'text/html,*/*;q=0.5'})
    try:
        with _OPENER.open(req, timeout=FETCH_TIMEOUT) as r:
            ctype = r.headers.get('Content-Type', '')
            if ctype and 'html' not in ctype and 'text' not in ctype:
                raise ValueError('the link is not a web page')
            body = r.read(FETCH_CAP)
            charset = r.headers.get_content_charset()
            if not charset:
                match = re.search(rb'<meta\b[^>]*\bcharset\s*=\s*["\']?([\w.-]+)', body[:4096], re.I)
                charset = match.group(1).decode('ascii', 'ignore') if match else 'utf-8'
            try:
                codecs.lookup(charset)
            except LookupError:
                charset = 'utf-8'
            return r.geturl(), body.decode(charset, 'replace')
    except urllib.error.HTTPError as e:
        raise ValueError(f'the website answered with error {e.code}') from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        raise ValueError('timed out' if 'timed out' in str(e) else 'could not reach the website') from None


def _host(url):
    h = (urllib.parse.urlsplit(url).hostname or '').lower()
    return h[4:] if h.startswith('www.') else h


def _is(host, domains):
    return any(host == d or host.endswith('.' + d) for d in domains)


class _PageText(HTMLParser):
    SKIP = {'script', 'style', 'noscript', 'svg', 'template'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.title = []
        self.description = ''
        self.body = []
        self.main = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == 'meta':
            a = dict(attrs)
            if (a.get('name') or a.get('property') or '').lower() in ('description', 'og:description') and not self.description:
                self.description = a.get('content') or ''
        if tag not in ('meta', 'link', 'img', 'br', 'hr', 'input', 'source', 'area', 'base', 'embed', 'wbr'):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]

    def handle_data(self, data):
        if any(tag in self.SKIP for tag in self.stack):
            return
        if 'title' in self.stack:
            self.title.append(data)
        elif data.strip():
            self.body.append(data)
            if 'main' in self.stack or 'article' in self.stack:
                self.main.append(data)


def page_text(doc, limit=3500, prefer_main=True):
    parsed = _PageText()
    parsed.feed(doc)
    clean = lambda parts: re.sub(r'\s+', ' ', html.unescape(' '.join(parts))).strip()  # noqa: E731
    # Main/article text avoids menus and cookie banners crowding out the product evidence.
    chosen = parsed.main if prefer_main and len(clean(parsed.main)) >= 100 else parsed.body
    return clean(parsed.title)[:200], clean([parsed.description])[:400], clean(chosen)[:limit]


def links_out(doc, base):
    out = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', doc, re.I):
        u = urllib.parse.urljoin(base, html.unescape(href))
        if u.startswith(('http://', 'https://')) and not _is(_host(u), SOCIAL) and u not in out:
            out.append(u)
    return out


def signals(doc, final_url):
    """Plain facts read straight from the page source (no model needed)."""
    low = doc.lower()
    s = {
        'shop': 'Shopify' if ('cdn.shopify.com' in low or 'shopify.theme' in low) else 'WooCommerce' if 'woocommerce' in low
        else 'BigCommerce' if 'bigcommerce' in low else 'Wix' if 'wix.com' in low else 'Squarespace' if 'squarespace' in low else None,
        'cart': bool(re.search(r'add to (cart|bag)|/cart\b|checkout', low)),
        'meta_pixel': 'connect.facebook.net' in low or 'fbq(' in low,
        'tiktok_pixel': 'analytics.tiktok.com' in low or 'ttq.load' in low,
        'google_ads': 'googleadservices' in low or re.search(r"gtag\(['\"]config['\"],\s*['\"]aw-", low) is not None,
        'klaviyo': 'klaviyo' in low,
        'usd': bool(re.search(r'\$\s?\d|\busd\b', low)),
        'eur': bool(re.search(r'€\s?\d|\d\s?€|\beur\b', low)),
    }
    # Installed tracking code can remain after a campaign ends (or precede one).
    # Page source alone does not establish that ads are currently running.
    s['ad_tracking_detected'] = s['meta_pixel'] or s['tiktok_pixel'] or s['google_ads']
    s['runs_ads'] = None
    return s


SUMMARY_PROMPT = """You read a brand's website for Fortunate, a studio that makes ad creatives for physical-product (DTC) brands.
Reply with one JSON object only:
{"summary": "two plain sentences: what they sell and to whom, and how established the shop looks",
 "sells_physical_products": true|false, "product_category": "one of: %s, or null",
 "stage": "pre-launch|early|growing|established|unknown", "us_market": true|false|null}
Use only what the page shows. Never invent facts. An installed ad pixel or tracking tag does not show that ads are running."""


def summarise(url, title, desc, text, sig):
    niches = [t for t, g in qualify.TAXONOMY.items() if g == 'niche']
    facts = ', '.join(k for k, v in sig.items() if v is True) or 'none'
    user = f'URL: {url}\nTitle: {title}\nDescription: {desc}\nDetected in page source: {facts}; platform: {sig.get("shop") or "unknown"}\nPage text: {text}'
    try:
        content, model = qualify._providers().chat([{'role': 'system', 'content': SUMMARY_PROMPT % ', '.join(niches)},
                                                    {'role': 'user', 'content': user}], timeout=30, budget=45, max_tokens=700)
    except llm.Unavailable:
        return None, None
    data = qualify.parse_json(content)
    return _valid_summary(data), model


def _valid_summary(data):
    if not isinstance(data, dict) or not isinstance(data.get('summary'), str) or not data['summary'].strip():
        return None
    required = {'summary', 'sells_physical_products', 'product_category', 'stage', 'us_market'}
    if not required.issubset(data) or not isinstance(data['sells_physical_products'], bool):
        return None
    allowed = {t for t, group in qualify.TAXONOMY.items() if group == 'niche'}
    for key in ('sells_physical_products', 'us_market'):
        if data.get(key) is not None and not isinstance(data[key], bool):
            return None
    if data.get('product_category') is not None and (not isinstance(data['product_category'], str)
                                                   or data['product_category'] not in allowed):
        return None
    if data.get('stage') not in ('pre-launch', 'early', 'growing', 'established', 'unknown'):
        return None
    return {key: data.get(key) for key in ('summary', 'sells_physical_products', 'product_category', 'stage', 'us_market')}


def site_tags(data, sig):
    """Claims supported by this website read, stored apart from profile verdict tags."""
    out = []
    if sig.get('ad_tracking_detected'):
        out.append('AI: Ad tracking detected')
    if (sig.get('shop') or sig.get('cart')) and (not data or data.get('sells_physical_products') is not False):
        out.append('AI: Has online shop')
    if data:
        if data.get('us_market') is True:
            out.append('AI: US market')
        cat = data.get('product_category')
        if isinstance(cat, str) and qualify.TAXONOMY.get(cat) == 'niche':
            out.append('AI: ' + cat)
        stage = {'pre-launch': 'Pre-launch', 'early': 'Early stage', 'growing': 'Growing', 'established': 'Established'}.get(data.get('stage'))
        if stage:
            out.append('AI: ' + stage)
    return out


def _recent_read(row, url):
    if not row or row['url'] != url or not row['content_hash']:
        return False
    try:
        at = datetime.fromisoformat(row['at'].replace('Z', '+00:00'))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return timedelta(0) <= datetime.now(timezone.utc) - at < SITE_CACHE_AGE
    except (AttributeError, ValueError, TypeError):
        return False


def _site_evidence(conn, pid, url, names):
    conn.execute('DELETE FROM site_evidence WHERE person_id=?', (pid,))
    conn.executemany('INSERT INTO site_evidence(person_id,tag,url) VALUES(?,?,?)',
                     [(pid, name, url) for name in dict.fromkeys(names)])


def read_site(conn, pid, url, force=False):
    _ensure(conn)
    url = url.strip()
    previous = conn.execute('SELECT * FROM site_reads WHERE person_id=?', (pid,)).fetchone()
    paused = control.stage_paused(conn, 'ai')
    reusable = previous and (not previous['error'] or (paused and previous['error'] == 'AI scoring is off; page facts are still shown'))
    if not force and reusable and _recent_read(previous, url):
        return site_row(conn, pid)
    try:
        old_sig = json.loads(previous['signals'] or '{}') if previous else {}
    except (ValueError, TypeError):
        old_sig = {}
    if not isinstance(old_sig, dict):
        old_sig = {}
    title = desc = text = ''
    final, sig, data, model, err = url, {}, None, None, None
    content_hash = None
    summary = None
    try:
        final, doc = fetch(url)
        if _is(_host(final), HUBS):   # link hub: follow the first real destination once
            dest = links_out(doc, final)
            if dest:
                final, doc = fetch(dest[0])
        title, desc, text = page_text(doc)
        sig = signals(doc, final)
        payload = [final, title, desc, text, sig]
        content_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if text or title:
            if control.stage_paused(conn, 'ai'):
                err = 'AI scoring is off; page facts are still shown'
            elif (previous and previous['url'] == url and previous['content_hash'] == content_hash
                  and not previous['error'] and previous['model']):
                # A forced reread with identical evidence needs no new model call.
                model = previous['model']
                sig.update({k: old_sig.get(k) for k in ('sells_products', 'category', 'stage', 'us_market')})
                summary = previous['summary']
            else:
                data, model = summarise(final, title, desc, text, sig)
                data = _valid_summary(data)
                if data is None:
                    model = None
                    err = 'the AI was not available; page facts are still shown'
    except ValueError as e:
        err = str(e)
    summary = summary or re.sub(r'\s+', ' ', str((data or {}).get('summary') or '')).strip()[:500] or None
    if data:
        sig = dict(sig, sells_products=data.get('sells_physical_products'), category=data.get('product_category'),
                   stage=data.get('stage'), us_market=data.get('us_market'))
    conn.execute('INSERT OR REPLACE INTO site_reads(person_id,url,final_url,title,summary,signals,error,model,at,content_hash) '
                 'VALUES(?,?,?,?,?,?,?,?,?,?)',
                 (pid, url, final, title or None, summary, json.dumps(sig), err, model, db.now(), content_hash))
    # Website evidence owns only these rows. Profile/manual tags may carry the
    # same names and cannot safely be attributed to an older website read.
    site_data = data or ({'us_market': sig.get('us_market'), 'product_category': sig.get('category'),
                          'stage': sig.get('stage'), 'sells_physical_products': sig.get('sells_products')} if model else None)
    _site_evidence(conn, pid, url, site_tags(site_data, sig))
    conn.commit()
    return site_row(conn, pid)


def site_row(conn, pid):
    _ensure(conn)
    r = conn.execute('SELECT * FROM site_reads WHERE person_id=?', (pid,)).fetchone()
    if not r:
        return None
    current = conn.execute('SELECT website FROM people WHERE id=?', (pid,)).fetchone()
    if not current or (current['website'] or '').strip() != (r['url'] or '').strip():
        return None
    try:
        sig = json.loads(r['signals'] or '{}')
    except (ValueError, TypeError):
        sig = {}
    if not isinstance(sig, dict):
        sig = {}
    stale = not _recent_read(r, r['url'])
    return {'url': r['url'], 'final_url': r['final_url'], 'title': r['title'], 'summary': r['summary'], 'signals': sig,
            'error': r['error'], 'model': r['model'], 'at': r['at'], 'stale': stale,
            'tags': [x[0] for x in conn.execute('SELECT tag FROM site_evidence WHERE person_id=? AND url=? ORDER BY tag',
                                                (pid, r['url']))] if not stale else []}


# ---------- endpoints ----------

def routes(srv):
    """srv = the server module (for lead_rows, lead_filter, api_read, errors)."""

    def api_qual(conn, q, b):
        _ensure(conn)
        view = q.get('view', ['all'])[0]
        where, args = srv.lead_filter(q)
        where = [srv.NOT_ME, 'v.person_id IS NOT NULL'] + where
        if view == 'ai':
            where.append("coalesce(v.model,'rules')!='rules'")
        elif view == 'rules':
            where.append("coalesce(v.model,'rules')='rules'")
        elif view != 'all':
            raise srv.Bad('view must be all, ai or rules')
        sort = {'recent': 'v.updated_at DESC', 'score': 'v.score DESC'}.get(q.get('sort', ['score'])[0])
        if not sort:
            raise srv.Bad('sort must be recent or score')
        offset = max(0, srv.qint(q, 'offset') or 0)
        limit = min(100, max(1, srv.qint(q, 'limit') or 30))
        sw = ' WHERE ' + ' AND '.join(where)
        total = conn.execute(f'SELECT count(*) {srv.PEOPLE_FROM}{sw}', args).fetchone()[0]
        rows = conn.execute(f'{srv.LEAD_SQL}{sw} ORDER BY {sort}, p.id LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
        base = {r['id']: r for r in rows}
        out = srv.lead_rows(conn, rows)
        ids = [r['id'] for r in rows]
        vs = {}
        connections = {}
        if ids:
            marks = ','.join('?' * len(ids))
            me = srv.me_handle(conn)
            # Keep the direction with each observed link. A source-list name alone
            # cannot tell the reader who follows whom.
            for edge in conn.execute(f'SELECT person_id, seed, direction FROM current_edges '
                                     f'WHERE person_id IN ({marks}) ORDER BY seed, direction', ids):
                connections.setdefault(edge['person_id'], []).append({'handle': edge['seed'],
                                                                      'direction': edge['direction'],
                                                                      'is_me': bool(me and edge['seed'].casefold() == me.casefold())})
            for v in conn.execute(f'SELECT person_id, prefilter, model, updated_at, prompt, evidence FROM verdicts WHERE person_id IN ({marks})', ids):
                try:
                    ev = json.loads(v['evidence'] or '[]')
                except ValueError:
                    ev = []
                vs[v['person_id']] = {'prefilter': v['prefilter'], 'model': v['model'], 'at': v['updated_at'], 'prompt': v['prompt'],
                                      'evidence': [x for x in ev if isinstance(x, str)] if isinstance(ev, list) else []}
        for r in out:
            p = base[r['id']]
            r.update(verdict=vs.get(r['id']), category=p['category'], bio_at=p['bio_at'], is_private=p['is_private'],
                     site=site_row(conn, r['id']), connection_edges=connections.get(r['id'], []))
        s = conn.execute("SELECT count(*), sum(coalesce(model,'rules')!='rules'), sum(model='rules') FROM verdicts").fetchone()
        return {'total': total, 'rows': out,
                'summary': {'verdicts': s[0] or 0, 'ai': s[1] or 0, 'rules': s[2] or 0,
                            'with_bio': conn.execute("SELECT count(*) FROM people WHERE coalesce(bio,'')!=''").fetchone()[0],
                            'sites': conn.execute('SELECT count(*) FROM site_reads').fetchone()[0]}}

    def api_deeper(conn, q, b, pid):
        _ensure(conn)
        row = srv.person_row(conn, pid)
        queued = True
        try:
            srv.api_read(conn, q, {}, pid)
        except srv.Bad:
            queued = False
        site = None
        url = (row['website'] or '').strip()
        if url:
            site = read_site(conn, pid, url)
        note = ('Bio re-read queued' if queued else 'Bio cannot be re-read') + (
            '; website read' if site and not site.get('error') else f"; website: {site['error']}" if site else '; no website in bio')
        return {'bio_queued': queued, 'site': site, 'note': note}

    return [('GET', r'/api/qual', api_qual), ('POST', r'/api/qual/(\d+)/deeper', api_deeper)]
