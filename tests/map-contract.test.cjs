const { test } = require('node:test');
const assert = require('node:assert/strict');
const { MapModel } = require('../web/map-model.js');
const { createWorld } = require('../web/map-world.js');
test('ten-million-person demo keeps viewport replies bounded in every layout', () => {
  const world = createWorld();
  assert.equal(world.meta.total, 10000000);
  for (const mode of world.meta.modes) {
    const reply = world.view({ mode, scope: 'all', x0: 0, y0: 0, x1: 1, y1: 1, budget: 600 });
    assert.ok(reply.nodes.length <= 600);
    assert.ok(reply.clusters.length <= 260);
    assert.equal(reply.total, 10000000);
    assert.ok(reply.nodes.every(n => Number.isFinite(n.x) && Number.isFinite(n.y)));
  }
});
test('map requests only the viewport and explicit map filters', () => {
  const map = new MapModel();
  map.scope = 'leads'; map.minFit = 'good'; map.status = 'client';
  const q = map.params({ x0: .2, y0: .3, x1: .4, y1: .5 });
  assert.equal(q.get('scope'), 'leads'); assert.equal(q.get('min_fit'), 'good'); assert.equal(q.get('status'), 'client');
  assert.equal(q.get('x0'), '.2'.replace(/^\./, '0.')); assert.ok(Number(q.get('budget')) <= 1500);
  assert.equal(q.has('tags'), false);
});
