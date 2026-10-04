const test = require('node:test');
const assert = require('node:assert/strict');
const Core = require('../web/map-core.js');
const Modes = require('../web/map-modes.js');
const { MapModel } = require('../web/map-model.js');
const { doc } = require('./fake-dom.cjs');

const person = (id, extra = {}) => ({ id, handle: 'p' + id, x: .5, y: .5, closeness: .1, rank: 0, ...extra });
const ids = (people, mode) => Core.cohortLayout(people, { id: 0 }, Modes.get(mode).size, Modes.comparator(mode)).filter(n => n.id !== 0).map(n => n.id);

test('five modes, each with a plain one-line meaning, its own size scale and a named slice', () => {
  assert.deepEqual(Modes.MODES.map(m => m.id), ['network', 'reach', 'warm', 'shared', 'leads']);
  assert.deepEqual(Modes.MODES.map(m => m.lens), ['', 'reach', 'warm', 'shared', 'leads']);
  for (const mode of Modes.MODES) {
    assert.ok(mode.label && mode.line && mode.help && mode.closer, mode.id);
    assert.ok(mode.line.length <= 110, `${mode.id} line fits one line: ${mode.line.length}`);
    assert.match(mode.line, /\.$/);
    assert.equal(mode.label, mode.label[0].toUpperCase() + mode.label.slice(1), 'sentence case');
    assert.doesNotMatch(mode.line + mode.help, /!|Let's|Simply|Oops|Please/);
    assert.ok(Core.SIZE_OPTIONS.some(o => o.id === mode.size), `${mode.id} size ${mode.size} is selectable`);
  }
});

test('saved or retired names resolve to a real mode', () => {
  assert.equal(Modes.normalize('warm'), 'warm');
  assert.equal(Modes.normalize('fit'), 'leads', 'the retired best-fit view becomes promising leads');
  for (const bad of [undefined, null, '', 'bogus', 'audience', 'equal']) assert.equal(Modes.normalize(bad), 'network');
});

test('closest to me puts mutual follows first, then follows-you, then you-follow, whatever the score', () => {
  const order = ids([
    person(1, { followed: true, closeness: .6, followers: 9e9 }),
    person(2, { followed: true, follows_me: true, closeness: .9 }),
    person(3, { follows_me: true, closeness: .75 }),
    person(4, { closeness: .99, source_count: 500, priority: 100 })
  ], 'network');
  assert.deepEqual(order, [2, 3, 1, 4]);
});

test('biggest reach orders by follower count and never ranks an unknown count', () => {
  const order = ids([person(1, { followers: 500 }), person(2, { followers: null }), person(3, { followers: 3e6 }), person(4, { followers: 0 }), person(5)], 'reach');
  assert.deepEqual(order.slice(0, 3), [3, 1, 4]);
  assert.deepEqual(new Set(order.slice(3)), new Set([2, 5]));
});

test('warm paths order by links to your connections, then audiences, then followers', () => {
  const order = ids([
    person(1, { links: 2, source_count: 9 }), person(2, { links: 7, source_count: 1, followers: 5 }),
    person(3, { links: 2, source_count: 9, followers: 4000 }), person(4, { links: 0, source_count: 99 })
  ], 'warm');
  assert.deepEqual(order, [2, 3, 1, 4]);
});

test('shared audiences order by distinct audiences; promising leads by follow evidence then priority', () => {
  assert.deepEqual(ids([person(1, { source_count: 3 }), person(2, { source_count: 12 }), person(3, { source_count: 3, closeness: .8 })], 'shared'), [2, 3, 1]);
  assert.deepEqual(ids([
    person(1, { closeness: .5, priority: 90 }), person(2, { followed: true, follows_me: true, closeness: .9, priority: 50 }),
    person(3, { closeness: .5, priority: 70 })
  ], 'leads'), [2, 1, 3]);
});

test('size scales: wide for reach, priority for leads, unknown is smallest, existing scales untouched', () => {
  const r = (n, size) => Core.radiusFor(n, 1, size);
  assert.ok(r({ followers: 1e8 }, 'reach') > 1.8 * r({ followers: 5000 }, 'reach'), 'reach spans a wide range');
  assert.ok(r({ followers: 5000 }, 'reach') > r({ followers: null }, 'reach'));
  assert.ok(r({ followers: 700 }, 'reach') < r({ followers: 7e5 }, 'reach'));
  assert.ok(r({ priority: 88 }, 'score') > r({ priority: 55 }, 'score'));
  assert.ok(r({ priority: null }, 'score') < r({ priority: 46 }, 'score'));
  assert.equal(r({ followers: 100000 }, 'followers'), Core.radiusFor({ followers: 100000 }, 1), 'the modest follower scale is unchanged');
  assert.ok(r({ followers: 1e8 }, 'followers') < 2 * r({ followers: 5000 }, 'followers'));
});

test('hover and result text follow the mode', () => {
  assert.equal(Modes.metricText({ followers: 1234 }, 'network'), '1,234 followers');
  assert.equal(Modes.metricText({}, 'network'), 'Followers unknown');
  assert.equal(Modes.metricText({ source_count: 1 }, 'network'), '1 source audience');
  assert.equal(Modes.metricText({ links: 6, followers: 12000 }, 'warm'), '6 links · 12k followers');
  assert.equal(Modes.metricText({ links: 1 }, 'warm'), '1 link');
  assert.equal(Modes.metricText({ source_count: 3, followers: 9 }, 'shared'), '3 source audiences');
  assert.equal(Modes.metricText({ priority: 74.4 }, 'leads'), 'Priority 74');
});

test('coverage says how many people on the page have the number a mode relies on', () => {
  const page = [person(1, { followers: 10 }), person(2), person(3), person(4, { followers: 0 })];
  assert.equal(Modes.coverage('network', page), '2 of 4 have a saved follower count.');
  assert.equal(Modes.coverage('warm', page), '2 of 4 have a saved follower count.');
  assert.equal(Modes.coverage('network', [person(1, { followers: 1 })]), null, 'complete coverage needs no note');
  assert.equal(Modes.coverage('reach', page), null);
  assert.equal(Modes.coverage('network', [{ ...person(9), guest: true }]), null, 'a guest is not part of the page');
  assert.equal(Modes.coverage('network', []), null);
});

test('the segmented control selects with click and arrow keys and shows the active meaning', () => {
  const picked = [];
  const group = doc.createElement('div'), line = doc.createElement('p');
  const control = Modes.build(doc, (id) => picked.push(id), { group, line });
  assert.equal(control.buttons.length, 5);
  control.update('reach', [person(1, { followers: 5 }), person(2)]);
  const checked = control.buttons.filter(b => b.getAttribute('aria-checked') === 'true');
  assert.equal(checked.length, 1);
  assert.equal(checked[0].getAttribute('data-mode'), 'reach');
  assert.equal(control.buttons.filter(b => b.getAttribute('tabindex') === '0').length, 1, 'one tab stop for the whole group');
  assert.equal(line.textContent.startsWith(Modes.get('reach').line), true);
  group.dispatch('click', { target: control.buttons[2] });
  assert.deepEqual(picked, ['warm']);
  control.update('warm', [person(1, { followers: 1 }), person(2)]);
  assert.match(line.textContent, /1 of 2 have a saved follower count/);
  const right = group.dispatch('keydown', { key: 'ArrowRight' });
  assert.equal(right.defaultPrevented, true);
  assert.equal(picked.at(-1), 'shared');
  group.dispatch('keydown', { key: 'End' });
  assert.equal(picked.at(-1), 'leads');
  group.dispatch('keydown', { key: 'Home' });
  assert.equal(picked.at(-1), 'network');
  control.update('network', []);
  group.dispatch('keydown', { key: 'ArrowLeft' });
  assert.equal(picked.at(-1), 'leads', 'arrows wrap around');
});

test('each mode asks for its own slice and a visited mode is not asked for twice', async () => {
  const urls = [];
  const model = new MapModel({ reduced: true, fetchJson: async (url) => { urls.push(url); return { rev: 1, total: 1, nodes: [person(7, { x: .4, y: .4 })], world: { me: { id: 0 } } }; } });
  await model.load();
  assert.doesNotMatch(urls[0], /lens=/);
  for (const [mode, lens] of [['reach', 'reach'], ['warm', 'warm'], ['shared', 'shared'], ['leads', 'leads']]) {
    model.setViewMode(mode);
    await new Promise(r => setImmediate(r));
    assert.match(urls.at(-1), new RegExp('lens=' + lens));
    assert.equal(model.params().get('lens'), lens);
  }
  const before = urls.length;
  model.setViewMode('reach'); await new Promise(r => setImmediate(r));
  assert.equal(urls.length, before, 'cached');
  assert.equal(model.cache.limit, 6);
  model.density = 5000; await model.load({ force: false }).catch(() => {});
  assert.equal(model.cache.limit, 1, 'large pages are not kept in bulk');
});

test('compact pages carry links and priority; other pages default them', async () => {
  const model = new MapModel({ density: 3000, fetchJson: async () => ({ compact: true, rev: 1, total: 2, nodes: [], world: { me: { id: 0 } }, rows: [
    [1, .5, .5, 1, 50, .5, 100, 0, 0, 1, 0, 0, 4, 6, 77],
    [2, .5, .5, 1, 50, .5, 100, 0, 0, 1, 0, 0, 4]
  ] }) });
  const ticket = model.viewReq.begin();
  const response = await model.loadSnapshot(model.params(), ticket);
  assert.deepEqual([response.nodes[0].links, response.nodes[0].priority], [6, 77]);
  assert.deepEqual([response.nodes[1].links, response.nodes[1].priority], [0, null]);
});

test('switching to a different slice clears the selection but never moves the camera', () => {
  const model = new MapModel({ reduced: true, fetchJson: async () => ({ rev: 1, total: 0, nodes: [], world: { me: { id: 0 } } }) });
  model.apply({ rev: 1, total: 2, nodes: [person(1, { x: .4 }), person(2, { x: .6 })], world: { me: { id: 0 } } }, {});
  model.select(model.scene.get(1).d);
  model.cam.set(.3, .6, 2.5);
  const camera = model.cam.state();
  model.setViewMode('warm');
  assert.equal(model.selected, null);
  assert.deepEqual(model.cam.state(), camera);
  assert.equal(model.pageIndex, 0);
});
