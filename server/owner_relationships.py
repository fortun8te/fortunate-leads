"""Observed Instagram directions to the canonical owner, independent of lead status."""
OWNER = 'fortun8te'
OWNER_SQL = "coalesce((SELECT handle FROM seeds WHERE is_me=1 ORDER BY (lower(handle)='fortun8te') DESC,handle LIMIT 1),'fortun8te')"


def owner_handle(conn):
    return conn.execute('SELECT ' + OWNER_SQL).fetchone()[0]


def facts(conn, ids):
    owner = owner_handle(conn)
    ids = list(dict.fromkeys(ids))
    result = {pid: {'owner_relationship': None, 'relationship_owner': owner,
                    'relationship_evidence': []} for pid in ids}
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        slots = ','.join('?' for _ in batch)
        for edge in conn.execute(f'SELECT person_id,seed,direction,observed_at FROM current_edges '
                                 f'WHERE seed=? AND person_id IN ({slots}) ORDER BY direction', [owner, *batch]):
            result[edge['person_id']]['relationship_evidence'].append({
                'seed': edge['seed'], 'direction': edge['direction'], 'observed_at': edge['observed_at']})
    for value in result.values():
        directions = {edge['direction'] for edge in value['relationship_evidence']}
        value['owner_relationship'] = ('mutual' if directions == {'followers', 'following'} else
                                       'follows' if 'followers' in directions else
                                       'followed' if 'following' in directions else None)
    return result


def filter_sql(value):
    directions = {'follows': ['followers'], 'followed': ['following'], 'mutual': ['followers', 'following']}
    if value not in directions:
        raise ValueError('relationship must be follows, followed or mutual')
    return ["EXISTS (SELECT 1 FROM current_edges owner_edge WHERE owner_edge.person_id=p.id "
            f"AND owner_edge.seed=({OWNER_SQL}) AND owner_edge.direction=?)" for _ in directions[value]], [
                direction for direction in directions[value]]
