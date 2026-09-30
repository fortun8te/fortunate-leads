/* Map model: stable pages, explicit filtering and selection, and a local camera.
 * Zoom and pan never change page membership. Server cursors advance only through
 * the page controls; size and display mode are local presentation choices. */
(function (root) {
  'use strict';
  const Core = root.MapCore || (typeof require === 'function' ? require('./map-core.js') : null);
  const { Camera, Flight, Scene, Cache, Latest, snapRect, viewQuery, debounceMax, clamp, K_MAX, cohortLayout } = Core;
  const FITS = ['strong', 'good', 'weak', 'unread'];
  const PAD = 0.25;

  const finite = (v) => typeof v === 'number' && Number.isFinite(v);
  // Trust the shape, not the server: keep only people with a place on the map.
  function cleanNode(n, mode) {
    const position = n?.positions?.[mode];
    if (position) n = { ...n, ...position };
    if (!n || n.id == null || n.x == null || n.y == null || !finite(+n.x) || !finite(+n.y)) return null;
    return { ...n, x: +n.x, y: +n.y, rank: finite(+n.rank) ? +n.rank : 0, fit: FITS.includes(n.fit) ? n.fit : finite(n.fit) ? (n.fit >= 70 ? 'strong' : n.fit >= 45 ? 'good' : 'weak') : 'unread', closeness: n.closeness != null && finite(+n.closeness) ? +n.closeness : null, pic: typeof n.pic === 'string' && /^\/img\/\d+$/.test(n.pic) ? n.pic : null, handle: String(n.handle || n.id), name: n.name ? String(n.name) : '', status: n.status || null };
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
      this.density = 500; this.cursor = ''; this.pages = ['']; this.pageIndex = 0; this.nextCursor = null;
      this.size = 'followers'; this.mode = o.mode || 'closeness'; this.scope = 'all'; this.minFit = ''; this.status = ''; this.follow = 'all'; this.q = '';
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
    get budget() { return this.density; }
    get filtersActive() { return (this.scope !== 'all' ? 1 : 0) + (this.minFit ? 1 : 0) + (this.status ? 1 : 0) + (this.follow !== 'all' ? 1 : 0); }

    /* ----- loading ----- */
    params() {
      const p = viewQuery({ x0: 0, y0: 0, x1: 1, y1: 1 }, {
        mode: 'closeness', budget: this.budget, scope: this.scope, minFit: this.minFit,
        status: this.status, q: this.q, follow: this.follow
      });
      p.set('cohort', '1');
      if (this.cursor) p.set('after', this.cursor);
      return p;
    }
    needsLoad() { return !this.loaded; }
    // While a flight is under way toward a view we already asked for, do not ask for the places in between.
    moved() { if (this.paused) return; if (!this.hold && this.needsLoad()) this.schedule(); this.emit('camera'); }
    async load(opt = {}) {
      if (this.paused) return;
      const cam = opt.cam ? Object.assign(new Camera(), this.cam, { ...opt.cam }) : this.cam;
      const rect = {x0:0,y0:0,x1:1,y1:1}, params = this.params(), key = params.toString();
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
        if (this.follow !== 'all') { this.pending = 0; this.emit('busy'); this.setPhase(this.layout.building ? 'building' : 'unprepared'); return; }
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
      const nodes = cohortLayout(resp.nodes.map(n => cleanNode(n, this.mode)).filter(Boolean), resp.world?.me), clusters = [];
      this.nextCursor = resp.next_cursor || null; this.cohort = true;
      if (resp.cohort_reset) {
        this.cursor = ''; this.pages = ['']; this.pageIndex = 0;
      }
      this.fallback = !!resp.fallback; this.sampled = resp.sampled || 0;
      this.rev = resp.rev == null ? null : String(resp.rev);
      this.total = finite(+resp.total) ? +resp.total : nodes.length;
      this.worldTotal = resp.world_total != null && finite(+resp.world_total) ? +resp.world_total : this.total;
      this.shown = finite(+resp.shown) ? +resp.shown : nodes.length;
      this.hidden = finite(+resp.hidden) ? +resp.hidden : clusters.reduce((a, c) => a + c.count, 0);
      this.world = { ...(resp.world || {}), cohort: true };
      if (!this.world.guides && Array.isArray(resp.groups)) this.world.guides = resp.groups.map((g) => ({ ...g, type: this.mode === 'status' ? 'lane' : 'island' }));
      this.seedList = resp.seeds || this.seedList || [];
      this.stale = null;
      this.morph = 0;
      this.scene.clear();
      this.scene.apply({ nodes, clusters }, this.now(), { morph: 0, instant: true });
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
    poll() { /* A visible page stays stable until the user explicitly changes it. */ }
    pause(on) { this.paused = !!on; if (on) { this.schedule.cancel(); this.viewReq.cancel(); this.edgeReq.cancel(); this.searchReq.cancel(); this.locateReq.cancel(); this.pending = 0; this.hold = false; this.emit('busy'); } else if (!this.loaded) this.load(); }

    /* ----- modes and filters ----- */
    setMode(mode) {
      if (mode === this.mode) return;
      this.mode=mode; this.emit('mode'); this.emit('scene');
    }
    async browse(direction) {
      if (this.pending || direction > 0 && !this.nextCursor || direction < 0 && this.pageIndex === 0) return;
      const previous = { cursor: this.cursor, pages: [...this.pages], pageIndex: this.pageIndex, loaded: this.loaded };
      const applied = this.stats.applied;
      if (direction > 0) {
        this.pages[++this.pageIndex] = this.nextCursor;
        this.pages.length = this.pageIndex + 1;
      } else this.pageIndex--;
      this.cursor = this.pages[this.pageIndex];
      this.deselect(); this.loaded = null;
      await this.load();
      if (this.stats.applied === applied) { Object.assign(this, previous); this.emit('scene'); }
    }
    setDensity(n) {
      if (![250, 500, 1000].includes(+n) || +n === this.density) return;
      this.density = +n; this.deselect(); this.resetPages(); this.load();
    }
    resetPages() {
      this.cursor = ''; this.pages = ['']; this.pageIndex = 0; this.nextCursor = null; this.loaded = null;
    }
    setFilters(f) {
      const next = { scope: f.scope ?? this.scope, minFit: f.minFit ?? this.minFit, status: f.status ?? this.status, follow: f.follow ?? this.follow };
      if (next.scope === this.scope && next.minFit === this.minFit && next.status === this.status && next.follow === this.follow) return;
      Object.assign(this, next); this.deselect(); this.resetPages(); this.morph = 0;
      this.emit('filters'); this.load();
    }
    setQuery(q) { if (q === this.q) return; this.q = q; this.resetPages(); this.load(); }

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
        const it = this.scene.get(id); if (it) return { x: it.d.x, y: it.d.y, node: it.d };
        const known = byId.get(String(id)); if (known && !this.cohort) return { x: known.x, y: known.y, node: known };
        if (String(id) === '0' && this.world.me) return { x: this.world.me.x, y: this.world.me.y, node: null };
        return null;
      };
      const lines = [], seeds = [], seen = new Set();
      for (const e of (r.edges || r.links || [])) {
        const s = e.source ?? e.from ?? e.a, t = e.target ?? e.to ?? e.b;
        if (s == null || t == null) continue;
        if (String(s) !== String(n.id) && String(t) !== String(n.id)) continue;
        const other = String(s) === String(n.id) ? t : s;
        const rawKind = e.kind || e.type || 'follows';
        const kind = rawKind === 'follow' ? 'follows' : rawKind;
        const source = byId.get(String(other)) || this.scene.get(other)?.d;
        if (kind === 'follows' && source?.source && !seen.has(String(other))) {
          seen.add(String(other)); seeds.push(source);
        }
        const a = place(s), b = place(t);
        if (!a || !b) continue;
        lines.push({ a: { x: a.x, y: a.y }, b: { x: b.x, y: b.y }, kind, other: String(other) });

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
    kFor() { return Math.max(1,this.cam.k); }
    goTo(n) {
      const person = cleanNode(n);
      if (!person) return;
      const existing = this.scene.get(person.id);
      if (existing) this.select(existing.d);
      else {
        const members = this.scene.nodes.map(it => it.d).filter(it => String(it.id) !== String(this.world.me?.id));
        if (members.length >= this.density) members.pop();
        members.push(person);
        const nodes = cohortLayout(members, this.world.me);
        this.scene.clear();
        this.scene.apply({ nodes, clusters: [] }, this.now(), { instant: true });
        this.select(nodes.find(it => String(it.id) === String(person.id)));
        this.emit('scene');
      }
      if (this.selected) this.flyTo({ cx: this.selected.x, cy: this.selected.y, k: this.cam.k }, 450);
    }

    /* ----- camera ----- */
    flyTo(target, ms = 700) {
      this.goal = null; this.vel = null;
      if (this.reduced) { this.cam.set(target.cx, target.cy, target.k); this.flight = null; this.emit('camera'); return; }
      this.flight = new Flight(this.cam.state(), target, this.now(), ms);
      this.emit('camera');
    }
    fit() { this.flyTo({ cx:0.5,cy:0.5,k:1 },600); }
    pan(dx, dy) { this.interruptCamera(); this.cam.panBy(dx, dy); this.moved(); }
    release(vx, vy) { const speed=Math.hypot(vx,vy); if (!this.reduced && speed > 60) { const scale=Math.min(1,700/speed); this.vel = { vx:vx*scale, vy:vy*scale }; } }
    setSizeEncoding(size) { if (!Core.SIZE_OPTIONS.some(o=>o.id===size) || size === this.size) return; this.size=size; this.emit('size'); }
    interruptCamera() {
      this.locateReq.cancel(); this.flight=null; this.goal=null; this.vel=null;
      if (this.hold) { this.viewReq.cancel(); this.pending=0; this.loaded=null; this.emit('busy'); }
      this.hold=false;
    }
    // Smooth zoom that keeps the point under the pointer still.
    zoomBy(factor, px, py) {
      if (this.hold) { this.viewReq.cancel(); this.pending=0; this.loaded=null; this.hold=false; }
      this.locateReq.cancel(); this.flight = null; this.vel = null;
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
