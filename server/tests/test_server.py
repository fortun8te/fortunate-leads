import os; os.environ.setdefault('FL_NO_ORSLOT', '1')  # tests never see the real key pool
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

stub = types.ModuleType('qualify')
stub.prefilter = lambda p, seeds, net=None, laya_fit=None: 10 * len(seeds)
stub.network_strength = lambda net: min(100, 10 * (net or {}).get('lists', 0))
stub.rule_tags = lambda p, edges, me: [(f"via @{e['seed']}", 'source') for e in edges] + (
    [('founder', 'role')] if 'founder' in (p.get('bio') or '') else [])
stub.rule_verdict = lambda p, tags, net=None: {'score': 80 if ('founder', 'role') in tags else 30,
                                     'content_fit': (80 if ('founder', 'role') in tags else 30) if p.get('bio') else None,
                                     'role': 'x', 'reason': 'r',
                                     'tier': 'unread' if not p.get('bio') else 'hot' if ('founder', 'role') in tags else 'cold'}
stub.llm_verdict = lambda p, tags, edges: None
stub.input_hash = lambda p, edges, net=None: 'h' if p.get('bio') else 'h0'   # a new bio changes the input
stub.legacy_input_hash = lambda p, edges, net=None: 'h'
stub.blend = lambda content, net: content or 0   # network reblend is a no-op in these tests
stub._tier = lambda score, has_bio: 'unread' if not has_bio else 'hot' if score >= 70 else 'cold'

import db  # noqa: E402
import migrate  # noqa: E402
import server  # noqa: E402

EXT = server.EXT_ORIGIN


class Base(unittest.TestCase):
    def setUp(self):
        self.tokens = {}
        self.old_qualify = server.qualify
        server.qualify = stub
        self.tmp = tempfile.TemporaryDirectory()
        server.CFG['db'] = str(Path(self.tmp.name) / 'leads.sqlite')
        c = db.init(server.CFG['db'])
        db.set_setting(c, 'bio_min', 0)   # the stub prefilter (10 per seed) is not on the real scale
        c.commit()
        c.close()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        server.CFG['port'] = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.conn = db.connect(server.CFG['db'])

    def tearDown(self):
        self.conn.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()
        server.qualify = self.old_qualify

    def call(self, path, body=None, origin=None, host=None):
        port = server.CFG['port']
        if isinstance(body, dict) and body.get('job_id') and 'lease_token' not in body:
            body = dict(body, lease_token=self.tokens.get(body['job_id']))
        req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', method='POST' if body is not None else 'GET',
                                     data=json.dumps(body).encode() if body is not None else None)
        if origin is None and path.startswith('/api/ext/'):
            origin = EXT
        elif origin is None and body is not None:  # browsers always send Origin on POST
            origin = f'http://127.0.0.1:{port}'
        if origin:
            req.add_header('Origin', origin)
        req.add_header('Host', host or f'127.0.0.1:{port}')
        try:
            with urllib.request.urlopen(req) as r:
                out = json.loads(r.read())
                if isinstance(out, dict) and out.get('job') and out['job'].get('lease_token'):
                    self.tokens[out['job']['id']] = out['job']['lease_token']
                return r.status, out
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def page(self, job, users, cursor=None, done=False, **extra):
        if 'requested_cursor' not in extra:   # like the extension: name the cursor this page was fetched from
            row = self.conn.execute('SELECT cursor FROM lists WHERE seed=? AND direction=?', (job['seed'], job['direction'])).fetchone()
            extra['requested_cursor'] = row[0] if row else None
        return self.call('/api/ext/list-page', {'job_id': job['id'], 'seed': job['seed'], 'ig_id': '99', 'direction': job['direction'],
                                                'users': users, 'next_cursor': cursor, 'done': done, 'total': 3,
                                                'total_source': 'current_run', **extra})


class ServerTest(Base):
    def test_origin_and_host_checks(self):
        self.assertEqual(self.call('/api/ext/next', origin='https://evil.example')[0], 403)
        self.assertEqual(self.call('/api/ext/next', origin='http://127.0.0.1:%d' % server.CFG['port'])[0], 403)
        self.assertEqual(self.call('/api/ext/next')[0], 200)
        self.assertEqual(self.call('/api/scraper/pause', {'paused': True}, origin='https://evil.example')[0], 403)
        self.assertEqual(self.call('/api/counts', host='evil.example:8766')[0], 403)
        self.assertEqual(self.call('/api/counts')[0], 200)

    def test_list_page_idempotent_and_direction(self):
        self.call('/api/scraper/seeds', {'handles': ['@Brand'], 'directions': ['followers']})
        job = self.call('/api/ext/next')[1]['job']
        self.assertEqual((job['kind'], job['seed'], job['direction'], job['cursor']), ('list', 'brand', 'followers', None))
        users = [{'ig_id': '1', 'handle': 'alice', 'name': 'A'}, {'ig_id': '2', 'handle': 'bob'}]
        self.assertEqual(self.page(job, users, cursor='c1', requested_cursor=None)[1], {'ok': True, 'received': 2})
        again = self.call('/api/ext/next')[1]['job']
        self.assertEqual((again['id'], again['cursor'], again['ig_id']), (job['id'], 'c1', '99'))
        self.page(job, [{'ig_id': '4', 'handle': 'dan'}], cursor='c2', requested_cursor='c1')
        # outbox retry: the extension names the cursor each page was fetched from
        self.assertEqual(self.page(job, users, cursor='c1', requested_cursor=None)[1], {'ok': True, 'received': 3, 'duplicate': True})
        self.assertEqual(self.conn.execute("SELECT cursor, received FROM lists WHERE seed='brand'").fetchone()[:], ('c2', 3))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM edges WHERE seed='brand' AND direction='followers'").fetchone()[0], 3)
        self.call('/api/ext/next')
        self.page(job, [{'ig_id': '3', 'handle': 'carol'}], done=True)
        lst = self.conn.execute("SELECT * FROM lists WHERE seed='brand'").fetchone()
        self.assertEqual((lst['state'], lst['received'], lst['total']), ('done', 4, 3))
        self.assertIsNone(self.call('/api/ext/next')[1]['job'])

    def test_profile_ingest(self):
        pid = db.upsert_person(self.conn, {'ig_id': '7', 'handle': 'dave'})
        self.conn.commit()
        self.call(f'/api/person/{pid}/read', {})
        job = self.call('/api/ext/next')[1]['job']
        self.assertEqual(job, {'id': job['id'], 'kind': 'profile', 'handle': 'dave', 'ig_id': '7', 'lease_token': job['lease_token']})
        self.call('/api/ext/profile', {'job_id': job['id'], 'profile': {'ig_id': '7', 'handle': 'dave', 'bio': 'founder of X',
                                                                         'followers': 1200, 'is_business': True}})
        p = self.conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()
        self.assertEqual((p['bio'], p['followers'], p['is_business']), ('founder of X', 1200, 1))
        self.assertTrue(p['bio_at'])
        self.assertEqual(self.conn.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'done')

    def test_error_cooldown_and_private(self):
        self.call('/api/scraper/seeds', {'handles': ['a'], 'directions': ['followers']})
        job = self.call('/api/ext/next')[1]['job']
        self.call('/api/ext/error', {'job_id': job['id'], 'code': 'rate_limit', 'retry_at': '2099-01-01T00:00:00Z', 'message': '429'})
        s = self.call('/api/scraper')[1]
        self.assertTrue(s['ext']['cooldown_until'].startswith('2099-01-01'))
        self.assertEqual(s['queue']['list'], 1)
        job = self.call('/api/ext/next')[1]['job']  # the extension enforces cooldowns; the server just records them
        self.call('/api/ext/error', {'job_id': job['id'], 'code': 'private', 'retry_at': None, 'message': 'private'})
        self.assertEqual(self.conn.execute("SELECT state FROM lists WHERE seed='a'").fetchone()[0], 'private')
        self.assertIsNone(self.call('/api/ext/next')[1]['job'])

    def test_next_order_and_pause(self):
        pid = db.upsert_person(self.conn, {'handle': 'erin'})
        self.conn.commit()
        self.call(f'/api/person/{pid}/read', {})
        self.call('/api/scraper/seeds', {'handles': ['s1'], 'directions': ['following']})
        self.assertEqual(self.call('/api/ext/next?kinds=profile')[1]['job']['kind'], 'profile')
        nxt = self.call('/api/ext/next')[1]
        self.assertEqual((nxt['job']['kind'], nxt['budget'], nxt['paused']), ('list', {'list': 3000, 'profile': 300}, False))
        self.assertIsNone(self.call('/api/ext/next?kinds=list')[1]['job'])
        self.call('/api/scraper/pause', {'paused': True})
        self.assertEqual(self.call('/api/ext/next')[1], {'ok': True, 'paused': True, 'budget': {'list': 3000, 'profile': 300},
                                                         'job': None, 'cooldown_until': None, 'stages': {'list': False, 'profile': False}})
        self.call('/api/scraper/pause', {'paused': False})
        self.assertEqual(self.call('/api/scraper/budget', {'list': 9999, 'profile': 200})[0], 400)  # out of range: nothing changes
        self.call('/api/scraper/budget', {'list': 3000, 'profile': 200})
        hb = self.call('/api/ext/heartbeat', {'version': '1', 'state': 'idle', 'today': {'list': 1, 'profile': 2}})[1]
        self.assertEqual(hb, {'ok': True, 'paused': False, 'budget': {'list': 3000, 'profile': 200}, 'stages': {'list': True, 'profile': True}})
        self.assertTrue(self.call('/api/scraper')[1]['ext']['online'])
        self.call('/api/ext/error', {'job_id': None, 'code': 'challenge', 'retry_at': None, 'message': 'checkpoint'})
        self.assertFalse(self.call('/api/ext/next')[1]['paused'])  # the extension's own hold stops requests, not a global pause
        self.assertEqual(self.call('/api/scraper')[1]['ext']['last_error'], 'checkpoint')

    def test_planner(self):
        for i, seeds in enumerate([['s1'], ['s1', 's2'], []]):
            pid = db.upsert_person(self.conn, {'handle': f'p{i}'})
            for s in seeds:
                db.add_edge(self.conn, s, pid, 'followers')
        db.upsert_person(self.conn, {'handle': 'locked', 'is_private': True})
        db.set_setting(self.conn, 'budget', {'list': 10, 'profile': 2})
        self.conn.execute("INSERT INTO lists(seed, direction, state) VALUES('s3','followers','queued')")
        self.conn.commit()
        server.qualify_batch(self.conn)
        self.assertEqual(server.plan_profiles(self.conn), 1)  # still collecting: only people in 2+ lists
        self.assertEqual([r[0] for r in self.conn.execute('SELECT handle FROM jobs')], ['p1'])
        db.set_setting(self.conn, 'qualify', True)
        self.assertEqual(server.plan_profiles(self.conn), 1)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT handle FROM jobs ORDER BY priority DESC")], ['p1', 'p0'])
        self.assertEqual(server.plan_profiles(self.conn), 0)
        db.set_setting(self.conn, 'qualify', False)
        self.conn.execute('DELETE FROM jobs')
        self.conn.execute('DELETE FROM lists')
        self.assertEqual(server.plan_profiles(self.conn), 2)  # bio reads do not wait for Qualify; lists done: everyone

    def test_leads_filter_and_map(self):
        ids = {}
        for h, bio, seeds in [('ann', 'founder at A', ['s1', 's2']), ('ben', 'founder', ['s1']), ('cat', 'hobby', ['s2']), ('dan', None, [])]:
            ids[h] = db.upsert_person(self.conn, {'handle': h, 'bio': bio, 'pic_url': 'https://x.cdninstagram.com/a.jpg'})
            for s in seeds:
                db.add_edge(self.conn, s, ids[h], 'followers')
        db.queue_list(self.conn, 's1', 'followers')
        self.conn.commit()
        server.qualify_batch(self.conn)
        rev = self.call('/api/map')[1]['rev']
        self.call(f"/api/person/{ids['ben']}/tags", {'add': ['vip']})
        server.qualify_batch(self.conn)
        rows = lambda qs: [r['handle'] for r in self.call('/api/leads?' + qs)[1]['rows']]  # noqa: E731
        self.assertEqual(rows('tier=hot'), ['ann', 'ben'])
        self.assertEqual(rows('tags=founder,vip'), ['ben'])
        self.assertEqual(rows('tags=founder,via%20@s2'), ['ann'])
        self.assertEqual(rows('tier=unread'), ['dan'])
        self.assertEqual(rows('q=hobb'), ['cat'])
        lead = self.call('/api/leads?tier=hot&limit=1')[1]
        self.assertEqual((lead['total'], lead['rows'][0]['via'], lead['rows'][0]['tier']), (2, ['s1', 's2'], 'hot'))
        self.assertIn({'tag': 'vip', 'grp': 'signal', 'source': 'manual'}, self.call(f"/api/person/{ids['ben']}")[1]['tags'])
        self.call(f"/api/person/{ids['ann']}/mark", {'status': 'no'})
        self.assertEqual(rows('tier=hot'), ['ben'])
        self.assertEqual(rows('status=no'), ['ann'])
        counts = self.call('/api/counts')[1]
        self.assertEqual((counts['hot'], counts['unread'], counts['with_bio'], counts['total']), (1, 1, 3, 4))  # like the list: no 'no'
        self.assertEqual(self.call('/api/counts?status=all')[1]['hot'], 2)
        m = self.call('/api/map?scope=all')[1]
        self.assertNotEqual(m['rev'], rev)
        self.assertEqual({n['id'] for n in m['nodes'] if n['kind'] == 'seed'}, {'s:s1', 's:s2'})
        self.assertIn({'source': 's:s1', 'target': f"p:{ids['ben']}", 'direction': 'followers'},
                      [{k: link[k] for k in ('source', 'target', 'direction')} for link in m['links']])
        self.assertEqual(set(m['nodes'][0]), {'id', 'kind', 'label', 'tier', 'score', 'pic', 'degree', 'followers', 'status', 'lists',
                                              'tags', 'seeds', 'is_me', 'pid', 'note'})
        self.assertEqual(self.call(f"/img/{ids['ben']}")[0], 404)

    def test_connected_sort_min_lists_and_tag_sources(self):
        ids = {}
        for h, seeds, fol in [('ann', ['s1', 's2', 's3'], 10), ('ben', ['s1', 's2'], 500), ('cat', ['s1', 's2'], 900), ('dan', ['s1'], 5000)]:
            ids[h] = db.upsert_person(self.conn, {'handle': h, 'bio': 'founder', 'followers': fol})
            for s in seeds:
                db.add_edge(self.conn, s, ids[h], 'followers')
                db.add_edge(self.conn, s, ids[h], 'following')  # both directions still count as one list
        self.conn.commit()
        server.qualify_batch(self.conn)
        res = self.call('/api/leads?sort=connected')[1]
        self.assertEqual([(r['handle'], r['lists']) for r in res['rows']], [('ann', 3), ('cat', 2), ('ben', 2), ('dan', 1)])
        res = self.call('/api/leads?min_lists=2&sort=connected')[1]
        self.assertEqual((res['total'], [r['handle'] for r in res['rows']]), (3, ['ann', 'cat', 'ben']))
        self.assertEqual(self.call('/api/leads?min_lists=x')[0], 400)
        m = self.call('/api/map?scope=all')[1]
        self.assertEqual({n['label']: n['degree'] for n in m['nodes'] if n['kind'] == 'lead'}, {'ann': 3, 'ben': 2, 'cat': 2, 'dan': 1})
        self.call(f"/api/person/{ids['ann']}/tags", {'add': ['via @s1']})  # same tag name, manual on ann
        tags = self.call('/api/tags')[1]
        self.assertIn({'tag': 'via @s1', 'grp': 'source', 'count': 3, 'total': 3, 'source': 'auto'}, tags)
        self.assertIn({'tag': 'via @s1', 'grp': 'source', 'count': 1, 'total': 1, 'source': 'manual'}, tags)

    def test_heartbeat_rate_and_soak(self):
        self.call('/api/ext/heartbeat', {'version': '3.9.0', 'state': 'running', 'rate': {'pages_hour': 300, 'people_hour': 7400.55,
                                                                                    'last_hit_at': '2026-09-24T10:00:00Z', 'x': 1}})
        s = self.call('/api/scraper')[1]
        self.assertEqual(s['ext']['rate'], {'pages_hour': 300.0, 'people_hour': 7400.6, 'last_hit_at': '2026-09-24T10:00:00.000000+00:00'})
        self.call('/api/ext/heartbeat', {'version': '3.9.0', 'rate': {'pages_hour': 'fast', 'last_hit_at': 'nope'}})
        self.assertEqual(self.call('/api/scraper')[1]['ext']['rate'], {'pages_hour': None, 'people_hour': None, 'last_hit_at': None})
        self.call('/api/scraper/seeds', {'handles': ['s'], 'directions': ['followers']})
        job = self.call('/api/ext/next')[1]['job']
        self.page(job, [{'ig_id': '1', 'handle': 'a'}, {'ig_id': '2', 'handle': 'b'}], cursor='c1')
        self.page(job, [{'ig_id': '1', 'handle': 'a'}, {'ig_id': '2', 'handle': 'b'}], cursor='c1', requested_cursor=None)  # retry
        soak = self.call('/api/scraper')[1]['soak']
        self.assertEqual(soak['1h'], {'pages': 1, 'people': 2, 'new_people': 2, 'profiles': 0})
        self.assertEqual(soak['6h']['pages'], 1)

    def test_qualify_toggle_and_auto(self):
        s = self.call('/api/scraper')[1]
        self.assertEqual((s['qualify'], s['qualify_auto']), (False, False))
        self.assertEqual(self.call('/api/settings/qualify', {'on': 'yes'})[0], 400)
        self.assertEqual(self.call('/api/settings/qualify', {'on': True})[1], {'ok': True, 'qualify': True})
        self.assertTrue(self.call('/api/scraper')[1]['qualify'])
        self.call('/api/settings/qualify', {'on': False, 'auto': True})
        # auto (opt-in): stays off while a list is queued, flips on when all are done
        self.call('/api/scraper/seeds', {'handles': ['s'], 'directions': ['followers']})
        self.assertFalse(server.auto_qualify(self.conn))
        job = self.call('/api/ext/next')[1]['job']
        self.page(job, [{'ig_id': '1', 'handle': 'a'}], done=True)
        self.assertTrue(server.auto_qualify(self.conn))
        self.assertTrue(self.call('/api/scraper')[1]['qualify'])
        self.call('/api/settings/qualify', {'on': False, 'auto': False})
        self.assertFalse(server.auto_qualify(self.conn))

    def test_qualify_end_to_end(self):
        pid = db.upsert_person(self.conn, {'ig_id': '5', 'handle': 'eve'})
        db.add_edge(self.conn, 's1', pid, 'followers')
        self.conn.commit()
        server.qualify_batch(self.conn)
        self.call('/api/settings/qualify', {'on': True})
        self.assertEqual(server.plan_profiles(self.conn), 1)
        job = self.call('/api/ext/next')[1]['job']
        self.assertEqual((job['kind'], job['handle']), ('profile', 'eve'))
        self.call('/api/ext/profile', {'job_id': job['id'], 'profile': {'ig_id': '5', 'handle': 'eve', 'bio': 'founder'}})
        server.qualify_batch(self.conn)
        self.assertEqual(self.conn.execute('SELECT tier, model FROM verdicts WHERE person_id=?', (pid,)).fetchone()[:], ('hot', 'rules'))
        skip = {}
        self.assertFalse(server.llm_step(self.conn, skip))  # model unavailable: rule verdict stays
        self.assertIn(pid, skip)
        self.assertIsNone(server.llm_step(self.conn, skip))  # held back, nothing else to do
        server.qualify.llm_verdict = lambda p, tags, edges: {'score': 90, 'tier': 'hot', 'role': 'buyer', 'reason': 'llm', 'model': 'm',
                                                             'tags': [('Skincare', 'niche')]}
        try:
            self.assertTrue(server.llm_step(self.conn, {}))
        finally:
            server.qualify.llm_verdict = lambda p, tags, edges: None
        v = self.conn.execute('SELECT score, model, input_hash FROM verdicts WHERE person_id=?', (pid,)).fetchone()
        self.assertEqual(v[:], (90, 'm', 'h'))
        self.assertIn(('Skincare', 'niche', 'auto'), [tuple(r) for r in self.conn.execute('SELECT tag, grp, source FROM tags')])

    def test_late_messages_do_not_corrupt_jobs(self):
        self.call('/api/scraper/seeds', {'handles': ['s'], 'directions': ['followers']})
        job = self.call('/api/ext/next')[1]['job']
        # a profile post carrying a list job's id must not finish the list job
        self.call('/api/ext/profile', {'job_id': job['id'], 'profile': {'handle': 's', 'bio': 'x'}})
        self.assertEqual(self.conn.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'leased')
        self.page(job, [], done=True)
        self.call('/api/ext/error', {'job_id': job['id'], 'code': 'rate_limit', 'message': '429'})  # late error
        self.assertEqual(self.conn.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'done')
        self.assertEqual(self.conn.execute("SELECT state FROM lists WHERE seed='s'").fetchone()[0], 'partial')

    def test_budget_defaults_not_mutated(self):
        self.call('/api/scraper/budget', {'profile': 7})
        self.assertEqual(db.DEFAULTS['budget'], {'list': 3000, 'profile': 300})

    def test_migration(self):
        old = Path(self.tmp.name) / 'old.sqlite'
        c = sqlite3.connect(old)
        c.executescript("""
            CREATE TABLE entities(id TEXT PRIMARY KEY, platform TEXT, handle TEXT, platform_id TEXT, created_at TEXT);
            CREATE TABLE edges(src TEXT, dst TEXT, relationship TEXT, observed_date TEXT);
            CREATE TABLE collections(source_entity TEXT, side TEXT, displayed_count INT, captured_count INT, coverage TEXT,
              observed_date TEXT, stop_reason TEXT);
            CREATE TABLE profile_current(entity_id TEXT, field TEXT, value_json TEXT, observed_at TEXT);
            CREATE TABLE known_people(entity_id TEXT, relation TEXT, note TEXT, added_at TEXT);
            CREATE TABLE exporter_commands(payload TEXT, type TEXT, created_at TEXT);
            INSERT INTO entities VALUES('S','instagram','seedy','100','t'),('A','instagram','amy','1','t'),
              ('B','instagram','bo',NULL,'t'),('M','instagram','fortun8te','5','t'),('T','tiktok','amy',NULL,'t');
            INSERT INTO edges VALUES('A','S','follows','2026-09-01'),('S','B','follows','2026-09-01'),
              ('B','M','follows','2026-09-02'),('A','B','follows','2026-09-02'),('T','S','follows','x');
            INSERT INTO collections VALUES('S','followers',50,1,'partial','2026-09-01','partial'),
              ('S','following',1,1,'complete','2026-09-01','done');
            INSERT INTO profile_current VALUES('A','biography','"founder"','2026-09-03'),('A','is_private','false','x'),
              ('A','followers_count','1500','x'),('A','display_name','"Amy"','x');
            INSERT INTO known_people VALUES('B','client','Worked with them','t');
            INSERT INTO exporter_commands VALUES('{"handle": "newseed", "lists": ["followers"]}','collect','t');
        """)
        c.commit()
        c.close()
        batch = Path(self.tmp.name) / 'batch.txt'
        batch.write_text('seedy\nnewseed\n')
        out = Path(self.tmp.name) / 'new.sqlite'
        for _ in range(2):
            s = migrate.migrate(old, out, batch)
        self.assertEqual((s['people'], s['edges'], s['seeds'], s['bios'], s['marks'], s['skipped_edges']), (4, 3, 3, 1, 1, 2))
        n = db.connect(str(out))
        edges = {tuple(r) for r in n.execute('SELECT e.seed, p.handle, e.direction FROM edges e JOIN people p ON p.id=e.person_id')}
        self.assertEqual(edges, {('seedy', 'amy', 'followers'), ('seedy', 'bo', 'following'), ('fortun8te', 'bo', 'followers')})
        amy = n.execute("SELECT * FROM people WHERE handle='amy'").fetchone()
        self.assertEqual((amy['ig_id'], amy['name'], amy['bio'], amy['is_private'], amy['followers']), ('1', 'Amy', 'founder', 0, 1500))
        lists = {(r['seed'], r['direction']): r['state'] for r in n.execute('SELECT * FROM lists')}
        self.assertEqual(lists[('seedy', 'following')], 'done')
        self.assertEqual(lists[('seedy', 'followers')], 'queued')
        self.assertEqual(lists[('fortun8te', 'followers')], 'done')
        jobs = sorted(tuple(r) for r in n.execute("SELECT seed, direction FROM jobs WHERE state='queued'"))
        self.assertEqual(jobs, [('newseed', 'followers'), ('newseed', 'following'), ('seedy', 'followers')])
        self.assertEqual(n.execute("SELECT is_me FROM seeds WHERE handle='fortun8te'").fetchone()[0], 1)
        self.assertEqual(tuple(n.execute('SELECT status, note FROM marks').fetchone()), ('client', 'client: Worked with them'))
        n.close()


class TagsViewsMapTest(Base):
    """Shared lead filter, tag facets, tag management, tag rules, saved views, map v2, queue safety."""

    def people(self, spec):
        """spec: handle -> (bio, followers, seeds). Qualifies with the stub (seed tags 'via @s', 'founder' role tag)."""
        ids = {}
        for h, (bio, fol, seeds) in spec.items():
            ids[h] = db.upsert_person(self.conn, {'handle': h, 'bio': bio, 'followers': fol, 'name': h.title()})
            for s in seeds:
                db.add_edge(self.conn, s, ids[h], 'followers')
        self.conn.commit()
        server.qualify_batch(self.conn)
        return ids

    def test_scores_are_separate_and_edges_keep_direction(self):
        ids = self.people({'ann': ('ordinary profile', 200, ['s1', 's2', 's3', 's4'])})
        pid = ids['ann']
        db.add_edge(self.conn, 's1', pid, 'following')
        self.conn.commit()
        lead = self.call('/api/leads?status=all')[1]['rows'][0]
        self.assertEqual((lead['business_fit'], lead['score']), (30, 30))
        self.assertEqual(lead['connection_strength'], 40)
        self.assertEqual(self.call(f'/api/person/{pid}')[1]['business_fit'], 30)
        self.assertEqual({e['direction'] for e in self.call(f'/api/person/{pid}')[1]['edges'] if e['seed'] == 's1'},
                         {'followers', 'following'})
        node = next(n for n in self.call('/api/map?scope=all')[1]['nodes'] if n['id'] == f'p:{pid}')
        self.assertEqual((node['business_fit'], node['connection_strength']), (30, 40))

    def test_legacy_model_fit_stays_unknown(self):
        ids = self.people({'ann': ('founder', 200, ['s1']), 'ben': ('ordinary profile', 100, ['s1'])})
        pid = ids['ben']
        self.conn.execute("UPDATE verdicts SET model='llm', content_fit=NULL, input_hash='h' WHERE person_id=?", (pid,))
        self.conn.commit()
        blend, tier = stub.blend, stub._tier
        stub.NET_WEIGHT = 0.6
        stub.blend = lambda content, net: round(.6 * stub.network_strength(net) + .4 * content)
        stub._tier = lambda score, has_bio: 'hot' if score >= 70 else 'warm' if score >= 45 else 'cold'
        try:
            self.call(f"/api/person/{ids['ann']}/mark", {'status': 'client'})
        finally:
            stub.blend, stub._tier = blend, tier
            del stub.NET_WEIGHT
        self.assertIsNone(self.conn.execute('SELECT content_fit FROM verdicts WHERE person_id=?', (pid,)).fetchone()[0])
        self.assertIsNone(self.call(f'/api/person/{pid}')[1]['business_fit'])

    def rows(self, qs, path='/api/leads?'):
        code, out = self.call(path + qs)
        self.assertEqual(code, 200, out)
        return [r['handle'] for r in out['rows']]

    def tagset(self, pid, source=None):
        sql = 'SELECT tag FROM tags WHERE person_id=?' + (' AND source=?' if source else '')
        return {r[0] for r in self.conn.execute(sql, (pid, source) if source else (pid,))}

    SPEC = {'ann': ('founder of glow skincare', 5000, ['s1', 's2']), 'ben': ('founder', 300, ['s1']),
            'cat': ('hobby', 90000, ['s2']), 'dan': (None, None, ['s3'])}

    def test_fit_sort_counts_filter_evidence_and_map_fit(self):
        ids = self.people({'ann': ('founder', 10, ['s1']), 'ben': ('founder', 20, ['s1', 's2']), 'cat': ('hobby', 30, ['s1', 's2', 's3']),
                           'dan': (None, 40, ['s1', 's2', 's3', 's4'])})
        # stub tiers: founder -> hot (80), hobby -> cold (30), no bio -> unread; lists break ties inside a tier
        self.assertEqual(self.rows('sort=fit'), ['ben', 'ann', 'cat', 'dan'])
        self.assertEqual(self.call('/api/leads?sort=nope')[0], 400)
        self.call('/api/people/bulk', {'ids': [ids['ben']], 'status': 'interested'})
        self.call('/api/people/bulk', {'ids': [ids['cat']], 'status': 'no'})
        c = self.call('/api/counts?min_lists=2&tier=hot')[1]
        self.assertEqual((c['hot'], c['cold'], c['unread'], c['interested'], c['no'], c['total']), (1, 0, 1, 1, 0, 4))  # own dimension ignored
        c = self.call('/api/counts?min_lists=2')[1]
        self.assertEqual((c['no'], c['interested'], c['none'], c['open']), (1, 1, 1, 2))
        self.assertEqual(self.call('/api/counts?q=zzz')[1]['hot'], 0)
        self.conn.execute("UPDATE verdicts SET evidence=? WHERE person_id=?", (json.dumps(['founder']), ids['ann']))
        self.conn.execute("UPDATE verdicts SET evidence='not json' WHERE person_id=?", (ids['ben'],))
        self.conn.commit()
        self.assertEqual(self.call(f"/api/person/{ids['ann']}")[1]['verdict']['evidence'], ['founder'])
        self.assertEqual(self.call(f"/api/person/{ids['ben']}")[1]['verdict']['evidence'], [])
        fits = {n['label']: n['fit'] for n in self.call('/api/map?scope=all&status=all')[1]['nodes'] if n['kind'] == 'lead'}
        self.assertEqual(fits, {'ann': 'strong', 'ben': 'strong', 'cat': 'weak', 'dan': 'unread'})

    def test_shared_filter(self):
        ids = self.people(self.SPEC)
        self.call('/api/people/bulk', {'ids': [ids['ben'], ids['cat']], 'add': ['vip']})
        self.call('/api/people/bulk', {'ids': [ids['cat']], 'add': ['nl']})
        self.assertEqual(self.rows('tags=founder,vip&sort=followers'), ['ben'])
        self.assertEqual(self.rows('any=vip,via%20@s3&sort=followers'), ['cat', 'ben', 'dan'])
        self.assertEqual(self.rows('any=vip&not=nl'), ['ben'])
        self.assertEqual(self.rows('not=founder&sort=followers'), ['cat', 'dan'])
        self.assertEqual(self.rows('has_bio=0'), ['dan'])
        self.assertEqual(sorted(self.rows('has_bio=1')), ['ann', 'ben', 'cat'])
        self.assertEqual(self.rows('seed=s2&sort=followers'), ['cat', 'ann'])
        self.assertEqual(self.rows('seed=@S1,s2'), ['ann'])
        self.assertEqual(self.rows('followers_min=300&followers_max=5000&sort=followers'), ['ann', 'ben'])
        self.assertEqual(self.rows('followers_min=100000'), [])
        self.call(f"/api/person/{ids['ben']}/mark", {'status': 'no'})
        self.call(f"/api/person/{ids['cat']}/mark", {'status': 'interested'})
        self.assertNotIn('ben', self.rows(''))
        self.assertEqual(self.rows('status=no'), ['ben'])
        self.assertEqual(sorted(self.rows('status=none')), ['ann', 'dan'])
        self.assertEqual(sorted(self.rows('status=good,none')), ['ann', 'cat', 'dan'])
        self.assertEqual(len(self.rows('status=all')), 4)
        for bad in ('has_bio=maybe', 'followers_min=lots', 'status=weird', 'limit=x'):
            self.assertEqual(self.call('/api/leads?' + bad)[0], 400, bad)
        self.assertEqual(self.call('/api/map?has_bio=2')[0], 400)

    def test_facets_and_tag_order(self):
        ids = self.people(self.SPEC)
        self.call(f"/api/person/{ids['ann']}/tags", {'add': ['zz manual']})
        server.qualify_batch(self.conn)
        facets = {(f['tag'], f['source']): f for f in self.call('/api/tags?seed=s2')[1]}
        self.assertEqual(facets[('founder', 'auto')], {'tag': 'founder', 'grp': 'role', 'source': 'auto', 'count': 1, 'total': 2})
        self.assertEqual(facets[('via @s1', 'auto')]['count'], 1)
        self.assertEqual((facets[('via @s3', 'auto')]['count'], facets[('via @s3', 'auto')]['total']), (0, 1))
        self.assertEqual({(f['count'], f['total']) for f in self.call('/api/tags')[1] if f['tag'] == 'founder'}, {(2, 2)})
        self.assertEqual(self.call('/api/tags?tags=nothing')[1][0]['count'], 0)
        ann = self.call(f"/api/person/{ids['ann']}")[1]
        self.assertEqual(ann['tags'][0], {'tag': 'zz manual', 'grp': 'signal', 'source': 'manual'})
        self.assertEqual([t['grp'] for t in ann['tags'][1:]], ['role', 'source', 'source'])

    def test_rename_and_delete_manual_tags(self):
        ids = self.people(self.SPEC)
        self.call('/api/people/bulk', {'ids': [ids['ann'], ids['ben']], 'add': ['warm lead']})
        self.call('/api/people/bulk', {'ids': [ids['ben']], 'add': ['priority']})
        self.assertEqual(self.call('/api/tags/rename', {'from': 'warm lead', 'to': 'priority'})[1], {'ok': True, 'renamed': 2})
        rows = {(r['tag'], r['person_id'], r['source']) for r in self.conn.execute("SELECT * FROM tags WHERE tag IN ('warm lead','priority')")}
        self.assertEqual(rows, {('priority', ids['ann'], 'manual'), ('priority', ids['ben'], 'manual')})  # merged, no duplicate
        # renaming onto an auto tag name upgrades that person's tag to manual (it survives requalify)
        self.call('/api/people/bulk', {'ids': [ids['ann']], 'add': ['x']})
        self.call('/api/tags/rename', {'from': 'x', 'to': 'founder'})
        server.qualify_batch(self.conn)
        self.assertEqual(self.conn.execute("SELECT source, grp FROM tags WHERE tag='founder' AND person_id=?", (ids['ann'],)).fetchone()[:],
                         ('manual', 'role'))
        self.assertEqual(self.call('/api/tags/delete', {'tag': 'founder'})[1], {'ok': True, 'deleted': 1})  # manual only
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tags WHERE tag='founder'").fetchone()[0], 1)  # ben's auto one
        self.assertEqual(self.call('/api/tags/rename', {'from': '', 'to': 'a'})[0], 400)
        self.assertEqual(self.call('/api/tags/rename', {'from': 'a,b', 'to': 'c'})[0], 400)
        self.assertEqual(self.call('/api/tags/delete', {})[0], 400)

    def test_bulk(self):
        ids = self.people(self.SPEC)
        allids = list(ids.values())
        out = self.call('/api/people/bulk', {'ids': allids + [999999], 'add': ['  batch   one ', 'b2'], 'status': 'contacted'})[1]
        self.assertEqual(out, {'ok': True, 'updated': 4, 'updated_ids': sorted(allids), 'missing_ids': [999999]})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tags WHERE tag='batch one' AND source='manual'").fetchone()[0], 4)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM marks WHERE status='contacted'").fetchone()[0], 4)
        self.call(f"/api/person/{ids['ann']}/mark", {'status': 'interested', 'note': 'call'})
        self.call('/api/people/bulk', {'ids': allids, 'remove': ['b2']})  # no status key: marks untouched
        self.assertEqual(self.conn.execute("SELECT count(*) FROM marks WHERE status IS NOT NULL").fetchone()[0], 4)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tags WHERE tag='b2'").fetchone()[0], 0)
        self.call('/api/people/bulk', {'ids': allids, 'status': None})
        self.assertEqual([tuple(r) for r in self.conn.execute('SELECT person_id, status, note FROM marks')], [(ids['ann'], None, 'call')])
        self.assertEqual(self.call('/api/people/bulk', {'ids': list(range(5001))})[0], 400)
        self.assertEqual(self.call('/api/people/bulk', {'ids': ['1']})[0], 400)
        self.assertEqual(self.call('/api/people/bulk', {'ids': allids, 'status': 'meh'})[0], 400)
        self.assertEqual(self.call('/api/people/bulk', {'ids': [], 'add': ['x']})[1], {'ok': True, 'updated': 0, 'updated_ids': [], 'missing_ids': []})

    def test_tag_rules(self):
        ids = self.people(self.SPEC)
        self.call('/api/people/bulk', {'ids': [ids['cat']], 'add': ['Skincare']})  # manual tag with the same name
        code, rule = self.call('/api/tag-rules', {'tag': 'Skincare', 'field': 'bio', 'match': 'skin care, skincare, serum*', 'grp': 'niche'})
        self.assertEqual(code, 200, rule)
        self.assertEqual({k: rule[k] for k in ('tag', 'grp', 'field', 'hits')}, {'tag': 'Skincare', 'grp': 'niche', 'field': 'bio', 'hits': 1})
        self.assertEqual(self.tagset(ids['ann'], 'rule'), {'Skincare'})
        self.assertEqual(self.tagset(ids['cat'], 'manual'), {'Skincare'})  # manual tag untouched
        # the same rule again is not duplicated
        self.assertEqual(self.call('/api/tag-rules', {'tag': 'Skincare', 'field': 'bio', 'match': 'skin care, skincare, serum*'})[1]['id'], rule['id'])
        # regex on handle; rules re-apply on list ingest (handle/name known) and profile ingest (bio known)
        r2 = self.call('/api/tag-rules', {'tag': 'Shop handle', 'field': 'handle', 'match': '/^shop|shop$/'})[1]
        self.assertEqual(r2['hits'], 0)
        self.call('/api/scraper/seeds', {'handles': ['s9'], 'directions': ['followers']})
        job = self.call('/api/ext/next')[1]['job']
        self.page(job, [{'ig_id': '71', 'handle': 'shopglow'}, {'ig_id': '72', 'handle': 'eve'}], cursor='c1')
        glow = self.conn.execute("SELECT id FROM people WHERE handle='shopglow'").fetchone()[0]
        eve = self.conn.execute("SELECT id FROM people WHERE handle='eve'").fetchone()[0]
        self.assertEqual(self.tagset(glow, 'rule'), {'Shop handle'})
        self.call('/api/ext/profile', {'job_id': None, 'profile': {'ig_id': '72', 'handle': 'eve', 'bio': 'Serums for dry skin'}})
        self.assertEqual(self.tagset(eve, 'rule'), {'Skincare'})
        self.call('/api/ext/profile', {'job_id': None, 'profile': {'ig_id': '72', 'handle': 'eve', 'bio': 'now a baker'}})
        self.assertEqual(self.tagset(eve, 'rule'), set())  # stale rule tag removed
        self.call('/api/ext/profile', {'job_id': None, 'profile': {'ig_id': '72', 'handle': 'eve', 'bio': 'SERUM lab'}})
        # rule tags survive requalify (which only rebuilds 'auto') and show up as a facet with source 'rule'
        server.qualify_batch(self.conn)
        self.assertEqual(self.tagset(eve, 'rule'), {'Skincare'})
        self.assertIn({'tag': 'Skincare', 'grp': 'niche', 'source': 'rule', 'count': 2, 'total': 2}, self.call('/api/tags')[1])
        self.assertEqual(self.rows('tags=Skincare&sort=followers'), ['cat', 'ann', 'eve'])
        # a second rule for the same tag keeps its people when the first is deleted
        r3 = self.call('/api/tag-rules', {'tag': 'Skincare', 'field': 'any', 'match': 'glow'})[1]
        listed = {r['id']: r for r in self.call('/api/tag-rules')[1]}
        self.assertEqual(set(listed), {rule['id'], r2['id'], r3['id']})
        self.assertEqual(set(listed[r3['id']]), {'id', 'tag', 'grp', 'field', 'match', 'hits'})
        self.assertEqual(self.call(f"/api/tag-rules/{rule['id']}/delete", {})[1], {'ok': True, 'deleted': 1})
        self.assertEqual(self.tagset(ids['ann'], 'rule'), {'Skincare'})   # still matches 'glow'
        self.assertEqual(self.tagset(glow, 'rule'), {'Shop handle', 'Skincare'})
        self.assertEqual(self.tagset(eve, 'rule'), set())
        self.assertEqual(self.tagset(ids['cat'], 'manual'), {'Skincare'})
        self.assertEqual(self.call('/api/tag-rules/99999/delete', {})[1], {'ok': True, 'deleted': 0})
        for bad in ({'tag': 'x', 'field': 'bio', 'match': '/(unclosed/'}, {'tag': 'x', 'field': 'bio', 'match': '/(a+)+$/'},
                    {'tag': 'x', 'field': 'bio', 'match': '/' + 'a' * 201 + '/'}, {'tag': 'x', 'field': 'email', 'match': 'a'},
                    {'tag': 'x', 'field': 'bio', 'match': ' , '}, {'tag': '', 'field': 'bio', 'match': 'a'},
                    {'tag': 'x', 'field': 'bio', 'match': '/(a)\\1/'}, {'tag': 'x', 'field': 'bio', 'match': '/a.*b.*c.*d.*/'}):
            code, out = self.call('/api/tag-rules', bad)
            self.assertEqual((code, out['ok']), (400, False), bad)
        self.assertEqual(len(self.call('/api/tag-rules')[1]), 2)

    def test_saved_views(self):
        self.assertEqual(self.call('/api/views')[1], [])
        v = self.call('/api/views', {'name': ' Skincare US ', 'query': '?tags=Skincare,US&sort=connected'})[1]
        self.assertEqual((v['ok'], v['name'], v['query']), (True, 'Skincare US', 'tags=Skincare,US&sort=connected'))
        self.call('/api/views', {'name': 'Agencies', 'query': 'tags=Agency'})
        self.call('/api/views', {'name': 'skincare us', 'query': 'tags=Skincare'})  # same name (any case) overwrites
        views = self.call('/api/views')[1]
        self.assertEqual([(x['name'], x['query']) for x in views], [('Agencies', 'tags=Agency'), ('Skincare US', 'tags=Skincare')])
        self.assertEqual(set(views[0]), {'id', 'name', 'query'})
        self.assertEqual(self.call(f"/api/views/{v['id']}/delete", {})[1], {'ok': True, 'deleted': 1})
        self.assertEqual([x['name'] for x in self.call('/api/views')[1]], ['Agencies'])
        self.assertEqual(self.call('/api/views', {'name': '', 'query': 'a=b'})[0], 400)
        self.assertEqual(self.call('/api/views', {'name': 'x', 'query': None})[0], 400)

    def test_map_filter_nodes_and_seed_links(self):
        ids = self.people({'ann': ('founder', 5000, ['s1', 's2', 's3']), 'ben': ('founder', 300, ['s1', 's2']),
                           'cat': ('hobby', 90000, ['s2', 's3']), 'dan': (None, None, ['s1'])})
        self.call('/api/people/bulk', {'ids': [ids['ann']], 'add': ['m1', 'm2'], 'status': 'interested'})
        server.qualify_batch(self.conn)
        m = self.call('/api/map?scope=all')[1]
        leads = {n['label']: n for n in m['nodes'] if n['kind'] == 'lead'}
        ann = leads['ann']
        self.assertEqual((ann['lists'], ann['degree'], ann['status'], ann['followers'], ann['seeds']), (3, 3, 'interested', 5000, ['s1', 's2', 's3']))
        self.assertEqual(ann['tags'], ['m1', 'm2', 'founder', 'via @s1'])  # max 4, manual first
        self.assertEqual(leads['dan']['tags'], ['via @s1'])
        self.assertEqual(m['seed_links'], [{'source': 's:s1', 'target': 's:s2', 'shared': 2},
                                           {'source': 's:s2', 'target': 's:s3', 'shared': 2},
                                           {'source': 's:s1', 'target': 's:s3', 'shared': 1}])
        f = self.call('/api/map?scope=all&tags=founder&seed=s2')[1]
        self.assertEqual({n['label'] for n in f['nodes'] if n['kind'] == 'lead'}, {'ann', 'ben'})
        self.assertEqual({n['id'] for n in f['nodes'] if n['kind'] == 'seed'}, {'s:s1', 's:s2', 's:s3'})
        self.assertTrue(all(link['target'] in {f"p:{ids['ann']}", f"p:{ids['ben']}"} for link in f['links']))
        self.assertEqual(f['seed_links'], m['seed_links'])  # audience overlap is structural, not filtered
        self.assertEqual({n['label'] for n in self.call('/api/map?any=m1&not=m2')[1]['nodes'] if n['kind'] == 'lead'}, set())
        self.assertEqual({n['label'] for n in self.call('/api/map?status=good')[1]['nodes'] if n['kind'] == 'lead'}, {'ann'})
        rev = m['rev']
        self.call('/api/tag-rules', {'tag': 'Hobby', 'field': 'bio', 'match': 'hobby'})
        self.assertNotEqual(self.call('/api/map')[1]['rev'], rev)  # rule tags change the map
        db.add_edge(self.conn, 's3', ids['dan'], 'following')
        self.conn.commit()
        shared = lambda: [x['shared'] for x in self.call('/api/map')[1]['seed_links']]  # noqa: E731
        self.assertEqual(shared(), [2, 2, 1])  # recomputed at most every SEED_LINKS_MIN_AGE s while edges stream in
        old, server.SEED_LINKS_MIN_AGE = server.SEED_LINKS_MIN_AGE, 0
        try:
            self.assertEqual(shared(), [2, 2, 2])
        finally:
            server.SEED_LINKS_MIN_AGE = old

    def test_rule_preview(self):
        self.people(self.SPEC)
        prev = lambda qs: self.call('/api/tag-rules/preview?' + qs)  # noqa: E731
        self.assertEqual(prev('field=bio&match=founder'), (200, {'hits': 2}))
        self.assertEqual(prev('field=bio&match=%2Fglow%20skin%5Cw%2B%2F'), (200, {'hits': 1}))   # /glow skin\w+/
        self.assertEqual(prev('field=handle&match=an'), (200, {'hits': 2}))  # ann, dan: substring
        self.assertEqual(prev('field=any&match=zzz'), (200, {'hits': 0}))
        for bad in ('field=bio&match=%2F(a%2B)%2B%2F', 'field=bio&match=', 'field=email&match=x', 'field=bio&match=%2F(%2F'):
            code, out = prev(bad)
            self.assertEqual((code, out['ok'], bool(out['error'])), (400, False, True), bad)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tag_rules').fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tags WHERE source='rule'").fetchone()[0], 0)

    def test_map_marks_my_seed(self):
        pid = db.upsert_person(self.conn, {'handle': 'friend'})
        db.add_edge(self.conn, 'fortun8te', pid, 'followers')
        db.add_edge(self.conn, 'other', pid, 'followers')
        self.conn.execute("INSERT INTO seeds(handle, is_me) VALUES('fortun8te', 1)")
        self.conn.commit()
        seeds = {n['id']: n['is_me'] for n in self.call('/api/map')[1]['nodes'] if n['kind'] == 'seed'}
        self.assertEqual(seeds, {'s:fortun8te': True, 's:other': False})

    def test_leases_expire_and_requeue(self):
        self.call('/api/scraper/seeds', {'handles': ['s'], 'directions': ['followers']})
        job = self.call('/api/ext/next')[1]['job']
        self.assertIsNone(self.call('/api/ext/next')[1]['job'])  # leased
        self.conn.execute("UPDATE jobs SET leased_until='2000-01-01' WHERE id=?", (job['id'],))
        self.conn.commit()
        again = self.call('/api/ext/next')[1]['job']
        self.assertEqual(again['id'], job['id'])
        self.assertEqual(self.conn.execute('SELECT attempts FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 2)
        # a profile read whose lease keeps expiring is parked after PROFILE_MAX_ATTEMPTS
        self.page(again, [], done=True)
        pid = db.upsert_person(self.conn, {'handle': 'hang'})
        self.conn.commit()
        self.call(f'/api/person/{pid}/read', {})
        for i in range(server.PROFILE_MAX_ATTEMPTS):
            j = self.call('/api/ext/next')[1]['job']
            self.assertEqual(j['handle'], 'hang', i)
            self.conn.execute("UPDATE jobs SET leased_until='2000-01-01' WHERE id=?", (j['id'],))
            self.conn.commit()
        self.assertIsNone(self.call('/api/ext/next')[1]['job'])
        self.assertEqual(self.conn.execute('SELECT state FROM jobs WHERE id=?', (j['id'],)).fetchone()[0], 'error')

    def test_planner_fit_first_then_lists(self):
        self.people({'one_hi': ('', None, ['s1']), 'three_lo': ('', None, ['s1', 's2', 's3']), 'two_hi': ('', None, ['s1', 's2']),
                     'two_lo': ('', None, ['s1', 's2'])})
        self.conn.execute('UPDATE people SET bio=NULL, bio_at=NULL')
        for h, pre in (('one_hi', 99), ('three_lo', 1), ('two_hi', 80), ('two_lo', 5)):
            self.conn.execute('UPDATE verdicts SET prefilter=? WHERE person_id=(SELECT id FROM people WHERE handle=?)', (pre, h))
        db.set_setting(self.conn, 'qualify', True)
        self.conn.commit()
        self.assertEqual(server.plan_profiles(self.conn), 4)
        order = [r[0] for r in self.conn.execute("SELECT handle FROM jobs WHERE kind='profile' ORDER BY priority DESC")]
        self.assertEqual(order, ['one_hi', 'two_hi', 'two_lo', 'three_lo'])
        self.assertLess(server.plan_priority(50, 100), server.READ_PRIORITY)
        self.assertGreater(server.plan_priority(2, 50), server.plan_priority(1, 50))  # lists break ties
        self.assertEqual([self.call('/api/ext/next')[1]['job']['handle'] for _ in range(2)], ['one_hi', 'two_hi'])

    def test_llm_exception_falls_back(self):
        pid = self.people({'eve': ('founder', 10, ['s1'])})['eve']
        db.set_setting(self.conn, 'qualify', True)
        self.conn.commit()

        def boom(p, tags, edges):
            raise RuntimeError('bad reply')
        server.qualify.llm_verdict = boom
        try:
            skip = {}
            with contextlib.redirect_stderr(io.StringIO()):  # the worker logs the traceback; expected here
                self.assertFalse(server.llm_step(self.conn, skip))
            self.assertIn(pid, skip)
        finally:
            server.qualify.llm_verdict = lambda p, tags, edges: None
        self.assertEqual(self.conn.execute('SELECT model, tier FROM verdicts WHERE person_id=?', (pid,)).fetchone()[:], ('rules', 'hot'))

    def test_retag_on_taxonomy_change(self):
        pid = self.people({'eve': ('founder', 10, ['s1'])})['eve']
        self.conn.execute("UPDATE verdicts SET model='m', score=91, content_fit=91, input_hash='h'")  # an LLM verdict (stub hash is 'h')
        self.conn.execute("DELETE FROM tags WHERE source='auto'")  # stands in for tags from an older taxonomy
        self.conn.commit()
        self.assertFalse(server.retag_if_changed(self.conn))  # stub has no TAGS_VERSION
        server.qualify.TAGS_VERSION = 't9'
        try:
            self.assertTrue(server.retag_if_changed(self.conn))
            self.assertFalse(server.retag_if_changed(self.conn))  # once per version
            self.assertEqual(server.qualify_batch(self.conn), 1)
        finally:
            del server.qualify.TAGS_VERSION
        self.assertEqual(self.tagset(pid, 'auto'), {'founder', 'via @s1'})  # re-derived
        self.assertEqual(self.conn.execute('SELECT model, score FROM verdicts WHERE person_id=?', (pid,)).fetchone()[:], ('m', 91))
        self.assertEqual(server.qualify_batch(self.conn), 0)

    def test_old_tags_table_is_migrated(self):
        path = str(Path(self.tmp.name) / 'old.sqlite')
        c = sqlite3.connect(path)
        c.executescript("""CREATE TABLE tags(person_id INT, tag TEXT, grp TEXT, source TEXT CHECK(source IN('auto','manual')),
                             PRIMARY KEY(person_id,tag));
                           CREATE INDEX tags_tag ON tags(tag);
                           INSERT INTO tags VALUES(1,'vip','signal','manual'),(1,'Brand','role','auto');""")
        c.commit()
        c.close()
        for _ in range(2):  # idempotent
            n = db.init(path)
            self.assertEqual(sorted(tuple(r) for r in n.execute('SELECT * FROM tags')), [(1, 'Brand', 'role', 'auto'), (1, 'vip', 'signal', 'manual')])
            n.execute("INSERT INTO tags VALUES(2,'x','signal','rule')")
            self.assertRaises(sqlite3.IntegrityError, n.execute, "INSERT INTO tags VALUES(3,'x','signal','bogus')")
            self.assertRaises(sqlite3.IntegrityError, n.execute, "INSERT INTO tags VALUES(1,'vip','signal','rule')")
            idx = {r[0] for r in n.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tags'")}
            self.assertIn('tags_tag_src', idx)
            self.assertNotIn('tags_tag', idx)
            n.rollback()
            n.close()


class Redirector(BaseHTTPRequestHandler):
    hits = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Redirector.hits.append(self.path)
        self.send_response(302)
        self.send_header('Location', '/elsewhere')
        self.send_header('Content-Length', '0')
        self.end_headers()


class AuditTest(Base):
    """Fixes from the 2026-09-24 security/bug audit."""

    def raw(self, path, body=None, headers=None):
        port = server.CFG['port']
        req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', method='POST' if body is not None else 'GET',
                                     data=json.dumps(body).encode() if body is not None else None, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers)

    def test_mark_keeps_note_when_status_cleared(self):
        pid = db.upsert_person(self.conn, {'handle': 'ann'})
        self.conn.commit()
        mark = lambda: self.conn.execute('SELECT status, note FROM marks WHERE person_id=?', (pid,)).fetchone()  # noqa: E731
        self.call(f'/api/person/{pid}/mark', {'status': 'interested', 'note': 'met at expo'})
        self.call(f'/api/person/{pid}/mark', {'status': None})
        self.assertEqual(mark()[:], (None, 'met at expo'))
        self.call(f'/api/person/{pid}/mark', {'status': 'contacted'})
        self.assertEqual(mark()[:], ('contacted', 'met at expo'))
        self.call(f'/api/person/{pid}/mark', {'note': 'follow up'})  # status absent: kept
        self.assertEqual(mark()[:], ('contacted', 'follow up'))
        self.call(f'/api/person/{pid}/mark', {'note': ''})
        self.assertEqual(mark()[:], ('contacted', None))
        self.call(f'/api/person/{pid}/mark', {'status': None})
        self.assertIsNone(mark())  # nothing left: row removed
        self.assertEqual(self.call(f'/api/person/{pid}/mark', {'note': 5})[0], 400)

    def test_handle_taken_by_new_account(self):
        old = db.upsert_person(self.conn, {'ig_id': '100', 'handle': 'glow'})
        db.add_edge(self.conn, 's1', old, 'followers')
        self.conn.execute("INSERT INTO marks VALUES(?, 'client', 'x', 't')", (old,))
        new = db.upsert_person(self.conn, {'ig_id': '200', 'handle': 'glow', 'name': 'New Glow'})
        self.assertNotEqual(new, old)
        self.assertEqual(self.conn.execute('SELECT handle FROM people WHERE id=?', (old,)).fetchone()[0], f'glow~{old}')
        self.assertIsNone(self.conn.execute('SELECT 1 FROM marks WHERE person_id=?', (new,)).fetchone())
        self.assertEqual(self.conn.execute('SELECT count(*) FROM edges WHERE person_id=?', (new,)).fetchone()[0], 0)
        self.assertEqual(db.upsert_person(self.conn, {'ig_id': '200', 'handle': 'glow'}), new)  # stable afterwards
        same = db.upsert_person(self.conn, {'handle': 'solo'})
        self.assertEqual(db.upsert_person(self.conn, {'ig_id': '300', 'handle': 'solo'}), same)  # first ig_id: same account

    def test_profile_fields_sanitised(self):
        for site, stored in (('javascript:alert(1)', None), ('data:text/html,x', None), ('glow.com', None),
                             (' https://glow.com/shop ', 'https://glow.com/shop'), ('HTTP://x.co', 'HTTP://x.co')):
            h = f'p{abs(hash(site)) % 10 ** 6}'
            self.call('/api/ext/profile', {'job_id': None, 'profile': {'handle': h, 'website': site, 'followers': '1,234',
                                                                         'following': 'many', 'posts': True}})
            row = self.conn.execute('SELECT website, followers, following, posts FROM people WHERE handle=?', (h,)).fetchone()
            self.assertEqual(row[:], (stored, 1234, None, None), site)
        self.call('/api/ext/profile', {'job_id': None, 'profile': {'handle': 'keep', 'website': 'https://a.co', 'followers': 12.0}})
        self.call('/api/ext/profile', {'job_id': None, 'profile': {'handle': 'keep', 'website': 'vbscript:x', 'followers': -5}})
        self.assertEqual(self.conn.execute("SELECT website, followers FROM people WHERE handle='keep'").fetchone()[:], ('https://a.co', 12))
        self.assertEqual(self.call('/api/ext/profile', {'job_id': None, 'profile': 'x'})[0], 400)

    def test_security_headers_everywhere(self):
        for path in ('/api/counts', '/api/nothing', '/img/1', '/', '/missing.css'):
            code, headers = self.raw(path)
            with self.subTest(path=path, code=code):
                self.assertEqual(headers.get('X-Frame-Options'), 'DENY')
                self.assertEqual(headers.get('Content-Security-Policy'), "frame-ancestors 'none'")
                self.assertEqual(headers.get('X-Content-Type-Options'), 'nosniff')

    def test_ui_posts_need_same_origin(self):
        self.assertEqual(self.raw('/api/scraper/pause', {'paused': True})[0], 403)  # no Origin
        self.assertEqual(self.raw('/api/scraper/pause', {'paused': True}, {'Origin': 'null'})[0], 403)
        self.assertEqual(self.raw('/api/scraper/pause', {'paused': True}, {'Origin': f"http://localhost:{server.CFG['port']}"})[0], 200)
        self.assertEqual(self.raw('/api/counts')[0], 200)  # GETs stay open to curl
        self.assertTrue(db.get_setting(self.conn, 'paused'))  # only the same-origin call got through

    def test_pictures_never_follow_redirects(self):
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Redirector)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        Redirector.hits = []
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                server.PIC_OPENER.open(f'http://127.0.0.1:{httpd.server_address[1]}/pic.jpg', timeout=5)
            cm.exception.close()
            self.assertEqual(cm.exception.code, 302)
            self.assertEqual(Redirector.hits, ['/pic.jpg'])  # the redirect target was never requested
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertIsNone(server.fetch_pic('https://evil.example/a.jpg'))
        self.assertIsNone(server.fetch_pic('http://x.cdninstagram.com/a.jpg'))

    def test_seed_links_threads(self):
        pid = db.upsert_person(self.conn, {'handle': 'a'})
        for s in ('s1', 's2'):
            db.add_edge(self.conn, s, pid, 'followers')
        self.conn.commit()
        results = []

        def hit():
            for i in range(5):
                c = db.connect(server.CFG['db'])
                try:
                    if i % 2:
                        server.SEED_LINKS[0] = None  # force recomputes to interleave
                    results.append(server.seed_links(c))
                finally:
                    c.close()
        threads = [threading.Thread(target=hit) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(results), 30)
        self.assertTrue(all(r == [{'source': 's:s1', 'target': 's:s2', 'shared': 1}] for r in results))

    def test_only_real_errors_park_a_list(self):
        self.call('/api/scraper/seeds', {'handles': ['s'], 'directions': ['followers']})
        for code in ['rate_limit'] * 6 + ['soft_block', 'login'] + ['other'] * 4:
            job = self.call('/api/ext/next')[1]['job']
            self.assertIsNotNone(job, code)
            self.call('/api/ext/error', {'job_id': job['id'], 'code': code, 'message': code})
        self.assertEqual(self.conn.execute("SELECT state FROM lists WHERE seed='s'").fetchone()[0], 'queued')
        job = self.call('/api/ext/next')[1]['job']
        self.call('/api/ext/error', {'job_id': job['id'], 'code': 'other', 'message': 'boom'})  # 5th real error
        self.assertEqual(self.conn.execute("SELECT state FROM lists WHERE seed='s'").fetchone()[0], 'error')
        self.assertIsNone(self.call('/api/ext/next')[1]['job'])

    def test_rule_regex_guard_and_scan(self):
        for m in (r'/.{0,99}.{0,99}.{0,99}.{0,99}.{0,99}#/', r'/(?:a?){26}a{26}/', '/(ab|cd)+e/', '/(x)\\1/'):
            self.assertEqual(self.call('/api/tag-rules', {'tag': 'x', 'field': 'bio', 'match': m})[0], 400, m)
        for m in (r'/shop(ify)?/', r'/(?:founder|ceo) @\w+/'):
            self.assertEqual(self.call('/api/tag-rules', {'tag': 'ok', 'field': 'bio', 'match': m})[0], 200, m)
        db.upsert_person(self.conn, {'handle': 'p1', 'bio': 'Founder @glow'})
        self.conn.commit()
        c = db.connect(server.CFG['db'])
        try:
            rule = {'tag': 't', 'grp': 'signal', 'field': 'bio', 'match': 'founder'}
            self.assertEqual(len(server.rules.matching_ids(c, rule)), 1)
            self.assertFalse(c.in_transaction)  # the scan never holds the write lock
            old = server.rules.SCAN_BUDGET
            server.rules.SCAN_BUDGET = -1
            try:
                for i in range(1200):
                    db.upsert_person(self.conn, {'handle': f'bulk{i}'})
                self.conn.commit()
                code, out = self.call('/api/tag-rules', {'tag': 'slow', 'field': 'bio', 'match': 'zzz'})
                self.assertEqual((code, 'longer than' in out['error']), (400, True))
                self.assertIsNone(self.conn.execute("SELECT 1 FROM tag_rules WHERE tag='slow'").fetchone())
            finally:
                server.rules.SCAN_BUDGET = old
        finally:
            c.close()


if __name__ == '__main__':
    unittest.main()
