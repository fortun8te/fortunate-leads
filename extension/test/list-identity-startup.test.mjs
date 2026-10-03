import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import FL from '../lib/core.js';

const source = fs.readFileSync(new URL('../background.js', import.meta.url), 'utf8').split('// ---- lifecycle')[0];
const identity = (overrides = {}) => ({ig_id: '12', total: 100, total_source: 'cached', run_id: 9, first_page_seen: false, ...overrides});
const job = (overrides = {}) => ({id: 9, kind: 'list', seed: 'seed', direction: 'following', ig_id: '12', cursor: null,
  received: 0, lease_token: 'lease-a', list_identity: identity(), ...overrides});
function harness({data = {}, response = {users: [{pk: 13, username: 'person'}], has_more: false}, realGate = false, denied = false} = {}) {
  const calls = [];
  const ctx = vm.createContext({FL, Date, Set, URLSearchParams, AbortController, setTimeout, clearTimeout, importScripts() {},
    chrome: {runtime: {getManifest: () => ({version: '3.9.31'}), onMessage: {addListener() {}}}, storage: {local: {
      get: async keys => Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map(k => [k, structuredClone(data[k])])),
      set: async values => Object.assign(data, structuredClone(values)),
    }}}});
  vm.runInContext(source, ctx);
  Object.assign(ctx, {note: value => calls.push(structuredClone(value)), fixtureResponse: response, denied});
  vm.runInContext(`
    lookupViaPage = async () => { note('lookup'); return {p: {ig_id: '12', handle: 'seed', following: 200, followers: 200}}; };
    waitUntil = async () => true;
    queueDone = async (path, body) => note({path, body});
    heartbeat = async () => { mem.offline = false; };
    api = async (path, body) => { note({path, body}); return {status: 200, json: denied ? {granted: false, wait_ms: 1000} :
      {granted: true, token: 'permit', expires_at: new Date(Date.now() + 90000).toISOString()}}; };
    inTab = async (id, url) => { note({browser: url}); return {status: 200, sent: true, json: fixtureResponse}; };
    globalThis.run = j => { mem.job = j; return runList(mem.gen, j, {id: 1}); };
  `, ctx);
  if (!realGate) vm.runInContext(`igRequest = async (gen, tab, url, kind, context) => {
    note({url, kind, context}); const res = {status: 200, json: fixtureResponse};
    return {res, bad: FL.classify(res, kind, Date.now(), context)};
  };`, ctx);
  return {ctx, calls, data, result: () => calls.find(c => c.path === '/api/ext/list-page')?.body};
}

test('known fresh identity skips profile navigation and retains cached provenance and exact request', async () => {
  const h = harness(); await h.ctx.run(job());
  assert.equal(h.calls.includes('lookup'), false);
  assert.equal(h.calls.find(c => c.url).url, 'https://www.instagram.com/api/v1/friendships/12/following/?count=200');
  assert.equal(h.result().total, 100); assert.equal(h.result().total_source, 'cached');
  assert.equal(h.result().lease_token, 'lease-a'); assert.equal(h.result().requested_cursor, null);
  assert.equal(h.data.prog['seed/following'].countAttempted, true);
});

test('cleared progress resumes server checkpoint and preserves opaque cursor and same-run count', async () => {
  const h = harness(); const cursor = 'opaque/+?=&';
  await h.ctx.run(job({cursor, received: 50, list_identity: identity({total: 300, total_source: 'current_run', first_page_seen: true})}));
  assert.equal(h.calls.includes('lookup'), false);
  assert.equal(h.calls.find(c => c.url).url, 'https://www.instagram.com/api/v1/friendships/12/following/?count=200&max_id=' + encodeURIComponent(cursor));
  assert.equal(h.calls.find(c => c.context).context.received, 50);
  assert.equal(h.result().total_source, 'current_run'); assert.equal(h.result().total, 300);
  assert.equal(h.result().requested_cursor, cursor); assert.equal(h.result().job_id, 9);
});

test('fresh run cannot upgrade cached counts into current-run proof', async () => {
  const h = harness(); await h.ctx.run(job({list_identity: identity({total_source: 'current_run'})}));
  assert.equal(h.result().total_source, 'cached');
  const unknown = harness(); await unknown.ctx.run(job({list_identity: identity({total: null, total_source: 'unknown'})}));
  assert.equal(unknown.result().total_source, 'unknown'); assert.equal(unknown.result().total, null);
  assert.equal(unknown.calls.includes('lookup'), false);
});

test('cached totals do not trigger current-run short-terminal retry', async () => {
  const h = harness({response: {users: [], has_more: false}});
  await h.ctx.run(job({cursor: 'tail', received: 5}));
  assert.equal(h.result().done, true); assert.equal(h.result().total_source, 'cached');
  // Server owns the partial/complete verdict; extension reports provenance unchanged.
  assert.equal(h.result().total, 100);
});

test('missing, mismatched and malformed metadata keep existing first lookup', async () => {
  for (const metadata of [undefined, null, identity({run_id: 8}), identity({ig_id: '13'}), identity({ig_id: '12/evil'}),
    identity({total_source: 'stale'}), identity({total: -1}), identity({total: 1.5}), identity({first_page_seen: 'true'})]) {
    const h = harness(); await h.ctx.run(job({list_identity: metadata}));
    assert.equal(h.calls.filter(c => c === 'lookup').length, 1);
    assert.equal(h.result().total, 200); assert.equal(h.result().total_source, 'current_run');
  }
});

test('pinned experiment retains lookup and requested page-size control', async () => {
  const h = harness({data: {account: {ig_id: '55'}}});
  await h.ctx.run(job({page_size: 50, experiment_viewer_ig_id: '55'}));
  assert.equal(h.calls.filter(c => c === 'lookup').length, 1);
  assert.match(h.calls.find(c => c.url).url, /count=50$/);
});

test('list response count becomes current-run evidence without a profile lookup', async () => {
  const h = harness({response: {data: {user: {edge_follow: {count: 1, edges: [{node: {pk: 13, username: 'person'}}], page_info: {has_next_page: false}}}}}});
  await h.ctx.run(job()); assert.equal(h.result().total_source, 'current_run'); assert.equal(h.result().total, 1);
});

test('unchanged request gate denies locally paused and server-held known-identity work', async () => {
  for (const kind of ['local', 'server']) {
    const st = FL.fresh(); st.hold = {code: 'challenge', message: 'Keep hold'};
    const h = harness({realGate: true, denied: kind === 'server', data: {st, localPaused: kind === 'local'}});
    await assert.rejects(h.ctx.run(job()));
    assert.equal(h.calls.includes('lookup'), false); assert.equal(h.calls.some(c => c.browser), false);
    assert.deepEqual(h.data.st.hold, st.hold);
    assert.equal(h.result(), undefined);
  }
});

test('successful known identity still acquires and releases permit and advances normal pacing', async () => {
  const h = harness({realGate: true}); await h.ctx.run(job());
  assert.deepEqual(h.calls.filter(c => c.path === '/api/ext/request').map(c => c.body.action), ['acquire', 'release']);
  assert.equal(h.calls.filter(c => c.browser).length, 1);
  assert.ok(h.data.st.listNextAt > Date.now());
  assert.equal(h.result().requested_count, 200);
});

test('same-run local count survives cached metadata but cannot cross run boundaries', async () => {
  const data = {prog: {'seed/following': {jobId: 9, next: 'tail', total: 8, totalSource: 'current_run', countAttempted: true}}};
  const h = harness({data}); await h.ctx.run(job({cursor: 'tail', received: 7}));
  assert.equal(h.result().total, 8); assert.equal(h.result().total_source, 'current_run');
  const next = harness({data}); await next.ctx.run(job({id: 10, list_identity: identity({run_id: 10})}));
  assert.equal(next.result().total, 100); assert.equal(next.result().total_source, 'cached');
});

test('known identity empty-page warning still holds list work without a fallback lookup', async () => {
  const h = harness({response: {users: [], has_more: true}});
  await h.ctx.run(job());
  assert.equal(h.calls.includes('lookup'), false);
  assert.equal(h.result(), undefined);
  assert.ok(h.data.st.cool.list.until > Date.now());
  assert.equal(h.calls.find(c => c.path === '/api/ext/error').body.reason, 'empty_page_with_more');
});
