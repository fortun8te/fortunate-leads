/* Bounded tile streaming for the precomputed connection universe. No graph-wide objects. */
(function (root) {
  "use strict";
  const STRIDE = 32,
    HEADER = 16,
    GPU_STRIDE = 40;
  function parseTile(buffer, maxRecords = 500000) {
    if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < HEADER)
      throw Error("Invalid map tile");
    const d = new DataView(buffer);
    if (
      d.getUint32(0, true) !== 0x3156554d ||
      d.getUint32(8, true) !== STRIDE ||
      d.getUint32(12, true) !== 0
    )
      throw Error("Unsupported map tile");
    const count = d.getUint32(4, true);
    if (count > maxRecords || buffer.byteLength !== HEADER + count * STRIDE)
      throw Error("Invalid map tile size");
    for (let i = 0; i < count; i++) {
      const p = HEADER + i * STRIDE;
      for (const o of [4, 8, 12])
        if (!Number.isFinite(d.getFloat32(p + o, true)))
          throw Error("Invalid map position");
      if (d.getFloat32(p + 12, true) < 0) throw Error("Invalid map radius");
    }
    return {
      buffer,
      view: d,
      count,
      bytes: buffer.byteLength + count * (GPU_STRIDE * 2 + 4) + 4096,
    };
  }
  function transformFor(manifest) {
    const rawBounds = manifest.bounds || {};
    const b = Array.isArray(rawBounds)
        ? {
            x0: rawBounds[0],
            y0: rawBounds[1],
            x1: rawBounds[2],
            y1: rawBounds[3],
          }
        : rawBounds,
      a = manifest.anchor || { x: 0, y: 0 };
    const span =
      Math.max(
        1,
        Math.abs((b.x0 ?? b.min_x ?? -1) - a.x),
        Math.abs((b.x1 ?? b.max_x ?? 1) - a.x),
        Math.abs((b.y0 ?? b.min_y ?? -1) - a.y),
        Math.abs((b.y1 ?? b.max_y ?? 1) - a.y),
      ) * 2.2;
    return { x: a.x, y: a.y, span };
  }
  function record(tile, i, t) {
    const d = tile.view,
      p = HEADER + i * STRIDE,
      id = d.getUint32(p, true),
      flags = d.getUint16(p + 22, true),
      hop = d.getUint16(p + 20, true);
    return {
      id,
      compact: true,
      handle: String(id),
      x: 0.5 + (d.getFloat32(p + 4, true) - t.x) / t.span,
      y: 0.5 + (d.getFloat32(p + 8, true) - t.y) / t.span,
      portraitRadius: d.getFloat32(p + 12, true) / t.span,
      followers: flags & 4 ? d.getUint32(p + 16, true) : null,
      hop: hop === 65535 ? null : hop,
      distance_state: flags & 2 ? "reachable" : "unknown",
      flags,
      pic: "/img/" + id,
    };
  }
  function pack(tile, t) {
    const out = new Float32Array(tile.count * 10),
      d = tile.view;
    for (let i = 0; i < tile.count; i++) {
      const p = HEADER + i * STRIDE,
        o = i * 10,
        hop = d.getUint16(p + 20, true),
        flags = d.getUint16(p + 22, true);
      out[o] = 0.5 + (d.getFloat32(p + 4, true) - t.x) / t.span;
      out[o + 1] = 0.5 + (d.getFloat32(p + 8, true) - t.y) / t.span;
      out[o + 2] = d.getFloat32(p + 12, true) / t.span;
      const c =
        flags & 1
          ? 0.1
          : hop === 1
            ? 0.28
            : hop === 2
              ? 0.43
              : hop === 65535
                ? 0.72
                : 0.57;
      out.set([c, c, c, 1, -1, 0, 0], o + 3);
    }
    return out;
  }
  function buildIndex(tile) {
    const p = tile.packed;
    let x0 = Infinity,
      y0 = Infinity,
      x1 = -Infinity,
      y1 = -Infinity,
      r = 0;
    for (let i = 0; i < tile.count; i++) {
      const o = i * 10;
      x0 = Math.min(x0, p[o]);
      x1 = Math.max(x1, p[o]);
      y0 = Math.min(y0, p[o + 1]);
      y1 = Math.max(y1, p[o + 1]);
      r = Math.max(r, p[o + 2]);
    }
    const heads = new Int32Array(1024).fill(-1),
      next = new Int32Array(tile.count),
      dx = Math.max(1e-9, x1 - x0) / 32,
      dy = Math.max(1e-9, y1 - y0) / 32;
    for (let i = 0; i < tile.count; i++) {
      const o = i * 10,
        x = Math.min(31, Math.floor((p[o] - x0) / dx)),
        y = Math.min(31, Math.floor((p[o + 1] - y0) / dy)),
        cell = y * 32 + x;
      next[i] = heads[cell];
      heads[cell] = i;
    }
    tile.index = { x0, y0, x1, y1, dx, dy, r, heads, next };
  }

  class ByteCache {
    constructor(limit, onEvict = () => {}) {
      this.limit = limit;
      this.bytes = 0;
      this.items = new Map();
      this.onEvict = onEvict;
    }
    get(key) {
      const v = this.items.get(key);
      if (v) {
        this.items.delete(key);
        this.items.set(key, v);
      }
      return v;
    }
    set(key, value, pinned = new Set()) {
      if (value.bytes > this.limit) return false;
      this.delete(key);
      this.items.set(key, value);
      this.bytes += value.bytes;
      for (const k of this.items.keys()) {
        if (this.bytes <= this.limit) break;
        if (!pinned.has(k)) this.delete(k);
      }
      if (this.bytes > this.limit) {
        this.delete(key);
        return false;
      }
      return true;
    }
    delete(key) {
      const v = this.items.get(key);
      if (v) {
        this.items.delete(key);
        this.bytes -= v.bytes;
        this.onEvict(v);
      }
    }
    clear() {
      for (const k of [...this.items.keys()]) this.delete(k);
    }
  }
  class Universe {
    constructor(o = {}) {
      this.o = o;
      this.fetchJson =
        o.fetchJson ||
        ((u, opt) =>
          fetch(u, opt).then((r) => {
            if (!r.ok) throw Error("Map unavailable");
            return r.json();
          }));
      this.fetchBinary =
        o.fetchBinary ||
        ((u, opt) =>
          fetch(u, opt).then((r) => {
            if (!r.ok) throw Error("Map unavailable");
            return r.arrayBuffer();
          }));
      this.maxVisibleRecords = o.maxVisibleRecords || 250000;
      this.cache = new ByteCache(
        o.byteBudget || 64 * 1024 * 1024,
        (v) => (this.atlas?.releaseTile(v), this.renderer?.remove(v)),
      );
      this.visible = new Set();
      this.generation = 0;
      this.disposed = false;
      this.owner = null;
    }
    async mount(container, { camera, owner } = {}) {
      this.container = container;
      this.camera = camera;
      this.owner = owner || null;
      this.manifest =
        this.o.manifest || (await this.fetchJson("/api/map/universe/manifest"));
      if (!this.manifest.available || this.disposed) return false;
      this.transform = transformFor(this.manifest);
      this.owner = this.owner || this.manifest.owner || null;
      if (this.owner && this.owner.id == null)
        this.owner = { ...this.owner, id: this.owner.person_id };
      this.canvas = root.document.createElement("canvas");
      this.canvas.className = "map-universe-canvas";
      Object.assign(this.canvas.style, {
        width: "100%",
        height: "100%",
        position: "absolute",
        inset: "0",
        pointerEvents: "none",
      });
      container.appendChild(this.canvas);
      this.o.onCanvas?.(this.canvas);
      this.renderer = await root.MapUniverseGPU.create(this.canvas, {
        onFailure: (e) => this.fallback(e),
      });
      if (this.disposed) {
        this.renderer.dispose();
        this.canvas.remove();
        return false;
      }
      if (this.renderer.replacementCanvas) {
        this.canvas = this.renderer.replacementCanvas;
        this.o.onCanvas?.(this.canvas);
      }
      this.atlas = new root.MapUniverseGPU.Atlas(
        this.renderer,
        () => this.o.onChange?.(),
        { fetchBinary: this.fetchBinary },
      );
      this.makeOwner();
      await this.updateCamera(camera);
      return true;
    }
    async fallback(error) {
      if (this.disposed || this.fallingBack) return;
      this.fallingBack = true;
      try {
        if (this.ownerTile) this.renderer?.remove(this.ownerTile);
        this.ownerTile = null;
        for (const tile of this.cache.items.values())
          this.renderer?.remove(tile);
        this.renderer?.dispose();
        const next = this.canvas.cloneNode(false);
        this.canvas.replaceWith(next);
        this.canvas = next;
        this.o.onCanvas?.(next);
        this.renderer = await root.MapUniverseGPU.create(next, {
          webglOnly: true,
          onFailure: (e) => this.fallback(e),
        });
        this.atlas?.dispose();
        this.atlas = new root.MapUniverseGPU.Atlas(
          this.renderer,
          () => this.o.onChange?.(),
          { fetchBinary: this.fetchBinary },
        );
        for (const tile of this.cache.items.values()) {
          tile.gpu = null;
          tile.packed = pack(tile, this.transform);
          this.renderer.upload(tile);
        }
        this.makeOwner();
        this.portraitKey = null;
        this.o.onChange?.();
      } catch (e) {
        this.o.onError?.(e || error);
      } finally {
        this.fallingBack = false;
      }
    }
    rawRect(cam, pad = 0) {
      const r = cam.rect(pad),
        t = this.transform;
      return {
        x0: (r.x0 - 0.5) * t.span + t.x,
        y0: (r.y0 - 0.5) * t.span + t.y,
        x1: (r.x1 - 0.5) * t.span + t.x,
        y1: (r.y1 - 0.5) * t.span + t.y,
      };
    }
    async updateCamera(camera = this.camera) {
      if (!camera || !this.manifest || this.disposed || this.paused) return;
      this.camera = camera;
      const generation = ++this.generation;
      this.controller?.abort();
      const ctl = (this.controller = new AbortController()),
        signal = ctl.signal;
      try {
        const wanted = new Set();
        let records = 0,
          bytes = 0;
        this.visible = wanted;
        const live = () =>
          !signal.aborted && generation === this.generation && !this.disposed;
        const loadPage = async (metadata) => {
          const tiles = metadata
            .slice()
            .sort(
              (a, b) =>
                (a.priority ?? a.hop_min ?? 65535) -
                (b.priority ?? b.hop_min ?? 65535),
            );
          const admitted = [];
          for (const meta of tiles) {
            const id = String(meta.id ?? meta.tile_id),
              n = meta.record_count ?? meta.count ?? 0;
            if (wanted.has(id)) continue;
            const size = HEADER + n * (STRIDE + GPU_STRIDE * 2 + 4) + 4096;
            if (
              records + n > this.maxVisibleRecords ||
              bytes + size > this.cache.limit
            )
              continue;
            wanted.add(id);
            records += n;
            bytes += size;
            admitted.push(meta);
          }
          this.o.onChange?.();
          let cursor = 0;
          const worker = async () => {
            while (cursor < admitted.length && live()) {
              const meta = admitted[cursor++],
                id = String(meta.id ?? meta.tile_id);
              if (this.cache.get(id)) continue;
              const tile = parseTile(
                await this.fetchBinary(meta.url, { signal }),
                this.maxVisibleRecords,
              );
              if (!live()) return;
              if (
                tile.count !== (meta.record_count ?? meta.count ?? tile.count)
              )
                throw Error("Invalid map tile count");
              tile.id = id;
              tile.packed = pack(tile, this.transform);
              buildIndex(tile);
              if (this.cache.set(id, tile, wanted)) {
                this.renderer.upload(tile);
                this.o.onChange?.();
              }
            }
          };
          await Promise.all([worker(), worker()]);
        };
        if (!this.warmed && this.manifest.tiles?.length) {
          this.warmed = true;
          await loadPage(this.manifest.tiles);
          if (!live()) return;
        }
        const rect = this.rawRect(camera, 0.08);
        let cursor = null,
          pages = 0;
        const seenCursors = new Set();
        do {
          const q = new URLSearchParams({
            ...rect,
            budget: Math.min(65536, this.maxVisibleRecords),
            version: this.manifest.version,
          });
          if (cursor != null) q.set("cursor", cursor);
          const selection = await this.fetchJson(
            "/api/map/universe/view?" + q,
            { signal },
          );
          if (!live()) return;
          if (selection.available === false)
            throw Error("Map unavailable. Reload the map.");
          if (
            selection.version != null &&
            String(selection.version) !== String(this.manifest.version)
          )
            throw Error("Map changed. Reload the map.");
          await loadPage(selection.tiles || []);
          cursor = selection.next_cursor;
          if (cursor != null) {
            if (seenCursors.has(String(cursor)))
              throw Error("Map page repeated");
            seenCursors.add(String(cursor));
          }
          pages++;
        } while (
          live() &&
          cursor != null &&
          records < this.maxVisibleRecords &&
          bytes < this.cache.limit - 4096 &&
          pages < 64
        );
      } catch (e) {
        if (!signal.aborted) {
          ctl.abort();
          this.o.onError?.(e);
        }
      }
    }
    render(camera = this.camera) {
      if (!this.renderer || !camera || this.disposed || this.paused) return;
      const tiles = [];
      if (this.ownerTile) tiles.push(this.ownerTile);
      for (const id of this.visible) {
        const t = this.cache.items.get(id);
        if (t) tiles.push(t);
      }
      const drawTiles = tiles.filter((t) => !t.owner);
      if (this.ownerTile) drawTiles.push(this.ownerTile);
      this.renderer.render(drawTiles, camera);
      this.schedulePortraits(tiles, camera);
    }
    schedulePortraits(tiles, cam) {
      const key = [
        cam.cx,
        cam.cy,
        cam.k,
        cam.w,
        cam.h,
        ...tiles.map((t) => t.id ?? "owner"),
      ].join(":");
      const limit = Math.min(this.atlas.maxSlots ?? 1024, 1024);
      if (key !== this.portraitKey) {
        this.portraitKey = key;
        this.portraitCandidates = [];
        const ids = new Set(),
          rect = cam.rect(),
          center = cam.mid;
        // Bounded max heap: inspect visible indexed records, retain the nearest
        // faces regardless of tile/scanline order without a graph-wide array.
        const nearest = this.portraitCandidates;
        const admit = (tile, i, id, distance = -1) => {
          if (ids.has(id)) return;
          const candidate = { tile, offset: i * 10 + 7, id, distance };
          if (nearest.length < limit) {
            ids.add(id);
            nearest.push(candidate);
            let child = nearest.length - 1;
            while (child > 0) {
              const parent = (child - 1) >> 1;
              if (nearest[parent].distance >= candidate.distance) break;
              nearest[child] = nearest[parent];
              child = parent;
            }
            nearest[child] = candidate;
          } else if (distance < nearest[0].distance) {
            ids.delete(nearest[0].id);
            ids.add(id);
            let parent = 0;
            while (parent * 2 + 1 < nearest.length) {
              let child = parent * 2 + 1;
              if (child + 1 < nearest.length && nearest[child + 1].distance > nearest[child].distance)
                child++;
              if (nearest[child].distance <= distance) break;
              nearest[parent] = nearest[child];
              parent = child;
            }
            nearest[parent] = candidate;
          }
        };
        if (this.ownerTile && this.owner)
          admit(this.ownerTile, 0, this.owner.id);
        for (const tile of tiles) {
          if (tile.owner) continue;
          const g = tile.index;
          if (!g || g.r * cam.scale < 6) continue;
          const a = Math.max(0, Math.floor((rect.x0 - g.r - g.x0) / g.dx)),
            b = Math.min(31, Math.floor((rect.x1 + g.r - g.x0) / g.dx)),
            c = Math.max(0, Math.floor((rect.y0 - g.r - g.y0) / g.dy)),
            d = Math.min(31, Math.floor((rect.y1 + g.r - g.y0) / g.dy));
          for (let cy = c; cy <= d; cy++)
            for (let cx = a; cx <= b; cx++)
              for (
                let i = g.heads[cy * 32 + cx];
                i >= 0;
                i = g.next[i]
              ) {
                const o = i * 10,
                  r = tile.packed[o + 2] * cam.scale;
                if (r < 6) continue;
                const [x, y] = cam.toScreen(tile.packed[o], tile.packed[o + 1]);
                if (x + r < 0 || y + r < 0 || x - r > cam.w || y - r > cam.h)
                  continue;
                admit(tile, i, tile.view.getUint32(HEADER + i * STRIDE, true),
                  (x - center[0]) ** 2 + (y - center[1]) ** 2);
              }
        }
        nearest.sort((a, b) => a.distance - b.distance);
        this.atlas.setPins(ids);
      }
      for (const { tile, offset, id } of this.portraitCandidates) {
        const ready = this.atlas.request(id);
        if (ready != null) {
          const slot = this.atlas.bind(id, tile, offset);
          if (tile.packed[offset] !== slot) {
            tile.packed[offset] = slot;
            this.renderer.update(
              tile,
              offset * 4,
              tile.packed.subarray(offset, offset + 1),
            );
          }
        }
      }
    }
    pick(px, py) {
      if (!this.camera || !this.transform) return null;
      const cam = this.camera,
        [x, y] = cam.toWorld(px, py);
      if (
        this.owner &&
        Math.hypot(...cam.toScreen(0.5, 0.5).map((v, i) => v - [px, py][i])) <=
          13
      )
        return {
          ...this.owner,
          x: 0.5,
          y: 0.5,
          portraitRadius: 13 / cam.scale,
          owner: true,
        };
      let best = null,
        bestD = Infinity;
      for (const id of this.visible) {
        const tile = this.cache.items.get(id);
        if (!tile?.index) continue;
        const g = tile.index,
          r = Math.max(3 / cam.scale, g.r);
        if (x + r < g.x0 || x - r > g.x1 || y + r < g.y0 || y - r > g.y1)
          continue;
        const a = Math.max(0, Math.floor((x - r - g.x0) / g.dx)),
          b = Math.min(31, Math.floor((x + r - g.x0) / g.dx)),
          c = Math.max(0, Math.floor((y - r - g.y0) / g.dy)),
          d = Math.min(31, Math.floor((y + r - g.y0) / g.dy));
        for (let cy = c; cy <= d; cy++)
          for (let cx = a; cx <= b; cx++)
            for (let i = g.heads[cy * 32 + cx]; i >= 0; i = g.next[i]) {
              const o = i * 10,
                dx = (tile.packed[o] - x) * cam.scale,
                dy = (tile.packed[o + 1] - y) * cam.scale,
                distance = dx * dx + dy * dy,
                hitR = Math.max(3, tile.packed[o + 2] * cam.scale);
              if (distance <= hitR * hitR && distance < bestD) {
                bestD = distance;
                best = record(tile, i, this.transform);
              }
            }
      }
      return best;
    }
    normalize(x, y) {
      return {
        x: 0.5 + (x - this.transform.x) / this.transform.span,
        y: 0.5 + (y - this.transform.y) / this.transform.span,
      };
    }
    pause(value = true) {
      this.paused = value;
      this.atlas?.pause(value);
      if (value) {
        this.controller?.abort();
        this.generation++;
      } else return this.updateCamera();
    }
    stats() {
      let loaded = 0;
      for (const id of this.visible)
        loaded += this.cache.items.get(id)?.count || 0;
      return {
        loaded,
        total:
          this.manifest?.node_count ??
          this.manifest?.total_records ??
          this.manifest?.total ??
          0,
        residentBytes: this.cache.bytes,
        renderer:
          this.renderer instanceof root.MapUniverseGPU.GPURenderer
            ? "webgpu"
            : "webgl2",
      };
    }
    makeOwner() {
      if (this.ownerTile) this.renderer.remove(this.ownerTile);
      if (!this.owner) return;
      this.ownerTile = {
        owner: true,
        count: 1,
        packed: new Float32Array([0.5, 0.5, 0, 0.1, 0.1, 0.1, -1, -1, 0, 0]),
      };
      this.renderer.upload(this.ownerTile);
      this.atlas?.request(this.owner.id);
    }
    position(id) {
      if (this.owner && Number(id) === Number(this.owner.id))
        return { ...this.owner, x: 0.5, y: 0.5, owner: true };
      for (const tile of this.cache.items.values())
        for (let i = 0; i < tile.count; i++)
          if (tile.view.getUint32(HEADER + i * STRIDE, true) === Number(id))
            return record(tile, i, this.transform);
      return null;
    }
    async locate(id, { signal } = {}) {
      const result = await this.fetchJson(
        "/api/map/universe/person?id=" +
          encodeURIComponent(id) +
          "&version=" +
          encodeURIComponent(this.manifest.version),
        { signal },
      );
      if (
        result.version != null &&
        String(result.version) !== String(this.manifest.version)
      )
        throw Error("Map changed. Reload the map.");
      const person = result.person || result;
      if (person.x == null || person.y == null) return null;
      return {
        ...person,
        id: person.id ?? person.person_id,
        ...this.normalize(person.x, person.y),
        portraitRadius: (person.r ?? person.radius ?? 0) / this.transform.span,
      };
    }
    async select(id) {
      const person = await this.fetchJson(
        "/api/person/" + encodeURIComponent(id),
      );
      if (!this.disposed) this.o.onSelect?.(person);
      return person;
    }
    setOwner(owner) {
      this.owner = owner;
      this.portraitKey = null;
      if (this.renderer) this.makeOwner();
      this.o.onChange?.();
    }
    dispose() {
      this.disposed = true;
      this.generation++;
      this.controller?.abort();
      this.atlas?.dispose();
      this.cache.clear();
      if (this.ownerTile) this.renderer?.remove(this.ownerTile);
      this.renderer?.dispose();
      this.canvas?.remove();
    }
  }
  const api = {
    Universe,
    parseTile,
    pack,
    record,
    transformFor,
    ByteCache,
    buildIndex,
    STRIDE,
    HEADER,
    GPU_STRIDE,
  };
  root.MapUniverse = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
