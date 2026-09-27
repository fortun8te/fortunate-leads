import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import FL from '../lib/core.js';

const source = fs.readFileSync(new URL('../background.js', import.meta.url), 'utf8').split('// ---- lifecycle')[0];

function harness(account, st, clock = { now: Date.now() }) {
  const data = { laneId: 'lane-1', account: { ig_id: account, handle: 'old', at: 0 }, st };
  let observed = account;
  const beats = [];
  const TestDate = class extends Date { static now() { return clock.now; } };
  const ctx = vm.createContext({ FL, Date: TestDate, Set, URLSearchParams, AbortController, setTimeout, clearTimeout,
    importScripts() {},
    chrome: {
      runtime: { getManifest: () => ({ version: '3.9.16' }), onMessage: { addListener() {} } },
      tabs: { query: async () => [{ id: 1, status: 'complete', discarded: false, url: 'https://www.instagram.com/' }] },
      scripting: { executeScript: async () => [{ result: { host: 'www.instagram.com', ready: 'complete',
        cookie: observed ? `ds_user_id=${observed}` : '',
        text: observed ? `{"id":"${observed}","username":"viewer"}` : '' } }] },
      action: { setBadgeText() {}, setBadgeBackgroundColor() {} },
      storage: { local: {
        get: async keys => Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map(k => [k, structuredClone(data[k])])),
        set: async values => Object.assign(data, structuredClone(values)),
      } },
    },
  });
  vm.runInContext(source, ctx);
  ctx.apiStub = async (path, body) => { if (path === '/api/ext/heartbeat') beats.push(structuredClone(body));
    if (path === '/api/ext/request') return { status: 200, json: body.action === 'release' ? { released: true } : { granted: true, token: 'simulated-request-token', expires_at: new Date(clock.now + 90000).toISOString() } };
    return { status: 200, json: { paused: false } }; };
  vm.runInContext('selfUpdate = async () => false; api = async (path, body) => apiStub(path, await tagged(body)); globalThis.beat = () => heartbeat(true)', ctx);
  return { data, beats, ctx, observe: id => { observed = id; } };
}

function limitedState(now) {
  const st = FL.fresh();
  st.day = FL.dayKey(now);
  st.today = { list: 8, profile: 5, people: 90, bios: 4 };
  st.cool.list = { until: now + FL.HOUR, hits: [now], retryUntil: 0 };
  st.cool.profile = { until: now + FL.HOUR, hits: [now], retryUntil: 0 };
  st.listEndpointUntil = now + FL.HOUR;
  st.rlog = [[now, 'list']];
  return st;
}

test('a different Instagram id gets fresh counters and cooldowns in the next heartbeat and after restart', async () => {
  const now = Date.now();
  const h = harness('111', limitedState(now));
  h.observe('222');
  await h.ctx.beat();
  assert.equal(h.data.account.ig_id, '222');
  assert.equal(h.beats[0].account.ig_id, '222');
  assert.deepEqual(h.beats[0].today, { list: 0, profile: 0 });
  assert.deepEqual(h.beats[0].cool, { list: null, profile: null });
  assert.equal(h.beats[0].cooldown_until, null);
  assert.equal(h.beats[0].list_endpoint_until, null);
  assert.equal(h.data.st.accountIgId, '222');
  const resumed = harness('222', h.data.st);
  await resumed.ctx.beat();
  assert.deepEqual(resumed.beats[0].today, { list: 0, profile: 0 });
  assert.deepEqual(resumed.beats[0].cool, { list: null, profile: null });
});

test('logout preserves account ownership and returning to the same id keeps its limits', async () => {
  const now = Date.now();
  const h = harness('111', limitedState(now));
  await h.ctx.beat();
  h.observe(null);
  await h.ctx.beat();
  assert.equal(h.data.st.accountIgId, '111');
  h.observe('111');
  await h.ctx.beat();
  assert.equal(h.beats.at(-1).today.list, 8);
  assert.ok(h.beats.at(-1).cool.list);
});

test('a new id after logout does not inherit the prior account limits', async () => {
  const h = harness('111', limitedState(Date.now()));
  await h.ctx.beat();
  h.observe(null);
  await h.ctx.beat();
  h.observe('222');
  await h.ctx.beat();
  assert.deepEqual(h.beats.at(-1).today, { list: 0, profile: 0 });
  assert.deepEqual(h.beats.at(-1).cool, { list: null, profile: null });
});

test('A to B to A restores A limits without replaying them onto B', async () => {
  const h = harness('111', limitedState(Date.now()));
  h.observe('222');
  await h.ctx.beat();
  assert.equal(h.beats.at(-1).today.list, 0);
  assert.equal(h.data.accountStates['111'].st.today.list, 8);
  h.data.st.day = FL.dayKey(Date.now());
  h.data.st.today.list = 2;
  h.observe('111');
  await h.ctx.beat();
  assert.equal(h.beats.at(-1).account.ig_id, '111');
  assert.equal(h.beats.at(-1).today.list, 8);
  assert.ok(h.beats.at(-1).cool.list);
  assert.equal(h.data.accountStates['222'].st.today.list, 2);
  h.observe('222');
  await h.ctx.beat();
  assert.equal(h.beats.at(-1).today.list, 2);
  assert.equal(h.beats.at(-1).cool.list, null);
});

test('heartbeat sends the local day and restores old account state with a new day after midnight', async () => {
  const before = new Date(2026, 8, 27, 23, 59, 50).getTime();
  const after = new Date(2026, 8, 28, 0, 0, 10).getTime();
  const clock = { now: before };
  const h = harness('111', limitedState(before), clock);
  h.observe('222');
  await h.ctx.beat();
  assert.equal(h.beats.at(-1).day, FL.dayKey(before));
  clock.now = after;
  h.observe('111');
  await h.ctx.beat();
  assert.equal(h.beats.at(-1).day, FL.dayKey(after));
  assert.equal(h.data.st.day, FL.dayKey(after));
  assert.deepEqual(h.beats.at(-1).today, { list: 0, profile: 0 });
  assert.ok(h.beats.at(-1).cool.list);
});

test('an old-account request finishing after a switch cannot refill the new counters', async () => {
  const h = harness('111', limitedState(Date.now()));
  let started, finish;
  const entered = new Promise(resolve => { started = resolve; });
  h.ctx.pageStub = () => new Promise(resolve => { finish = resolve; started(); });
  vm.runInContext("inTab = (...args) => pageStub(...args); globalThis.request = () => igRequest(mem.gen, {id: 1}, 'https://www.instagram.com/api/', 'list')", h.ctx);
  const oldRequest = h.ctx.request();
  await entered;
  h.observe('222');
  await h.ctx.beat();
  finish({ sent: true, status: 200, text: '{}', ms: 1 });
  await assert.rejects(oldRequest, { name: 'Error' });
  assert.equal(h.data.st.accountIgId, '222');
  assert.equal(h.data.st.today.list, 0);
  assert.equal(h.data.st.rlog.length, 0);
});
