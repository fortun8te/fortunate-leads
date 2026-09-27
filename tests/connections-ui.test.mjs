import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// Small DOM seam: tests async state and safe text rendering, not browser layout.
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.events = {}; this.hidden = true; this.value = ''; this.classes = new Set(); this.classList = { toggle: (name, force) => force ? this.classes.add(name) : this.classes.delete(name) }; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(k, fn) { this.events[k] = fn; }
  focus() { this.focused = true; }
  get firstElementChild() { return this.children[0]; }
}
const code = readFileSync(new URL('../web/connections.js', import.meta.url), 'utf8');
function mount(search = '') {
  const panel = new Element('section'), toggle = new Element('button'), mapPane = new Element('div'), calls = [], events = [];
  const document = {
    getElementById: id => ({ 'connections-panel': panel, 'connections-toggle': toggle, 'pane-map': mapPane })[id],
    createElement: tag => new Element(tag),
    createElementNS: (_, tag) => new Element(tag)
  };
  vm.runInNewContext(code, { document, window: { dispatchEvent: event => events.push(event.type) }, CustomEvent: class { constructor(type) { this.type = type; } }, location: { search }, URLSearchParams, AbortController, fetch: (url, opts) => new Promise(resolve => calls.push({ url, opts, resolve })) });
  const form = panel.children[2], status = panel.children[3], results = panel.children[4];
  const submit = (a, b) => {
    form.children[0].children[0].value = a;
    form.children[1].children[0].value = b;
    return form.events.submit({ preventDefault() {} });
  };
  return { panel, toggle, mapPane, calls, events, form, status, results, submit };
}
const payload = (handle = 'alice') => ({ source: { id: 'a', handle }, target: { id: 'b', handle: 'bob' }, connectors: [], direct_relationships: [], total_candidates: 0, coverage: [], limitations: [] });
const response = data => ({ ok: true, json: async () => data });
const text = node => [node.textContent || '', ...node.children.map(text)].join(' ');

test('requires explicit comparison and demo never fetches fabricated data', async () => {
  const ui = mount('?mock=1');
  assert.equal(ui.calls.length, 0);
  assert.equal(ui.panel.hidden, false);
  assert.equal(ui.mapPane.classes.has('comparing'), true);
  assert.equal(ui.toggle.textContent, 'Explore map');
  assert.equal(ui.form.children[0].children[0].value, 'fortun8te');
  ui.toggle.events.click();
  assert.equal(ui.panel.hidden, true);
  assert.equal(ui.mapPane.classes.has('comparing'), false);
  assert.equal(ui.toggle.textContent, 'Compare profiles');
  ui.toggle.events.click();
  assert.equal(ui.panel.hidden, false);
  assert.equal(ui.form.children[1].children[0].focused, true);
  assert.deepEqual(ui.events, ['connections-viewchange', 'connections-viewchange']);
  await ui.submit('@Alice', 'Bob');
  assert.equal(ui.calls.length, 0);
  assert.match(ui.status.textContent, /requires collected data/);
});
test('late response cannot overwrite newer comparison and missing edges stay unknown', async () => {
  const ui = mount();
  const first = ui.submit('alice', 'bob');
  const second = ui.submit('carol', 'dave');
  assert.equal(ui.calls[0].opts.signal.aborted, true);
  ui.calls[1].resolve(response(payload()));
  await second;
  const displayed = text(ui.results);
  ui.calls[0].resolve(response({ ...payload(), total_candidates: 999 }));
  await first;
  assert.equal(text(ui.results), displayed);
  assert.match(displayed, /relationship is unknown/);
  assert.match(ui.status.textContent, /of 0/);
});
test('invalid follow-up cancels loading and does not make another request', async () => {
  const ui = mount();
  const pending = ui.submit('alice', 'bob');
  await ui.submit('alice', 'alice');
  assert.equal(ui.calls.length, 1);
  assert.equal(ui.panel.attrs['aria-busy'], undefined);
  ui.calls[0].resolve(response(payload()));
  await pending;
  assert.match(ui.status.textContent, /two different/);
});
test('untrusted handles and evidence are rendered as text, with directed evidence', async () => {
  const ui = mount();
  const pending = ui.submit('alice', 'bob');
  const data = payload('<img src=x onerror=alert(1)>');
  data.direct_relationships = [{ source: 'a', target: 'b', evidence: [{ seed: '<script>', direction: 'following', first_observed: '2026-01-01T00:00:00Z' }] }];
  ui.calls[0].resolve(response(data));
  await pending;
  const displayed = text(ui.results);
  assert.match(displayed, /<img src=x onerror=alert\(1\)> follows @bob/);
  assert.match(displayed, /Last observed: unknown/);
  assert.match(displayed, /Source: @<script>/);
  const walk = node => { assert.equal(Object.hasOwn(node, 'innerHTML'), false); node.children.forEach(walk); };
  walk(ui.results);
});
test('server failure does not imply absent connection', async () => {
  const ui = mount();
  const pending = ui.submit('alice', 'bob');
  ui.calls[0].resolve({ ok: false, json: async () => ({ error: 'Unavailable' }) });
  await pending;
  assert.match(ui.status.textContent, /No conclusion/);
  assert.equal(ui.panel.attrs['aria-busy'], undefined);
});

test('render failure removes any partial evidence before announcing no conclusion', async () => {
  const ui = mount();
  const pending = ui.submit('alice', 'bob');
  const data = payload();
  data.direct_relationships = [{ source: 'a', target: 'b' }];
  data.coverage = {};
  ui.calls[0].resolve(response(data));
  await pending;
  assert.match(ui.status.textContent, /Could not compare profiles/);
  assert.match(ui.status.textContent, /No conclusion/);
  assert.equal(ui.results.children.length, 0);
  assert.equal(ui.panel.attrs['aria-busy'], undefined);
});

test('coverage separates historical recorded counts from reported totals', async () => {
  const ui = mount();
  const pending = ui.submit('alice', 'bob');
  const data = payload();
  data.coverage = [{ seed: 'alice', direction: 'following', state: 'done', status: 'count_mismatch', received: 120, total: 100 }];
  ui.calls[0].resolve(response(data));
  await pending;
  assert.match(text(ui.results), /Recorded and reported counts differ/);
  assert.match(text(ui.results), /120 recorded across imports; 100 total reported/);
  assert.match(text(ui.results), /Experimental ordering/);
});

test('empty results identify submitted profiles after pending and completed form edits', async () => {
  const ui = mount();
  const pending = ui.submit('alice', 'bob');
  ui.form.children[0].children[0].value = 'carol';
  ui.form.children[1].children[0].value = 'dave';
  ui.calls[0].resolve(response(payload()));
  await pending;
  assert.equal(ui.results.children[0].tag, 'h3');
  assert.equal(ui.results.children[0].textContent, 'Results for @alice and @bob');
  assert.match(text(ui.results), /No direct follows or shared accounts/);
  ui.form.children[0].children[0].value = 'eve';
  assert.equal(ui.results.children[0].textContent, 'Results for @alice and @bob');
});

test('candidate controls announce selection and provide a direct keyboard path to described evidence', async () => {
  const ui = mount();
  const pending = ui.submit('alice', 'bob');
  const longHandle = 'a_full_thirty_character_handle';
  const data = payload(longHandle);
  data.direct_relationships = [{ source: 'a', target: 'b' }];
  data.connectors = [{ node: { id: 'c', handle: 'carol' }, links: [{ source: 'a', target: 'c' }, { source: 'c', target: 'b' }], motifs: ['directed_path'] }];
  ui.calls[0].resolve(response(data));
  await pending;
  const layout = ui.results.children.find(node => node.className === 'connections-layout');
  const [choices, detail] = layout.children;
  const announcement = ui.results.children.find(node => node.attrs.role === 'status');
  const readEvidence = ui.results.children.find(node => node.tag === 'button');
  assert.equal(choices.attrs.role, 'group');
  assert.equal(choices.attrs['aria-label'], 'Observed connection patterns');
  assert.equal(detail.attrs.tabindex, '-1');
  assert.equal(announcement.attrs['aria-live'], 'polite');
  assert.equal(announcement.textContent, 'Selected evidence: Direct follows.');
  assert.equal(readEvidence.attrs['aria-controls'], detail.id);
  readEvidence.events.click();
  assert.equal(detail.focused, true);
  detail.focused = false;
  const candidate = choices.children[1];
  assert.equal(candidate.attrs['aria-controls'], detail.id);
  assert.ok(ui.results.children.some(node => node.id === candidate.attrs['aria-describedby']));
  let prevented = false;
  candidate.events.keydown({ key: 'ArrowRight', preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(detail.focused, true);
  assert.equal(candidate.attrs['aria-pressed'], 'true');
  assert.equal(choices.children[0].attrs['aria-pressed'], 'false');
  assert.equal(announcement.textContent, 'Selected evidence: @carol.');
  assert.equal(detail.attrs['aria-label'], 'Selected connection evidence: @carol');
  const svg = detail.children.find(node => node.tag === 'svg');
  const directions = detail.children.find(node => node.id === svg.attrs['aria-describedby']);
  assert.ok(directions);
  assert.match(text(directions), new RegExp(`@${longHandle} follows @carol`));
  assert.match(text(directions), /@carol follows @bob/);
});
