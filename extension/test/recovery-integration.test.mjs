import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import FL from '../lib/core.js';

const source = fs.readFileSync(new URL('../background.js', import.meta.url), 'utf8').split('// ---- lifecycle')[0];
function worker(data = {}, hooks = {}) {
  const ctx = vm.createContext({FL, Date, Set, URLSearchParams, AbortController, setTimeout, clearTimeout,
    importScripts() {}, chrome: {
      runtime: {getManifest: () => ({version: '3.9.0'}), onMessage: {addListener() {}}},
      action: {setBadgeText() {}, setBadgeBackgroundColor() {}},
      storage: {local: {
        get: async keys => Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map(k => [k, structuredClone(data[k])])),
        set: async values => { hooks.write?.(values); Object.assign(data, structuredClone(values)); },
      }},
    }});
  vm.runInContext(source, ctx);
  ctx.apiStub = hooks.api || (async () => { throw new Error('offline'); });
  ctx.pageStub = hooks.page;
  ctx.domStub = hooks.dom;
  ctx.lookupStub = hooks.lookup || (async () => ({p: {ig_id: '12', handle: 'seed', followers: 3}}));
  vm.runInContext(`api = (...args) => apiStub(...args);
    igRequest = async (...args) => pageStub(...args);
    lookupViaPage = (...args) => lookupStub(...args);
    if (domStub) tabDom = (...args) => domStub(...args);
    waitUntil = async () => true;
    globalThis.checked = (handle, profile) => checkedProfileDom(1, handle, profile);
    globalThis.run = async job => { await set({cur: {job, at: Date.now()}}); await runList(mem.gen, job, {id: 1}); };
    globalThis.flush = () => flushBox();
    globalThis.error = job => fail(job, {code: 'other', reason: 'fixture'}, 'fixture', {status: 400}, 'list');
    globalThis.pausedStep = async () => { await set({localPaused: true}); return step(mem.gen); };
  `, ctx);
  return ctx;
}
const job = (cursor = null, token = 'lease-1') => ({id: 9, kind: 'list', seed: 'seed', direction: 'followers', ig_id: '12', received: 0, cursor, lease_token: token});
const user = n => ({pk: n, username: 'user' + n});

test('multi-page collection survives a worker restart and replays identical leased data before continuing', async () => {
  const data = {laneId: 'fixture-lane'};
  const posted = [];
  const first = worker(data, {page: async () => ({bad: null, res: {json: {data: {user: {
    edge_followed_by: {count: 3, edges: [{node: user(1)}], page_info: {has_next_page: true, end_cursor: 'A'}}}}}}})});
  await first.run(job());
  assert.equal(data.cur, null);
  assert.equal(data.box.length, 1);
  assert.equal(data.box[0].body.total_source, 'current_run');
  const saved = structuredClone(data.box[0].body);
  let pageNumber = 1;
  const resumed = worker(data, {
    api: async (path, body) => { posted.push(structuredClone(body)); return {status: 200, json: {received: posted.length}}; },
    page: async () => ({bad: null, res: {json: {data: {users: [user(++pageNumber)], has_more: pageNumber < 3, next_max_id: pageNumber < 3 ? 'B' : null}}}}),
  });
  await resumed.pausedStep();
  assert.deepEqual(posted[0], saved);
  assert.equal(data.box.length, 0);
  await resumed.run(job('A', 'lease-2'));
  await resumed.run(job('B', 'lease-3'));
  assert.deepEqual(posted.map(p => p.requested_cursor), [null, 'A', 'B']);
  assert.deepEqual(posted.map(p => p.next_cursor), ['A', 'B', null]);
  assert.deepEqual(posted.map(p => p.done), [false, false, true]);
  assert.ok(posted.every(p => p.total === 3 && p.total_source === 'current_run'));
});

test('server cycle acknowledgement retains the final people and stops local replay', async () => {
  const data = {laneId: 'fixture-lane'};
  let body;
  const ctx = worker(data, {
    page: async () => ({bad: null, res: {json: {users: [user(3)], has_more: true, next_max_id: 'A'}}}),
    api: async (path, posted) => { body = posted; return {status: 200, json: {received: 3, stalled: true, partial: true}}; },
  });
  await ctx.run(job('B'));
  assert.equal(body.requested_cursor, 'B');
  assert.equal(body.next_cursor, 'A');
  assert.equal(body.users[0].handle, 'user3');
  assert.equal(data.cur, null);
  assert.equal(data.box.length, 0);
});

test('transient failures and malformed acknowledgements never discard a leased result', async () => {
  const data = {laneId: 'fixture-lane', box: FL.enqueue([], '/api/ext/list-page', {job_id: 9, lease_token: 'old', users: [user(1)]})};
  let response;
  const ctx = worker(data, {api: async () => response});
  for (const r of [{status: 503, json: {}}, {status: 408}, {status: 429}, {status: 200, json: null}, {status: 200, json: {}}]) {
    response = r;
    for (let n = 0; n < 12; n++) assert.equal(await ctx.flush(), false);
    assert.equal(data.box.length, 1);
    assert.equal(data.dead, undefined);
  }
  response = {status: 200, json: {received: 1, duplicate: true}};
  assert.equal(await ctx.flush(), true);
  assert.equal(data.box.length, 0);
});

test('stale lease results stay recoverable even after more than fifty rejections', async () => {
  const box = Array.from({length: 70}, (_, i) => ({qid: String(i), path: '/api/ext/list-page', body: {job_id: i + 1, users: [user(i)]}}));
  const data = {box};
  const ctx = worker(data, {api: async () => ({status: 200, json: {received: 0, stale: true}})});
  await ctx.flush();
  assert.equal(data.box.length, 0);
  assert.equal(data.dead.length, 70);
  assert.equal(data.dead[0].body.users[0].username, 'user0');
});

test('storage failure while recording a job error preserves its resumable lease', async () => {
  const leased = job();
  const data = {laneId: 'fixture-lane', cur: {job: leased, at: Date.now()}};
  const ctx = worker(data, {write: values => { if ('box' in values) throw new Error('storage full'); }});
  await assert.rejects(ctx.error(leased), /storage full/);
  assert.deepEqual(data.cur.job, leased);
  assert.equal(data.box, undefined);
});

test('HTML list redirect with verified private wall hands off this viewer', async () => {
  const data = {laneId: 'fixture-lane'};
  let lookups = 0;
  const ctx = worker(data, {
    page: async () => ({bad: {code: 'other', reason: 'list_html_home_redirect'}, res: {
      status: 200, contentType: 'text/html', url: 'https://www.instagram.com/', redirected: true, text: '<html></html>'}}),
    lookup: async () => ++lookups === 1 ? {p: {ig_id: '12', handle: 'seed', is_private: true},
      info: {url: 'https://www.instagram.com/seed/', privateWall: false}} :
      {p: null, info: {url: 'https://www.instagram.com/seed/', privateWall: true, privateWallRechecked: true}},
  });
  await ctx.run(job());
  assert.equal(data.box[0].path, '/api/ext/error');
  assert.equal(data.box[0].body.code, 'private');
  assert.equal(data.box[0].body.reason, 'profile_private_wall');
  assert.equal(data.st.cool.list.until, 0);
  assert.equal(lookups, 2);
});

test('profile wall must persist across two reads at the target URL', async () => {
  const wall = {url: 'https://www.instagram.com/seed/', privateWall: true};
  let reads = 0;
  const confirmed = worker({}, {dom: async () => { reads++; return wall; }});
  assert.equal(FL.privateWall(await confirmed.checked('seed', null), 'seed', null), true);
  assert.equal(reads, 2);

  const transient = worker({}, {dom: async () => { reads++; return reads % 2 ? wall :
    {url: wall.url, privateWall: false}; }});
  reads = 0;
  assert.equal(FL.privateWall(await transient.checked('seed', null), 'seed', null), false);
});

test('HTML list redirect without wall quarantines only the target and keeps other work eligible', async () => {
  const data = {laneId: 'fixture-lane'};
  let lookups = 0;
  const ctx = worker(data, {
    page: async () => ({bad: {code: 'other', reason: 'list_html_home_redirect'}, res: {
      status: 200, contentType: 'text/html', url: 'https://www.instagram.com/', redirected: true, text: '<html></html>'}}),
    lookup: async () => { lookups++; return {p: {ig_id: '12', handle: 'seed', is_private: true},
      info: {url: 'https://www.instagram.com/seed/', privateWall: false}}; },
  });
  await ctx.run(job());
  assert.equal(data.box[0].body.code, 'other');
  assert.equal(data.box[0].body.reason, 'list_html_home_redirect');
  assert.equal(data.st.streak.other, 0);
  assert.equal(data.st.cool.list.until, 0);
  assert.match(data.st.lastError, /Instagram returned its home page/);
  assert.equal(lookups, 2);
});

test('follower-only public home redirects leave following available without claiming a rate limit', async () => {
  const data = {laneId: 'fixture-lane'};
  const ctx = worker(data, {
    page: async () => ({bad: {code: 'other', reason: 'list_html_home_redirect'}, res: {
      status: 200, contentType: 'text/html', url: 'https://www.instagram.com/', redirected: true, text: '<html></html>'}}),
    lookup: async (_gen, handle) => ({p: {ig_id: '12', handle, is_private: false},
      info: {url: `https://www.instagram.com/${handle}/`, privateWall: false}}),
  });
  for (const [i, seed] of ['public_one', 'public_two', 'public_three'].entries()) {
    await ctx.run({...job(), id: 20 + i, seed});
  }
  assert.equal(data.st.listRedirects.length, 3);
  assert.ok(data.st.listEndpointUntil > Date.now());
  assert.equal(data.st.cool.list.until, 0);
  assert.ok(data.box.every(x => x.body.reason === 'list_html_home_redirect'));
  assert.deepEqual(FL.plan(data.st, {list: 1, profile: 1}, Date.now()).kinds, ['list', 'profile']);
});

test('missing privacy and a failed fresh recheck never count as public redirects', async () => {
  const page = async () => ({bad: {code: 'other', reason: 'list_html_home_redirect'}, res: {
    status: 200, contentType: 'text/html', url: 'https://www.instagram.com/', redirected: true, text: '<html></html>'}});
  for (const mode of ['missing_privacy', 'failed_recheck']) {
    const data = {laneId: 'fixture-lane'};
    let lookups = 0;
    const ctx = worker(data, {page, lookup: async (_gen, handle) => {
      lookups++;
      const first = lookups % 2 === 1;
      return {p: mode === 'failed_recheck' && !first ? null :
        {ig_id: '12', handle, is_private: mode === 'missing_privacy' ? null : false},
        info: {url: `https://www.instagram.com/${handle}/`, privateWall: false}};
    }});
    for (const [i, seed] of ['one', 'two', 'three'].entries()) await ctx.run({...job(), id: 40 + i, seed});
    assert.equal(data.st.listRedirects.length, 0, mode);
    assert.equal(data.st.listEndpointUntil, 0, mode);
    assert.equal(data.box.length, 3, mode); // target-specific retries still reach the server
  }
});

test('recorded errors atomically clear the lease while keeping the backoff', async () => {
  const leased = job();
  const data = {laneId: 'fixture-lane', cur: {job: leased, at: Date.now()}};
  const ctx = worker(data);
  await ctx.error(leased);
  assert.equal(data.cur, null);
  assert.equal(data.box[0].path, '/api/ext/error');
  assert.equal(data.box[0].body.lease_token, leased.lease_token);
  assert.equal(data.st.streak.other, 1);
});

test('pause after the fresh seed lookup resumes the same lease without repeating that lookup', async () => {
  const data = {laneId: 'fixture-lane', ids: {seed: {ig_id: '12', followers: 99}}};
  let lookups = 0, requests = 0;
  const ctx = worker(data, {
    lookup: async () => { lookups++; return {p: {ig_id: '12', handle: 'seed', followers: 1}}; },
    page: async () => { requests++; return {bad: null, res: {json: {users: [user(1)], has_more: false}}}; },
  });
  vm.runInContext('waitUntil = async () => false;', ctx);
  await ctx.run(job());
  assert.equal(requests, 0);
  assert.equal(data.cur.job.lease_token, 'lease-1');
  assert.equal(data.prog['seed/followers'].totalSource, 'current_run');
  vm.runInContext('waitUntil = async () => true;', ctx);
  await ctx.run(job());
  assert.equal(lookups, 1);
  assert.equal(requests, 1);
  assert.equal(data.box[0].body.total, 1);
  assert.equal(data.box[0].body.total_source, 'current_run');
});
