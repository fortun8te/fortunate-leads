// The screenshot regression harness: its pixel comparison and idle-frame check (tests/visual/).
import test from 'node:test';
import assert from 'node:assert/strict';
import { compareRGBA, changedBox, idleReport } from '../visual/compare.mjs';

const frame = (w, h, rgb = [20, 20, 20]) => { const a = new Uint8ClampedArray(w * h * 4); for (let i = 0; i < a.length; i += 4) a.set([...rgb, 255], i); return a; };
const paint = (a, w, x, y, rgb) => a.set([...rgb, 255], (y * w + x) * 4);

test('identical images differ by nothing', () => {
  assert.deepEqual(compareRGBA(frame(10, 10), frame(10, 10)), { ratio: 0, changed: 0, total: 100 });
});

test('a small anti-aliasing shift stays under the per-pixel threshold', () => {
  assert.equal(compareRGBA(frame(4, 4, [20, 20, 20]), frame(4, 4, [28, 24, 20]), 12).ratio, 0);
});

test('changed pixels are counted as a share of the image and located', () => {
  const a = frame(10, 10), b = frame(10, 10);
  for (let x = 2; x < 5; x++) for (let y = 6; y < 8; y++) paint(b, 10, x, y, [200, 200, 200]);
  const r = compareRGBA(a, b);
  assert.equal(r.changed, 6); assert.equal(r.ratio, 0.06);
  assert.deepEqual(changedBox(a, b, 10), { x: 2, y: 6, w: 3, h: 2 });
});

test('images of different sizes never count as equal', () => {
  assert.equal(compareRGBA(frame(4, 4), frame(5, 4)).ratio, 1);
});

test('idle frames: a steady screen is stable, a flicker is not', () => {
  const still = frame(8, 8), flick = frame(8, 8);
  for (let x = 0; x < 8; x++) paint(flick, 8, x, 0, [255, 255, 255]);
  assert.equal(idleReport([still, still, still]).stable, true);
  const r = idleReport([still, still, flick]);
  assert.equal(r.stable, false); assert.equal(r.worstRatio, 8 / 64);
  assert.equal(idleReport([still, flick], 12, 0.2).stable, true, 'a tolerance can allow a known small change');
});
