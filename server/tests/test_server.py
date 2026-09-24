import json
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

stub = types.ModuleType('qualify')
stub.prefilter = lambda p, seeds: 10 * len(seeds)
stub.rule_tags = lambda p, edges, me: [(f"via @{e['seed']}", 'source') for e in edges] + (
    [('founder', 'role')] if 'founder' in (p.get('bio') or '') else [])
stub.rule_verdict = lambda p, tags: {'score': 80 if ('founder', 'role') in tags else 30, 'role': 'x', 'reason': 'r',
                                     'tier': 'unread' if not p.get('bio') else 'hot' if ('founder', 'role') in tags else 'cold'}
stub.llm_verdict = lambda p, tags, edges: None
stub.input_hash = lambda p, edges: 'h'
sys.modules['qualify'] = stub

import db  # noqa: E402
import migrate  # noqa: E402
import server  # noqa: E402

EXT = server.EXT_ORIGIN


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.CFG['db'] = str(Path(self.tmp.name) / 'leads.sqlite')
        db.init(server.CFG['db']).close()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        server.CFG['port'] = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.conn = db.connect(server.CFG['db'])

    def tearDown(self):
        self.conn.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def call(self, path, body=None, origin=None, host=None):
        port = server.CFG['port']
        req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', method='POST' if body is not None else 'GET',
                                     data=json.dumps(body).encode() if body is not None else None)
        if origin is None and path.startswith('/api/ext/'):
            origin = EXT
        if origin:
            req.add_header('Origin', origin)
        req.add_header('Host', host or f'127.0.0.1:{port}')
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def page(self, job, users, cursor=None, done=False):
        return self.call('/api/ext/list-page', {'job_id': job['id'], 'seed': job['seed'], 'ig_id': '99', 'direction': job['direction'],
                                                'users': users, 'next_cursor': cursor, 'done': done, 'total': 3})

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
        self.assertEqual(self.page(job, users, cursor='c1')[1], {'ok': True, 'received': 2})
        again = self.call('/api/ext/next')[1]['job']
        self.assertEqual((again['id'], again['cursor'], again['ig_id']), (job['id'], 'c1', '99'))
        self.page(job, [{'ig_id': '4', 'handle': 'dan'}], cursor='c2')
        self.assertEqual(self.page(job, users, cursor='c1')[1], {'ok': True, 'received': 3, 'duplicate': True})  # outbox retry
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
        self.assertEqual(job, {'id': job['id'], 'kind': 'profile', 'handle': 'dave', 'ig_id': '7'})
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
        self.assertEqual((nxt['job']['kind'], nxt['budget'], nxt['paused']), ('list', {'list': 2000, 'profile': 150}, False))
        self.assertIsNone(self.call('/api/ext/next?kinds=list')[1]['job'])
        self.call('/api/scraper/pause', {'paused': True})
        self.assertEqual(self.call('/api/ext/next')[1], {'ok': True, 'paused': True, 'budget': {'list': 2000, 'profile': 150},
                                                         'job': None, 'cooldown_until': None})
        self.call('/api/scraper/pause', {'paused': False})
        self.call('/api/scraper/budget', {'list': 9999, 'profile': 200})
        hb = self.call('/api/ext/heartbeat', {'version': '1', 'state': 'idle', 'today': {'list': 1, 'profile': 2}})[1]
        self.assertEqual(hb, {'ok': True, 'paused': False, 'budget': {'list': 3000, 'profile': 200}})
        self.assertTrue(self.call('/api/scraper')[1]['ext']['online'])
        self.call('/api/ext/error', {'job_id': None, 'code': 'challenge', 'retry_at': None, 'message': 'checkpoint'})
        self.assertTrue(self.call('/api/ext/next')[1]['paused'])

    def test_planner(self):
        for i, seeds in enumerate([['s1'], ['s1', 's2'], []]):
            pid = db.upsert_person(self.conn, {'handle': f'p{i}'})
            for s in seeds:
                db.add_edge(self.conn, s, pid, 'followers')
        db.upsert_person(self.conn, {'handle': 'locked', 'is_private': True})
        db.set_setting(self.conn, 'budget', {'list': 10, 'profile': 2})
        self.conn.commit()
        server.qualify_batch(self.conn)
        self.assertEqual(server.plan_profiles(self.conn), 0)  # off until qualification is switched on
        db.set_setting(self.conn, 'qualify', True)
        self.assertEqual(server.plan_profiles(self.conn), 2)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT handle FROM jobs ORDER BY priority DESC")], ['p1', 'p0'])
        self.assertEqual(server.plan_profiles(self.conn), 0)

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
        self.assertEqual((counts['hot'], counts['unread'], counts['with_bio'], counts['total']), (2, 1, 3, 4))
        m = self.call('/api/map?scope=all')[1]
        self.assertNotEqual(m['rev'], rev)
        self.assertEqual({n['id'] for n in m['nodes'] if n['kind'] == 'seed'}, {'s:s1', 's:s2'})
        self.assertIn({'source': 's:s1', 'target': f"p:{ids['ben']}", 'direction': 'followers'}, m['links'])
        self.assertEqual(set(m['nodes'][0]), {'id', 'kind', 'label', 'tier', 'score', 'pic', 'degree'})
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
        self.assertIn({'tag': 'via @s1', 'grp': 'source', 'count': 3, 'source': 'auto'}, tags)
        self.assertIn({'tag': 'via @s1', 'grp': 'source', 'count': 1, 'source': 'manual'}, tags)

    def test_heartbeat_rate_and_soak(self):
        self.call('/api/ext/heartbeat', {'version': '2', 'state': 'running', 'rate': {'pages_hour': 300, 'people_hour': 7400.55,
                                                                                    'last_hit_at': '2026-09-24T10:00:00Z', 'x': 1}})
        s = self.call('/api/scraper')[1]
        self.assertEqual(s['ext']['rate'], {'pages_hour': 300.0, 'people_hour': 7400.6, 'last_hit_at': '2026-09-24T10:00:00.000000+00:00'})
        self.call('/api/ext/heartbeat', {'version': '2', 'rate': {'pages_hour': 'fast', 'last_hit_at': 'nope'}})
        self.assertEqual(self.call('/api/scraper')[1]['ext']['rate'], {'pages_hour': None, 'people_hour': None, 'last_hit_at': None})
        self.call('/api/scraper/seeds', {'handles': ['s'], 'directions': ['followers']})
        job = self.call('/api/ext/next')[1]['job']
        self.page(job, [{'ig_id': '1', 'handle': 'a'}, {'ig_id': '2', 'handle': 'b'}], cursor='c1')
        self.page(job, [{'ig_id': '1', 'handle': 'a'}, {'ig_id': '2', 'handle': 'b'}], cursor='c1')  # retry
        soak = self.call('/api/scraper')[1]['soak']
        self.assertEqual(soak['1h'], {'pages': 1, 'people': 2, 'new_people': 2, 'profiles': 0})
        self.assertEqual(soak['6h']['pages'], 1)

    def test_qualify_toggle_and_auto(self):
        s = self.call('/api/scraper')[1]
        self.assertEqual((s['qualify'], s['qualify_auto']), (False, True))
        self.assertEqual(self.call('/api/settings/qualify', {'on': 'yes'})[0], 400)
        self.assertEqual(self.call('/api/settings/qualify', {'on': True})[1], {'ok': True, 'qualify': True})
        self.assertTrue(self.call('/api/scraper')[1]['qualify'])
        self.call('/api/settings/qualify', {'on': False})
        # auto: stays off while a list is queued, flips on when all are done
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
        self.assertEqual(self.conn.execute("SELECT state FROM lists WHERE seed='s'").fetchone()[0], 'done')

    def test_budget_defaults_not_mutated(self):
        self.call('/api/scraper/budget', {'profile': 7})
        self.assertEqual(db.DEFAULTS['budget'], {'list': 2000, 'profile': 150})

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


if __name__ == '__main__':
    unittest.main()
