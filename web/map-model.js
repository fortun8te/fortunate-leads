/* Map model: state and data flow for the Connections map, with no DOM.
 * Requests go out by viewport (debounced, stale ones aborted), answers are cached by
 * revision, the scene crossfades between them, and the camera glides. The view
 * (map-view.js) paints this model and forwards input; tests drive it with the
 * synthetic world in map-world.js as the server. */
(function (root) {
  'use strict';
  const Core = root.MapCore || (typeof require === 'function' ? require('./map-core.js') : null);
  const { Camera, Flight, Scene, Cache, Latest, snapRect, viewQuery, debounceMax, clamp, K_MAX } = Core;
  const FITS = ['strong', 'good', 'weak', 'unread'];
  const PAD = 0.25;

  const finite = (v) => typeof v === 'number' && Number.isFinite(v);
  // Trust the shape, not the server: keep only people with a place on the map.
  function cleanNode(n, mode) {
    const position = n?.positions?.[mode];
    if (position) n = { ...n, ...position };
    if (!n || n.id == null || n.x == null || n.y == null || !finite(+n.x) || !finite(+n.y)) return null;
    return { ...n, x: +n.x, y: +n.y, rank: finite(+n.rank) ? +n.rank : 0, fit: FITS.includes(n.fit) ? n.fit : finite(n.fit) ? (n.fit >= 70 ? 'strong' : n.fit >= 45 ? 'good' : 'weak') : 'unread', closeness: n.closeness != null && finite(+n.closeness) ? +n.closeness : null, handle: String(n.handle || n.id), name: n.name ? String(n.name) : '', status: n.status || null };
  }
  function cleanCluster(c) {
    if (!c || c.id == null || c.x == null || c.y == null || !finite(+c.x) || !finite(+c.y) || !(+c.count > 0)) return null;
    return { ...c, x: +c.x, y: +c.y, count: +c.count, label: c.label ? String(c.label) : '' };
  }

  // The older endpoint remains useful before a spatial layout has been prepared.
  // A small ranked overview uses neutral positions. They do not encode relationships.
  function overview(data, filters = {}) {
    const people = (data.nodes || []).filter((n) => n.kind === 'lead').slice(0, 400);
    const columns = Math.max(1, Math.ceil(Math.sqrt(people.length))), rows = Math.max(1, Math.ceil(people.length / columns));
    const all = people.map((n, i) => ({ ...n, id: Number(String(n.id).replace(/^p:/, '')), x: .08 + .84 * ((i % columns + .5) / columns), y: .08 + .84 * ((Math.floor(i / columns) + .5) / rows), rank: n.score || 0, closeness: null, lead: true, handle: n.handle || n.label, reason: 'Shown in the ranked overview. Position does not indicate a relationship.' }));
    const tier = { strong: 3, good: 2, weak: 1, unread: 0 };
    const nodes = all.filter(n => (!filters.status || n.status === filters.status) && (!filters.minFit || (tier[n.fit] || 0) >= tier[filters.minFit]));
    return { rev: data.rev, nodes, clusters: [], world: {}, groups: [], total: data.total ?? people.length, shown: nodes.length, hidden: 0, fallback: true, sampled: people.length };
  }

  class MapModel {
    constructor(o = {}) {
      this.fetchJson = o.fetchJson;
      this.now = o.now || (() => (typeof performance !== 'undefined' ? performance.now() : Date.now()));
      this.reduced = !!o.reduced;
      this.online = o.online || (() => true);
      this.budgetOverride = o.budget || 0;
      this.cam = new Camera(); this.scene = new Scene(); this.cache = new Cache(); this.viewReq = new Latest(); this.edgeReq = new Latest(); this.searchReq = new Latest(); this.locateReq = new Latest();
      this.mode = o.mode || 'closeness'; this.scope = 'all'; this.minFit = ''; this.status = ''; this.q = '';
      this.phase = 'idle'; this.error = ''; this.total = 0; this.worldTotal = 0; this.shown = 0; this.hidden = 0; this.world = {}; this.rev = null;
      this.selected = null; this.edges = null; this.flight = null; this.goal = null; this.vel = null;
      this.loaded = null; this.paused = false; this.pending = 0; this.listeners = new Set(); this.morph = 0;
      this.load = this.load.bind(this);
      this.schedule = debounceMax(() => this.load(), 130, 420, o.timers);
      this.stats = { requests: 0, cacheHits: 0, applied: 0, aborted: 0 };
    }
    on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
    emit(what) { for (const fn of this.listeners) fn(what); }
    setSize(w, h) { this.cam.resize(w, h); }
    get budget() { return this.budgetOverride ? clamp(Math.round(this.budgetOverride), 250, 1500) : clamp(Math.round(this.cam.w * this.cam.h / 3400 * (1 + 2 * PAD) ** 2), 250, 1500); }
    get filtersActive() { return (this.scope !== 'all' ? 1 : 0) + (this.minFit ? 1 : 0) + (this.status ? 1 : 0); }

    /* ----- loading ----- */
    params(rect) { return viewQuery(rect, { mode: this.mode, budget: this.budget, scope: this.scope, minFit: this.minFit, status: this.status, q: this.q }); }
    // Does what we hold already answer the current view? Then panning costs nothing.
    needsLoad() {
      if (!this.loaded) return true;
      if (this.fallback) return false;
      const r = this.cam.rect(0), L = this.loaded.rect;
      if (r.x0 < L.x0 || r.y0 < L.y0 || r.x1 > L.x1 || r.y1 > L.y1) return true;
      return Math.abs(Math.log(this.cam.k / this.loaded.k)) > 0.3;
    }
    // While a flight is under way toward a view we already asked for, do not ask for the places in between.
    moved() { if (this.paused) return; if (!this.hold && this.needsLoad()) this.schedule(); this.emit('camera'); }
    async load(opt = {}) {
      if (this.paused) return;
      const cam = opt.cam ? Object.assign(new Camera(), this.cam, { ...opt.cam }) : this.cam;
      const rect = snapRect(cam.rect(PAD)), params = this.params(rect), key = params.toString();
      this.schedule.cancel();
      const cached = !opt.force && this.cache.get(key);
      if (cached) { this.stats.cacheHits++; this.viewReq.cancel(); this.pending = 0; this.emit('busy'); this.loaded = { rect, k: cam.k }; this.apply(cached, { rect, k: cam.k }); return; }
      this.hold = !!(opt.cam && this.flight);
      const ticket = this.viewReq.begin();
      if (this.viewReq.n > 1 && this.pending) this.stats.aborted++;
      this.pending = 1; this.stats.requests++;
      if (!this.scene.nodes.length && !this.scene.clusters.length) this.setPhase(this.online() ? 'loading' : 'offline');
      else this.emit('busy');
      let resp;
      try {
        resp = await this.fetchJson('/api/map/view?' + params, { signal: ticket.signal });
      } catch (e) {
        if (ticket.live()) { this.pending = 0; this.emit('busy'); }
        if (e && e.name === 'AbortError') return;
        if (!ticket.live()) return;
        this.fail(e);
        return;
      }
      if (ticket.live()) { this.pending = 0; this.emit('busy'); }
      if (!ticket.live()) return;
      if (!resp || !Array.isArray(resp.nodes)) { this.fail(new Error('The map came back incomplete.')); return; }
      if (resp.ready === false) {
        this.layout = resp.layout || {};
        try { const old = await this.fetchJson('/api/map?limit=400&scope=' + encodeURIComponent(this.scope), { signal: ticket.signal });
          if (!ticket.live()) return;
          resp = overview(old, this);
        } catch (e) { if (!ticket.live() || e?.name === 'AbortError') return; this.setPhase(this.layout.building ? 'building' : 'unprepared'); return; }
      }
      this.cache.set(key, resp);
      this.loaded = { rect, k: cam.k };
      this.apply(resp, { rect, k: cam.k });
    }
    fail(e) {
      this.error = e && e.message ? e.message : '';
      const offline = !this.online() || (e && e.name === 'TypeError');
      if (this.scene.nodes.length || this.scene.clusters.length) { this.stale = offline ? 'offline' : 'error'; this.emit('busy'); this.emit('stale'); return; }
      this.setPhase(offline ? 'offline' : 'error');
    }
    apply(resp, at) {
      const previousRev = this.rev;
      const nodes = resp.nodes.map(n => cleanNode(n, this.mode)).filter(Boolean), clusters = (resp.clusters || []).map(cleanCluster).filter(Boolean);
      this.fallback = !!resp.fallback; this.sampled = resp.sampled || 0;
      this.rev = resp.rev == null ? null : String(resp.rev);
      this.total = finite(+resp.total) ? +resp.total : nodes.length;
      this.worldTotal = resp.world_total != null && finite(+resp.world_total) ? +resp.world_total : this.total;
      this.shown = finite(+resp.shown) ? +resp.shown : nodes.length;
      this.hidden = finite(+resp.hidden) ? +resp.hidden : clusters.reduce((a, c) => a + c.count, 0);
      this.world = { ...(resp.world || {}) };
      if (!this.world.guides && Array.isArray(resp.groups)) this.world.guides = resp.groups.map((g) => ({ ...g, type: this.mode === 'status' ? 'lane' : 'island' }));
      this.seedList = resp.seeds || this.seedList || [];
      this.stale = null;
      const morph = this.reduced ? 0 : this.morph; this.morph = 0;
      this.scene.apply({ nodes, clusters }, this.now(), { morph, instant: this.reduced });
      if (this.selected) {
        const current = nodes.find((n) => String(n.id) === String(this.selected.id));
        if (current) this.selected = current;
        this.scene.pin(this.selected);
      }
      this.stats.applied++;
      this.emit('selection');
      this.setPhase(!nodes.length && !clusters.length && !this.selected ? 'empty' : 'ready');
      this.emit('scene');
      if (this.selected && previousRev !== this.rev) this.loadEdges(this.selected);
    }
    setPhase(p) { if (this.phase !== p) { this.phase = p; this.emit('phase'); } }
    retry() { this.stale = null; this.load({ force: true }); }
    // Called on an interval; a changed revision means new data, so refetch the current view.
    poll() { if (this.paused || this.phase === 'loading') return; this.load({ force: true }); }
    pause(on) { this.paused = !!on; if (on) { this.schedule.cancel(); this.viewReq.cancel(); this.edgeReq.cancel(); this.searchReq.cancel(); this.locateReq.cancel(); this.pending = 0; this.hold = false; this.emit('busy'); } else this.load(); }

    /* ----- modes and filters ----- */
    setMode(mode) {
      if (this.fallback || mode === this.mode) return;
      const keep = this.selected;
      this.mode = mode; this.morph = 720; this.edges = null; this.edgeReq.cancel();
      this.emit('mode');
      const target = { cx: 0.5, cy: 0.5, k: 1 };
      if (keep) { this.locate(keep); return; }
      this.flyTo(target, 520);
      this.load({ cam: target });
    }
    // The same person in the new layout: ask where they are now, then follow them.
    async locate(person) {
      const mode = this.mode, k = Math.max(this.cam.k, 1), ticket = this.locateReq.begin();
      try {
        const r = await this.fetchJson(`/api/map/search?q=${encodeURIComponent(person.handle)}&mode=${mode}`, { signal: ticket.signal });
        if (!ticket.live() || mode !== this.mode) return;
        const hit = (r.results || []).map(n => cleanNode(n, mode)).find((n) => n && String(n.id) === String(person.id));
        if (hit) { this.selected = { ...person, ...hit }; this.scene.pin(this.selected); const t = { cx: hit.x, cy: hit.y, k }; this.flyTo(t, 560); this.load({ cam: t }); this.emit('selection'); this.loadEdges(this.selected); return; }
      } catch (_) { /* fall through to the whole map */ }
      if (!ticket.live() || mode !== this.mode) return;
      this.deselect(); this.flyTo({ cx: 0.5, cy: 0.5, k: 1 }, 520); this.load({ cam: { cx: 0.5, cy: 0.5, k: 1 } });
    }
    setFilters(f) {
      const next = { scope: f.scope ?? this.scope, minFit: f.minFit ?? this.minFit, status: f.status ?? this.status };
      if (next.scope === this.scope && next.minFit === this.minFit && next.status === this.status) return;
      Object.assign(this, next); this.morph = 0;
      this.emit('filters'); this.load();
    }
    setQuery(q) { if (q === this.q) return; this.q = q; this.load(); }

    /* ----- selection and lines ----- */
    select(n) {
      this.locateReq.cancel();
      if (!n) return this.deselect();
      this.selected = n; this.scene.pin(n); this.emit('selection');
      this.loadEdges(n);
    }
    deselect() {
      if (!this.selected) return false;
      this.locateReq?.cancel();
      this.selected = null; this.edges = null; this.edgeReq.cancel(); this.scene.unpin(); this.scene.rebuild();
      this.emit('selection'); return true;
    }
    async loadEdges(n) {
      const ticket = this.edgeReq.begin(), mode = this.mode;
      if (this.fallback) { this.edges = { id: n.id, state: 'ready', lines: [], seeds: [], overview: true }; this.emit('edges'); return; }
      this.edges = { id: n.id, state: 'loading', lines: [], seeds: [] }; this.emit('edges');
      try {
        const r = await this.fetchJson(`/api/map/edges?ids=${encodeURIComponent(n.id)}&mode=${mode}`, { signal: ticket.signal });
        if (!ticket.live() || !this.selected || this.selected.id !== n.id) return;
        this.edges = this.readEdges(n, r); this.emit('edges');
      } catch (e) {
        if (!ticket.live() || (e && e.name === 'AbortError')) return;
        this.edges = { id: n.id, state: 'error', lines: [], seeds: [] }; this.emit('edges');
      }
    }
    // Accept either { edges: [{source,target}] } or { links: [{from,to}] }; place endpoints from the reply or the scene.
    readEdges(n, r) {
      const byId = new Map();
      for (const e of (r.nodes || [])) { const c = cleanNode(e); if (c) byId.set(String(c.id), c); }
      const place = (id) => {
        if (String(id) === String(n.id)) return { x: n.x, y: n.y, node: n };
        const known = byId.get(String(id)); if (known) return { x: known.x, y: known.y, node: known };
        const it = this.scene.get(id); if (it) return { x: it.d.x, y: it.d.y, node: it.d };
        if (String(id) === '0' && this.world.me) return { x: this.world.me.x, y: this.world.me.y, node: null };
        return null;
      };
      const lines = [], seeds = [], seen = new Set();
      for (const e of (r.edges || r.links || [])) {
        const s = e.source ?? e.from ?? e.a, t = e.target ?? e.to ?? e.b;
        if (s == null || t == null) continue;
        const other = String(s) === String(n.id) ? t : s;
        const a = place(s), b = place(t);
        if (!a || !b) continue;
        if (String(s) !== String(n.id) && String(t) !== String(n.id)) continue;
        const rawKind = e.kind || e.type || 'follows';
        const kind = rawKind === 'follow' ? 'follows' : rawKind;
        lines.push({ a: { x: a.x, y: a.y }, b: { x: b.x, y: b.y }, kind, other: String(other) });
        const o = place(other);
        if (kind === 'follows' && o && o.node && o.node.source && !seen.has(String(other))) { seen.add(String(other)); seeds.push(o.node); }
      }
      return { id: n.id, state: 'ready', lines, seeds };
    }
    patchStatus(id, status) {
      const it = this.scene.get(id);
      if (it) it.d = { ...it.d, status };
      if (this.selected && String(this.selected.id) === String(id)) this.selected = { ...this.selected, status };
      this.cache.clear(); this.emit('scene'); this.emit('selection');
    }

    /* ----- search ----- */
    async searchPeople(q) {
      const ticket = this.searchReq.begin();
      q = q.trim();
      if (!q) return [];
      const r = await this.fetchJson(this.fallback ? '/api/map?limit=400&scope=' + encodeURIComponent(this.scope) + '&q=' + encodeURIComponent(q) : `/api/map/search?q=${encodeURIComponent(q)}&mode=${this.mode}`, { signal: ticket.signal });
      if (!ticket.live()) return null;
      return (this.fallback ? overview(r).nodes : r.results || []).map(n => cleanNode(n, this.mode)).filter(Boolean);
    }
    // Where to zoom so this person shows as an individual: enough that the server's budget covers the area.
    kFor(n) { if (this.fallback) return Math.max(1, Math.min(this.cam.k, 4)); return clamp(Math.max(this.cam.k, Math.sqrt(Math.max(1, this.total) / Math.max(1, this.budget / 2.2)) * 1.1), 1, K_MAX); }
    goTo(n, opt = {}) {
      const person = cleanNode(n); if (!person) return;
      this.select(person);
      const t = { cx: person.x, cy: person.y, k: opt.k || this.kFor(person) };
      this.flyTo(t, opt.ms || 900); this.load({ cam: t });
    }

    /* ----- camera ----- */
    flyTo(target, ms = 700) {
      this.goal = null; this.vel = null;
      if (this.reduced) { this.cam.set(target.cx, target.cy, target.k); this.flight = null; this.emit('camera'); return; }
      this.flight = new Flight(this.cam.state(), target, this.now(), ms);
      this.emit('camera');
    }
    fit() { this.flyTo({ cx: 0.5, cy: 0.5, k: 1 }, 600); this.load({ cam: { cx: 0.5, cy: 0.5, k: 1 } }); }
    pan(dx, dy) { this.flight = null; this.goal = null; this.vel = null; this.cam.panBy(dx, dy); this.moved(); }
    release(vx, vy) { if (!this.reduced && Math.hypot(vx, vy) > 60) this.vel = { vx, vy }; }
    // Smooth zoom that keeps the point under the pointer still.
    zoomBy(factor, px, py) {
      this.flight = null; this.vel = null;
      const [wx, wy] = this.goal ? [this.goal.wx, this.goal.wy] : this.cam.toWorld(px, py);
      const base = this.goal && Math.abs(this.goal.px - px) < 2 && Math.abs(this.goal.py - py) < 2 ? this.goal.k : this.cam.k;
      const k = clamp(base * factor, Core.K_MIN, K_MAX);
      if (this.reduced) { this.cam.lock(wx, wy, px, py, k); this.goal = null; this.moved(); return; }
      this.goal = { k, wx, wy, px, py };
    }
    // Advance animation. Returns true while the view keeps changing, so the loop can go idle.
    tick(now, dt) {
      let busy = false;
      if (this.flight) { const s = this.flight.at(now); this.cam.set(s.cx, s.cy, s.k); if (s.done) { this.flight = null; this.hold = false; } else busy = true; this.moved(); }
      if (this.goal) {
        const g = this.goal, lk = Math.log(this.cam.k), lg = Math.log(g.k), gap = lg - lk;
        const nk = Math.abs(gap) < 0.003 ? g.k : Math.exp(lk + gap * (1 - Math.exp(-dt / 0.07)));
        this.cam.lock(g.wx, g.wy, g.px, g.py, nk);
        if (nk === g.k) this.goal = null; else busy = true;
        this.moved();
      }
      if (this.vel) {
        this.cam.panBy(this.vel.vx * dt, this.vel.vy * dt);
        const d = Math.exp(-dt / 0.22); this.vel.vx *= d; this.vel.vy *= d;
        if (Math.hypot(this.vel.vx, this.vel.vy) < 8) this.vel = null; else busy = true;
        this.moved();
      }
      if (this.scene.step(now, dt, this.reduced)) busy = true;
      return busy;
    }
    get idle() { return !this.flight && !this.goal && !this.vel && !this.scene.animating; }
  }

  const api = { MapModel, cleanNode, cleanCluster, overview };
  root.MapModel = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window === 'undefined' ? globalThis : window);
