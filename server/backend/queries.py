"""queries keep read and write ownership explicit."""

import re
import db
import owner
import owner_relationships
import tag_projection
import qualify
import workflows
from .common import (
    status_in,
    Bad,
    NotFound,
    csv,
    qint,
    STATUSES,
    LISTS,
    PEOPLE_FROM,
    LEAD_SQL,
    NOT_ME,
    TAG_ORDER,
)
from .evidence import network_context


def lead_rows(conn, rows):
    ids = [r['id'] for r in rows]
    if not ids:
        return []
    marks = ','.join('?' * len(ids))
    nets = network_context(conn, ids)
    tags, via, history_via, connection_edges = {}, {}, {}, {}
    followups = {r['person_id']: {k: r[k] for k in ('due_on', 'note', 'completed_at', 'updated_at')} for r in conn.execute(f'SELECT * FROM followups WHERE person_id IN ({marks})', ids)}
    for t in conn.execute(f'SELECT * FROM ({tag_projection.relation()}) t WHERE t.person_id IN ({marks}) ORDER BY {TAG_ORDER}', ids):
        tags.setdefault(t['person_id'], []).append({'tag': t['tag'], 'grp': t['grp'], 'source': t['source']})
    for e in conn.execute(f'SELECT person_id, seed, direction, observed_at FROM current_edges WHERE person_id IN ({marks}) ORDER BY seed,direction', ids):
        sources = via.setdefault(e['person_id'], [])
        if e['seed'] not in sources:
            sources.append(e['seed'])
        connection_edges.setdefault(e['person_id'], []).append({
            'seed': e['seed'], 'direction': e['direction'], 'observed_at': e['observed_at']})
    for e in conn.execute(f'SELECT DISTINCT person_id, seed FROM edges WHERE person_id IN ({marks}) ORDER BY seed', ids):
        history_via.setdefault(e['person_id'], []).append(e['seed'])
    result = [{'id': r['id'], 'handle': r['handle'], 'name': r['name'], 'pic': f"/img/{r['id']}" if r['pic_file'] else None,
             'bio': r['bio'], 'website': r['website'], 'followers': r['followers'], 'following': r['following'],
             'posts': r['posts'], 'tier': r['tier'] or 'unread', 'score': r['score'],
             'business_fit': round(r['content_fit']) if r['content_fit'] is not None else None,
             'connection_strength': qualify.network_strength(nets[r['id']]),
             'relationship': nets[r['id']]['me'],
             'role': r['role'], 'reason': r['reason'],
             'tags': tags.get(r['id'], []), 'via': via.get(r['id'], []), 'lists': r['lists'],
             'connection_edges': connection_edges.get(r['id'], []),
             'history_via': history_via.get(r['id'], []), 'history_lists': len(history_via.get(r['id'], [])),
             'status': r['status'], 'mark_rev': r['mark_rev'],
             'note': r['note'] or None, 'bio_at': r['bio_at'], 'bio_src': r['bio_src'], 'follow_up': followups.get(r['id'])} for r in rows]
    owner.hydrate(conn, result)
    owner_links = owner_relationships.facts(conn, ids)
    for person in result:
        person.update(owner_links[person['id']])
        person['manual_tags'] = [t['tag'] for t in person['tags'] if t['source'] == 'manual']
        person['owner_status'] = owner.owner_status(person)
        person['reachable'] = True if person['owner_status'] in ('client', 'talking') else None
        person['owner_conflict'] = owner.owner_conflict(person)
        person['tags'] = owner.visible_tags(person, person['tags'])
        person.update(owner.owner_recommendation(person, person))
    return result


def lead_filter(q, status_default=True):
    """Shared by /api/leads, /api/counts, /api/tags (facets) and /api/map. -> (where clauses on p/v/m, args).
    status_default: without a status filter, leave out people marked no."""
    where, args = [], []

    def within(sql, values):  # sql has one {} for the placeholders
        where.append(sql.format(','.join('?' * len(values))))
        args.extend(values)

    relationship = q.get('relationship', [''])[0]
    if relationship:
        try:
            clauses, values = owner_relationships.filter_sql(relationship)
        except ValueError as error:
            raise Bad(str(error)) from None
        where.extend(clauses)
        args.extend(values)

    tiers = csv(q, 'tier')
    if tiers:
        if any(t not in ('hot', 'warm', 'cold', 'unread') for t in tiers):
            raise Bad('bad tier')
        within("coalesce(v.tier,'unread') IN ({})", tiers)
    fits = csv(q, 'fit')
    if fits:
        conditions = {'strong': 'v.content_fit>=70',
                      'good': 'v.content_fit>=45 AND v.content_fit<70',
                      'weak': 'v.content_fit<45',
                      'unread': 'v.content_fit IS NULL'}
        if any(fit not in conditions for fit in fits):
            raise Bad('bad fit')
        where.append('(' + ' OR '.join('(' + conditions[fit] + ')' for fit in dict.fromkeys(fits)) + ')')
    effective_tags = 'SELECT t.person_id FROM (' + tag_projection.relation() + ') t WHERE '
    for t in dict.fromkeys(csv(q, 'tags')):  # all of
        within('p.id IN (' + effective_tags + 't.tag={})', [t])
    if csv(q, 'any'):  # at least one of
        within('p.id IN (' + effective_tags + 't.tag IN ({}))', csv(q, 'any'))
    if csv(q, 'not'):  # none of
        within('p.id NOT IN (' + effective_tags + 't.tag IN ({}))', csv(q, 'not'))
    statuses = csv(q, 'status')
    if any(status_in(s) not in (*STATUSES, 'none', 'all') for s in statuses):
        raise Bad('bad status')
    if not statuses:
        if status_default:
            where.append("coalesce(m.status,'')!='no'")
    elif 'all' not in statuses:
        named = [status_in(s) for s in statuses if s != 'none']
        if any(s not in STATUSES for s in named):
            raise Bad('bad status')
        conds = (['m.status IS NULL'] if 'none' in statuses else []) + (['m.status IN ({})'] if named else [])
        within('(' + ' OR '.join(conds) + ')', named)
    text = q.get('q', [''])[0].strip()
    if text:
        where.append("(p.handle LIKE ? ESCAPE '\\' OR p.name LIKE ? ESCAPE '\\' OR p.bio LIKE ? ESCAPE '\\' OR m.note LIKE ? ESCAPE '\\')")
        like = re.sub(r'([\\%_])', r'\\\1', text)
        args += [f'%{like}%'] * 4
    min_lists = qint(q, 'min_lists') or 0
    if min_lists < 0:
        raise Bad('min_lists must be nonnegative')
    if min_lists > 0:
        where.append(f'{LISTS}>=?')
        args.append(min_lists)
    has_bio = q.get('has_bio', [''])[0].strip()
    if has_bio in ('1', 'true'):
        where.append("coalesce(p.bio,'')!=''")
    elif has_bio in ('0', 'false'):
        where.append("coalesce(p.bio,'')=''")
    elif has_bio:
        raise Bad('has_bio must be 1 or 0')
    for s in dict.fromkeys(map(db.norm_handle, csv(q, 'seed'))):  # discovered through every listed seed, including history
        where.append('p.id IN (SELECT person_id FROM edges WHERE seed=?)')
        args.append(s)
    for key, op in (('followers_min', '>='), ('followers_max', '<=')):
        n = qint(q, key)
        if n is not None:
            if n < 0:
                raise Bad(f'{key} must be nonnegative')
            where.append(f'p.followers {op} ?')
            args.append(n)
    workflows.filters(q, where, args)
    return where, args


def tag_facets(conn, q):
    """Per-tag counts for the filtered set and overall, including current fit labels."""
    (where, args) = lead_filter(q)
    if where == ["coalesce(m.status,'')!='no'"] and (not args):
        import tag_facets as prepared_tag_facets
        prepared = prepared_tag_facets.default(conn)
        if prepared is not None:
            return prepared
    import tag_facets as prepared_tag_facets
    selected = f"SELECT p.id {PEOPLE_FROM} WHERE {' AND '.join([NOT_ME] + where)}"
    prepared = prepared_tag_facets.filtered(conn, selected, args)
    if prepared is not None:
        return prepared
    (counts, totals) = ({}, {})
    for (branch, projected) in enumerate(tag_projection.parts()):
        if where == ["coalesce(m.status,'')!='no'"] and (not args):
            eligible = f"EXISTS(SELECT 1 FROM people p LEFT JOIN marks m ON m.person_id=p.id WHERE p.id=t.person_id AND {NOT_ME} AND coalesce(m.status,'')!='no')"
            for (tag, source, grp, total, n) in conn.execute(f'SELECT t.tag,t.source,min(t.grp),count(*),sum({eligible}) FROM ({projected}) t GROUP BY t.tag,t.source'):
                counts[tag, source] = counts.get((tag, source), 0) + n
                previous = totals.get((tag, source), (grp, 0))
                totals[tag, source] = (grp, previous[1] + total)
            continue
        selected = f"SELECT p.id {PEOPLE_FROM} WHERE {' AND '.join([NOT_ME] + where)}"
        count_sql = f'WITH f AS MATERIALIZED ({selected}) SELECT t.tag,t.source,count(*) FROM f JOIN ({projected}) t ON t.person_id=f.id GROUP BY t.tag,t.source' if branch == 0 else f'SELECT t.tag,t.source,count(*) FROM ({projected}) t WHERE t.person_id IN ({selected}) GROUP BY t.tag,t.source'
        for (tag, source, n) in conn.execute(count_sql, args):
            counts[tag, source] = counts.get((tag, source), 0) + n
        for (tag, source, grp, n) in conn.execute(f'SELECT t.tag,t.source,min(t.grp),count(*) FROM ({projected}) t GROUP BY t.tag,t.source'):
            previous = totals.get((tag, source), (grp, 0))
            totals[tag, source] = (grp, previous[1] + n)
    out = [{'tag': tag, 'grp': grp, 'source': source, 'count': counts.get((tag, source), 0), 'total': total} for ((tag, source), (grp, total)) in totals.items()]
    return sorted(out, key=lambda f: (-f['count'], -f['total'], f['tag'], f['source']))


def counts(conn, q):
    """Tier and status counts inside the shared filter, each ignoring its own dimension (so the choices stay visible).
    none = unmarked, open = everyone but 'no'; total / with_bio: everyone in the database."""
    import lead_rank
    if not any(values[0].strip() for values in q.values() if values):
        indexed = lead_rank.counts(conn, STATUSES)
        if indexed is not None:
            return indexed
    out = dict.fromkeys(('hot', 'warm', 'cold', 'unread', *STATUSES, 'none', 'open'), 0)
    where, args = lead_filter({k: v for k, v in q.items() if k != 'tier'})
    out.update(conn.execute(f"SELECT coalesce(v.tier,'unread'), count(*) {PEOPLE_FROM} WHERE {' AND '.join([NOT_ME] + where)} "
                            'GROUP BY 1', args).fetchall())
    where, args = lead_filter({k: v for k, v in q.items() if k != 'status'}, status_default=False)
    for status, n in conn.execute(f"SELECT m.status, count(*) {PEOPLE_FROM} WHERE {' AND '.join([NOT_ME] + where)} GROUP BY 1", args):
        out[status or 'none'] = out.get(status or 'none', 0) + n
        out['open'] += n if status != 'no' else 0
    out['total'], out['with_bio'] = conn.execute("SELECT count(*), count(nullif(bio,'')) FROM people").fetchone()
    return out


def person_row(conn, pid):
    row = conn.execute(LEAD_SQL + ' WHERE p.id=?', (pid,)).fetchone()
    if not row:
        raise NotFound('not found')
    return row
