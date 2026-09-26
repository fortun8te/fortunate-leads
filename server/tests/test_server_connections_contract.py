"""Connection HTTP contracts, with fictional records in Base's temporary database.

Keep this module after test_server in discovery order: Base deliberately installs
the HTTP suite's qualification stub, which must not precede pure qualifier tests.
"""

from collections import Counter
from urllib.parse import urlencode
from unittest.mock import patch
import unittest

from test_server import Base, db, server


RECORDED_AT = '2026-01-02T03:04:05+00:00'


class ConnectionHTTPContractTests(Base):
    def account(self, handle, ig_id, seed=False, own=False):
        pid = db.upsert_person(self.conn, {'handle': handle, 'ig_id': str(ig_id)}, RECORDED_AT)
        if seed:
            self.conn.execute('INSERT INTO seeds(handle,ig_id,is_me,added_at) VALUES(?,?,?,?)',
                              (handle, str(ig_id), own, RECORDED_AT))
        return pid

    def observation(self, seed, seed_id, person_id, member_id, direction='following'):
        db.add_edge(self.conn, seed, person_id, direction, RECORDED_AT,
                    seed_ig_id=str(seed_id), person_ig_id=str(member_id))

    def get(self, endpoint='/api/connections', **query):
        return self.call(endpoint + ('?' + urlencode(query) if query else ''))

    def pair(self, **query):
        params = {'from': 'contract_start', 'to': 'contract_target'}
        params.update(query)
        return self.get(**params)

    def assert_error(self, response, status):
        code, payload = response
        self.assertEqual(code, status, payload)
        self.assertIs(payload.get('ok'), False)
        self.assertIsInstance(payload.get('error'), str)
        self.assertTrue(payload['error'].strip())
        self.assertNotIn('paths', payload)

    def many_routes(self, count=55):
        self.account('contract_start', 101, seed=True, own=True)
        self.account('contract_target', 102, seed=True)
        for i in range(count):
            member_id = 1000 + i
            pid = self.account(f'contract_bridge_{i:02}', member_id)
            self.observation('contract_start', 101, pid, member_id)
            self.observation('contract_target', 102, pid, member_id, 'followers')
        self.conn.commit()

    def mixed_routes(self):
        ids = {'contract_start': 101, 'contract_target': 102, 'contract_bridge': 103,
               'contract_out': 104, 'contract_in': 105}
        pids = {handle: self.account(handle, ig, seed=ig in (101, 102), own=ig == 101)
                for handle, ig in ids.items()}
        observations = [
            ('contract_start', 'contract_target', 'following'),
            ('contract_start', 'contract_target', 'followers'),
            ('contract_start', 'contract_bridge', 'following'),
            ('contract_start', 'contract_out', 'following'),
            ('contract_start', 'contract_in', 'followers'),
            ('contract_target', 'contract_start', 'followers'),
            ('contract_target', 'contract_bridge', 'followers'),
            ('contract_target', 'contract_in', 'followers'),
            ('contract_target', 'contract_out', 'following'),
        ]
        for seed, member, direction in observations:
            self.observation(seed, ids[seed], pids[member], ids[member], direction)
        for (seed, direction), count in Counter((s, d) for s, _, d in observations).items():
            self.conn.execute('INSERT INTO lists(seed,direction,state,received,total,updated_at) '
                              'VALUES(?,?,?,?,?,?)', (seed, direction, 'done', count, count, RECORDED_AT))
        self.conn.commit()

    def test_result_limits_reject_invalid_values_on_both_endpoints(self):
        self.many_routes(2)
        for limit in ('0', '-1', '51', '999999999999999999999', '1.5', 'invalid', '   '):
            for endpoint in ('/api/connections', '/api/connections/search'):
                with self.subTest(endpoint=endpoint, limit=limit):
                    params = {'limit': limit, 'q': 'contract_bridge'}
                    if endpoint == '/api/connections':
                        params.update({'from': 'contract_start', 'to': 'contract_target'})
                    self.assert_error(self.get(endpoint, **params), 400)

    def test_default_and_boundary_limits_preserve_found_counts_and_stable_prefix(self):
        self.many_routes()
        status, whole = self.pair(mode='following', limit=50)
        self.assertEqual(status, 200, whole)
        expected_paths = [['ig:101', f'ig:{1000 + i}', 'ig:102'] for i in range(50)]
        self.assertEqual([p['nodes'] for p in whole['paths']], expected_paths)
        for requested, expected in ((None, 12), ('', 12), (1, 1), (50, 50)):
            with self.subTest(limit=requested):
                params = {} if requested is None else {'limit': requested}
                status, result = self.pair(mode='following', **params)
                self.assertEqual(status, 200, result)
                self.assertEqual(result['paths'], whole['paths'][:expected])
                self.assertEqual(result['search']['paths_found'], 55)
                self.assertEqual(result['search']['paths_displayed'], expected)
                self.assertIs(result['search']['results_truncated'], True)
                self.assertIs(result['search']['search_truncated'], False)
                self.assertIs(result['search']['truncated'], True)
                common = result['common_neighbors']['forward_chain']
                self.assertEqual((common['count'], common['displayed'], len(common['accounts'])),
                                 (55, expected, expected))
                status, search = self.get('/api/connections/search', q='contract_bridge_', **params)
                self.assertEqual(status, 200, search)
                self.assertEqual((search['limit'], len(search['accounts'])), (expected, expected))
                self.assertEqual([a['id'] for a in search['accounts']],
                                 [f'ig:{1000 + i}' for i in range(expected)])
                self.assertIs(search['truncated'], True)

    def test_empty_search_returns_own_sources_first_and_never_unlinked_people(self):
        self.account('contract_unlinked', 900)
        self.conn.commit()
        self.assertEqual(self.get('/api/connections/search')[1],
                         {'accounts': [], 'truncated': False, 'limit': 12})
        for i in range(15):
            self.account(f'contract_source_{i:02}', 200 + i, seed=True, own=i == 14)
        self.conn.commit()
        for query in ({}, {'q': ''}, {'q': '   '}):
            with self.subTest(query=query):
                status, result = self.get('/api/connections/search', **query)
                self.assertEqual(status, 200, result)
                self.assertEqual(result['limit'], 12)
                self.assertEqual([a['id'] for a in result['accounts']],
                                 ['ig:214'] + [f'ig:{200 + i}' for i in range(11)])
                self.assertIs(result['accounts'][0]['is_me'], True)
                self.assertIs(result['truncated'], True)
        status, result = self.get('/api/connections/search', limit=50)
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result['accounts']), 15)
        self.assertIs(result['truncated'], False)

    def test_selector_errors_distinguish_malformed_and_absent_accounts(self):
        self.many_routes(1)
        malformed = ('', ' ', 'bad/name', 'bad handle', 'ig:', 'ig:abc', 'ig:' + '9' * 33,
                     'person:x', 'person:-1', 'person:' + '9' * 19, 'legacy-person:x',
                     'seed:bad/name', 'legacy:bad/name', 'https://example.test/contract_start/',
                     'https://instagram.com.evil.test/contract_start/', "x' OR 1=1 --")
        absent = ('contract_missing', 'ig:999999', 'person:999999', 'seed:contract_missing',
                  'legacy:contract_missing', 'legacy-person:999999')
        for key in ('from', 'to'):
            for selector in malformed + absent:
                with self.subTest(key=key, selector=selector):
                    self.assert_error(self.pair(**{key: selector}), 400 if selector in malformed else 404)
        for query in ({}, {'from': 'contract_start'}, {'to': 'contract_target'}):
            self.assert_error(self.get(**query), 400)
        for mode in ('followers', 'both', 'ALL', 'null', ' '):
            self.assert_error(self.pair(mode=mode), 400)
        self.assertEqual(self.pair()[1]['mode'], 'all')

    def test_encoded_urls_handles_and_literal_prefixes_reach_correct_accounts(self):
        self.many_routes(2)
        self.account('contractXbridge_00', 999)
        self.conn.commit()
        status, expected = self.pair()
        self.assertEqual(status, 200, expected)
        selectors = [
            (' @CONTRACT_START ', '@CONTRACT_TARGET'),
            ('https://www.instagram.com/CONTRACT_START/?utm_source=test&note=a+b#profile',
             'https://instagram.com/contract_target/?x=one&two=2'),
            ('ig:101', 'ig:102'),
            ('seed:contract_start', 'seed:contract_target'),
        ]
        for source, target in selectors:
            with self.subTest(source=source, target=target):
                self.assertEqual(self.pair(**{'from': source, 'to': target}), (200, expected))
        for query in ('contract_bridge_', ' @CONTRACT_BRIDGE_',
                      'https://www.instagram.com/contract_bridge_/?utm_source=test&note=a+b'):
            status, data = self.get('/api/connections/search', q=query)
            self.assertEqual(status, 200, data)
            self.assertEqual([a['id'] for a in data['accounts']], ['ig:1000', 'ig:1001'])
        for query in ('%', 'contract+bridge', 'contract%5Fbridge'):
            self.assert_error(self.get('/api/connections/search', q=query), 400)

    def test_origin_and_host_rules_apply_to_every_read_endpoint(self):
        self.mixed_routes()
        port = self.httpd.server_address[1]
        endpoints = ('/api/connections?from=contract_start&to=contract_target',
                     '/api/connections/search?q=contract', '/api/map?scope=all')
        for path in endpoints:
            for origin in (None, f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
                with self.subTest(path=path, allowed_origin=origin):
                    self.assertEqual(self.call(path, origin=origin)[0], 200)
            for origin in ('https://other.test', 'null', server.EXT_ORIGIN,
                           f'http://localhost:{port + 1}', f'http://localhost.evil.test:{port}'):
                with self.subTest(path=path, forbidden_origin=origin):
                    self.assertEqual(self.call(path, origin=origin),
                                     (403, {'ok': False, 'error': 'origin'}))
            self.assertEqual(self.call(path, host=f'localhost:{port}')[0], 200)
            self.assertEqual(self.call(path, host=f'evil.test:{port}'),
                             (403, {'ok': False, 'error': 'host'}))

    def test_json_paths_arrows_evidence_coverage_and_counters_are_consistent(self):
        self.mixed_routes()
        for mode, expected in (
            ('all', [('ig:101', 'ig:102'), ('ig:101', 'ig:103', 'ig:102'),
                     ('ig:101', 'ig:104', 'ig:102'), ('ig:101', 'ig:105', 'ig:102')]),
            ('following', [('ig:101', 'ig:102'), ('ig:101', 'ig:103', 'ig:102')]),
        ):
            with self.subTest(mode=mode):
                status, data = self.pair(mode=mode)
                self.assertEqual(status, 200, data)
                self.assertEqual([tuple(p['nodes']) for p in data['paths']], expected)
                nodes = {n['id']: n for n in data['nodes']}
                edges = {e['id']: e for e in data['edges']}
                self.assertEqual(len(nodes), len(data['nodes']))
                self.assertEqual(len(edges), len(data['edges']))
                self.assertEqual(nodes[data['source']['id']], data['source'])
                self.assertEqual(nodes[data['target']['id']], data['target'])
                for path in data['paths']:
                    self.assertEqual(path['length'], len(path['nodes']) - 1)
                    self.assertEqual(len(path['hops']), path['length'])
                    self.assertEqual(len(set(path['nodes'])), len(path['nodes']))
                    self.assertLessEqual(path['length'], 3)
                    self.assertEqual(path['reciprocal_hops'], sum(h['reciprocal'] for h in path['hops']))
                    for (a, b), hop in zip(zip(path['nodes'], path['nodes'][1:]), path['hops']):
                        self.assertEqual((hop['from'], hop['to']), (a, b))
                        edge = edges[hop['edge']]
                        arrow = (a, b) if hop['traversal'] == 'forward' else (b, a)
                        self.assertEqual((edge['from'], edge['to']), arrow)
                        if mode == 'following':
                            self.assertEqual(hop['traversal'], 'forward')
                for edge in edges.values():
                    self.assertIn(edge['from'], nodes)
                    self.assertIn(edge['to'], nodes)
                    self.assertTrue(edge['evidence'])
                    for evidence in edge['evidence']:
                        arrow = ('ig:' + evidence['seed_ig_id'], 'ig:' + evidence['person_ig_id'])
                        if evidence['direction'] == 'followers':
                            arrow = arrow[::-1]
                        self.assertEqual((edge['from'], edge['to']), arrow)
                        self.assertEqual((evidence['first_seen'], evidence['last_seen']),
                                         (RECORDED_AT, RECORDED_AT))
                self.assertEqual(len(edges['ig:101>ig:102']['evidence']), 2)
                self.assertEqual({e['id'] for e in data['direct_edges']}, {'ig:101>ig:102', 'ig:102>ig:101'})
                self.assertEqual({k: c['count'] for k, c in data['common_neighbors'].items()},
                                 {'shared_followees': 1, 'shared_followers': 1,
                                  'forward_chain': 1, 'reverse_chain': 0})
                stats = data['search']
                self.assertEqual((stats['paths_found'], stats['paths_displayed']), (len(expected), len(expected)))
                self.assertFalse(any(stats[k] for k in ('truncated', 'search_truncated', 'results_truncated',
                                                        'neighborhoods_clipped', 'paths_clipped')))
                for key in ('observations_read', 'observation_queries', 'middle_queries'):
                    self.assertIs(type(stats[key]), int)
                    self.assertGreater(stats[key], 0)
                self.assertLessEqual(stats['middle_queries'], stats['middle_query_limit'])
                self.assertLessEqual(stats['middle_queries'], stats['observation_queries'])
                self.assertGreaterEqual(stats['observations_read'], 9)
                for coverage in data['coverage']:
                    self.assertIn(coverage['account'], nodes)
                    if coverage['account'] in ('ig:101', 'ig:102'):
                        self.assertEqual(coverage['status'], 'ended_count_matched')
                        self.assertEqual(coverage['captured'], coverage['total'])
                    else:
                        self.assertEqual(coverage['status'], 'missing')
                        self.assertIsNone(coverage['total'])

    def test_successful_and_failed_reads_never_write_any_database_table(self):
        self.mixed_routes()
        before = '\n'.join(self.conn.iterdump())
        version = self.conn.execute('PRAGMA data_version').fetchone()[0]
        trace = []
        connect = db.connect

        def traced_connect(path):
            connection = connect(path)
            connection.set_trace_callback(trace.append)
            return connection

        requests = [
            ('/api/connections/search', 200),
            ('/api/connections/search?q=contract', 200),
            ('/api/connections/search?q=contract&limit=0', 400),
            ('/api/connections?from=contract_start&to=contract_target', 200),
            ('/api/connections?from=contract_target&to=contract_start&mode=following', 200),
            ('/api/connections?from=contract_start&to=contract_start', 200),
            ('/api/connections?from=contract_missing&to=contract_target', 404),
            ('/api/connections?from=contract_start&to=contract_target&mode=bad', 400),
            ('/api/map?scope=all&limit=10', 200),
            ('/api/map?scope=all&q=missing', 200),
        ]
        with patch.object(db, 'connect', side_effect=traced_connect):
            for path, status in requests * 2:
                with self.subTest(path=path):
                    self.assertEqual(self.call(path)[0], status)
        self.assertTrue(trace)
        writes = [sql for sql in trace if sql.lstrip().split(None, 1)[0].upper() in
                  {'INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'DROP', 'ALTER', 'VACUUM', 'REINDEX'}]
        self.assertEqual(writes, [])
        self.assertEqual(self.conn.execute('PRAGMA data_version').fetchone()[0], version)
        self.assertEqual('\n'.join(self.conn.iterdump()), before)

    def test_overview_reports_applied_limit_and_filtered_population(self):
        self.account('contract_source', 101, seed=True)
        for i in range(55):
            pid = self.account(f'contract_person_{i:02}', 1000 + i)
            self.observation('contract_source', 101, pid, 1000 + i)
        self.conn.commit()
        for requested, applied in ((None, 400), (0, 400), (-1, 10), (1, 10), (10, 10),
                                   (51, 51), (55, 55), (9999, 3000)):
            with self.subTest(limit=requested):
                params = {} if requested is None else {'limit': requested}
                status, data = self.get('/api/map', scope='all', **params)
                self.assertEqual(status, 200, data)
                shown = min(applied, 55)
                self.assertEqual(data['display'], {'shown_people': shown, 'matched_people': 55,
                                                 'limit': applied, 'truncated': shown < 55})
                self.assertEqual(sum(n['kind'] == 'lead' for n in data['nodes']), shown)
                node_ids = {n['id'] for n in data['nodes']}
                for link in data['links']:
                    self.assertIn(link['source'], node_ids)
                    self.assertIn(link['target'], node_ids)
        for query, count in (('contract_person_0', 10), ('contract_missing', 0)):
            status, data = self.get('/api/map', scope='all', limit=51, q=query)
            self.assertEqual(status, 200, data)
            self.assertEqual(data['display'], {'shown_people': count, 'matched_people': count,
                                             'limit': 51, 'truncated': False})


if __name__ == '__main__':
    unittest.main()
