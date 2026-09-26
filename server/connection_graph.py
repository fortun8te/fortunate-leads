"""Evidence-only pair comparison. A follow observation is never a friendship claim.

compare returns JSON-ready nodes, directed links with independent source observations,
ranked two-hop connectors, and collection coverage. Scores are deterministic heuristics,
not probabilities. Historical union edges cannot establish current absence or freshness.
"""
import math
import re
from datetime import datetime, timezone


def _timestamp(value):
    """Do not promote malformed, timezone-free or future source dates into freshness."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None or dt > datetime.now(timezone.utc):
            return None
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


class _Graph:
    def __init__(self, conn):
        self.conn = conn
        self.nodes = {}
        self.people = {}
        self.seeds = {}
        # Seeds are a small account registry, not a scan of the collected graph.
        for row in conn.execute('SELECT handle, ig_id FROM seeds ORDER BY handle'):
            self.seeds[row['handle'].lower()] = dict(row)
        self.has_observations = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='edge_observations'").fetchone() is not None
        self.seed_nodes = {}
        for handle, row in self.seeds.items():
            person = None
            if row['ig_id']:
                person = conn.execute('SELECT * FROM people WHERE ig_id=?', (str(row['ig_id']),)).fetchone()
                if person is None:
                    person = conn.execute("SELECT * FROM people WHERE handle=? AND (ig_id IS NULL OR ig_id='')", (handle,)).fetchone()
            else:
                person = conn.execute('SELECT * FROM people WHERE handle=?', (handle,)).fetchone()
            if person is not None:
                node = self.person(person)
            else:
                key = 'ig:' + str(row['ig_id']) if row['ig_id'] else 'seed:' + handle
                node = self.nodes.setdefault(key, dict(id=key, handle=handle, name=None, person_id=None,
                                                       identity_basis='instagram_id' if row['ig_id'] else 'handle'))
            self.seed_nodes[handle] = node
            # Historical ID-less rows can still own incoming edges. Register each
            # agreeing seed alias for indexed person_id lookups, while preserving
            # the stable-ID person's preferred display record above.
            if row['ig_id']:
                alias = conn.execute(
                    "SELECT * FROM people WHERE handle=? AND (ig_id IS NULL OR ig_id='')", (handle,)
                ).fetchone()
                if alias is not None:
                    self.person(alias)

    def person(self, row):
        pid = row['id']
        if pid in self.people:
            return self.people[pid]
        # A seed ID may enrich an ID-less person only when all same-handle seed
        # evidence agrees. IDs always override names; known ID conflicts never merge.
        seed = self.seeds.get(row['handle'].lower())
        ig_id = row['ig_id'] or (seed['ig_id'] if seed else None)
        key = 'ig:' + str(ig_id) if ig_id else 'person:' + str(pid)
        node = self.nodes.setdefault(key, dict(id=key, handle=row['handle'], name=row['name'], person_id=pid,
                                               identity_basis='instagram_id' if ig_id else 'handle'))
        self.people[pid] = node
        return node

    def resolve(self, handle):
        handle = str(handle or '').strip().lstrip('@').lower()
        if not re.fullmatch(r'[a-z0-9_.]{1,30}', handle):
            raise ValueError('Use a valid, non-parked Instagram handle.')
        row = self.conn.execute('SELECT * FROM people WHERE handle=?', (handle,)).fetchone()
        person = self.person(row) if row is not None else None
        seed = self.seed_nodes.get(handle)
        if person and seed and person['id'] != seed['id']:
            raise ValueError('Ambiguous handle: seed and profile have conflicting Instagram IDs.')
        node = person or seed
        if node is None:
            raise ValueError('Unknown profile: @' + handle)
        if '~' in node['handle']:
            raise ValueError('This profile has a parked handle; refresh its identity first.')
        return node

    def observations(self, ids):
        """Use both indexed edge orientations, then deduplicate source records."""
        rows = {}
        pids = [pid for pid, n in self.people.items() if n['id'] in ids]
        seeds = [h for h, n in self.seed_nodes.items() if n['id'] in ids]
        for column, vals in [('person_id', pids), ('seed', seeds)]:
            for start in range(0, len(vals), 400):
                chunk = vals[start:start + 400]
                sql = 'SELECT seed, person_id, direction, first_seen FROM edges WHERE ' + column + ' IN (' + ','.join('?' * len(chunk)) + ')'
                for row in self.conn.execute(sql, chunk):
                    rows[(row['seed'].lower(), row['person_id'], row['direction'])] = dict(row)
        return list(rows.values())

    def links(self, rows, include_evidence=True, only_keys=None):
        missing = sorted({row['person_id'] for row in rows} - self.people.keys())
        for start in range(0, len(missing), 400):
            chunk = missing[start:start + 400]
            for person in self.conn.execute('SELECT * FROM people WHERE id IN (' + ','.join('?' * len(chunk)) + ')', chunk):
                self.person(person)
        links = {}
        for row in rows:
            seed = self.seed_nodes.get(row['seed'].lower())
            if seed is None or row['direction'] not in ('following', 'followers'):
                continue
            person = self.people.get(row['person_id'])
            if person is None:
                continue
            src, dst = (seed, person) if row['direction'] == 'following' else (person, seed)
            if src['id'] == dst['id']:
                continue
            key = (src['id'], dst['id'])
            if only_keys is not None and key not in only_keys:
                continue
            link = links.setdefault(key, dict(source=key[0], target=key[1], evidence=[]))
            if not include_evidence:
                continue
            ts = _timestamp(row['first_seen'])
            evidence = dict(seed=row['seed'], direction=row['direction'], first_observed=ts,
                            timestamp_status='valid' if ts else 'unknown', freshness='unknown',
                            observation_count=0, last_observed=None, last_job_id=None, last_page_key=None)
            if self.has_observations:
                observations = self.conn.execute(
                    'SELECT observed_at, job_id, page_key FROM edge_observations WHERE seed=? AND person_id=? AND direction=?',
                    (row['seed'], row['person_id'], row['direction'])).fetchall()
                valid = [(stamp, item) for item in observations if (stamp := _timestamp(item['observed_at']))]
                evidence['observation_count'] = len(observations)
                if valid:
                    last, item = max(valid, key=lambda pair: (pair[0], pair[1]['page_key']))
                    evidence.update(last_observed=last, last_job_id=item['job_id'], last_page_key=item['page_key'],
                                    freshness='last_observed_available')
            if evidence not in link['evidence']:
                link['evidence'].append(evidence)
        for link in links.values():
            link['evidence'].sort(key=lambda e: (e['seed'].lower(), e['direction']))
        return links

    def coverage(self, node_ids, evidence):
        required = {(e['seed'].lower(), e['direction']) for e in evidence}
        for handle, node in self.seed_nodes.items():
            if node['id'] in node_ids:
                required.update((handle, d) for d in ('followers', 'following'))
        for node_id in node_ids:
            node = self.nodes[node_id]
            handle = node['handle'].lower()
            if handle not in self.seeds and '~' not in handle:
                required.update((handle, d) for d in ('followers', 'following'))
        result = []
        for handle, direction in sorted(required):
            row = self.conn.execute('SELECT * FROM lists WHERE seed=? AND direction=?', (handle, direction)).fetchone()
            if row is None:
                result.append(dict(seed=handle, direction=direction, state=None, received=0, total=None,
                                   status='uncollected', updated_at=None))
                continue
            received, total = row['received'] or 0, row['total']
            if total is not None and (received > total or (row['state'] == 'done' and received != total)):
                status = 'count_mismatch'
            elif row['state'] == 'done':
                status = 'reported_complete'
            else:
                status = 'partial'
            result.append(dict(seed=handle, direction=direction, state=row['state'], received=received,
                               total=total, status=status, updated_at=_timestamp(row['updated_at'])))
        return result


def compare(conn, source_handle, target_handle, limit=20):
    """Read identities, links, ranking and coverage from one database snapshot.

    Existing caller transactions remain owned by the caller. A standalone comparison
    starts a read transaction and releases it even when input validation fails.
    """
    if conn.in_transaction:
        return _compare(conn, source_handle, target_handle, limit)
    conn.execute('BEGIN')
    try:
        return _compare(conn, source_handle, target_handle, limit)
    finally:
        conn.rollback()


def _compare(conn, source_handle, target_handle, limit=20):
    """Compare known handles; limit 1..100. Raises ValueError for unsafe identity/input.

    Rank by motif tier (reciprocal=3, either directed path=2, overlap=1), then
    1/log2(2+observed unique-neighbor degree). This degree describes only this
    dataset; it is not Instagram follower count or interpersonal closeness.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError('limit must be an integer from 1 to 100.')
    g = _Graph(conn)
    a, b = g.resolve(source_handle), g.resolve(target_handle)
    if a['id'] == b['id']:
        raise ValueError('Choose two different profiles.')
    endpoints = {a['id'], b['id']}
    endpoint_rows = g.observations(endpoints)
    links = g.links(endpoint_rows, include_evidence=False)
    neighbors = {endpoint: set() for endpoint in endpoints}
    for src, dst in links:
        if src in endpoints:
            neighbors[src].add(dst)
        if dst in endpoints:
            neighbors[dst].add(src)
    candidates = (neighbors[a['id']] & neighbors[b['id']]) - endpoints
    degree_links = g.links(g.observations(candidates), include_evidence=False) if candidates else {}
    degrees = {key: set() for key in candidates}
    for src, dst in degree_links:
        if src in degrees:
            degrees[src].add(dst)
        if dst in degrees:
            degrees[dst].add(src)
    connectors = []
    for x in candidates:
        ax, xa = (a['id'], x) in links, (x, a['id']) in links
        bx, xb = (b['id'], x) in links, (x, b['id']) in links
        motifs = [name for name, yes in [('reciprocal_support', ax and xa and bx and xb),
                  ('directed_path', ax and xb), ('reverse_path', bx and xa),
                  ('shared_follower', xa and xb), ('shared_followee', ax and bx)] if yes]
        tier = 3 if 'reciprocal_support' in motifs else 2 if ('directed_path' in motifs or 'reverse_path' in motifs) else 1
        degree = len(degrees[x])
        penalty = 1 / math.log2(2 + degree)
        node = g.nodes[x]
        support = [links[key] for key in ((a['id'], x), (x, a['id']), (b['id'], x), (x, b['id'])) if key in links]
        connectors.append(dict(node=node, motifs=motifs, links=sorted(support, key=lambda l: (l['source'], l['target'])),
                               manual_known=False, rank=dict(tier=tier, observed_degree=degree,
                               hub_penalty=round(penalty, 6), heuristic_score=round(tier + penalty, 6))))
    connectors.sort(key=lambda c: (-c['rank']['tier'], c['rank']['observed_degree'], c['node']['handle'].lower(), c['node']['id']))
    total = len(connectors)
    connectors = connectors[:limit]
    direct = [v for (src, dst), v in links.items() if src in endpoints and dst in endpoints]
    direct.sort(key=lambda l: (l['source'], l['target']))
    shown_links = {(l['source'], l['target']): l for l in direct}
    for c in connectors:
        for link in c['links']:
            shown_links[(link['source'], link['target'])] = link
    # Ledger history is only hydrated for bounded displayed links, never the
    # tens of thousands of discarded endpoint edges.
    shown_links = g.links(endpoint_rows, only_keys=set(shown_links))
    direct = [shown_links[(link['source'], link['target'])] for link in direct]
    members = {}
    for pid, node in g.people.items():
        members.setdefault(node['id'], []).append(pid)
    for c in connectors:
        c['links'] = [shown_links[(link['source'], link['target'])] for link in c['links']]
        c['manual_known'] = any(conn.execute(
            "SELECT 1 FROM tags WHERE person_id=? AND tag='Already know them' AND source='manual'", (pid,)
        ).fetchone() is not None for pid in members.get(c['node']['id'], []))
    graph_links = [shown_links[k] for k in sorted(shown_links)]
    node_ids = endpoints | {c['node']['id'] for c in connectors}
    coverage = g.coverage(node_ids, [e for l in graph_links for e in l['evidence']])
    return dict(source=a, target=b, direct_relationships=direct, connectors=connectors,
                graph=dict(nodes=[a, b] + [c['node'] for c in connectors], links=graph_links), coverage=coverage,
                total_candidates=total, returned_count=len(connectors), truncated=total > limit,
                ranking=dict(kind='heuristic_not_probability', order='motif tier, observed unique-neighbor degree, handle',
                             hub_penalty_formula='1 / log2(2 + observed_degree)', degree_scope='observed dataset only'),
                limitations=['Follow observations do not establish friendship or a willingness to introduce.',
                             'Missing links are unknown, not evidence of no connection.',
                             'Edges and received counts accumulate historical observations, not a current snapshot.',
                             'Reported completion is collector metadata, not independently verified coverage.',
                             'Manual known tags describe the operator relationship, not a relationship to the target.',
                             'First observed is not when a follow began; last observed is available only for tracked collection pages.'])
