"""Closeness order for the lead list: how directly a person is linked to the owner.

Tiers, closest first: follow each other, follows the owner, followed by the owner,
a relationship or familiarity the owner recorded, then everyone else. Inside a tier
the order is shared lists, then followers, then person id, so the order is total and
a page never repeats or skips a person while the list is scrolled.

The people with any direct link are a few thousand out of a six-figure list, so the
fast path ranks only them first and appends the rest in the shared-lists order.
``TIER`` is the same ranking as one expression; it defines the order and is what
``SORTS['closeness']`` uses. The tests compare the fast path against it.
"""
import owner_relationships

OWNER = owner_relationships.OWNER_SQL

# 4 follow each other, 3 follows the owner, 2 followed by the owner, 1 recorded relationship, 0 none.
TIER = (
    "max(coalesce((SELECT CASE sum(CASE e.direction WHEN 'followers' THEN 2 WHEN 'following' THEN 1 ELSE 0 END) "
    f"WHEN 3 THEN 4 WHEN 2 THEN 3 WHEN 1 THEN 2 ELSE 0 END FROM current_edges e WHERE e.person_id=p.id AND e.seed=({OWNER})), 0), "
    "CASE WHEN EXISTS(SELECT 1 FROM owner_context oc WHERE oc.person_id=p.id "
    "AND (oc.relationships!='[]' OR oc.familiarity IS NOT NULL)) THEN 1 ELSE 0 END)"
)

THEN = 'lists DESC, p.followers DESC'

ORDER = f'{TIER} DESC, {THEN}'

# Everyone with a direct link or a recorded relationship.
NEAR = (
    f"SELECT person_id FROM current_edges WHERE seed=({OWNER}) "
    "UNION SELECT person_id FROM owner_context WHERE relationships!='[]' OR familiarity IS NOT NULL"
)


def page(conn, from_sql, where_sql, args, lists_sql, limit, offset):
    """The person ids of one page of the closeness order.

    ``from_sql`` and ``where_sql`` are the list's own FROM and WHERE, so filters apply
    the same way as for every other sort. The page runs through the filtered people with a
    direct link first, then continues with the shared-lists order.
    """
    near_where = f'{where_sql} AND p.id IN ({NEAR})'
    near_count = conn.execute(f'SELECT count(*) {from_sql}{near_where}', args).fetchone()[0]
    ids = []
    if offset < near_count:
        ids = [row[0] for row in conn.execute(
            f'SELECT p.id, {lists_sql} AS lists {from_sql}{near_where} ORDER BY {ORDER}, p.id LIMIT ? OFFSET ?',
            args + [limit, offset])]
    rest = limit - len(ids)
    if rest > 0:
        ids += [row[0] for row in conn.execute(
            f'SELECT p.id, {lists_sql} AS lists {from_sql}{where_sql} AND p.id NOT IN ({NEAR}) '
            f'ORDER BY {THEN}, p.id LIMIT ? OFFSET ?', args + [rest, max(0, offset - near_count)])]
    return ids
