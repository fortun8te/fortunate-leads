"""Web research for qualification: a local SearXNG search plus the person's own website. Stdlib only.

Before the model scores someone, research() looks them up (name + handle, the brand/website) on the SearXNG instance
at SEARXNG_URL (default http://127.0.0.1:8888, started by ~/bin/searxng) and reads their website's title, description
and opening text. The result is cached per person for CACHE_AGE and only refetched when the handle, name or website
changes. SearXNG down or slow -> no research, qualification carries on with the profile alone.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone

import db

URL = os.environ.get('SEARXNG_URL', 'http://127.0.0.1:8888').rstrip('/')
PARALLEL = max(1, int(os.environ.get('SEARXNG_PARALLEL', '6')))   # upstream engines rate-limit bursts from one IP
SEARCH_TIMEOUT = 6
LOOKUP_BUDGET = 16   # seconds for both searches and the site, including time waiting for the search gate
SITE_WORKERS = 8     # website reads are shared across lookup workers, not one extra thread per person
RESULTS = 6          # search results kept per person
SNIPPET = 240        # characters per result
SITE_TEXT = 600      # characters of the website's own text
CACHE_AGE = timedelta(days=30)
HEALTH_TTL = 60

SCHEMA = """CREATE TABLE IF NOT EXISTS web_research(person_id INTEGER PRIMARY KEY, input TEXT NOT NULL,
  results TEXT NOT NULL, site TEXT, at TEXT NOT NULL)"""

_gate = threading.BoundedSemaphore(PARALLEL)
_sites = ThreadPoolExecutor(max_workers=SITE_WORKERS, thread_name_prefix='research-site')
_health = {'at': None, 'ok': False}
_lock = threading.Lock()


def ensure(conn):
    conn.execute(SCHEMA)


def available(now=None):
    """Cached health check (HEALTH_TTL): a dead SearXNG costs one probe a minute, not one per person."""
    if os.environ.get('FL_NO_ORSLOT'):   # the offline switch tests use for every outside service
        return False
    now = time.monotonic() if now is None else now
    with _lock:
        if _health['at'] is not None and 0 <= now - _health['at'] < HEALTH_TTL:
            return _health['ok']
    try:
        with urllib.request.urlopen(URL + '/healthz', timeout=3) as r:
            ok = r.status == 200
    except OSError:
        ok = False
    with _lock:
        _health.update(at=now, ok=ok)
    return ok


def search(query, n=RESULTS, retry=True, deadline=None):
    """-> [{'title','url','snippet'}] for one query; [] on any failure. An empty answer is retried once (engines
    that were just rate-limited or still warming up often answer a second later)."""
    params = urllib.parse.urlencode({'q': query, 'format': 'json', 'language': 'all', 'safesearch': 0})
    req = urllib.request.Request(f'{URL}/search?{params}', headers={'Accept': 'application/json'})
    remaining = (deadline - time.monotonic()) if deadline is not None else SEARCH_TIMEOUT
    if remaining <= 0 or not _gate.acquire(timeout=remaining):
        return []
    try:
        try:
            remaining = (deadline - time.monotonic()) if deadline is not None else SEARCH_TIMEOUT
            if remaining <= 0:
                return []
            with urllib.request.urlopen(req, timeout=min(SEARCH_TIMEOUT, remaining)) as r:
                data = json.loads(r.read(3_000_000))
        except (OSError, ValueError):
            return []
    finally:
        _gate.release()
    if not isinstance(data, dict):
        return []
    out = []
    for item in data.get('results') or []:
        if not isinstance(item, dict) or not isinstance(item.get('url'), str):
            continue
        out.append({'title': _clean(item.get('title'))[:140], 'url': item['url'][:300],
                    'snippet': _clean(item.get('content'))[:SNIPPET]})
        if len(out) >= n:
            break
    if not out and retry and (deadline is None or deadline - time.monotonic() > 1):
        time.sleep(1)
        return search(query, n, retry=False, deadline=deadline)
    return out


def _clean(s):
    return re.sub(r'\s+', ' ', str(s or '')).strip()


def _host(url):
    try:
        return (urllib.parse.urlsplit(url if '//' in url else 'https://' + url).hostname or '').lower().removeprefix('www.')
    except ValueError:
        return ''


def queries(person):
    """The searches that identify a person and their business, most specific first."""
    handle = str(person.get('handle') or '').lstrip('@')
    name = _clean(person.get('name'))
    qs = [f'"{name}" {handle}' if name and name.lower() != handle.lower() else f'"{handle}" instagram']
    import qual_api   # local import: qual_api imports the server's shared settings
    site = person.get('website') or ''
    host = _host(site)
    who = name or handle
    if host and not qual_api._is(host, qual_api.SOCIAL) and not qual_api._is(host, qual_api.HUBS):
        qs.append(f'"{who}" founder OR ceo OR owner -site:{host}')   # who runs it, from outside their own pages
    else:
        qs.append(f'"{who}" founder OR brand OR company')
    return qs


def read_website(person):
    """Title, description and opening text of the person's own website ('' for link hubs, socials or failures)."""
    import qual_api
    site = person.get('website') or ''
    host = _host(site)
    if not host or qual_api._is(host, qual_api.SOCIAL):
        return ''
    try:
        final, doc = qual_api.fetch(site if '//' in site else 'https://' + site)
    except ValueError:
        return ''
    title, desc, text = qual_api.page_text(doc)
    sig = qual_api.signals(doc, final)
    shop = f"store on {sig['shop']}" if sig.get('shop') else ''
    parts = [p for p in (title, desc, shop, 'has a cart/checkout' if sig.get('cart') else '', text[:SITE_TEXT]) if p]
    return f'{_host(final)}: ' + ' | '.join(parts)


def _key(person):
    return json.dumps([str(person.get('handle') or '').lower(), _clean(person.get('name')), str(person.get('website') or '')])


def cached(conn, person):
    row = conn.execute('SELECT * FROM web_research WHERE person_id=?', (person['id'],)).fetchone()
    if not row or row['input'] != _key(person):
        return None
    try:
        at = datetime.fromisoformat(row['at'])
    except ValueError:
        return None
    results = json.loads(row['results'])
    site = row['site'] or ''
    # Retry missing evidence soon. A transient search outage must not suppress research for 30 days.
    complete = bool(results) and (bool(site) or not person.get('website'))
    age = CACHE_AGE if complete else timedelta(days=1) if results or site else timedelta(hours=1)
    if datetime.now(timezone.utc) - at > age:
        return None
    return {'results': results, 'site': site}


def lookup(person):
    """Network only (no DB). Site and searches overlap; slow lookups return partial evidence."""
    deadline = time.monotonic() + LOOKUP_BUDGET
    site_future = _sites.submit(read_website, person) if person.get('website') else None
    seen, per_host, results = set(), {}, []
    # A result has to name them (full name, handle or their domain) to count; common words alone match strangers.
    marks = {m.lower() for m in (_clean(person.get('name')), str(person.get('handle') or '').lstrip('@'),
                                 _host(person.get('website') or '')) if m and len(m) >= 4}
    for q in queries(person):
        # The second query covers ownership/brand evidence absent from a profile-name search.
        # Two distinct queries replace the old empty-result retry; neither can consume the full batch.
        for r in search(q, retry=False, deadline=deadline):
            host = _host(r['url'])
            blob = f"{r['title']} {r['snippet']} {r['url']}".lower()
            if marks and not any(m in blob for m in marks):
                continue
            # Instagram's own pages repeat the profile the model already has; two pages per site keep sources varied.
            if r['url'] in seen or host.endswith('instagram.com') or per_host.get(host, 0) >= 2:
                continue
            seen.add(r['url'])
            per_host[host] = per_host.get(host, 0) + 1
            results.append(r)
    site = ''
    if site_future:
        try:
            site = site_future.result(timeout=max(0, deadline - time.monotonic()))
        except FutureTimeout:
            site_future.cancel()
        except Exception:
            # A failed site read must not discard independently useful search evidence.
            pass
    return {'results': results[:RESULTS], 'site': site}


def store(conn, person, found):
    conn.execute('INSERT OR REPLACE INTO web_research VALUES(?,?,?,?,?)',
                 (person['id'], _key(person), json.dumps(found['results'], ensure_ascii=False), found['site'], db.now()))


def lines(found):
    """Packet lines for the model, labelled so it weighs them as outside evidence that may be about someone else."""
    if not found or not (found.get('results') or found.get('site')):
        return []
    out = ['WEB RESEARCH (search results and their website; results can be about a different person with the same '
           'name, so only use what clearly matches this profile):']
    if found.get('site'):
        out.append('- Their website ' + found['site'])
    out += [f"- {r['title']} ({_host(r['url'])}): {r['snippet']}" for r in found.get('results') or []]
    return out


def text(found):
    """Plain text the evidence checks may quote from."""
    if not found:
        return ''
    return ' \n '.join([found.get('site') or ''] + [f"{r['title']} {r['snippet']}" for r in found.get('results') or []])
