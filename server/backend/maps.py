"""MapService owns one workspace and its request dependencies."""

from datetime import datetime
import connection_graph
import map_scale
import map_view
import db
import owner
import owner_relationships
import tag_projection
import qualify
from .common import (
    Bad,
    judge,
    chunks,
    csv,
    qint,
    read_snapshot,
    data_rev,
    OWNER_HANDLE,
    LISTS,
    TAG_ORDER,
    MAP_TAGS,
    SEED_LINKS_TOP,
)
from .evidence import network_context
from .queries import lead_filter


class MapService:

    def __init__(self, config, cache):
        self.config = config
        self.cache = cache

    def seed_links(self, conn, cacheable=None):
        # A caller's uncommitted revision can be reused after rollback. API reads
        # pass an explicit committed-snapshot flag from before cached() opens one.
        if cacheable is None:
            cacheable = not conn.in_transaction
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        # Closed in-memory connections can reuse a Python object ID. They have
        # no durable workspace identity and must bypass this secondary cache.
        cacheable = bool(cacheable and path)
        # Overlap means people currently observed in both source lists. The
        # transactionally maintained distinct membership table avoids rejoining
        # every edge to evidence on each exact-revision refresh.
        members_ready = db.get_setting(conn, 'map_seed_member_v1', False) and conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_member_%'").fetchone()[0] == 6 and conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_seed_degree_%'").fetchone()[0] == 2
        membership_rev = db.get_setting(conn, 'map_membership_rev', None)
        revision_ready = members_ready and membership_rev is not None and conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_overlap_rev_%'").fetchone()[0] == 3
        revision = ('membership', membership_rev) if revision_ready else ('all', data_rev(conn))
        key = (path or ('memory', id(conn)), revision)
        cached = self.cache.seed_links[0] if cacheable else None
        if cached and cached[0] == key:
            return cached[2]
        # Aggregate overlap in SQLite and return only the top pairs. This avoids
        # grouping every person's source list into strings or materializing every
        # member in Python on each exact-revision refresh.
        membership = 'map_seed_member' if members_ready else '(SELECT DISTINCT person_id,seed FROM current_edges)'
        # Without this hint SQLite scans the seed-first secondary index, making
        # each person lookup jump across the entire membership table.
        outer = 'map_seed_member a NOT INDEXED' if members_ready else f'{membership} a'
        top = conn.execute(f'SELECT a.seed,b.seed,count(*) AS shared FROM {outer} '
                           f'JOIN {membership} b ON b.person_id=a.person_id AND b.seed>a.seed '
                           'GROUP BY a.seed,b.seed ORDER BY shared DESC,a.seed,b.seed LIMIT ?', (SEED_LINKS_TOP,))
        links = [{'source': f's:{a}', 'target': f's:{b}', 'shared': n} for a, b, n in top]
        if cacheable:
            self.cache.seed_links[0] = (key, datetime.now().timestamp(), links)
        return links

    def api_map(self, conn, q, b):
        # cached() reads the revision and computes the complete response in one
        # snapshot. Capturing the caller's state first keeps rollback-only reads
        # out of both caches while allowing committed UI polls to reuse the result.
        committed = not conn.in_transaction
        if (qint(q, 'limit') or 400) > 10000:
            with read_snapshot(conn):
                if q.get('rev', [''])[0] == str(data_rev(conn)):
                    return {'unchanged': True, 'rev': data_rev(conn)}
                return self.map_graph(conn, q)
        return self.cache.cached(conn, 'map', q, lambda: dict(self.map_graph(conn, q), seed_links=self.seed_links(conn, cacheable=committed)))

    def _map_database(self, conn):
        return conn.execute('PRAGMA database_list').fetchone()[2]

    def api_map_view(self, conn, q, b):
        with read_snapshot(conn):
            return map_view.view(conn, self._map_database(conn), q)

    def api_map_search(self, conn, q, b):
        with read_snapshot(conn):
            return map_view.search(conn, self._map_database(conn), q)

    def api_map_edges(self, conn, q, b):
        with read_snapshot(conn):
            return map_view.edges(conn, q, self._map_database(conn))

    def api_map_overview(self, conn, q, b):
        where, args = lead_filter({k: v for k, v in q.items() if k != 'seed'})
        for seed in dict.fromkeys(map(db.norm_handle, csv(q, 'seed'))):
            where.append('p.id IN (SELECT person_id FROM current_edges WHERE seed=?)')
            args.append(seed)
        return self.cache.cached(conn, 'map-overview', q, lambda: map_scale.overview(conn, data_rev(conn), q, where, args))

    def api_connections(self, conn, q, b):
        """Inspect observed connections independently of qualification and map display caps."""
        limit = qint(q, 'limit') if 'limit' in q else 20
        if limit is None or not 1 <= limit <= 100:
            raise Bad('limit must be between 1 and 100')
        try:
            return connection_graph.compare(conn, q.get('source', [''])[0], q.get('target', [''])[0], limit)
        except ValueError as exc:
            raise Bad(str(exc)) from exc

    def map_graph(self, conn, q):
        requested_limit = qint(q, 'limit') or 400
        if requested_limit > 10000 and not q.get('q', [''])[0].strip():
            where, args = lead_filter({k: v for k, v in q.items() if k not in ('seed', 'q')})
            for seed in dict.fromkeys(map(db.norm_handle, csv(q, 'seed'))):
                where.append('p.id IN (SELECT person_id FROM current_edges WHERE seed=?)')
                args.append(seed)
            return map_scale.graph(conn, q, where, args, data_rev(conn), min(100000, requested_limit))
        limit = min(10000, max(10, qint(q, 'limit') or 400))
        try:
            search = connection_graph.map_search(q.get('q', [''])[0])
        except ValueError as exc:
            raise Bad(str(exc)) from exc
        if search:
            limit = min(limit, 100)
        # The general lead seed filter intentionally includes discovery history. On the map,
        # a seed filter must mean an observed connection to that seed.
        where, args = lead_filter({k: v for k, v in q.items() if k not in ('seed', 'q')}, status_default=not bool(search))
        if search:
            where.append(search['where'])
            args.extend(search['args'])
        for seed in dict.fromkeys(map(db.norm_handle, csv(q, 'seed'))):
            where.append('p.id IN (SELECT person_id FROM current_edges WHERE seed=?)')
            args.append(seed)
        # Older databases, or an interrupted backfill without the ready marker,
        # retain the exact read-through query until db.init repairs the summary.
        people_ready = db.get_setting(conn, 'map_people_present_v1', False) and conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_people_%'").fetchone()[0] == 3
        summary_ready = people_ready and db.get_setting(conn, 'map_person_degree_v1', False) and conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_degree_%'").fetchone()[0] == 6
        if summary_ready:
            where = [part.replace(LISTS, 'd.degree') for part in where]
        sources_ready = db.get_setting(conn, 'map_source_handles_v1', False) and conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_sources_%'").fetchone()[0] == 6
        rank_ready = summary_ready and sources_ready and people_ready and db.get_setting(conn, 'map_rank_v1', False) and conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_rank_%'").fetchone()[0] == 6
        excluded = ('p.handle NOT IN (SELECT handle FROM map_source_handles)' if sources_ready else
                    'p.handle NOT IN (SELECT handle FROM seeds UNION SELECT seed FROM edges)')
        cond = ' AND '.join([excluded] + where)
        # Materialize only ranking fields for the full match set. Display fields
        # are fetched after the limit; otherwise millions of names, notes and
        # reasons are copied into SQLite's temporary sort table on every refresh.
        rank_column = f",{search['rank']} AS search_rank" if search else ''
        if search:
            args = [*search['rank_args'], *args]
        if summary_ready:
            base = (f'SELECT p.id, v.score, coalesce(d.degree,0) AS degree{rank_column} ' +
                    ('FROM people p LEFT JOIN map_person_degree d ON p.id=d.person_id ' if search else
                     'FROM map_person_degree d JOIN people p ON p.id=d.person_id ')
                    + f'LEFT JOIN verdicts v ON v.person_id=p.id LEFT JOIN marks m ON m.person_id=p.id WHERE {cond}')
        else:
            base = (f'SELECT p.id, v.score, count(DISTINCT e.seed) AS degree{rank_column} '
                    f"FROM people p {'LEFT JOIN' if search else 'JOIN'} current_edges e ON e.person_id=p.id "
                    f'LEFT JOIN map_person_degree d ON d.person_id=p.id '
                    f'LEFT JOIN verdicts v ON v.person_id=p.id LEFT JOIN marks m ON m.person_id=p.id WHERE {cond} GROUP BY p.id')
        by_score = 'ORDER BY ' + ('search_rank, ' if search else '') + 'score IS NULL, score DESC, degree DESC, id LIMIT ?'
        multi_n = 0 if search or q.get('scope', ['leads'])[0] == 'all' else limit * 3 // 5  # scope=leads: people in several lists first
        # The common unfiltered overview can follow two exact ranking indexes and
        # stop after its display limit. Other filters still use the general query.
        # The UI always adds its local date; it changes only follow-up filters.
        simple = rank_ready and set(q) <= {'scope', 'limit', 'today'}
        if simple:
            source_cond = 'd.hidden=0 AND ' + excluded
            source_join = 'FROM map_person_degree d JOIN people p ON p.id=d.person_id '
            total = conn.execute('SELECT count(*) FROM map_person_degree WHERE hidden=0').fetchone()[0]
            # Force the tiny source registry outermost; SQLite otherwise sometimes
            # scans every ranked person before joining its handle.
            total -= conn.execute('SELECT count(*) FROM map_source_handles h CROSS JOIN people p '
                                  'CROSS JOIN map_person_degree d WHERE p.handle=h.handle '
                                  'AND d.person_id=p.id AND d.hidden=0').fetchone()[0]
            rows = []
            if multi_n:
                rows.extend(conn.execute('SELECT 0 AS part,d.person_id AS id,d.score,d.degree '
                                         f'{source_join} WHERE {source_cond} AND d.degree>=2 '
                                         'ORDER BY d.degree DESC,d.score DESC,d.person_id LIMIT ?', (multi_n,)).fetchall())
            rows.extend(conn.execute('SELECT 1 AS part,d.person_id AS id,d.score,d.degree '
                                     f'{source_join} WHERE {source_cond} '
                                     'ORDER BY d.score IS NULL,d.score DESC,d.degree DESC,d.person_id LIMIT ?',
                                     (limit,)).fetchall())
        else:
            rows = conn.execute(f"""WITH b AS MATERIALIZED ({base})
                SELECT (SELECT count(*) FROM b) AS total, picked.* FROM (
                  SELECT * FROM (SELECT 0 AS part, * FROM b WHERE degree>=2 ORDER BY degree DESC, score DESC, id LIMIT ?)
                  UNION ALL SELECT * FROM (SELECT 1 AS part, * FROM b {by_score})
                ) picked""", (*args, multi_n, limit)).fetchall()
            total = rows[0]['total'] if rows else 0
        multi = [r for r in rows if r['part'] == 0]
        seen = {r['id'] for r in multi}
        picked = multi + [r for r in rows if r['part'] == 1 and r['id'] not in seen][:limit - len(multi)]
        display = {}
        for chunk in chunks([r['id'] for r in picked]):
            marks = ','.join('?' * len(chunk))
            for row in conn.execute('SELECT p.id,p.handle,p.name,p.pic_file,p.followers,'
                                    'v.tier,v.score,v.content_fit,v.reason,m.status,m.note '
                                    'FROM people p LEFT JOIN verdicts v ON v.person_id=p.id '
                                    'LEFT JOIN marks m ON m.person_id=p.id '
                                    f'WHERE p.id IN ({marks})', chunk):
                display[row['id']] = row
        people = [dict(display[r['id']], degree=r['degree']) for r in picked]
        members_ready = db.get_setting(conn, 'map_seed_member_v1', False) and conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_member_%'").fetchone()[0] == 6 and conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'map_seed_degree_%'").fetchone()[0] == 2
        source_from = 'map_source_handles' if sources_ready else '(SELECT handle FROM seeds UNION SELECT seed FROM edges)'
        seed_degree = ('coalesce(md.degree,0)' if members_ready else
                       '(SELECT count(DISTINCT e.person_id) FROM current_edges e WHERE e.seed=s.handle)')
        degree_join = 'LEFT JOIN map_seed_degree md ON md.seed=s.handle ' if members_ready else ''
        seeds = conn.execute(f'SELECT s.handle, {seed_degree} AS degree, p.id AS pid, '
                             'p.name, p.pic_file, p.followers, v.tier, v.score, m.status, m.note, coalesce(sd.is_me, 0) AS is_me '
                             f'FROM {source_from} s LEFT JOIN seeds sd ON sd.handle=s.handle '
                             f'{degree_join}LEFT JOIN people p ON p.handle=s.handle LEFT JOIN verdicts v ON v.person_id=p.id '
                             'LEFT JOIN marks m ON m.person_id=p.id').fetchall()
        node_of = {r['id']: f"p:{r['id']}" for r in people}
        node_of.update((s['pid'], f"s:{s['handle']}") for s in seeds if s['pid'])
        links, seeds_of, tags, alltags = [], {}, {}, {}
        map_owners = {r['id']: dict(r) for r in people}
        map_owners.update({s['pid']: dict(s, id=s['pid']) for s in seeds if s['pid']})
        raw_tags = {}
        for chunk in chunks(node_of):
            marks = ','.join('?' * len(chunk))
            for e in conn.execute(f'SELECT e.*,v.active,v.observed_at,v.checked_at FROM edges e '
                                  'LEFT JOIN edge_evidence v ON v.seed=e.seed AND v.person_id=e.person_id AND v.direction=e.direction '
                                  f'WHERE e.person_id IN ({marks}) ORDER BY e.seed,e.direction', chunk):
                links.append({'source': f"s:{e['seed']}", 'target': node_of[e['person_id']], 'direction': e['direction'],
                              'state': 'observed' if e['active'] == 1 else 'absent' if e['active'] == 0 else 'unverified',
                              'observed_at': e['observed_at'], 'checked_at': e['checked_at']})
                if e['active'] == 1 and e['seed'] not in seeds_of.setdefault(e['person_id'], []):
                    seeds_of[e['person_id']].append(e['seed'])
            for t in conn.execute(f'SELECT t.* FROM ({tag_projection.relation()}) t WHERE t.person_id IN ({marks}) ORDER BY t.person_id, {TAG_ORDER}', chunk):
                raw_tags.setdefault(t['person_id'], []).append(dict(t))
        owner.hydrate(conn, list(map_owners.values()))
        for pid, person in map_owners.items():
            raw = raw_tags.get(pid, [])
            person['manual_tags'] = [t['tag'] for t in raw if t['source'] == 'manual']
            visible = owner.visible_tags(person, raw)
            alltags[pid] = {t['tag'] for t in visible}
            tags[pid] = [t['tag'] for t in visible[:MAP_TAGS]]
        nodes = [{'id': f"s:{s['handle']}", 'kind': 'seed', 'label': s['handle'], 'name': s['name'], 'tier': s['tier'], 'score': s['score'],
                  'pic': f"/img/{s['pid']}" if s['pic_file'] else None, 'degree': s['degree'], 'followers': s['followers'],
                  'status': s['status'], 'lists': len(seeds_of.get(s['pid'], [])), 'tags': tags.get(s['pid'], []),
                  'seeds': seeds_of.get(s['pid'], []), 'is_me': bool(s['is_me']), 'pid': s['pid'], 'note': s['note'] or None} for s in seeds]
        nets = network_context(conn, [r['id'] for r in people]) if people else {}
        nodes += [{'id': f"p:{r['id']}", 'kind': 'lead', 'label': r['handle'], 'handle': r['handle'], 'name': r['name'],
                   'tier': r['tier'] or 'unread', 'fit': ('strong' if r['content_fit'] >= 70 else 'good' if r['content_fit'] >= 45 else 'weak') if r['content_fit'] is not None else 'unread',
                   'score': r['score'], 'business_fit': round(r['content_fit']) if r['content_fit'] is not None else None,
                   'connection_strength': qualify.network_strength(nets[r['id']]), 'reason': r['reason'],
                   'relationship': nets[r['id']]['me'],
                   'tags': tags.get(r['id'], []), 'judge': judge(alltags.get(r['id'], set())),
                   'pic': f"/img/{r['id']}" if r['pic_file'] else None, 'degree': r['degree'], 'lists': r['degree'],
                   'status': r['status'], 'note': r['note'] or None, 'followers': r['followers'], 'seeds': seeds_of.get(r['id'], [])} for r in people]
        owner_links = owner_relationships.facts(conn, [r['id'] for r in people] + [s['pid'] for s in seeds if s['pid']])
        for node in nodes:
            pid = node.get('pid') if node['kind'] == 'seed' else int(node['id'].split(':', 1)[1])
            node.update(owner_links.get(pid, {'owner_relationship': None, 'relationship_owner': OWNER_HANDLE, 'relationship_evidence': []}))
            facts = map_owners.get(pid, {})
            node['relationships'] = owner.relationships(facts)
            node['familiarity'] = facts.get('familiarity')
            node['owner_status'] = owner.owner_status(facts)
            node.update(owner.owner_recommendation(facts, node))
        return {'nodes': nodes, 'links': links, 'total': total, 'limit': limit, 'search_query': search['text'] if search else None, 'rev': data_rev(conn)}
