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
  const easeOut = (t) => 1 - Math.pow(1 - clamp(t, 0, 1), 5);
  const easeInOut = (t) => { t = clamp(t, 0, 1); return t < 0.5 ? 16 * Math.pow(t, 5) : 1 - Math.pow(-2 * t + 2, 5) / 2; };

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
        const fromRadius = it.d.portraitRadius, toRadius = d.portraitRadius;
        const radiusChanged = Number.isFinite(fromRadius) && Number.isFinite(toRadius) && Math.abs(fromRadius - toRadius) > 1e-9;
        it.d = morph && radiusChanged ? { ...d, portraitRadius: fromRadius } : d; it.ta = 1; if(o.instant)it.a=1;
        if (radiusChanged || Math.abs(it.tx - d.x) > 1e-7 || Math.abs(it.ty - d.y) > 1e-7) {
          it.tx = d.x; it.ty = d.y;
          if (morph) { it.fx = it.x; it.fy = it.y; it.t0 = now; it.dur = morph; it.fr = fromRadius; it.tr = toRadius; } else { it.x = d.x; it.y = d.y; it.dur = 0; }
        }
      };
      for (const n of resp.nodes || []) put('n', n);
      for (const c of resp.clusters || []) put('c', c);
      for (const [key,it] of this.items) if (!seen.has(key)) { if(o.instant && !it.pin)this.items.delete(key); else it.ta=it.pin?1:0; }
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
      // Dense pages reuse a photo raster; individual records stay in the spatial index.
      this.large = this.nodes.reduce((count,it)=>count+(it.ta!==0?1:0),0) > 1500; this.spatial = this.large ? new SpatialIndex(this.nodes) : null; this.version = (this.version || 0) + 1;
    }
    clear() { this.items.clear(); this.nodes = []; this.clusters = []; this.animating = false; this.pinned = null; this.large = false; this.spatial = null; }
    // Advance by dt seconds. Returns true while anything is still moving, so the loop can stop when idle.
    step(now, dt, reduced) {
      if (this.large && !this.animating) return false;
      let busy = false, dead = false, moved = false;
      const k = reduced ? 1 : 1 - Math.exp(-dt / 0.085);
      for (const it of this.items.values()) {
        if (it.a !== it.ta) {
          it.a += (it.ta - it.a) * k;
          if (Math.abs(it.ta - it.a) < 0.01) it.a = it.ta; else busy = true;
          if (it.a === 0 && it.ta === 0) { it.dead = true; dead = true; }
        }
        if (it.dur) {
          const t = reduced ? 1 : (now - it.t0) / it.dur, e = easeOut(t);
          moved = true;
          if (Number.isFinite(it.fr) && Number.isFinite(it.tr)) it.d.portraitRadius = it.fr + (it.tr - it.fr) * e;
          it.x = it.fx + (it.tx - it.fx) * e; it.y = it.fy + (it.ty - it.fy) * e;
          if (t >= 1) { it.dur = 0; it.x = it.tx; it.y = it.ty; } else busy = true;
        }
      }
      if (dead) { for (const [key, it] of this.items) if (it.dead) this.items.delete(key); this.rebuild(); }
      if (moved && this.large && !dead) this.spatial = new SpatialIndex(this.nodes);
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
  const OWNER_RADIUS = 18;
  const SIZE_OPTIONS = [{id:'followers',label:'Followers'}, {id:'fit',label:'Fit'}, {id:'connections',label:'Shared sources'}, {id:'equal',label:'Equal'}];
  const SIZE_HELP = {
    followers:'Size uses a modest saved-follower scale and shrinks gradually farther from you. Unknown follower counts stay unknown.',
    fit:'Larger bubbles have a stronger saved fit assessment. Unread profiles use the smallest size.',
    connections:'Larger bubbles appear in more collected audiences. Shared sources do not establish a relationship.',
    equal:'Every person has the same bubble size. Distance still uses the selected view.'
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

  function distanceValue(n, mode = 'network') {
    if (mode === 'fit') return {strong:3,good:2,weak:1,unread:0}[n.fit] ?? 0;
    const value = mode === 'shared' ? n.source_count : n.closeness;
    return value != null && Number.isFinite(+value) ? +value : -1;
  }
  function cohortLayout(people, owner, size = 'followers', distance = 'network') {
    const members = people.filter(n => String(n.id) !== String(owner?.id));
    members.sort((a,b) => distanceValue(b,distance)-distanceValue(a,distance)
      || (b.closeness||0)-(a.closeness||0) || (b.rank||0)-(a.rank||0) || String(a.id).localeCompare(String(b.id)));
    if (!members.length) return owner ? [{...owner,x:.5,y:.5,cohort:true,spacing:.075}] : [];
    // Distance is a continuous ordering of the loaded people. Ties occupy the
    // available space; they do not acquire an invented strength or relationship.
    // A single organic disk avoids score bands and the gaps they create.
    const count=members.length, centre=.06, outer=.46;
    const weights=members.map((n,i)=>radiusFor(n,1,size)
      * (size==='equal'?1:1-.24*Math.pow(i/Math.max(1,count-1),.8)));
    const largest=weights.reduce((max,r)=>Math.max(max,r),0);
    let scale=Math.min(.055*(1-1e-12)/largest,Math.sqrt(.5*(outer*outer-centre*centre)/weights.reduce((sum,r)=>sum+r*r,0)));
    const candidateCount=Math.max(800,count*32);
    let slots=[];
    // Stream deterministic random-angle candidates rather than generating a
    // regular spiral, lattice, or millions of candidate objects. Radius grows
    // continuously, preserving evidence order even when values are discrete.
    for(let attempt=0;attempt<24;attempt++) {
      slots=[];let seed=0x6d2b79f5;
      const cell=largest*scale*2.12, columns=Math.ceil(1/cell)+2,cells=new Map();
      for(let i=0;i<candidateCount;i++) {
        seed^=seed<<13;seed^=seed>>>17;seed^=seed<<5;
        const angle=(seed>>>0)/4294967296*Math.PI*2;
        const d=Math.sqrt(centre*centre+(outer*outer-centre*centre)*(i+.5)/candidateCount);
        const r=weights[slots.length]*scale;
        if(d<centre+r || d+r>outer)continue;
        const x=.5+d*Math.cos(angle),y=.5+d*Math.sin(angle);
        const cx=Math.floor(x/cell),cy=Math.floor(y/cell);let overlap=false;
        for(let dy=-1;dy<=1&&!overlap;dy++)for(let dx=-1;dx<=1&&!overlap;dx++)
          for(const other of cells.get((cy+dy)*columns+cx+dx)||[]) {
            const gap=(r+other.r)*1.06,dx=x-other.x,dy=y-other.y;
            if(dx*dx+dy*dy<gap*gap){overlap=true;break;}
          }
        if(overlap)continue;
        const point={x,y,r};slots.push(point);
        const key=cy*columns+cx;if(!cells.has(key))cells.set(key,[]);cells.get(key).push(point);
        if(slots.length===count)break;
      }
      if(slots.length===count)break;
      // Keep the same organic candidates at a smaller scale. Never fall back to
      // a geometric pattern for a skewed page or a page containing only ties.
      scale*=.91;
    }
    if(slots.length!==count)throw new Error('Could not space the loaded profiles.');
    const nodes=members.map((n,i)=>({...n,evidenceX:n.evidenceX??n.x,evidenceY:n.evidenceY??n.y,
      x:slots[i].x,y:slots[i].y,cohort:true,portraitRadius:weights[i]*scale,spacing:largest*scale*2.12}));
    if(owner)nodes.push({...owner,x:.5,y:.5,cohort:true,spacing:largest*scale*2.12});
    return nodes;
  }

  // Select what can be read at this scale. Screen collisions never alter world coordinates.
  function displayPlan(scene, cam, guides = [], selected = null, blocked = null, ownerId = null, mode = 'closeness', size = 'followers') {
    if (scene.large && scene.spatial) {
      const visible = scene.spatial.query(cam.rect(), 12001);
      const vector = visible.length <= 12000;
      const marks = vector ? visible.map(it => { const [x,y]=cam.toScreen(it.x,it.y); return {kind:'n',it,x,y,r:String(it.d.id)===String(ownerId)?Math.min(28,OWNER_RADIUS*cam.k):it.d.portraitRadius*cam.scale}; }) : [];
      const priority=(a,b)=>(a.x-.5)**2+(a.y-.5)**2-(b.x-.5)**2-(b.y-.5)**2;
      // Keep a bounded portrait layer over the cached overview as well. A dense
      // page uses every saved face in the raster, promoting readable faces on zoom.
      const nodes = vector ? marks.filter(mark=>mark.r>=6 || String(mark.it.d.id)===String(ownerId) || String(mark.it.d.id)===String(selected?.id)).sort((a,b)=>priority(a.it,b.it)).slice(0,1000)
        : visible.filter(it=>it.d.portraitRadius*cam.scale>=6 || String(it.d.id)===String(ownerId) || String(it.d.id)===String(selected?.id)).sort(priority).slice(0,1000)
          .map(it=>{const[x,y]=cam.toScreen(it.x,it.y);return{kind:'n',it,x,y,r:String(it.d.id)===String(ownerId)?Math.min(28,OWNER_RADIUS*cam.k):it.d.portraitRadius*cam.scale};});
      return {nodes,groups:[],large:true,vector,marks};
    }
    if (scene.nodes.some(it => it.d.cohort)) {
      const scale = cam.scale / Math.max(1, cam.k);
      const nodes = scene.nodes.filter(it => it.a > .001 || it.ta !== 0).map(it => {
        const [x, y] = cam.toScreen(it.x, it.y);
        const owner = String(it.d.id) === String(ownerId);
        const radiusScale = Math.min(1, it.d.spacing * scale * .49 / 15);
        const r = owner ? Math.min(28, OWNER_RADIUS * cam.k) : it.d.portraitRadius ? it.d.portraitRadius * cam.scale : radiusFor(it.d, 1, size) * radiusScale * cam.k;
        return { kind: 'n', it, x, y, r };
      });
      return { nodes: nodes.filter(n => n.x+n.r>=0 && n.y+n.r>=0 && n.x-n.r<=cam.w && n.y-n.r<=cam.h), groups: [] };
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
    constructor({createImage, normalize = null, changed = () => {}, max = 1024, concurrency = 6, now = () => performance.now()} = {}) {
      this.createImage = createImage; this.normalize = normalize; this.changed = changed; this.max = max;
      this.concurrency = concurrency; this.entries = new Map(); this.queue = []; this.active = 0; this.now = now; this.wanted = new Set();
    }
    peek(url) { const entry=this.entries.get(url); return entry?.state === 'ready' ? entry.image : null; }
    reveal(url, now, reduced = false) {
      const entry=this.entries.get(url);
      if(entry?.state !== 'ready')return 0;
      // Decode time is monotonic and set once: redraws never restart the reveal.
      return reduced ? 1 : easeOut((now-entry.readyAt)/280);
    }
    // Queue the current view first. Old, not-yet-started requests can wait for a
    // later visit instead of delaying photos at the new pan/zoom location.
    prioritize(urls) {
      // Admission is bounded and stable across paint frames. Without this guard,
      // an oversized view continually evicts its own ready faces and restarts fades.
      const wanted = new Set();
      for (const url of urls) {
        if (typeof url === 'string' && /^\/img\/\d+$/.test(url)) wanted.add(url);
        if (wanted.size === this.max) break;
      }
      this.wanted = wanted;
      for(const [url,entry] of this.entries)if(entry.state==='queued' && !wanted.has(url))this.entries.delete(url);
      this.queue=this.queue.filter(entry=>wanted.has(entry.url));
      const order=new Map([...wanted].map((url,i)=>[url,i]));
      this.queue.sort((a,b)=>order.get(a.url)-order.get(b.url));
      for(const url of wanted)this.get(url);
    }
    get(url) {
      if (typeof url !== 'string' || !/^\/img\/\d+$/.test(url)) return null;
      let entry = this.entries.get(url);
      if (entry) { this.entries.delete(url); this.entries.set(url, entry); return entry.state === 'ready' ? entry.image : null; }
      if (this.entries.size >= this.max) {
        let victim;
        for (const pair of this.entries) {
          // A visible face, active decode, or bitmap waiting for raster transfer
          // still has an owner. Closing it here would flicker or detach its pixels.
          if (pair[1].state !== 'loading' && !pair[1].retained && !this.wanted.has(pair[0])) { victim = pair; break; }
        }
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
          entry.state = ready ? 'ready' : 'failed'; if(ready)entry.readyAt=this.now(); this.active--;
          image.onload = image.onerror = null;
          if (!ready) { entry.image = null; image.src = ''; }
          this.pump(); this.changed(entry);
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

  const api = { clamp, int, plural, compact, easeOut, easeInOut, MODES, FIT_LABEL, STATUS_LABEL, K_MIN, K_MAX, Camera, Flight, SpatialIndex, snapRect, viewQuery, Cache, Latest, debounceMax, Scene, RADIUS, SIZE_OPTIONS, SIZE_HELP, followRing, cohortLayout, distanceValue, hasRing, toneFor, radiusFor, LEGENDS, whyLine, closenessWords, Labeler, displayPlan, PortraitCache, OWNER_RADIUS };
  root.MapCore = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window === 'undefined' ? globalThis : window);
