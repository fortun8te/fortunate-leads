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
test('size changes preserve people, selection and camera without fetching another page', () => {
  const requests = [];
  const model = new MapModel({ reduced: true, fetchJson: url => { requests.push(url); throw new Error('Unexpected request'); } });
  model.apply({ ...response(1, [person(1, { followers: 100 }), person(2, { followers: 1000000 })]), world: { me: person(0, { x: .5, y: .5 }) } }, {});
  model.cam.set(.3, .4, 2);
  model.selected = model.scene.get(1).d;
  const ids = model.scene.nodes.map(n => n.d.id);
  const followers = model.scene.nodes.filter(n => n.d.id !== 0).map(n => n.d.portraitRadius);
  assert.notEqual(followers[0], followers[1]);
  model.setSizeEncoding('equal');
  assert.deepEqual(model.scene.nodes.map(n => n.d.id), ids);
  assert.equal(new Set(model.scene.nodes.filter(n => n.d.id !== 0).map(n => n.d.portraitRadius)).size, 1);
  assert.deepEqual([model.cam.cx, model.cam.cy, model.cam.k], [.3, .4, 2]);
  assert.equal(model.selected, model.scene.get(1).d);
  assert.deepEqual(requests, []);
});
test('invalid coordinates cannot poison the scene', () => {
  assert.equal(cleanNode(person(1, { x: null })), null); assert.equal(cleanNode(person(1, { x: NaN })), null); assert.equal(cleanNode(person(1, { y: Infinity })), null);
});
test('combobox has a list alternative and mobile sheet', () => {
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
  assert.equal(model.kFor(model.scene.nodes[0].d), 1, 'overview search should keep context at large totals');
});

// Real API search places coordinates inside positions, unlike the demo world.
test('real search positions and numeric fit survive the API boundary', async () => {
  const model = new MapModel({ fetchJson: async () => ({ results: [{ id: 3, handle: 'source', fit: 81, positions: { closeness: { x: .3, y: .2, rank: .9 }, fit: { x: .8, y: .7, rank: .8 } } }] }) });
  const rows = await model.searchPeople('source');
  assert.equal(rows.length, 1); assert.equal(rows[0].x, .3); assert.equal(rows[0].fit, 'strong');
  model.mode = 'fit';
  const other = await model.searchPeople('source'); assert.equal(other[0].x, .8);
  assert.equal(cleanNode(person(1, { fit: 45 })).fit, 'good');
  assert.equal(cleanNode(person(1, { fit: 44 })).fit, 'weak');
  assert.equal(cleanNode(person(1, { fit: null })).fit, 'unread');
});


test('viewport totals keep the full filtered population available across zoom levels', async () => {
  const model = new MapModel({reduced:true,fetchJson:async()=>({...response(1),total:20,world_total:111699})});
  await model.load();
  assert.equal(model.total,20);
  assert.equal(model.worldTotal,111699);
  assert.equal(model.shown,1);
});

test('zoom and pan magnify the loaded cohort without requesting replacement people', async () => {
  const requests=[];
  const m=new MapModel({reduced:true,fetchJson:async(url)=>{requests.push(url);return response(1,[person(1),person(2)]);}});
  m.setSize(1092,665);await m.load();
  const ids=m.scene.nodes.map(n=>n.d.id);
  m.cam.set(.7,.3,4);m.moved();
  assert.equal(m.needsLoad(),false);
  await m.load();
  assert.equal(requests.length,1);
  assert.deepEqual(m.scene.nodes.map(n=>n.d.id),ids);
});

test('paging is explicit and failed next-page requests preserve the current page', async()=>{
 let fail=false;const requests=[];
 const m=new MapModel({reduced:true,fetchJson:async url=>{requests.push(url);if(fail)throw new Error('failed');return {...response(1,[person(1)]),next_cursor:'[0.5,1,"rev"]'};}});
 await m.load();const ids=m.scene.nodes.map(n=>n.d.id);fail=true;await m.browse(1);
 assert.equal(m.pageIndex,0);assert.equal(m.cursor,'');assert.deepEqual(m.scene.nodes.map(n=>n.d.id),ids);
 assert.match(requests[1],/after=/);
});

test('map status saves read back the server result and revert failed writes',async()=>{
 const {MapView}=require('../../web/map-view.js');
 const writes=[];
 const view={r:{card:{querySelector:()=>null}},model:{scene:{get:()=>({d:{status:'interested'}})},patchStatus:(id,status)=>writes.push(status)},host:{setStatus:async()=>({status:'talking'}),toast:()=>{}}};
 await MapView.prototype.setStatus.call(view,{id:1,status:'interested'},'contacted');
 assert.deepEqual(writes,['contacted','talking']);writes.length=0;
 view.host.setStatus=async()=>{throw new Error('save failed');};
 await MapView.prototype.setStatus.call(view,{id:1,status:'interested'},'contacted');
 assert.deepEqual(writes,['contacted','interested']);
});

test('search outside a zoomed cohort pans to the selected person without changing zoom',()=>{
 const requests=[];
 const model=new MapModel({reduced:true,fetchJson:async url=>{requests.push(url);return {nodes:[],edges:[]};}});
 model.apply(response(1,[person(1),person(2)]),{});model.cam.set(.2,.3,3);
 model.goTo(person(3,{x:.9,y:.9}));
 assert.equal(model.cam.k,3);assert.equal(model.cam.cx,model.selected.x);assert.equal(model.cam.cy,model.selected.y);
 assert.equal(model.selected.id,3);assert.ok(requests.every(url=>!url.includes('/api/map/view')));
});

test('map number shortcuts still set and clear status without the removed hidden button list',()=>{
 const {MapView}=require('../../web/map-view.js');const writes=[];
 const view={model:{selected:{id:1}},host:{statuses:['interested','contacted','talking','spoke_before','no']},setStatus:(n,status)=>writes.push([n.id,status])};
 for(const key of ['1','5','0']) MapView.prototype.key.call(view,{key,stopPropagation:()=>{}},true);
 assert.deepEqual(writes,[[1,'interested'],[1,'no'],[1,null]]);
});

test('compact tag editor focuses on reveal, preserves drafts on Escape, and keeps failed saves open', async()=>{
 const {MapView}=require('../../web/map-view.js');
 const focus=[];
 const input={value:'Founder',disabled:false,focus:()=>focus.push('input')};
 const button={disabled:false,setAttribute(name,value){this[name]=value;},focus:()=>focus.push('button')};
 const form={hidden:true,querySelector:()=>input};
 const feedback={textContent:''};
 const box={isConnected:true,querySelector:selector=>({'.mv-tag-form':form,'.mv-tag-add':button,'.mv-tag-feedback':feedback,input}[selector]),querySelectorAll:()=>[input,button]};
 let fail;
 const view={r:{card:{querySelector:()=>box}},model:{selected:{id:1},patchStatus(){}},host:{editTags:()=>new Promise((_,reject)=>{fail=reject;})},paintTags(){}};
 view.toggleTagForm=show=>MapView.prototype.toggleTagForm.call(view,show);
 view.toggleTagForm(true);assert.equal(form.hidden,false);assert.equal(button['aria-expanded'],'true');assert.equal(focus.at(-1),'input');
 view.toggleTagForm(false);assert.equal(form.hidden,true);assert.equal(input.value,'Founder');assert.equal(focus.at(-1),'button');
 view.toggleTagForm(true);
 const saving=MapView.prototype.editTag.call(view,{id:1},'Founder',false);
 view.toggleTagForm(false);assert.equal(form.hidden,false);assert.equal(input.disabled,true);
 fail(new Error('offline'));await saving;
 assert.equal(form.hidden,false);assert.equal(input.value,'Founder');assert.equal(input.disabled,false);assert.equal(feedback.textContent,'Couldn’t save. Try again.');
 view.host.editTags=async()=>({manual_tags:['Founder'],status:null});
 await MapView.prototype.editTag.call(view,{id:1},'Founder',false);
 assert.equal(form.hidden,true);assert.equal(input.value,'');assert.equal(feedback.textContent,'Saved');assert.equal(focus.at(-1),'button');
});

test('map card merges stored relationships and manual tags without a duplicate Client', () => {
  const { cardTags } = require('../../web/map-view.js');
  assert.deepEqual(cardTags({ relationships: ['client', 'friend'], manual_tags: ['Client', 'Founder'] }), [
    { label: 'Client', relationship: 'client' },
    { label: 'Friend', relationship: 'friend' },
    { label: 'Founder', relationship: undefined }
  ]);
  assert.equal(cardTags({ status: 'client' }).filter(tag => tag.relationship === 'client').length, 1);
  assert.deepEqual(cardTags({ relationships: [], status: 'client' }), []);
});

test('map qualification distinguishes missing evidence, unclear rules and a scored estimate', () => {
  const { cardFit } = require('../../web/map-view.js');
  assert.equal(cardFit({ business_fit: 34 }).text, 'Not reviewed');
  const unclear = cardFit({ business_fit: 34, verdict: { model: 'rules', role: 'unclear' } });
  assert.equal(unclear.text, 'Unclear');
  assert.match(unclear.detail, /Not enough evidence/);
  const assessed = cardFit({ business_fit: 34, verdict: { model: 'rules', role: 'agency' } });
  assert.equal(assessed.text, '34/100');
  assert.match(assessed.detail, /Rules estimate.*physical-product/);
  assert.equal(cardFit({ business_fit: 0, verdict: { model: 'rules', role: 'agency' } }).text, '0/100');
});

test('selected card overlays the map and keeps pipeline and connection detail closed initially', () => {
  const css = readFileSync(new URL('../../web/map-view.css', import.meta.url), 'utf8');
  const source = readFileSync(new URL('../../web/map-view.js', import.meta.url), 'utf8');
  const cardRule = css.match(/\.mv-card \{([^}]+)\}/)[1];
  assert.match(cardRule, /position: absolute/);
  assert.match(cardRule, /max-height: calc\(100% - 24px\)/);
  assert.match(source, /h\('details', \{ class: 'mv-lead-stage' \}/);
  assert.match(source, /h\('details', \{ class: 'mv-connections' \}/);
  assert.match(source, /setConnectionsVisible\(connections\.open\)/);
});

test('compact card hydrates identity locally without replacing geometry or a newer status', () => {
  const { MapView } = require('../../web/map-view.js');
  const node = { id: 51, compact: true, handle: '51', x: .4, y: .6, status: 'talking' };
  const fields = { '.mv-who b': {}, '.mv-who span': {}, '.mv-av': { replaceChildren(value) { this.value = value; } } };
  const view = {
    model: { selected: node, scene: { get: () => ({ d: node }) } },
    r: { card: { querySelector: selector => fields[selector] } }, invalidate() {}
  };
  MapView.prototype.hydrateCardIdentity.call(view, node, { id: 51, handle: 'alice', name: 'Alice Founder', status: 'interested' });
  assert.equal(fields['.mv-who b'].textContent, 'Alice Founder');
  assert.equal(fields['.mv-who span'].textContent, '@alice');
  assert.equal(fields['.mv-av'].value, 'AF');
  assert.equal(node.compact, false);
  assert.equal(node.status, 'talking');
  assert.equal(node.x, .4);
  assert.equal(node.y, .6);
  MapView.prototype.hydrateCardIdentity.call(view, node, { id: 52, name: 'Wrong profile' });
  assert.equal(node.name, 'Alice Founder');
});

test('compact identity reads ignore an old selection and show a useful local read failure', async () => {
  const { MapView } = require('../../web/map-view.js');
  const node = { id: 51, compact: true, handle: '51' };
  const fields = { '.mv-who b': { textContent: 'Loading profile…' }, '.mv-who span': { textContent: '' }, '.mv-summary > div:last-child dd': { textContent: 'Loading…' } };
  const feedback = {};
  const box = { isConnected: true, querySelector: () => feedback };
  const requests = [], hydrated = [];
  const view = {
    model: { selected: node },
    r: { card: { querySelector: selector => selector === '.mv-tags' ? box : fields[selector] } },
    host: { person: () => new Promise((resolve, reject) => requests.push({ resolve, reject })) },
    hydrateCardIdentity: (_, person) => hydrated.push(person.id), paintTags() {}
  };
  const first = MapView.prototype.loadTags.call(view, node);
  view.model.selected = { id: 52 };
  requests[0].resolve({ id: 51, handle: 'alice' });
  await first;
  assert.deepEqual(hydrated, []);
  view.model.selected = node;
  const second = MapView.prototype.loadTags.call(view, node);
  requests[1].reject(new Error('offline'));
  await second;
  assert.equal(fields['.mv-who b'].textContent, 'Profile unavailable');
  assert.equal(fields['.mv-who span'].textContent, '');
  assert.equal(fields['.mv-summary > div:last-child dd'].textContent, 'Unavailable');
  assert.match(feedback.textContent, /Reopen to retry/);
  const stale = MapView.prototype.loadTags.call(view, node);
  const latest = MapView.prototype.loadTags.call(view, node);
  requests[3].resolve({ id: 51, handle: 'alice' });
  await latest;
  requests[2].reject(new Error('late failure'));
  await stale;
  assert.deepEqual(hydrated, [51]);
  assert.equal(feedback.textContent, '');
});
