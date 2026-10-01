/* Connections map view: paints the model on a canvas, owns the toolbar, legend,
 * side card, states and keyboard. Data flow lives in map-model.js; encodings and
 * copy in map-core.js. One canvas, one frame loop that sleeps when nothing moves. */
(function (root) {
  'use strict';
  const Core = root.MapCore, { MapModel, VIEW_MODES } = root.MapModel;
  const { OWNER_RADIUS, LEGENDS, FIT_LABEL, STATUS_LABEL, int, plural, compact, clamp, Labeler, whyLine, radiusFor, displayPlan, PortraitCache, SIZE_OPTIONS, SIZE_HELP } = Core;
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
  const profileMetric = (person, shared) => {
    const hasFollowers = person.followers != null && Number.isFinite(+person.followers) && +person.followers >= 0;
    const hasSources = person.source_count != null && Number.isFinite(+person.source_count) && +person.source_count >= 0;
    if (shared && hasSources || !hasFollowers && hasSources) return plural(+person.source_count, 'source audience', 'source audiences');
    return hasFollowers ? `${int(person.followers)} followers` : 'Followers unknown';
  };
  const savedDensity = () => { try { const n=+root.localStorage?.getItem('fortunate.map.count'); return [500,1000,3000].includes(n)?n:500; } catch (_) { return 500; } };
  const savedViewMode = () => { try { return root.localStorage?.getItem('fortunate.map.view'); } catch (_) { return null; } };
  const reducedMotion = () => !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const SLABEL = (s) => STATUS_LABEL[s] || (s ? s.charAt(0).toUpperCase() + s.slice(1) : '');

  const RELATIONSHIP_TAGS = {
    client: 'Client', worked_with: 'Worked with', colleague: 'Colleague',
    friend: 'Friend', acquaintance: 'Acquaintance'
  };
  function cardTags(person) {
    const relationships = Array.isArray(person.relationships) ? person.relationships
      : person.status === 'client' ? ['client', 'worked_with'] : [];
    const tags = new Map();
    for (const key of relationships) {
      if (RELATIONSHIP_TAGS[key]) tags.set(RELATIONSHIP_TAGS[key].toLowerCase(), { label: RELATIONSHIP_TAGS[key], relationship: key });
    }
    for (const label of person.manual_tags || []) {
      const key = label.toLowerCase();
      if (!tags.has(key)) tags.set(key, { label, relationship: Object.keys(RELATIONSHIP_TAGS).find(k => RELATIONSHIP_TAGS[k].toLowerCase() === key) });
    }
    return [...tags.values()];
  }
  // Only recorded follow lines qualify. Shared audiences never become follow paths.
  function directedConnections(lines, selectedId) {
    const groups = new Map(), selected = String(selectedId);
    for (const line of lines || []) {
      if (!['follow', 'follows', 'mutual'].includes(line.kind)) continue;
      const from = String(line.a.id), to = String(line.b.id);
      if (from === to || from !== selected && to !== selected) continue;
      const outgoing = from === selected, other = outgoing ? line.b : line.a;
      const key = String(other.id);
      const group = groups.get(key) || { selected: outgoing ? line.a : line.b, other, outgoing: false, incoming: false };
      if (outgoing || line.kind === 'mutual') group.outgoing = true;
      if (!outgoing || line.kind === 'mutual') group.incoming = true;
      groups.set(key, group);
    }
    return [...groups.values()];
  }
  function cardFit(person) {
    const verdict = person.verdict;
    if (!verdict) return { text: 'Not reviewed', detail: 'Checks for physical-product brands.' };
    if (verdict.model === 'rules' && verdict.role === 'unclear') {
      return { text: 'Unclear', detail: 'Not enough evidence of a physical-product brand.' };
    }
    const score = person.business_fit ?? verdict.content_fit;
    if (score == null) return { text: 'Not reviewed', detail: 'Checks for physical-product brands.' };
    return { text: Math.round(score) + '/100', detail: (verdict.model === 'rules' ? 'Rules estimate' : 'Profile estimate') + ' for physical-product brands.' };
  }

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
      const Model = MapModel;
      this.model = new Model({
        container: refs.canvasBox,
        fetchJson: (u, o) => host.fetchJson(u, o), viewMode: savedViewMode(), density: savedDensity(), reduced: reducedMotion(), online: () => root.navigator.onLine !== false,
        budget: Number(new URLSearchParams(root.location.search).get('mapbudget')) || 0
      });
      this.model.cam.set(.5,.5,1.55);
      this.shown = false; this.raf = 0; this.last = 0; this.dpr = 1; this.hover = null; this.textW = new Map(); this.labelA = new Map();
      this.hud = null; this.busyTimer = 0; this.edgeAt = 0; this.stats = { frames: 0, paintMs: 0, maxMs: 0 };
      this.portraits = new PortraitCache({ max:3072, createImage: () => new root.Image(), normalize: root.createImageBitmap ? image => { const side = Math.min(image.naturalWidth, image.naturalHeight), pixels = image.src.endsWith(this.model.world.me?.pic || '#owner') ? 128 : 64; return root.createImageBitmap(image, (image.naturalWidth-side)/2, (image.naturalHeight-side)/2, side, side, { resizeWidth: pixels, resizeHeight: pixels }); } : null, changed: () => this.invalidate() });
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
      if (what === 'density') { try { root.localStorage?.setItem('fortunate.map.count', String(this.model.density)); } catch (_) {} }
      if (what === 'scene' || what === 'mode') this.finishNodeDrag(true);
      if (what === 'size') { this.renderLegend(); this.invalidate(); }
      if (what === 'scene' || what === 'camera' || what === 'edges') this.invalidate();
      if (what === 'edges') { this.edgeAt = performance.now(); this.renderSeeds(); }
      if (what === 'phase' || what === 'scene') { this.renderModes(); this.renderLegend(); this.renderState(); this.renderCount(); }
      if (what === 'busy' || what === 'scene' || what === 'phase') this.renderBusy();
      if (what === 'stale') this.renderStale();
      if (what === 'scene') this.renderStale();
      if (what === 'selection') { if (!this.model.selected || this.cardId !== this.model.selected.id) this.renderCard(); else this.updateCardFacts(); this.invalidate(); }
      if (what === 'mode') { try { root.localStorage?.setItem('fortunate.map.view', this.model.viewMode); } catch (_) {} this.renderModes(); this.renderLegend(); this.invalidate(); }
      if (what === 'filters') this.renderFilters();
      if (what === 'scene' && this.model.universe) this.canvas.style.zIndex = '1';
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
    hide() { this.finishNodeDrag(true); this.shown = false; clearInterval(this.poller); this.model.pause(true); this.closeResults(); this.hideTip(); }
    resize() { this.measure(false); }
    measure(first) {
      const box = this.r.canvasBox, w = box.clientWidth, h = box.clientHeight;
      if (!w || !h) return;
      const dpr = Math.min(root.devicePixelRatio || 1, 2), cam = this.model.cam;
      const narrow = root.matchMedia('(max-width: 760px)').matches, sheet = this.r.card.hidden || !narrow ? 0 : this.r.card.offsetHeight;
      if (w === cam.w && h === cam.h && dpr === this.dpr && cam.inset.bottom === sheet && !first) return;
      this.dpr = dpr; this.canvas.width = Math.round(w * dpr); this.canvas.height = Math.round(h * dpr);
      this.model.setSize(w, h);
      this.sized = true;
      const priorSheet = cam.inset.bottom;
      cam.inset = { top: 0, right: 0, bottom: sheet, left: 0 };
      if (priorSheet !== sheet && this.model.selected && !this.model.universe) { const it = this.model.scene.get(this.model.selected.id); if (it) this.ensureVisible(it); }
      this.measureHud();
      if (this.model.needsLoad() && this.shown && !this.comparing) this.model.schedule();
      this.invalidate();
    }
    measureHud() {
      const legend = this.r.hud.getBoundingClientRect(), stage = this.r.canvasBox.getBoundingClientRect();
      this.hud = { x0: legend.left - stage.left - 8, y0: legend.top - stage.top - 8, x1: legend.right - stage.left + 8, y1: legend.bottom - stage.top + 8 };
    }
    readPalette() {
      const cs = root.getComputedStyle(doc.documentElement), v = (n, f) => cs.getPropertyValue(n).trim() || f, ctx = this.ctx;
      const fg = rgbOf(ctx, v('--fg', '#ededed')), bg = rgbOf(ctx, v('--bg', '#0d0d0d'));
      const tone = (t) => mix(fg, bg, t);
      this.palette = {
        fg: v('--fg', '#ededed'), fg2: v('--fg2', '#b5b5b5'), fg3: v('--fg3', '#969696'), fg4: v('--fg4', '#8d8d8d'), bg: v('--bg', '#0d0d0d'), bg1: v('--bg1', '#141414'), bg2: v('--bg2', '#1c1c1c'),
        line: v('--line', '#202020'), line2: v('--line2', '#303030'), line3: v('--line3', '#454545'), accent: v('--accent', '#ededed'), sans: v('--sans', 'system-ui, sans-serif'),
        tones: { t0: tone(0.12), t1: tone(0.26), t2: tone(0.38), t3: tone(0.5), t4: tone(0.6), t5: tone(0.68) }
      };
      this.palette.tones.a = this.palette.accent;
      this.dark = doc.documentElement.dataset.theme !== 'light'; this.overviewRaster = null;
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
      if (!this.stats.drawTimes) this.stats.drawTimes=[]; this.stats.drawTimes.push(ms); if(this.stats.drawTimes.length>240)this.stats.drawTimes.shift();
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
      if (m.universe) return this.paintUniverse(now, dt);
      if (!m.scene.nodes.length && !m.scene.clusters.length && !m.selected) { this.labelA.clear(); return false; }
      const sx = (x) => (x - cam.cx) * S + mx, sy = (y) => (y - cam.cy) * S + my;
      const lab = new Labeler(W, H);
      if (this.hud) lab.block([this.hud.x0, this.hud.y0, this.hud.x1, this.hud.y1]);
      const want = [];   // labels to place, in priority order
      const font = (w, px) => `${w} ${px}px ${P.sans}`;

      this.paintGuides(ctx, P, m, cam, S, sx, sy, want);
      const sel = m.selected, edges = m.connectionsVisible && m.edges && sel && m.edges.id === sel.id ? m.edges : null;
      const related = new Set(sel ? [String(sel.id)] : []);
      if(edges?.state === 'ready') for(const line of directedConnections(edges.lines,sel.id)) {
        related.add(String(line.selected.id));related.add(String(line.other.id));
      }
      const focusTarget=sel?1:0;
      this.focusMix=(this.focusMix||0)+(focusTarget-(this.focusMix||0))*(m.reduced?1:1-Math.exp(-dt/.07));
      if(Math.abs(this.focusMix-focusTarget)>.005)this.invalidate();
      else this.focusMix=focusTarget;
      const dim=1-.74*this.focusMix;

      const hovered = this.hover;
      const plan = this.displayMarks = displayPlan(m.scene, cam, m.world.guides || [], m.selected, this.hud, m.world.layout === 'network_disk' || m.mode === 'closeness' ? m.world.me?.id : null, m.mode, m.size);
      const activeId=this.nodeDrag?.d.id ?? sel?.id;
      const active=plan.nodes.find(mark=>String(mark.it.d.id)===String(activeId));
      if(active)active.r*=1+(this.nodeDrag?.06:.07*this.focusMix);
      for(const mark of plan.nodes) {
        let dx=0,dy=0;
        if(active && mark!==active && String(mark.it.d.id)!==String(m.world.me?.id)) {
          const x=mark.x-active.x,y=mark.y-active.y,d=Math.hypot(x,y),gap=active.r+mark.r+4;
          if(d>0 && d<gap){const push=Math.min(14,(gap-d)*.8);dx=x/d*push;dy=y/d*push;}
        }
        const it=mark.it,k=m.reduced?1:1-Math.exp(-dt/.06);
        it.displayDX=(it.displayDX||0)+(dx-(it.displayDX||0))*k;
        it.displayDY=(it.displayDY||0)+(dy-(it.displayDY||0))*k;
        mark.x+=it.displayDX;mark.y+=it.displayDY;
        if(Math.abs(it.displayDX-dx)+Math.abs(it.displayDY-dy)>.05)this.invalidate();
      }
      if (plan.large) this.paintLargeOverview(ctx, P, plan);
      const bubbles = plan.groups;
      for (const mark of plan.nodes) lab.block([mark.x-mark.r-2, mark.y-mark.r-2, mark.x+mark.r+2, mark.y+mark.r+2]);
      const disk = m.world.layout === 'network_disk';
      const network = disk || m.mode === 'closeness' && (m.world.guides || []).some(g => g.label === 'Direct connections');
      const spheres = network && !disk && cam.k < 2.5;
      const largestGroup = Math.max(1, ...bubbles.map(b => b.it.d.count));
      for (const b of bubbles) {
        const { x, y, it } = b, on = hovered && hovered.it.d.id === it.d.id;
        const guide = (m.world.guides || []).find(g => g.label === it.d.label);
        ctx.globalAlpha = edges ? .65 : 1;
        ctx.fillStyle = on ? P.bg2 : P.bg1; ctx.strokeStyle = on ? P.fg3 : P.line3; ctx.lineWidth = 1;
        ctx.beginPath();
        if (spheres) {
          b.r = Math.min((guide?.r || .12) * S, 28 + 52 * Math.sqrt(it.d.count / largestGroup));
          ctx.arc(x, y, Math.max(20, b.r), 0, TAU);
        } else ctx.roundRect(x - 19, y - 13, 38, 26, 7);
        ctx.fill(); ctx.stroke();
        ctx.globalAlpha = 1; ctx.fillStyle = P.fg; ctx.font = font(600, spheres ? 15 : 11); ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(compact(it.d.count), x, y + .5);
        lab.block([x - 22, y - 16, x + 22, y + 16]);
      }
      ctx.globalAlpha = 1; ctx.textAlign = 'left';
      // Lines for the selected person, under the dots.
      if (edges && edges.state === 'ready') this.paintEdges(ctx, P, edges, sx, sy, now, sel);

      // A bounded set of saved profile portraits; missing photos use their initials.
      const k = cam.k, mode = m.mode;
      const me = m.world && m.world.me && (disk || mode === 'closeness') ? m.world.me : null;
      let ownerCaption = null;
      this.queuePortraits(plan.nodes, me);
      let photoBudget = 3001;
      for (const mark of plan.nodes) {
        const {it, x, y, r} = mark, d = it.d;
        if (me && d.id === me.id) continue;
        ctx.globalAlpha = it.a * (related.has(String(d.id)) ? 1 : dim);
        this.paintPortrait(ctx, P, d, x, y, r, photoBudget-- > 0);
      }
      ctx.globalAlpha = 1;
      if (me) {
        const x = sx(me.x), y = sy(me.y);
        ctx.strokeStyle = P.line3; ctx.lineWidth = 1;

        const ownerRadius=m.scene.large?OWNER_RADIUS:Math.min(28,OWNER_RADIUS*k);
        this.paintPortrait(ctx, P, me, x, y, ownerRadius, true, true);
        const text = 'You · ' + (me.name || '@' + me.handle), width = this.text(font(600, 12), text);
        ownerCaption = { x, y: y + ownerRadius + 16, text };
        lab.block([x-width/2-4, y+ownerRadius+7, x+width/2+4, y+ownerRadius+25]);
      }

      // Label the selected person's recorded sources.
      if (edges && edges.state === 'ready') for (const n of edges.seeds.filter(n=>m.scene.get(n.id))) {
        const placed=m.scene.get(n.id) || n;
        const x = sx(placed.x), y = sy(placed.y);
        want.unshift({ key: 'e:' + n.id, text: '@' + n.handle, x, y, r: 5, w: 500 });
      }
      if (sel) {
        const it = m.scene.get(sel.id), x = sx(it ? it.x : sel.x), y = sy(it ? it.y : sel.y), r = plan.nodes.find(mark=>String(mark.it.d.id)===String(sel.id))?.r || (it?.d.portraitRadius ? Math.max(2,it.d.portraitRadius*S) : radiusFor(sel, k, m.size));
        this.paintPortrait(ctx, P, sel, x, y, r, true);
        if (!me || sel.id !== me.id) want.unshift({ key: 'sel', text: sel.name || (sel.compact ? 'Saved profile' : '@' + sel.handle), x, y, r: r + 6, w: 600, strong: true });
      }


      const moving = this.nodeDrag || m.returningNode;
      if (moving && m.scene.large) this.paintPortrait(ctx,P,moving.d,sx(moving.x),sy(moving.y),moving.d.portraitRadius*S,true);

      // Labels. Bubbles first (which community is this), then the best-ranked people.
      let nb = 0; const labelledGroups = new Set();
      for (const b of bubbles) {
        if (!b.it.d.label || labelledGroups.has(b.it.d.label)) continue;
        labelledGroups.add(b.it.d.label);
        want.push({ key: 'c:' + b.it.d.id, text: b.it.d.label.replace(/^(Around |Audience of )/, ''), x: b.x, y: spheres ? b.y - b.r - 12 : b.y, r: spheres ? 0 : b.r, w: 600, prefer: spheres ? 'top' : 'bottom', prio: 100 });
        if (++nb >= 14) break;
      }
      const maxPeople = clamp(Math.round(W * H / 80000 * Math.min(k, 2)), 3, 12);
      let np = 0; const peoplePerGroup = new Map();
      for (let i = 0; i < plan.nodes.length && np < maxPeople; i++) {
        const it = plan.nodes[i].it, d = it.d;
        if (d.compact || it.a < 0.9 || (me && d.id === me.id) || (sel && d.id === sel.id)) continue;
        if (network && cam.k < 2.5 && (peoplePerGroup.get(d.cluster) || 0) >= 2) continue;
        const x = sx(it.x), y = sy(it.y);
        if (x < 8 || y < 8 || x > W - 8 || y > H - 8) continue;
        want.push({ key: 'n:' + d.id, text: d.name || '@' + d.handle, x, y, r: plan.nodes[i].r + 2, w: 500, dimmed: !!edges });
        np++; peoplePerGroup.set(d.cluster, (peoplePerGroup.get(d.cluster) || 0) + 1);
      }
      this.paintLabels(ctx, P, lab, want, dt);
      if (ownerCaption) {
        ctx.font = font(600, 12); ctx.textAlign = 'center'; ctx.textBaseline = 'top';
        ctx.strokeStyle = P.bg; ctx.lineWidth = 4; ctx.strokeText(ownerCaption.text, ownerCaption.x, ownerCaption.y);
        ctx.fillStyle = P.fg; ctx.fillText(ownerCaption.text, ownerCaption.x, ownerCaption.y); ctx.textAlign = 'left';
      }
      this.renderCount(false);
      return this.labelsBusy;
    }
    paintUniverse(now, dt) {
      const m=this.model, cam=m.cam, ctx=this.ctx, P=this.palette;
      m.universe.render(cam);
      const sx=x=>cam.toScreen(x,0)[0], sy=y=>cam.toScreen(0,y)[1];
      const selected=m.selected, edges=m.connectionsVisible && m.edges?.id===selected?.id?m.edges:null;
      if(edges?.state==='ready') this.paintEdges(ctx,P,edges,sx,sy,now,selected);
      const lab=new Labeler(cam.w,cam.h), want=[];
      for(const person of [selected,this.hover?.it?.d]) {
        if(!person)continue;
        const [x,y]=cam.toScreen(person.x,person.y),r=Math.max(3,(person.portraitRadius||0)*cam.scale);
        want.push({key:'u:'+person.id,text:person.name||'@'+person.handle,x,y,r:r+6,w:500,strong:true,prio:10});
      }
      this.paintLabels(ctx,P,lab,want,dt);
      return this.labelsBusy;
    }
    paintLargeOverview(ctx, P, plan) {
      const m=this.model,cam=m.cam,ownerId=String(m.world.me?.id);
      ctx.fillStyle=P.fg3;
      if (plan.vector) {
        ctx.beginPath();
        for(const mark of plan.marks) { if(String(mark.it.d.id)===ownerId)continue; ctx.moveTo(mark.x+mark.r,mark.y);ctx.arc(mark.x,mark.y,mark.r,0,TAU); }
        ctx.fill(); return;
      }
      if (!this.overviewRaster || this.overviewRaster.version!==m.scene.version) {
        const start=performance.now(),side=2048,canvas=doc.createElement('canvas');canvas.width=side;canvas.height=side;
        const draw=canvas.getContext('2d');draw.fillStyle=P.fg3;
        for(let offset=0;offset<m.scene.nodes.length;offset+=2048) {
          draw.beginPath();
          for(const it of m.scene.nodes.slice(offset,offset+2048)) {
            if(String(it.d.id)===ownerId)continue;
            const x=it.x*side,y=it.y*side,r=it.d.portraitRadius*side;
            draw.moveTo(x+r,y);draw.arc(x,y,r,0,TAU);
          }
          draw.fill();
        }
        this.overviewRaster={canvas,version:m.scene.version};this.stats.rasterMs=performance.now()-start;
      }
      const [x,y]=cam.toScreen(0,0);ctx.drawImage(this.overviewRaster.canvas,x,y,cam.scale,cam.scale);
    }
    queuePortraits(nodes, owner) {
      if (owner) this.portraits.get(owner.pic);
      const x = owner?.x ?? .5, y = owner?.y ?? .5;
      const distance = mark => (mark.it.x - x) ** 2 + (mark.it.y - y) ** 2;
      for (const mark of [...nodes].sort((a, b) => distance(a) - distance(b)).slice(0, 3001)) {
        this.portraits.get(mark.it.d.pic);
      }
    }
    paintPortrait(ctx, P, person, x, y, radius, requestPhoto, owner = false) {
      const image = requestPhoto ? this.portraits.get(person.pic) : null;
      if (!image && !owner) return;
      ctx.save(); ctx.beginPath(); ctx.arc(x, y, radius, 0, TAU); ctx.clip();
      ctx.fillStyle = owner ? P.bg2 : P.bg1; ctx.fillRect(x-radius, y-radius, radius*2, radius*2);
      if (image) {
        const iw = image.naturalWidth || image.width, ih = image.naturalHeight || image.height, side = Math.min(iw, ih);
        ctx.drawImage(image, (iw-side)/2, (ih-side)/2, side, side, x-radius, y-radius, radius*2, radius*2);
      } else {
        ctx.fillStyle = owner ? P.fg : P.fg2; ctx.font = `${owner ? 600 : 500} ${Math.round(radius * .67)}px ${P.sans}`;
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; if (!person.compact || owner) ctx.fillText(initials(person), x, y+.5);
      }
      ctx.restore();
      ctx.textAlign = 'left';
    }

    paintGuides(ctx, P, m, cam, S, sx, sy, want) {
      if (m.world.cohort || m.world.layout === 'network_disk') return;
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
      ctx.globalAlpha = (reduced ? 1 : a) * .7;
      for (const line of directedConnections(edges.lines, sel.id)) {
        const from = this.model.scene.get(line.selected.id) || line.selected;
        const to = this.model.scene.get(line.other.id) || line.other;
        const x0 = sx(from.x), y0 = sy(from.y), x1 = sx(to.x), y1 = sy(to.y);
        const length = Math.hypot(x1 - x0, y1 - y0);
        if (!length) continue;
        const mutual = line.incoming && line.outgoing;
        const ox = -(y1 - y0) / length * 1.25, oy = (x1 - x0) / length * 1.25;
        ctx.strokeStyle = P.fg2; ctx.lineWidth = 1;
        for (const outgoing of [false, true]) {
          if (!(outgoing ? line.outgoing : line.incoming)) continue;
          const offset = mutual ? outgoing ? 1 : -1 : 0;
          ctx.setLineDash(outgoing ? [4, 4] : []);
          ctx.beginPath(); ctx.moveTo(x0 + ox * offset, y0 + oy * offset);
          ctx.lineTo(x1 + ox * offset, y1 + oy * offset); ctx.stroke();
        }
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
        if (!seen.has(key)) { this.labelA.delete(key); continue; }
        const target = 1;
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
      if(this.model?.universe) {
        const person=this.model.universe.pick(px,py);if(!person)return null;
        const [x,y]=this.model.cam.toScreen(person.x,person.y);
        return {kind:'n',it:{d:person,x:person.x,y:person.y},x,y,r:Math.max(3,(person.portraitRadius||0)*this.model.cam.scale)};
      }
      const plan = this.displayMarks; if (!plan) return null;
      const selected=plan.nodes.find(mark=>String(mark.it.d.id)===String(this.model?.selected?.id));
      if(selected && Math.hypot(selected.x-px,selected.y-py)<=selected.r+3)return selected;
      if (plan.large && this.model.scene.spatial) {
        const moving=this.nodeDrag||this.model.returningNode;
        if(moving){const[x,y]=this.model.cam.toScreen(moving.x,moving.y),r=moving.d.portraitRadius*this.model.cam.scale;if(Math.hypot(px-x,py-y)<=r)return{kind:'n',it:moving,x,y,r};}
        const cam=this.model.cam,[wx,wy]=cam.toWorld(px,py),reach=(this.touch?16:9)/cam.scale;
        const nearby=this.model.scene.spatial.query({x0:wx-reach,y0:wy-reach,x1:wx+reach,y1:wy+reach});
        let best=null,distance=Infinity;
        for(const it of nearby){const [x,y]=cam.toScreen(it.x,it.y),r=String(it.d.id)===String(this.model.world.me?.id)?OWNER_RADIUS:it.d.portraitRadius*cam.scale,d=Math.hypot(x-px,y-py);if(d<=Math.max(r,4)&&d<distance){best={kind:'n',it,x,y,r};distance=d;}}
        return best;
      }
      const reach = this.touch ? 16 : 9;
      // Real photo bodies win before expanded hit targets in the dense overview.
      for (const mark of plan.nodes) if (Math.hypot(mark.x-px,mark.y-py)<=mark.r) return mark;
      let nearest=null, distance=Infinity;
      for (const mark of plan.nodes) { const d=Math.hypot(mark.x-px,mark.y-py); if (d<=Math.max(mark.r+3,reach) && d<distance) { nearest=mark;distance=d; } }
      if (nearest) return nearest;
      for (const mark of plan.groups) if (Math.hypot(mark.x - px, mark.y - py) <= Math.max(22, mark.r)) return mark;
      return null;
    }

    // Dragging is a temporary inspection gesture; recorded distance stays unchanged.
    startNodeDrag(mark) {
      if(this.model.universe)return false;
      const it = mark?.kind === 'n' && mark.it;

      if (!it || String(it.d.id) === String(this.model.world.me?.id)) return false;
      this.finishNodeDrag(true);
      this.nodeDrag = it; it.dur = 0; this.hover = null;
      return true;
    }
    moveNodeDrag(dx, dy) {
      const it = this.nodeDrag;
      if (!it || this.model.scene.get(it.d.id) !== it) return false;
      it.x += dx / this.model.cam.scale; it.y += dy / this.model.cam.scale;
      this.invalidate();
      return true;
    }
    finishNodeDrag(immediate = false) {
      const it = this.nodeDrag; this.nodeDrag = null;
      if (!it) return;
      if (immediate || this.model.reduced) { it.x = it.tx; it.y = it.ty; it.dur = 0; }
      else { it.fx = it.x; it.fy = it.y; it.t0 = performance.now(); it.dur = 320; if(this.model.scene.large)this.model.returningNode=it; }
      this.invalidate();
    }

    /* ---------- input ---------- */
    bind() {
      const c = this.canvas, m = this.model, pts = new Map();
      let drag = null, pinch = null, samples = [];
      const pos = (e) => { const b = c.getBoundingClientRect(); return [e.clientX - b.left, e.clientY - b.top]; };
      c.addEventListener('pointerdown', (e) => {
        if (e.pointerType === 'mouse' && e.button !== 0) return;
        this.touch = e.pointerType === 'touch';
        c.setPointerCapture(e.pointerId); c.style.cursor=''; c.classList.remove('kb'); c.focus({ preventScroll: true });
        pts.set(e.pointerId, pos(e)); m.interruptCamera();
        if (pts.size === 2) { this.finishNodeDrag(true); const [a, b] = [...pts.values()]; pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), k: m.cam.k, mid: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] }; drag = null; return; }
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
          if (!drag.moved && Math.hypot(dx, dy) > (e.pointerType === 'mouse' ? 4 : 9)) { drag.moved = true; drag.node = this.startNodeDrag(drag.at); this.hideTip(); }
          if (drag.moved) {
            const ddx = e.clientX - (drag.lx ?? drag.x), ddy = e.clientY - (drag.ly ?? drag.y);
            drag.lx = e.clientX; drag.ly = e.clientY;
            if (drag.node) { this.moveNodeDrag(ddx, ddy); return; }
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
        else if (drag.node) this.finishNodeDrag();
        else if (samples.length > 1) {
          const a = samples[0], b = samples[samples.length - 1], dt = (b[0] - a[0]) / 1000;
          if (dt > 0) m.release((b[1] - a[1]) / dt, (b[2] - a[2]) / dt);
        }
        const wasNode = drag.node; drag = null; if (!wasNode) m.moved(); this.invalidate();
      };
      c.addEventListener('pointerup', end);
      const cancel = () => { this.finishNodeDrag(true); pts.clear(); pinch=null; drag=null; m.interruptCamera(); c.classList.remove('drag'); };
      c.addEventListener('pointercancel', cancel); c.addEventListener('lostpointercapture', (e) => { if (pts.has(e.pointerId)) cancel(); });
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
      this.r.size?.addEventListener('change', () => m.setViewMode(this.r.size.value));
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
      f.follow?.addEventListener('change', () => m.setFilters({ follow: f.follow.value }));
      f.clear.addEventListener('click', () => { m.setFilters({ scope: 'all', minFit: '', status: '', follow: 'all' }); });
      doc.addEventListener('click', (e) => { if (this.r.filtersBox.open && !this.r.filtersBox.contains(e.target)) this.r.filtersBox.open = false; });
      this.r.filtersBtn.addEventListener('click', (e) => { if(m.universe)e.preventDefault(); });
      this.r.filtersBox.addEventListener('keydown', (e) => { if (e.key === 'Escape' && this.r.filtersBox.open) { e.stopPropagation(); this.r.filtersBox.open = false; this.r.filtersBox.querySelector('summary').focus(); } });
      // Card, states.
      this.r.card.addEventListener('click', (e) => this.cardClick(e));
      this.r.state.addEventListener('click', (e) => {
        if (e.target.closest('[data-act="leads"]')) this.host.openList();
        if (e.target.closest('[data-act="retry"]')) m.retry();
        if (e.target.closest('[data-act="clear"]')) m.setFilters({ scope: 'all', minFit: '', status: '', follow: 'all' });
      });
      this.r.stale.addEventListener('click', () => m.retry());
      if (root.ResizeObserver) { const observer = new ResizeObserver(() => this.measure(false)); observer.observe(this.r.canvasBox); observer.observe(this.r.card); }
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
      const same = hit && prev && hit.it.d.id === prev.it.d.id;
      this.hover = hit;
      this.canvas.style.cursor = hit?.kind === 'n' && String(hit.it.d.id) !== String(this.model.world.me?.id) ? 'grab' : hit ? 'pointer' : '';
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
        tip.append(h('b', { text: plural(d.count, 'person', 'people') }), h('span', { text: (d.label ? d.label.replace(/^(Around |Audience of )/, '') + '. ' : '') + 'Click to explore this group.' }));
      } else {
        tip.append(h('b', { text: d.name || (d.compact ? 'Saved profile' : '@' + d.handle) }), h('span', { text: d.compact ? 'Click to open' : '@' + d.handle }),
          h('span', { text: profileMetric(d, this.model.viewMode === 'shared') }));
        if (d.status) tip.append(h('span', { text: SLABEL(d.status) }));
      }
      tip.hidden = false;
    }
    hideTip() { this.r.tip.hidden = true; }
    activate(hit) {
      this.hover = null; this.hideTip();
      const m = this.model;
      if (!hit) { m.deselect(); return; }
      if (hit.kind === 'c') { const [wx, wy] = [hit.it.x, hit.it.y]; m.flyTo({ cx: wx, cy: wy, k: clamp(m.cam.k * 3.4, 1, Core.K_MAX) }, 700); m.load({ cam: { cx: wx, cy: wy, k: clamp(m.cam.k * 3.4, 1, Core.K_MAX) } }); return; }
      m.select(hit.it.d);
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
        h('b', { text: '@' + n.handle }), h('span', { text: n.name || '' }), h('i', { text: profileMetric(n, this.model.viewMode === 'shared') }))));
    }
    moveActive(d) {
      this.active = (this.active + d + this.results.length) % this.results.length;
      this.showResults(this.results, 'ok');
    }
    closeResults() { this.model.searchReq.cancel(); this.r.q.removeAttribute('aria-activedescendant'); this.r.results.hidden = true; this.r.q.setAttribute('aria-expanded', 'false'); this.results = []; this.active = -1; }
    choose(n) {
      this.closeResults(); this.r.q.value = ''; this.r.q.blur();
      this.model.goTo(n); this.canvas.focus({ preventScroll: true }); this.announce(n.compact ? 'Loading selected profile.' : `Selected ${n.name || '@' + n.handle}.`);
    }

    /* ---------- static DOM ---------- */
    buildStatic() {
      const r = this.r;
      this.density = h('select', { 'aria-label': 'People per page' },
        ...[500, 1000, 3000].map(n => h('option', { value: n, text: `${int(n)} people` })));
      this.density.value = this.model.density;
      this.previous = h('button', { type: 'button', text: 'Previous', 'aria-label': 'Previous people' });
      this.next = h('button', { type: 'button', text: 'Next', 'aria-label': 'Next people' });
      this.pageLabel = h('span', { text: 'Page 1' });
      this.pager = h('div', { class: 'mv-pager' }, this.density, this.previous, this.pageLabel, this.next);
      r.canvasBox.append(this.pager);
      this.density.addEventListener('change', () => this.model.setDensity(this.density.value));
      this.previous.addEventListener('click', () => this.model.browse(-1));
      this.next.addEventListener('click', () => this.model.browse(1));
      r.fit.setAttribute('aria-label', 'Show this page');
      r.fit.title = 'Show this page';
      r.size?.replaceChildren(...VIEW_MODES.map(o=>h('option',{value:o.id,text:o.label})));
      if (r.size) r.size.value=this.model.viewMode;
      const pop=r.filtersBox?.querySelector('.mv-pop');
      if(pop) {
        for(const [select,text] of [[r.size,'Arrange by'],[r.filters.follow,'Following']]) {
          const label=select?.closest('label');if(!label)continue;
          label.className='mv-f';label.firstChild.textContent=text+' ';pop.prepend(label);
        }
        const scope=pop.querySelector('#map-scope-l');if(scope)scope.textContent='People';
      }
      this.renderModes();
      this.renderFilters();
    }
    renderModes() {
      const universe=!!this.model.universe;
      for (const label of [this.r.size?.closest?.('label'), this.r.filters.follow?.closest?.('label')]) {
        if (label) label.hidden = universe;
      }
      if (this.r.filtersBox) this.r.filtersBox.hidden = universe;
      if(this.r.q){
        this.r.q.placeholder=universe?'Instagram handle':'Name or handle';
        this.r.q.setAttribute('aria-label',universe?'Find an Instagram handle on the map':'Find a person by name or handle on the map');
      }
      if(this.r.filtersBtn){
        this.r.filtersBtn.setAttribute('aria-disabled',String(universe));
        this.r.filtersBtn.title=universe?'Filters are available in the paged map; the full map shows all saved people.':'';
        this.r.filtersBtn.style.opacity=universe?'.45':'';
        if(universe)this.r.filtersBox.open=false;
      }
      if(this.pager)this.pager.hidden=universe;
      if(this.r.size){this.r.size.disabled=universe;this.r.size.title=universe?'The full map uses recorded follow distance and follower size.':'';}
      for(const control of [this.r.filters.fit,this.r.filters.status,this.r.filters.follow,...this.r.filters.scope.children]) {
        if(control){control.disabled=universe;control.title=universe?'Filters are available in the paged map; the full map shows all saved people.':'';}
      }
      this.r.fit.title=universe?'Show the full map':'Show this page';
      if (this.r.size) { this.r.size.value = this.model.viewMode; this.r.size.title = universe ? 'The full map uses recorded follow distance and follower size.' : VIEW_MODES.find(mode => mode.id === this.model.viewMode)?.description || ''; }
      if (this.r.filters.follow) { this.r.filters.follow.disabled = universe || !!this.model.fallback; this.r.filters.follow.title = universe ? 'Following filters are available in the paged map; the full map shows all saved people.' : this.model.fallback ? 'Following filters become available when the network layout is ready.' : 'Not following requires an explicit absence at the last complete check.'; }
      this.r.me.hidden = !this.model.world.me;
    }
    renderFilters() {
      const m = this.model, f = this.r.filters;
      for (const b of f.scope.children) { const on = b.dataset.scope === m.scope; b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on)); }
      f.fit.value = m.minFit; f.status.value = m.status; if (f.follow) f.follow.value = m.follow;
      const n = m.filtersActive; this.r.badge.hidden = !n; this.r.badge.textContent = String(n);
      f.clear.hidden = !n;
      this.r.filtersBtn.setAttribute('aria-label', n ? `Filters, ${plural(n, 'filter')} on` : 'Filters');
    }
    renderLegend() {
      const shared = this.model.viewMode === 'shared';
      const rows = this.model.universe ? [{g:'centre',s:'Closer: fewer recorded follow steps',t:'Distance shows shortest recorded follow chains. The outer band has no recorded path; that does not prove no connection.'},{g:'size',s:'Larger: more followers',t:'Portrait size uses saved follower counts. Missing counts remain unknown.'}] : this.model.fallback ? [{ g: 'size', t: 'Ranked overview. Position does not indicate a relationship.', s: 'Ranked overview' }] : [
        { g: 'centre', s: shared ? 'Closer: more shared audiences' : 'Closer: stronger connection evidence', t: shared ? 'Distance orders the saved page by distinct collected source audiences. It does not show mutual friends.' : 'Distance orders the saved page by recorded network evidence, not personal familiarity.' },
        { g: 'size', s: shared ? 'Larger: more shared audiences' : 'Larger: more followers', t: SIZE_HELP[this.model.size] }
      ], box = this.r.legend;
      const glyph = (g) => {
        const svg = (inner) => { const s = doc.createElementNS('http://www.w3.org/2000/svg', 'svg'); s.setAttribute('viewBox', '0 0 20 14'); s.setAttribute('width', '20'); s.setAttribute('height', '14'); s.setAttribute('aria-hidden', 'true'); s.innerHTML = inner; return s; };
        switch (g) {
          case 'size': return svg('<circle cx="4" cy="7" r="1.8" class="k-f"/><circle cx="10" cy="7" r="2.8" class="k-f"/><circle cx="16.4" cy="7" r="3.6" class="k-f"/>');
          case 'follow': return svg('<circle cx="5" cy="7" r="3.5" class="k-r"/><circle cx="15" cy="7" r="3.5" class="k-r" stroke-dasharray="2 2"/>');
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
      box.replaceChildren(...rows.map((row) => h('li', {}, h('span', { tabindex: '0', role: 'note', class: 'mv-key', title: row.t, 'aria-label': `${row.s || row.t}. ${row.t}` }, glyph(row.g), h('span', { text: row.s || row.t })))));
      this.measureHud(); this.invalidate();
    }
    renderCount(announce = true) {
      const m = this.model, el = this.r.count;
      if(m.universe){
        const stats=m.universe.stats(), snapshot=m.universe.manifest;
        el.textContent=`${int(stats.loaded||0)} loaded · ${int(m.total)} people`;
        el.title=snapshot?.stale ? 'Positions use a saved snapshot. Prepare a new snapshot to include later data changes.' : 'Saved network snapshot. Zoom or search to explore; visible tiles load as you move.';
        return;
      }

      if(m.pending && m.loadingCount){el.textContent=`${int(m.loadingCount)} / ${int(m.loadingTotal)} loaded`;return;}
      if (m.phase !== 'ready' && m.phase !== 'empty') { el.textContent = ''; return; }
      if (m.fallback) { el.textContent = `Overview · ${int(m.shown)} shown of ${int(m.total)} people`; el.title = `A ranked sample of up to 400 people. Filters apply to this sample. Prepare the spatial layout locally for the full map.`; return; }
      const count=m.scene.nodes.length-(m.world.me && m.scene.get(m.world.me.id)?1:0);
      el.textContent=`${int(count)} ${m.scene.large?'loaded':'shown'} · ${int(m.total)} matching people`;
      el.title='Zoom magnifies this same page. Use Next, search, or filters to see other people.';
      if (this.pager) {
        this.previous.disabled = m.pending > 0 || m.pageIndex === 0;
        this.next.disabled = m.pending > 0 || !m.nextCursor;
        this.density.disabled = m.pending > 0;
        this.pageLabel.textContent = `Page ${m.pageIndex + 1}`;
      }
      if (!announce) return;
      clearTimeout(this.annT); this.annT = setTimeout(() => this.announce(el.textContent + '.'), 900);
    }
    announce(t) { this.r.live.textContent = ''; setTimeout(() => { this.r.live.textContent = t; }, 30); }
    renderBusy() {
      const el = this.r.progress, m = this.model;
      if(m.pending && m.loadingCount)this.r.count.textContent=`${int(m.loadingCount)} / ${int(m.loadingTotal)} loaded`;
      if (this.pager) {
        this.previous.disabled = m.pending > 0 || m.pageIndex === 0;
        this.next.disabled = m.pending > 0 || !m.nextCursor;
        this.density.disabled = m.pending > 0;
      }
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
        loading: ['Loading the map', 'Loading saved connections.'],
        offline: ['You’re offline', 'The map returns when the connection does.'],
        error: ['Couldn’t load the map', 'Check your connection, then try again.'],
        empty: [m.fallback ? 'No one in this sample matches' : 'No one matches these filters', m.filtersActive ? 'Try clearing a filter.' : 'Add profiles in Accounts to get started.']
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
      card.hidden = false;
      const summary = [['Followers', n.followers == null ? 'Unknown' : int(n.followers)], ['DTC fit', 'Loading…']];
      const facts = [['Network', Core.closenessWords(n.closeness || 0)]];
      if (n.source_count != null) facts.push(['Sources', int(n.source_count)]);

      if (n.id !== m.world.me?.id && n.following_evidence) facts.push(['You follow', n.followed ? 'Recorded' : n.following_evidence === 'absent' ? 'Not following at last check' : 'Unknown']);
      if (n.follows_me) facts.push(['Follows you', 'Recorded']);
      const avatar = h('span', { class: 'mv-av', 'aria-hidden': 'true', text: n.compact ? '…' : initials(n) });
      if (/^\/img\/\d+$/.test(n.pic || '')) {
        const image = h('img', { src: n.pic, alt: '', decoding: 'async' });
        image.addEventListener('error', () => avatar.replaceChildren(n.compact ? '…' : initials(n)), { once: true });
        avatar.replaceChildren(image);
      }
      card.replaceChildren(
        h('div', { class: 'mv-c-head' },
          avatar,
          h('div', { class: 'mv-who' }, h('b', { text: n.compact ? 'Loading profile…' : n.name || '@' + n.handle }), h('span', { text: n.compact ? '' : '@' + n.handle })),
          h('button', { type: 'button', class: 'mv-x', 'data-act': 'close', 'aria-label': 'Close details', title: 'Close (esc)', text: '×' })),
        h('section', { class: 'mv-tags' },
          h('div', { class: 'mv-tag-head' }, h('h4', { text: 'Relationship & tags' }),
            h('button', { type: 'button', class: 'mv-tag-add', text: 'Add tag', 'aria-expanded': 'false', 'aria-controls': 'mv-tag-form' })),
          h('div', { class: 'mv-tag-chips' }),
          h('form', { class: 'mv-tag-form', id: 'mv-tag-form', hidden: true },
            h('input', { class: 'input', placeholder: 'Add a tag', list: 'mv-tag-options', 'aria-label': 'Add a tag', maxlength: 100 }),
            h('datalist', { id: 'mv-tag-options' }), h('button', { class: 'btn', type: 'submit', text: 'Add' })),
          h('p', { class: 'mv-tag-feedback', role: 'status' })),
        h('dl', { class: 'mv-facts mv-summary' }, summary.map(([k, v]) => h('div', {}, h('dt', { text: k }), h('dd', { text: v })))),
        h('details', { class: 'mv-lead-stage' }, h('summary', { text: 'Lead stage' }),
          h('label', { class: 'mv-quick-status' }, h('span', { text: 'Stage' }),
            h('select', { 'aria-label': 'Lead status' }, h('option', { value: '', text: 'Not in pipeline' }),
              this.host.statuses.filter(status => status !== 'client').map(status => h('option', { value: status, text: SLABEL(status) }))))),
        h('details', { class: 'mv-connections' }, h('summary', { text: 'Connections' }),
          h('div', { class: 'mv-line-key', 'aria-label': 'Recorded follow directions' },
            [['incoming', 'They follow this person'], ['outgoing', 'This person follows them'], ['mutual', 'Both follow each other']].map(([kind, label]) =>
              h('span', {}, h('i', { class: 'mv-line-sample ' + kind, 'aria-hidden': 'true' }), h('span', { text: label })))),
          h('dl', { class: 'mv-facts' }, facts.map(([k, v]) => h('div', {}, h('dt', { text: k }), h('dd', { text: v })))),
          h('section', { class: 'mv-seeds', 'aria-live': 'polite' }, h('h4', { text: 'Sources' }), h('ul', { class: 'mv-chips', id: 'mv-chips' }))),
        h('div', { class: 'mv-actions' },
          h('button', { type: 'button', class: 'btn', 'data-act': 'open', text: 'Full profile' }),
          h('button', { type: 'button', class: 'btn', 'data-act': 'note', 'aria-expanded': 'false', text: 'Note' })),
        h('div', { class: 'mv-note', hidden: true }));
      const statusSelect = card.querySelector('.mv-quick-status select');
      statusSelect.value = n.status === 'client' ? '' : n.status || '';
      statusSelect.addEventListener('change', () => this.setStatus(n, statusSelect.value || null));
      const connections = card.querySelector('.mv-connections');
      connections.addEventListener('toggle', () => {
        if (this.model?.selected?.id === n.id) this.model.setConnectionsVisible(connections.open);
      });
      const tagForm = card.querySelector('.mv-tag-form');
      card.querySelector('.mv-tag-add').addEventListener('click', () => this.toggleTagForm(tagForm.hidden));
      tagForm.addEventListener('keydown', event => {
        if (event.key !== 'Escape') return;
        event.preventDefault(); event.stopPropagation();
        this.toggleTagForm(false);
      });
      tagForm.addEventListener('submit', event => {
        event.preventDefault();
        const input = tagForm.querySelector('input');
        if (input.value.trim()) this.editTag(n, input.value.trim(), false);
      });
      this.loadTags(n);
      if (!same) card.scrollTop = 0;
      this.renderSeeds();
      // A sheet on a phone changes what "centre" means; recompute the inset.
      requestAnimationFrame(() => this.measure(false));
      this.announce(n.compact ? 'Loading selected profile.' : `Selected ${n.name || '@' + n.handle}.`);
    }
    hydrateCardIdentity(n, person) {
      if (String(person.id) !== String(n.id) || this.model?.selected?.id !== n.id) return;
      const identity = {
        handle: person.handle || '', name: person.name || '',
        pic: /^\/img\/\d+$/.test(person.pic || '') ? person.pic : null,
        compact: false
      };
      Object.assign(n, identity);
      Object.assign(this.model.selected, identity);
      const item = this.model.scene.get(n.id);
      if (item) Object.assign(item.d, identity);
      const card = this.r.card;
      card.querySelector('.mv-who b').textContent = identity.name || (identity.handle ? '@' + identity.handle : 'Profile');
      card.querySelector('.mv-who span').textContent = identity.handle ? '@' + identity.handle : '';
      const avatar = card.querySelector('.mv-av');
      avatar.replaceChildren(initials(identity));
      if (identity.pic) {
        const image = h('img', { src: identity.pic, alt: '', decoding: 'async' });
        image.addEventListener('error', () => avatar.replaceChildren(initials(identity)), { once: true });
        avatar.replaceChildren(image);
      }
      this.invalidate();
    }
    updateCardFacts() {
      const n = this.model.selected;
      const status = this.r.card.querySelector('.mv-quick-status select');
      if (status) status.value = n.status === 'client' ? '' : n.status || '';
    }
    renderSeeds() {
      const ul = this.r.card.querySelector('#mv-chips'), e = this.model.edges, n = this.model.selected;
      if (!ul || !n) return;
      ul.replaceChildren();
      if (!e || e.id !== n.id || e.state === 'loading') { ul.append(h('li', { class: 'mv-note-line', text: 'Loading…' })); return; }
      if (e.state === 'error') { ul.append(h('li', { class: 'mv-note-line', text: 'Couldn’t load connections.' })); return; }
      if (e.overview) { ul.append(h('li', { class: 'mv-note-line', text: 'Use Compare profiles to see recorded follow paths.' })); return; }
      if (!e.seeds.length) { ul.append(h('li', { class: 'mv-note-line', text: 'No recorded source path yet.' })); return; }
      this.r.card.querySelector('.mv-seeds h4').textContent = `${plural(e.seeds.length, 'recorded source')}`;
      for (const s of e.seeds.slice(0, 8)) ul.append(h('li', {}, h('button', { type: 'button', class: 'mv-chip', 'data-goto': s.id, title: 'Show on the map', text: '@' + s.handle })));
    }
    cardClick(e) {
      const m = this.model, n = m.selected; if (!n) return;
      const act = e.target.closest('[data-act]')?.dataset.act, card = this.r.card;
      const go = e.target.closest('[data-goto]');
      if (go) { const it = m.edges?.seeds.find((s) => String(s.id) === go.dataset.goto); if (it) m.goTo(it, { k: Math.max(m.cam.k, 4) }); return; }
      if (act === 'close') { m.deselect(); this.canvas.focus({ preventScroll: true }); }
      else if (act === 'open') this.host.openLead(n.id, n.handle);
      else if (act === 'note') this.toggleNote(e.target.closest('[data-act]'));
      if (e.target.closest('[data-note="save"]')) this.saveNote();
      if (e.target.closest('[data-note="cancel"]')) { card.querySelector('.mv-note').hidden = true; card.querySelector('[data-act="note"]').setAttribute('aria-expanded', 'false'); }
    }
    async setStatus(n, status) {
      this.statusWrites ||= new Map();
      if (this.statusWrites.has(n.id)) return;
      this.statusWrites.set(n.id, true);
      const before = this.model.scene.get(n.id)?.d.status || n.status || null;
      const control = this.r.card.querySelector('.mv-quick-status select');
      if (control) control.disabled = true;
      this.model.patchStatus(n.id, status);
      try {
        const saved = await this.host.setStatus(n.id, status);
        this.model.patchStatus(n.id, saved.status ?? status);
        this.host.toast(status ? `Marked ${SLABEL(status).toLowerCase()}` : 'Status cleared');
      } catch (_) {
        this.model.patchStatus(n.id, before);
        this.host.toast('Couldn’t save. Try again.');
      } finally {
        this.statusWrites.delete(n.id);
        if (control?.isConnected) control.disabled = false;
      }
    }
    toggleTagForm(show) {
      if (this.tagSaving) return;
      const box = this.r.card.querySelector('.mv-tags');
      if (!box) return;
      const form = box.querySelector('.mv-tag-form');
      const button = box.querySelector('.mv-tag-add');
      form.hidden = !show;
      button.setAttribute('aria-expanded', String(show));
      if (show) form.querySelector('input').focus();
      else button.focus();
    }
    async loadTags(n) {
      const box = this.r.card.querySelector('.mv-tags');
      if (!box) return;
      const version = box._readVersion = (box._readVersion || 0) + 1;
      const feedback = box.querySelector('.mv-tag-feedback');
      feedback.textContent = 'Loading…';
      try {
        const person = await this.host.person(n.id);
        if (this.model?.selected?.id !== n.id || !box.isConnected || box._readVersion !== version) return;
        this.hydrateCardIdentity(n, person);
        this.paintTags(n, person, box);
        const fit = cardFit(person);
        const fitValue = this.r.card.querySelector('.mv-summary > div:last-child dd');
        fitValue.textContent = fit.text;
        fitValue.title = fit.detail;
        feedback.textContent = '';
        if (this.host.tags) {
          const tags = await this.host.tags();
          if (box.isConnected) {
            const names = [...new Set([...Object.values(RELATIONSHIP_TAGS), ...tags.map(t => t.tag)])];
            box.querySelector('datalist').replaceChildren(...names.map(tag => h('option', { value: tag })));
          }
        }
      } catch (_) {
        if (this.model?.selected?.id === n.id && box.isConnected && box._readVersion === version) {
          feedback.textContent = 'Couldn’t load profile details. Reopen to retry.';
          if (n.compact) {
            this.r.card.querySelector('.mv-who b').textContent = 'Profile unavailable';
            this.r.card.querySelector('.mv-who span').textContent = '';
          }
          const fitValue = this.r.card.querySelector('.mv-summary > div:last-child dd');
          if (fitValue?.textContent === 'Loading…') fitValue.textContent = 'Unavailable';
        }
      }
    }
    paintTags(n, person, box) {
      const tags = cardTags(person);
      box.querySelector('.mv-tag-chips').replaceChildren(...tags.map(tag => {
        const presentation = this.host.tagPresentation?.(tag.label) || {
          label: tag.label, importance: tag.relationship === 'client' ? 'strong' : 'standard', tone: '',
          icon: tag.relationship === 'client' ? 'verified' : ''
        };
        const button = h('button', {
          type: 'button', class: 'tag mv-card-tag', 'data-relationship': tag.relationship,
          'data-importance': presentation.importance, 'data-tone': presentation.tone,
          title: tag.label, 'aria-label': 'Remove ' + tag.label
        }, presentation.icon ? h('span', { class: 'tag-icon', 'data-icon': presentation.icon, 'aria-hidden': 'true' }) : null,
          h('span', { text: presentation.label }), h('i', { class: 'x', text: '×', 'aria-hidden': 'true' }));
        button.addEventListener('click', () => this.editTag(n, tag.label, true));
        return button;
      }));
    }
    async editTag(n, tag, remove) {
      if (this.tagSaving) return;
      const box = this.r.card.querySelector('.mv-tags');
      if (!box) return;
      box._readVersion = (box._readVersion || 0) + 1;
      this.tagSaving = true;
      for (const el of box.querySelectorAll('input,button')) el.disabled = true;
      const feedback = box.querySelector('.mv-tag-feedback');
      feedback.textContent = 'Saving…';
      let saved = false;
      try {
        const person = await this.host.editTags(n.id, remove ? [] : [tag], remove ? [tag] : []);
        if (this.model?.selected?.id === n.id && box.isConnected) {
          this.paintTags(n, person, box);
          if (person.status !== undefined) this.model.patchStatus(n.id, person.status);
          if (!remove) box.querySelector('input').value = '';
          feedback.textContent = 'Saved';
          saved = true;
        }
      } catch (_) {
        if (box.isConnected) feedback.textContent = 'Couldn’t save. Try again.';
      } finally {
        this.tagSaving = false;
        for (const el of box.querySelectorAll('input,button')) el.disabled = false;
        if (saved && !remove && this.model?.selected?.id === n.id && box.isConnected) this.toggleTagForm(false);
      }
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
        if (this.model?.selected?.id !== n.id || read !== this.noteRead || !box.isConnected) return;
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
      pane, size: $('map-size'), canvas: $('map-canvas'), canvasBox: $('map-canvas-box'), q: $('map-q'), results: $('map-search-results'), filtersBox: $('map-filters'), filtersBtn: $('map-filters').querySelector('summary'),
      badge: $('map-filter-badge'), filters: { scope: $('map-scope'), fit: $('map-fit-filter'), status: $('map-status-filter'), follow: $('map-follow-filter'), clear: $('map-filters-clear') },
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

  const api = { MapView, mount, initials, cardTags, cardFit, directedConnections };
  root.MapViewModule = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window === 'undefined' ? globalThis : window);
