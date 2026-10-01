/* Map core: the parts of the Connections map that need no DOM, so they can be tested.
 * Camera maths, request planning and caching, the animated scene (crossfade and
 * morph), label collision, encodings per view mode, and the plain-words copy. */
(function (root) {
  'use strict';
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const int = (n) => Math.round(Number(n) || 0).toLocaleString('en-US');
  const plural = (n, one, many) => `${int(n)} ${n === 1 ? one : many || one + 's'}`;
  // 12,400 becomes 12k; 1,250,000 becomes 1.3M. Bubbles are small.
  const compact = (n) => {
    n = Math.round(Number(n) || 0);
    if (n < 1000) return String(n);
    if (n < 10000) return (Math.round(n / 100) / 10).toString().replace(/\.0$/, '') + 'k';
    if (n < 1e6) return Math.round(n / 1000) + 'k';
    if (n < 1e7) return (Math.round(n / 1e5) / 10).toString().replace(/\.0$/, '') + 'M';
    return Math.round(n / 1e6) + 'M';
  };
  const easeOut = (t) => 1 - Math.pow(1 - clamp(t, 0, 1), 3);
  const easeInOut = (t) => { t = clamp(t, 0, 1); return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; };

  const MODES = [
    { id: 'closeness', label: 'Network', short: 'Network' },
    { id: 'fit', label: 'Fit', short: 'Fit' },
    { id: 'seeds', label: 'Sources', short: 'Sources' },
    { id: 'status', label: 'Status', short: 'Status' }
  ];
  const FIT_LABEL = { strong: 'Strong', good: 'Good', weak: 'Weak', unread: 'Not read yet' };
  const STATUS_LABEL = { interested: 'Interested', contacted: 'Contacted', talking: 'Talking', spoke_before: 'Spoke before', client: 'Client', no: 'Not a fit' };
  const K_MIN = 0.55, K_MAX = 128;

  /* ---------- Camera ---------- */
  // The world is the unit square. At k = 1 the whole square fits the shorter side.
  class Camera {
    constructor() { this.maxK = K_MAX; this.cx = 0.5; this.cy = 0.5; this.k = 1; this.w = 800; this.h = 600; this.inset = { top: 0, right: 0, bottom: 0, left: 0 }; }
    resize(w, h) { this.w = w; this.h = h; }
    get scale() { return Math.min(this.w, this.h) * 0.9 * this.k; }
    // The visible area excludes anything laid over the map, so "centre" means centre of what you can see.
    get mid() { const i = this.inset; return [(this.w + i.left - i.right) / 2, (this.h + i.top - i.bottom) / 2]; }
    toScreen(x, y) { const s = this.scale, m = this.mid; return [(x - this.cx) * s + m[0], (y - this.cy) * s + m[1]]; }
    toWorld(px, py) { const s = this.scale, m = this.mid; return [(px - m[0]) / s + this.cx, (py - m[1]) / s + this.cy]; }
    // World rectangle currently on screen, grown by pad (a fraction of its size) on every side.
    rect(pad = 0) {
      const a = this.toWorld(0, 0), b = this.toWorld(this.w, this.h), dx = (b[0] - a[0]) * pad, dy = (b[1] - a[1]) * pad;
      return { x0: a[0] - dx, y0: a[1] - dy, x1: b[0] + dx, y1: b[1] + dy };
    }
    clampCentre() { this.cx = clamp(this.cx, -0.15, 1.15); this.cy = clamp(this.cy, -0.15, 1.15); this.k = clamp(this.k, K_MIN, this.maxK); }
    panBy(dx, dy) { const s = this.scale; this.cx -= dx / s; this.cy -= dy / s; this.clampCentre(); }
    // Zoom by factor keeping the world point under (px, py) where it is.
    zoomAt(factor, px, py) {
      const [wx, wy] = this.toWorld(px, py);
      this.k = clamp(this.k * factor, K_MIN, this.maxK);
      const s = this.scale, m = this.mid;
      this.cx = wx - (px - m[0]) / s; this.cy = wy - (py - m[1]) / s; this.clampCentre();
    }
    // Put world point (wx, wy) under screen point (px, py) at zoom k.
    lock(wx, wy, px, py, k) {
      this.k = clamp(k, K_MIN, this.maxK);
      const s = this.scale, m = this.mid;
      this.cx = wx - (px - m[0]) / s; this.cy = wy - (py - m[1]) / s; this.clampCentre();
    }
    set(cx, cy, k) { this.cx = cx; this.cy = cy; this.k = k; this.clampCentre(); }
    state() { return { cx: this.cx, cy: this.cy, k: this.k }; }
  }

  // A timed move of the camera between two states, eased in log-zoom so it feels even.
  class Flight {
    constructor(from, to, now, ms) { this.from = from; this.to = to; this.t0 = now; this.ms = Math.max(1, ms); }
    at(now) {
      const t = clamp((now - this.t0) / this.ms, 0, 1), e = easeInOut(t), a = this.from, b = this.to;
      // Zoom in log space; travel in world space scaled so the move reads as one arc, not a slide then a zoom.
      const lk = Math.log(a.k) + (Math.log(b.k) - Math.log(a.k)) * e;
      return { cx: a.cx + (b.cx - a.cx) * e, cy: a.cy + (b.cy - a.cy) * e, k: Math.exp(lk), done: t >= 1 };
    }
  }

  /* ---------- Requests and cache ---------- */
  // Round a rectangle outward to a power-of-two grid, so nearby views ask for the same thing and hit the cache.
  function snapRect(r) {
    const span = Math.max(r.x1 - r.x0, r.y1 - r.y0);
    const step = Math.pow(2, Math.floor(Math.log2(span / 4)));
    return { x0: Math.floor(r.x0 / step) * step, y0: Math.floor(r.y0 / step) * step, x1: Math.ceil(r.x1 / step) * step, y1: Math.ceil(r.y1 / step) * step };
  }
  const num = (v) => +v.toFixed(6);
  function viewQuery(rect, o) {
    const p = new URLSearchParams({ mode: o.mode, x0: num(rect.x0), y0: num(rect.y0), x1: num(rect.x1), y1: num(rect.y1), budget: String(o.budget || 600), scope: o.scope || 'all' });
    if (o.minFit) p.set('min_fit', o.minFit);
    if (o.status) p.set('status', o.status);
    if (o.q) p.set('q', o.q);
    if (o.follow && o.follow !== 'all') p.set('follow', o.follow);
    if (o.overview) p.set('overview', '1');
    return p;
  }
  // Small LRU keyed by request. Entries only count while the data revision still matches.
  class Cache {
    constructor(limit = 48) { this.limit = limit; this.map = new Map(); this.rev = null; }
    get(key) { const e = this.map.get(key); if (!e) return null; this.map.delete(key); this.map.set(key, e); return e; }
    set(key, value) {
      if (value.rev !== undefined && this.rev !== null && String(value.rev) !== this.rev) this.map.clear();
      if (value.rev !== undefined) this.rev = String(value.rev);
      this.map.set(key, value);
      while (this.map.size > this.limit) this.map.delete(this.map.keys().next().value);
    }
    clear() { this.map.clear(); this.rev = null; }
    get size() { return this.map.size; }
  }
  // Latest wins: begin() returns a ticket and cancels the previous request; only the live ticket may apply.
  class Latest {
    constructor() { this.n = 0; this.controller = null; }
    begin() { this.controller?.abort(); this.controller = typeof AbortController === 'function' ? new AbortController() : null; const id = ++this.n; return { id, signal: this.controller?.signal, live: () => id === this.n }; }
    cancel() { this.controller?.abort(); this.n++; }
  }
  // Debounce with a ceiling, so a long drag still refreshes now and then.
  function debounceMax(fn, wait, maxWait, timers = { set: (f, ms) => setTimeout(f, ms), clear: (t) => clearTimeout(t), now: () => Date.now() }) {
    let t = null, first = 0;
    const run = () => { timers.clear(t); t = null; first = 0; fn(); };
    const call = () => {
      const now = timers.now();
      if (!first) first = now;
      timers.clear(t);
      t = timers.set(run, Math.max(0, Math.min(wait, first + maxWait - now)));
    };
    call.cancel = () => { timers.clear(t); t = null; first = 0; };
    call.flush = () => { if (t !== null) run(); };
    return call;
  }

  class SpatialIndex {
    constructor(items) {
      this.cells = new Map(); this.maxRadius = 0;
      for (const it of items) {
        const key = Math.floor(it.x * 128) + ':' + Math.floor(it.y * 128);
        if (!this.cells.has(key)) this.cells.set(key, []);
        this.cells.get(key).push(it); this.maxRadius = Math.max(this.maxRadius, it.d.portraitRadius || 0);
      }
    }
    query(rect, limit = Infinity) {
      const found = [], pad = this.maxRadius;
      const x0 = Math.max(-1, Math.floor((rect.x0-pad)*128)), x1 = Math.min(129, Math.floor((rect.x1+pad)*128));
      const y0 = Math.max(-1, Math.floor((rect.y0-pad)*128)), y1 = Math.min(129, Math.floor((rect.y1+pad)*128));
      for (let y=y0;y<=y1;y++) for (let x=x0;x<=x1;x++) {
        for (const it of this.cells.get(x+':'+y) || []) {
          const r=it.d.portraitRadius || 0;
          if (it.x+r<rect.x0 || it.x-r>rect.x1 || it.y+r<rect.y0 || it.y-r>rect.y1 || it.ta===0) continue;
          found.push(it); if (found.length>=limit) return found;
        }
      }
      return found;
    }
  }

  /* ---------- Scene: what is on screen, and how it fades ---------- */
  class Scene {
    constructor() { this.items = new Map(); this.nodes = []; this.clusters = []; this.animating = false; this.maxCluster = 1; }
    // Merge a response. Things still present persist and glide; new things fade in; missing things fade out.
    apply(resp, now, o = {}) {
      const morph = o.morph || 0, seen = new Set();
      const put = (kind, d) => {
        const key = kind + ':' + d.id;
        seen.add(key);
        let it = this.items.get(key);
        if (!it) { it = { key, kind, d, x: d.x, y: d.y, fx: d.x, fy: d.y, tx: d.x, ty: d.y, t0: now, dur: 0, a: o.instant ? 1 : 0, ta: 1, pin: false }; this.items.set(key, it); return; }
        it.d = d; it.ta = 1;
        if (Math.abs(it.tx - d.x) > 1e-7 || Math.abs(it.ty - d.y) > 1e-7) {
          it.tx = d.x; it.ty = d.y;
          if (morph) { it.fx = it.x; it.fy = it.y; it.t0 = now; it.dur = morph; } else { it.x = d.x; it.y = d.y; it.dur = 0; }
        }
      };
      for (const n of resp.nodes || []) put('n', n);
      for (const c of resp.clusters || []) put('c', c);
      for (const it of this.items.values()) if (!seen.has(it.key)) it.ta = it.pin ? 1 : 0;
      this.rebuild(); this.animating = !!morph || !o.instant;
    }
    // Keep one person visible whatever the server sends: the selected or searched person.
    pin(node) {
      this.unpin();
      if (!node) return;
      const key = 'n:' + node.id; let it = this.items.get(key);
      if (!it) { it = { key, kind: 'n', d: node, x: node.x, y: node.y, fx: node.x, fy: node.y, tx: node.x, ty: node.y, t0: 0, dur: 0, a: 0, ta: 1, pin: true }; this.items.set(key, it); }
      it.d = node; it.x = node.x; it.y = node.y; it.tx = node.x; it.ty = node.y; it.dur = 0;
      it.pin = true; it.ta = 1; this.pinned = key; if (!this.large) this.rebuild();
    }
    unpin() { if (this.pinned) { const it = this.items.get(this.pinned); if (it) { it.pin = false; } this.pinned = null; } }
    get(id) { return this.items.get('n:' + id) || null; }
    rebuild() {
      this.nodes = []; this.clusters = []; let max = 1;
      for (const it of this.items.values()) {
        if (it.kind === 'n') this.nodes.push(it); else { this.clusters.push(it); if (it.d.count > max) max = it.d.count; }
      }
      // Lowest rank first, so the best people are painted last and stay on top.
      this.nodes.sort((a, b) => (a.d.rank || 0) - (b.d.rank || 0));
      this.clusters.sort((a, b) => b.d.count - a.d.count);
      this.maxCluster = max;
      this.large = this.nodes.length > 2000; this.spatial = this.large ? new SpatialIndex(this.nodes) : null; this.version = (this.version || 0) + 1;
    }
    clear() { this.items.clear(); this.nodes = []; this.clusters = []; this.animating = false; this.pinned = null; this.large = false; this.spatial = null; }
    // Advance by dt seconds. Returns true while anything is still moving, so the loop can stop when idle.
    step(now, dt, reduced) {
      if (this.large && !this.animating) return false;
      let busy = false, dead = false;
      const k = reduced ? 1 : 1 - Math.exp(-dt / 0.085);
      for (const it of this.items.values()) {
        if (it.a !== it.ta) {
          it.a += (it.ta - it.a) * k;
          if (Math.abs(it.ta - it.a) < 0.01) it.a = it.ta; else busy = true;
          if (it.a === 0 && it.ta === 0) { it.dead = true; dead = true; }
        }
        if (it.dur) {
          const t = (now - it.t0) / it.dur, e = easeOut(t);
          it.x = it.fx + (it.tx - it.fx) * e; it.y = it.fy + (it.ty - it.fy) * e;
          if (t >= 1) { it.dur = 0; it.x = it.tx; it.y = it.ty; } else busy = true;
        }
      }
      if (dead) { for (const [key, it] of this.items) if (it.dead) this.items.delete(key); this.rebuild(); }
      this.animating = busy;
      return busy;
    }
  }

  /* ---------- Encodings ---------- */
  const RADIUS = { strong: 19, good: 16, weak: 12.5, unread: 10 };
  const followRing = n => n.followed && n.follows_me ? 'mutual' : n.followed ? 'outgoing' : n.follows_me ? 'incoming' : 'unknown';
  const hasRing = n => followRing(n) !== 'unknown';
  // Which palette slot a person takes in a mode. 'a' is the one accent. Slots 0-5 are neutral tones.
  function toneFor(mode, n) {
    if (mode === 'seeds') return n.status && n.status !== 'no' ? 'a' : 't' + ((n.cluster | 0) % 6);
    if (mode === 'status') {
      if (n.status === 'talking' || n.status === 'client') return 'a';
      if (n.status === 'interested' || n.status === 'contacted' || n.status === 'spoke_before') return 't0';
      if (n.status === 'no') return 't5';
      return 't4';
    }
    if (mode === 'fit') return n.fit === 'strong' ? 'a' : n.fit === 'good' ? 't1' : n.fit === 'weak' ? 't3' : 't5';
    if (n.status && n.status !== 'no') return 'a';
    return n.fit === 'strong' ? 't1' : n.fit === 'good' ? 't2' : n.fit === 'weak' ? 't3' : 't4';
  }
  const OWNER_RADIUS = 12;
  const SIZE_OPTIONS = [{id:'followers',label:'Followers'}, {id:'fit',label:'Fit'}, {id:'connections',label:'Shared sources'}, {id:'equal',label:'Equal'}];
  const SIZE_HELP = {
    followers:'Larger bubbles have more saved followers. Unknown counts use the smallest size. Counts use a compressed scale.',
    fit:'Larger bubbles have a stronger saved fit assessment. Unread profiles use the smallest size.',
    connections:'Larger bubbles appear in more collected audiences. Shared sources do not establish a relationship.',
    equal:'Every person has the same bubble size.'
  };
  const radiusFor = (n, k, size = 'followers') => {
    let base = 14;
    if (size === 'equal') base = 14;
    else if (size === 'connections') base = clamp(7 + 6 * Math.sqrt(Math.max(0, +n.source_count || 0)), 7, 28);
    else if (size === 'fit') base = { strong: 24, good: 16, weak: 10, unread: 7 }[n.fit] || 7;
    else if (n.followers != null) base = 14 + 7 * Math.pow(clamp((Math.log10(1 + Math.max(0, +n.followers || 0)) - 2) / 4, 0, 1), 2);
    return base * clamp(.85 + .15 * Math.sqrt(k), .85, 1.35);
  };
  const distanceKey = { g: 'centre', t: 'Closer portraits have stronger recorded network evidence. Positions are evenly spaced for readability; distance is an ordering, not a measure of friendship.', s: 'Distance: evidence' };
  const fitSizeKey = { g: 'size', t: 'Larger portraits mean a stronger saved fit assessment. All people in this page stay visible; zoom only magnifies them.', s: 'Size: fit' };
  const LEGENDS = Object.fromEntries(MODES.map(m => [m.id, [distanceKey, fitSizeKey]]));
  const closenessWords = (c) => c >= 0.75 ? 'strong network evidence' : c >= 0.5 ? 'some network evidence' : c >= 0.25 ? 'limited network evidence' : 'little network evidence';
  // One line on why this person is on your map, from what the map knows.
  function whyLine(n, seedLabel) {
    const parts = [];
    parts.push(n.fit === 'unread' ? 'Not read yet' : `${FIT_LABEL[n.fit] || 'Unknown'} fit`);
    if (seedLabel) parts.push(`in the ${seedLabel} audience`);
    if (typeof n.closeness === 'number') parts.push(closenessWords(n.closeness));
    let s = parts[0] + (parts.length > 1 ? ', ' + parts.slice(1).join(', ') : '') + '.';
    if (n.follows_me) s = s.replace(/\.$/, '') + ', follows you.';
    return s;
  }

  function cohortLayout(people, owner, size = 'followers', distance = 'network') {
    const members = people.filter(n => String(n.id) !== String(owner?.id));
    const distanceValue = n => {
      const value = distance === 'shared' ? n.source_count : n.closeness;
      return value != null && Number.isFinite(+value) ? +value : -1;
    };
    members.sort((a, b) => distanceValue(b) - distanceValue(a)
      || (b.closeness || 0) - (a.closeness || 0) || (b.rank || 0) - (a.rank || 0) || String(a.id).localeCompare(String(b.id)));
    if (!members.length) return owner ? [{ ...owner, x: .5, y: .5, cohort: true, spacing: .075 }] : [];
    if (members.length > 2000) {
      const count = members.length, hole = .06, outer = .46, golden = Math.PI * (3 - Math.sqrt(5));
      const unit = .29 / Math.sqrt(count), maximum = size === 'equal' ? 14 : size === 'fit' ? 24 : 28;
      const nodes = members.map((n, i) => {
        const r = Math.sqrt(hole * hole + (outer * outer - hole * hole) * (i + .5) / count);
        const portraitRadius = unit * radiusFor(n, 1, size) / maximum;
        let hash=2166136261; for(const char of String(n.id)) hash=Math.imul(hash^char.charCodeAt(0),16777619);
        hash^=hash>>>16; hash=Math.imul(hash,0x7feb352d); hash^=hash>>>15;
        // Rotate inside the available clearance: organic spacing without changing
        // anyone's evidence distance or allowing adjacent circles to overlap.
        const jitter=((hash>>>0)/4294967296*2-1)*(1.25*unit-portraitRadius);
        const angle = i * golden + jitter/r;
        return { ...n, evidenceX: n.evidenceX ?? n.x, evidenceY: n.evidenceY ?? n.y,
          x: .5 + r * Math.cos(angle), y: .5 + r * Math.sin(angle), cohort: true,
          portraitRadius, spacing: unit * 2 };
      });
      if (owner) nodes.push({ ...owner, x: .5, y: .5, cohort: true, spacing: unit * 2 });
      return nodes;
    }
    const weights = members.map(n => radiusFor(n, 1, size));
    const largest = Math.max(...weights), outer = .46, centre = .06;
    let scale = Math.min(.065 / largest, Math.sqrt(.44 * (outer * outer - centre * centre) / weights.reduce((sum, r) => sum + r * r, 0)));
    const candidates = [];
    let seed = 0x6d2b79f5;
    const random = () => {
      seed ^= seed << 13; seed ^= seed >>> 17; seed ^= seed << 5;
      return (seed >>> 0) / 4294967296;
    };
    for (let i = 0; i < Math.max(800, members.length * 48); i++) {
      const distance = outer * Math.sqrt(random()), angle = random() * Math.PI * 2;
      candidates.push({ x: .5 + distance * Math.cos(angle), y: .5 + distance * Math.sin(angle), distance });
    }
    candidates.sort((a, b) => a.distance - b.distance);
    let slots = [];
    // A single radial pass per attempt preserves evidence ordering. Nearby-cell
    // checks use the largest radius, so differently sized portraits cannot overlap.
    for (let attempt = 0; attempt < 8; attempt++) {
      slots = [];
      const cellSize = largest * scale * 2.12, cells = new Map();
      for (const point of candidates) {
        const radius = weights[slots.length] * scale;
        if (point.distance < centre + radius || point.distance + radius > outer) continue;
        // Leave room beneath the owner for its name, without drawing a boundary.
        if (Math.abs(point.x - .5) < .055 + radius && point.y > .5 && point.y < .585 + radius) continue;
        const cx = Math.floor(point.x / cellSize), cy = Math.floor(point.y / cellSize);
        let overlaps = false;
        for (let dy = -1; dy <= 1 && !overlaps; dy++) {
          for (let dx = -1; dx <= 1 && !overlaps; dx++) {
            const nearby = cells.get((cx + dx) + ':' + (cy + dy)) || [];
            overlaps = nearby.some(other => Math.hypot(point.x - other.x, point.y - other.y) < (radius + other.radius) * 1.06);
          }
        }
        if (overlaps) continue;
        const placed = { ...point, radius };
        slots.push(placed);
        const key = cx + ':' + cy;
        if (!cells.has(key)) cells.set(key, []);
        cells.get(key).push(placed);
        if (slots.length === members.length) break;
      }
      if (slots.length === members.length) break;
      scale *= .86;
    }
    if (slots.length !== members.length) {
      // Guaranteed finite fallback for unusually skewed pages: enough square-grid
      // slots inside the disk, each separated by more than the largest diameter.
      const step = Math.min(.12, .35 / Math.sqrt(members.length)), limit = Math.ceil(outer / step);
      scale = step * .45 / largest;
      slots = [];
      for (let y = -limit; y <= limit; y++) for (let x = -limit; x <= limit; x++) {
        const distance = Math.hypot(x * step, y * step), radius = largest * scale;
        if (distance < centre + radius || distance + radius > outer) continue;
        if (Math.abs(x * step) < .055 + radius && y > 0 && y * step < .085 + radius) continue;
        slots.push({ x: .5 + x * step, y: .5 + y * step, distance });
      }
      slots.sort((a, b) => a.distance - b.distance || a.x - b.x || a.y - b.y);
    }
    const nodes = members.map((n, i) => ({ ...n, evidenceX: n.evidenceX ?? n.x, evidenceY: n.evidenceY ?? n.y,
      x: slots[i].x, y: slots[i].y, cohort: true, portraitRadius: weights[i] * scale, spacing: largest * scale * 2.12 }));
    if (owner) nodes.push({ ...owner, x: .5, y: .5, cohort: true, spacing: largest * scale * 2.12 });
    return nodes;
  }

  // Select what can be read at this scale. Screen collisions never alter world coordinates.
  function displayPlan(scene, cam, guides = [], selected = null, blocked = null, ownerId = null, mode = 'closeness', size = 'followers') {
    if (scene.large && scene.spatial) {
      const visible = scene.spatial.query(cam.rect(), 12001);
      const vector = visible.length <= 12000;
      const marks = vector ? visible.map(it => { const [x,y]=cam.toScreen(it.x,it.y); return {kind:'n',it,x,y,r:String(it.d.id)===String(ownerId)?OWNER_RADIUS:it.d.portraitRadius*cam.scale}; }) : [];
      const nodes = marks.filter(mark => mark.r >= 5).sort((a,b) => (a.it.x-.5)**2+(a.it.y-.5)**2-(b.it.x-.5)**2-(b.it.y-.5)**2).slice(0,1000);
      return {nodes,groups:[],large:true,vector,marks};
    }
    if (scene.nodes.some(it => it.d.cohort)) {
      const scale = cam.scale / Math.max(1, cam.k);
      const nodes = scene.nodes.filter(it => it.ta !== 0).map(it => {
        const [x, y] = cam.toScreen(it.x, it.y);
        const owner = String(it.d.id) === String(ownerId);
        const radiusScale = Math.min(1, it.d.spacing * scale * .49 / 15);
        const r = owner ? Math.min(24, OWNER_RADIUS * cam.k) : it.d.portraitRadius ? it.d.portraitRadius * cam.scale : radiusFor(it.d, 1, size) * radiusScale * cam.k;
        return { kind: 'n', it, x, y, r };
      });
      return { nodes, groups: [] };
    }
    const inside = (x, y, pad = 20) => x >= pad && y >= pad && x <= cam.w - pad && y <= cam.h - cam.inset.bottom - pad;
    const occupied = [], summaries = new Map(), nodeMarks = [];
    const guideByLabel = new Map(guides.map(g => [g.label, g]));
    const guideById = new Map(guides.map(g => [String(g.id), g]));
    const overview = cam.k < 2.5;
    const add = (label, count, wx, wy) => {
      const key = overview ? label : label + ':' + Math.floor(wx * cam.scale / 70) + ':' + Math.floor(wy * cam.scale / 70);
      let group = summaries.get(key);
      if (!group) { group = { key, label, count: 0, wx: 0, wy: 0 }; summaries.set(key, group); }
      group.wx += wx * count; group.wy += wy * count; group.count += count;
    };
    for (const it of scene.clusters) {
      if (it.a < .3 || it.ta === 0) continue;
      const [x, y] = cam.toScreen(it.x, it.y); if (!inside(x, y, 0)) continue;
      add(it.d.label || 'People nearby', it.d.count, it.x, it.y);
    }
    const overlaps = (x, y, r) => occupied.some(p => Math.hypot(x - p.x, y - p.y) < r + p.r + 1.5) || (blocked && x + r > blocked.x0 && x - r < blocked.x1 && y + r > blocked.y0 && y - r < blocked.y1);
    const network = ownerId != null || guides.some(g => g.label === 'Direct connections');
    const limit = Math.round(clamp(cam.w * cam.h / 2200 * Math.min(8, cam.k), 120, network && overview ? 420 : 480));
    let candidates = scene.nodes.filter(it => it.a >= .3 && it.ta !== 0).slice().sort((a, b) => {
      const score = it => (((selected && String(it.d.id) === String(selected.id)) || String(it.d.id) === String(ownerId)) ? 1e6 : 0) + (mode === 'closeness' ? (it.d.source ? 40 : 0) + (it.d.status && it.d.status !== 'no' ? 1000 : 0) + (hasRing(it.d) ? 100 : 0) : 0) + (it.d.rank || 0);
      return score(b) - score(a) || String(a.d.id).localeCompare(String(b.d.id));
    });
    if (network && overview) {
      // Keep the strongest few people, then interleave real communities so a dense
      // inner audience cannot hide every outer audience from the overview.
      const priority = candidates.slice(0, 12), buckets = new Map();
      for (const it of candidates.slice(12)) {
        const key = String(it.d.cluster ?? 'unknown');
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(it);
      }
      const balanced = [];
      for (let index = 0, any = true; any; index++) {
        any = false;
        for (const bucket of buckets.values()) if (bucket[index]) { balanced.push(bucket[index]); any = true; }
      }
      candidates = [...priority, ...balanced];
    }
    // Summary anchors are reserved first so individual dots cannot cover their counts.
    const groups = [...summaries.values()].sort((a, b) => b.count - a.count);
    const groupMarks = [];
    for (const g of groups) {
      const anchor = overview && guideByLabel.get(g.label);
      g.wx /= g.count; g.wy /= g.count;
      if (anchor) { g.wx = anchor.x; g.wy = anchor.y; }
      const [x, y] = cam.toScreen(g.wx, g.wy), r = 18;
      if (!inside(x, y) || overlaps(x, y, r)) continue;
      const d = { id: 'display:' + g.key, count: g.count, label: g.label, x: g.wx, y: g.wy };
      const mark = { kind: 'c', it: { d, x: g.wx, y: g.wy, a: 1 }, x, y, r };
      groupMarks.push(mark); occupied.push(mark);
    }
    for (const it of candidates) {
      const [x, y] = cam.toScreen(it.x, it.y), r = String(it.d.id) === String(ownerId) ? OWNER_RADIUS * cam.k : radiusFor(it.d, cam.k, size);
      if (!inside(x, y, 8)) continue;
      const pinned = (selected && String(it.d.id) === String(selected.id)) || String(it.d.id) === String(ownerId);
      if (!pinned && (nodeMarks.length >= limit || overlaps(x, y, r))) {
        const guide = guideById.get(String(it.d.cluster));
        const label = guide?.label || 'People nearby';
        const key = overview ? label : label + ':' + Math.floor(it.x * cam.scale / 70) + ':' + Math.floor(it.y * cam.scale / 70);
        const grouped = groupMarks.find(g => g.it.d.id === 'display:' + key);
        if (grouped) grouped.it.d.count++;
        continue;
      }
      const mark = { kind: 'n', it, x, y, r }; nodeMarks.push(mark); occupied.push(String(it.d.id) === String(ownerId) ? { ...mark, r: 46 } : mark);
      if (String(it.d.id) === String(ownerId)) occupied.push({ x, y: y + 39, r: 44 });
    }
    return { nodes: nodeMarks, groups: groupMarks };
  }

  // Only saved local photos are eligible. Cap decoded images and in-flight requests;
  // drawing thousands of records must never enqueue thousands of image downloads.
  class PortraitCache {
    constructor({createImage, normalize = null, changed = () => {}, max = 1024, concurrency = 6} = {}) {
      this.createImage = createImage; this.normalize = normalize; this.changed = changed; this.max = max;
      this.concurrency = concurrency; this.entries = new Map(); this.queue = []; this.active = 0;
    }
    get(url) {
      if (typeof url !== 'string' || !/^\/img\/\d+$/.test(url)) return null;
      let entry = this.entries.get(url);
      if (entry) { this.entries.delete(url); this.entries.set(url, entry); return entry.state === 'ready' ? entry.image : null; }
      if (this.entries.size >= this.max) {
        const victim = [...this.entries].find(([,e]) => e.state !== 'loading');
        if (!victim) return null;
        victim[1].image?.close?.();
        this.entries.delete(victim[0]);
        this.queue = this.queue.filter(e => e !== victim[1]);
      }
      entry = {url, state:'queued', image:null}; this.entries.set(url, entry); this.queue.push(entry); this.pump(); return null;
    }
    pump() {
      while (this.active < this.concurrency && this.queue.length) {
        const entry = this.queue.shift();
        if (this.entries.get(entry.url) !== entry) continue;
        entry.state = 'loading'; this.active++;
        const image = entry.image = this.createImage(); image.decoding = 'async';
        const finish = (ready) => {
          if (entry.state !== 'loading') return;
          entry.state = ready ? 'ready' : 'failed'; this.active--;
          image.onload = image.onerror = null;
          if (!ready) { entry.image = null; image.src = ''; }
          this.pump(); this.changed();
        };
        image.onload = () => {
          if (!image.naturalWidth) return finish(false);
          if (!this.normalize) return finish(true);
          Promise.resolve().then(() => this.normalize(image)).then(decoded => { entry.image = decoded; image.src = ''; finish(true); }, () => finish(false));
        }; image.onerror = () => finish(false); image.src = entry.url;
      }
    }
  }

  /* ---------- Labels ---------- */
  // Greedy placement: highest priority first; each label tries right, left, above, below of its marker;
  // it is skipped when every spot collides. A coarse grid keeps the collision test cheap.
  class Labeler {
    constructor(w, h, cell = 48) { this.w = w; this.h = h; this.cell = cell; this.grid = new Map(); this.out = []; }
    _keys(b, fn) {
      const c = this.cell;
      for (let gx = Math.floor(b[0] / c); gx <= Math.floor(b[2] / c); gx++) for (let gy = Math.floor(b[1] / c); gy <= Math.floor(b[3] / c); gy++) if (fn(gx + ',' + gy)) return true;
      return false;
    }
    hit(b) { return this._keys(b, (k) => { const l = this.grid.get(k); if (!l) return false; for (const o of l) if (b[0] < o[2] && b[2] > o[0] && b[1] < o[3] && b[3] > o[1]) return true; return false; }); }
    block(b) { this._keys(b, (k) => { let l = this.grid.get(k); if (!l) this.grid.set(k, l = []); l.push(b); return false; }); }
    // marker: {x, y, r}; size: {w, h}; prefer: side used last time, for stability.
    place(marker, size, prefer) {
      const { x, y, r } = marker, g = 4, { w, h } = size;
      const spots = { right: [x + r + g, y - h / 2], left: [x - r - g - w, y - h / 2], top: [x - w / 2, y - r - g - h], bottom: [x - w / 2, y + r + g] };
      const order = prefer ? [prefer, ...['right', 'left', 'top', 'bottom'].filter((s) => s !== prefer)] : ['right', 'left', 'top', 'bottom'];
      for (const side of order) {
        const [bx, by] = spots[side], b = [bx, by, bx + w, by + h];
        if (b[0] < 4 || b[1] < 4 || b[2] > this.w - 4 || b[3] > this.h - 4) continue;
        if (this.hit(b)) continue;
        this.block(b); return { x: bx, y: by, side };
      }
      return null;
    }
  }

  const api = { clamp, int, plural, compact, easeOut, easeInOut, MODES, FIT_LABEL, STATUS_LABEL, K_MIN, K_MAX, Camera, Flight, SpatialIndex, snapRect, viewQuery, Cache, Latest, debounceMax, Scene, RADIUS, SIZE_OPTIONS, SIZE_HELP, followRing, cohortLayout, hasRing, toneFor, radiusFor, LEGENDS, whyLine, closenessWords, Labeler, displayPlan, PortraitCache, OWNER_RADIUS };
  root.MapCore = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window === 'undefined' ? globalThis : window);
