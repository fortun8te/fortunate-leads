"""Multi-account lanes: leasing, roles, handoff, main-account protection, migration, account endpoints."""
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server import Base  # noqa: E402  (installs the qualify stub first)

import accounts  # noqa: E402
import db  # noqa: E402

ACCT = {'a': ('lane-a', '101', 'acct.a'), 'b': ('lane-b', '102', 'acct.b'), 'c': ('lane-c', '103', 'acct.c')}


class LaneTest(Base):
    def nxt(self, who, kinds=None):
        lane, ig, handle = ACCT[who]
        q = f'/api/ext/next?lane={lane}&ig_id={ig}&handle={handle}' + (f'&kinds={kinds}' if kinds else '')
        return self.call(q)[1]

    def post(self, who, path, body):
        lane, ig, handle = ACCT[who]
        return self.call(path, dict(body, lane_id=lane, account={'ig_id': ig, 'handle': handle}))

    def page(self, who, job, n, cursor, done=False):
        users = [{'ig_id': f'{job["seed"]}{cursor}{i}'.replace('.', ''), 'handle': f'{job["seed"]}_{cursor}_{i}'} for i in range(n)]
        users = [dict(u, ig_id=str(abs(hash(u['ig_id'])) % 10**12)) for u in users]
        return self.post(who, '/api/ext/list-page', {'job_id': job['id'], 'seed': job['seed'], 'direction': job['direction'],
                                                     'users': users, 'next_cursor': cursor, 'done': done})

    def seeds(self, *handles, direction='followers'):
        self.call('/api/scraper/seeds', {'handles': list(handles), 'directions': [direction]})

    def age(self, lane, minutes):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec='microseconds')
        self.conn.execute('UPDATE accounts SET last_seen=? WHERE lane_id=?', (ts, lane))
        self.conn.commit()

    def test_exclusive_and_sticky(self):
        self.seeds('s1', 's2')
        ja, jb = self.nxt('a')['job'], self.nxt('b')['job']
        self.assertEqual({ja['seed'], jb['seed']}, {'s1', 's2'})   # two lanes, two different lists
        self.assertIsNone(self.nxt('c')['job'])                    # nothing is handed out twice
        self.assertEqual(self.page('a', ja, 5, 'c1')[1]['received'], 5)
        self.assertIsNone(self.nxt('c', 'list')['job'])            # a's list waits for a, not for c
        again = self.nxt('a')['job']
        self.assertEqual((again['id'], again['cursor'], again['received']), (ja['id'], 'c1', 5))
        rows = {r['lane_id']: r for r in self.conn.execute('SELECT * FROM accounts')}
        self.assertEqual((rows['lane-a']['ig_id'], rows['lane-a']['handle']), ('101', 'acct.a'))
        lanes = dict(self.conn.execute('SELECT seed, lane FROM lists').fetchall())
        self.assertEqual(lanes, {ja['seed']: 'lane-a', jb['seed']: 'lane-b'})
        self.assertEqual(self.conn.execute("SELECT lane, users FROM pages").fetchall()[0][:], ('lane-a', 5))

    def test_roles_and_pause(self):
        pid = db.upsert_person(self.conn, {'ig_id': '7', 'handle': 'dave'})
        self.conn.commit()
        self.call(f'/api/person/{pid}/read', {})
        self.seeds('s1')
        self.nxt('a')   # a takes the list
        self.post('b', '/api/ext/heartbeat', {'version': '3.4.0', 'state': 'idle'})   # b checks in
        self.call('/api/scraper/pause', {'paused': False})
        self.assertEqual(self.call('/api/accounts/lane-b', {'role': 'lists'})[1]['account']['role'], 'lists')
        self.assertIsNone(self.nxt('b', 'profile')['job'])          # a lists-only lane never reads bios
        self.call('/api/accounts/lane-b', {'role': 'bios'})
        self.assertEqual(self.nxt('b')['job']['kind'], 'profile')   # a bios lane gets the bio, not a list
        self.assertEqual(self.call('/api/accounts/lane-c', {'role': 'x'})[0], 404)
        self.nxt('c')
        self.assertEqual(self.call('/api/accounts/lane-c', {'role': 'x'})[0], 400)
        self.assertEqual(self.call('/api/accounts/lane-c', {'paused': 'yes'})[0], 400)
        r = self.call('/api/accounts/lane-c', {'paused': True, 'budget': {'list': 99999, 'profile': 20}, 'label': ' spare '})[1]['account']
        self.assertEqual((r['paused'], r['budget'], r['budget_custom'], r['label'], r['status']),
                         (True, {'list': 3000, 'profile': 20}, True, 'spare', 'paused'))
        self.assertEqual(self.nxt('c'), {'ok': True, 'paused': True, 'budget': {'list': 3000, 'profile': 20}, 'job': None,
                                         'cooldown_until': None})
        hb = self.post('c', '/api/ext/heartbeat', {'version': '3.4.0', 'state': 'paused'})[1]
        self.assertEqual(hb, {'ok': True, 'paused': True, 'budget': {'list': 3000, 'profile': 20}})
        self.call('/api/accounts/lane-c', {'budget': None})
        self.assertEqual(self.nxt('a')['budget'], {'list': 2000, 'profile': 0})

    def test_handoff_on_login_keeps_cursor(self):
        self.seeds('s1')
        ja = self.nxt('a')['job']
        self.nxt('b')
        self.page('a', ja, 4, 'c1')
        self.nxt('a')   # a resumes its list...
        self.post('a', '/api/ext/error', {'job_id': ja['id'], 'code': 'login', 'retry_at': None, 'message': 'login_required'})
        jb = self.nxt('b')['job']     # ...and b takes over from the saved cursor right away
        self.assertEqual((jb['id'], jb['cursor'], jb['received']), (ja['id'], 'c1', 4))
        self.page('b', jb, 3, 'c2')
        self.assertEqual(self.conn.execute("SELECT received, lane FROM lists WHERE seed='s1'").fetchone()[:], (7, 'lane-b'))
        body = self.call('/api/accounts')[1]
        a = next(x for x in body['accounts'] if x['lane_id'] == 'lane-a')
        self.assertEqual((a['status'], a['hold']), ('needs_login', 'login'))
        self.assertIn('@acct.a logged out — open its Chrome profile and log in; its list moved to @acct.b',
                      [x['text'] for x in body['alerts']])
        self.assertEqual(db.get_setting(self.conn, 'handoffs')[-1]['why'], 'login')
        self.nxt('a')   # logged back in: asks for work again, the hold clears, the list stays with b
        self.assertIsNone(self.conn.execute("SELECT hold FROM accounts WHERE lane_id='lane-a'").fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT lane FROM lists WHERE seed='s1'").fetchone()[0], 'lane-b')

    def test_handoff_when_offline(self):
        self.seeds('s1')
        ja = self.nxt('a')['job']
        self.page('a', ja, 2, 'c1')
        self.age('lane-a', 5)
        self.assertIsNone(self.nxt('b')['job'])       # 5 min quiet: still a's
        self.age('lane-a', 11)
        jb = self.nxt('b')['job']                     # > 10 min: b resumes it
        self.assertEqual((jb['id'], jb['cursor']), (ja['id'], 'c1'))
        texts = [x['text'] for x in self.call('/api/accounts')[1]['alerts']]
        self.assertTrue(any(t.startswith('@acct.a offline for 11m — open its Chrome profile; its list moved to @acct.b') for t in texts), texts)

    def test_list_cooldown_is_per_lane(self):
        self.seeds('s1', 's2')
        ja, jb = self.nxt('a')['job'], self.nxt('b')['job']
        self.page('a', ja, 2, 'c1')
        self.post('a', '/api/ext/error', {'job_id': ja['id'], 'code': 'rate_limit', 'retry_at': '2099-01-01T00:00:00Z', 'message': '429'})
        s = self.call('/api/scraper')[1]
        by = {a['lane_id']: a for a in s['accounts']}
        self.assertEqual(by['lane-a']['status'], 'cooldown')
        self.assertEqual(by['lane-b']['status'], 'online')
        self.assertIsNone(s['ext']['cooldown_until'])   # one lane cooling is not the whole scraper cooling
        self.page('b', jb, 2, None, done=True)
        jb2 = self.nxt('b')['job']                     # b finished its own list and picks up a's where a stopped
        self.assertEqual((jb2['seed'], jb2['cursor']), (ja['seed'], 'c1'))

    def test_main_account_protected(self):
        self.conn.execute("INSERT INTO seeds(handle, is_me) VALUES('acct.a', 1)")
        self.conn.commit()
        pid = db.upsert_person(self.conn, {'ig_id': '7', 'handle': 'dave'})
        self.conn.commit()
        self.call(f'/api/person/{pid}/read', {})
        self.seeds('s1')
        got = self.nxt('a')['job']                     # Michael's own account: auto main, bios only
        self.assertEqual(got['kind'], 'profile')
        self.assertTrue(self.call('/api/accounts')[1]['accounts'][0]['is_main'])
        self.assertIsNone(self.nxt('a', 'list')['job'])
        self.assertIn('No online account takes lists — set one to lists or both', [x['text'] for x in self.call('/api/accounts')[1]['alerts']])
        jb = self.nxt('b')['job']
        self.assertEqual(jb['kind'], 'list')
        self.call('/api/accounts/lane-b', {'is_main': True})   # b made main while it holds a list: the list is freed
        self.conn.execute("UPDATE jobs SET leased_until='2000-01-01' WHERE id=?", (jb['id'],))
        self.conn.commit()
        self.assertIsNone(self.nxt('b', 'list')['job'])
        db.set_setting(self.conn, 'main_list_share', 0.25)
        self.conn.commit()
        self.assertEqual(self.nxt('a', 'list')['job']['seed'], 's1')   # a low list share when nobody else is busy

    def test_default_lane_backwards_compatible(self):
        self.seeds('s1')
        job = self.call('/api/ext/next')[1]['job']
        self.call('/api/ext/heartbeat', {'version': '3.3.0', 'state': 'running', 'today': {'list': 3, 'profile': 0}})
        s = self.call('/api/scraper')[1]
        self.assertEqual([a['lane_id'] for a in s['accounts']], ['default'])
        self.assertEqual((s['ext']['version'], s['ext']['state'], s['ext']['today']), ('3.3.0', 'running', {'list': 3, 'profile': 0}))
        self.assertEqual(self.conn.execute('SELECT lane FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'default')

    def test_remove_releases(self):
        self.seeds('s1')
        ja = self.nxt('a')['job']
        self.assertEqual(self.call('/api/accounts/lane-a/remove', {})[1], {'ok': True, 'removed': 1})
        self.assertEqual(self.nxt('b')['job']['id'], ja['id'])
        self.assertEqual([a['lane_id'] for a in self.call('/api/accounts')[1]['accounts']], ['lane-b'])
        self.assertEqual(self.call('/api/accounts/nope/remove', {})[1]['removed'], 0)

    def test_setup(self):
        s = self.call('/api/setup')[1]
        self.assertTrue(s['extension_path'].endswith('extension'))
        self.assertTrue(Path(s['extension_path'], 'manifest.json').is_file())
        self.assertEqual(s['extension_id'], 'fgdbghllamedgihmdcolaggnbhnakjnf')

    def test_same_account_twice(self):
        self.nxt('a')
        ACCT['z'] = ('lane-z', '101', 'acct.a')
        try:
            self.nxt('z')
        finally:
            del ACCT['z']
        self.assertTrue(any('logged in on 2 Chrome profiles' in x['text'] for x in self.call('/api/accounts')[1]['alerts']))


class MigrationTest(unittest.TestCase):
    OLD = """
    CREATE TABLE people(id INTEGER PRIMARY KEY, ig_id TEXT UNIQUE, handle TEXT UNIQUE NOT NULL COLLATE NOCASE, name TEXT, pic_url TEXT,
      pic_file TEXT, is_private INT, is_verified INT, bio TEXT, website TEXT, category TEXT, followers INT, following INT, posts INT,
      is_business INT, bio_at TEXT, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE lists(seed TEXT COLLATE NOCASE, direction TEXT CHECK(direction IN('followers','following')), state TEXT,
      cursor TEXT, received INT DEFAULT 0, total INT, error TEXT, updated_at TEXT, PRIMARY KEY(seed,direction));
    CREATE TABLE jobs(id INTEGER PRIMARY KEY, kind TEXT CHECK(kind IN('list','profile')), seed TEXT, direction TEXT,
      handle TEXT, priority INT DEFAULT 0, state TEXT DEFAULT 'queued', attempts INT DEFAULT 0, leased_until TEXT, created_at TEXT);
    CREATE TABLE pages(job_id INT, cursor TEXT, PRIMARY KEY(job_id, cursor));
    CREATE TABLE tags(person_id INT, tag TEXT, grp TEXT, source TEXT CHECK(source IN('auto','manual')), PRIMARY KEY(person_id,tag));
    INSERT INTO lists VALUES('s1','followers','running','c9',40,100,NULL,'2026-09-20');
    INSERT INTO jobs(kind, seed, direction, state, leased_until) VALUES('list','s1','followers','leased','2000-01-01');
    INSERT INTO pages VALUES(1,'c9');
    """

    def test_old_db_gains_lanes(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / 'old.sqlite')
            c = sqlite3.connect(path)
            c.executescript(self.OLD)
            c.commit()
            c.close()
            for _ in range(2):   # idempotent
                conn = db.init(path)
                conn.close()
            conn = db.connect(path)
            cols = {t: {r[1] for r in conn.execute(f'PRAGMA table_info({t})')} for t in ('jobs', 'lists', 'pages', 'accounts')}
            self.assertTrue({'lane'} <= cols['jobs'])
            self.assertTrue({'lane', 'prev_lane', 'released_at', 'released_why'} <= cols['lists'])
            self.assertTrue({'at', 'lane', 'users'} <= cols['pages'])
            self.assertTrue({'lane_id', 'ig_id', 'handle', 'role', 'budget', 'paused', 'is_main', 'first_seen', 'last_seen', 'version',
                             'state', 'cooldown_until', 'rate', 'last_error'} <= cols['accounts'])
            self.assertEqual(conn.execute('SELECT cursor, received FROM lists').fetchone()[:], ('c9', 40))
            # an unowned expired lease from before lanes is simply leased to the first lane that asks, cursor intact
            conn.execute('BEGIN IMMEDIATE')
            accounts.touch(conn, 'default')
            job = accounts.pick_job(conn, 'default', ['list'], datetime.now(timezone.utc))
            conn.rollback()
            self.assertEqual((job['seed'], job['owner']), ('s1', None))
            conn.close()


if __name__ == '__main__':
    unittest.main()
