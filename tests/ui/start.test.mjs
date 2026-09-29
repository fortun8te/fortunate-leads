import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';

const read = (p) => readFileSync(new URL('../../web/' + p, import.meta.url), 'utf8');
const html = read('index.html'), app = read('app.js');
const window = {};
vm.runInNewContext(read('start.js'), {window, document: undefined});
const V = window.StartView;

test('Get started is wired into the shell, router and empty Leads state', () => {
  assert.match(html, /id="tab-start"[^>]*hidden/);
  assert.match(html, /id="view-start"/);
  assert.match(html, /<script src="start\.js"><\/script>/);
  assert.match(html, /href="start\.css"/);
  assert.match(app, /'settings', 'start'\]\.includes\(v\)/);
  assert.match(app, /window\.Start\?\.show\(\)/);
  assert.match(app, /href="#\/start">Get started/);
});

test('a missing extension shows one solid action; optional steps can be skipped', () => {
  const todo = V.stepHTML({id: 'extension', title: 'Chrome extension', status: 'todo', detail: 'No profile yet.', optional: false, items: [],
    action: {label: 'Connect a profile', route: '#/accounts', kind: 'wizard'}});
  assert.match(todo, /data-st-wizard>Connect a profile/);
  assert.doesNotMatch(todo, /data-st-skip/);
  const ai = V.stepHTML({id: 'ai', title: 'AI checking', status: 'todo', detail: 'Optional.', optional: true, items: [], action: {label: 'Add a key', route: '#/settings'}});
  assert.match(ai, /data-st-skip="ai"/);
  assert.match(ai, /href="#\/settings"/);
});

test('per-profile rows and text are escaped', () => {
  const s = V.stepHTML({id: 'extension', title: 'Chrome extension', status: 'ok', detail: '2 connected', optional: false, action: null,
    items: [{name: '<b>@a</b>', state: 'ok', text: 'Connected'}, {name: '@b', state: 'wait', text: 'Not connected right now'}]});
  assert.match(s, /&lt;b&gt;@a/);
  assert.match(s, /Not connected right now/);
});

test('flow shows the headline, three numbers, list progress and best matches', () => {
  const flow = V.flowHTML({state: 'running', headline: "Collecting @x's followers: 240 of about 1,800 people so far.", people: 1234, bios: 56, ranked: 1200,
    lists: [{seed: 'x', direction: 'followers', received: 240, total: 1800, state: 'running'}, {seed: 'x', direction: 'following', received: 0, total: null, state: 'queued'}]});
  assert.match(flow, /role="status" aria-live="polite"/);
  assert.match(flow, /1,234/);
  assert.match(flow, /width:13%/);
  assert.match(flow, /240 of 1,800/);
  const leads = V.leadsHTML([{handle: 'a.b', name: 'A B', followers: 5200}]);
  assert.match(leads, /#\/leads\?q=a\.b/);
  assert.match(leads, /5,200 followers/);
  assert.equal(V.leadsHTML([]), '');
});

test('mock backend answers the onboarding and start routes with the real shape', async () => {
  const w = {fetch: async () => { throw Error('no network'); }};
  const c = vm.createContext({window: w, location: {origin: 'http://demo', search: '?mock=1&mock_setup=fresh'}, URL, URLSearchParams, Response, console, setInterval() {}, setTimeout: (f) => f()});
  vm.runInContext(read('mock.js'), c);
  const get = async (p, body) => (await w.fetch('/api/' + p, body ? {method: 'POST', body: JSON.stringify(body)} : {})).json();
  const before = await get('onboarding');
  assert.deepEqual(before.steps.map((s) => s.id), ['server', 'extension', 'instagram', 'ai', 'backups']);
  assert.equal(before.ready, false);
  assert.equal(before.flow.state, 'idle');
  const started = await get('start', {handles: ['glowbrand.co']});
  assert.equal(started.queued, 2);
  assert.equal(started.started, true);
});
