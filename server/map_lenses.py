"""Whole-database cohorts for the map's view modes (docs/MAP_VIEW_API.md, "View modes").

The default page (``/api/map/view?cohort=1``) is ordered by recorded follow evidence. Other view modes
need other people on the page, not just the same people in another order: the biggest accounts, the people
your connections link to, the people in the most collected audiences, the promising leads. Each lens is a
ranked list over every person in the layout; ``/api/map/view?...&lens=<id>`` pages through it with the same
filters, cursor contract and response shape as the default cohort.

Facts are computed once per data snapshot (about half a second for 170,000 people), cached in compact
arrays, and refreshed at most every ``FACTS_TTL`` seconds while collection keeps changing the data.
"""
import array
import json
import os
import threading
import time
from collections import OrderedDict

import db
import map_layout as ML

LEADS_MAX_FOLLOWERS = 100_000       # PRODUCT.md: "Too big" starts near 300k; leave margin for the ones that are reachable
FACTS_TTL = 90.0
FILTERED_KEEP = 12

# id -> (title, one line). The browser carries its own wording; these keep the API self-describing.
LENSES = {
    'reach': ('Biggest reach', 'People with a saved follower count, largest first.'),
    'warm': ('Warm paths', 'People who are not linked to you directly, ranked by how many of your connections link to them.'),
    'shared': ('Shared audiences', 'People seen in two or more collected audiences, most audiences first.'),
    'leads': ('Promising leads', f'Hot and warm leads under {LEADS_MAX_FOLLOWERS:,} followers, nearest to you first.'),
}
_PRIORITY_TIERS = ('hot', 'warm')
EXTRA_COLUMNS = ('links', 'priority')


class Facts:
    """Per-person numbers behind every lens, indexed by person id."""

    def __init__(self, signature):
        self.signature = signature
        self.id = os.urandom(4).hex()
        self.made = time.time()
        self.owner_id = 0
        self.size = 0
        self.cls = self.cl = self.followers = self.links = self.degree = self.priority = self.hot = None
        self.orders = {}
        self.filtered = OrderedDict()
        self.lock = threading.Lock()


_FACTS = {}
_FACTS_LOCK = threading.Lock()


def _array(code, size, fill):
    return array.array(code, [fill]) * size


def _compute(conn, store, signature):
    facts = Facts(signature)
    meta = store.meta('plan', {}) or {}
    facts.owner_id = meta.get('owner_id') or 0
    top = conn.execute('SELECT coalesce(max(id),0) FROM people').fetchone()[0]
    size = top + 1
    facts.size = size
    cls, cl = _array('H', size, 0xFFFF), _array('H', size, 0)
    direct = []
    for pid, value, closeness in store.conn.execute('SELECT person_id,cls,cl FROM mp'):
        if pid < size:
            cls[pid] = value
            cl[pid] = min(closeness, 65534)
            if (value // 64) & 3:
                direct.append(pid)
    facts.cls, facts.cl = cls, cl
    followers, priority, hot = _array('q', size, -1), _array('b', size, -1), _array('b', size, 0)
    for pid, count, score, tier in conn.execute('SELECT p.id,p.followers,v.score,v.tier FROM people p LEFT JOIN verdicts v ON v.person_id=p.id'):
        if pid >= size:
            continue
        if count is not None:
            followers[pid] = int(count)
        if score is not None:
            priority[pid] = max(0, min(100, int(round(score))))
        hot[pid] = 1 if tier in _PRIORITY_TIERS else 0
    degree = _array('H', size, 0)
    for pid, value in conn.execute('SELECT person_id,degree FROM map_person_degree'):
        if pid < size:
            degree[pid] = min(value, 65535)
    facts.followers, facts.priority, facts.hot, facts.degree = followers, priority, hot, degree
    # A connection "links to" a person when it is a collected source whose list contains them. Only people the
    # owner follows or who follow the owner count, so this is a path of two recorded steps, never a guess.
    links = _array('H', size, 0)
    direct_set = set(direct)
    handle_of = {}
    for pid, handle in conn.execute('SELECT p.id,p.handle FROM people p JOIN map_source_handles h ON h.handle=p.handle'):
        if pid in direct_set:
            handle_of[pid] = handle
    for pid, handle in handle_of.items():
        seen = {row[0] for row in conn.execute('SELECT person_id FROM current_edges WHERE seed=?', (handle,))}
        seen.discard(pid)
        seen.discard(facts.owner_id)
        for other in seen:
            if other < size and links[other] < 65535:
                links[other] += 1
    facts.links = links
    return facts


def facts_for(conn, store):
    """The cached facts for this layout and data snapshot, recomputed at most every FACTS_TTL seconds."""
    key = (str(store.path),)
    signature = (store.build_id, store.rev, db.get_setting(conn, 'lead_data_rev', 0))
    with _FACTS_LOCK:
        hit = _FACTS.get(key)
    if hit is not None and (hit.signature == signature or time.time() - hit.made < FACTS_TTL):
        return hit
    with _FACTS_LOCK:
        busy = _FACTS.setdefault(('busy',) + key, threading.Lock())
    if not busy.acquire(blocking=hit is None):
        return hit                      # another request is refreshing; keep serving the previous snapshot
    try:
        with _FACTS_LOCK:
            now = _FACTS.get(key)
        if now is not None and now is not hit:
            return now                  # someone finished while this request waited
        fresh = _compute(conn, store, signature)
        with _FACTS_LOCK:
            _FACTS[key] = fresh
        return fresh
    finally:
        busy.release()


def clear_cache():
    with _FACTS_LOCK:
        _FACTS.clear()


def _order(facts, lens):
    """Person ids for a lens, best first. Computed once per snapshot."""
    with facts.lock:
        hit = facts.orders.get(lens)
        if hit is not None:
            return hit
        owner, size = facts.owner_id, facts.size
        cls, cl, followers, links, degree, priority = facts.cls, facts.cl, facts.followers, facts.links, facts.degree, facts.priority
        present = [pid for pid in range(1, size) if cls[pid] != 0xFFFF and pid != owner]
        if lens == 'reach':
            chosen = [pid for pid in present if followers[pid] >= 0]
            chosen.sort(key=lambda pid: (-followers[pid], pid))
        elif lens == 'warm':
            chosen = [pid for pid in present if links[pid] >= 1 and not (cls[pid] // 64) & 3]
            chosen.sort(key=lambda pid: (-links[pid], -degree[pid], -followers[pid], pid))
        elif lens == 'shared':
            chosen = [pid for pid in present if degree[pid] >= 2]
            chosen.sort(key=lambda pid: (-degree[pid], -cl[pid], pid))
        elif lens == 'leads':
            chosen = [pid for pid in present if facts.hot[pid] and (followers[pid] < 0 or followers[pid] < LEADS_MAX_FOLLOWERS)]
            chosen.sort(key=lambda pid: (-cl[pid], -priority[pid], pid))
        else:
            raise ValueError('unknown lens')
        facts.orders[lens] = hit = array.array('i', chosen)
        return hit


def _eligible(facts, lens, mask):
    """The lens order restricted to the classes the filters allow (cached per filter set)."""
    key = (lens, format(mask, 'x'))
    with facts.lock:
        hit = facts.filtered.get(key)
        if hit is not None:
            facts.filtered.move_to_end(key)
            return hit
    order = _order(facts, lens)
    cls = facts.cls
    allowed = frozenset(c for c in range(7 * 64) if (mask >> c) & 1)
    chosen = array.array('i', (pid for pid in order if cls[pid] in allowed))
    with facts.lock:
        facts.filtered[key] = chosen
        while len(facts.filtered) > FILTERED_KEEP:
            facts.filtered.popitem(last=False)
    return chosen


def cohort(conn, store, query):
    """One page of a lens, in the shape of the default cohort page plus ``links`` and ``priority``."""
    import map_view as MV
    lens = query.lens
    facts = facts_for(conn, store)
    ids = _eligible(facts, lens, query.mask)
    revision = f'lens:{lens}:{facts.id}:{format(query.mask, "x")}'
    reset = bool(query.after and query.after[2] != revision)
    start = 0 if reset or not query.after else max(0, int(query.after[0]))
    budget = min(query.budget, MV.COMPACT_BUDGET_MAX if query.compact else 1000)
    page = list(ids[start:start + budget])
    more = start + budget < len(ids)
    details = MV._details(store, page)
    rows = [details[pid] for pid in page if pid in details]
    total = len(ids)
    extra = {pid: (facts.links[pid], facts.priority[pid] if facts.priority[pid] >= 0 else None) for pid in page}
    if query.compact:
        nodes = []
        compact = MV._compact_rows(conn, rows)
        for row in compact:
            row.extend(extra.get(row[0], (0, None)))
    else:
        people = MV._people(conn, [row[0] for row in rows])
        nodes = [MV._node(row, people, store.mode) for row in rows]
        for node in nodes:
            node['links'], node['priority'] = extra.get(node['id'], (0, None))
        compact = None
    result = MV._response(store, query, (0., 0., 1., 1.), nodes, [], [], total, total, False)
    if query.compact:
        result.update(compact=True, columns=MV.COMPACT_COLUMNS + EXTRA_COLUMNS, rows=compact,
                      shown=len(rows), hidden=total - len(rows))
    result['cohort'] = True
    result['cohort_reset'] = reset
    result['lens'] = {'id': lens, 'title': LENSES[lens][0], 'eligible': total}
    result['next_cursor'] = json.dumps([start + len(page), page[-1] if page else 0, revision]) if more and page else None
    return result
