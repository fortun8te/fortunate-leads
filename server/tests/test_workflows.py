"""Workflow regression tests use isolated databases; one integration test covers CSV HTTP routing."""
import csv
import io
import json
import os
import sys
import tempfile
import threading
import urllib.error
from http.server import ThreadingHTTPServer
import unittest
import urllib.request
from pathlib import Path
os.environ.setdefault('FL_NO_ORSLOT', '1')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
import server
import workflows


class Workflows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'test.sqlite')
        self.conn = db.init(self.path)
        self.pid = db.upsert_person(self.conn, {'handle': 'alice', 'ig_id': '1'})
        self.conn.commit()
        server.clear_caches()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def schedule(self, pid=None, **body):
        return workflows.api_follow_up(self.conn, {}, body or {'due_on': '2026-09-26'}, pid or self.pid)

    def leads(self, **q):
        return server.api_leads(self.conn, {k: [str(v)] for k, v in q.items()}, {})

    def test_lifecycle_noops_restart_and_history(self):
        self.schedule(due_on='2026-09-26', note='Send portfolio')
        self.schedule(due_on='2026-09-26', note='Send portfolio')
        self.assertEqual(len(workflows.history(self.conn, self.pid)['rows']), 1)
        self.schedule(action='complete')
        self.schedule(action='complete')
        self.assertIsNotNone(workflows.follow_up(self.conn, self.pid)['completed_at'])
        self.conn.close()
        self.conn = db.init(self.path)
        self.assertEqual(len(workflows.history(self.conn, self.pid)['rows']), 2)
        self.schedule(action='clear')
        self.schedule(action='clear')
        detail = server.api_person(self.conn, {}, {}, self.pid)
        self.assertIsNone(detail['follow_up'])
        self.assertEqual([r['kind'] for r in detail['activity']['rows']], ['follow_up_cleared', 'follow_up_completed', 'follow_up_scheduled'])

    def test_calendar_boundaries_shared_filters_and_cache(self):
        p2 = db.upsert_person(self.conn, {'handle': 'bob'})
        p3 = db.upsert_person(self.conn, {'handle': 'chris'})
        self.conn.execute("INSERT INTO tags VALUES(?, 'test', 'signal', 'manual')", (self.pid,))
        for pid in (self.pid, p2, p3):
            db.add_edge(self.conn, 'source', pid, 'followers')
        self.conn.commit()
        self.schedule(due_on='2026-09-26')
        self.schedule(p2, due_on='2026-09-25')
        self.schedule(p3, due_on='2026-09-27')
        self.assertEqual(self.leads(follow_up='due', today='2026-09-26')['total'], 2)
        self.assertEqual(self.leads(follow_up='overdue', today='2026-09-26')['total'], 1)
        self.assertEqual(self.leads(follow_up='due', today='2026-09-25')['total'], 1)
        rows = self.leads(sort='follow_up')['rows']
        self.assertEqual([r['id'] for r in rows], [p2, self.pid, p3])
        query = {'follow_up': ['due'], 'today': ['2026-09-26']}
        self.assertEqual(server.api_counts(self.conn, query, {})['open'], 2)
        graph = server.api_map(self.conn, query, {})
        self.assertEqual({int(n['id'][2:]) for n in graph['nodes'] if n['kind'] == 'lead'}, {self.pid, p2})
        exported = self.parse_export({'query': 'follow_up=due&today=2026-09-26'})
        self.assertEqual({int(r['id']) for r in exported}, {self.pid, p2})
        before = server.data_rev(self.conn)
        facets = server.api_tags(self.conn, query, {})
        self.schedule(action='complete')
        self.assertNotEqual(server.data_rev(self.conn), before)
        self.assertNotEqual(server.api_tags(self.conn, query, {}), facets)
        self.assertEqual(self.leads(follow_up='completed')['total'], 1)
        self.assertEqual(self.leads(follow_up='scheduled')['total'], 2)
        self.assertEqual(self.leads(follow_up='none')['total'], 0)

    def test_invalid_inputs(self):
        for body in ({'due_on': '2026-02-30'}, {'due_on': '2026-9-26'}, {'due_on': 1},
                     {'due_on': '2026-09-26', 'note': 'x' * 501}, {'action': 'delete'},
                     {'action': 'clear', 'due_on': '2026-09-26'}):
            with self.assertRaises(ValueError):
                self.schedule(**body)
            self.conn.rollback()
        for q in ({'follow_up': 'bad'}, {'follow_up': 'due', 'today': 'oops'}):
            with self.assertRaises(ValueError):
                self.leads(**q)
        for body in ({'kind': 'email', 'body': 'x'}, {'kind': 'dm', 'body': ''},
                     {'kind': 'dm', 'body': 'x', 'happened_at': '2026-09-26T12:00:00'}):
            with self.assertRaises(ValueError):
                workflows.api_interaction(self.conn, {}, body, self.pid)
        with self.assertRaises(server.NotFound):
            workflows.api_history(self.conn, {}, {}, 99999)
        for q in ({'cursor': ['garbage']}, {'limit': ['0']}, {'limit': ['101']}):
            with self.assertRaises(ValueError):
                workflows.history(self.conn, self.pid, q)

    def test_activity_changes_bulk_and_stable_pagination(self):
        server.api_mark(self.conn, {}, {'status': 'contacted', 'note': 'First'}, self.pid)
        server.api_mark(self.conn, {}, {'status': 'contacted', 'note': 'First'}, self.pid)
        self.assertEqual(len(workflows.history(self.conn, self.pid)['rows']), 2)
        server.api_bulk(self.conn, {}, {'ids': [self.pid], 'status': 'talking'})
        for i in range(5):
            workflows.api_interaction(self.conn, {}, {'kind': 'dm', 'body': str(i), 'happened_at': '2026-01-01T15:00:00+02:00'}, self.pid)
        seen, cursor = [], None
        while True:
            q = {'limit': ['2']}
            if cursor:
                q['cursor'] = [cursor]
            page = workflows.history(self.conn, self.pid, q)
            seen += page['rows']
            cursor = page['next_cursor']
            if not cursor:
                break
        self.assertEqual(len(seen), 8)
        self.assertEqual(len({r['id'] for r in seen}), 8)
        self.assertEqual(seen[-1]['happened_at'], '2026-01-01T13:00:00.000000+00:00')
        event = next(r for r in seen if r['kind'] == 'status' and r['after_value'] == 'talking')
        self.assertEqual(event['before_value'], 'contacted')

    def parse_export(self, body):
        out = workflows.api_export(self.conn, {}, body)
        return list(csv.DictReader(io.StringIO(out.data.decode('utf-8-sig'))))

    def test_export_all_pages_exact_selection_and_formula_escaping(self):
        for i in range(601):
            db.upsert_person(self.conn, {'handle': 'test' + str(i), 'name': '=HYPERLINK("x")' if i == 0 else 'Normal, "quoted"\nline'})
        self.conn.commit()
        server.api_mark(self.conn, {}, {'status': 'no', 'note': '\x01  +danger'}, self.pid)
        rows = self.parse_export({'query': 'q=test&limit=2&offset=100&sort=recent'})
        self.assertEqual(len(rows), 601)
        self.assertTrue(next(r for r in rows if r['handle'] == 'test0')['name'].startswith("'="))
        self.assertEqual(next(r for r in rows if r['handle'] == 'test1')['name'], 'Normal, "quoted"\nline')
        self.assertEqual(len(self.parse_export({'query': ''})), 601)
        selected = self.parse_export({'ids': [self.pid, self.pid]})
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['handle'], 'alice')
        self.assertTrue(selected[0]['note'].startswith("'"))
        for body in ({}, {'ids': []}, {'ids': [True]}, {'ids': [999999]}, {'query': '', 'ids': [self.pid]}, {'query': 'follow_up=bogus'}):
            with self.assertRaises(ValueError):
                self.parse_export(body)
        for value in ('=1', '+1', '-1', '@SUM(1)', '\t=1', '  =1', '\ufeff=1', '\x00=1'):
            self.assertTrue(workflows.safe_cell(value).startswith("'"))
        self.assertEqual(workflows.safe_cell(-1), -1)

    def test_profile_refresh_uses_existing_queue_and_freshness(self):
        self.conn.execute("UPDATE people SET bio='Already read',bio_at='2026-01-01',bio_src='tab' WHERE id=?", (self.pid,))
        self.conn.commit()
        server.api_read(self.conn, {}, {}, self.pid)
        server.api_read(self.conn, {}, {}, self.pid)
        detail = server.api_person(self.conn, {}, {}, self.pid)
        self.assertTrue(detail['profile_read_pending'])
        self.assertEqual(detail['bio_at'], '2026-01-01')
        self.assertEqual(detail['bio_src'], 'tab')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)

    def test_merge_keeps_history_both_notes_and_earliest_open_reminder(self):
        other = db.upsert_person(self.conn, {'handle': 'other'})
        self.conn.commit()
        self.schedule(due_on='2026-09-30', note='Later')
        self.schedule(other, due_on='2026-09-25', note='Urgent')
        server.api_mark(self.conn, {}, {'note': 'A', 'status': 'contacted'}, self.pid)
        server.api_mark(self.conn, {}, {'note': 'B', 'status': 'talking'}, other)
        db.merge_people(self.conn, self.pid, other)
        self.conn.commit()
        self.assertEqual(workflows.follow_up(self.conn, self.pid)['note'], 'Urgent')
        self.assertIsNone(workflows.follow_up(self.conn, other))
        detail = server.api_person(self.conn, {}, {}, self.pid)
        self.assertIn('A', detail['note'])
        self.assertIn('B', detail['note'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM activity WHERE person_id=?', (other,)).fetchone()[0], 0)
        self.assertIn('follow_up_merged', [r['kind'] for r in detail['activity']['rows']])
        self.assertEqual(len(detail['activity']['rows']), 8)

    def test_merge_large_notes_remains_editable_and_preserves_dropped_text(self):
        other = db.upsert_person(self.conn, {'handle': 'other'})
        self.conn.commit()
        server.api_mark(self.conn, {}, {'note': 'A' * 4000}, self.pid)
        server.api_mark(self.conn, {}, {'note': 'B' * 4000}, other)
        db.merge_people(self.conn, self.pid, other)
        self.conn.commit()
        detail = server.api_person(self.conn, {}, {}, self.pid)
        self.assertLessEqual(len(detail['note']), 5000)
        merged = next(r for r in detail['activity']['rows'] if r['kind'] == 'identity_merged')
        self.assertEqual(merged['before_value']['note'], 'B' * 4000)
        server.api_mark(self.conn, {}, {'note': detail['note'] + '!'}, self.pid)

    def test_http_routes_csv_headers_origin_and_errors(self):
        saved = dict(server.CFG)
        server.CFG['db'] = self.path
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        port = httpd.server_address[1]
        server.CFG['port'] = port
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        def request(path, body=None, origin=True):
            req = urllib.request.Request(f'http://127.0.0.1:{port}' + path,
                data=json.dumps(body).encode() if body is not None else None)
            if body is not None and origin:
                req.add_header('Origin', f'http://127.0.0.1:{port}')
            try:
                response = urllib.request.urlopen(req)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                return response.status, response.headers, response.read()
        try:
            code, headers, body = request('/api/leads/export', {'ids': [self.pid]})
            self.assertEqual(code, 200)
            self.assertEqual(headers['Content-Type'], 'text/csv; charset=utf-8')
            self.assertIn('attachment', headers['Content-Disposition'])
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertIn(b'alice', body)
            self.assertEqual(request('/api/leads/export', {'query': ''}, origin=False)[0], 403)
            self.assertEqual(request('/api/leads/export', {'ids': []})[0], 400)
            self.assertEqual(request(f'/api/person/{self.pid}/follow-up', {'due_on': '2026-09-26'})[0], 200)
            self.assertEqual(request('/api/person/999999/follow-up', {'due_on': '2026-09-26'})[0], 404)
            self.assertEqual(request(f'/api/person/{self.pid}/activity', {'kind': 'call', 'body': 'Spoke today'})[0], 200)
            code, _, body = request(f'/api/person/{self.pid}/activity?limit=1')
            self.assertEqual(code, 200)
            self.assertIsNotNone(json.loads(body)['next_cursor'])
        finally:
            httpd.shutdown()
            httpd.server_close()
            server.CFG.update(saved)

    def test_old_database_migration_preserves_marks(self):
        self.conn.execute('INSERT INTO marks VALUES(?,?,?,?)', (self.pid, 'talking', 'Legacy note', db.now()))
        self.conn.execute('DROP TABLE activity')
        self.conn.execute('DROP TABLE followups')
        self.conn.commit()
        self.conn.close()
        self.conn = db.init(self.path)
        self.assertEqual(server.api_person(self.conn, {}, {}, self.pid)['note'], 'Legacy note')
        self.assertEqual(workflows.history(self.conn, self.pid)['rows'], [])


if __name__ == '__main__':
    unittest.main()
