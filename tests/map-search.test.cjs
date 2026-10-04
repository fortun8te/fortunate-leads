const test = require('node:test');
const assert = require('node:assert/strict');
const S = require('../web/map-search.js');
const { MapModel } = require('../web/map-model.js');
const { doc, FakeElement, manualTimers } = require('./fake-dom.cjs');

test('highlight ignores case and accents and marks the typed words', () => {
  const marked = (t, q) => S.highlight(t, q).filter(s => s.hit).map(s => s.text);
  assert.deepEqual(marked('André Elmenhorst', 'andre elm'), ['André', 'Elm']);
  assert.deepEqual(marked('nothing here', 'zzz'), []);
  assert.deepEqual(marked('CeraVe', 'cera'), ['Cera']);
});

function fixture(results) {
  const input = new FakeElement('input'), list = new FakeElement('div'), timers = manualTimers();
  const calls = [], chosen = [];
  const model = { searchReq: { cancel() {} }, fetchJson: async () => ({}), searchPeople: async (q) => { calls.push(q); return results(q); }, goTo: (n) => chosen.push(n.id) };
  const store = new Map();
  const search = new S.MapSearch({ input, list, model, doc, timers, storage: { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) } });
  search.bind();
  return { input, list, search, calls, chosen, timers, store };
}
const row = (id, extra = {}) => ({ id, handle: 'h' + id, name: 'Name ' + id, followers: 10, pic: '/img/' + id, ...extra });
const tick = () => new Promise(r => setImmediate(r));

test('typing waits for a pause, searches once, and keyboard picks a result', async () => {
  const f = fixture(() => [row(1), row(2), row(3)]);
  f.input.value = 'h'; f.input.dispatch('input'); f.input.value = 'ha'; f.input.dispatch('input');
  assert.equal(f.calls.length, 0);
  f.timers.flush(); await tick();
  assert.deepEqual(f.calls, ['ha']);
  assert.equal(f.list.children.filter(c => c.attrs.role === 'option').length, 3);
  f.input.dispatch('keydown', { key: 'ArrowUp' });
  assert.equal(f.search.active, 2, 'wraps');
  f.input.dispatch('keydown', { key: 'ArrowDown' });
  f.input.dispatch('keydown', { key: 'ArrowDown' });
  f.input.dispatch('keydown', { key: 'Enter' });
  assert.deepEqual(f.chosen, [2]);
  assert.equal(f.input.value, '');
});

test('empty answers, errors and escape', async () => {
  const f = fixture(() => []);
  f.input.value = 'zz'; f.input.dispatch('input'); f.timers.flush(); await tick();
  assert.match(f.list.textContent, /No one matches “zz”/);
  const g = fixture(() => { throw new Error('x'); });
  g.input.value = 'zz'; g.input.dispatch('input'); g.timers.flush(); await tick();
  assert.match(g.list.textContent, /Search isn’t available/);
  const e = g.input.dispatch('keydown', { key: 'Escape' });
  assert.equal(g.input.value, ''); assert.equal(g.list.hidden, true); assert.equal(e.stopped, true);
});

test('choosing remembers up to five recent people, newest first, without repeats', async () => {
  const f = fixture(() => [row(1)]);
  for (const id of [1, 2, 3, 4, 5, 6, 3]) { f.search.results = [row(id)]; f.search.pickPerson(row(id)); }
  const recent = S.readRecent({ getItem: k => f.store.get(k) });
  assert.deepEqual(recent.map(r => r.id), [3, 6, 5, 4, 2]);
  f.input.dispatch('focus');
  assert.equal(f.list.children.filter(c => c.attrs.role === 'option').length, 5);
});

test('a person outside the page appears as a guest on the rim without moving anyone', () => {
  const model = new MapModel({ reduced: true, fetchJson: async () => ({}) });
  const people = Array.from({ length: 30 }, (_, i) => ({ id: i + 1, handle: 'p' + i, x: .5, y: .5, closeness: 1 - i / 40 }));
  model.apply({ rev: 1, total: 99, nodes: people, world: { me: { id: 0 } } }, {});
  const before = new Map(model.scene.nodes.map(it => [it.d.id, [it.d.x, it.d.y]]));
  model.goTo({ id: 999, handle: 'far', x: .1, y: .1 });
  assert.equal(model.selected.guest, true);
  assert.ok(Math.hypot(model.selected.x - .5, model.selected.y - .5) > .46);
  for (const [id, xy] of before) assert.deepEqual([model.scene.get(id).d.x, model.scene.get(id).d.y], xy);
  assert.equal(model.cam.cx, model.selected.x);
  model.goTo({ id: 5, handle: 'p4', x: 0, y: 0 });
  assert.equal(model.scene.get(999), null, 'the guest leaves when another person is chosen');
  assert.equal(model.selected.guest, undefined);
});
