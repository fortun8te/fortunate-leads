"""Qualification page API (owned by the frontend work; server.py only registers ROUTES from here).

GET  /api/qual?view=all|ai|rules&q=&sort=recent|score&offset=&limit=
     -> {"summary": {...}, "total", "rows": [lead row + verdict details + site read]}
POST /api/qual/{id}/deeper
     -> {"ok", "bio_queued": bool, "site": site read | null, "note": str}
     Queues a fresh bio read and, when the person has a website / link in bio, fetches it here (public hosts only,
     8 s timeout, 400 KB cap, at most 3 redirects, one hop out of a link hub) and summarises it with the qualify LLM helper.
"""
import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

import db
import llm
import qualify

FETCH_TIMEOUT = 8
FETCH_CAP = 400 * 1024
HUBS = ('linktr.ee', 'beacons.ai', 'lnk.bio', 'linkin.bio', 'taplink.cc', 'stan.store', 'bio.link', 'campsite.bio',
        'hoo.be', 'komi.io', 'linkbio.co', 'solo.to', 'msha.ke', 'snipfeed.co', 'many.link', 'tap.bio', 'allmylinks.com')
SOCIAL = ('instagram.com', 'tiktok.com', 'youtube.com', 'youtu.be', 'facebook.com', 'fb.me', 'twitter.com', 'x.com',
          'spotify.com', 'apple.com', 'threads.net', 'pinterest.com', 'snapchat.com', 'linkedin.com', 'wa.me', 'whatsapp.com',
          't.me', 'discord.gg', 'twitch.tv', 'patreon.com', 'onlyfans.com', 'amazon.com', 'amzn.to') + HUBS

SCHEMA = """CREATE TABLE IF NOT EXISTS site_reads(person_id INTEGER PRIMARY KEY, url TEXT, final_url TEXT, title TEXT,
  summary TEXT, signals TEXT, error TEXT, model TEXT, at TEXT)"""


def _ensure(conn):
    conn.execute(SCHEMA)


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
            charset = r.headers.get_content_charset() or 'utf-8'
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


def page_text(doc):
    title = re.search(r'<title[^>]*>(.*?)</title>', doc, re.I | re.S)
    desc = re.search(r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]*content=["\']([^"\']*)', doc, re.I)
    body = re.sub(r'<(script|style|noscript|svg|template)[^>]*>.*?</\1>', ' ', doc, flags=re.I | re.S)
    body = html.unescape(re.sub(r'<[^>]+>', ' ', body))
    body = re.sub(r'\s+', ' ', body).strip()
    clean = lambda m: re.sub(r'\s+', ' ', html.unescape(m.group(1))).strip() if m else ''  # noqa: E731
    return clean(title)[:200], clean(desc)[:400], body[:3500]


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
    s['runs_ads'] = s['meta_pixel'] or s['tiktok_pixel'] or bool(s['google_ads'])
    return s


SUMMARY_PROMPT = """You read a brand's website for Fortunate, a studio that makes ad creatives for physical-product (DTC) brands.
Reply with one JSON object only:
{"summary": "two plain sentences: what they sell and to whom, and how established the shop looks",
 "sells_physical_products": true|false, "product_category": "one of: %s, or null",
 "stage": "pre-launch|early|growing|established|unknown", "us_market": true|false|null}
Use only what the page shows. Never invent facts."""


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
    return (data if isinstance(data, dict) else None), model


def site_tags(data, sig):
    """AI tags from a website read (grp 'ai'); only written for people with an AI verdict."""
    out = []
    if sig.get('runs_ads'):
        out.append('AI: Runs ads')
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


def read_site(conn, pid, url):
    title = desc = text = ''
    final, sig, data, model, err = url, {}, None, None, None
    try:
        final, doc = fetch(url)
        if _is(_host(final), HUBS):   # link hub: follow the first real destination once
            dest = links_out(doc, final)
            if dest:
                final, doc = fetch(dest[0])
        title, desc, text = page_text(doc)
        sig = signals(doc, final)
        if text or title:
            data, model = summarise(final, title, desc, text, sig)
            if data is None:
                err = 'the AI was not available; page facts are still shown'
    except ValueError as e:
        err = str(e)
    summary = re.sub(r'\s+', ' ', str((data or {}).get('summary') or '')).strip()[:500] or None
    if data:
        sig = dict(sig, sells_products=data.get('sells_physical_products'), category=data.get('product_category'),
                   stage=data.get('stage'), us_market=data.get('us_market'))
    conn.execute('INSERT OR REPLACE INTO site_reads VALUES(?,?,?,?,?,?,?,?,?)',
                 (pid, url, final, title or None, summary, json.dumps(sig), err, model, db.now()))
    v = conn.execute('SELECT model FROM verdicts WHERE person_id=?', (pid,)).fetchone()
    if v and v['model'] and v['model'] != 'rules' and not err:
        conn.executemany("INSERT OR IGNORE INTO tags VALUES(?,?,'ai','auto')", [(pid, t) for t in site_tags(data, sig)])
    conn.commit()
    return site_row(conn, pid)


def site_row(conn, pid):
    r = conn.execute('SELECT * FROM site_reads WHERE person_id=?', (pid,)).fetchone()
    if not r:
        return None
    try:
        sig = json.loads(r['signals'] or '{}')
    except ValueError:
        sig = {}
    return {'url': r['url'], 'final_url': r['final_url'], 'title': r['title'], 'summary': r['summary'], 'signals': sig,
            'error': r['error'], 'model': r['model'], 'at': r['at']}


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
        if ids:
            marks = ','.join('?' * len(ids))
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
                     site=site_row(conn, r['id']))
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
