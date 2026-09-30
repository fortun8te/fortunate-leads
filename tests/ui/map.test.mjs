import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
const require = createRequire(import.meta.url);
const { MapModel, cleanNode } = require('../../web/map-model.js');
const { Camera, Cache, Scene, Labeler, Latest } = require('../../web/map-core.js');
const person = (id, extra = {}) => ({ id, x: .4, y: .5, handle: 'person' + id, fit: 'good', ...extra });
const response = (rev, nodes = [person(1)]) => ({ rev, nodes, clusters: [], total: 10000000, shown: nodes.length, hidden: 0 });
function pendingModel() {
  const requests = [];
  const model = new MapModel({ reduced: true, fetchJson: (url, opts) => new Promise((resolve, reject) => requests.push({ url, ...opts, resolve, reject })) });
  return { model, requests };
}
test('latest viewport wins even when server ignores abort', async () => {
  const { model, requests } = pendingModel();
  const first = model.load(), second = model.load({ force: true });
  assert.equal(requests[0].signal.aborted, true);
  requests[1].resolve(response(2, [person(2)])); await second;
  requests[0].resolve(response(1)); await first;
  assert.equal(model.rev, '2'); assert.equal(model.scene.nodes[0].d.id, 2); assert.equal(model.pending, 0);
});
test('stale failure cannot replace a successful latest view', async () => {
  const { model, requests } = pendingModel();
  const first = model.load(), second = model.load({ force: true });
  requests[1].resolve(response(2)); await second;
  requests[0].reject(new TypeError('Network')); await first;
  assert.equal(model.phase, 'ready'); assert.equal(model.stale, null);
});
test('hiding cancels viewport, search, edges, and selection relocation', async () => {
  const { model, requests } = pendingModel();
  const load = model.load(); const search = model.searchPeople('alice'); model.select(person(1));
  model.pause(true);
  assert.ok(requests.every(r => r.signal.aborted)); assert.equal(model.pending, 0);
  requests[0].resolve(response(1)); requests[1].resolve({ results: [person(2)] }); requests[2].resolve({ edges: [] });
  await load; assert.equal(await search, null); assert.equal(model.rev, null);
});
test('typing or closing search invalidates earlier results immediately', async () => {
  const { model, requests } = pendingModel();
  const search = model.searchPeople('ali'); model.searchReq.cancel();
  requests[0].resolve({ results: [person(1)] }); assert.equal(await search, null);
});
test('revision change evicts older viewport entries and cache stays bounded', () => {
  const cache = new Cache(3);
  for (let i = 0; i < 10; i++) cache.set(String(i), response(1));
  assert.equal(cache.size, 3); cache.set('new', response(2)); assert.equal(cache.size, 1); assert.equal(cache.get('9'), null);
});
test('same viewport cache cancels pending load and preserves newest cached revision', async () => {
  const { model, requests } = pendingModel();
  const first = model.load(); requests[0].resolve(response(1)); await first;
  await model.load(); assert.equal(requests.length, 1); assert.equal(model.stats.cacheHits, 1);
});
test('camera zoom preserves the point under the pointer', () => {
  const cam = new Camera(); const before = cam.toWorld(310, 180); cam.zoomAt(2, 310, 180);
  assert.deepEqual(cam.toWorld(310, 180), before);
});
test('camera flight wakes the renderer and reaches its destination', () => {
  const model = new MapModel({ now: () => 0 }); const events = []; model.on(e => events.push(e));
  model.flyTo({ cx: .6, cy: .7, k: 4 }, 300); assert.ok(events.includes('camera'));
  model.pause(true); model.tick(300, .016); assert.equal(model.cam.k, 4); assert.equal(model.flight, null);
});
test('new layout moves pinned selection rather than keeping stale coordinates', () => {
  const scene = new Scene(); scene.apply(response(1).nodes ? { nodes: [person(1)], clusters: [] } : {}, 0, { instant: true });
  scene.pin(person(1, { x: .8 })); assert.equal(scene.get(1).x, .8); assert.equal(scene.get(1).d.x, .8);
});
test('recorded seed chips exclude shared-audience neighbours and unrelated edges', () => {
  const model = new MapModel(); const selected = person(1);
  const data = { nodes: [person(2, { source: true }), person(3), person(4, { source: true })], edges: [
    { kind: 'follow', from: 2, to: 1 }, { kind: 'follow', from: 3, to: 1 }, { kind: 'overlap', a: 4, b: 1 }, { kind: 'follow', from: 2, to: 4 }
  ] };
  const edges = model.readEdges(selected, data); assert.equal(edges.lines.length, 3); assert.deepEqual(edges.seeds.map(n => n.id), [2]);
});
test('unprepared layout does not claim to be building or empty', async () => {
  const model = new MapModel({ fetchJson: async (url) => { if (!url.startsWith('/api/map/view')) throw new Error('fallback unavailable'); return { ready: false, layout: { building: false }, nodes: [] }; } });
  await model.load(); assert.equal(model.phase, 'unprepared');
});
test('real build state is distinct from an unprepared layout', async () => {
  const model = new MapModel({ fetchJson: async (url) => { if (!url.startsWith('/api/map/view')) throw new Error('fallback unavailable'); return { ready: false, layout: { building: true }, nodes: [] }; } });
  await model.load(); assert.equal(model.phase, 'building');
});
test('failed refresh keeps previously loaded people visible and offers retry', async () => {
  let fail = false;
  const model = new MapModel({ reduced: true, fetchJson: async () => { if (fail) throw new TypeError('Offline'); return response(1); } });
  await model.load(); fail = true; await model.load({ force: true });
  assert.equal(model.phase, 'ready'); assert.equal(model.stale, 'offline'); assert.equal(model.scene.nodes.length, 1);
});
test('status updates invalidate cached old statuses', async () => {
  const model = new MapModel({ reduced: true, fetchJson: async () => response(1) });
  await model.load(); model.selected = person(1); model.patchStatus(1, 'client');
  assert.equal(model.cache.size, 0); assert.equal(model.selected.status, 'client'); assert.equal(model.scene.get(1).d.status, 'client');
});
test('label collision avoids covering existing labels', () => {
  const labels = new Labeler(800, 600); const first = labels.place({ x: 400, y: 300, r: 10 }, { w: 70, h: 18 });
  const second = labels.place({ x: 400, y: 300, r: 10 }, { w: 70, h: 18 });
  assert.ok(first); assert.ok(second); assert.notDeepEqual(first, second);
});
test('invalid coordinates cannot poison the scene', () => {
  assert.equal(cleanNode(person(1, { x: NaN })), null); assert.equal(cleanNode(person(1, { y: Infinity })), null);
});
test('combobox has a list alternative, radio modes, and mobile sheet', () => {
  const html = readFileSync(new URL('../../web/index.html', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../../web/map-view.css', import.meta.url), 'utf8');
  assert.match(html, /role="combobox"/); assert.match(html, /aria-controls="map-search-results"/); assert.match(html, /Open Leads/); assert.match(css, /max-height: 56%/);
});

test('unprepared spatial layout still offers bounded honest ranked overview', async () => {
  const urls = [];
  const model = new MapModel({ reduced: true, fetchJson: async url => { urls.push(url); return url.startsWith('/api/map/view') ? { ready: false, nodes: [], layout: { building: false } } : { total: 10000000, nodes: Array.from({ length: 1000 }, (_, i) => ({ id: 'p:' + (i + 1), kind: 'lead', handle: 'person' + i, fit: 'good' })) }; } });
  await model.load(); assert.equal(model.phase, 'ready'); assert.equal(model.fallback, true); assert.equal(model.scene.nodes.length, 400); assert.equal(model.total, 10000000);
  assert.ok(urls[1].includes('limit=400')); assert.equal(model.world.me, undefined);
  assert.equal(model.scene.nodes[0].d.closeness, null);
});
