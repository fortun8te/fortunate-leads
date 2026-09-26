import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import FL from '../lib/core.js';

function harness() {
  const data = {cur: {job: {id: 9, kind: 'list', lease_token: 'lease-a'}, at: Date.now()}};
  const context = vm.createContext({FL, Date, Set, URLSearchParams, AbortController,
    importScripts() {}, setTimeout, clearTimeout,
    chrome: {runtime: {getManifest: () => ({version: '3.8.0'}), onMessage: {addListener() {}}},
      storage: {local: {get: async k => ({[k]: data[k]}), set: async x => Object.assign(data, x)}}}});
  const source = fs.readFileSync(new URL('../background.js', import.meta.url), 'utf8').split('// ---- lifecycle')[0];
  vm.runInContext(source, context);
  vm.runInContext(`globalThis.check = async (state) => {
    heartbeat = async () => { mem.offline = !!state.offline; await applyServer(state); };
    return nextJob(['list','profile']);
  };
  globalThis.request = async () => {
    try { await igRequest(0, {id: 1}, 'unused', 'list'); return false; }
    catch (e) { return e instanceof ControlPaused; }
  };
  globalThis.badLeaseResponse = async () => {
    api = async () => ({status: 503, json: {}});
    heartbeat = async () => { mem.offline = false; mem.serverPaused = false; };
    const result = await nextJob(['list']);
    return {result, offline: mem.offline};
  };
  globalThis.rejectedResult = async () => {
    api = async () => ({status: 400, json: {error: 'bad page'}});
    await set({box: [{path: '/api/ext/list-page', body: {job_id: 9}}]});
    await flushBox();
    return {box: await get('box'), dead: await get('dead'), st: await loadSt()};
  };
  globalThis.pausedFlush = async () => {
    const posted = [];
    api = async (path, body) => { posted.push({path, body}); return {status: 200, json: {received: 1, id: 1}}; };
    await set({st: {...FL.fresh(), hold: {code: 'login', message: 'Log in'}}, localPaused: true,
      box: [{path: '/api/ext/list-page', body: {job_id: 9, lease_token: 'lease-a'}}]});
    await step(mem.gen);
    return {posted, box: await get('box')};
  };
  globalThis.passiveGate = async () => {
    const posted = [];
    const user = {pk: 77, username: 'target', biography: 'Saved bio'};
    heartbeat = async () => { mem.offline = false; mem.stages = {profile: false}; };
    api = async (path, body) => { posted.push({path, body}); return {status: 200, json: {received: 1, id: 1}}; };
    await passive(user);
    const paused = {posted: posted.length, seen: await get('seen'), count: (await loadSt()).today.bios};
    heartbeat = async () => { mem.stages = {profile: true}; };
    await passive(user);
    return {paused, posted, count: (await loadSt()).today.bios, seen: await get('seen')};
  };
  globalThis.stalledList = async () => {
    api = async () => ({status: 503, json: {}});
    igRequest = async () => ({res: {json: {users: [{pk: 71, username: 'saved'}], next_max_id: 'same', has_more: true, status: 'ok'}}, bad: null});
    await set({ids: {seed: {ig_id: '12', followers: 100}}, cur: {job: {id: 9}, at: Date.now()}});
    await runList(mem.gen, {id: 9, kind: 'list', seed: 'seed', direction: 'followers', cursor: 'same', lease_token: 'lease-a', received: 25}, {id: 1});
    return {box: await get('box'), cur: await get('cur'), note: (await loadSt()).note};
  };
  globalThis.outboxRace = async () => {
    let release, started;
    const began = new Promise((r) => { started = r; });
    const first = new Promise((r) => { release = r; });
    const posted = [];
    api = async (path, body) => {
      posted.push(body.job_id);
      if (body.job_id === 1) { started(); await first; }
      return {status: 200, json: {received: 1, id: 1}};
    };
    await set({box: [{qid: 'old', path: '/api/ext/list-page', body: {job_id: 1}}]});
    const draining = flushBox();
    await began;
    await set({box: [{qid: 'new', path: '/api/ext/list-page', body: {job_id: 2}}]});
    release();
    await draining;
    return {posted, box: await get('box')};
  };`, context);
  return {context, data};
}

test('cached lease observes workspace and stage pause before making a request', async () => {
  const {context, data} = harness();
  assert.equal((await context.check({paused: true})).job, null);
  assert.equal(data.cur.job.id, 9);
  assert.equal(await context.request(), true);
  assert.equal((await context.check({paused: false, stages: {list: false, profile: true}})).job, null);
  assert.equal(await context.request(), true);
  assert.equal((await context.check({paused: false, stages: {list: true, profile: true}})).job.id, 9);
});

test('a failed lease response is treated as offline, not an empty queue', async () => {
  const {context, data} = harness();
  data.cur = null;
  const {result, offline} = await context.badLeaseResponse();
  assert.equal(result.job, null);
  assert.equal(offline, true);
});

test('a rejected saved result is identified for the dead-letter queue', async () => {
  const {context} = harness();
  const {box, dead, st} = await context.rejectedResult();
  assert.equal(box.length, 0);
  assert.equal(dead.length, 1);
  assert.equal(dead[0].reason, 'reject');
  assert.match(st.lastError, /rejected a saved result/);
});

test('cached lease waits when latest control check is offline', async () => {
  const {context, data} = harness();
  assert.equal((await context.check({offline: true})).job, null);
  assert.equal(await context.request(), true);
  assert.equal(data.cur.job.id, 9);
});

test('saved outbox flushes during both local pause and account hold', async () => {
  const {context} = harness();
  const {posted, box} = await context.pausedFlush();
  assert.equal(posted.length, 1);
  assert.equal(posted[0].path, '/api/ext/list-page');
  assert.equal(box.length, 0);
});

test('passive bio observes the bio stage pause without marking it as delivered', async () => {
  const {context} = harness();
  const {paused, posted, count, seen} = await context.passiveGate();
  assert.equal(paused.posted, 0);
  assert.equal(paused.count, 0);
  assert.equal(paused.seen, undefined);
  assert.equal(posted.length, 1);
  assert.equal(posted[0].path, '/api/ext/profile');
  assert.equal(count, 1);
  assert.ok(seen.target);
});

test('repeated cursor saves its users in the outbox and records partial coverage', async () => {
  const {context} = harness();
  const {box, cur, note} = await context.stalledList();
  assert.equal(cur, null);
  assert.equal(box.length, 1);
  assert.equal(box[0].path, '/api/ext/list-page');
  assert.equal(box[0].body.requested_cursor, 'same');
  assert.equal(box[0].body.next_cursor, 'same');
  assert.equal(box[0].body.users[0].handle, 'saved');
  assert.match(note, /list is partial/);
});

test('outbox does not remove a replacement head after an in-flight send', async () => {
  const {context} = harness();
  const {posted, box} = await context.outboxRace();
  assert.deepEqual(Array.from(posted), [1, 2]);
  assert.equal(box.length, 0);
});
