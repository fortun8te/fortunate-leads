import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import FL from '../lib/core.js';

const source = readFileSync(new URL('../background.js', import.meta.url), 'utf8').split('// ---- lifecycle')[0];
const job = {id: 9, kind: 'profile', handle: 'target', ig_id: '77', lease_token: 'lease-a'};
function harness(data = {}) {
  const calls = [];
  const context = vm.createContext({FL, Date, Set, URLSearchParams, AbortController, setTimeout, clearTimeout,
    importScripts() {}, chrome: {
      runtime: {getManifest: () => ({version: '3.9.18'}), onMessage: {addListener() {}}},
      storage: {local: {
        get: async keys => Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map(k => [k, structuredClone(data[k])])),
        set: async values => Object.assign(data, structuredClone(values)),
      }},
      tabs: {create: async () => {calls.push('open'); throw Error('unexpected browser request');}},
    }});
  vm.runInContext(source, context);
  context.note = value => calls.push(structuredClone(value));
  vm.runInContext(`
    igRequest = async () => { note('info'); throw Error('unexpected info endpoint'); };
    api = async (path, body) => { note({path, body}); return {status: 200, json: {id: 1}}; };
    heartbeat = async () => { mem.offline = false; };
    globalThis.run = j => runProfile(mem.gen, j, {id: 1});
    globalThis.pauseStage = () => { mem.stages = {profile: false}; };
    globalThis.lookupResult = result => { lookupViaPage = async () => { note('page'); return result; }; };
  `, context);
  return {context, data, calls};
}

test('known profile ID proactively uses the normal page route and saves route metadata', async () => {
  const h = harness();
  h.context.lookupResult({p: {ig_id: '77', handle: 'target', bio: 'Complete bio'}});
  await h.context.run(job);
  assert.equal(h.calls.filter(x => x === 'page').length, 1);
  assert.equal(h.calls.includes('info'), false);
  const saved = h.calls.find(x => x?.path === '/api/ext/profile').body;
  assert.equal(saved.route, 'profile_page');
  assert.ok(Number.isFinite(Date.parse(saved.captured_at)));
  assert.ok(saved.event_id);
});

test('local and workspace bio pauses prevent the page route before navigation', async () => {
  for (const paused of ['local', 'stage', 'hold']) {
    const st = FL.fresh();
    if (paused === 'hold') st.hold = {code: 'challenge', message: 'Complete security check'};
    const h = harness({st, localPaused: paused === 'local'});
    if (paused === 'stage') h.context.pauseStage();
    await assert.rejects(h.context.run(job));
    assert.deepEqual(h.calls, []);
    if (paused === 'hold') assert.equal(h.data.st.hold.code, 'challenge');
  }
});

test('a page warning records its route and never triggers a second route', async () => {
  const h = harness();
  h.context.lookupResult({p: null, bad: {code: 'rate_limit', reason: 'please_wait_page'}, res: {status: 0}});
  await h.context.run(job);
  assert.equal(h.calls.filter(x => x === 'page').length, 1);
  assert.equal(h.calls.includes('info'), false);
  const error = h.calls.find(x => x?.path === '/api/ext/error').body;
  assert.equal(error.route, 'profile_page');
  assert.equal(error.code, 'rate_limit');
  assert.ok(h.data.st.cool.profile.until > Date.now());
});

test('a security warning wins over profile data captured before the wall appeared', async () => {
  const h = harness();
  h.context.lookupResult({p: {ig_id: '77', handle: 'target', bio: 'Captured before wall'},
    info: {url: 'https://www.instagram.com/challenge/check/'}, res: {status: 0}});
  await h.context.run(job);
  assert.equal(h.calls.some(x => x?.path === '/api/ext/profile'), false);
  assert.equal(h.data.st.hold.code, 'challenge');
  assert.equal(h.calls.find(x => x?.path === '/api/ext/error').body.route, 'profile_page');
});

test('partial or mismatched page data cannot finish a bio job', async () => {
  for (const p of [{ig_id: '77', handle: 'target'}, {ig_id: '88', handle: 'target', bio: 'Wrong identity'}]) {
    const h = harness();
    h.context.lookupResult({p});
    await h.context.run(job);
    assert.equal(h.calls.some(x => x?.path === '/api/ext/profile'), false);
    assert.equal(h.calls.find(x => x?.path === '/api/ext/error').body.route, 'profile_page');
  }
});
