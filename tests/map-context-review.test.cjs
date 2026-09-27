const { test } = require('node:test');
const assert = require('node:assert/strict');
const { Index, Points } = require('../web/map-renderer.js');

function fixture() {
  let generation = 0, lost = false, failCompile = false, uploads = [], draws = [], bound;
  const listeners = new Map();
  const gl = new Proxy({
    isContextLost: () => lost,
    createProgram: () => ({ generation, kind: 'program' }),
    createBuffer: () => ({ generation, kind: 'buffer' }),
    createShader: () => ({ generation, kind: 'shader' }),
    getShaderParameter: () => !failCompile,
    getProgramParameter: () => true,
    getShaderInfoLog: () => 'simulated compilation failure',
    bindBuffer: (_, buffer) => {
      assert.equal(lost, false, 'must not use GPU while context is lost');
      assert.equal(buffer.generation, generation, 'stale buffer'); bound = buffer;
    },
    bufferData: (_, data) => uploads.push({ buffer: bound, data: [...data] }),
    useProgram: program => assert.equal(program.generation, generation, 'stale program'),
    drawArrays: (_, __, count) => draws.push(count),
  }, { get: (object, key) => key in object ? object[key] : () => ({}) });
  const canvas = { width: 0, height: 0, getContext: () => gl,
    addEventListener: (name, callback) => listeners.set(name, callback) };
  return {
    canvas, uploads, draws,
    lose() { lost = true; let prevented = false;
      listeners.get('webglcontextlost')({ preventDefault() { prevented = true; } });
      assert.equal(prevented, true);
    },
    restore(fail = false) { lost = false; failCompile = fail; generation++;
      listeners.get('webglcontextrestored')(); },
  };
}
const draw = renderer => renderer.draw(900, 600, 0, 0, 1, [0.5, 0.5, 0.5, 1]);

test('restoration creates new resources and redraws retained vertices', () => {
  const f = fixture(), renderer = new Points(f.canvas);
  renderer.set([{ x: 1, y: 2, r: 3 }]);
  assert.equal(draw(renderer), true);
  f.lose(); assert.equal(draw(renderer), false);
  f.restore(); assert.equal(draw(renderer), true);
  assert.deepEqual(f.uploads.at(-1).data, [1, 2, 3]);
  assert.equal(f.uploads.at(-1).buffer.generation, 1);
});

test('set during context loss retains the latest data without GPU calls', () => {
  const f = fixture(), renderer = new Points(f.canvas);
  renderer.set([{ x: 1, y: 2, r: 3 }]); f.lose();
  const before = f.uploads.length;
  renderer.set([{ x: 4, y: 5, r: 6 }, { x: 7, y: 8, r: 9 }]);
  assert.equal(f.uploads.length, before);
  f.restore(); assert.equal(draw(renderer), true);
  assert.deepEqual(f.uploads.at(-1).data, [4, 5, 6, 7, 8, 9]);
  assert.equal(f.draws.at(-1), 2);
});

test('failed restoration stays on fallback and a later restoration recovers', () => {
  const f = fixture(), renderer = new Points(f.canvas);
  renderer.set([{ x: 1, y: 2, r: 3 }]);
  f.lose(); assert.doesNotThrow(() => f.restore(true)); assert.equal(draw(renderer), false);
  f.lose(); f.restore(); assert.equal(draw(renderer), true);
});

test('empty dataset and repeated loss/restore cycles retain correct draw counts', () => {
  const f = fixture(), renderer = new Points(f.canvas);
  renderer.set([]);
  for (let i = 0; i < 3; i++) { f.lose(); f.restore(); assert.equal(draw(renderer), true); }
  assert.deepEqual(f.draws, [0, 0, 0]);
});

test('all 100,000 vertices survive GPU restoration', () => {
  const f = fixture(), renderer = new Points(f.canvas);
  renderer.set(Array.from({ length: 100000 }, (_, i) => ({ x: i, y: 2 * i, r: 7 })));
  f.lose(); f.restore(); assert.equal(draw(renderer), true);
  assert.equal(f.draws.at(-1), 100000);
  assert.equal(f.uploads.at(-1).data.length, 300000);
  assert.deepEqual(f.uploads.at(-1).data.slice(-3), [99999, 199998, 7]);
});

test('existing permissive GPU mock remains compatible', () => {
  let count;
  const gl = new Proxy({ getShaderParameter: () => true, getProgramParameter: () => true,
    drawArrays: (_, __, n) => count = n }, { get: (o, p) => p in o ? o[p] : () => ({}) });
  const renderer = new Points({ width: 0, height: 0, getContext: () => gl, addEventListener() {} });
  renderer.set([{ x: 1, y: 2, r: 3 }]);
  assert.equal(draw(renderer), true); assert.equal(count, 1);
});

test('spatial index still returns a bounded view and exact picks at 100k nodes', () => {
  const nodes = Array.from({ length: 100000 }, (_, i) => ({ id: i, x: i % 400 * 24, y: Math.floor(i / 400) * 24, r: 5 }));
  const index = new Index(nodes);
  assert.equal(index.query(-1, -1, 10000, 10000).length, 100000);
  assert.equal(index.query(-1, -1, 10000, 10000, 600).length, 600);
  for (let i = 0; i < 1000; i++) { const n = nodes[i * 7919 % nodes.length]; assert.equal(index.pick(n.x, n.y, 1), n); }
});
