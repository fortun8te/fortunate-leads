"""Read-only, bounded exploration of observed follows, never inferred relationships.

Only seed metadata is loaded globally. People use indexed identity lookups;
observations use source-ID and person indexes. Legacy handle owners are unknown.
"""
import re
from urllib.parse import urlparse

NEIGHBOR_LIMIT = 1000
MIDDLE_EDGE_LIMIT = 5000
MIDDLE_QUERY_LIMIT = 200
PATH_LIMIT = 5000


def _handle(value):
    if not isinstance(value, str):
        raise ValueError('Account selector must be text')
    value = value.strip()
    if '://' in value:
        parsed = urlparse(value)
        if parsed.hostname not in ('instagram.com', 'www.instagram.com'):
            raise ValueError('Use an Instagram profile URL')
        value = parsed.path.strip('/')
    value = value.lstrip('@').lower()
    if not re.fullmatch(r'[a-z0-9_.]{1,30}', value):
        raise ValueError('Invalid Instagram handle')
    return value


def _limit(value, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError('Invalid result limit')
    return value


class _Graph:
    def __init__(self, conn):
        self.conn = conn
        self.seeds = {r['handle'].lower(): dict(r) for r in conn.execute('SELECT * FROM seeds')}
        self.accounts = {}
        self.seed_ids = {}
        self.person_ids = {}
        self.edges = {}
        self.observations_read = 0
        self.query_count = 0
        self.middle_query_count = 0
        self.clipped = False
        for handle in self.seeds:
            self.seed(handle)

    def person(self, row):
        row = dict(row)
        pid, ig = row['id'], row['ig_id']
        key = 'ig:' + str(ig) if ig else 'person:' + str(pid)
        if pid in self.person_ids:
            return self.person_ids[pid]
        self.person_ids[pid] = key
        account = self.accounts.setdefault(key, {'id': key, 'handle': row['handle'], 'name': row.get('name'),
            'person_id': pid, 'ig_id': ig, 'is_me': False, 'is_seed': False, 'identity_basis': 'instagram_id' if ig else 'local_person'})
        account.update(handle=row['handle'], name=row.get('name'), person_id=pid)
        return key

    def seed(self, handle):
        handle = handle.lower()
        if handle in self.seed_ids:
            return self.seed_ids[handle]
        seed = self.seeds.get(handle, {'handle': handle})
        ig = seed.get('ig_id')
        row = self.conn.execute('SELECT * FROM people WHERE ig_id=?', (ig,)).fetchone() if ig else None
        if row is None:
            candidate = self.conn.execute('SELECT * FROM people WHERE handle=?', (handle,)).fetchone()
            # A known different ID proves handle reuse. Keep the old seed distinct.
            if candidate is not None and (not ig and not candidate['ig_id'] or ig and str(candidate['ig_id']) == str(ig)):
                row = candidate
        if row is not None:
            key = self.person(row)
        else:
            key = 'ig:' + str(ig) if ig else 'seed:' + handle
            self.accounts.setdefault(key, {'id': key, 'handle': handle, 'name': None,
                'person_id': None, 'ig_id': ig, 'is_me': False, 'is_seed': False, 'identity_basis': 'instagram_id' if ig else 'handle_fallback'})
        self.seed_ids[handle] = key
        self.accounts[key]['is_seed'] = True
        self.accounts[key]['is_me'] |= bool(seed.get('is_me'))
        return key

    def resolve(self, value):
        if not isinstance(value, str):
            raise ValueError('Account selector must be text')
        value = value.strip()
        if value.startswith('ig:'):
            ig = value[3:]
            if not re.fullmatch(r'[0-9]{1,32}', ig):
                raise ValueError('Invalid Instagram ID')
            if value in self.accounts:
                return value
            row = self.conn.execute('SELECT * FROM people WHERE ig_id=?', (ig,)).fetchone()
            if row is None:
                observed = self.conn.execute('SELECT seed FROM edge_observations WHERE seed_ig_id=? LIMIT 1', (ig,)).fetchone()
                if observed is not None:
                    return self.observed_seed(observed['seed'], ig)
                observed = self.conn.execute('SELECT e.*,p.* FROM edge_observations e JOIN people p ON p.id=e.person_id WHERE e.person_ig_id=? LIMIT 1', (ig,)).fetchone()
                if observed is not None:
                    return self.observed_person(observed)
        elif value.startswith('legacy-person:'):
            pid = value[14:]
            if not re.fullmatch(r'[0-9]{1,18}', pid):
                raise ValueError('Invalid legacy person ID')
            row = self.conn.execute("SELECT e.*,p.* FROM edge_observations e JOIN people p ON p.id=e.person_id WHERE e.person_id=? AND e.person_ig_id='' LIMIT 1", (int(pid),)).fetchone()
            if row is None:
                raise LookupError('Legacy member is not in imported data')
            return self.observed_person(row)
        elif value.startswith('person:'):
            pid = value[7:]
            if not re.fullmatch(r'[0-9]{1,18}', pid):
                raise ValueError('Invalid person ID')
            row = self.conn.execute('SELECT * FROM people WHERE id=?', (int(pid),)).fetchone()
        elif value.startswith('legacy:'):
            handle = _handle(value[7:])
            row = self.conn.execute("SELECT seed FROM edge_observations WHERE seed=? AND seed_ig_id='' LIMIT 1", (handle,)).fetchone()
            if row is None:
                raise LookupError('Legacy source is not in the imported data')
            return self.observed_seed(handle, '')
        elif value.startswith('seed:'):
            handle = _handle(value[5:])
            if handle in self.seed_ids:
                return self.seed_ids[handle]
            row = None
        else:
            handle = _handle(value)
            row = self.conn.execute('SELECT * FROM people WHERE handle=?', (handle,)).fetchone()
            if row is None and handle in self.seed_ids:
                return self.seed_ids[handle]
        if row is None:
            raise LookupError('Account is not in the imported data')
        return self.person(row)

    def aliases(self, keys):
        return ([h for h, k in self.seed_ids.items() if k in keys],
                [p for p, k in self.person_ids.items() if k in keys])

    def ingest(self, rows):
        for row in rows:
            self.observations_read += 1
            seed = self.observed_seed(row['seed'], row['seed_ig_id'])
            person = self.observed_person(row)
            a, b = (seed, person) if row['direction'] == 'following' else (person, seed)
            if a == b:
                continue
            edge = self.edges.setdefault((a, b), {'id': a + '>' + b, 'from': a, 'to': b, 'evidence': []})
            evidence = {k: row[k] for k in ('seed', 'direction', 'seed_ig_id', 'person_ig_id', 'first_seen', 'last_seen')}
            if evidence not in edge['evidence']:
                edge['evidence'].append(evidence)

    def query(self, sql, params, cap):
        self.query_count += 1
        rows = self.conn.execute(sql + ' ORDER BY e.person_id,e.seed COLLATE NOCASE,e.direction,e.seed_ig_id,e.person_ig_id LIMIT ?',
                                 (*params, cap + 1)).fetchall()
        self.clipped |= len(rows) > cap
        self.ingest(rows[:cap])

    def observed_seed(self, handle, ig):
        if not ig:
            key = 'legacy:' + handle.lower()
            self.accounts.setdefault(key, {'id': key, 'handle': handle, 'name': 'Legacy source; owner unknown',
                'person_id': None, 'ig_id': None, 'is_me': False, 'is_seed': True,
                'identity_basis': 'legacy_handle'})
            return key
        key = 'ig:' + str(ig)
        if key not in self.accounts:
            row = self.conn.execute('SELECT * FROM people WHERE ig_id=?', (str(ig),)).fetchone()
            if row is not None:
                self.person(row)
            else:
                self.accounts[key] = {'id': key, 'handle': handle, 'name': 'Historical source; current handle unverified', 'person_id': None,
                    'handle_basis': 'observed_source_handle', 'ig_id': str(ig), 'is_me': False, 'is_seed': True, 'identity_basis': 'instagram_id'}
        return key

    def observed_person(self, row):
        ig, pid = row['person_ig_id'], row['person_id']
        if not ig:
            key = 'legacy-person:' + str(pid)
            self.accounts.setdefault(key, {'id': key, 'handle': row['handle'],
                'name': 'Legacy member; historical identity unknown', 'person_id': pid,
                'ig_id': None, 'is_me': False, 'is_seed': False, 'identity_basis': 'legacy_person',
                'handle_basis': 'current_record_unverified'})
            return key
        if str(row['ig_id']) == str(ig):
            return self.person(row)
        key = 'ig:' + str(ig)
        if key not in self.accounts:
            current = self.conn.execute('SELECT * FROM people WHERE ig_id=?', (str(ig),)).fetchone()
            if current is not None:
                return self.person(current)
            self.accounts[key] = {'id': key, 'handle': 'id:' + str(ig),
                'name': 'Historical member; current handle unknown', 'person_id': None,
                'ig_id': str(ig), 'is_me': False, 'is_seed': False,
                'identity_basis': 'instagram_id', 'handle_basis': 'unavailable'}
        return key

    def neighborhood(self, key):
        if key.startswith('legacy:'):
            self.query("SELECT e.*,p.* FROM edge_observations e JOIN people p ON p.id=e.person_id "
                       "WHERE e.seed=? AND e.seed_ig_id=''", (key[7:],), NEIGHBOR_LIMIT)
        elif key.startswith('ig:'):
            self.query('SELECT e.*,p.* FROM edge_observations e JOIN people p ON p.id=e.person_id '
                       'WHERE e.seed_ig_id=?', (key[3:],), NEIGHBOR_LIMIT)
        if key.startswith('ig:'):
            self.query('SELECT e.*,p.* FROM edge_observations e JOIN people p ON p.id=e.person_id '
                       'WHERE e.person_ig_id=?', (key[3:],), NEIGHBOR_LIMIT)
        elif key.startswith('legacy-person:'):
            self.query("SELECT e.*,p.* FROM edge_observations e JOIN people p ON p.id=e.person_id "
                       "WHERE e.person_id=? AND e.person_ig_id=''", (int(key[14:]),), NEIGHBOR_LIMIT)

    def direct(self, source, target):
        for a, b in ((source, target), (target, source)):
            if b.startswith('ig:'):
                member_where, member_params = 'e.person_ig_id=?', (b[3:],)
            elif b.startswith('legacy-person:'):
                member_where, member_params = "e.person_id=? AND e.person_ig_id=''", (int(b[14:]),)
            else:
                continue
            if a.startswith('ig:'):
                source_where, source_params = 'e.seed_ig_id=?', (a[3:],)
            elif a.startswith('legacy:'):
                source_where, source_params = "e.seed=? AND e.seed_ig_id=''", (a[7:],)
            else:
                continue
            for direction in ('followers', 'following'):
                self.query('SELECT e.*,p.* FROM edge_observations e JOIN people p ON p.id=e.person_id WHERE '
                           + source_where + ' AND ' + member_where + ' AND e.direction=?',
                           (*source_params, *member_params, direction), NEIGHBOR_LIMIT)

    def neighbors(self, key):
        return {b if a == key else a for a, b in self.edges if key in (a, b)}

    def middle(self, left, right):
        # Legacy handles cannot establish a shared intermediate account identity.
        remaining = MIDDLE_EDGE_LIMIT
        for a, b in ((left, right), (right, left)):
            source_ids = sorted(k[3:] for k in a if k.startswith('ig:'))
            pids = sorted(k[3:] for k in b if k.startswith('ig:'))
            if not source_ids or not pids:
                continue
            for start in range(0, len(pids), 400):
                ids = pids[start:start + 400]
                # Most observed members were never collected as sources. Batch both sides so
                # empty lookups cannot spend the whole query budget before the reverse join.
                for source_start in range(0, len(source_ids), 400):
                    sources = source_ids[source_start:source_start + 400]
                    if remaining <= 0 or self.middle_query_count >= MIDDLE_QUERY_LIMIT:
                        self.clipped = True
                        return
                    self.middle_query_count += 1
                    before = self.observations_read
                    self.query('SELECT e.*,p.* FROM edge_observations e JOIN people p ON p.id=e.person_id '
                               'WHERE e.seed_ig_id IN (' + ','.join('?' for _ in sources) + ') '
                               'AND e.person_ig_id IN (' + ','.join('?' for _ in ids) + ')',
                               (*sources, *ids), remaining)
                    remaining -= self.observations_read - before


def search(conn, query='', limit=12):
    limit = _limit(limit, 50)
    if not isinstance(query, str):
        raise ValueError('Search query must be text')
    query = query.strip()
    graph = _Graph(conn)
    if not query:
        accounts = list(graph.accounts.values())
    else:
        handle = _handle(query)
        # Indexed handle prefix search, with literal underscores rather than LIKE wildcards.
        rows = conn.execute('SELECT * FROM people WHERE handle>=? COLLATE NOCASE AND handle<? COLLATE NOCASE '
                            'ORDER BY handle COLLATE NOCASE LIMIT ?', (handle, handle + '\uffff', limit + 1)).fetchall()
        keys = {graph.person(r) for r in rows}
        keys.update(k for h, k in graph.seed_ids.items() if h.startswith(handle))
        accounts = [graph.accounts[k] for k in keys]
    for account in list(accounts):
        if account.get('person_id') is not None:
            legacy_row = conn.execute("SELECT e.*,p.* FROM edge_observations e JOIN people p ON p.id=e.person_id WHERE e.person_id=? AND e.person_ig_id='' LIMIT 1", (account['person_id'],)).fetchone()
            if legacy_row is not None:
                accounts.append(graph.accounts[graph.observed_person(legacy_row)])
    source_rows = conn.execute("SELECT DISTINCT seed,seed_ig_id FROM edge_observations WHERE seed>=? COLLATE NOCASE AND seed<? COLLATE NOCASE ORDER BY seed COLLATE NOCASE,seed_ig_id LIMIT ?", (handle if query else '', (handle if query else '') + '\uffff', limit + 1)).fetchall()
    accounts.extend(graph.accounts[graph.observed_seed(r['seed'], r['seed_ig_id'])] for r in source_rows)
    accounts = list({a['id']: a for a in accounts}.values())
    accounts.sort(key=lambda a: (not a['is_me'], a['handle'].lower(), a['id']))
    return {'accounts': accounts[:limit], 'truncated': len(accounts) > limit or len(source_rows) > limit, 'limit': limit}


def explore(conn, source, target, mode='all', limit=12):
    limit = _limit(limit, 50)
    if mode not in ('all', 'following'):
        raise ValueError('Mode must be all or following')
    graph = _Graph(conn)
    source, target = graph.resolve(source), graph.resolve(target)
    if source != target:
        graph.direct(source, target)
    graph.neighborhood(source)
    if source != target:
        graph.neighborhood(target)
    left, right = graph.neighbors(source), graph.neighbors(target)
    left = {k for k in left if not k.startswith(('legacy:', 'legacy-person:'))}
    right = {k for k in right if not k.startswith(('legacy:', 'legacy-person:'))}
    common = {}
    for label, directions in [('shared_followees', (True, True)), ('shared_followers', (False, False)),
                              ('forward_chain', (True, False)), ('reverse_chain', (False, True))]:
        keys = sorted(k for k in left & right - {source, target}
                      if ((source, k) if directions[0] else (k, source)) in graph.edges
                      and ((target, k) if directions[1] else (k, target)) in graph.edges)
        common[label] = {'count': len(keys), 'accounts': [graph.accounts[k] for k in keys[:limit]],
                         'displayed': min(len(keys), limit), 'bounded': graph.clipped}
    graph.middle(left, right)
    adjacency = {}
    for a, b in graph.edges:
        if any(k.startswith(('legacy:', 'legacy-person:')) and k not in (source, target) for k in (a, b)):
            continue
        adjacency.setdefault(a, set()).add(b)
        if mode == 'all':
            adjacency.setdefault(b, set()).add(a)
    candidates = []
    path_clipped = False
    # Enumerate only joins that terminate at the target; never walk dead-end trees.
    predecessors = {a for a, nexts in adjacency.items() if target in nexts}
    if source != target:
        if target in adjacency.get(source, ()):
            candidates.append([source, target])
        for middle in sorted(adjacency.get(source, set()) & predecessors - {source, target}):
            if len(candidates) >= PATH_LIMIT:
                path_clipped = True
                break
            candidates.append([source, middle, target])
        if not path_clipped:
            for first in sorted(adjacency.get(source, set()) - {source, target}):
                for second in sorted(adjacency.get(first, set()) & predecessors - {source, target, first}):
                    if len(candidates) >= PATH_LIMIT:
                        path_clipped = True
                        break
                    candidates.append([source, first, second, target])
                if path_clipped:
                    break
    def reciprocity(path):
        return sum((a, b) in graph.edges and (b, a) in graph.edges for a, b in zip(path, path[1:]))
    candidates.sort(key=lambda p: (len(p), -reciprocity(p), tuple(p)))
    paths, used = [], {source, target}
    selected_edges = set(k for k in graph.edges if set(k) == {source, target})
    for path in candidates[:limit]:
        hops = []
        used.update(path)
        for a, b in zip(path, path[1:]):
            key = (a, b) if (a, b) in graph.edges else (b, a)
            reciprocal = (b, a) in graph.edges and (a, b) in graph.edges
            selected_edges.add(key)
            if reciprocal:
                selected_edges.add((key[1], key[0]))
            hops.append({'from': a, 'to': b, 'traversal': 'forward' if key == (a, b) else 'backward',
                         'edge': graph.edges[key]['id'], 'reciprocal': reciprocal})
        paths.append({'id': '|'.join(path), 'nodes': path, 'hops': hops, 'length': len(path) - 1,
                      'reciprocal_hops': reciprocity(path)})
    edges = []
    for key in sorted(selected_edges):
        edge = graph.edges[key]
        edge['reciprocal'] = (key[1], key[0]) in graph.edges
        edge['evidence'].sort(key=lambda e: (e['seed'], e['direction'], e['first_seen'] or ''))
        edges.append(edge)
    coverage = []
    coverage_sources = {(seed, key) for seed, key in graph.seed_ids.items() if key in used}
    for edge in edges:
        for evidence in edge['evidence']:
            key = ('ig:' + evidence['seed_ig_id']) if evidence['seed_ig_id'] else ('legacy:' + evidence['seed'].lower())
            if key in used:
                coverage_sources.add((evidence['seed'].lower(), key))
    known_coverage_sources = set(coverage_sources)
    for key in used:
        if not any(existing == key for _, existing in coverage_sources):
            coverage_sources.add((graph.accounts[key]['handle'].lower(), key))
    for seed, key in sorted(coverage_sources):
        for direction in ('followers', 'following'):
            row = conn.execute('SELECT * FROM lists WHERE seed=? AND direction=?', (seed, direction)).fetchone()
            row = dict(row) if row and (seed, key) in known_coverage_sources else {}
            captured, total, state = row.get('received', 0) or 0, row.get('total'), row.get('state')
            status = 'missing' if not row else 'partial'
            if state == 'done':
                status = ('ended_unknown' if total is None or captured > total else
                          'ended_short' if captured < total else 'ended_count_matched')
            coverage.append({'seed': seed, 'account': key, 'direction': direction, 'state': state, 'status': status,
                             'identity_basis': graph.accounts[key]['identity_basis'],
                             'metadata_scope': 'Current list metadata keyed by handle; not historical identity or freshness proof',
                             'captured': captured, 'total': total,
                             'count_relation': 'unknown' if total is None else 'below' if captured < total else 'above' if captured > total else 'equal', 'updated_at': row.get('updated_at'), 'error': row.get('error')})
    return {'source': graph.accounts[source], 'target': graph.accounts[target], 'mode': mode,
        'direct_edges': [e for e in edges if set((e['from'], e['to'])) == {source, target}],
        'common_neighbors': common, 'paths': paths, 'nodes': [graph.accounts[k] for k in sorted(used)],
        'edges': edges, 'coverage': coverage,
        'search': {'bounded': True, 'truncated': graph.clipped or path_clipped or len(candidates) > limit,
                   'neighborhoods_clipped': graph.clipped, 'paths_clipped': path_clipped,
                   'search_truncated': graph.clipped or path_clipped, 'results_truncated': len(candidates) > limit,
                   'neighborhood_limit': NEIGHBOR_LIMIT, 'middle_edge_limit': MIDDLE_EDGE_LIMIT,
                   'path_limit': PATH_LIMIT, 'observations_read': graph.observations_read,
                   'observation_queries': graph.query_count, 'middle_queries': graph.middle_query_count,
                   'middle_query_limit': MIDDLE_QUERY_LIMIT,
                   'paths_found': len(candidates), 'paths_displayed': len(paths), 'max_hops': 3},
        'ranking': 'Heuristic sorting within the bounded results: shorter paths, then more reciprocally observed hops, then stable account IDs; not a global ranking.',
        'interpretation': 'Arrows show observed follows, not friendship or willingness to introduce. first_seen is the first stored observation, not a freshness check. Missing paths and lists do not prove absence or an unfollow. Historical observations are a temporal union; path hops may not have coexisted. Legacy sources and members without captured IDs have unknown historical identities, remain separate from identified accounts, and cannot be intermediate path nodes. List updated_at is list metadata, not an edge observation date. Counts describe imported data; ended lists do not establish a complete or current graph.'}
