/* Connections map view: paints the model on a canvas, owns the toolbar, legend,
 * side card, states and keyboard. Data flow lives in map-model.js; encodings and
 * copy in map-core.js. One canvas, one frame loop that sleeps when nothing moves. */
(function (root) {
  'use strict';
  const Core = root.MapCore, { MapModel } = root.MapModel;
  const { MODES, LEGENDS, FIT_LABEL, STATUS_LABEL, int, plural, compact, clamp, Labeler, whyLine, toneFor, radiusFor, hasRing } = Core;
  const TAU = Math.PI * 2;
  const doc = root.document;

  const h = (tag, attrs, ...kids) => {
    const el = doc.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'text') el.textContent = v; else if (k === 'class') el.className = v; else el.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat()) if (kid != null) el.append(kid);
    return el;
  };
  const initials = (n) => String(n.name || n.handle || '?').replace(/[^\p{L}\p{N} ]/gu, ' ').trim().split(/\s+/).slice(0, 2).map((s) => s[0]).join('').toUpperCase() || '?';
  const reducedMotion = () => !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const SLABEL = (s) => STATUS_LABEL[s] || (s ? s.charAt(0).toUpperCase() + s.slice(1) : '');

  // Turn a CSS colour into [r,g,b] using the canvas itself, so any notation works.
  function rgbOf(ctx, css, fallback) {
    ctx.fillStyle = '#000'; ctx.fillStyle = css || fallback;
    const s = ctx.fillStyle;
    if (s[0] === '#') return [parseInt(s.slice(1, 3), 16), parseInt(s.slice(3, 5), 16), parseInt(s.slice(5, 7), 16)];
    const m = s.match(/[\d.]+/g); return m ? m.slice(0, 3).map(Number) : [128, 128, 128];
  }
  const mix = (a, b, t) => `rgb(${a.map((v, i) => Math.round(v + (b[i] - v) * t)).join(',')})`;

  class MapView {
    constructor(refs, host) {
      this.r = refs; this.host = host;
      this.canvas = refs.canvas; this.ctx = this.canvas.getContext('2d');
      this.model = new MapModel({
        fetchJson: (u, o) => host.fetchJson(u, o), reduced: reducedMotion(), online: () => root.navigator.onLine !== false,
        budget: Number(new URLSearchParams(root.location.search).get('mapbudget')) || 0
      });
      this.shown = false; this.raf = 0; this.last = 0; this.dpr = 1; this.hover = null; this.textW = new Map(); this.labelA = new Map();
      this.hud = null; this.busyTimer = 0; this.edgeAt = 0; this.stats = { frames: 0, paintMs: 0, maxMs: 0 };
      this.palette = null; this.results = []; this.active = -1; this.note = null;
      this.model.on((what) => this.onModel(what));
      this.buildStatic();
      this.bind();
      this.measure(true);
      this.renderLegend();
    }
    get comparing() { return this.r.pane.classList.contains('comparing'); }

    /* ---------- model events ---------- */
    onModel(what) {
      if (what === 'scene' || what === 'camera' || what === 'edges') this.invalidate();
      if (what === 'edges') { this.edgeAt = performance.now(); this.renderSeeds(); }
      if (what === 'phase' || what === 'scene') { this.renderModes(); this.renderLegend(); this.renderState(); this.renderCount(); }
      if (what === 'busy' || what === 'scene' || what === 'phase') this.renderBusy();
      if (what === 'stale') this.renderStale();
      if (what === 'scene') this.renderStale();
      if (what === 'selection') { if (!this.model.selected || this.cardId !== this.model.selected.id) this.renderCard(); else this.updateCardFacts(); this.invalidate(); }
      if (what === 'mode') { this.renderModes(); this.renderLegend(); this.invalidate(); }
      if (what === 'filters') this.renderFilters();
    }

    /* ---------- lifecycle ---------- */
    show() {
      this.shown = true;
      this.measure(true); this.readPalette();
      this.model.pause(this.comparing);
      clearInterval(this.poller);
      this.poller = setInterval(() => { if (!doc.hidden && this.shown) this.model.poll(); }, 45000);
      if (!this.comparing) { if (this.model.phase === 'idle' || this.model.phase === 'error' || this.model.phase === 'offline') this.model.load(); }
      this.invalidate();
    }
    hide() { this.shown = false; clearInterval(this.poller); this.model.pause(true); this.closeResults(); this.hideTip(); }
    resize() { this.measure(false); }
    measure(first) {
      const box = this.r.canvasBox, w = box.clientWidth, h = box.clientHeight;
      if (!w || !h) return;
      const dpr = Math.min(root.devicePixelRatio || 1, 2), cam = this.model.cam;
      if (w === cam.w && h === cam.h && dpr === this.dpr && !first) return;
      const keep = cam.toWorld(0, 0), had = this.sized;
      this.dpr = dpr; this.canvas.width = Math.round(w * dpr); this.canvas.height = Math.round(h * dpr);
      this.model.setSize(w, h);
      if (had) cam.lock(keep[0], keep[1], 0, 0, cam.k);
      this.sized = true;
      const narrow = root.matchMedia('(max-width: 760px)').matches, sheet = this.r.card.hidden || !narrow ? 0 : this.r.card.offsetHeight;
      cam.inset = { top: 0, right: 0, bottom: sheet, left: 0 };
      const legend = this.r.hud.getBoundingClientRect(), stage = box.getBoundingClientRect();
      this.hud = { x0: legend.left - stage.left - 8, y0: legend.top - stage.top - 8, x1: legend.right - stage.left + 8, y1: legend.bottom - stage.top + 8 };
      if (this.model.needsLoad() && this.shown && !this.comparing) this.model.schedule();
      this.invalidate();
    }
    readPalette() {
      const cs = root.getComputedStyle(doc.documentElement), v = (n, f) => cs.getPropertyValue(n).trim() || f, ctx = this.ctx;
      const fg = rgbOf(ctx, v('--fg', '#ededed')), bg = rgbOf(ctx, v('--bg', '#0d0d0d'));
      const tone = (t) => mix(fg, bg, t);
      this.palette = {
        fg: v('--fg', '#ededed'), fg2: v('--fg2', '#b5b5b5'), fg3: v('--fg3', '#969696'), fg4: v('--fg4', '#8d8d8d'), bg: v('--bg', '#0d0d0d'), bg1: v('--bg1', '#141414'), bg2: v('--bg2', '#1c1c1c'),
        line: v('--line', '#202020'), line2: v('--line2', '#303030'), line3: v('--line3', '#454545'), accent: v('--t-map-strong', '#3977ff'), sans: v('--sans', 'system-ui, sans-serif'),
        tones: { t0: tone(0.12), t1: tone(0.26), t2: tone(0.38), t3: tone(0.5), t4: tone(0.6), t5: tone(0.68) }
      };
      this.palette.tones.a = this.palette.accent;
      this.dark = doc.documentElement.dataset.theme !== 'light';
    }

    /* ---------- frame loop ---------- */
    invalidate() { if (!this.raf && this.shown) this.raf = root.requestAnimationFrame((t) => this.frame(t)); }
    frame(t) {
      this.raf = 0;
      const dt = this.last ? Math.min(0.05, (t - this.last) / 1000) : 0.016; this.last = t;
      let busy = this.model.tick(t, dt);
      const t0 = performance.now();
      if (this.shown && this.sized) busy = this.paint(t, dt) || busy;
      const ms = performance.now() - t0;
      this.stats.frames++; this.stats.paintMs += (ms - this.stats.paintMs) * 0.1; this.stats.maxMs = Math.max(this.stats.maxMs * 0.995, ms); this.stats.last = ms;
      if (busy) this.invalidate(); else this.last = 0;
    }

    /* ---------- painting ---------- */
    text(font, s) {
      const key = font + '|' + s; let w = this.textW.get(key);
      if (w === undefined) { this.ctx.font = font; w = this.ctx.measureText(s).width; if (this.textW.size > 3000) this.textW.clear(); this.textW.set(key, w); }
      return w;
    }
    paint(now, dt) {
      if (!this.palette) this.readPalette();
      const ctx = this.ctx, P = this.palette, m = this.model, cam = m.cam, W = cam.w, H = cam.h, S = cam.scale, mx = cam.mid[0], my = cam.mid[1];
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      if (!m.scene.nodes.length && !m.scene.clusters.length && !m.selected) { this.labelA.clear(); return false; }
      const sx = (x) => (x - cam.cx) * S + mx, sy = (y) => (y - cam.cy) * S + my;
      const lab = new Labeler(W, H);
      if (this.hud) lab.block([this.hud.x0, this.hud.y0, this.hud.x1, this.hud.y1]);
      const want = [];   // labels to place, in priority order
      const font = (w, px) => `${w} ${px}px ${P.sans}`;

      this.paintGuides(ctx, P, m, cam, S, sx, sy, want);
      const sel = m.selected, edges = m.edges && sel && m.edges.id === sel.id ? m.edges : null;
      const dim = edges && edges.state === 'ready' ? 0.5 : 1;

      // Bubbles: groups of people that do not fit as individuals at this zoom.
      const maxC = m.scene.maxCluster, hovered = this.hover;
      ctx.lineWidth = 1;
      const bubbles = [];
      for (const it of m.scene.clusters) {
        const x = sx(it.x), y = sy(it.y);
        const r = 9 + 30 * Math.sqrt(it.d.count / maxC);
        if (x < -r || y < -r || x > W + r || y > H + r) continue;
        bubbles.push({ it, x, y, r });
        ctx.globalAlpha = it.a * (edges ? 0.6 : 1);
        ctx.fillStyle = P.bg2; ctx.strokeStyle = hovered && hovered.it === it ? P.fg3 : P.line3;
        ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.fill(); ctx.stroke();
        if (r >= 14) {
          ctx.globalAlpha = it.a; ctx.fillStyle = P.fg2; ctx.font = font(500, 11); ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
          ctx.fillText(compact(it.d.count), x, y + 0.5);
          lab.block([x - r, y - r, x + r, y + r]);
        }
      }
      ctx.globalAlpha = 1; ctx.textAlign = 'left';

      // Lines for the selected person, under the dots.
      if (edges && edges.state === 'ready') this.paintEdges(ctx, P, edges, sx, sy, now, sel);

      // People. Opaque ones are batched by tone; fading ones draw one by one.
      const k = cam.k, mode = m.mode, batches = new Map(), rings = [], fading = [];
      for (const it of m.scene.nodes) {
        const d = it.d, x = sx(it.x), y = sy(it.y);
        if (x < -14 || y < -14 || x > W + 14 || y > H + 14) continue;
        const r = radiusFor(d, k), tone = toneFor(mode, d);
        if (it.a < 0.98) { fading.push([x, y, r, tone, it.a]); if (hasRing(d)) rings.push([x, y, r, it.a]); continue; }
        let b = batches.get(tone); if (!b) batches.set(tone, b = []);
        b.push(x, y, r);
        if (hasRing(d)) rings.push([x, y, r, 1]);
      }
      const q = P.tones;
      ctx.globalAlpha = dim;
      for (const [tone, list] of batches) {
        ctx.fillStyle = q[tone]; ctx.beginPath();
        for (let i = 0; i < list.length; i += 3) { ctx.moveTo(list[i] + list[i + 2], list[i + 1]); ctx.arc(list[i], list[i + 1], list[i + 2], 0, TAU); }
        ctx.fill();
      }
      for (const [x, y, r, tone, a] of fading) { ctx.globalAlpha = a * dim; ctx.fillStyle = q[tone]; ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.fill(); }
      ctx.lineWidth = 1.4; ctx.strokeStyle = P.fg;
      for (const [x, y, r, a] of rings) { ctx.globalAlpha = a * dim * 0.9; ctx.beginPath(); ctx.arc(x, y, r + 2.6, 0, TAU); ctx.stroke(); }
      ctx.globalAlpha = 1;

      // You, in the middle of the closeness view.
      const me = m.world && m.world.me && mode === 'closeness' ? m.world.me : null;
      if (me) {
        const x = sx(me.x), y = sy(me.y);
        ctx.fillStyle = P.fg; ctx.beginPath(); ctx.arc(x, y, 4.5, 0, TAU); ctx.fill();
        ctx.strokeStyle = P.fg; ctx.lineWidth = 1.2; ctx.beginPath(); ctx.arc(x, y, 9, 0, TAU); ctx.stroke();
        want.unshift({ key: 'me', text: 'You', x, y, r: 10, w: 600, prefer: 'bottom', strong: true });
      }

      // Endpoints of the selected person's lines, then the selection ring.
      if (edges && edges.state === 'ready') for (const n of edges.seeds) {
        const x = sx(n.x), y = sy(n.y); ctx.fillStyle = P.fg; ctx.beginPath(); ctx.arc(x, y, 4, 0, TAU); ctx.fill();
        ctx.strokeStyle = P.bg; ctx.lineWidth = 1.5; ctx.stroke();
        want.unshift({ key: 'e:' + n.id, text: '@' + n.handle, x, y, r: 5, w: 500 });
      }
      if (sel) {
        const it = m.scene.get(sel.id), x = sx(it ? it.x : sel.x), y = sy(it ? it.y : sel.y), r = radiusFor(sel, k);
        ctx.strokeStyle = P.accent; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(x, y, r + 5, 0, TAU); ctx.stroke();
        ctx.fillStyle = P.accent; ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.fill();
        want.unshift({ key: 'sel', text: sel.name || '@' + sel.handle, x, y, r: r + 6, w: 600, strong: true });
      }
      if (hovered && hovered.it && hovered.kind === 'n' && hovered.it.d !== sel) {
        ctx.strokeStyle = P.fg; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(hovered.x, hovered.y, hovered.r + 4, 0, TAU); ctx.stroke();
      }

      // Labels. Bubbles first (which community is this), then the best-ranked people.
      let nb = 0;
      for (const b of bubbles) {
        if (b.r < 17 || b.it.a < 0.6 || !b.it.d.label) continue;
        want.push({ key: 'c:' + b.it.d.id, text: b.it.d.label, x: b.x, y: b.y, r: b.r, w: 500, prefer: 'bottom' });
        if (++nb >= 12) break;
      }
      const maxPeople = clamp(Math.round(W * H / 30000 * (k < 4 ? 0.7 : 1.4)), 6, 64);
      let np = 0;
      for (let i = m.scene.nodes.length - 1; i >= 0 && np < maxPeople; i--) {
        const it = m.scene.nodes[i], d = it.d;
        if (it.a < 0.9 || (sel && d.id === sel.id)) continue;
        const x = sx(it.x), y = sy(it.y);
        if (x < 8 || y < 8 || x > W - 8 || y > H - 8) continue;
        want.push({ key: 'n:' + d.id, text: d.name || '@' + d.handle, x, y, r: radiusFor(d, k) + 1, w: 500, dimmed: !!edges });
        np++;
      }
      this.paintLabels(ctx, P, lab, want, dt);
      return this.labelsBusy;
    }
    paintGuides(ctx, P, m, cam, S, sx, sy, want) {
      const guides = (m.world && m.world.guides) || [];
      const W = cam.w, H = cam.h;
      ctx.lineWidth = 1; ctx.strokeStyle = P.line2; ctx.fillStyle = P.line2;
      const fadeIn = 1;
      ctx.globalAlpha = fadeIn;
      let laneLine = null;
      for (const g of guides) {
        if (g.type === 'ring') {
          const x = sx(g.x), y = sy(g.y), r = g.r * S;
          if (r < 6 || r > 6000 || x + r < 0 || x - r > W || y + r < 0 || y - r > H) continue;
          ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.stroke();
          // Name the ring where it crosses the top of the map, or the top of the screen if you have zoomed in.
          const ty = y - r, gy = clamp(ty, 22, H - 22);
          if (ty > 0 && ty < H && x > 30 && x < W - 30) want.push({ key: 'g:' + g.label, text: g.label, x, y: ty, r: 0, w: 500, guide: true, prefer: 'top' });
          else if (ty <= 0 && x > -1) { /* off-screen: no label */ }
        } else if (g.type === 'island') {
          const x = sx(g.x), y = sy(g.y), r = g.r * S;
          if (r < 5 || x + r < 0 || x - r > W || y + r < 0 || y - r > H) continue;
          ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.stroke();
          if (r > 14) want.push({ key: 'g:' + g.label, text: g.label, x, y: y - r, r: 0, w: 500, guide: true, prefer: 'top', prio: g.weight || 0 });
        } else if (g.type === 'lane') {
          const x = sx(g.x), y = sy(g.y);
          if (x > -40 && x < W + 40) want.push({ key: 'g:' + g.label, text: g.label, x, y: Math.max(y, 12), r: 0, w: 600, guide: true, prefer: 'bottom', noPill: true });
        } else if (g.type === 'axis') {
          const y = sy(g.y), a = sx(g.x0), b = sx(g.x1);
          if (y > 0 && y < H) { ctx.beginPath(); ctx.moveTo(a, y); ctx.lineTo(b, y); ctx.stroke(); }
          if (y > 14 && y < H) {
            want.push({ key: 'g:from', text: g.from, x: a, y: y - 12, r: 0, w: 500, guide: true, prefer: 'right' });
            want.push({ key: 'g:to', text: g.to, x: b, y: y - 12, r: 0, w: 500, guide: true, prefer: 'left' });
          }
        }
      }
      if (m.mode === 'status') {
        // Thin dividers between the pipeline columns.
        const xs = guides.filter((g) => g.type === 'lane').map((g) => g.x).sort((a, b) => a - b);
        ctx.setLineDash([2, 6]);
        for (let i = 1; i < xs.length; i++) {
          const x = sx((xs[i - 1] + xs[i]) / 2 + (i === 1 ? 0.02 : 0));
          if (x < 0 || x > W) continue;
          ctx.beginPath(); ctx.moveTo(x, Math.max(0, sy(0.07))); ctx.lineTo(x, Math.min(H, sy(0.97))); ctx.stroke();
        }
        ctx.setLineDash([]);
      }
      ctx.globalAlpha = 1;
    }
    paintEdges(ctx, P, edges, sx, sy, now, sel) {
      const a = clamp((now - this.edgeAt) / 260, 0, 1), reduced = this.model.reduced;
      ctx.globalAlpha = reduced ? 1 : a;
      for (const l of edges.lines) {
        const follows = l.kind === 'follows' || l.kind === 'follows_you';
        ctx.strokeStyle = follows ? P.accent : P.fg3; ctx.lineWidth = follows ? 1.25 : 1;
        ctx.setLineDash(l.kind === 'mutual' ? [4, 4] : []);
        ctx.beginPath(); ctx.moveTo(sx(l.a.x), sy(l.a.y)); ctx.lineTo(sx(l.b.x), sy(l.b.y)); ctx.stroke();
      }
      ctx.setLineDash([]); ctx.globalAlpha = 1;
      if (a < 1 && !reduced) this.invalidate();
    }
    // Greedy placement with a soft fade, so a label never blinks on or off.
    paintLabels(ctx, P, lab, want, dt) {
      const seen = new Set(), fade = this.model.reduced ? 1 : 1 - Math.exp(-dt / 0.06);
      ctx.textBaseline = 'middle'; ctx.lineJoin = 'round';
      const order = want.slice().sort((a, b) => (b.strong ? 1 : 0) - (a.strong ? 1 : 0) || (b.prio || 0) - (a.prio || 0));
      // Selected and "you" first, then guides in the order given, then bubbles, then people.
      const ranked = [...order.filter((w) => w.strong), ...order.filter((w) => !w.strong && w.key.startsWith('e:')), ...order.filter((w) => w.guide), ...order.filter((w) => !w.strong && !w.guide && !w.key.startsWith('e:'))];
      for (const w of ranked) {
        const f = `${w.w} 12px ${P.sans}`, tw = this.text(f, w.text) + 8, size = { w: tw, h: 18 };
        const prev = this.labelA.get(w.key), spot = lab.place({ x: w.x, y: w.y, r: w.r }, size, prev && prev.side || w.prefer);
        if (!spot) continue;
        seen.add(w.key);
        const e = prev || { a: this.model.reduced ? 1 : 0 };
        Object.assign(e, { x: spot.x, y: spot.y, side: spot.side, text: w.text, f, w: tw, strong: w.strong, dimmed: w.dimmed, guide: w.guide, noPill: w.noPill });
        this.labelA.set(w.key, e);
      }
      let busy = false;
      for (const [key, e] of this.labelA) {
        const target = seen.has(key) ? 1 : 0;
        if (e.a !== target) { e.a += (target - e.a) * fade; if (Math.abs(target - e.a) < 0.02) e.a = target; else busy = true; }
        if (e.a === 0 && target === 0) { this.labelA.delete(key); continue; }
        ctx.globalAlpha = e.a * (e.dimmed ? 0.6 : 1); ctx.font = e.f;
        ctx.lineWidth = 4; ctx.strokeStyle = P.bg; ctx.strokeText(e.text, e.x + 4, e.y + 9.5);
        ctx.fillStyle = e.strong ? P.fg : e.guide ? P.fg3 : P.fg2; ctx.fillText(e.text, e.x + 4, e.y + 9.5);
      }
      ctx.globalAlpha = 1; this.labelsBusy = busy;
    }

    /* ---------- picking ---------- */
    pick(px, py) {
      const m = this.model, cam = m.cam, S = cam.scale, mx = cam.mid[0], my = cam.mid[1], k = cam.k;
      let best = null, bd = Infinity;
      const reach = this.touch ? 16 : 9;
      for (let i = m.scene.nodes.length - 1; i >= 0; i--) {
        const it = m.scene.nodes[i]; if (it.a < 0.4) continue;
        const x = (it.x - cam.cx) * S + mx, y = (it.y - cam.cy) * S + my, dx = x - px, dy = y - py;
        if (dx > reach + 8 || dx < -reach - 8 || dy > reach + 8 || dy < -reach - 8) continue;
        const r = radiusFor(it.d, k), d = Math.hypot(dx, dy), lim = Math.max(r + 3, reach);
        if (d <= lim && d - r < bd) { bd = d - r; best = { kind: 'n', it, x, y, r }; }
      }
      if (best) return best;
      const maxC = m.scene.maxCluster;
      for (const it of m.scene.clusters) {
        if (it.a < 0.4) continue;
        const x = (it.x - cam.cx) * S + mx, y = (it.y - cam.cy) * S + my, r = 9 + 30 * Math.sqrt(it.d.count / maxC);
        if (Math.hypot(x - px, y - py) <= r) return { kind: 'c', it, x, y, r };
      }
      return null;
    }

    /* ---------- input ---------- */
    bind() {
      const c = this.canvas, m = this.model, pts = new Map();
      let drag = null, pinch = null, samples = [];
      const pos = (e) => { const b = c.getBoundingClientRect(); return [e.clientX - b.left, e.clientY - b.top]; };
      c.addEventListener('pointerdown', (e) => {
        this.touch = e.pointerType === 'touch';
        c.setPointerCapture(e.pointerId); c.classList.remove('kb'); c.focus({ preventScroll: true });
        pts.set(e.pointerId, pos(e)); m.flight = null; m.goal = null; m.vel = null;
        if (pts.size === 2) { const [a, b] = [...pts.values()]; pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), k: m.cam.k, mid: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] }; drag = null; return; }
        drag = { x: e.clientX, y: e.clientY, moved: false, at: this.pick(...pos(e)), t: performance.now() }; samples = [];
        c.classList.add('drag');
      });
      c.addEventListener('pointermove', (e) => {
        if (pts.has(e.pointerId)) pts.set(e.pointerId, pos(e));
        if (pinch && pts.size === 2) {
          const [a, b] = [...pts.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]), mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
          const prev = pinch.mid; m.cam.panBy(mid[0] - prev[0], mid[1] - prev[1]); pinch.mid = mid;
          m.cam.zoomAt(pinch.k * d / pinch.d / m.cam.k, mid[0], mid[1]); m.moved(); return;
        }
        if (drag) {
          const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
          if (!drag.moved && Math.hypot(dx, dy) > (e.pointerType === 'mouse' ? 4 : 9)) { drag.moved = true; this.hideTip(); }
          if (drag.moved) {
            const ddx = e.clientX - (drag.lx ?? drag.x), ddy = e.clientY - (drag.ly ?? drag.y);
            drag.lx = e.clientX; drag.ly = e.clientY;
            m.pan(ddx, ddy);
            const now = performance.now(); samples.push([now, e.clientX, e.clientY]); while (samples.length && now - samples[0][0] > 90) samples.shift();
          }
          return;
        }
        if (e.pointerType === 'touch') return;
        this.hoverAt(...pos(e), e);
      });
      const end = (e) => {
        const had = pts.delete(e.pointerId);
        if (pinch) { if (pts.size < 2) pinch = null; drag = null; c.classList.remove('drag'); return; }
        if (!had || !drag) return;
        c.classList.remove('drag');
        if (!drag.moved) this.activate(drag.at || this.pick(...pos(e)));
        else if (samples.length > 1) {
          const a = samples[0], b = samples[samples.length - 1], dt = (b[0] - a[0]) / 1000;
          if (dt > 0) m.release((b[1] - a[1]) / dt, (b[2] - a[2]) / dt);
        }
        drag = null; m.moved(); this.invalidate();
      };
      c.addEventListener('pointerup', end);
      c.addEventListener('pointercancel', (e) => { pts.delete(e.pointerId); pinch = null; drag = null; c.classList.remove('drag'); });
      c.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse') { this.hover = null; this.hideTip(); this.invalidate(); } });
      c.addEventListener('wheel', (e) => {
        e.preventDefault();
        const [x, y] = pos(e), unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 300 : 1;
        m.zoomBy(Math.exp(-e.deltaY * unit * (e.ctrlKey ? 0.012 : 0.0018)), x, y); this.invalidate();
      }, { passive: false });
      c.addEventListener('dblclick', (e) => { const [x, y] = pos(e); m.zoomBy(2.2, x, y); this.invalidate(); });
      c.addEventListener('keydown', (e) => { c.classList.add('kb'); this.key(e, true); });
      c.addEventListener('blur', () => c.classList.remove('kb'));
      doc.addEventListener('keydown', (e) => { if (this.shown && !this.comparing && e.target === doc.body) this.key(e, false); });

      this.r.zoomIn.addEventListener('click', () => this.zoomCentre(1.6));
      this.r.zoomOut.addEventListener('click', () => this.zoomCentre(1 / 1.6));
      this.r.fit.addEventListener('click', () => m.fit());
      this.r.me.addEventListener('click', () => m.world.me && m.flyTo({ cx: m.world.me.x, cy: m.world.me.y, k: Math.max(m.cam.k, 3) }, 700));
      this.r.modes.addEventListener('click', (e) => { const b = e.target.closest('[data-mode]'); if (b) m.setMode(b.dataset.mode); });
      this.r.modes.addEventListener('keydown', (e) => {
        const i = MODES.findIndex((x) => x.id === m.mode), d = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
        if (!d) return; e.preventDefault();
        const next = MODES[(i + d + MODES.length) % MODES.length]; m.setMode(next.id); this.r.modes.querySelector(`[data-mode="${next.id}"]`).focus();
      });
      // Search.
      const input = this.r.q; let timer = 0;
      input.addEventListener('input', () => { this.closeResults(); clearTimeout(timer); timer = setTimeout(() => this.runSearch(), 160); if (!input.value.trim()) this.closeResults(); });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { if (!this.results.length) return; e.preventDefault(); this.moveActive(e.key === 'ArrowDown' ? 1 : -1); }
        else if (e.key === 'Enter') { e.preventDefault(); const n = this.results[Math.max(0, this.active)]; if (n) this.choose(n); else this.runSearch(true); }
        else if (e.key === 'Escape') { if (input.value || !this.r.results.hidden) { e.stopPropagation(); input.value = ''; this.closeResults(); } }
      });
      this.r.results.addEventListener('mousedown', (e) => e.preventDefault());
      this.r.results.addEventListener('click', (e) => { const b = e.target.closest('[data-i]'); if (b) this.choose(this.results[+b.dataset.i]); });
      input.addEventListener('blur', () => setTimeout(() => this.closeResults(), 120));
      // Filters.
      const f = this.r.filters;
      f.scope.addEventListener('click', (e) => { const b = e.target.closest('[data-scope]'); if (b) m.setFilters({ scope: b.dataset.scope }); });
      f.fit.addEventListener('change', () => m.setFilters({ minFit: f.fit.value }));
      f.status.addEventListener('change', () => m.setFilters({ status: f.status.value }));
      f.clear.addEventListener('click', () => { m.setFilters({ scope: 'all', minFit: '', status: '' }); });
      doc.addEventListener('click', (e) => { if (this.r.filtersBox.open && !this.r.filtersBox.contains(e.target)) this.r.filtersBox.open = false; });
      this.r.filtersBox.addEventListener('keydown', (e) => { if (e.key === 'Escape' && this.r.filtersBox.open) { e.stopPropagation(); this.r.filtersBox.open = false; this.r.filtersBox.querySelector('summary').focus(); } });
      // Card, states.
      this.r.card.addEventListener('click', (e) => this.cardClick(e));
      this.r.state.addEventListener('click', (e) => {
        if (e.target.closest('[data-act="leads"]')) this.host.openList();
        if (e.target.closest('[data-act="retry"]')) m.retry();
        if (e.target.closest('[data-act="clear"]')) m.setFilters({ scope: 'all', minFit: '', status: '' });
      });
      this.r.stale.addEventListener('click', () => m.retry());
      if (root.ResizeObserver) new ResizeObserver(() => this.measure(false)).observe(this.r.canvasBox);
      root.addEventListener('online', () => { if (this.shown && (m.phase === 'offline' || m.phase === 'error' || m.stale)) m.retry(); });
      new MutationObserver(() => { this.readPalette(); this.renderLegend(); this.invalidate(); }).observe(doc.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
      if (root.matchMedia) root.matchMedia('(prefers-reduced-motion: reduce)').addEventListener?.('change', (e) => { m.reduced = e.matches; });
    }
    zoomCentre(f) { const [mx, my] = this.model.cam.mid; this.model.zoomBy(f, mx, my); this.invalidate(); }
    key(e, onCanvas) {
      const m = this.model; if (e.metaKey || e.ctrlKey || e.altKey) return;
      const step = e.shiftKey ? 220 : 70, pan = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] }[e.key];
      if (pan) { e.preventDefault(); e.stopPropagation(); m.pan(pan[0], pan[1]); this.invalidate(); return; }
      if (!onCanvas) return;
      if (e.key === '+' || e.key === '=') { e.preventDefault(); e.stopPropagation(); this.zoomCentre(1.6); }
      else if (e.key === '-' || e.key === '_') { e.preventDefault(); e.stopPropagation(); this.zoomCentre(1 / 1.6); }
      else if (e.key === 'f') { e.preventDefault(); e.stopPropagation(); m.fit(); }
      else if (e.key === 'Escape') { if (m.deselect()) { e.preventDefault(); e.stopPropagation(); } }
      else if (/^[0-5]$/.test(e.key) && m.selected) { e.stopPropagation(); this.setStatus(m.selected, e.key === '0' ? null : this.host.statuses[+e.key - 1]); }
    }
    hoverAt(x, y, e) {
      const hit = this.pick(x, y), prev = this.hover;
      const same = hit && prev && hit.it === prev.it;
      this.hover = hit;
      this.canvas.style.cursor = hit ? 'pointer' : '';
      if (!hit) { this.hideTip(); if (prev) this.invalidate(); return; }
      if (!same) this.showTip(hit);
      const tip = this.r.tip, box = this.r.canvasBox;
      tip.style.transform = `translate(${Math.round(Math.min(x + 14, box.clientWidth - tip.offsetWidth - 8))}px, ${Math.round(Math.min(y + 14, box.clientHeight - tip.offsetHeight - 8))}px)`;
      if (!same) this.invalidate();
    }
    showTip(hit) {
      const tip = this.r.tip, d = hit.it.d;
      tip.replaceChildren();
      if (hit.kind === 'c') {
        tip.append(h('b', { text: plural(d.count, 'person', 'people') }), h('span', { text: (d.label ? `Mostly ${d.label}. ` : '') + 'Click to zoom in.' }));
      } else {
        tip.append(h('b', { text: d.name || '@' + d.handle }), h('span', { text: '@' + d.handle }),
          h('span', { text: `${FIT_LABEL[d.fit] || ''} fit${d.status ? ' · ' + SLABEL(d.status) : ''}` }));
      }
      tip.hidden = false;
    }
    hideTip() { this.r.tip.hidden = true; }
    activate(hit) {
      const m = this.model;
      if (!hit) { m.deselect(); return; }
      if (hit.kind === 'c') { const [wx, wy] = [hit.it.x, hit.it.y]; m.flyTo({ cx: wx, cy: wy, k: clamp(m.cam.k * 3.4, 1, Core.K_MAX) }, 700); m.load({ cam: { cx: wx, cy: wy, k: clamp(m.cam.k * 3.4, 1, Core.K_MAX) } }); return; }
      m.select(hit.it.d); this.ensureVisible(hit.it);
    }
    // Keep the chosen person clear of the edges, and above the sheet on a phone.
    ensureVisible(it) {
      const cam = this.model.cam, [x, y] = cam.toScreen(it.x, it.y), pad = 70, W = cam.w, H = cam.h - cam.inset.bottom;
      const dx = x < pad ? pad - x : x > W - pad ? W - pad - x : 0, dy = y < pad ? pad - y : y > H - pad ? H - pad - y : 0;
      if (dx || dy) { const s = cam.scale, t = { cx: cam.cx - dx / s, cy: cam.cy - dy / s, k: cam.k }; this.model.flyTo(t, 400); }
    }

    /* ---------- search ---------- */
    async runSearch(now) {
      const q = this.r.q.value.trim();
      this.results = []; this.active = -1;
      if (!q) { this.closeResults(); return; }
      this.showResults([], 'loading');
      try {
        const list = await this.model.searchPeople(q);
        if (list === null || q !== this.r.q.value.trim()) return;
        this.results = list; this.active = list.length ? 0 : -1;
        this.showResults(list, list.length ? 'ok' : 'none');
        if (now && list.length === 1) this.choose(list[0]);
      } catch (e) {
        if ((e && e.name === 'AbortError') || q !== this.r.q.value.trim() || !this.shown) return;
        this.showResults([], 'error');
      }
    }
    showResults(list, state) {
      const box = this.r.results; box.replaceChildren(); box.hidden = false;
      this.r.q.removeAttribute('aria-activedescendant');
      if (list.length && this.active >= 0) this.r.q.setAttribute('aria-activedescendant', 'mv-result-' + this.active);
      this.r.q.setAttribute('aria-expanded', 'true');
      if (state === 'loading') box.append(h('p', { class: 'mv-r-note', text: 'Searching…' }));
      else if (state === 'none') box.append(h('p', { class: 'mv-r-note', text: 'No one matches. Try a name or handle.' }));
      else if (state === 'error') box.append(h('p', { class: 'mv-r-note', text: 'Search isn’t available. Try again.' }));
      else list.forEach((n, i) => box.append(h('button', { type: 'button', role: 'option', id: 'mv-result-' + i, tabindex: '-1', 'data-i': i, 'aria-selected': String(i === this.active), class: 'mv-r' + (i === this.active ? ' on' : '') },
        h('b', { text: '@' + n.handle }), h('span', { text: n.name || '' }), h('i', { text: FIT_LABEL[n.fit] || '' }))));
    }
    moveActive(d) {
      this.active = (this.active + d + this.results.length) % this.results.length;
      this.showResults(this.results, 'ok');
    }
    closeResults() { this.model.searchReq.cancel(); this.r.q.removeAttribute('aria-activedescendant'); this.r.results.hidden = true; this.r.q.setAttribute('aria-expanded', 'false'); this.results = []; this.active = -1; }
    choose(n) {
      this.closeResults(); this.r.q.value = ''; this.r.q.blur();
      this.model.goTo(n); this.canvas.focus({ preventScroll: true }); this.announce(`Selected ${n.name || '@' + n.handle}.`);
    }

    /* ---------- static DOM ---------- */
    buildStatic() {
      const r = this.r;
      r.modes.replaceChildren(...MODES.map((x) => h('button', { type: 'button', role: 'radio', 'data-mode': x.id, 'aria-checked': 'false', tabindex: '-1' },
        h('span', { class: 'mv-long', text: x.label }), h('span', { class: 'mv-short', text: x.short }))));
      this.renderModes();
      this.renderFilters();
    }
    renderModes() {
      for (const b of this.r.modes.children) { const on = b.dataset.mode === this.model.mode; b.classList.toggle('on', on); b.setAttribute('aria-checked', String(on)); b.tabIndex = on ? 0 : -1; }
      this.r.modes.hidden = this.model.fallback;
      this.r.me.hidden = this.model.mode !== 'closeness' || !this.model.world.me;
    }
    renderFilters() {
      const m = this.model, f = this.r.filters;
      for (const b of f.scope.children) { const on = b.dataset.scope === m.scope; b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on)); }
      f.fit.value = m.minFit; f.status.value = m.status;
      const n = m.filtersActive; this.r.badge.hidden = !n; this.r.badge.textContent = String(n);
      f.clear.hidden = !n;
      this.r.filtersBtn.setAttribute('aria-label', n ? `Filters, ${plural(n, 'filter')} on` : 'Filters');
    }
    renderLegend() {
      const rows = this.model.fallback ? [{ g: 'size', t: 'Ranked overview. Position does not indicate a relationship.', s: 'Position is not a relationship' }, { g: 'size', t: 'Bigger dot means a stronger fit.', s: 'Size: fit' }] : LEGENDS[this.model.mode] || [], box = this.r.legend;
      const glyph = (g) => {
        const svg = (inner) => { const s = doc.createElementNS('http://www.w3.org/2000/svg', 'svg'); s.setAttribute('viewBox', '0 0 20 14'); s.setAttribute('width', '20'); s.setAttribute('height', '14'); s.setAttribute('aria-hidden', 'true'); s.innerHTML = inner; return s; };
        switch (g) {
          case 'size': return svg('<circle cx="4" cy="7" r="1.8" class="k-f"/><circle cx="10" cy="7" r="2.8" class="k-f"/><circle cx="16.4" cy="7" r="3.6" class="k-f"/>');
          case 'accent': return svg('<circle cx="10" cy="7" r="3.6" class="k-a"/>');
          case 'ring': return svg('<circle cx="10" cy="7" r="2.6" class="k-f"/><circle cx="10" cy="7" r="5.4" class="k-s"/>');
          case 'bubble': return svg('<circle cx="10" cy="7" r="6" class="k-b"/>');
          case 'centre': return svg('<circle cx="10" cy="7" r="6.4" class="k-r"/><circle cx="10" cy="7" r="3" class="k-r"/><circle cx="10" cy="7" r="1.4" class="k-f"/>');
          case 'axis': return svg('<path d="M2 7h16M14.5 4l3.5 3-3.5 3" class="k-s"/>');
          case 'island': return svg('<circle cx="6" cy="6" r="4.2" class="k-r"/><circle cx="14.5" cy="8.5" r="3" class="k-r"/>');
          case 'lanes': return svg('<path d="M4 2v10M10 2v10M16 2v10" class="k-r"/>');
          default: return svg('');
        }
      };
      box.replaceChildren(...rows.map((row) => h('li', {}, glyph(row.g), h('span', { class: 'l-long', text: row.t }), h('span', { class: 'l-short', text: row.s || row.t }))));
    }
    renderCount() {
      const m = this.model, el = this.r.count;
      if (m.phase !== 'ready' && m.phase !== 'empty') { el.textContent = ''; return; }
      if (m.fallback) { el.textContent = `Overview · ${int(m.shown)} shown of ${int(m.total)} people`; el.title = `A ranked sample of up to 400 people. Filters apply to this sample. Prepare the spatial layout locally for the full map.`; return; }
      el.textContent = `${int(m.shown)} ${m.shown === 1 ? 'person' : 'people'} shown of ${int(m.total)}`;
      el.title = m.hidden ? `${int(m.hidden)} more in this view are grouped into bubbles. Zoom in to see them.` : 'Everyone in this view is shown.';
      clearTimeout(this.annT); this.annT = setTimeout(() => this.announce(el.textContent + '.'), 900);
    }
    announce(t) { this.r.live.textContent = ''; setTimeout(() => { this.r.live.textContent = t; }, 30); }
    renderBusy() {
      const el = this.r.progress, m = this.model;
      clearTimeout(this.busyTimer);
      if (m.pending > 0 && m.phase === 'ready') this.busyTimer = setTimeout(() => { el.hidden = false; }, 380); else el.hidden = true;
      this.r.canvasBox.setAttribute('aria-busy', String(m.pending > 0));
    }
    renderStale() {
      const m = this.model, el = this.r.stale;
      el.hidden = !m.stale;
      if (m.stale) el.textContent = m.stale === 'offline' ? 'You’re offline. Try again' : 'Couldn’t refresh the map. Try again';
    }
    renderState() {
      const m = this.model, box = this.r.state, p = m.phase;
      if (p === 'ready' || p === 'idle') { box.hidden = true; box.dataset.kind = ''; return; }
      const copy = {
        unprepared: ['Map layout is not prepared yet', 'Your leads are available in the list. Prepare the map locally to explore connections here.'],
        building: ['Preparing the map', 'Your leads are available while the layout is being prepared.'],
        loading: ['Loading the map', 'Placing people by how close they are to you.'],
        offline: ['You’re offline', 'The map returns when the connection does.'],
        error: ['Couldn’t load the map', 'Check your connection, then try again.'],
        empty: [m.fallback ? 'No one in this sample matches' : 'No one matches these filters', m.filtersActive ? 'Try clearing a filter.' : 'Nobody is on the map yet. Collect a follower list to fill it.']
      }[p];
      if (!copy) return;
      box.dataset.kind = p; box.hidden = false;
      const rings = p === 'loading' ? h('div', { class: 'mv-skel', 'aria-hidden': 'true' }, h('i'), h('i'), h('i'), h('i')) : null;
      box.replaceChildren(...[rings, h('b', { text: copy[0] }), h('p', { text: copy[1] }),
        p === 'unprepared' || p === 'building' ? h('button', { type: 'button', class: 'btn', 'data-act': 'leads', text: 'Open Leads' }) : null,
        p === 'offline' || p === 'error' ? h('button', { type: 'button', class: 'btn', 'data-act': 'retry', text: 'Try again' }) : null,
        p === 'empty' && m.filtersActive ? h('button', { type: 'button', class: 'btn', 'data-act': 'clear', text: 'Clear filters' }) : null].filter(Boolean));
    }

    /* ---------- side card ---------- */
    renderCard() {
      const m = this.model, n = m.selected, card = this.r.card;
      if (!n) { this.cardId = null; this.noteRead = (this.noteRead || 0) + 1; card.hidden = true; card.replaceChildren(); this.r.pane.classList.remove('has-card'); this.note = null; this.measure(false); this.r.canvasBox.style.removeProperty('--sheet'); return; }
      const same = this.cardId === n.id;
      this.cardId = n.id; this.note = null; this.noteRead = (this.noteRead || 0) + 1; this.r.pane.classList.add('has-card');
      const seed = (m.seedList || [])[n.cluster]; const seedLabel = seed ? '@' + (seed.handle || seed) : '';
      const label = n.lead ? 'Open lead' : 'Open profile';
      const why = n.reason || whyLine(n, seedLabel);
      card.hidden = false;
      const facts = [['Fit', FIT_LABEL[n.fit] || 'Unknown'], ['Status', n.status ? SLABEL(n.status) : 'No status']];
      if (typeof n.closeness === 'number') facts.push(['Closeness', Core.closenessWords(n.closeness).replace(' to you', '').replace(/^./, (c) => c.toUpperCase())]);
      if (n.follows_me) facts.push(['Follows you', 'Yes']);
      card.replaceChildren(
        h('div', { class: 'mv-c-head' },
          h('span', { class: 'mv-av', 'aria-hidden': 'true', text: initials(n) }),
          h('div', { class: 'mv-who' }, h('b', { text: n.name || '@' + n.handle }), h('span', { text: '@' + n.handle })),
          h('button', { type: 'button', class: 'mv-x', 'data-act': 'close', 'aria-label': 'Close details', title: 'Close (esc)', text: '×' })),
        h('p', { class: 'mv-why', text: why }),
        h('dl', { class: 'mv-facts' }, facts.map(([k, v]) => h('div', {}, h('dt', { text: k }), h('dd', { text: v })))),
        h('section', { class: 'mv-seeds', 'aria-live': 'polite' }, h('h4', { text: 'Seen in seed audiences' }), h('ul', { class: 'mv-chips', id: 'mv-chips' })),
        h('div', { class: 'mv-actions' },
          h('button', { type: 'button', class: 'btn solid', 'data-act': 'open', text: label }),
          h('button', { type: 'button', class: 'btn', 'data-act': 'note', 'aria-expanded': 'false', text: 'Add note' }),
          h('button', { type: 'button', class: 'btn', 'data-act': 'status', 'aria-expanded': 'false', text: 'Set status' })),
        h('div', { class: 'mv-status', hidden: true, role: 'group', 'aria-label': 'Set status' },
          this.host.statuses.map((s, i) => h('button', { type: 'button', class: 'mv-s' + (n.status === s ? ' on' : ''), 'data-s': s, 'aria-pressed': String(n.status === s) }, h('span', { text: SLABEL(s) }), h('kbd', { text: String(i + 1) }))),
          n.status ? h('button', { type: 'button', class: 'mv-s', 'data-s': '', text: 'Clear status' }) : null),
        h('div', { class: 'mv-note', hidden: true }));
      if (!same) card.scrollTop = 0;
      this.renderSeeds();
      // A sheet on a phone changes what "centre" means; recompute the inset.
      requestAnimationFrame(() => this.measure(false));
      this.announce(`Selected ${n.name || '@' + n.handle}.`);
    }
    updateCardFacts() {
      const n = this.model.selected;
      const status = this.r.card.querySelector('.mv-facts div:nth-child(2) dd');
      if (status) status.textContent = n.status ? SLABEL(n.status) : 'No status';
      for (const b of this.r.card.querySelectorAll('[data-s]')) { const on = (n.status || '') === b.dataset.s; b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on)); }
    }
    renderSeeds() {
      const ul = this.r.card.querySelector('#mv-chips'), e = this.model.edges, n = this.model.selected;
      if (!ul || !n) return;
      ul.replaceChildren();
      if (!e || e.id !== n.id || e.state === 'loading') { ul.append(h('li', { class: 'mv-note-line', text: 'Checking seeds…' })); return; }
      if (e.state === 'error') { ul.append(h('li', { class: 'mv-note-line', text: 'Couldn’t load seeds.' })); return; }
      if (e.overview) { ul.append(h('li', { class: 'mv-note-line', text: 'Use Compare profiles to see recorded follow paths.' })); return; }
      if (!e.seeds.length) { ul.append(h('li', { class: 'mv-note-line', text: 'Not linked to a seed yet.' })); return; }
      this.r.card.querySelector('.mv-seeds h4').textContent = `Seen in ${plural(e.seeds.length, 'seed audience')}`;
      for (const s of e.seeds.slice(0, 8)) ul.append(h('li', {}, h('button', { type: 'button', class: 'mv-chip', 'data-goto': s.id, title: 'Show on the map', text: '@' + s.handle })));
    }
    cardClick(e) {
      const m = this.model, n = m.selected; if (!n) return;
      const act = e.target.closest('[data-act]')?.dataset.act, card = this.r.card;
      const go = e.target.closest('[data-goto]');
      if (go) { const it = m.edges?.seeds.find((s) => String(s.id) === go.dataset.goto); if (it) m.goTo(it, { k: Math.max(m.cam.k, 4) }); return; }
      if (act === 'close') { m.deselect(); this.canvas.focus({ preventScroll: true }); }
      else if (act === 'open') this.host.openLead(n.id, n.handle);
      else if (act === 'status') { const box = card.querySelector('.mv-status'), on = box.hidden; box.hidden = !on; e.target.closest('[data-act]').setAttribute('aria-expanded', String(on)); if (on) box.querySelector('button')?.focus(); }
      else if (act === 'note') this.toggleNote(e.target.closest('[data-act]'));
      const s = e.target.closest('[data-s]'); if (s) this.setStatus(n, s.dataset.s || null);
      if (e.target.closest('[data-note="save"]')) this.saveNote();
      if (e.target.closest('[data-note="cancel"]')) { card.querySelector('.mv-note').hidden = true; card.querySelector('[data-act="note"]').setAttribute('aria-expanded', 'false'); }
    }
    async setStatus(n, status) {
      this.statusWrites ||= new Map();
      if (this.statusWrites.has(n.id)) return;
      this.statusWrites.set(n.id, true);
      const before = n.status || null;
      this.model.patchStatus(n.id, status);
      try { await this.host.setStatus(n.id, status); this.host.toast(status ? `Marked ${SLABEL(status).toLowerCase()}` : 'Status cleared'); }
      catch (_) { this.model.patchStatus(n.id, before); this.host.toast('Couldn’t save. Try again.'); }
      finally { this.statusWrites.delete(n.id); }
    }
    async toggleNote(btn) {
      const box = this.r.card.querySelector('.mv-note'), n = this.model.selected;
      if (!box.hidden) { box.hidden = true; btn.setAttribute('aria-expanded', 'false'); return; }
      box.hidden = false; btn.setAttribute('aria-expanded', 'true');
      this.note = null;
      const ta = h('textarea', { class: 'input', id: 'mv-note-in', rows: '4', maxlength: '5000', disabled: true });
      box.replaceChildren(h('label', { for: 'mv-note-in', text: 'Note' }), ta, h('p', { class: 'mv-note-msg', text: 'Loading your note…', role: 'status' }),
        h('div', { class: 'mv-note-row' }, h('button', { type: 'button', class: 'btn solid', 'data-note': 'save', disabled: true, text: 'Save note' }), h('button', { type: 'button', class: 'btn', 'data-note': 'cancel', text: 'Cancel' })));
      try {
        const read = this.noteRead = (this.noteRead || 0) + 1;
        const p = await this.host.person(n.id);
        if (this.model.selected?.id !== n.id || read !== this.noteRead || !box.isConnected) return;
        this.note = { id: n.id, rev: p.mark_rev, base: p.note || '' };
        ta.value = this.note.base; ta.disabled = false; ta.focus();
        box.querySelector('.mv-note-msg').textContent = ''; box.querySelector('[data-note="save"]').disabled = false;
      } catch (_) { box.querySelector('.mv-note-msg').textContent = 'Couldn’t load the note. Try again.'; }
    }
    async saveNote() {
      const box = this.r.card.querySelector('.mv-note'), ta = box.querySelector('textarea'), msg = box.querySelector('.mv-note-msg'), n = this.model.selected, keep = this.note;
      if (!n || !keep || keep.id !== n.id || keep.saving) return;
      keep.saving = true;
      msg.textContent = 'Saving…';
      try {
        await this.host.saveNote(n.id, ta.value.trim(), keep.rev);
        if (this.note === keep && box.isConnected) { box.hidden = true; this.r.card.querySelector('[data-act="note"]').setAttribute('aria-expanded', 'false'); } this.host.toast('Note saved');
      } catch (e) { msg.textContent = e && e.status === 409 ? 'This note changed elsewhere. Close and reopen it to see the latest.' : 'Couldn’t save. Try again.'; } finally { keep.saving = false; }
    }
  }

  /* ---------- mounting ---------- */
  function refs() {
    const $ = (id) => doc.getElementById(id);
    const pane = $('pane-map'); if (!pane || !$('map-canvas')) return null;
    return {
      pane, canvas: $('map-canvas'), canvasBox: $('map-canvas-box'), q: $('map-q'), results: $('map-search-results'), modes: $('map-modes'), filtersBox: $('map-filters'), filtersBtn: $('map-filters').querySelector('summary'),
      badge: $('map-filter-badge'), filters: { scope: $('map-scope'), fit: $('map-fit-filter'), status: $('map-status-filter'), clear: $('map-filters-clear') },
      state: $('map-state'), stale: $('map-stale'), progress: $('map-progress'), tip: $('hover'), legend: $('map-legend'), hud: $('map-hud'), count: $('map-shown'), live: $('map-live'),
      card: $('map-card'), zoomIn: $('zoom-in'), zoomOut: $('zoom-out'), fit: $('map-fit'), me: $('map-me')
    };
  }
  function mount(host) {
    const r = refs(); if (!r) return null;
    const view = new MapView(r, host);
    root.__map = view;
    return view;
  }

  const api = { MapView, mount, initials };
  root.MapViewModule = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window === 'undefined' ? globalThis : window);
