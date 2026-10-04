"""Recorded network evidence and owner context, independent of scoring services."""

import db
import owner
import owner_relationships

from .common import POSITIVE_SQL, chunks

def me_handle(conn):
    return owner_relationships.owner_handle(conn)


def edges_of(conn, pid):
    return [dict(r) for r in conn.execute('SELECT seed, direction, observed_at FROM current_edges WHERE person_id=? ORDER BY seed, direction', (pid,))]


def edge_history_of(conn, pid):
    """All discovered links with their latest evidence; never use this for scoring."""
    return [dict(r) for r in conn.execute('SELECT e.seed,e.direction,e.first_seen,v.observed_at,v.checked_at,'
            "CASE WHEN v.active=1 THEN 'observed' WHEN v.active=0 THEN 'absent' ELSE 'unverified' END AS state "
            'FROM edges e LEFT JOIN edge_evidence v ON v.seed=e.seed AND v.person_id=e.person_id '
            'AND v.direction=e.direction WHERE e.person_id=? ORDER BY e.seed,e.direction', (pid,))]


def network_context(conn, pids, me=None):
    """Network signals per person for the prefilter and the LLM packet:
    seeds + direction, lists count, link to Michael (is_me seed), seed yield learned from marks (smoothed good+client / marked),
    and how many of the person's seeds are people Michael marked good/client."""
    pids = list(dict.fromkeys(pids))
    if not pids:
        return {}
    me = me if me is not None else me_handle(conn)
    out = {p: {'seeds': [], 'lists': 0, 'me': None, 'seed_yield': None, 'seed_marked': 0, 'client_seeds': 0, 'followers': None}
           for p in pids}
    for chunk in chunks(pids):
        for r in conn.execute(f"SELECT id, followers FROM people WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            out[r['id']]['followers'] = r['followers']
        for e in conn.execute(f"SELECT person_id, seed, direction FROM current_edges WHERE person_id IN ({','.join('?' * len(chunk))}) "
                              'ORDER BY seed, direction', chunk):
            out[e['person_id']]['seeds'].append((e['seed'], e['direction']))
    # The old aggregation read every observed edge for every list page, even
    # though the page usually touches only a few seeds. Limit the aggregate to
    # the seeds of these people; a marked person outside them cannot affect the
    # result. Chunking also stays within SQLite's variable limit.
    relevant = sorted({seed for n in out.values() for seed, _ in n['seeds']})
    yields = {}
    good_handles = set()
    known_handles = set()
    members_ready = db.get_setting(conn, 'map_seed_member_v1', False) and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_member_%'").fetchone()[0] == 6 and conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_seed_degree_%'").fetchone()[0] == 2
    # Start with explicit feedback, then look up memberships. Driving this
    # join by seed scans an entire audience for every fifty-person page.
    # Unary + keeps the source filter out of the index lookup: fetch each
    # marked person's memberships once, rather than seek once per source.
    for chunk in chunks(relevant):
        slots = ','.join('?' * len(chunk))
        membership = 'map_seed_member' if members_ready else 'current_edges'
        distinct = '' if members_ready else 'DISTINCT '
        for r in conn.execute(
                f"SELECT e.seed, count({distinct}CASE WHEN m.status IN {POSITIVE_SQL} THEN e.person_id END), "
                f"count({distinct}e.person_id) FROM ({owner.feedback_marks_sql()}) m "
                f"CROSS JOIN {membership} e ON e.person_id=m.person_id "
                f"WHERE m.status IS NOT NULL AND +e.seed IN ({slots}) GROUP BY e.seed", chunk):
            yields[r[0]] = (r[1], r[2])
        good_handles.update(r[0] for r in conn.execute(
            f"SELECT p.handle FROM people p JOIN ({owner.feedback_marks_sql()}) m ON m.person_id=p.id "
            f"WHERE m.status IN {POSITIVE_SQL} AND p.handle IN ({slots})", chunk))
        known_handles.update(r[0] for r in conn.execute(
            f"SELECT p.handle FROM people p JOIN owner_context oc ON oc.person_id=p.id "
            f"WHERE oc.relationships!='[]' AND p.handle IN ({slots})", chunk))
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
        n['known_seeds'] = len((others & known_handles) - good_handles)
    return out


def network_snapshot(conn, pids, me=None):
    """Capture the evidence used by an existing verdict before a graph/mark change."""
    pids = list(dict.fromkeys(pids))
    nets = network_context(conn, pids, me)
    out = {}
    for pid in pids:
        row = conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()
        if row:
            out[pid] = {'person': with_owner(conn, dict(row)), 'edges': edges_of(conn, pid), 'net': nets[pid]}
    return out


def with_owner(conn, p):
    """Hydrate recorded owner context. External prompt builders control private-note exclusion."""
    m = conn.execute('SELECT status, note FROM marks WHERE person_id=?', (p['id'],)).fetchone()
    p['status'], p['note'] = (m['status'], m['note']) if m else (None, None)
    p['manual_tags'] = sorted(r[0] for r in conn.execute("SELECT tag FROM tags WHERE person_id=? AND source='manual'", (p['id'],)))
    owner.hydrate(conn, [p])
    return p
