import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../../web/map-host.js', import.meta.url), 'utf8');
const app = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const queue = app.slice(app.indexOf('let mutationQueue ='), app.indexOf('// ---------- marking ----------'));
const tick = () => new Promise(resolve => setImmediate(resolve));

function mount(overrides = {}) {
  let host;
  const context = vm.createContext({
    M: { attach() {} },
    window: { MapViewModule: { mount(value) { host = value; } } },
    document: { getElementById: () => ({}) },
    STATUSES: [],
    ...overrides
  });
  vm.runInContext(queue + '\n' + source, context);
  return { host, context };
}

test('opening a map profile preserves the lead filters and navigates before updating the URL', () => {
  const calls = [];
  const filters = { q: 'existing search', tags: ['Founder'], status: 'interested' };
  const state = { view: 'map', f: filters };
  const { host } = mount({
    S: state,
    setView(view) { calls.push(['view', state.view, view]); state.view = view; },
    setURL(replace) { calls.push(['url', state.view, replace]); },
    openDetail(id) { calls.push(['detail', id]); }
  });
  host.openLead(42, 'another_handle');
  assert.equal(state.f, filters);
  assert.equal(state.f.q, 'existing search');
  assert.deepEqual(calls, [['view', 'map', 'leads'], ['url', 'leads', true], ['detail', 42]]);
});

test('map status and tag writes serialize their authoritative reads and row updates together', async () => {
  const calls = [];
  let releaseRead;
  let reads = 0;
  const firstPerson = { status: 'interested', tags: ['One'], manual_tags: ['One'] };
  const secondPerson = { status: 'client', tags: ['Client'], manual_tags: ['Client'] };
  const { host } = mount({
    api: {
      async post(path) { calls.push(path); return { status: 'stale POST value' }; },
      get(path) {
        calls.push(path);
        return ++reads === 1 ? new Promise(resolve => { releaseRead = resolve; }) : Promise.resolve(secondPerson);
      }
    },
    patchRow(id, person) { calls.push(['patch', id, person.status]); }
  });
  const status = host.setStatus(42, 'interested');
  const tags = host.editTags(42, ['Client'], []);
  await tick();
  assert.deepEqual(calls, ['/api/person/42/mark', '/api/person/42']);
  releaseRead(firstPerson);
  assert.equal(await status, firstPerson);
  assert.equal(await tags, secondPerson);
  assert.deepEqual(calls, [
    '/api/person/42/mark', '/api/person/42', ['patch', 42, 'interested'],
    '/api/person/42/tags', '/api/person/42', ['patch', 42, 'client']
  ]);
});

test('a failed map write does not prevent the next queued classification change', async () => {
  let attempts = 0;
  const { host } = mount({ api: {
    async post() { if (++attempts === 1) throw new Error('failed'); },
    async get() { return { status: null, tags: [], manual_tags: [] }; }
  } });
  const first = host.setStatus(42, 'interested');
  const second = host.editTags(42, ['Founder'], []);
  await assert.rejects(first, /failed/);
  assert.equal((await second).status, null);
  assert.equal(attempts, 2);
});
