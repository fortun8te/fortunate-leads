import os; os.environ.setdefault('FL_NO_ORSLOT', '1')  # tests never see the real key pool
"""Regression tests for bugs found in the adversarial review (each failed before its fix)."""
import socket

from test_server import Base, db, server


class BugTest(Base):
    def raw(self, request, timeout=3):
        s = socket.create_connection(('127.0.0.1', server.CFG['port']), timeout=timeout)
        try:
            s.sendall(request.encode())
            return s.recv(4096).decode(errors='replace')
        finally:
            s.close()

    def test_negative_content_length_does_not_hang(self):
        port = server.CFG['port']
        out = self.raw(f'POST /api/scraper/pause HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: http://127.0.0.1:{port}\r\n'
                       'Content-Length: -1\r\n\r\n')
        self.assertIn(' 400 ', out.splitlines()[0])

    def test_non_object_json_body_is_400(self):
        for body in ([], 'x', 3):
            self.assertEqual(self.call('/api/ext/profile', body)[0], 400)
            self.assertEqual(self.call('/api/scraper/pause', body)[0], 400)

    def test_bad_heartbeat_cooldown_does_not_break_scraper(self):
        self.call('/api/ext/heartbeat', {'version': '1', 'state': 'cooldown', 'cooldown_until': 'soon', 'today': {}})
        self.assertEqual(self.call('/api/scraper')[0], 200)
        self.call('/api/ext/heartbeat', {'version': '1', 'state': 'cooldown', 'cooldown_until': 12345})
        self.assertEqual(self.call('/api/scraper')[0], 200)

    def test_bad_retry_at_still_releases_job(self):
        self.call('/api/scraper/seeds', {'handles': ['a'], 'directions': ['followers']})
        job = self.call('/api/ext/next')[1]['job']
        self.assertEqual(self.call('/api/ext/error', {'job_id': job['id'], 'code': 'rate_limit', 'retry_at': 'garbage'})[0], 200)
        self.assertEqual(self.conn.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'queued')
        self.assertIsNotNone(self.call('/api/scraper')[1]['accounts'][0]['cool']['list'])

    def test_search_underscore_and_percent_are_literal(self):
        for h in ('a_b', 'axb', 'cxd'):
            db.upsert_person(self.conn, {'handle': h})
        db.upsert_person(self.conn, {'handle': 'literal_percent', 'name': 'c%d'})
        self.conn.commit()
        rows = lambda q: sorted(r['handle'] for r in self.call('/api/leads?q=' + q)[1]['rows'])  # noqa: E731
        self.assertEqual(rows('a_b'), ['a_b'])
        self.assertEqual(rows('c%25d'), ['literal_percent'])

    def test_planner_skips_parked_handles(self):
        db.upsert_person(self.conn, {'handle': 'shop', 'ig_id': '1'})
        db.upsert_person(self.conn, {'handle': 'shop', 'ig_id': '2'})  # new account took the handle: old row parked
        self.conn.commit()
        server.qualify_batch(self.conn)
        db.set_setting(self.conn, 'qualify', True)
        server.plan_profiles(self.conn)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT handle FROM jobs WHERE kind='profile'")], ['shop'])
        parked = self.conn.execute("SELECT id FROM people WHERE handle LIKE 'shop~%'").fetchone()[0]
        self.assertEqual(self.call(f'/api/person/{parked}/read', {})[0], 400)

    def test_late_page_after_done_keeps_list_done(self):
        self.call('/api/scraper/seeds', {'handles': ['brand'], 'directions': ['followers']})
        job = self.call('/api/ext/next')[1]['job']
        self.page(job, [{'ig_id': '1', 'handle': 'x'}], done=True)
        self.page(job, [{'ig_id': '2', 'handle': 'y'}], cursor='late')
        self.assertEqual(self.conn.execute("SELECT state, cursor FROM lists WHERE seed='brand'").fetchone()[:], ('partial', None))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM edges WHERE seed='brand'").fetchone()[0], 1)  # stale unleased rows do not alter observed membership

    def test_unknown_person_is_404(self):
        self.assertEqual(self.call('/api/person/999')[0], 404)
        self.assertEqual(self.call('/api/person/999/mark', {'status': 'good'})[0], 404)

    def test_huge_budget_is_400_not_500(self):
        self.assertEqual(self.call('/api/scraper/budget', {'list': 1e400})[0], 400)

    def test_non_string_profile_fields_do_not_500(self):
        r = self.call('/api/ext/profile', {'job_id': None, 'profile': {'handle': 'z', 'bio': {'x': 1}, 'name': ['n'], 'ig_id': 5}})
        self.assertEqual(r[0], 200)
        r = self.call('/api/ext/list-page', {'job_id': None, 'seed': 's', 'direction': 'followers', 'next_cursor': None,
                                             'users': [{'handle': 'u', 'name': {'a': 1}}, 'junk', None], 'done': False})
        self.assertEqual(r[0], 200)


class StatusPipelineTest(Base):
    def test_old_statuses_migrate(self):
        ids = {h: db.upsert_person(self.conn, {'handle': h}) for h in ('g', 'm', 'k', 'c', 'n')}
        for h, st, note in (('g', 'good', None), ('m', 'maybe', None), ('k', 'known', 'school friend'), ('c', 'client', None), ('n', 'no', None)):
            self.conn.execute('INSERT INTO marks VALUES(?,?,?,?)', (ids[h], st, note, db.now()))
        db.migrate_statuses(self.conn)
        marks = dict(self.conn.execute('SELECT person_id, status FROM marks').fetchall())
        self.assertEqual(marks, {ids['g']: 'interested', ids['k']: None, ids['c']: 'client', ids['n']: 'no'})  # m: no status, no note -> gone
        tags = {tuple(r) for r in self.conn.execute("SELECT person_id, tag FROM tags WHERE source='manual'")}
        self.assertEqual(tags, {(ids['m'], 'Maybe'), (ids['k'], 'Already know them')})
        db.migrate_statuses(self.conn)  # idempotent
        self.assertEqual(dict(self.conn.execute('SELECT person_id, status FROM marks').fetchall()), marks)

    def test_new_statuses_validate_and_note_is_searchable(self):
        pid = db.upsert_person(self.conn, {'handle': 'ann'})
        self.conn.commit()
        self.assertEqual(self.call(f'/api/person/{pid}/mark', {'status': 'maybe'})[0], 400)
        self.assertEqual(self.call(f'/api/person/{pid}/mark', {'status': 'talking', 'note': 'Met at Shopify meetup'})[0], 200)
        self.assertEqual([r['handle'] for r in self.call('/api/leads?q=shopify%20meetup')[1]['rows']], ['ann'])
        self.assertEqual(self.call('/api/counts')[1]['talking'], 1)

    def test_status_and_note_feed_the_qualifier(self):
        import importlib.util, pathlib   # the real module: test_server stubs 'qualify' for the server
        spec = importlib.util.spec_from_file_location('qualify_real', pathlib.Path(server.__file__).with_name('qualify.py'))
        qualify = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(qualify)
        pid = db.upsert_person(self.conn, {'handle': 'bea', 'bio': 'Founder of a candle brand'})
        self.conn.commit()
        server.qualify_batch(self.conn)
        p = lambda: server.with_owner(self.conn, dict(self.conn.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()))  # noqa: E731
        before = qualify.input_hash(p(), [])
        self.assertEqual(before, qualify.input_hash({k: v for k, v in p().items() if k not in ('status', 'note', 'manual_tags')}, []))
        stamp = lambda: self.conn.execute('SELECT updated_at FROM verdicts WHERE person_id=?', (pid,)).fetchone()[0]  # noqa: E731
        old = stamp()
        self.call(f'/api/person/{pid}/mark', {'status': 'no', 'note': 'Sells wholesale only'})
        self.assertGreater(server.qualify_batch(self.conn), 0)   # re-qualified after the mark, not a one-time pass
        self.assertNotEqual(stamp(), old)
        after = p()
        self.assertNotEqual(qualify.input_hash(after, []), before)
        packet = qualify._packet(after, [], [])
        self.assertIn("OWNER'S OWN JUDGEMENT", packet)
        self.assertIn('Not a fit', packet)
        self.assertNotIn('Sells wholesale only', packet)
        self.call(f'/api/person/{pid}/tags', {'add': ['Wholesale']})
        self.assertIn('Wholesale', qualify._packet(p(), [], []))
