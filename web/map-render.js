/* Map rendering helpers that need no DOM, so they can be tested and reused.
 *
 *   SpriteAtlas    circular portrait sprites packed into a few canvases, in three resolutions.
 *                  One drawImage per portrait instead of save/clip/fill/draw/restore.
 *   CircleGrid     a spatial hash of portraits in world space for picking and label clearance,
 *                  so pointer moves never scan every portrait.
 *   PortraitLayer  the settled picture of every portrait, kept in an offscreen canvas. Panning
 *                  only moves it; it is redrawn when the camera zooms, the scene or the
 *                  selection changes, or the pan runs past its margin.
 *   FrameClock     requests at most one animation frame, and says why it was asked for. */
(function (root) {
  'use strict';
  const TAU = Math.PI * 2;

  /* ---------- Sprites ---------- */
  // Three resolutions so a portrait is never shrunk by more than 2x. Sampling a 64 px photo down to
  // 12 px shimmers while you move; picking the nearest larger level does not.
  const LEVELS = Object.freeze([64, 32, 16]);
  class SpriteAtlas {
    constructor({ makeCanvas, levels = LEVELS, side = 32, max = 4096 } = {}) {
      this.makeCanvas = makeCanvas; this.levels = levels; this.side = side; this.per = side * side; this.max = max;
      this.pages = levels.map(() => []);
      this.slots = new Map();      // url -> slot
      this.owner = [];             // slot -> url, for eviction
      this.free = []; this.count = 0; this.epoch = 0;
    }
    get size() { return this.slots.size; }
    slotOf(url) { const s = this.slots.get(url); return s === undefined ? -1 : s; }
    // Level whose cell is the smallest one at least as large as the portrait on screen (device pixels).
    levelFor(diameter) { return diameter > this.levels[1] ? 0 : diameter > this.levels[2] ? 1 : 2; }
    page(level, index) {
      const pages = this.pages[level];
      if (!pages[index]) {
        const canvas = this.makeCanvas(this.side * this.levels[level], this.side * this.levels[level]);
        // A restored 2D canvas comes back empty. Start over and let the photos be drawn in again.
        if (canvas.addEventListener) canvas.addEventListener('contextrestored', () => { this.reset(); if (this.onReset) this.onReset(); });
        pages[index] = { canvas, ctx: canvas.getContext('2d') };
      }
      return pages[index];
    }
    // Draw an image into a circular cell. Returns the slot, or -1 when the atlas is full of pinned sprites.
    add(url, image, pinned) {
      let slot = this.slots.get(url);
      if (slot !== undefined) return slot;
      if (this.free.length) slot = this.free.pop();
      else if (this.count < this.max) slot = this.count++;
      else {
        let victim = -1;
        for (const [key, s] of this.slots) if (!pinned || !pinned.has(key)) { victim = s; this.slots.delete(key); break; }
        if (victim < 0) return -1;
        slot = victim;
      }
      const iw = image.naturalWidth || image.width, ih = image.naturalHeight || image.height;
      if (!iw || !ih) { this.free.push(slot); return -1; }
      const crop = Math.min(iw, ih), sx = (iw - crop) / 2, sy = (ih - crop) / 2, col = slot % this.side, row = (slot / this.side | 0) % this.side, index = Math.floor(slot / this.per);
      for (let level = 0; level < this.levels.length; level++) {
        const size = this.levels[level], { ctx } = this.page(level, index), x = col * size, y = row * size;
        ctx.clearRect(x, y, size, size);
        ctx.save(); ctx.beginPath(); ctx.arc(x + size / 2, y + size / 2, size / 2 - 1, 0, TAU); ctx.clip();
        ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
        ctx.drawImage(image, sx, sy, crop, crop, x + 1, y + 1, size - 2, size - 2);
        ctx.restore();
      }
      this.slots.set(url, slot); this.owner[slot] = url; this.epoch++;
      return slot;
    }
    remove(url) { const s = this.slots.get(url); if (s === undefined) return; this.slots.delete(url); this.owner[s] = null; this.free.push(s); this.epoch++; }
    // Draw the portrait in `slot` centred at (x, y) with radius r (all in the context's current units).
    draw(ctx, slot, level, x, y, r) {
      const size = this.levels[level], col = slot % this.side, row = (slot / this.side | 0) % this.side;
      ctx.drawImage(this.pages[level][Math.floor(slot / this.per)].canvas, col * size + 1, row * size + 1, size - 2, size - 2, x - r, y - r, r * 2, r * 2);
    }
    reset() { this.pages.forEach((p) => p.length = 0); this.slots.clear(); this.owner.length = 0; this.free.length = 0; this.count = 0; this.epoch++; }
    dispose() { this.reset(); }
  }

  /* ---------- Picking in world space ---------- */
  class CircleGrid {
    constructor(cell = 1 / 64) { this.cell = cell; this.cells = new Map(); this.maxR = 0; this.count = 0; }
    key(cx, cy) { return (cx + 4096) * 8192 + (cy + 4096); }
    // items: objects with world x, y and a radius from radiusOf(item).
    build(items, radiusOf) {
      this.cells.clear(); this.maxR = 0; this.count = items.length;
      const c = this.cell;
      for (let i = 0; i < items.length; i++) {
        const it = items[i], k = this.key(Math.floor(it.x / c), Math.floor(it.y / c));
        let list = this.cells.get(k); if (!list) this.cells.set(k, list = []);
        list.push(it);
        const r = radiusOf(it); if (r > this.maxR) this.maxR = r;
      }
      return this;
    }
    // Calls fn(item) for every item whose centre may lie within `reach` of (wx, wy).
    near(wx, wy, reach, fn) {
      const c = this.cell, pad = reach + this.maxR;
      const x0 = Math.floor((wx - pad) / c), x1 = Math.floor((wx + pad) / c), y0 = Math.floor((wy - pad) / c), y1 = Math.floor((wy + pad) / c);
      for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
        const list = this.cells.get(this.key(x, y)); if (!list) continue;
        for (let i = 0; i < list.length; i++) fn(list[i]);
      }
    }
  }

  /* ---------- Settled-frame layer ---------- */
  class PortraitLayer {
    constructor({ makeCanvas, margin = 96 } = {}) {
      this.makeCanvas = makeCanvas; this.margin = margin; this.canvas = null; this.ctx = null;
      this.valid = false; this.cx = 0; this.cy = 0; this.scale = 0; this.w = 0; this.h = 0; this.dpr = 0; this.key = '';
    }
    invalidate() { this.valid = false; }
    // Can the layer stand in for a fresh draw at this camera? Only a translation is allowed.
    usable(cam, dpr, key) {
      if (!this.valid || this.key !== key || this.dpr !== dpr || this.w !== cam.w || this.h !== cam.h) return false;
      if (Math.abs(cam.scale / this.scale - 1) > 1e-6) return false;
      const m = cam.mid, lm = this.mid;
      if (Math.abs(m[0] - lm[0]) > 1e-6 || Math.abs(m[1] - lm[1]) > 1e-6) return false;
      const dx = (this.cx - cam.cx) * cam.scale, dy = (this.cy - cam.cy) * cam.scale;
      return Math.abs(dx) <= this.margin - 1 && Math.abs(dy) <= this.margin - 1;
    }
    // Prepare to draw into the layer. The caller draws in screen coordinates and passes tx/ty, the
    // offset that places the screen origin inside the margin.
    begin(cam, dpr, key) {
      const m = this.margin, w = Math.ceil((cam.w + m * 2) * dpr), h = Math.ceil((cam.h + m * 2) * dpr);
      if (!this.canvas || this.canvas.width !== w || this.canvas.height !== h) {
        this.canvas = this.makeCanvas(w, h); this.ctx = this.canvas.getContext('2d');
      }
      const ctx = this.ctx;
      ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, w, h); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.cx = cam.cx; this.cy = cam.cy; this.scale = cam.scale; this.w = cam.w; this.h = cam.h; this.dpr = dpr; this.key = key; this.mid = cam.mid.slice();
      this.valid = true;
      return ctx;
    }
    // Draw the layer so it lines up with the camera. Offsets snap to whole device pixels, so a pure
    // pan copies pixels instead of resampling them.
    blit(ctx, cam) {
      const m = this.margin, dpr = this.dpr;
      const dx = Math.round(((this.cx - cam.cx) * cam.scale - m) * dpr) / dpr, dy = Math.round(((this.cy - cam.cy) * cam.scale - m) * dpr) / dpr;
      ctx.drawImage(this.canvas, dx, dy, this.canvas.width / dpr, this.canvas.height / dpr);
    }
    dispose() { this.canvas = this.ctx = null; this.valid = false; }
  }

  /* ---------- Frame requests ---------- */
  // One pending animation frame at most. `dirty` says a repaint is needed; `wake()` asks for a frame
  // that may only process input (hover), so moving the pointer does not repaint the map.
  class FrameClock {
    constructor(request, cancel = () => {}) {
      this.request = request; this.cancel = cancel; this.id = 0; this.dirty = false; this.frames = 0; this.reasons = Object.create(null);
    }
    get pending() { return !!this.id; }
    invalidate(reason = 'other') { this.dirty = true; this.reasons[reason] = (this.reasons[reason] || 0) + 1; this.schedule(); }
    wake() { this.schedule(); }
    schedule() { if (!this.id) this.id = this.request((t) => this.run(t)); }
    run(t) { this.id = 0; this.frames++; if (this.onFrame) this.onFrame(t); }
    take() { const d = this.dirty; this.dirty = false; return d; }
    stop() { if (this.id) this.cancel(this.id); this.id = 0; }
  }

  const api = { SpriteAtlas, CircleGrid, PortraitLayer, FrameClock, LEVELS };
  root.MapRender = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window === 'undefined' ? globalThis : window);
