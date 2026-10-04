// lib/log.js: bounded structured events for the server log, and the background.js call sites that feed it.
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import FL from '../lib/core.js';
import FLLog from '../lib/log.js';

const reset = () => { FLLog.reset(); delete globalThis.chrome; };

test('sanitize keeps short scalar fields and drops anything that could carry a credential or content', () => {
  const out = FLLog.sanitize({
    status: 429, ms: 812.34567, code: 'rate_limit', ok: true,
    cookie: 'sessionid=abc', sessionid: 'abc', csrf_token: 'x', authorization: 'Bearer y', text: 'hello', body: '{}', message: 'Please wait',
    sample: '<html>', bio: 'my bio', note: 'private', nested: { a: 1 }, list: [1], nothing: null, skipped: undefined,
    path: '/api/v1/friendships/55/following/?count=50&max_id=QVFABCDEF', err: 'failed sessionid=999999 csrftoken=zzzzzz',
    long: 'x'.repeat(500),
  });
  assert.deepEqual(Object.keys(out).sort(), ['code', 'err', 'long', 'ms', 'ok', 'path', 'status']);
  assert.equal(out.ms, 812.346);
  assert.equal(out.path, '/api/v1/friendships/55/following/');
  assert.ok(!/999999|zzzzzz/.test(out.err), out.err);
  assert.equal(out.long.length, 120);
});

test('sanitize accepts at most twelve fields and only snake_case names', () => {
  const many = Object.fromEntries(Array.from({ length: 30 }, (_, i) => ['k' + i, i]));
  assert.equal(Object.keys(FLLog.sanitize(many)).length, 12);
  assert.deepEqual(Object.keys(FLLog.sanitize({ 'Bad-Name': 1, ok_name: 2, '9x': 3 })), ['ok_name']);
});

test('the ring is bounded and counts what it had to discard', () => {
  reset();
  for (let i = 0; i < FLLog.MAX + 25; i++) FLLog.event('info', 'tick', { i });
  const { events, dropped } = FLLog.peek();
  assert.equal(events.length, FLLog.MAX);
  assert.equal(dropped, 25);
  assert.equal(events[0].i, 25);
});

test('event never throws and ignores invalid event names', () => {
  reset();
  assert.doesNotThrow(() => FLLog.event('info', 'Bad Name!', { a: 1 }));
  assert.doesNotThrow(() => FLLog.event('nonsense', 'fine', { a: {} }));
  assert.deepEqual(FLLog.peek().events.map((e) => [e.ev, e.lv]), [['fine', 'info']]);
});

test('warnings and errors ask for an early flush, routine events do not', () => {
  reset();
  let asked = 0;
  FLLog.onUrgent = () => { asked++; };
  FLLog.event('info', 'a');
  FLLog.event('warn', 'b');
  FLLog.event('error', 'c');
  assert.equal(asked, 2);
  FLLog.onUrgent = null;
});

test('every: a noisy event is limited per key', () => {
  reset();
  for (let i = 0; i < 5; i++) FLLog.event('info', 'permit_denied', { i }, { every: 60e3, key: 'a' });
  FLLog.event('info', 'permit_denied', { i: 9 }, { every: 60e3, key: 'b' });
  assert.deepEqual(FLLog.peek().events.map((e) => e.i), [0, 9]);
});

test('flush sends a bounded batch and removes only what the server accepted', async () => {
  reset();
  for (let i = 0; i < FLLog.BATCH + 40; i++) FLLog.event('info', 'tick', { i });
  const posted = [];
  let result = { status: 500, json: null };
  const post = async (body) => { posted.push(body); if (posted.length === 2) FLLog.event('info', 'late', {}); return result; };
  assert.equal(await FLLog.flush(post, { version: '3.9.31', force: true }), 0);   // server error: keep everything
  assert.equal(FLLog.peek().events.length, FLLog.BATCH + 40);
  result = { status: 200, json: { accepted: FLLog.BATCH } };
  assert.equal(await FLLog.flush(post, { version: '3.9.31', force: true }), FLLog.BATCH);
  assert.equal(posted[1].events.length, FLLog.BATCH);
  assert.equal(posted[1].v, '3.9.31');
  const left = FLLog.peek().events;
  assert.deepEqual(left.slice(0, 2).map((e) => e.i), [FLLog.BATCH, FLLog.BATCH + 1]);   // 40 unsent + 'late'
  assert.equal(left.at(-1).ev, 'late');
});

test('flush survives a throwing transport and respects the minimum interval', async () => {
  reset();
  FLLog.event('info', 'a', {});
  assert.equal(await FLLog.flush(async () => { throw new Error('offline'); }, { force: true }), 0);
  assert.equal(FLLog.peek().events.length, 1);
  let calls = 0;
  const post = async () => { calls++; return { status: 200, json: { accepted: 1 } }; };
  assert.equal(await FLLog.flush(post, { force: true }), 1);
  FLLog.event('info', 'b', {});
  assert.equal(await FLLog.flush(post), 0);   // within 20 s of the last flush
  assert.equal(calls, 1);
});

test('events survive a stopped service worker through chrome.storage.local', async () => {
  reset();
  const data = {};
  globalThis.chrome = { storage: { local: { get: async (k) => ({ [k]: data[k] }), set: async (o) => Object.assign(data, o) } } };
  FLLog.event('warn', 'server_offline', { n: 1 });      // warnings persist immediately
  await new Promise((r) => setTimeout(r, 5));
  assert.equal(data.elog.events[0].ev, 'server_offline');
  FLLog.reset();                                          // the worker was stopped and restarted
  await FLLog.load();
  FLLog.event('info', 'sw_start', { n: 2 });
  assert.deepEqual(FLLog.peek().events.map((e) => e.ev), ['server_offline', 'sw_start']);
  delete globalThis.chrome;
});

// ---- the call sites in background.js ------------------------------------------------------------------------
function harness() {
  const data = {};
  const context = vm.createContext({ FL, FLLog, Date, Set, URLSearchParams, AbortController, importScripts() {}, setTimeout, clearTimeout,
    chrome: { runtime: { getManifest: () => ({ version: '3.9.31' }), onMessage: { addListener() {} } },
      storage: { local: { get: async (k) => ({ [k]: data[k] }), set: async (x) => Object.assign(data, x) } } } });
  const source = fs.readFileSync(new URL('../background.js', import.meta.url), 'utf8').split('// ---- lifecycle')[0];
  vm.runInContext(source, context);
  return context;
}

test('an Instagram request leaves one status-only event and no response content', async () => {
  reset();
  const ctx = harness();
  vm.runInContext(`
    assertControl = async () => {}; acquireSharedRequest = async () => 'permit'; releaseSharedRequest = async () => {};
    inTab = async () => ({ status: 429, text: '{"message":"Please wait a few minutes","sessionid":"SECRETVALUE"}', sent: true, ms: 812,
      json: { message: 'Please wait a few minutes', status: 'fail' }, retryAfter: null });
    globalThis.go = () => igRequest(mem.gen, { id: 1 }, 'https://www.instagram.com/api/v1/friendships/55/following/?count=50&max_id=QVFSECRETCURSOR', 'list', {});
  `, ctx);
  const { bad } = await ctx.go();
  assert.ok(bad, 'the 429 is classified');
  const sent = FLLog.peek().events.find((e) => e.ev === 'ig.req');
  assert.equal(sent.status, 429);
  assert.equal(sent.ms, 812);
  assert.equal(sent.kind, 'list');
  assert.equal(sent.lv, 'warn');
  assert.equal(sent.path, '/api/v1/friendships/55/following/');
  const text = JSON.stringify(FLLog.peek().events);
  assert.ok(!/SECRETVALUE|SECRETCURSOR|Please wait/.test(text), text);
});

test('server going away and coming back is logged once each, not per attempt', () => {
  reset();
  const ctx = harness();
  vm.runInContext(`mem.offline = true; noteConnection(); noteConnection(); mem.offline = false; noteConnection();`, ctx);
  assert.deepEqual(FLLog.peek().events.map((e) => e.ev), ['server_offline', 'server_online']);
});

test('the worker still runs when the logger is not loaded', async () => {
  const context = vm.createContext({ FL, Date, Set, URLSearchParams, AbortController, importScripts() {}, setTimeout, clearTimeout,
    chrome: { runtime: { getManifest: () => ({ version: '3.9.31' }), onMessage: { addListener() {} } }, storage: { local: { get: async () => ({}), set: async () => {} } } } });
  const source = fs.readFileSync(new URL('../background.js', import.meta.url), 'utf8').split('// ---- lifecycle')[0];
  vm.runInContext(source, context);
  vm.runInContext(`mem.offline = true; noteConnection();`, context);
  assert.equal(await vm.runInContext('flushLog()', context), 0);
});
