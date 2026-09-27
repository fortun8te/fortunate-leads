"""Evidence-only pair comparison. A follow observation is never a friendship claim.

compare returns JSON-ready nodes, directed links with independent source observations,
ranked two-hop connectors, and collection coverage. Scores are deterministic heuristics,
not probabilities. Historical union edges cannot establish current absence or freshness.
"""
import math
import re
import uuid
from urllib.parse import urlsplit, unquote
from datetime import datetime, timezone


def map_search(query):
    """Identity search for the map, independent of bios, tags and display limits.

    Return bound SQL fragments so handles containing underscores stay literal.
    Exact handles lead, then handle/name prefixes, then identity substrings.
    """
    text = str(query or '').strip()
    if not text:
        return None
    if len(text) > 512:
        raise ValueError('Search for a name or Instagram handle.')
    candidate = text if '://' in text else 'https://' + text
    parsed = urlsplit(candidate)
    if parsed.hostname and parsed.hostname.lower() in ('instagram.com', 'www.instagram.com', 'm.instagram.com'):
        parts = [unquote(part) for part in parsed.path.split('/') if part]
        if len(parts) != 1 or not re.fullmatch(r'[A-Za-z0-9_.]{1,30}', parts[0]):
            raise ValueError('Use an Instagram profile link, not a post or reel.')
        text = parts[0]
    elif '://' in text or text.lower().startswith(('www.', 'instagram.com/')):
        raise ValueError('Search for a name, handle or Instagram profile link.')
    text = text.lstrip('@').strip().casefold()
    if not text:
        return None
    escaped = re.sub(r'([\\%_])', r'\\\1', text)
    prefix, contains = escaped + '%', '%' + escaped + '%'
    return {
        'text': text,
        'where': "(p.handle LIKE ? ESCAPE '\\' OR p.name LIKE ? ESCAPE '\\')",
        'args': (contains, contains),
        'rank': "CASE WHEN p.handle=? THEN 0 WHEN p.handle LIKE ? ESCAPE '\\' THEN 1 "
                "WHEN lower(p.name)=? THEN 2 WHEN p.name LIKE ? ESCAPE '\\' THEN 3 ELSE 4 END",
        'rank_args': (text, prefix, text, prefix),
    }


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

    def load_people(self, rows):
        missing = sorted({row['person_id'] for row in rows if row['person_id'] not in self.people})
        for start in range(0, len(missing), 400):
            chunk = missing[start:start + 400]
            for person in self.conn.execute('SELECT * FROM people WHERE id IN (' + ','.join('?' * len(chunk)) + ')', chunk):
                self.person(person)

    def degrees(self, ids):
        """Count canonical neighbors without retaining another full link graph.

        Both lookup orientations may visit the same edge. Neighbor sets collapse
        those visits, reciprocal edges and agreeing identity aliases alike.
        """
        neighbors = {key: set() for key in ids}
        pids = [pid for pid, node in self.people.items() if node['id'] in ids]
        seeds = [handle for handle, node in self.seed_nodes.items() if node['id'] in ids]
        for column, values in [('person_id', pids), ('seed', seeds)]:
            for start in range(0, len(values), 400):
                chunk = values[start:start + 400]
                cursor = self.conn.execute(
                    'SELECT seed, person_id, direction FROM edges WHERE ' + column
                    + ' IN (' + ','.join('?' * len(chunk)) + ')', chunk)
                while rows := cursor.fetchmany(400):
                    self.load_people(rows)
                    for row in rows:
                        seed = self.seed_nodes.get(row['seed'].lower())
                        person = self.people.get(row['person_id'])
                        if seed is None or person is None or row['direction'] not in ('following', 'followers'):
                            continue
                        left, right = seed['id'], person['id']
                        if left == right:
                            continue
                        if left in neighbors:
                            neighbors[left].add(right)
                        if right in neighbors:
                            neighbors[right].add(left)
        return {key: len(values) for key, values in neighbors.items()}

    def links(self, rows, include_evidence=True, only_keys=None):
        self.load_people(rows)
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
                    (row['seed'], row['person_id'], row['direction']))
                latest = None
                for item in observations:
                    evidence['observation_count'] += 1
                    stamp = _timestamp(item['observed_at'])
                    if stamp is not None and (latest is None or (stamp, item['page_key']) > latest[0]):
                        latest = ((stamp, item['page_key']), item)
                if latest is not None:
                    (last, _), item = latest
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
    temp_mode = conn.execute('PRAGMA temp_store').fetchone()[0]
    # db.connect uses MEMORY. For the normal standalone API read, use SQLite's
    # file temp store to keep dense intersections out of process RSS. A caller
    # transaction or existing temp object retains its original storage policy.
    switch_temp_store = temp_mode != 1 and conn.execute(
        'SELECT 1 FROM sqlite_temp_master LIMIT 1').fetchone() is None
    if switch_temp_store:
        conn.execute('PRAGMA temp_store=FILE')
    conn.execute('BEGIN')
    try:
        return _compare(conn, source_handle, target_handle, limit)
    finally:
        conn.rollback()
        if switch_temp_store:
            conn.execute(f'PRAGMA temp_store={temp_mode}')


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
    selected, total, endpoint_rows = _ranked_endpoint_rows(conn, g, a, b, limit)
    links = g.links(endpoint_rows, include_evidence=False)
    connectors = []
    for x, degree in selected:
        ax, xa = (a['id'], x) in links, (x, a['id']) in links
        bx, xb = (b['id'], x) in links, (x, b['id']) in links
        motifs = [name for name, yes in [('reciprocal_support', ax and xa and bx and xb),
                  ('directed_path', ax and xb), ('reverse_path', bx and xa),
                  ('shared_follower', xa and xb), ('shared_followee', ax and bx)] if yes]
        tier = 3 if 'reciprocal_support' in motifs else 2 if ('directed_path' in motifs or 'reverse_path' in motifs) else 1
        penalty = 1 / math.log2(2 + degree)
        node = g.nodes[x]
        support = [links[key] for key in ((a['id'], x), (x, a['id']), (b['id'], x), (x, b['id'])) if key in links]
        connectors.append(dict(node=node, motifs=motifs, links=sorted(support, key=lambda l: (l['source'], l['target'])),
                               manual_known=False, rank=dict(tier=tier, observed_degree=degree,
                               hub_penalty=round(penalty, 6), heuristic_score=round(tier + penalty, 6))))
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


def _ranked_endpoint_rows(conn, g, a, b, limit):
    """Stage endpoint edges on SQLite's temp store, keeping Python state O(limit).

    Each directed edge keeps its source record. Canonical IDs use the same seed
    and person rules as _Graph; the temporary tables are removed before the
    caller's read transaction is released.
    """
    suffix = uuid.uuid4().hex
    seed_table, edge_table, candidate_table, pid_table, candidate_seed_table = (
        'cg_' + name + '_' + suffix for name in ('seeds', 'edges', 'candidates', 'pids', 'candidate_seeds'))
    tables = (seed_table, edge_table, candidate_table, pid_table, candidate_seed_table)
    person_key = ("CASE WHEN COALESCE(NULLIF(p.ig_id,''), NULLIF(alias.ig_id,'')) IS NOT NULL "
                  "THEN 'ig:' || COALESCE(NULLIF(p.ig_id,''), NULLIF(alias.ig_id,'')) "
                  "ELSE 'person:' || p.id END")
    try:
        conn.execute(f'CREATE TEMP TABLE {seed_table}(handle TEXT PRIMARY KEY COLLATE NOCASE, node_id TEXT)')
        conn.executemany(f'INSERT INTO {seed_table} VALUES(?,?)',
                         ((handle, node['id']) for handle, node in g.seed_nodes.items()))
        conn.execute(f'''CREATE TEMP TABLE {edge_table}(
            seed TEXT COLLATE NOCASE, person_id INT, direction TEXT, first_seen TEXT,
            seed_key TEXT, person_key TEXT, PRIMARY KEY(seed,person_id,direction))''')
        select = (f' SELECT e.seed,e.person_id,e.direction,e.first_seen,s.node_id,{person_key} '
                  f'FROM edges e JOIN {seed_table} s ON s.handle=e.seed '
                  'JOIN people p ON p.id=e.person_id LEFT JOIN seeds alias ON alias.handle=p.handle '
                  "WHERE e.direction IN ('followers','following') AND s.node_id != " + person_key)
        insert = f'INSERT OR IGNORE INTO {edge_table}' + select
        for handle, node in g.seed_nodes.items():
            if node['id'] in (a['id'], b['id']):
                conn.execute(insert + ' AND e.seed=?', (handle,))
        for pid, node in g.people.items():
            if node['id'] in (a['id'], b['id']):
                conn.execute(insert + ' AND e.person_id=?', (pid,))
        conn.execute(f'''CREATE TEMP TABLE {candidate_table}(
            node_id TEXT PRIMARY KEY, mask INT, min_pid INT, display_handle TEXT, degree INT DEFAULT 0)''')
        # Bits are A->X, X->A, B->X, X->B. Multiple aliases and observations
        # collapse to one canonical candidate without losing direction.
        conn.execute(f'''INSERT INTO {candidate_table}(node_id,mask,min_pid)
            WITH seen AS (
              SELECT person_key AS node_id, person_id AS pid,
                CASE WHEN seed_key=? THEN CASE direction WHEN 'following' THEN 1 ELSE 2 END
                     ELSE CASE direction WHEN 'following' THEN 4 ELSE 8 END END AS bit
              FROM {edge_table} WHERE seed_key IN (?,?) AND person_key NOT IN (?,?)
              UNION ALL
              SELECT seed_key, NULL,
                CASE WHEN person_key=? THEN CASE direction WHEN 'following' THEN 2 ELSE 1 END
                     ELSE CASE direction WHEN 'following' THEN 8 ELSE 4 END END
              FROM {edge_table} WHERE person_key IN (?,?) AND seed_key NOT IN (?,?)
            ), masks AS (
              SELECT node_id, MAX(bit&1)+MAX(bit&2)+MAX(bit&4)+MAX(bit&8) AS mask,
                     MIN(pid) AS min_pid FROM seen GROUP BY node_id
            ) SELECT node_id,mask,min_pid FROM masks WHERE (mask&3)!=0 AND (mask&12)!=0''',
            (a['id'], a['id'], b['id'], a['id'], b['id'], a['id'], a['id'], b['id'], a['id'], b['id']))
        total = conn.execute(f'SELECT COUNT(*) FROM {candidate_table}').fetchone()[0]
        conn.execute(f'CREATE TEMP TABLE {pid_table}(node_id TEXT, pid INT, PRIMARY KEY(node_id,pid))')
        conn.execute(f'''INSERT OR IGNORE INTO {pid_table}
            SELECT c.node_id,e.person_id FROM {edge_table} e
            JOIN {candidate_table} c ON c.node_id=e.person_key''')
        conn.executemany(f'INSERT OR IGNORE INTO {pid_table} VALUES(?,?)',
                         ((node['id'], pid) for pid, node in g.people.items()
                          if node['id'] not in (a['id'], b['id'])))
        conn.execute(f'''CREATE TEMP TABLE {candidate_seed_table} AS
            SELECT s.handle,s.node_id FROM {seed_table} s
            JOIN {candidate_table} c ON c.node_id=s.node_id''')
        conn.execute(f'''UPDATE {candidate_table} SET display_handle=
            (SELECT handle FROM people WHERE id=min_pid)''')
        for node in g.nodes.values():
            conn.execute(f'UPDATE {candidate_table} SET display_handle=? WHERE node_id=?',
                         (node['handle'], node['id']))
        if total:
            # Count distinct canonical neighbors in SQL. This matches _Graph.degrees,
            # including alias collapse and reciprocal edge deduplication, without
            # allocating one Python set per candidate.
            conn.execute(f'''UPDATE {candidate_table} SET degree=COALESCE((
                SELECT degree FROM (
                  SELECT node_id,COUNT(DISTINCT neighbor) AS degree FROM (
                    SELECT cp.node_id,s.node_id AS neighbor
                    FROM {pid_table} cp CROSS JOIN edges e INDEXED BY edges_person_seed
                    JOIN {seed_table} s ON s.handle=e.seed
                    WHERE e.person_id=cp.pid AND e.direction IN ('followers','following')
                      AND s.node_id!=cp.node_id
                    UNION ALL
                    SELECT s.node_id,{person_key} AS neighbor
                    FROM {candidate_seed_table} s CROSS JOIN edges e
                    JOIN people p ON p.id=e.person_id
                    LEFT JOIN seeds alias ON alias.handle=p.handle
                    WHERE e.seed=s.handle AND e.direction IN ('followers','following')
                      AND {person_key}!=s.node_id
                  ) GROUP BY node_id
                ) d WHERE d.node_id={candidate_table}.node_id
              ),0)''')
        ranked = conn.execute(f'''SELECT node_id,degree FROM {candidate_table}
            ORDER BY CASE WHEN (mask&15)=15 THEN 3
                          WHEN ((mask&1)!=0 AND (mask&8)!=0) OR ((mask&4)!=0 AND (mask&2)!=0) THEN 2
                          ELSE 1 END DESC,
                     degree, lower(display_handle), node_id LIMIT ?''', (limit,)).fetchall()
        selected = [(row['node_id'], row['degree']) for row in ranked]
        shown_ids = [a['id'], b['id']] + [key for key, _ in selected]
        placeholders = ','.join('?' * len(shown_ids))
        endpoint_rows = [dict(row) for row in conn.execute(
            f'''SELECT seed,person_id,direction,first_seen FROM {edge_table}
                WHERE seed_key IN ({placeholders}) AND person_key IN ({placeholders})''',
            shown_ids + shown_ids)]
        return selected, total, endpoint_rows
    finally:
        for table in reversed(tables):
            conn.execute(f'DROP TABLE IF EXISTS temp.{table}')
