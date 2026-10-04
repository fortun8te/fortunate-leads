/* A tiny DOM for tests that need elements, attributes, events and text, without a browser. */
class FakeElement {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.hidden = false; this.className = '';
    this.value = ''; this.focused = false; this._text = '';
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'class') this.className = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : (k === 'class' ? this.className : null); }
  removeAttribute(k) { delete this.attrs[k]; }
  append(...kids) { this.children.push(...kids.map((k) => (typeof k === 'string' ? k : k))); }
  prepend(...kids) { this.children.unshift(...kids); }
  replaceChildren(...kids) { this.children = [...kids]; this._text = ''; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => (typeof c === 'string' ? c : c.textContent)).join(''); }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  dispatch(name, event = {}) {
    const e = { target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; }, ...event };
    for (const fn of this.listeners[name] || []) fn(e);
    return e;
  }
  focus() { this.focused = true; }
  blur() { this.focused = false; }
  scrollIntoView() {}
  closest(selector) {
    const attr = /^\[([\w-]+)\]$/.exec(selector)?.[1];
    for (let el = this; el; el = el.parent) if (attr && attr in el.attrs) return el;
    return null;
  }
  // Link children to parents lazily so closest() works after a render.
  link() { for (const c of this.children) if (c && typeof c === 'object') { c.parent = this; c.link(); } return this; }
  find(predicate, out = []) {
    if (predicate(this)) out.push(this);
    for (const c of this.children) if (c && typeof c === 'object') c.find(predicate, out);
    return out;
  }
}
const doc = { createElement: (tag) => new FakeElement(tag) };
// Timers that run only when told to, so debounce and delays are deterministic.
function manualTimers() {
  let next = 1; const pending = new Map();
  return {
    set(fn, ms) { const id = next++; pending.set(id, { fn, ms }); return id; },
    clear(id) { pending.delete(id); },
    get size() { return pending.size; },
    flush() { const run = [...pending.entries()]; pending.clear(); for (const [, t] of run) t.fn(); },
    delays() { return [...pending.values()].map((t) => t.ms); }
  };
}
module.exports = { FakeElement, doc, manualTimers };
