"""Benign untrusted-input checks. All records live in a temporary in-memory DB.

The UI check executes the actual connection script with a small DOM recorder,
then parses every emitted HTML fragment independently. It needs Node, but no
browser, installed JS packages, network requests, or running application.
"""
import json
import shutil
import sqlite3
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connections
import db

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = "<img src=x onerror=\"globalThis.security_probe=1\">&'\""


class HTMLInventory(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        self.text.append(data)


UI_RECORDER = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const writes = [], requests = [];
class Element {
  constructor(id) {
    this.id = id; this.value = ''; this.hidden = false; this.children = [];
    this.events = {}; this.attrs = {}; this.classList = { toggle() {} };
    this.parentElement = { contains() { return true; } };
  }
  set innerHTML(value) { this.html = value; writes.push(value); }
  get innerHTML() { return this.html || ''; }
  addEventListener(event, fn) { this.events[event] = fn; }
  setAttribute(key, value) { this.attrs[key] = value; }
  replaceChildren(...items) { this.children = items; }
  append(item) { this.children.push(item); }
  insertAdjacentHTML(position, value) { writes.push(value); }
  after() {}
  querySelector() { return this.children[0] || null; }
  querySelectorAll() { return this.children; }
  focus() { document.activeElement = this; }
}
const elements = new Map();
const get = id => {
  if (!elements.has(id)) elements.set(id, new Element(id));
  return elements.get(id);
};
global.document = {
  activeElement: null,
  querySelector: selector => get(selector.slice(1)),
  getElementById: get,
  createElement: tag => new Element(tag),
  addEventListener() {},
};
global.api = { get: async url => {
  requests.push(url);
  return url.startsWith('/api/connections/search?') ? input.suggestions : input.result;
} };
// Use the production escaping and numeric formatting helpers, not copies.
const app = fs.readFileSync(input.root + '/web/app.js', 'utf8');
for (const name of ['esc', 'int']) {
  const declaration = app.match(new RegExp('^const ' + name + ' = .+$', 'm'));
  if (!declaration) throw new Error('Missing production utility ' + name);
  vm.runInThisContext(declaration[0]);
}
vm.runInThisContext(fs.readFileSync(input.root + '/web/connections.js', 'utf8'));
(async () => {
  get('connection-from').value = input.from;
  get('connection-to').value = input.to;
  get('connection-mode').value = 'all';
  document.activeElement = get('connection-from');
  await get('connection-from').events.focus();
  await get('connection-form').events.submit({ preventDefault() {} });
  // Also render every route, exercising attribute/text sinks on each node.
  for (const button of get('connection-routes').children) await button.events.click();
  process.stdout.write(JSON.stringify({
    writes, requests, status: get('connection-status').textContent,
    routeCount: get('connection-routes').children.length,
    suggestionCount: get('connection-from-results').children.length,
    executed: Boolean(globalThis.security_probe),
  }));
})().catch(error => { process.stderr.write(error.stack); process.exitCode = 1; });
"""


class ConnectionUntrustedInputsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.addCleanup(self.conn.close)
        self.person('alpha', '1')
        self.person('omega', '2')
        self.seed('alpha', '1')
        self.seed('omega', '2')

    def person(self, handle, ig, name=None):
        return db.upsert_person(self.conn, {'handle': handle, 'ig_id': ig, 'name': name}, '2026-01-01')

    def seed(self, handle, ig):
        self.conn.execute('INSERT INTO seeds VALUES(?,?,0,?)', (handle, ig, '2026-01-01'))

    def edge(self, seed, source_id, person_id, member_id):
        db.add_edge(self.conn, seed, person_id, 'following', '2026-01-01',
                    seed_ig_id=source_id, person_ig_id=member_id)

    def test_sql_and_non_profile_selectors_cannot_change_query_meaning(self):
        before = tuple(self.conn.iterdump())
        invalid = [
            "alpha' OR 1=1--", "ig:1' OR 1=1--", 'person:1;DROP TABLE people',
            'legacy-person:1 OR 1=1', 'legacy:alpha%', 'seed:alpha%',
            'https://instagram.com.attacker.invalid/alpha',
            'https://instagram.com@attacker.invalid/alpha',
            'https://attacker.invalid/alpha', 'javascript:alpha',
            '//attacker.invalid/alpha', 'alpha/../omega', '<svg/onload=1>',
            'a' * 60000, 'ig:' + '9' * 60000, 'person:' + '9' * 60000,
            None, 1, True, [], {},
        ]
        for selector in invalid:
            with self.subTest(selector=repr(selector)[:90]):
                with self.assertRaises(ValueError):
                    connections.explore(self.conn, selector, 'omega')
        self.assertEqual(tuple(self.conn.iterdump()), before)
        for selector in ('alpha', '@ALPHA', 'https://www.instagram.com/ALPHA/?q=omega'):
            with self.subTest(valid_selector=selector):
                self.assertEqual(connections.explore(self.conn, selector, 'omega')['source']['id'], 'ig:1')

    def test_prefix_search_treats_underscores_as_literal_and_rejects_wildcards(self):
        handles = ['a_b', 'a_beyond', 'aXb', '_solo', 'other']
        for i, handle in enumerate(handles, 10):
            self.person(handle, str(i))
        for prefix in ('a_', '_', '@A_'):
            with self.subTest(prefix=prefix):
                expected = {handle.lower() for handle in handles if handle.lower().startswith(prefix.lstrip('@').lower())}
                actual = {a['handle'] for a in connections.search(self.conn, prefix)['accounts']}
                self.assertEqual(actual, expected)
        for query in ('%', 'a%', "a' OR 1=1--", '[a-z]', 'a\\_', 'a' * 60000):
            with self.subTest(query=query[:90]), self.assertRaises(ValueError):
                connections.search(self.conn, query)

    def test_invalid_result_limits_and_modes_cannot_disable_bounds(self):
        for limit in (0, -1, 51, 10**100, True, 1.0, '50', None):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    connections.search(self.conn, 'a', limit=limit)
                with self.assertRaises(ValueError):
                    connections.explore(self.conn, 'alpha', 'omega', limit=limit)
        for mode in ('all OR 1=1', 'following;DROP TABLE people', '', None):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                connections.explore(self.conn, 'alpha', 'omega', mode=mode)

    def test_high_degree_exploration_is_bounded_and_works_on_read_only_database(self):
        for i in range(1100):
            ig = str(10000 + i)
            pid = self.person('member_' + str(i), ig)
            self.edge('alpha', '1', pid, ig)
            self.edge('omega', '2', pid, ig)
        self.conn.commit()
        before = tuple(self.conn.iterdump())
        self.conn.execute('PRAGMA query_only=ON')
        result = connections.explore(self.conn, 'alpha', 'omega', limit=50)
        self.assertEqual(len(result['paths']), 50)
        self.assertTrue(result['search']['search_truncated'])
        self.assertTrue(result['search']['results_truncated'])
        self.assertEqual(result['search']['observations_read'], 2000)
        self.assertEqual(result['search']['paths_found'], 1000)
        self.assertLessEqual(result['search']['middle_queries'], 200)
        self.assertLessEqual(result['search']['paths_found'], 5000)
        self.assertTrue(all(path['length'] <= 3 for path in result['paths']))
        self.assertEqual(len(connections.search(self.conn, 'member_', limit=50)['accounts']), 50)
        self.assertEqual(tuple(self.conn.iterdump()), before)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed to execute the production UI')
    def test_stored_imported_payloads_stay_text_and_request_values_stay_isolated(self):
        # Both source and member metadata cross the import-to-render boundary.
        source_handle = 'source' + PAYLOAD
        member_handle = 'member' + PAYLOAD
        self.person(source_handle, '3', PAYLOAD)
        self.seed(source_handle, '3')
        pid = self.person(member_handle, '4', PAYLOAD)
        self.edge(source_handle, '3', pid, '4')
        target = self.conn.execute('SELECT id FROM people WHERE ig_id=?', ('2',)).fetchone()[0]
        self.edge(member_handle, '4', target, '2')
        self.conn.execute('INSERT INTO lists(seed,direction,state,received,total,error,updated_at) VALUES(?,?,?,?,?,?,?)',
                          (source_handle, 'following', PAYLOAD, PAYLOAD, PAYLOAD, PAYLOAD, PAYLOAD))
        # Non-profile URL-shaped handles are legal stored data but must stay plain text.
        for ig, value in [('5', 'javascript:globalThis.security_probe=1'), ('6', '//attacker.invalid/path')]:
            middle = self.person(value, ig, PAYLOAD)
            self.edge(source_handle, '3', middle, ig)
            self.edge(value, ig, target, '2')
        result = connections.explore(self.conn, 'ig:3', 'ig:2')
        requested_from = "ig:3&to=ig:999&mode=following#'\"<>"
        requested_to = 'ig:2&limit=500000'
        suggestions = {'accounts': [dict(n, is_seed=False, is_me=False) for n in result['nodes']], 'truncated': False}
        completed = subprocess.run(
            ['node', '-e', UI_RECORDER], input=json.dumps({'root': str(ROOT), 'result': result,
                'suggestions': suggestions, 'from': requested_from, 'to': requested_to}),
            text=True, capture_output=True, timeout=15, check=True)
        rendered = json.loads(completed.stdout)
        self.assertEqual(rendered['routeCount'], 3)
        self.assertGreater(rendered['suggestionCount'], 0)
        self.assertFalse(rendered['executed'])
        inventory = HTMLInventory()
        for fragment in rendered['writes']:
            inventory.feed(fragment)
        self.assertIn(PAYLOAD, ''.join(inventory.text))
        self.assertIn(PAYLOAD.lower(), ''.join(inventory.text))
        self.assertIn(source_handle.lower(), rendered['status'])
        anchors = []
        for tag, attrs in inventory.tags:
            self.assertNotIn(tag, {'script', 'img', 'svg', 'iframe', 'object', 'embed', 'style'})
            self.assertFalse(any(key.startswith('on') for key in attrs), (tag, attrs))
            if tag == 'a':
                anchors.append(attrs)
                self.assertEqual(attrs.get('href'), 'https://www.instagram.com/omega/')
                self.assertEqual(set(attrs.get('rel', '').split()), {'noopener', 'noreferrer'})
        self.assertTrue(anchors)
        request_queries = {urlsplit(url).path: parse_qs(urlsplit(url).query) for url in rendered['requests']}
        self.assertEqual(request_queries['/api/connections/search'], {'q': [requested_from]})
        self.assertEqual(request_queries['/api/connections'],
                         {'from': [requested_from], 'to': [requested_to], 'mode': ['all']})


if __name__ == '__main__':
    unittest.main()
