import os; os.environ.setdefault('FL_NO_ORSLOT', '1')  # tests never see the real key pool
"""Server side of the staged qualifier: network context, Laya stage, LLM worker pool, few-shot re-runs, snowball, /api/llm."""
import json
import os
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_server import Base, db, server

laya = server.laya


class LayaStub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self._send({'ok': True})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self._send({'results': [{'id': it['id'], 'answers': {'brand_account': {'p': 0.95}, 'dtc_founder': {'p': 0.9}}}
                                for it in body['items']]})

    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class PipelineTest(Base):
    def people(self, spec):
        ids = {}
        for h, (bio, seeds) in spec.items():
            ids[h] = db.upsert_person(self.conn, {'handle': h, 'bio': bio})
            for s, d in seeds:
                db.add_edge(self.conn, s, ids[h], d)
        self.conn.commit()
        return ids

    def test_network_context(self):
        ids = self.people({'good1': ('x', [('s1', 'followers')]), 'good2': ('x', [('s1', 'followers')]), 'no1': ('x', [('s2', 'followers')]),
                           'lead': (None, [('s1', 'following'), ('me', 'followers'), ('me', 'following'), ('good1', 'following')])})
        self.conn.execute("INSERT INTO seeds(handle, is_me) VALUES('me', 1)")
        for h, st in (('good1', 'good'), ('good2', 'client'), ('no1', 'no')):
            server.set_status(self.conn, [ids[h]], status=st)
        self.conn.commit()
        n = server.network_context(self.conn, [ids['lead']])[ids['lead']]
        self.assertEqual((n['lists'], n['me'], n['client_seeds'], n['seed_marked']), (3, 'mutual', 1, 2))
        self.assertAlmostEqual(n['seed_yield'], (2 + 1) / (2 + 4), places=3)   # s1: 2 good of 2 marked, smoothed
        self.assertIn(('s1', 'following'), n['seeds'])

    def test_snowball_is_opt_in_and_queues_following_lists(self):
        ids = self.people({'g': ('x', []), 'c': ('x', []), 'm': ('x', []), 'priv': ('x', [])})
        self.conn.execute("UPDATE people SET is_private=1 WHERE handle='priv'")
        for h, st in (('g', 'good'), ('c', 'client'), ('m', 'maybe'), ('priv', 'good')):
            server.set_status(self.conn, [ids[h]], status=st)
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)   # nothing happens by itself
        self.assertEqual(self.call('/api/scraper/snowball', {'min_status': 'bad'})[0], 400)
        r = self.call('/api/scraper/snowball', {'min_status': 'client'})[1]
        self.assertEqual(r['seeds'], ['c'])
        r = self.call('/api/scraper/snowball', {'min_status': 'good'})[1]
        self.assertEqual((r['queued'], r['seeds']), (1, ['g']))
        self.assertEqual({tuple(x) for x in self.conn.execute("SELECT seed, direction FROM jobs WHERE kind='list'")},
                         {('c', 'following'), ('g', 'following')})
        self.assertEqual(self.call('/api/scraper/snowball', {})[1]['queued'], 0)   # idempotent

    def test_api_llm_masks_keys_and_is_in_scraper(self):
        old = server.llm.PROVIDERS[0]
        server.llm.PROVIDERS[0] = server.llm.Providers(['sk-or-v1-secretsecret9876'], None)
        try:
            out = self.call('/api/llm')[1]
            self.assertEqual([p['key'] for p in out['providers']], [None, 'sk-…9876'])
            self.assertNotIn('secret', json.dumps(out))
            self.assertEqual(out['workers'], 8)
            self.assertIn('llm', self.call('/api/scraper')[1])
        finally:
            server.llm.PROVIDERS[0] = old
        self.assertEqual(self.call('/api/settings/qualify', {'on': True, 'workers': 0})[0], 400)
        self.assertEqual(self.call('/api/settings/qualify', {'on': True, 'workers': 2})[0], 200)
        self.assertEqual(db.get_setting(self.conn, 'llm_workers'), 2)

    def test_pool_is_bounded_parallel_and_never_blocks_ingest(self):
        self.people({f'p{i}': ('founder', [('s1', 'followers')]) for i in range(12)})
        server.qualify_batch(self.conn)
        db.set_setting(self.conn, 'qualify', True)
        db.set_setting(self.conn, 'llm_workers', 3)
        self.conn.commit()
        state = {'now': 0, 'max': 0}
        lock, release = threading.Lock(), threading.Event()

        def slow(items, examples):
            with lock:
                state['now'] += 1
                state['max'] = max(state['max'], state['now'])
            release.wait(5)
            with lock:
                state['now'] -= 1
            return [{'score': 90, 'tier': 'hot', 'role': 'buyer', 'reason': 'llm', 'model': 'm', 'tags': [('Fit: strong', 'signal')],
                     'prompt': 'q2:0', 'evidence': ['founder']} for _ in items]
        server.qualify.llm_verdicts = slow
        pool = server.LLMPool(batch=2)
        try:
            self.assertTrue(pool.step(self.conn))
            self.assertFalse(pool.step(self.conn))      # all 3 workers busy: nothing more is dispatched
            time.sleep(0.2)
            self.assertEqual(state['max'], 3)
            t = time.monotonic()                        # ingest and UI reads go on while the model calls hang
            self.call('/api/ext/list-page', {'job_id': None, 'seed': 's9', 'direction': 'followers', 'users': [{'handle': 'new1'}],
                                             'next_cursor': None, 'done': True})
            self.assertEqual(self.call('/api/leads')[0], 200)
            self.assertLess(time.monotonic() - t, 2)
            release.set()
            for _ in range(50):
                if pool.idle():
                    break
                time.sleep(0.05)
            self.assertTrue(pool.idle())
            self.assertEqual(self.conn.execute("SELECT count(*) FROM verdicts WHERE model='m' AND prompt='q2:0'").fetchone()[0], 6)
            self.assertEqual(self.conn.execute("SELECT count(*) FROM tags WHERE tag='Fit: strong'").fetchone()[0], 6)
        finally:
            del server.qualify.llm_verdicts
            release.set()

    def test_fewshot_rerun_when_marks_change_a_lot(self):
        server.qualify.prompt_version = lambda ex: 'v' + str(len(ex))
        try:
            ids = self.people({f'g{i}': ('bio', []) for i in range(12)})
            self.assertEqual(server.fewshot(self.conn), [])
            for who, score in (('g0', 50), ('g11', 10)):   # a warm LLM verdict and a clearly cold one
                self.conn.execute("INSERT OR REPLACE INTO verdicts(person_id, model, prompt, score, updated_at) VALUES(?, 'm', 'v0', ?, '')",
                                  (ids[who], score))
            server.set_status(self.conn, [ids['g1']], status='good')
            self.conn.commit()
            self.assertEqual(server.fewshot(self.conn), [])            # one new mark: examples stay frozen
            server.set_status(self.conn, [ids[f'g{i}'] for i in range(2, 9)], status='good')
            self.conn.commit()
            ex = server.fewshot(self.conn)
            self.assertEqual(len(ex), 8)
            self.assertEqual({e['label'] for e in ex}, {'good'})
            self.assertEqual(self.conn.execute('SELECT model FROM verdicts WHERE person_id=?', (ids['g0'],)).fetchone()[0], 'rules')
            self.assertEqual(self.conn.execute('SELECT model FROM verdicts WHERE person_id=?', (ids['g11'],)).fetchone()[0], 'm')
        finally:
            del server.qualify.prompt_version

    def test_laya_prequalifies_without_bio_and_is_soft(self):
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), LayaStub)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        old = laya.URL
        laya.URL = f'http://127.0.0.1:{httpd.server_address[1]}'
        laya.reset()
        stub_pre = server.qualify.prefilter
        server.qualify.prefilter = lambda p, seeds, net=None, laya_fit=None: 10 if laya_fit is None else laya_fit
        try:
            ids = self.people({'nobio': (None, [('s1', 'followers')]), 'withbio': ('we make candles', [('s1', 'followers')])})
            server.qualify_batch(self.conn)
            before = self.conn.execute('SELECT prefilter FROM verdicts WHERE person_id=?', (ids['nobio'],)).fetchone()[0]
            self.assertTrue(server.laya_step(self.conn))
            self.assertEqual(self.conn.execute('SELECT count(*) FROM laya').fetchone()[0], 2)   # list-only people too
            self.assertFalse(server.laya_step(self.conn))   # nothing new to score
            self.conn.execute("UPDATE people SET bio='we make kandles' WHERE id=?", (ids['withbio'],))   # same length, new text
            self.conn.commit()
            self.assertTrue(server.laya_step(self.conn))
            server.qualify_batch(self.conn)
            after = self.conn.execute('SELECT prefilter FROM verdicts WHERE person_id=?', (ids['nobio'],)).fetchone()[0]
            self.assertNotEqual(before, after)
            self.assertIn('Brand', {r[0] for r in self.conn.execute("SELECT tag FROM tags WHERE person_id=? AND source='auto'",
                                                                    (ids['nobio'],))})   # p >= 0.9 brand_account
            self.assertNotIn('Founder', {r[0] for r in self.conn.execute('SELECT tag FROM tags WHERE person_id=?', (ids['nobio'],))})
        finally:
            server.qualify.prefilter = stub_pre
            laya.URL = old
            laya.reset()
            httpd.shutdown()
            httpd.server_close()
        laya.URL = 'http://127.0.0.1:9'
        try:
            self.assertFalse(server.laya_step(self.conn))   # down: silently skipped
        finally:
            laya.URL = old
            laya.reset()

    def test_unlimited_profile_budget_plans_a_batch(self):
        self.people({f'p{i}': (None, [('s1', 'followers')]) for i in range(5)})
        self.conn.execute('UPDATE people SET bio=NULL, bio_at=NULL')
        self.conn.commit()
        server.qualify_batch(self.conn)
        db.set_setting(self.conn, 'qualify', True)
        db.set_setting(self.conn, 'budget', {'list': 3000, 'profile': 0})
        self.conn.commit()
        self.assertEqual(server.plan_profiles(self.conn), 5)
        self.call('/api/scraper/budget', {'profile': 5000})
        self.assertEqual(db.get_setting(self.conn, 'budget')['profile'], 5000)   # no hard cap

    def test_bio_min_floor_skips_hopeless_handles_but_not_explicit_reads(self):
        ids = self.people({'good': (None, [('s1', 'followers')]), 'hopeless': (None, [('s1', 'followers')])})
        self.conn.execute('UPDATE people SET bio=NULL, bio_at=NULL')
        server.qualify_batch(self.conn)
        self.conn.execute("UPDATE verdicts SET prefilter=CASE person_id WHEN ? THEN 60 ELSE 10 END", (ids['good'],))
        db.set_setting(self.conn, 'qualify', True)
        db.set_setting(self.conn, 'bio_min', 25)
        self.conn.commit()
        self.assertEqual(server.plan_profiles(self.conn), 1)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT handle FROM jobs")], ['good'])
        self.call(f"/api/person/{ids['hopeless']}/read", {})
        self.assertEqual(self.call('/api/ext/next?kinds=profile')[1]['job']['handle'], 'hopeless')
        self.assertEqual(self.call('/api/settings/qualify', {'on': True, 'bio_min': 101})[0], 400)
        self.call('/api/settings/qualify', {'on': True, 'bio_min': 5})
        self.assertEqual(db.get_setting(self.conn, 'bio_min'), 5)

    def test_old_verdicts_table_gains_columns(self):
        c = db.connect(server.CFG['db'])
        c.executescript('DROP TABLE verdicts; CREATE TABLE verdicts(person_id INT PRIMARY KEY, prefilter INT, score INT, tier TEXT, '
                        'role TEXT, reason TEXT, model TEXT, input_hash TEXT, updated_at TEXT);')
        c.close()
        db.init(server.CFG['db']).close()
        cols = [r[1] for r in self.conn.execute('PRAGMA table_info(verdicts)')]
        self.assertEqual(cols[-2:], ['prompt', 'evidence'])
        self.people({'x': ('founder', [('s1', 'followers')])})
        self.assertEqual(server.qualify_batch(self.conn), 1)


class PerfTest(Base):
    def test_indexes_and_pragmas(self):
        idx = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({'edges_person_seed', 'people_bio_at'} <= idx)
        self.assertNotIn('edges_person', idx)
        self.assertEqual(self.conn.execute('PRAGMA temp_store').fetchone()[0], 2)   # MEMORY

    def test_tags_and_map_are_cached_until_data_changes(self):
        pid = db.upsert_person(self.conn, {'handle': 'ann', 'bio': 'founder'})
        db.add_edge(self.conn, 's1', pid, 'followers')
        self.conn.commit()
        server.qualify_batch(self.conn)
        first = self.call('/api/map')[1]
        self.assertIs(server.api_tags(self.conn, {}, {}), server.api_tags(self.conn, {}, {}))
        # an LLM verdict keeps verdicts.updated_at, yet the map must show it
        server.qualify.llm_verdicts = lambda items, ex: [{'score': 99, 'tier': 'hot', 'role': 'buyer', 'reason': 'r', 'model': 'm'}
                                                         for _ in items]
        try:
            rows = self.conn.execute('SELECT * FROM people').fetchall()
            self.assertEqual(server.run_llm(self.conn, rows, {}), 1)
        finally:
            del server.qualify.llm_verdicts
        second = self.call('/api/map')[1]
        self.assertNotEqual(first['rev'], second['rev'])
        self.assertEqual([n['score'] for n in second['nodes'] if n['kind'] == 'lead'], [99])
        self.conn.execute("INSERT INTO tag_rules(tag, field, match) VALUES('x', 'bio', 'nothing matches')")
        self.conn.commit()
        self.assertNotEqual(self.call('/api/map')[1]['rev'], second['rev'])   # rule count is part of the rev


class LLMSettingsTest(Base):
    def setUp(self):
        super().setUp()
        self.cfg = Path(self.tmp.name) / 'openrouter.json'
        self.old = (server.llm.CONFIG, server.llm.PROVIDERS[0], server.llm.OPENROUTER)
        server.llm.CONFIG = self.cfg
        server.llm.OPENROUTER = 'http://127.0.0.1:9/down'
        server.llm.PROVIDERS[0] = server.llm.Providers.load(self.cfg)

    def tearDown(self):
        server.llm.CONFIG, server.llm.PROVIDERS[0], server.llm.OPENROUTER = self.old
        super().tearDown()

    def test_keys_models_and_test_button(self):
        key = 'sk-or-v1-' + 'a' * 40 + 'wxyz'
        self.assertEqual(self.call('/api/llm/keys', {'key': 'short'})[0], 400)
        kid = self.call('/api/llm/keys', {'key': key})[1]['id']
        self.assertEqual(self.call('/api/llm/keys', {'key': key})[0], 400)            # already there
        self.assertEqual(self.cfg.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.cfg.read_text())['keys'], [key])
        out = self.call('/api/llm')[1]
        self.assertNotIn('aaaa', json.dumps(out))                                   # never the key itself
        self.assertEqual([(p['id'], p['key'], p['source']) for p in out['providers']], [('proxy', None, None), (kid, 'sk-…wxyz', 'file')])
        t = self.call(f'/api/llm/keys/{kid}/test', {})[1]
        self.assertEqual((t['passed'], t['error']), (False, 'not reachable (URLError)'))
        self.assertEqual(self.call('/api/llm/keys/0123456789/test', {})[0], 404)
        self.assertEqual(self.call('/api/llm/models', {'models': ['bad model']})[0], 400)
        m = self.call('/api/llm/models', {'models': ['a/b:free', 'c/d'], 'daily_limit': 50})[1]
        self.assertEqual((m['models'], m['daily_limit']), (['a/b:free', 'c/d'], 50))
        self.assertEqual(json.loads(self.cfg.read_text())['keys'], [key])            # other fields kept
        self.assertEqual(self.call('/api/llm/keys', {'key': key}, origin='http://evil.test')[0], 403)
        self.assertEqual(self.call(f'/api/llm/keys/{kid}/remove', {})[1], {'ok': True})
        self.assertEqual(len(self.call('/api/llm')[1]['providers']), 1)
        self.assertEqual(self.call(f'/api/llm/keys/{kid}/remove', {})[0], 404)
        h = self.call('/api/llm/health')[1]
        self.assertEqual(set(h), {'proxy', 'laya'})
        self.assertIn('up', h['proxy'])

    def test_env_keys_cannot_be_removed_here(self):
        key = 'sk-or-v1-' + 'e' * 40
        server.llm.PROVIDERS[0] = server.llm.Providers([key], env_keys=[key])
        kid = server.llm.key_id(key)
        self.assertEqual(self.call('/api/llm')[1]['providers'][1]['source'], 'env')
        old = os.environ.get('OPENROUTER_API_KEYS')
        os.environ['OPENROUTER_API_KEYS'] = key
        try:
            self.assertEqual(self.call(f'/api/llm/keys/{kid}/remove', {})[0], 400)
        finally:
            if old is None:
                del os.environ['OPENROUTER_API_KEYS']
            else:
                os.environ['OPENROUTER_API_KEYS'] = old

    def test_settings_without_on_leave_qualify_alone(self):
        self.assertEqual(self.call('/api/settings/qualify', {'workers': 3, 'llm_min': 30})[1], {'ok': True, 'qualify': False})
        self.assertEqual((self.call('/api/llm')[1]['workers'], self.call('/api/llm')[1]['llm_min']), (3, 30))
