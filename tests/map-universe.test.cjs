const test = require("node:test");
const assert = require("node:assert/strict");
const {
  Universe,
  parseTile,
  pack,
  record,
  transformFor,
  ByteCache,
  buildIndex,
} = require("../web/map-universe.js");
const { Camera } = require("../web/map-core.js");
function binary(rows) {
  const buffer = new ArrayBuffer(16 + rows.length * 32),
    d = new DataView(buffer);
  d.setUint32(0, 0x3156554d, true);
  d.setUint32(4, rows.length, true);
  d.setUint32(8, 32, true);
  rows.forEach((r, i) => {
    const p = 16 + i * 32;
    d.setUint32(p, r.id, true);
    [r.x, r.y, r.r].forEach((v, j) => d.setFloat32(p + 4 + j * 4, v, true));
    d.setUint32(p + 16, r.followers || 0, true);
    d.setUint16(p + 20, r.hop || 0, true);
    d.setUint16(p + 22, r.flags || 0, true);
  });
  return buffer;
}
const manifest = {
  available: true,
  version: "v1",
  bounds: { x0: -1000, y0: -1000, x1: 1000, y1: 1000 },
  anchor: { x: 0, y: 0 },
  total: 5000000,
};
function engine(o = {}) {
  const e = new Universe(o);
  e.manifest = manifest;
  e.transform = transformFor(manifest);
  e.camera = new Camera();
  e.renderer = { upload() {}, remove() {}, update() {}, render() {} };
  return e;
}
test("strict binary header, count, coordinates and radii validation", () => {
  const b = binary([
    { id: 9, x: 200, y: 0, r: 3, followers: 123, hop: 1, flags: 6 },
  ]);
  const t = parseTile(b);
  assert.equal(t.count, 1);
  assert.equal(record(t, 0, transformFor(manifest)).followers, 123);
  for (const [offset, value] of [
    [0, 1],
    [8, 48],
    [4, 2],
  ]) {
    const bad = b.slice(0);
    new DataView(bad).setUint32(offset, value, true);
    assert.throws(() => parseTile(bad));
  }
  assert.throws(() => parseTile(b, 0));
  assert.throws(() => parseTile(binary([{ id: 1, x: NaN, y: 0, r: 1 }])));
  assert.throws(() => parseTile(binary([{ id: 1, x: 0, y: 0, r: -1 }])));
  assert.throws(() => parseTile(b.slice(0, -1)));
  assert.throws(() => parseTile(new ArrayBuffer(15)));
});
test("unit world preserves backend radial order and explicit unknown followers", () => {
  const t = parseTile(
    binary([
      { id: 1, x: 0, y: 0, r: 10, flags: 1 },
      { id: 2, x: 200, y: 0, r: 4, hop: 1 },
      { id: 3, x: 400, y: 0, r: 3, hop: 2 },
    ]),
  );
  const x = transformFor(manifest);
  t.packed = pack(t, x);
  assert.equal(t.packed[0], 0.5);
  assert.ok(t.packed[10] < t.packed[20]);
  assert.equal(record(t, 1, x).followers, null);
  assert.equal(record(t, 1, x).portraitRadius, 4 / 2200);
  assert.equal(t.bytes, 16 + 3 * (32 + 40 + 40 + 4) + 4096);
});
test("cache bounds include CPU, GPU and index bytes; pinned overflow is refused", () => {
  const evicted = [];
  const cache = new ByteCache(100, (v) => evicted.push(v.id));
  const a = { id: "a", bytes: 60 },
    b = { id: "b", bytes: 60 };
  assert.ok(cache.set("a", a));
  assert.ok(cache.set("b", b));
  assert.equal(cache.bytes, 60);
  assert.deepEqual(evicted, ["a"]);
  assert.equal(
    cache.set("c", { id: "c", bytes: 60 }, new Set(["b", "c"])),
    false,
  );
  assert.equal(cache.bytes, 60);
  assert.equal(cache.set("x", { bytes: 101 }), false);
  cache.clear();
  assert.equal(cache.bytes, 0);
});
test("tile loading prioritizes nearby hops and obeys record and byte budgets", async () => {
  const called = [],
    e = engine({
      maxVisibleRecords: 3,
      fetchJson: async () => ({
        version: "v1",
        tiles: [
          { id: "far", record_count: 2, priority: 4, url: "far" },
          { id: "near", record_count: 2, priority: 1, url: "near" },
        ],
      }),
      fetchBinary: async (url) => {
        called.push(url);
        return binary([
          { id: 1, x: 200, y: 0, r: 3 },
          { id: 2, x: 210, y: 0, r: 3 },
        ]);
      },
    });
  await e.updateCamera();
  assert.deepEqual(called, ["near"]);
  assert.equal(e.cache.items.size, 1);
  assert.equal(e.visible.has("far"), false);
});
test("stale fetch cannot upload tiles after another camera or pause wins", async () => {
  let finish;
  let queries = 0;
  const uploads = [];
  const e = engine({
    fetchJson: async () => ({
      version: "v1",
      tiles: [{ id: String(++queries), record_count: 1, url: String(queries) }],
    }),
    fetchBinary: (url) =>
      url === "1"
        ? new Promise((resolve) => (finish = resolve))
        : Promise.resolve(binary([{ id: 2, x: 2, y: 0, r: 1 }])),
  });
  e.renderer.upload = (t) => uploads.push(t.id);
  const first = e.updateCamera();
  await new Promise((resolve) => setImmediate(resolve));
  await e.updateCamera();
  finish(binary([{ id: 1, x: 1, y: 0, r: 1 }]));
  await first;
  assert.deepEqual(uploads, ["2"]);
  e.pause();
  assert.ok(e.controller.signal.aborted);
});
test("version mismatch refuses a mixed snapshot", async () => {
  const e = engine({
    fetchJson: async () => ({ version: "v2", tiles: [] }),
    onError: (error) => (e.error = error),
  });
  await e.updateCamera();
  assert.match(e.error.message, /changed/);
});
test("indexed picking returns exact stable profile IDs and supports readable owner", () => {
  const e = engine();
  const rows = Array.from({ length: 8000 }, (_, i) => ({
    id: i + 1,
    x: (i % 100) * 10 - 500,
    y: Math.floor(i / 100) * 10 - 400,
    r: 2,
  }));
  const tile = parseTile(binary(rows));
  tile.packed = pack(tile, e.transform);
  buildIndex(tile);
  e.cache.set("t", tile);
  e.visible.add("t");
  e.camera.k = 64;
  const expected = record(tile, 4567, e.transform),
    point = e.camera.toScreen(expected.x, expected.y);
  assert.equal(e.pick(...point).id, expected.id);
  assert.equal(e.pick(-999, -999), null);
  e.owner = { id: 90000 };
  assert.equal(e.pick(...e.camera.toScreen(0.5, 0.5)).id, 90000);
});
test("portrait admission pins a bounded camera set and remains stable across frames", () => {
  const e = engine(),
    seen = [];
  e.atlas = {
    setPins(ids) {
      this.pins = ids;
    },
    request: (id) => {
      seen.push(id);
      return null;
    },
  };
  e.camera.k = 64;
  const tile = parseTile(
    binary(
      Array.from({ length: 60 }, (_, i) => ({ id: i + 1, x: 0, y: 0, r: 10 })),
    ),
  );
  tile.packed = pack(tile, e.transform);
  buildIndex(tile);
  e.schedulePortraits([tile], e.camera);
  e.schedulePortraits([tile], e.camera);
  assert.equal(seen.length, 120);
  assert.equal(e.atlas.pins.size, 60);
  assert.equal(seen[0], seen[60]);
});
test("array manifest bounds normalize owner and farthest points consistently", () => {
  const t = transformFor({ ...manifest, bounds: [-1000, -500, 1000, 500] });
  assert.equal(t.span, 2200);
  assert.equal(t.x, 0);
});
test("initial center tiles load before paginated viewport and repeated cursors fail", async () => {
  const order = [],
    e = engine({
      maxVisibleRecords: 5,
      fetchJson: async (url) => {
        order.push("view");
        const q = new URL("http://local" + url).searchParams;
        return q.has("cursor")
          ? {
              version: "v1",
              tiles: [{ id: "b", record_count: 1, url: "b" }],
              next_cursor: null,
            }
          : {
              version: "v1",
              tiles: [{ id: "a", record_count: 1, url: "a" }],
              next_cursor: "next",
            };
      },
      fetchBinary: async (url) => {
        order.push(url);
        return binary([
          { id: url === "center" ? 1 : url === "a" ? 2 : 3, x: 20, y: 0, r: 2 },
        ]);
      },
    });
  e.manifest = {
    ...manifest,
    tiles: [{ id: "center", record_count: 1, url: "center" }],
  };
  await e.updateCamera();
  assert.deepEqual(order, ["center", "view", "a", "view", "b"]);
  assert.equal(e.visible.size, 3);
});
test("metadata count mismatch refuses a corrupt tile", async () => {
  const e = engine({
    fetchJson: async () => ({
      version: "v1",
      tiles: [{ id: "a", record_count: 1, url: "a" }],
    }),
    fetchBinary: async () => binary([]),
    onError: (error) => (e.error = error),
  });
  await e.updateCamera();
  assert.match(e.error.message, /count/);
  assert.equal(e.cache.bytes, 0);
});
test("local portrait atlas caps pending decodes, slots and releases bitmap resources", async () => {
  const { Atlas } = require("../web/map-universe-gpu.js");
  const oldFetch = global.fetch,
    oldBitmap = global.createImageBitmap;
  let active = 0,
    maxActive = 0,
    closed = 0,
    uploaded = 0;
  const finishes = [];
  global.fetch = () => {
    active++;
    maxActive = Math.max(maxActive, active);
    return new Promise((resolve) =>
      finishes.push(() => {
        active--;
        resolve({ ok: true, blob: async () => new Blob(["a"]) });
      }),
    );
  };
  global.createImageBitmap = async () => ({
    close() {
      closed++;
    },
  });
  const atlas = new Atlas(
    {
      image() {
        uploaded++;
      },
    },
    () => {},
    { maxSlots: 4, maxQueue: 2, concurrency: 2 },
  );
  try {
    for (let i = 0; i < 20; i++) atlas.request(i);
    assert.equal(atlas.slots, 4);
    assert.equal(atlas.active, 2);
    assert.equal(atlas.queue.length, 2);
    while (finishes.length) {
      finishes.shift()();
      await new Promise((resolve) => setImmediate(resolve));
    }
    assert.equal(uploaded, 4);
    assert.equal(closed, 4);
    assert.equal(maxActive, 2);
    assert.equal(atlas.request(0), 0);
    atlas.dispose();
    assert.equal(atlas.entries.size, 0);
  } finally {
    global.fetch = oldFetch;
    global.createImageBitmap = oldBitmap;
  }
});
test("GPU portraits are borderless, unloaded bubbles hidden and subpixel dots fade away", () => {
  const {
    GLSL_VERTEX,
    GLSL_FRAGMENT,
    WGSL,
  } = require("../web/map-universe-gpu.js");
  for (const shader of [GLSL_FRAGMENT, WGSL])
    assert.doesNotMatch(shader, /d\s*>\s*\.92/); // no portrait ring repaint
  assert.match(GLSL_VERTEX, /color\.a<0\.\?13\./);
  assert.match(WGSL, /13\.,color\.a<0\./);
  assert.match(
    GLSL_VERTEX,
    /smoothstep\(\.25,1\.,natural\)\*\(1\.-smoothstep\(2\.,6\.,natural\)\)\*\.18/,
  );
  assert.match(GLSL_FRAGMENT, /clamp\(\.75\/max\(radius,1\.\),\.06,\.45\)/);
  assert.match(WGSL, /smoothstep\(\.25,1\.,natural\)\*\(1\.-smoothstep\(2\.,6\.,natural\)\)\*\.18/);
  assert.match(WGSL, /textureSampleLevel\(atlas,atlasSampler,uv,0\.\)/);
  assert.doesNotMatch(WGSL, /textureSample\(/);
  assert.match(GLSL_FRAGMENT, /else if\(radius>=6\.\)\{discard;/);
  assert.match(WGSL, /else if\(o.radius>=6\.\)\{discard;/);
  for (const shader of [GLSL_VERTEX, WGSL])
    assert.doesNotMatch(shader, /max\(1\.1|,\.18,1\./);
  assert.match(GLSL_FRAGMENT, /mod\(atlasSlot,32\.\)/);
  assert.match(GLSL_FRAGMENT, /\/2048\./);
  assert.match(WGSL, /o.slot%32\./);
  assert.match(GLSL_FRAGMENT, /smoothstep\(6\.,10\.,radius\)/);
  assert.match(WGSL, /smoothstep\(6\.,10\.,o.radius\)/);
  assert.match(WGSL, /\/2048\./);
});
test("atlas LRU replaces more than1024 profiles across views without stale faces", async () => {
  const { Atlas } = require("../web/map-universe-gpu.js");
  const oldFetch = global.fetch,
    oldBitmap = global.createImageBitmap;
  const images = [],
    updates = [];
  global.fetch = async (url) => ({
    ok: true,
    blob: async () => new Blob([url]),
  });
  global.createImageBitmap = async (blob) => ({
    id: Number((await blob.text()).split("/").pop()),
    close() {},
  });
  const atlas = new Atlas(
    {
      image(slot, bitmap) {
        images.push([slot, bitmap.id]);
      },
      update(tile, offset, data) {
        updates.push([offset, data[0]]);
      },
    },
    () => {},
  );
  async function load(ids) {
    atlas.setPins(ids);
    for (let turn = 0; turn < 64; turn++) {
      for (const id of ids) atlas.request(id);
      await new Promise((resolve) => setImmediate(resolve));
      if (ids.every((id) => atlas.entries.get(id)?.ready)) return;
    }
    throw Error("Atlas did not settle");
  }
  try {
    const first = Array.from({ length: 1024 }, (_, i) => i + 1);
    await load(first);
    assert.equal(atlas.entries.size, 1024);
    assert.ok(atlas.queue.length <= 48);
    const tile = { packed: new Float32Array(10) };
    tile.packed[7] = atlas.bind(1, tile, 7);
    assert.equal(tile.packed[7], atlas.entries.get(1).slot);
    const second = Array.from({ length: 1024 }, (_, i) => i + 1025);
    await load(second);
    assert.equal(atlas.entries.size, 1024);
    assert.equal(tile.packed[7], -1);
    assert.ok(updates.some(([offset, value]) => offset === 28 && value === -1));
    assert.equal(atlas.entries.has(1), false);
    assert.ok(second.every((id) => atlas.entries.get(id).ready));
    assert.ok(images.some(([slot, id]) => id === 2048));
  } finally {
    atlas.dispose();
    global.fetch = oldFetch;
    global.createImageBitmap = oldBitmap;
  }
});
test("an evicted active decode cannot overwrite its reused slot", async () => {
  const { Atlas } = require("../web/map-universe-gpu.js");
  const oldFetch = global.fetch,
    oldBitmap = global.createImageBitmap,
    finished = [];
  let release;
  global.fetch = async (url) => ({
    ok: true,
    blob: async () => new Blob([url]),
  });
  global.createImageBitmap = async (blob) => {
    const id = Number((await blob.text()).split("/").pop());
    if (id === 1) await new Promise((resolve) => (release = resolve));
    return { id, close() {} };
  };
  const atlas = new Atlas(
    {
      image(slot, bitmap) {
        finished.push(bitmap.id);
      },
      update() {},
    },
    () => {},
    { maxSlots: 1, concurrency: 2 },
  );
  try {
    atlas.setPins([1]);
    atlas.request(1);
    await new Promise((resolve) => setImmediate(resolve));
    atlas.setPins([2]);
    atlas.request(2);
    await new Promise((resolve) => setImmediate(resolve));
    release();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(finished, [2]);
    assert.equal(atlas.entries.get(2).ready, true);
  } finally {
    atlas.dispose();
    global.fetch = oldFetch;
    global.createImageBitmap = oldBitmap;
  }
});


test("portrait admission accepts six pixel faces and caps a large view at1024", () => {
  const e = engine();
  e.camera.k = 64;
  e.atlas = { maxSlots: 1024, setPins(ids) { this.pins = ids; }, request() { return null; } };
  const radius = 6.1 * e.transform.span / e.camera.scale;
  const tile = parseTile(binary(Array.from({length: 1400}, (_, i) => ({id: i + 1, x: 0, y: 0, r: radius}))));
  tile.packed = pack(tile, e.transform);
  buildIndex(tile);
  e.schedulePortraits([tile], e.camera);
  assert.equal(e.atlas.pins.size, 1024);
  assert.equal(e.portraitCandidates.length, 1024);
  assert.equal(e.maxVisibleRecords, 250000);
});

test("portrait atlas geometry fits1024 faces within16MiB and caps loading", () => {
  const { Atlas, SIZE, CELL, COLS, MAX_PORTRAITS } = require("../web/map-universe-gpu.js");
  assert.equal(SIZE, 2048);
  assert.equal(CELL, 64);
  assert.equal(COLS, 32);
  assert.equal(MAX_PORTRAITS, 1024);
  assert.equal(SIZE * SIZE * 4, 16 * 1024 * 1024);
  const atlas = new Atlas({}, () => {}, { maxSlots: 5000, maxQueue: 900, concurrency: 20 });
  assert.equal(atlas.maxSlots, 1024);
  assert.equal(atlas.maxQueue, 48);
  assert.equal(atlas.concurrency, 4);
  const normal = new Atlas({}, () => {});
  assert.equal(normal.maxSlots, 1024);
  assert.equal(normal.concurrency, 2);
  atlas.dispose();
  normal.dispose();
});


test("portrait admission retains the closest faces across scanlines and loads center first", () => {
  const e = engine(), seen = [];
  e.camera.k = 64;
  e.atlas = { maxSlots: 2, setPins(ids) { this.pins = ids; }, request(id) { seen.push(id); return null; } };
  const unit = e.transform.span / e.camera.scale;
  const tile = parseTile(binary([
    {id: 1, x: -200 * unit, y: -200 * unit, r: 9 * unit},
    {id: 2, x: 100 * unit, y: -150 * unit, r: 9 * unit},
    {id: 3, x: 0, y: 0, r: 9 * unit},
    {id: 4, x: 20 * unit, y: 20 * unit, r: 9 * unit},
  ]));
  tile.packed = pack(tile, e.transform);
  buildIndex(tile);
  e.schedulePortraits([tile], e.camera);
  assert.deepEqual(seen, [3, 4]);
  assert.deepEqual([...e.atlas.pins].sort(), [3, 4]);
});

test("failed portrait batches continue admission without re-fetching failed photos", async () => {
  const { Atlas } = require("../web/map-universe-gpu.js");
  const oldFetch = global.fetch, requests = [];
  const ids = Array.from({length: 130}, (_, i) => i + 1);
  global.fetch = async (url) => { requests.push(Number(url.split("/").pop())); return {ok: false}; };
  let notifications = 0;
  const atlas = new Atlas({}, () => {
    notifications++;
    for (const id of ids) atlas.request(id);
  });
  try {
    atlas.setPins(ids);
    for (const id of ids) atlas.request(id);
    for (let turn = 0; turn < 10 && requests.length < ids.length; turn++)
      await new Promise(resolve => setImmediate(resolve));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(requests.length, ids.length);
    assert.equal(new Set(requests).size, ids.length);
    assert.equal(notifications, ids.length);
    assert.equal(atlas.active, 0);
    assert.equal(atlas.queue.length, 0);
    assert.ok(ids.every(id => atlas.entries.get(id).failed));
    for (const id of ids) atlas.request(id);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(requests.length, ids.length);
  } finally {
    atlas.dispose();
    global.fetch = oldFetch;
  }
});
