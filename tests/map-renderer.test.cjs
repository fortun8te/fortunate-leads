const { test } = require('node:test');
const assert = require('node:assert/strict');
const { Index, Points } = require('../web/map-renderer.js');
const { performance } = require('node:perf_hooks');

test('100,000 distinct bubbles retain identity and bounded spatial picking', () => {
  const before = process.memoryUsage().heapUsed;
  const nodes = Array.from({ length: 100000 }, (_, i) => ({ id: `p:${i}`, x: (i % 400) * 24, y: Math.floor(i / 400) * 24, r: 5 }));
  const start = performance.now(), index = new Index(nodes), buildMs = performance.now() - start;
  assert.equal(index.query(-1, -1, 10000, 10000).length, 100000);
  assert.equal(index.query(-1, -1, 10000, 10000, 600).length, 600);
  const pickStart = performance.now();
  for (let i = 0; i < 1000; i++) {
    const n = nodes[(i * 7919) % nodes.length];
    assert.equal(index.pick(n.x, n.y, 1), n);
  }
  const pickMs = performance.now() - pickStart;
  assert.equal(index.pick(-1000, -1000, 1), null);
  console.log(JSON.stringify({ people: nodes.length, buildMs: Math.round(buildMs), thousandPicksMs: Math.round(pickMs), heapDeltaMB: Math.round((process.memoryUsage().heapUsed - before) / 1048576) }));
});

test('viewport results and expanded radius picking agree with direct geometry', () => {
  const nodes = Array.from({ length: 5000 }, (_, i) => ({ id: i, x: Math.sin(i * 7) * 700, y: Math.cos(i * 3) * 500, r: 4 + i % 18 }));
  const index = new Index(nodes);
  const ids = ns => ns.map(n => n.id).sort((a, b) => a - b);
  assert.deepEqual(ids(index.query(-100, -50, 150, 120)), ids(nodes.filter(n => n.x >= -100 && n.x <= 150 && n.y >= -50 && n.y <= 120)));
  for (const k of [0.04, 0.1, 1, 10]) for (let i = 0; i < 40; i++) {
    const x = i * 31 - 500, y = i * 13 - 300;
    let expected = null, score = Infinity;
    for (const n of nodes) { const r = Math.max(n.r, 2.4 / k), d = Math.hypot(n.x - x, n.y - y); if (d <= Math.max(r + 3 / k, 7 / k) && d - r < score) { expected = n; score = d - r; } }
    assert.equal(index.pick(x, y, k), expected);
  }
});

test('GPU upload and draw preserve all 100,000 point vertices', () => {
  let uploaded, count;
  const gl = new Proxy({ getShaderParameter: () => true, getProgramParameter: () => true, bufferData: (_, data) => uploaded = data, drawArrays: (_, __, n) => count = n }, { get: (o, p) => p in o ? o[p] : () => ({}) });
  const canvas = { width: 0, height: 0, getContext: () => gl, addEventListener() {} };
  const renderer = new Points(canvas);
  renderer.set(Array.from({ length: 100000 }, (_, i) => ({ x: i, y: i * 2, r: 7 })));
  assert.equal(uploaded.length, 300000);
  assert.equal(uploaded[299997], 99999);
  assert.equal(renderer.draw(900, 600, 0, 0, 1, [0.5, 0.5, 0.5, 1]), true);
  assert.equal(count, 100000);
});

test('100k full map build uses recorded seed membership without force simulation', () => {
  const vm = require('node:vm'), fs = require('node:fs');
  const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
  const mapSource = source.slice(source.indexOf('const M = {'), source.indexOf('\nfunction hoverCard(n)'));
  const fields = new Map(), context = { console, Map, Set, Math, JSON, DenseMap: { Index }, debounce: fn => fn, int: String, fitOf: () => 'unread', LEAD_R: [0, 9, 10, 11, 12, 13], S: {}, window: { DenseMap: { Index } }, $: id => { if (!fields.has(id)) fields.set(id, {}); return fields.get(id); } };
  vm.createContext(context); vm.runInContext(mapSource + '\nthis.map = M;', context);
  const map = context.map; map.draw = () => {}; map.fit = () => {}; map.renderSearch = () => {};
  const nodes = [{ id: 's:a', kind: 'seed', label: 'a', degree: 50000 }, { id: 's:b', kind: 'seed', label: 'b', degree: 50000 }];
  for (let i = 0; i < 100000; i++) nodes.push({ id: `p:${i}`, kind: 'lead', seeds: [i % 2 ? 'a' : 'b'], lists: 1 });
  nodes[2].owner_relationship = 'mutual'; nodes[3].owner_relationship = 'follows'; nodes[4].owner_relationship = 'followed';
  const start = performance.now(), before = process.memoryUsage().heapUsed;
  map.build({ nodes, links: [], seed_links: [], total: 5000000 });
  assert.equal(map.leads.length, 100000); assert.equal(map.sim, null);
  assert.equal(map.selfRelation.get('p:0'), 'both'); assert.equal(map.selfRelation.get('p:1'), 'followers'); assert.equal(map.selfRelation.get('p:2'), 'following');
  assert.equal(map.seeds[0].vis, 50000); assert.equal(map.seeds[1].vis, 50000);
  assert.equal(nodes[2].x, undefined, 'raw response must not be mutated with layout coordinates');
  assert.ok(map.leads.every(n => Number.isFinite(n.x) && Number.isFinite(n.y)));
  const oldX = map.leads[100].x, oldY = map.leads[100].y;
  map.build({ nodes, links: [], seed_links: [], total: 5000000 });
  assert.equal(map.leads[100].x, oldX); assert.equal(map.leads[100].y, oldY);
  console.log(JSON.stringify({ fullBuildAndRefreshMs: Math.round(performance.now() - start), heapDeltaMB: Math.round((process.memoryUsage().heapUsed - before) / 1048576) }));
});

function loadMapContext(extras = {}) {
  const vm = require('node:vm'), fs = require('node:fs');
  const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
  const mapSource = source.slice(source.indexOf('const M = {'), source.indexOf('\nfunction hoverCard(n)'));
  const fields = new Map(), context = { console, Map, Set, Math, JSON, URLSearchParams, debounce: fn => fn, int: String, S: { f: {} }, LeadWorkflow: { runtimeQuery: p => p }, $: id => { if (!fields.has(id)) fields.set(id, { hidden: true }); return fields.get(id); }, ...extras };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('function toQuery('), source.indexOf('function fromQuery(')) + mapSource + '\nthis.map = M;', context);
  return context;
}

test('dense polling sends revision only for exact previously loaded URL and stops on unchanged', async () => {
  const requests = [], context = loadMapContext({ api: { get: async url => { requests.push(url); return url.includes('&rev=') ? { unchanged: true, rev: 'version 1' } : { rev: 'version 1', nodes: [{ id: 'p:1' }], links: [] }; } } });
  const map = context.map; let base = '/api/map?limit=100000', builds = 0;
  map.limit = 100000; map.url = () => base; map.build = d => { builds++; map.nodes = d.nodes; };
  await map.load(); assert.equal(builds, 1);
  await map.load(true); assert.equal(builds, 1); assert.equal(map.loading, false);
  assert.equal(requests[1], base + '&rev=version%201');
  base = '/api/map?seed=changed&limit=100000';
  await map.load(true); assert.equal(requests[2], base); assert.equal(builds, 2);
  await map.load(false); assert.equal(requests[3], base, 'explicit refresh must request full data');
  map.limit = 400; base = '/api/map?limit=400';
  await map.load(); await map.load(true);
  assert.equal(requests.at(-1), base, 'default map polling stays unchanged');
});

test('map URL preserves zero follower bounds and actual shared filters', () => {
  const context = loadMapContext();
  context.S.f = { tags: ['Friend'], any: [], not: [], fmin: 0, fmax: 0, relationship: 'mutual' };
  const map = context.map, query = new URL(map.url(), 'http://localhost').searchParams;
  assert.equal(query.get('followers_min'), '0'); assert.equal(query.get('followers_max'), '0');
  assert.equal(query.get('tags'), 'Friend'); assert.equal(query.get('relationship'), 'mutual');
  assert.equal(query.get('limit'), '400');
});
