const test = require('node:test');
const assert = require('node:assert/strict');
const { PhotoTransport, parseBatch, imageSize } = require('../web/map-photos.js');
const { PortraitCache } = require('../web/map-core.js');
const frame = (entries) => {
  const head = Buffer.alloc(8 + entries.length * 8); head.write('MPH1'); head.writeUInt32LE(entries.length, 4);
  entries.forEach(([id, bytes], i) => { head.writeUInt32LE(id, 8 + i * 8); head.writeUInt32LE(bytes === 'deferred' ? 0xffffffff : bytes.length, 12 + i * 8); });
  const body = Buffer.concat([head, ...entries.filter(([, b]) => b !== 'deferred').map(([, b]) => Buffer.from(b))]);
  return body.buffer.slice(body.byteOffset, body.byteOffset + body.length);
};
test('parseBatch returns one result per url in order, marking missing and deferred', () => {
  const r = parseBatch(frame([[2, [1, 2]], [1, []], [3, 'deferred']]), ['/img/2', '/img/1', '/img/3', '/img/9']);
  assert.equal(r[0].ok, true); assert.equal(r[0].bytes.byteLength, 2);
  assert.equal(r[1].status, 404); assert.equal(r[2].deferred, true); assert.equal(r[3].deferred, true);
  assert.throws(() => parseBatch(new ArrayBuffer(4), []));
});
test('imageSize reads png and jpeg headers and gives null otherwise', () => {
  const png = Buffer.alloc(24); Buffer.from([0x89, 0x50, 0x4e, 0x47]).copy(png); png.writeUInt32BE(150, 16); png.writeUInt32BE(150, 20);
  assert.deepEqual(imageSize(png.buffer.slice(0, 24)), { w: 150, h: 150 });
  const jpg = Buffer.from([0xff, 0xd8, 0xff, 0xc0, 0, 17, 8, 0, 100, 0, 200, 3, 0, 0, 0, 0, 0, 0, 0, 0]);
  assert.deepEqual(imageSize(jpg.buffer.slice(jpg.byteOffset, jpg.byteOffset + jpg.length)), { w: 200, h: 100 });
  assert.equal(imageSize(new ArrayBuffer(30)), null);
});
test('PortraitCache over PhotoTransport batches requests, keeps order and bounds', async () => {
  const calls = [];
  const transport = new PhotoTransport({ fetchBatch: async (urls) => { calls.push(urls.slice()); return urls.map(() => ({ ok: true, bytes: new ArrayBuffer(3) })); },
    decode: async () => ({ width: 64, height: 64, close() {} }) });
  const cache = new PortraitCache({ max: 200, concurrency: 160, createImage: () => transport.createImage(), normalize: (i) => i.bitmap });
  const urls = Array.from({ length: 100 }, (_, i) => '/img/' + (i + 1));
  cache.prioritize(urls);
  for (let i = 0; i < 50; i++) await new Promise((r) => setTimeout(r, 2));
  assert.equal(calls[0].length, 16); assert.deepEqual(calls[0][0], '/img/1');
  assert.ok(calls.length <= 8, 'few requests: ' + calls.length);
  assert.equal(urls.filter((u) => cache.peek(u)).length, 100);
});
test('missing batch route falls back to single requests; failures become failed entries', async () => {
  let singles = 0;
  const transport = new PhotoTransport({ fetchBatch: async () => { const e = new Error('x'); e.status = 404; throw e; },
    fetchPhoto: async (u) => { singles++; return u === '/img/2' ? { ok: false, status: 404 } : { ok: true, bytes: new ArrayBuffer(3) }; },
    decode: async () => ({ width: 64, height: 64 }) });
  const cache = new PortraitCache({ concurrency: 10, createImage: () => transport.createImage(), normalize: (i) => i.bitmap });
  cache.prioritize(['/img/1', '/img/2', '/img/3']);
  for (let i = 0; i < 30; i++) await new Promise((r) => setTimeout(r, 2));
  assert.equal(singles, 3); assert.ok(cache.peek('/img/1')); assert.equal(cache.entries.get('/img/2').state, 'failed');
});
