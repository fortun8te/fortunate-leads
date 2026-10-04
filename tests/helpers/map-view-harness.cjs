/* Runs the real MapView in Node against a recording canvas, a fake animation-frame clock and a
 * scriptable photo loader, so draw counts, blank frames and fades can be asserted.
 *
 *   const h = harness({ people: 500 });
 *   await h.ready();            // data loaded, photos decoded, frames settled
 *   h.pan(40, 10);              // drives the real pointer handlers
 *   h.frames(5);                // runs five animation frames; returns what each one drew
 */
const fs = require('node:fs');
const vm = require('node:vm');
const { performance } = require('node:perf_hooks');

class Ctx {
  constructor(canvas, log) {
    this.canvas = canvas; this.log = log; this.counts = {}; this.globalAlpha = 1; this.fillStyle = '#000'; this.strokeStyle = '#000'; this.lineWidth = 1; this.font = '';
    this.alphas = []; this.draws = [];
    for (const name of ['save', 'restore', 'beginPath', 'closePath', 'moveTo', 'lineTo', 'arc', 'clip', 'fill', 'stroke', 'fillRect', 'strokeRect', 'clearRect', 'setTransform', 'fillText', 'strokeText', 'roundRect', 'setLineDash', 'rect', 'resetTransform', 'translate', 'scale']) {
      this[name] = (...a) => { this.counts[name] = (this.counts[name] || 0) + 1; if (name === 'clearRect') log.push({ op: 'clear', canvas: canvas.id }); };
    }
  }
  drawImage(source, ...a) {
    this.counts.drawImage = (this.counts.drawImage || 0) + 1;
    this.draws.push({ source, a, alpha: this.globalAlpha, canvas: this.canvas.id });
    this.log.push({ op: 'draw', canvas: this.canvas.id, source: source && source.id });
  }
  measureText(s) { return { width: String(s).length * 6 }; }
  createLinearGradient() { return { addColorStop() {} }; }
  getImageData() { return { data: new Uint8ClampedArray(4) }; }
}

class El {
  constructor(tag, log, id) {
    this.tagName = String(tag).toUpperCase(); this.id = id || ''; this.style = { removeProperty() {}, setProperty() {} }; this.children = []; this.attributes = {}; this.dataset = {}; this.hidden = false;
    this.handlers = {}; this.value = ''; this.textContent = ''; this.className = ''; this.disabled = false; this.open = false;
    this.classes = new Set(); this.log = log; this.clientWidth = 0; this.clientHeight = 0; this.offsetWidth = 120; this.offsetHeight = 60;
    this.classList = { add: (...c) => c.forEach((x) => this.classes.add(x)), remove: (...c) => c.forEach((x) => this.classes.delete(x)), toggle: (c, on) => { if (on === undefined ? !this.classes.has(c) : on) this.classes.add(c); else this.classes.delete(c); }, contains: (c) => this.classes.has(c) };
    this.widthSets = 0; this._width = 300; this._height = 150; this.rect = { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 };
  }
  get width() { return this._width; }
  set width(v) { this._width = v; this.widthSets++; this.log.push({ op: 'canvas-width', canvas: this.id }); if (this.ctxObj) this.ctxObj.counts.clearRect = (this.ctxObj.counts.clearRect || 0); }
  get height() { return this._height; }
  set height(v) { this._height = v; }
  getContext() { return this.ctxObj || (this.ctxObj = new Ctx(this, this.log)); }
  setAttribute(k, v) { this.attributes[k] = v; if (k === 'id') this.id = v; }
  removeAttribute(k) { delete this.attributes[k]; }
  getAttribute(k) { return this.attributes[k]; }
  append(...kids) { this.children.push(...kids); }
  appendChild(k) { this.children.push(k); return k; }
  replaceChildren(...kids) { this.children = kids; }
  after() {} remove() {} replaceWith() {} focus() {} blur() {} click() { (this.handlers.click || []).forEach((f) => f({ target: this })); }
  closest() { return this; }
  contains() { return false; }
  cloneNode() { return new El(this.tagName, this.log, this.id); }
  querySelector() { return new El('div', this.log); }
  querySelectorAll() { return []; }
  getBoundingClientRect() { return { left: 0, top: 0, right: this.clientWidth, bottom: this.clientHeight, width: this.clientWidth, height: this.clientHeight }; }
  setPointerCapture() {} releasePointerCapture() {} hasPointerCapture() { return true; }
  addEventListener(name, fn) { (this.handlers[name] || (this.handlers[name] = [])).push(fn); }
  removeEventListener() {}
  fire(name, event = {}) { for (const fn of this.handlers[name] || []) fn({ preventDefault() {}, stopPropagation() {}, target: this, ...event }); }
}

function harness({ people = 500, width = 1000, height = 700, dpr = 2, reduced = false, nodeFor = null, edges = null } = {}) {
  const log = [];
  const els = new Map();
  const get = (id) => { if (!els.has(id)) els.set(id, new El('div', log, id)); return els.get(id); };
  const doc = {
    hidden: false, handlers: {},
    createElement: (tag) => new El(tag, log),
    createElementNS: (ns, tag) => new El(tag, log),
    getElementById: (id) => get(id),
    documentElement: { dataset: { theme: 'dark' } }, body: new El('body', log),
    addEventListener(name, fn) { (this.handlers[name] || (this.handlers[name] = [])).push(fn); }
  };
  const canvas = get('map-canvas'), box = get('map-canvas-box'), card = get('map-card');
  canvas.id = 'map-canvas'; canvas.getContext();
  box.clientWidth = width; box.clientHeight = height;
  get('pane-map').classList.add('on');
  get('map-filters').querySelector = () => new El('summary', log);
  // frame clock
  const queue = []; let nextId = 1, clock = 1000;
  // photos
  const images = [];
  class Img { constructor() { images.push(this); this.naturalWidth = 0; this.naturalHeight = 0; } set src(v) { this._src = v; } get src() { return this._src; } }
  const root = {
    document: doc, performance, console, Math, Promise, setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {}, URLSearchParams, Map, Set, Float32Array, Float64Array, Intl,
    devicePixelRatio: dpr, navigator: { onLine: true }, location: { search: '' },
    requestAnimationFrame: (cb) => { const id = nextId++; queue.push({ id, cb }); return id; },
    cancelAnimationFrame: (id) => { const i = queue.findIndex((q) => q.id === id); if (i >= 0) queue.splice(i, 1); },
    matchMedia: (q) => ({ matches: reduced && /reduce/.test(q), addEventListener() {} }),
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    localStorage: { getItem: () => null, setItem() {} },
    Image: Img, ResizeObserver: class { constructor(fn) { root.__resize = fn; } observe() {} },
    MutationObserver: class { observe() {} }, addEventListener() {}, Event: class {}, AbortController
  };
  root.window = undefined;
  const context = vm.createContext(root);
  context.MapCore = require('../../web/map-core.js');
  context.MapModel = require('../../web/map-model.js');
  context.MapRender = require('../../web/map-render.js');
  vm.runInContext(fs.readFileSync(require.resolve('../../web/map-view.js'), 'utf8'), context);

  const make = (i) => nodeFor ? nodeFor(i) : ({ id: i + 1, handle: 'p' + (i + 1), name: 'Person ' + (i + 1), x: .5, y: .5, rank: 1 - i / people, fit: 'good', closeness: 1 - i / people, followers: 1000 + (i % 40) * 10000, pic: '/img/' + (i + 1), source_count: i % 7, cluster: i % 5 });
  const nodes = Array.from({ length: people }, (_, i) => make(i));
  const me = { id: 0, handle: 'owner', name: 'Owner', x: .5, y: .5, pic: '/img/0', rank: 1, fit: 'good' };
  const requests = [];
  const host = {
    fetchJson: async (url) => {
      requests.push(url);
      if (url.startsWith('/api/map/edges')) return edges ? edges(url) : { nodes: [], edges: [] };
      return { rev: 1, ready: true, nodes, clusters: [], total: people, shown: people, hidden: 0, world: { layout: 'network_disk', me, guides: [] }, next_cursor: null };
    },
    statuses: [], openList() {}, toast() {}, person: async () => ({}), tags: async () => [], setStatus: async () => ({}), editTags: async () => ({}), saveNote: async () => ({})
  };
  const api = context.MapViewModule;
  const view = api.mount(host);
  let t = clock;
  const h = {
    view, model: view.model, canvas, ctx: canvas.getContext(), log, images, requests, host, context, els, doc, root,
    get pendingFrames() { return queue.length; },
    // Run one animation frame at the next 60 Hz tick. Returns canvas operations since the last call.
    frame(ms = 16.667) {
      t += ms; const run = queue.splice(0); const before = snapshot(canvas.getContext());
      for (const q of run) q.cb(t);
      return diff(before, canvas.getContext(), run.length);
    },
    // Run frames until none is pending or `max` is reached. Returns the per-frame results.
    settle(max = 600) { const out = []; for (let i = 0; i < max && queue.length; i++) out.push(h.frame()); return out; },
    frames(n) { const out = []; for (let i = 0; i < n; i++) out.push(h.frame()); return out; },
    // Finish every photo request with a decoded 64 px image.
    async photos() {
      for (let guard = 0; guard < 2000; guard++) {
        const loading = images.filter((i) => i.onload && !i.done && i.src);
        if (!loading.length) break;
        for (const i of loading) { i.done = true; i.naturalWidth = i.naturalHeight = 64; i.width = i.height = 64; i.onload(); }
        await Promise.resolve();
      }
    },
    async ready() {
      view.show(); await view.model.load(); await Promise.resolve();
      h.settle(); await h.photos(); h.settle(); h.frames(40); h.settle();
      return h;
    },
    event(name, e = {}) { canvas.fire(name, { pointerId: 1, pointerType: 'mouse', button: 0, buttons: 1, clientX: 0, clientY: 0, ...e }); },
    pan(dx, dy, steps = 6, from = [width / 2, height / 2]) {
      h.event('pointerdown', { clientX: from[0], clientY: from[1] });
      for (let i = 1; i <= steps; i++) { h.event('pointermove', { clientX: from[0] + dx * i / steps, clientY: from[1] + dy * i / steps }); }
      h.event('pointerup', { clientX: from[0] + dx, clientY: from[1] + dy, buttons: 0 });
    },
    hover(x, y) { h.event('pointermove', { clientX: x, clientY: y, buttons: 0 }); },
    resize(w, hh) { box.clientWidth = w; box.clientHeight = hh; root.__resize && root.__resize(); },
    marks() { const m = view.model, cam = m.cam; return m.scene.nodes.filter((it) => String(it.d.id) !== '0').map((it) => { const [x, y] = cam.toScreen(it.x, it.y); return { it, x, y, r: it.d.portraitRadius * cam.scale }; }); }
  };
  return h;
}
function snapshot(ctx) { return { counts: { ...ctx.counts }, draws: ctx.draws.length, log: 0 }; }
function diff(before, ctx, callbacks) {
  const counts = {};
  for (const k of Object.keys(ctx.counts)) { const d = ctx.counts[k] - (before.counts[k] || 0); if (d) counts[k] = d; }
  return { counts, callbacks, draws: ctx.draws.slice(before.draws) };
}
module.exports = { harness, El, Ctx };
