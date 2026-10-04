/* Transport for the connection map's saved profile photos: many photos per request, decoded
 * off the main thread.
 *
 * PortraitCache (map-core.js) owns the queue, the bound and the reveal. It starts a photo by
 * creating an image object, setting its src and waiting for onload. PhotoTransport hands it
 * image objects that behave the same way but gather the requested urls, fetch them together
 * from /api/map/photos, decode each with createImageBitmap at the size the map draws, and then
 * call onload. normalize(image) returns that bitmap. A server without the batch route falls
 * back to one request per photo, and nothing else about the cache changes. */
(function (root) {
  'use strict';

  const SNIFF = [
    ['image/jpeg', [0xff, 0xd8, 0xff]],
    ['image/png', [0x89, 0x50, 0x4e, 0x47]],
  ];

  function sniffType(bytes) {
    const b = new Uint8Array(bytes, 0, Math.min(12, bytes.byteLength));
    for (const [type, magic] of SNIFF) if (magic.every((v, i) => b[i] === v)) return type;
    if (b.length >= 12 && b[0] === 0x52 && b[1] === 0x49 && b[8] === 0x57 && b[9] === 0x45) return 'image/webp';
    return 'application/octet-stream';
  }

  // Width and height from the file header, or null when unreadable. Used only to decide whether
  // a photo is already square; anything unexpected takes the slower, always correct path.
  function imageSize(bytes, type = sniffType(bytes)) {
    const b = new Uint8Array(bytes);
    try {
      if (type === 'image/png' && b.length >= 24) {
        const view = new DataView(b.buffer, b.byteOffset, b.byteLength);
        return { w: view.getUint32(16), h: view.getUint32(20) };
      }
      if (type === 'image/jpeg') {
        let i = 2;
        while (i + 9 < b.length) {
          if (b[i] !== 0xff) { i++; continue; }
          const marker = b[i + 1];
          if (marker === 0xff) { i++; continue; }
          if (marker === 0x01 || marker >= 0xd0 && marker <= 0xd9) { i += 2; continue; }
          if (marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
            return { h: b[i + 5] << 8 | b[i + 6], w: b[i + 7] << 8 | b[i + 8] };
          }
          i += 2 + (b[i + 2] << 8 | b[i + 3]);
        }
        return null;
      }
      if (type === 'image/webp' && b.length >= 30) {
        const tag = String.fromCharCode(b[12], b[13], b[14], b[15]);
        if (tag === 'VP8 ') return { w: (b[26] | b[27] << 8) & 0x3fff, h: (b[28] | b[29] << 8) & 0x3fff };
        if (tag === 'VP8X') return { w: 1 + (b[24] | b[25] << 8 | b[26] << 16), h: 1 + (b[27] | b[28] << 8 | b[29] << 16) };
        if (tag === 'VP8L') { const v = b[21] | b[22] << 8 | b[23] << 16 | b[24] << 24; return { w: 1 + (v & 0x3fff), h: 1 + (v >> 14 & 0x3fff) }; }
      }
    } catch (_) { /* fall through */ }
    return null;
  }

  async function fetchPhoto(url, signal) {
    const response = await root.fetch(url, { signal });
    if (!response.ok) return { ok: false, status: response.status };
    return { ok: true, status: 200, bytes: await response.arrayBuffer() };
  }

  const BATCH_MAGIC = 0x3148504d;   // 'MPH1' read little-endian
  const DEFERRED = 0xffffffff;

  // Split a /api/map/photos body into one result per requested url, in order.
  function parseBatch(buffer, urls) {
    const view = new DataView(buffer);
    if (buffer.byteLength < 8 || view.getUint32(0, true) !== BATCH_MAGIC) throw new Error('photos');
    const count = view.getUint32(4, true);
    if (8 + count * 8 > buffer.byteLength) throw new Error('photos');
    const found = new Map(); let offset = 8 + count * 8;
    for (let i = 0; i < count; i++) {
      const id = view.getUint32(8 + i * 8, true), length = view.getUint32(12 + i * 8, true);
      if (length === DEFERRED) found.set(id, { ok: false, deferred: true });
      else if (length === 0) found.set(id, { ok: false, status: 404 });
      else {
        if (offset + length > buffer.byteLength) throw new Error('photos');
        found.set(id, { ok: true, status: 200, bytes: buffer.slice(offset, offset + length) });
        offset += length;
      }
    }
    return urls.map((url) => found.get(Number(url.slice(5))) || { ok: false, deferred: true });
  }

  async function fetchPhotoBatch(urls, signal) {
    const response = await root.fetch('/api/map/photos?ids=' + urls.map((url) => url.slice(5)).join(','), { signal });
    if (!response.ok) { const failure = new Error('photos'); failure.status = response.status; throw failure; }
    return parseBatch(await response.arrayBuffer(), urls);
  }

  // Decode to a pixels-by-pixels square, cropping the centre when the photo is not square.
  async function decodePhoto(bytes, pixels) {
    const blob = new root.Blob([bytes], { type: sniffType(bytes) });
    const resize = { resizeWidth: pixels, resizeHeight: pixels, resizeQuality: 'medium' };
    const size = imageSize(bytes);
    if (size && size.w > 0 && size.w === size.h) return root.createImageBitmap(blob, resize);
    const full = await root.createImageBitmap(blob);
    try {
      const side = Math.min(full.width, full.height);
      return await root.createImageBitmap(full, Math.floor((full.width - side) / 2), Math.floor((full.height - side) / 2), side, side, resize);
    } finally { full.close?.(); }
  }

  const FIRST_BATCH = 16;          // the nearest photos, so something appears at once
  const BATCH = 48;                // later requests: large enough to amortise a request, small enough to stay even
  const MAX_REQUESTS = 3;          // batches in flight; the browser allows six connections to this server
  const SINGLE_REQUESTS = 6;       // one-photo requests in flight when the batch route is missing
  const RETRY_AFTER_MS = 250;
  // How many photos PortraitCache should have started at once so every request slot stays full.
  const CACHE_CONCURRENCY = FIRST_BATCH + BATCH * MAX_REQUESTS;

  // Behaves like the part of an <img> that PortraitCache uses: assign src, receive onload/onerror.
  class TransportImage {
    constructor(transport) { this.transport = transport; this.onload = this.onerror = null; this.naturalWidth = 0; this.bitmap = null; this._src = ''; this.decoding = 'async'; }
    get src() { return this._src; }
    set src(url) {
      if (this._src === url) return;
      this._src = url;
      if (url) this.transport.enqueue(this); else this.transport.drop(this);
    }
  }

  class PhotoTransport {
    constructor(options = {}) {
      this.batch = options.fetchBatch === undefined ? fetchPhotoBatch : options.fetchBatch;
      this.single = options.fetchPhoto || fetchPhoto;
      this.decode = options.decode || decodePhoto;
      this.pixelsFor = options.pixelsFor || (() => 64);
      this.wait = options.wait || ((ms) => new Promise((resolve) => root.setTimeout(resolve, ms)));
      this.pending = [];     // images waiting for a request, nearest first
      this.requests = 0;     // requests in flight
      this.scheduled = false;
      this.stats = { requests: 0, photos: 0, failed: 0, bytes: 0 };
    }

    createImage() { return new TransportImage(this); }

    enqueue(image) {
      this.pending.push(image);
      if (!this.scheduled) { this.scheduled = true; Promise.resolve().then(() => { this.scheduled = false; this.pump(); }); }
    }

    drop(image) {
      const at = this.pending.indexOf(image);
      if (at >= 0) this.pending.splice(at, 1);
    }

    // A short request is only worth sending when nothing else is on the way: otherwise the photos
    // wait for the next completion and travel together with the ones PortraitCache starts then.
    pump() {
      const limit = this.batch ? MAX_REQUESTS : SINGLE_REQUESTS;
      while (this.requests < limit && this.pending.length) {
        const size = this.batch ? (this.requests === 0 ? FIRST_BATCH : BATCH) : 1;
        if (this.requests > 0 && this.pending.length < size) break;
        this.send(this.pending.splice(0, size));
      }
    }

    async send(images) {
      this.requests++; this.stats.requests++;
      let results;
      try { results = await this.fetchAll(images.map((image) => image.src)); }
      catch (error) { results = null; if (error && error.status === 404 && this.batch) { this.batch = null; this.pending.unshift(...images); this.requests--; this.pump(); return; } }
      this.requests--;
      images.forEach((image, i) => this.arrive(image, results && results[i]));
      this.pump();
    }

    // One attempt, then one more after a short wait: a connection reset is not a missing photo.
    async fetchAll(urls) {
      for (let attempt = 0; ; attempt++) {
        try {
          if (this.batch) return await this.batch(urls);
          return [await this.single(urls[0])];
        } catch (error) {
          if (attempt || error && error.status >= 400 && error.status < 500) throw error;
          await this.wait(RETRY_AFTER_MS);
        }
      }
    }

    arrive(image, result) {
      if (image.src === '') return;
      if (result && result.deferred) { this.enqueue(image); return; }
      if (!result || !result.ok) { this.stats.failed++; image.onerror && image.onerror(); return; }
      this.stats.photos++; this.stats.bytes += result.bytes.byteLength || 0;
      Promise.resolve().then(() => this.decode(result.bytes, this.pixelsFor(image.src))).then((bitmap) => {
        if (image.src === '') { bitmap && bitmap.close && bitmap.close(); return; }
        image.bitmap = bitmap; image.naturalWidth = bitmap.width || 1;
        image.onload && image.onload();
      }, () => { this.stats.failed++; image.onerror && image.onerror(); });
    }
  }

  const api = { PhotoTransport, TransportImage, CACHE_CONCURRENCY, imageSize, sniffType, decodePhoto, fetchPhoto, fetchPhotoBatch, parseBatch };
  root.MapPhotos = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window === 'undefined' ? globalThis : window);
