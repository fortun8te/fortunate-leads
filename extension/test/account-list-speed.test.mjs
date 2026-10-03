import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import FL from '../lib/core.js';

const NOW = Date.UTC(2026, 9, 3, 19, 0);
const selectedId = '64189916997';
const plain = value => JSON.parse(JSON.stringify(value));
function harness(identity = selectedId) {
  const data = { st: { ...FL.fresh(), accountIgId: identity }, budget: { list: 0, profile: 100 } };
  class Clock extends Date { static now() { return NOW; } }
  const ctx = vm.createContext({ FL, Date: Clock, Set, URLSearchParams, AbortController,
    importScripts() {}, setTimeout, clearTimeout,
    chrome: { runtime: { getManifest: () => ({ version: '3.9.31' }), onMessage: { addListener() {} } },
      storage: { local: { get: async key => ({ [key]: data[key] }), set: async value => Object.assign(data, value) } } } });
  const source = fs.readFileSync(new URL('../background.js', import.meta.url), 'utf8').split('// ---- lifecycle')[0];
  vm.runInContext(source, ctx);
  vm.runInContext(`globalThis.check = async (kinds=['profile'], control={}) => {
    heartbeat = async () => {
      mem.offline=!!control.offline; mem.serverPaused=!!control.paused;
      mem.noTab=control.tab || null;
      mem.stages=control.stages || {list:true,profile:false};
    };
    api = async () => { throw new Error('unexpected collector lease request'); };
    return nextJob(kinds);
  };`, ctx);
  return { data, ctx, check: async (...args) => plain(await ctx.check(...args)) };
}

test('only strict selected identity shortens the paused-bio poll; core gaps stay original', async () => {
  const selected = harness();
  assert.deepEqual(await selected.check(), { job: null, wait: 11650 });
  for (const identity of ['55966675376', '25764895364', undefined, null, 64189916997, '64189916997 ', 'other']) {
    const h=harness(); h.data.st.accountIgId=identity;
    assert.deepEqual(await h.check(), { job: null, wait: 15000 });
  }
  assert.deepEqual(await selected.check([]), {job:null,wait:15000});
  for (const identity of [selectedId, '55966675376']) {
    const st = { ...FL.fresh(), accountIgId: identity, breakEvery: 50 };
    FL.afterRequest(st, 'list', NOW, () => 0.5);
    assert.equal(st.listNextAt - NOW, 9500);
    assert.equal(st.nextAt - NOW, 3500);
  }
});

test('real plan reaches the filtered-profile branch while lists remain unready', async () => {
  const h = harness();
  h.data.st.listNextAt = NOW + 12000;
  const plan = FL.plan(h.data.st, FL.budgetLeft(h.data.st, h.data.budget, NOW), NOW);
  assert.deepEqual(plan.kinds, ['profile']);
  assert.deepEqual(await h.check(plan.kinds), { job: null, wait: 11650 });
  assert.equal(h.data.st.listNextAt, NOW + 12000);
  assert.equal(FL.plan(h.data.st, { list: Infinity, profile: 0 }, NOW).why, 'pace');
});

test('workspace, stage, offline and local protective holds retain the ordinary poll', async () => {
  for (const control of [{ offline:true }, { paused:true }, { stages:{list:false,profile:false} },
                         { tab:'tab_login' }, { tab:'tab_challenge' }, { tab:'tab_scraping_warning' }]) {
    assert.deepEqual(await harness().check(['profile'], control), { job:null, wait:15000 });
  }
  for (const mutation of [h => { h.data.localPaused=true; },
                          h => { h.data.st.hold={code:'scraping_warning'}; },
                          h => { h.data.st.cool.list.until=NOW+FL.HOUR; },
                          h => { h.data.st.rlog=Array.from({length:72},()=>[NOW-1000,'list']); },
                          h => { h.data.budget.list=1; h.data.st.day=FL.dayKey(NOW); h.data.st.today.list=1; }]) {
    const h=harness(); mutation(h);
    assert.deepEqual(await h.check(), { job:null, wait:15000 });
  }
});

test('enabled bios and cached leases keep their existing priority and readiness', async () => {
  const h=harness();
  h.data.cur={job:{id:9,kind:'profile',lease_token:'existing'},at:NOW};
  assert.deepEqual(await h.check(['profile'], { stages:{list:true,profile:true} }), {job:h.data.cur.job});
  assert.deepEqual(await h.check(['list'], { stages:{list:true,profile:true} }), {job:null,wait:15000});
  assert.deepEqual(await h.check(['profile']), {job:null,wait:11650});
  assert.equal(h.data.cur.job.lease_token,'existing');
});

test('polling adjustment preserves stored waits, breaks, bios, window and backoffs', async () => {
  const h=harness();
  Object.assign(h.data.st,{nextAt:NOW+90000,listNextAt:NOW+180000,profileNextAt:NOW+240000,
    day:FL.dayKey(NOW),pages:49,breakEvery:50});
  const before=structuredClone(h.data);
  await h.check();
  assert.deepEqual(h.data,before);
  FL.afterRequest(h.data.st,'list',NOW,()=>0.5);
  assert.equal(h.data.st.listNextAt-NOW,9500+135000);
  assert.equal(h.data.st.nextAt,NOW+90000);
  const profile={...FL.fresh(),accountIgId:selectedId};
  FL.afterRequest(profile,'profile',NOW,()=>0.5);
  assert.equal(profile.profileNextAt-NOW,52500);
  assert.equal(FL.PACE.window,11*FL.MIN); assert.equal(FL.PACE.windowMax,72);
  FL.applyHit(profile,NOW,NOW+FL.HOUR,'list');
  FL.backoff(profile,NOW,'net');
  assert.equal(FL.plan(profile,{list:Infinity,profile:0},NOW+10000).why,'cooldown');
});
