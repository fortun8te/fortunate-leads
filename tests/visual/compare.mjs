// Pixel comparison with a tolerance. Pure functions on RGBA buffers, so they run in Node tests and
// (stringified) inside the headless page, which decodes the PNGs for us without any image library.

// Count pixels whose largest channel difference exceeds `channel` (0-255). Returns the changed share.
export function compareRGBA(a, b, channel = 12) {
  if (a.length !== b.length) return { ratio: 1, changed: Math.max(a.length, b.length) / 4, total: Math.max(a.length, b.length) / 4 };
  let changed = 0;
  for (let i = 0; i < a.length; i += 4) {
    const d = Math.max(Math.abs(a[i] - b[i]), Math.abs(a[i + 1] - b[i + 1]), Math.abs(a[i + 2] - b[i + 2]));
    if (d > channel) changed++;
  }
  const total = a.length / 4;
  return { ratio: total ? changed / total : 0, changed, total };
}

// The bounding box of changed pixels, to say where a difference is.
export function changedBox(a, b, width, channel = 12) {
  let x0 = Infinity, y0 = Infinity, x1 = -1, y1 = -1;
  for (let i = 0; i < a.length; i += 4) {
    const d = Math.max(Math.abs(a[i] - b[i]), Math.abs(a[i + 1] - b[i + 1]), Math.abs(a[i + 2] - b[i + 2]));
    if (d > channel) { const p = i / 4, x = p % width, y = (p - x) / width; if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
  }
  return x1 < 0 ? null : { x: x0, y: y0, w: x1 - x0 + 1, h: y1 - y0 + 1 };
}

// Idle stability: every frame must match the first within `tolerance` (a share of pixels). A drawing
// that flickers, drifts or redraws while nothing is happening fails this even if it looks right in a still.
export function idleReport(frames, channel = 12, tolerance = 0) {
  const worst = frames.slice(1).map((f) => compareRGBA(frames[0], f, channel).ratio);
  const max = worst.length ? Math.max(...worst) : 0;
  return { frames: frames.length, worstRatio: max, stable: max <= tolerance };
}

// Runs in the page: decode two PNG data URLs and compare them. Returns the ratio, the box and a diff PNG.
export const PAGE_COMPARE = `async (baseUrl, currentUrl, channel) => {
  const load = (src) => new Promise((ok, no) => { const i = new Image(); i.onload = () => ok(i); i.onerror = () => no(new Error('decode failed')); i.src = src; });
  const [a, b] = await Promise.all([load(baseUrl), load(currentUrl)]);
  if (a.width !== b.width || a.height !== b.height) return { sizeMismatch: [a.width + 'x' + a.height, b.width + 'x' + b.height], ratio: 1 };
  const read = (img) => { const c = document.createElement('canvas'); c.width = img.width; c.height = img.height; const g = c.getContext('2d', { willReadFrequently: true }); g.drawImage(img, 0, 0); return g.getImageData(0, 0, c.width, c.height); };
  const A = read(a), B = read(b), out = new ImageData(a.width, a.height);
  let changed = 0, x0 = 1e9, y0 = 1e9, x1 = -1, y1 = -1;
  for (let i = 0; i < A.data.length; i += 4) {
    const d = Math.max(Math.abs(A.data[i] - B.data[i]), Math.abs(A.data[i + 1] - B.data[i + 1]), Math.abs(A.data[i + 2] - B.data[i + 2]));
    const hit = d > channel;
    if (hit) { changed++; const p = i / 4, x = p % a.width, y = (p - x) / a.width; if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
    out.data[i] = hit ? 255 : B.data[i] * 0.35; out.data[i + 1] = hit ? 40 : B.data[i + 1] * 0.35; out.data[i + 2] = hit ? 40 : B.data[i + 2] * 0.35; out.data[i + 3] = 255;
  }
  const c = document.createElement('canvas'); c.width = a.width; c.height = a.height; c.getContext('2d').putImageData(out, 0, 0);
  return { ratio: changed / (a.width * a.height), changed, box: x1 < 0 ? null : { x: x0, y: y0, w: x1 - x0 + 1, h: y1 - y0 + 1 }, diff: c.toDataURL('image/png') };
}`;

// Runs in the page: compare consecutive screenshots of one region. `frames` are PNG data URLs.
export const PAGE_IDLE = `async (frames, channel) => {
  const load = (src) => new Promise((ok, no) => { const i = new Image(); i.onload = () => ok(i); i.onerror = () => no(new Error('decode failed')); i.src = src; });
  const imgs = await Promise.all(frames.map(load));
  const px = imgs.map((img) => { const c = document.createElement('canvas'); c.width = img.width; c.height = img.height; const g = c.getContext('2d', { willReadFrequently: true }); g.drawImage(img, 0, 0); return g.getImageData(0, 0, c.width, c.height).data; });
  const worst = [];
  for (let f = 1; f < px.length; f++) { let n = 0; for (let i = 0; i < px[0].length; i += 4) if (Math.max(Math.abs(px[0][i] - px[f][i]), Math.abs(px[0][i + 1] - px[f][i + 1]), Math.abs(px[0][i + 2] - px[f][i + 2])) > channel) n++; worst.push(n / (px[0].length / 4)); }
  return { worst };
}`;
