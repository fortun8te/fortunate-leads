import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';

const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const code = source.slice(source.indexOf('function parseHandles('), source.indexOf('// ---------- accounts'));

function setup(post) {
  const elements = new Map();
  const node = id => {
    if (!elements.has(id)) elements.set(id, {value: '', textContent: '', disabled: false, addEventListener() {}});
    return elements.get(id);
  };
  const directions = ['following'].map(v => ({dataset: {v}, classList: {contains: () => true}, setAttribute() {}}));
  let events = 0;
  const ctx = vm.createContext({
    $: node, $$: () => directions, S: {scStale: false},
    plural: (n, label) => `${n} ${label}${n === 1 ? '' : 's'}`,
    ucf: s => s, toast() {}, loadScraper() {}, Event,
    window: {dispatchEvent() { events++; }}, api: {post}
  });
  vm.runInContext(code, ctx);
  node('#seed-in').value = '@brand\nhttps://instagram.com/founder/';
  return {ctx, node, events: () => events};
}

test('start preserves chosen scope, confirms start and prevents duplicate submits', async () => {
  let release, calls = 0;
  const s = setup(async (path, body) => {
    calls++;
    assert.equal(path, '/api/start');
    assert.deepEqual(JSON.parse(JSON.stringify(body)), {handles: ['brand', 'founder'], directions: ['following']});
    await new Promise(resolve => { release = resolve; });
    return {queued: 2, started: true};
  });
  const first = s.ctx.submitSeed(true);
  await s.ctx.submitSeed(true);
  assert.equal(calls, 1);
  assert.equal(s.node('#seed-in').disabled, true);
  release(); await first;
  assert.equal(s.node('#seed-in').value, '');
  assert.equal(s.node('#seed-in').disabled, false);
  assert.equal(s.events(), 1);
  assert.match(s.node('#seed-feedback').textContent, /Collection is on/);
});

test('add to queue does not resume collection', async () => {
  const paths = [];
  const s = setup(async path => { paths.push(path); return {queued: 2}; });
  await s.ctx.submitSeed(false);
  assert.deepEqual(paths, ['/api/scraper/seeds']);
  assert.match(s.node('#seed-feedback').textContent, /added to the queue/);
});

test('unconfirmed or failed start keeps input and gives no success signal', async () => {
  for (const post of [async () => ({queued: 2, started: false}), async () => { throw new Error('offline'); }]) {
    const s = setup(post);
    const original = s.node('#seed-in').value;
    await s.ctx.submitSeed(true);
    assert.equal(s.node('#seed-in').value, original);
    assert.equal(s.events(), 0);
    assert.match(s.node('#seed-feedback').textContent, /[Cc]ould.*start/);
  }
});

test('stale status blocks changes and the summary counts selected lists', async () => {
  let calls = 0;
  const s = setup(async () => { calls++; });
  s.ctx.S.scStale = true;
  s.ctx.syncSeed();
  assert.equal(s.node('#seed-start').disabled, true);
  assert.equal(s.node('#seed-add').disabled, true);
  assert.equal(s.node('#seed-n').textContent, '2 profiles · 2 lists');
  await s.ctx.submitSeed(true);
  assert.equal(calls, 0);
});
