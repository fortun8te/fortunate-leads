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

function liveStart() {
  const listeners = {}, messages = []; let reads = 0, offline = false;
  const body = { innerHTML: '' }, input = { value: '@glowbrand.co' }, error = { textContent: '', hidden: true };
  const root = { classList: { contains: () => true }, querySelector: selector => ({ '#st-body': body, '#st-in': input, '#st-err': error, '#st-form': {} })[selector],
    addEventListener: (name, handler) => { listeners[name] = handler; } };
  const nodes = { 'view-start': root, 'tab-start': {}, 'n-start': {} };
  const data = { ready: false, open: 1, blocking: 1, steps: [], flow: { state: 'idle', people: 0, bios: 0, ranked: 0, lists: [], headline: 'Ready' } };
  const document = { hidden: false, activeElement: null, getElementById: id => nodes[id], querySelector: () => ({}), addEventListener() {} };
  const window = { parseHandles: () => ['glowbrand.co'], toast: msg => messages.push(msg), addEventListener() {}, dispatchEvent() {} };
  const context = vm.createContext({ window, document, location: { hash: '#/start' }, parseHandles: window.parseHandles, toast: window.toast,
    setTimeout() {}, clearTimeout() {}, Event,
    fetch: async (_url, options) => { if (!options?.method) { ++reads; if (offline) throw Error('offline'); } return options?.method === 'POST' ? { ok: false, json: async () => ({ error: 'lease_429_internal' }) } : { ok: true, json: async () => data }; } });
  vm.runInContext(read('start.js'), context);
  return { window, listeners, messages, body, input, error, setOffline(value) { offline = value; }, get reads() { return reads; } };
}

test('failed start keeps the entered handle and gives actionable feedback without raw diagnostics', async () => {
  const h = liveStart();
  await h.window.Start.refresh();
  await h.listeners.submit({ target: { id: 'st-form' }, preventDefault() {} });
  assert.match(h.body.innerHTML, /id="st-in" value="@glowbrand.co"/);
  assert.equal(h.error.textContent, "Couldn't start collection. Try again.");
  assert.equal(h.error.hidden, false);
  assert.doesNotMatch(h.error.textContent, /lease|429/);
});

test('an optional setup save failure is reported instead of silently disappearing', async () => {
  const h = liveStart();
  await h.window.Start.refresh();
  await h.listeners.click({ target: { closest: selector => selector === '[data-st-skip]' ? { dataset: { stSkip: 'ai' } } : null } });
  assert.deepEqual(h.messages, ["Couldn't save setup. Try again."]);
});

test('overlapping setup refreshes share one request instead of repeating local work', async () => {
  const h = liveStart();
  const first = h.window.Start.refresh(), second = h.window.Start.refresh();
  await Promise.all([first, second]);
  assert.equal(h.reads, 1);
  assert.match(h.body.innerHTML, />Start collection<\/button>/);
});

test('stopped setup explains how to continue without adding the handle again', () => {
  const html = V.flowHTML({ state: 'paused', headline: 'Collection is paused. Press Start to continue.', people: 12, bios: 3, ranked: 2, lists: [] });
  assert.match(html, /Collection is stopped. Continue collecting where you left off./);
  assert.doesNotMatch(html, /Press Start/);
});


test('saved setup becomes visibly unavailable after disconnect and recovers on retry', async () => {
  const h = liveStart();
  await h.window.Start.refresh();
  h.setOffline(true);
  await h.window.Start.refresh();
  assert.match(h.body.innerHTML, /Cannot update collection status/);
  assert.match(h.body.innerHTML, /id="st-go" disabled/);
  const before = h.reads;
  await h.listeners.submit({target:{id:'st-form'},preventDefault(){}});
  assert.equal(h.reads, before);
  h.setOffline(false);
  await h.listeners.click({target:{closest: selector => selector === '[data-st-retry]' ? {} : null}});
  assert.doesNotMatch(h.body.innerHTML, /Cannot update collection status|id="st-go" disabled/);
});
