import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import FL from '../lib/core.js';

const T0 = Date.UTC(2026, 9, 4, 10, 0, 0);

test('plan waits for the allowed kind, not for an idle one that is already ready', () => {
  const st = FL.fresh();
  FL.afterRequest(st, 'list', T0, () => 0.5);          // list due in ~9.5 s, bios ready after the 2 to 5 s spacing
  const left = { list: Infinity, profile: Infinity };
  const both = FL.plan(st, left, T0 + 6e3);
  assert.deepEqual(both.kinds, ['profile']);            // what used to make the worker poll for bios and oversleep
  const listOnly = FL.plan(st, left, T0 + 6e3, ['list']);
  assert.deepEqual(listOnly.kinds, []);
  assert.equal(listOnly.why, 'pace');
  assert.ok(Math.abs(listOnly.wait - (st.listNextAt - T0 - 6e3)) < 1);
  assert.deepEqual(FL.plan(st, left, T0 + 6e3, []).why, 'control');
});

function worker(data, answer) {
  const ctx = vm.createContext({FL, Date, Set, URLSearchParams, AbortController, importScripts() {}, setTimeout, clearTimeout,
    chrome: {runtime: {getManifest: () => ({version: '3.9.30'}), onMessage: {addListener() {}}}, action: {setBadgeText() {}, setBadgeBackgroundColor() {}},
      storage: {local: {get: async k => ({[k]: structuredClone(data[k])}), set: async x => Object.assign(data, structuredClone(x))}}}});
  vm.runInContext(fs.readFileSync(new URL('../background.js', import.meta.url), 'utf8').split('// ---- lifecycle')[0], ctx);
  ctx.answer = answer;
  vm.runInContext(`heartbeat = async () => { mem.offline = false; mem.stages = {list: true, profile: true}; };
    api = async () => ({status: 200, json: {paused: false, stages: {list: true, profile: true}, job: answer()}});
    globalThis.ask = (kinds) => nextJob(kinds);
    globalThis.allowed = (now) => allowedKinds(now);`, ctx);
  return ctx;
}

test('a "no job" answer is remembered briefly so the list is planned alone', async () => {
  const data = {laneId: 'x'};
  const ctx = worker(data, () => null);
  const r = await ctx.ask(['profile']);
  assert.equal(r.job, null);
  assert.equal(r.answered, true);
  assert.deepEqual([...await ctx.allowed(Date.now())], ['list']);
  assert.deepEqual([...await ctx.allowed(Date.now() + 16e3)].sort(), ['list', 'profile']);
});

test('while a leased job is held only its kind is planned', async () => {
  const data = {laneId: 'x', cur: {job: {id: 1, kind: 'list'}, at: Date.now()}};
  const ctx = worker(data, () => null);
  assert.deepEqual([...await ctx.allowed(Date.now())], ['list']);
});
