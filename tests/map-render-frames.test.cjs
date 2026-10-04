/* Frame-loop behaviour of the real MapView, on a recording canvas: no work when nothing changes,
 * pans that only move a cached picture, and no frame that shows less than the one before it. */
const test = require('node:test');
const assert = require('node:assert/strict');
const { harness } = require('./helpers/map-view-harness.cjs');

test('an idle map requests no frames and draws nothing', async () => {
  const h = await harness({ people: 500 }).ready();
  assert.equal(h.pendingFrames, 0, 'no animation frame is pending once everything has settled');
  const before = h.ctx.draws.length;
  assert.deepEqual(h.frames(120).map((f) => f.callbacks), new Array(120).fill(0), 'two seconds of waiting runs no frames');
  assert.equal(h.ctx.draws.length, before);
});

test('panning moves one cached picture instead of redrawing every portrait', async () => {
  const h = await harness({ people: 1000 }).ready();
  h.frame();
  h.pan(60, 20, 8);
  const frames = h.settle(40);
  assert.ok(frames.length > 0);
  for (const f of frames) {
    assert.ok((f.counts.drawImage || 0) <= 4, `a pan frame blits the layer and the owner, not ${f.counts.drawImage} images`);
    assert.ok((f.counts.clip || 0) <= 1, 'only the owner is clipped while panning');
    assert.ok((f.counts.save || 0) <= 1);
  }
});

test('a pan that outruns the layer margin redraws it once, in a single frame', async () => {
  const h = await harness({ people: 1000 }).ready();
  const renders = () => h.view.stats.layerDraws || 0;
  const start = renders();
  h.pan(400, 0, 20);                       // 400 px, far beyond the 96 px margin
  const frames = h.settle(40);
  const heavy = frames.filter((f) => (f.counts.drawImage || 0) > 100);
  assert.ok(renders() - start >= 1 && renders() - start <= 2, `layer redrawn ${renders() - start} times`);
  assert.ok(heavy.length <= 2, 'only the frames that rebuilt the layer draw every portrait');
});

test('moving the pointer over portraits never repaints the map', async () => {
  const h = await harness({ people: 500 }).ready();
  const marks = h.marks().filter((m) => m.x > 50 && m.y > 50);
  const clears = () => h.ctx.counts.clearRect || 0;
  const before = clears();
  for (let i = 0; i < 40; i++) { const m = marks[i * 3]; h.hover(m.x, m.y); h.frame(); }
  assert.equal(clears(), before, 'hover frames run, but none paints');
  assert.ok(h.view.hover, 'the pointer is over someone');
  assert.equal(h.view.r.tip.hidden, false);
});

test('pointer moves are coalesced to one hit test per frame', async () => {
  const h = await harness({ people: 500 }).ready();
  let picks = 0; const pick = h.view.pick.bind(h.view); h.view.pick = (...a) => { picks++; return pick(...a); };
  for (let i = 0; i < 50; i++) h.hover(100 + i, 100 + i);
  assert.equal(picks, 0, 'nothing is hit-tested inside the event handlers');
  h.frame();
  assert.equal(picks, 1, 'fifty moves, one hit test');
});

test('resizing the map redraws in the same turn, so the cleared canvas is never shown', async () => {
  const h = await harness({ people: 500 }).ready();
  h.log.length = 0;
  h.resize(900, 640);
  const ops = h.log.filter((e) => e.canvas === 'map-canvas');
  const cleared = ops.findIndex((e) => e.op === 'canvas-width');
  assert.ok(cleared >= 0, 'the backing store was replaced');
  assert.ok(ops.slice(cleared + 1).some((e) => e.op === 'draw'), 'and portraits were drawn again before returning to the browser');
  assert.equal(h.view.canvas.width, Math.round(900 * 2));
});

test('showing the map again after a tab switch does not blank the canvas', async () => {
  const h = await harness({ people: 500 }).ready();
  h.log.length = 0;
  h.view.hide(); h.view.show();
  assert.equal(h.log.filter((e) => e.op === 'canvas-width').length, 0, 'same size: the canvas keeps its pixels');
});
