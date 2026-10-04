"""Compact, exact-identity map reads for dense views.

The point response contains at most ``limit`` real people. Source buckets in
``overview`` overlap by design and are never presented as unique people.
"""

import json


MAX_POINTS = 100_000
CHUNK = 800


def _condition(where):
    # lead_filter's list-count expression is exact but repeats an edge query for
    # every candidate. The maintained degree is the same distinct current count.
    from backend.common import LISTS
    parts = [part.replace(LISTS, 'd.degree').replace("coalesce(m.status,'')!='no'", 'd.hidden=0')
             for part in where]
    parts.append('NOT EXISTS (SELECT 1 FROM map_source_handles h WHERE h.handle=p.handle)')
    return ' AND '.join(parts)


def _base(condition):
    return (' FROM map_person_degree d JOIN people p ON p.id=d.person_id '
            'LEFT JOIN verdicts v ON v.person_id=p.id '
            'LEFT JOIN marks m ON m.person_id=p.id WHERE ' + condition)


def _source_nodes(conn):
    rows = conn.execute('SELECT h.handle,p.id AS pid,p.name,p.pic_file,p.followers,'
                        'coalesce(md.degree,0) AS degree,coalesce(s.is_me,0) AS is_me '
                        'FROM map_source_handles h LEFT JOIN seeds s ON s.handle=h.handle '
                        'LEFT JOIN map_seed_degree md ON md.seed=h.handle '
                        'LEFT JOIN people p ON p.handle=h.handle ORDER BY h.handle').fetchall()
    return [{'id': 's:' + r['handle'], 'kind': 'seed', 'label': r['handle'],
             'name': r['name'], 'pid': r['pid'], 'pic': f"/img/{r['pid']}" if r['pic_file'] else None,
             'followers': r['followers'], 'degree': r['degree'], 'lists': 0,
             'is_me': bool(r['is_me']), 'seeds': []} for r in rows]


def _ranked_ids(conn, base, args, limit, scope):
    # Both passes have a hard SQL LIMIT. SQLite scans the ranked index even for
    # a filtered view; Python never materializes the millions of candidates.
    chosen = []
    seen = set()
    if scope == 'leads':
        multi = limit * 3 // 5
        for r in conn.execute('SELECT d.person_id' + base + ' AND d.degree>=2 '
                              'ORDER BY d.degree DESC,d.score DESC,d.person_id LIMIT ?',
                              (*args, multi)):
            chosen.append(r[0]); seen.add(r[0])
    for r in conn.execute('SELECT d.person_id' + base + ' '
                          'ORDER BY d.score IS NULL,d.score DESC,d.degree DESC,d.person_id LIMIT ?',
                          (*args, limit)):
        if r[0] not in seen:
            chosen.append(r[0]); seen.add(r[0])
            if len(chosen) == limit:
                break
    return chosen


def _people(conn, ids):
    by_id = {}
    owner_row = conn.execute("SELECT handle FROM seeds WHERE is_me=1 ORDER BY (lower(handle)='fortun8te') DESC,handle LIMIT 1").fetchone()
    relationship_owner = owner_row[0] if owner_row else 'fortun8te'
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start:start + CHUNK]
        slots = ','.join('?' for _ in chunk)
        for r in conn.execute('SELECT p.id,p.handle,p.name,p.pic_file,p.followers,'
                              'd.degree,d.score,v.tier,v.content_fit,m.status,oc.relationships,oc.familiarity '
                              'FROM people p JOIN map_person_degree d ON d.person_id=p.id '
                              'LEFT JOIN verdicts v ON v.person_id=p.id '
                              'LEFT JOIN marks m ON m.person_id=p.id '
                              'LEFT JOIN owner_context oc ON oc.person_id=p.id '
                              f'WHERE p.id IN ({slots})', chunk):
            fit = r['content_fit']
            try:
                relationships = json.loads(r['relationships'] or '[]')
            except (ValueError, TypeError):
                relationships = []
            if r['status'] == 'client' and 'client' not in relationships:
                relationships.append('client')
            by_id[r['id']] = {'id': f"p:{r['id']}", 'kind': 'lead', 'label': r['handle'],
                              'handle': r['handle'], 'name': r['name'], 'tier': r['tier'] or 'unread',
                              'fit': 'strong' if fit is not None and fit >= 70 else
                                     'good' if fit is not None and fit >= 45 else
                                     'weak' if fit is not None else 'unread',
                              'score': r['score'], 'followers': r['followers'],
                              'degree': r['degree'], 'lists': r['degree'], 'seeds': [],
                              'pic': f"/img/{r['id']}" if r['pic_file'] else None,
                              'status': r['status'], 'relationships': relationships,
                              'familiarity': r['familiarity'], 'owner_relationship': None,
                              'relationship_owner': relationship_owner}
        for r in conn.execute(f'SELECT person_id,seed FROM map_seed_member WHERE person_id IN ({slots})', chunk):
            person = by_id.get(r['person_id'])
            if person is not None:
                person['seeds'].append(r['seed'])
        directions = {}
        for r in conn.execute(f'SELECT person_id,direction FROM current_edges WHERE seed=? AND person_id IN ({slots})',
                              [relationship_owner, *chunk]):
            directions.setdefault(r['person_id'], set()).add(r['direction'])
        for pid, found in directions.items():
            person = by_id.get(pid)
            if person is not None:
                person['owner_relationship'] = ('mutual' if found == {'followers', 'following'} else
                                                'follows' if 'followers' in found else 'followed')
    return [by_id[pid] for pid in ids if pid in by_id]


def graph(conn, q, where, args, rev, limit):
    """Return up to 100k real person nodes and no inferred relationship lines."""
    limit = max(1, min(MAX_POINTS, int(limit)))
    condition = _condition(where)
    base = _base(condition)
    total = conn.execute('SELECT count(*)' + base, args).fetchone()[0]
    ids = _ranked_ids(conn, base, args, limit, q.get('scope', ['leads'])[0])
    nodes = _source_nodes(conn)
    nodes.extend(_people(conn, ids))
    return {'nodes': nodes, 'links': [], 'seed_links': [], 'total': total,
            'limit': limit, 'rev': rev, 'dense': True}


def overview(conn, rev, q=None, where=(), args=()):
    """Exact distinct total and overlapping current source membership counts."""
    condition = _condition(where)
    total = conn.execute('SELECT count(*)' + _base(condition), args).fetchone()[0]
    if where:
        rows = conn.execute('SELECT sm.seed,count(*) AS n FROM map_seed_member sm '
                            'JOIN map_person_degree d ON d.person_id=sm.person_id '
                            'JOIN people p ON p.id=d.person_id '
                            'LEFT JOIN verdicts v ON v.person_id=p.id '
                            'LEFT JOIN marks m ON m.person_id=p.id WHERE ' + condition +
                            ' GROUP BY sm.seed ORDER BY n DESC,sm.seed', args).fetchall()
    else:
        # A source profile can itself appear in another source's list. The map
        # displays source identities once as anchors, so exclude them from each
        # bucket while retaining the maintained count for everyone else.
        rows = conn.execute('SELECT md.seed,md.degree-coalesce(x.n,0) AS n FROM map_seed_degree md '
                            'LEFT JOIN (SELECT sm.seed,count(*) AS n FROM map_source_handles h '
                            'CROSS JOIN people p ON p.handle=h.handle '
                            'CROSS JOIN map_seed_member sm ON sm.person_id=p.id GROUP BY sm.seed) x '
                            'ON x.seed=md.seed ORDER BY n DESC,md.seed').fetchall()
    return {'total': total, 'buckets': [{'id': 'source:' + r['seed'],
                                        'label': r['seed'], 'seed': r['seed'], 'count': r['n']}
                                       for r in rows],
            'overlapping_lists': True, 'limit': MAX_POINTS, 'rev': rev}
