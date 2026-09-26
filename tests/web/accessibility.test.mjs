import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const helperSource = readFileSync(new URL('../../web/accessibility.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
function between(start, end) {
  const from = appSource.indexOf(start), to = appSource.indexOf(end, from);
  assert.ok(from >= 0 && to > from, 'the test must execute the actual app handler');
  return appSource.slice(from, to);
}
const keyboardSource = between('// ---------- keyboard ----------', '// ---------- tags manager ----------');
const detailEscapeSource = between("$('#detail').addEventListener('keydown', (e) => {", '\nconst noteQueue =');

// Only the DOM operations used by these handlers are faked. Keyboard defaults are
// deliberately not simulated: the checks assert whether the app cancels the event.
class Events {
  listeners = new Map();
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(listener);
  }
  removeEventListener(type, listener) { this.listeners.get(type)?.delete(listener); }
  emit(type, event) { for (const listener of this.listeners.get(type) || []) listener(event); }
}
class Element extends Events {
  constructor(doc, tag, attributes = {}) {
    super();
    this.ownerDocument = doc;
    this.tagName = tag.toUpperCase();
    this.nodeType = 1;
    this.attributes = new Map();
    this.dataset = {};
    this.children = [];
    this.parentElement = null;
    this.style = {};
    this.value = '';
    this.classes = new Set();
    this.classList = {
      contains: (name) => this.classes.has(name),
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
    };
    for (const [name, value] of Object.entries(attributes)) this.setAttribute(name, value);
  }
  get id() { return this.getAttribute('id') || ''; }
  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(value) { value ? this.setAttribute('hidden', '') : this.removeAttribute('hidden'); }
  get inert() { return this.hasAttribute('inert'); }
  set inert(value) { value ? this.setAttribute('inert', '') : this.removeAttribute('inert'); }
  get open() { return this.hasAttribute('open'); }
  set open(value) { value ? this.setAttribute('open', '') : this.removeAttribute('open'); }
  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(value) { value ? this.setAttribute('disabled', '') : this.removeAttribute('disabled'); }
  get isConnected() { return this.ownerDocument.documentElement.contains(this); }
  get tabIndex() {
    if (this.hasAttribute('tabindex')) return Number(this.getAttribute('tabindex'));
    return /^(BUTTON|INPUT|SELECT|TEXTAREA|SUMMARY)$/.test(this.tagName) || this.tagName === 'A' && this.hasAttribute('href') || this.hasAttribute('contenteditable') && this.isContentEditable ? 0 : -1;
  }
  get isContentEditable() {
    const own = this.getAttribute('contenteditable');
    return own === null ? !!this.parentElement?.isContentEditable : own.toLowerCase() !== 'false';
  }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
  }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  hasAttribute(name) { return this.attributes.has(name); }
  removeAttribute(name) { this.attributes.delete(name); }
  append(...nodes) { for (const node of nodes) { node.remove(); node.parentElement = this; this.children.push(node); } }
  remove() {
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((node) => node !== this);
    this.parentElement = null;
    if (this.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = this.ownerDocument.body;
  }
  replaceChildren(...nodes) { for (const node of [...this.children]) node.remove(); this.append(...nodes); }
  contains(node) { return !!node && (node === this || this.children.some((child) => child.contains(node))); }
  matches(selector) {
    return selector.split(',').some((part) => {
      let query = part.trim();
      if (query === ':disabled') return this.disabled || !!this.closest('fieldset[disabled]');
      const excluded = [...query.matchAll(/:not\(([^)]+)\)/g)];
      if (excluded.some((match) => this.matches(match[1]))) return false;
      query = query.replace(/:not\([^)]+\)/g, '');
      const tag = query.match(/^[a-z]+/i)?.[0];
      if (tag && this.tagName !== tag.toUpperCase()) return false;
      const id = query.match(/#([\w-]+)/)?.[1];
      if (id && this.id !== id) return false;
      for (const match of query.matchAll(/\[([^\]=]+)(?:="([^"]*)")?\]/g)) {
        if (!this.hasAttribute(match[1]) || match[2] !== undefined && this.getAttribute(match[1]) !== match[2]) return false;
      }
      return true;
    });
  }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; }
  querySelectorAll(selector) { return this.children.flatMap((node) => [...(node.matches(selector) ? [node] : []), ...node.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getClientRects() {
    for (let node = this; node; node = node.parentElement) if (node.hidden || node.style.display === 'none') return [];
    return this.isConnected ? [{}] : [];
  }
  focus() {
    if (!this.isConnected || !this.getClientRects().length || this.closest('[inert]') || this.matches(':disabled')) return;
    this.ownerDocument.activeElement = this;
    this.ownerDocument.emit('focusin', { target: this });
  }
  blur() { if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = this.ownerDocument.body; }
  setSelectionRange(start, end, direction) { this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction; }
}
function fixture({ mobile = true, globals = false } = {}) {
  const document = new Events(), window = new Events(), calls = [];
  document.defaultView = window;
  document.documentElement = new Element(document, 'html');
  document.body = new Element(document, 'body');
  document.documentElement.append(document.body);
  document.activeElement = document.body;
  document.getElementById = (id) => document.documentElement.querySelector('#' + id);
  window.getComputedStyle = (node) => node.style;
  window.open = (...args) => calls.push(['window.open', ...args]);
  const add = (tag, attributes = {}, parent = document.body) => {
    const node = new Element(document, tag, attributes); parent.append(node); return node;
  };
  const shell = add('main'), list = add('section', { id: 'rows' }, shell);
  const opener = add('button', { id: 'open-lead' }, list);
  const fallback = add('input', { id: 'q' }, list);
  const panel = add('aside', { id: 'detail', hidden: '', 'data-owner': '7', role: 'complementary', 'aria-label': 'Lead' }, shell);
  const title = add('b', { id: 'd-person-title', tabindex: '-1' }, panel);
  const first = add('button', { id: 'd-close' }, panel);
  const last = add('textarea', { id: 'note' }, panel);
  const help = add('div', { id: 'help', hidden: '' });
  add('button', { id: 'help-btn' });
  const filters = add('div', { id: 'filters' });
  const state = { view: 'leads', open: null, seedCard: null, person: null, cur: 0, anchor: -1, pick: new Set(), rows: [{ id: 7, handle: 'one' }, { id: 8, handle: 'two' }] };
  const map = { focus: null, draw: () => calls.push(['map.draw']), fit: () => calls.push(['map.fit']), zoomBy: () => calls.push(['map.zoom']), toggleLabels: () => calls.push(['map.labels']), next: () => calls.push(['map.next']) };
  const record = (name) => (...args) => calls.push([name, ...args]);
  const context = vm.createContext({ window, document, Date, location: { hash: '#leads' },
    $: (selector) => selector.startsWith('#') && !selector.includes(' ') ? document.getElementById(selector.slice(1)) : null,
    narrow: () => mobile, S: state, M: map, STATUSES: ['new', 'contacted', 'replied', 'won', 'rejected'], CYCLE: [null, 'contacted'],
    hashFor: (view) => '#' + view,
    select: (index) => { state.cur = index; calls.push(['select', index]); },
    current: () => state.person || state.rows[state.cur],
    closeDetail: () => { calls.push(['closeDetail']); state.open = null; panel.hidden = true; access.close(); },
    openDetail: async (id) => { calls.push(['openDetail', id]); state.open = id; },
    setDrawer: (show) => { calls.push(['setDrawer', show]); filters.classList.remove('show'); },
    clearPick: () => { state.pick.clear(); calls.push(['clearPick']); },
    renderRows: record('renderRows'), mark: record('mark'), bulk: record('bulk'), togglePick: record('togglePick'),
    pickAllInFilter: record('pickAll'), applyDensity: record('density'), suggest: record('suggest'),
    toggleSide: record('toggleSide'), clearFilters: record('clearFilters'), startSaveView: record('startSaveView'),
  });
  vm.runInContext(helperSource, context, { filename: 'web/accessibility.js' });
  context.LeadAccessibility = window.LeadAccessibility;
  let access;
  if (globals) {
    vm.runInContext(keyboardSource + '\n' + detailEscapeSource, context, { filename: 'web/app.js keyboard handlers' });
    access = vm.runInContext('detailAccess', context);
  } else access = window.LeadAccessibility.createDetailFocus({ panel, isMobile: () => mobile, fallbackFocus: () => fallback });
  const key = (key, target = document.activeElement, options = {}) => {
    const event = { key, target, defaultPrevented: false, stopped: false,
      preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; }, ...options };
    for (let node = target?.nodeType === 3 ? target.parentElement : target; node; node = node.parentElement) {
      node.emit('keydown', event); if (event.stopped) return event;
    }
    document.emit('keydown', event);
    return event;
  };
  const open = () => { opener.focus(); access.open(); state.open = 7; panel.hidden = false; access.sync(); };
  return { document, window, add, shell, list, opener, fallback, panel, title, first, last, help, filters, state, map, calls, context, access, key, open,
    api: window.LeadAccessibility, resize(value) { mobile = value; window.emit('resize', {}); } };
}

test('native controls keep Enter and navigation keys without opening or changing a lead', () => {
  const h = fixture({ mobile: false, globals: true });
  for (const [tag, attributes] of [['button', {}], ['a', { href: '/profile' }], ['summary', {}], ['span', { role: 'button' }]]) {
    const control = h.add(tag, attributes, h.list), child = h.add('span', {}, control);
    for (const target of [control, child, { nodeType: 3, parentElement: child }]) {
      for (const key of ['Enter', 'ArrowDown', 'ArrowUp', 'j', 'm', '/', '1']) {
        assert.equal(h.key(key, target).defaultPrevented, false, `${tag} ${key}`);
        assert.equal(h.state.cur, 0);
        assert.deepEqual(h.calls, []);
      }
    }
  }
});

test('typing in editors or their children never triggers a global shortcut', () => {
  const h = fixture({ mobile: false, globals: true });
  for (const [tag, attributes] of [['input', {}], ['textarea', {}], ['select', {}], ['div', { contenteditable: '' }], ['div', { contenteditable: 'plaintext-only' }], ['div', { role: 'textbox' }]]) {
    const control = h.add(tag, attributes, h.list), child = h.add('span', {}, control);
    for (const key of ['Enter', 'j', 'm', '1', '/', 'g', 'l']) {
      assert.equal(h.key(key, child).defaultPrevented, false, `${tag} ${key}`);
      assert.deepEqual(h.calls, []);
      assert.equal(h.context.location.hash, '#leads');
    }
  }
  const editor = h.add('div', { contenteditable: 'true' }, h.list);
  const readOnly = h.add('div', { contenteditable: 'false' }, editor);
  assert.equal(h.api.isEditableTarget(readOnly), false, 'an explicit read-only island is not an editor');
});

test('browser modifiers, composition and already-handled events keep native behavior', () => {
  const h = fixture({ mobile: false, globals: true });
  for (const options of [{ ctrlKey: true }, { metaKey: true }, { altKey: true }, { isComposing: true }, { keyCode: 229 }, { defaultPrevented: true }]) {
    for (const key of ['Enter', 'j', 'm', '1', '/', 'g', 'l']) {
      const event = h.key(key, h.document.body, options);
      assert.equal(event.defaultPrevented, !!options.defaultPrevented);
      assert.deepEqual(h.calls, []);
      assert.equal(h.state.cur, 0);
      assert.equal(h.context.location.hash, '#leads');
    }
  }
});

test('plain list navigation still works and a native control cancels a pending g shortcut', () => {
  const h = fixture({ mobile: false, globals: true });
  assert.equal(h.key('j', h.document.body).defaultPrevented, true);
  assert.equal(h.state.cur, 1);
  h.key('Enter', h.document.body);
  assert.deepEqual(h.calls, [['select', 1], ['openDetail', 8]]);
  h.calls.length = 0;
  h.key('g', h.document.body);
  h.key('m', h.opener);
  h.key('m', h.document.body);
  assert.equal(h.context.location.hash, '#leads');
  assert.deepEqual(h.calls, [['mark', 8, 'contacted']]);
  h.key('g', h.document.body);
  assert.equal(h.key('m', h.document.body).defaultPrevented, true);
  assert.equal(h.context.location.hash, '#map');
});

test('mobile details focus the heading and isolate siblings without masking their ancestor', () => {
  const h = fixture();
  const existingInert = h.add('section', { inert: '' });
  h.open();
  assert.equal(h.document.activeElement, h.title);
  assert.equal(h.panel.getAttribute('role'), 'dialog');
  assert.equal(h.panel.getAttribute('aria-modal'), 'true');
  assert.equal(h.panel.getAttribute('aria-labelledby'), 'd-person-title');
  assert.equal(h.list.inert, true);
  assert.equal(h.shell.inert, false);
  assert.equal(h.panel.inert, false);
  h.access.close();
  assert.equal(h.document.activeElement, h.opener);
  assert.equal(h.list.inert, false);
  assert.equal(existingInert.inert, true);
  assert.equal(h.panel.getAttribute('role'), 'complementary');
  assert.equal(h.panel.getAttribute('aria-label'), 'Lead');
  assert.equal(h.panel.hasAttribute('aria-modal'), false);
  assert.equal(h.panel.hasAttribute('aria-labelledby'), false);
});

test('mobile Tab wraps and excludes hidden, disabled and collapsed editor controls', () => {
  const h = fixture();
  const disclosure = h.add('details', {}, h.panel);
  const summary = h.add('summary', {}, disclosure);
  h.add('input', {}, disclosure);
  h.add('button', { hidden: '' }, h.panel);
  h.add('button', { disabled: '' }, h.panel);
  h.add('button', {}, h.add('fieldset', { disabled: '' }, h.panel));
  h.add('button', {}, h.add('div', { hidden: '' }, h.panel));
  h.add('button', {}, h.add('div', { inert: '' }, h.panel));
  h.add('button', { tabindex: '-1' }, h.panel);
  const invisible = h.add('button', {}, h.panel); invisible.style.visibility = 'hidden';
  const notDisplayed = h.add('button', {}, h.panel); notDisplayed.style.display = 'none';
  h.open();
  let event = { key: 'Tab', preventDefault() { this.prevented = true; } };
  assert.equal(h.access.handleKeydown(event), true, 'heading advances to the first control');
  assert.equal(h.document.activeElement, h.first);
  event = { key: 'Tab', shiftKey: true, preventDefault() { this.prevented = true; } };
  assert.equal(h.access.handleKeydown(event), true);
  assert.equal(h.document.activeElement, summary, 'the collapsed summary remains usable');
  event = { key: 'Tab', preventDefault() { this.prevented = true; } };
  assert.equal(h.access.handleKeydown(event), true);
  assert.equal(h.document.activeElement, h.first);
  assert.equal(h.access.handleKeydown({ key: 'Tab', preventDefault() { assert.fail('ordinary Tab should remain native'); } }), false);
  h.document.activeElement = h.opener;
  h.document.emit('focusin', { target: h.opener });
  assert.equal(h.document.activeElement, h.title, 'focus escaping the dialog returns inside');
});

test('an empty mobile detail retains focus, while desktop details never trap it', () => {
  const h = fixture();
  h.panel.replaceChildren();
  h.open();
  assert.equal(h.document.activeElement, h.panel);
  const event = { key: 'Tab', preventDefault() { this.prevented = true; } };
  assert.equal(h.access.handleKeydown(event), true);
  assert.equal(event.prevented, true);
  h.resize(false);
  assert.equal(h.access.isModal(), false);
  assert.equal(h.list.inert, false);
  assert.equal(h.access.handleKeydown({ key: 'Tab', preventDefault() { assert.fail('desktop Tab is native'); } }), false);
  h.opener.focus();
  assert.equal(h.document.activeElement, h.opener);
  h.resize(true);
  assert.equal(h.document.activeElement, h.panel);
});

test('closing details restores a replaced opener by ID, then falls back when it is gone', () => {
  for (const replace of [true, false]) {
    const h = fixture();
    h.open();
    h.opener.remove();
    const replacement = replace ? h.add('button', { id: 'open-lead' }, h.list) : null;
    h.panel.hidden = true;
    h.access.sync();
    assert.equal(h.document.activeElement, replacement || h.fallback);
    assert.equal(h.access.isModal(), false);
    assert.equal(h.list.inert, false);
  }
});

test('rerender restores the focused editor and caret only for the same person', () => {
  const h = fixture();
  h.open();
  h.last.focus(); h.last.setSelectionRange(2, 9, 'backward');
  const saved = h.access.capture();
  h.last.remove();
  const replacement = h.add('textarea', { id: 'note' }, h.panel);
  h.access.restore(saved);
  assert.equal(h.document.activeElement, replacement);
  assert.deepEqual([replacement.selectionStart, replacement.selectionEnd, replacement.selectionDirection], [2, 9, 'backward']);
  const previousPerson = h.access.capture();
  replacement.remove();
  h.panel.dataset.owner = '8';
  h.add('textarea', { id: 'note' }, h.panel);
  h.access.restore(previousPerson);
  assert.equal(h.document.activeElement, h.title, 'another person does not inherit the editor caret');
});

test('rerender restores an action by its value and a disclosure by its section', () => {
  const h = fixture(); h.open();
  for (const attribute of ['data-s', 'data-addtag', 'data-rmtag', 'data-seed', 'data-follow-action']) {
    const original = h.add('button', { [attribute]: 'chosen' }, h.panel);
    original.focus(); const saved = h.access.capture(); original.remove();
    h.add('button', { [attribute]: 'other' }, h.panel);
    const replacement = h.add('button', { [attribute]: 'chosen' }, h.panel);
    h.access.restore(saved);
    assert.equal(h.document.activeElement, replacement, attribute);
  }
  const details = h.add('details', { 'data-detail-section': 'profile' }, h.panel);
  const summary = h.add('summary', {}, details);
  summary.focus(); const saved = h.access.capture(); details.remove();
  const nextDetails = h.add('details', { 'data-detail-section': 'profile' }, h.panel);
  const nextSummary = h.add('summary', {}, nextDetails);
  h.access.restore(saved);
  assert.equal(h.document.activeElement, nextSummary);
  nextDetails.remove(); h.access.restore(saved);
  assert.equal(h.document.activeElement, h.title, 'a removed control falls back inside the dialog');
});

test('focus reveal opens every enclosing disclosure before focusing the requested editor', () => {
  const h = fixture({ mobile: false });
  const outer = h.add('details'), inner = h.add('details', {}, outer), input = h.add('input', {}, inner);
  assert.equal(h.api.revealAndFocus(input), true);
  assert.equal(outer.open, true); assert.equal(inner.open, true);
  assert.equal(h.document.activeElement, input);
  assert.equal(h.api.revealAndFocus(null), false);
});

test('mobile details block background shortcuts and Escape exits the editor before closing', () => {
  const h = fixture({ globals: true }); h.open();
  for (const key of ['j', 'Enter', 'm', '1', 'x', 'A', '/', '#', 'g', 'l', 't', 'c', 'v', '?', 'd']) {
    h.key(key, h.title);
    assert.deepEqual(h.calls, [], key);
    assert.equal(h.state.cur, 0);
  }
  h.last.focus();
  const firstEscape = h.key('Escape');
  assert.equal(firstEscape.stopped, true);
  assert.equal(h.state.open, 7);
  assert.equal(h.document.activeElement, h.title);
  h.state.pick.add(7);
  h.filters.classList.add('show');
  h.key('Escape');
  assert.equal(h.state.open, null);
  assert.equal(h.state.pick.has(7), true, 'closing a modal must preserve selection');
  assert.equal(h.filters.classList.contains('show'), true, 'the dialog closes before a background drawer');
  assert.equal(h.document.activeElement, h.opener);
  assert.deepEqual(h.calls, [['closeDetail']]);
});

test('Escape dismisses the active context one step at a time', () => {
  const h = fixture({ mobile: false, globals: true });
  h.state.open = 7; h.state.pick.add(7); h.filters.classList.add('show'); h.help.hidden = false;
  h.key('Escape', h.document.body);
  assert.equal(h.help.hidden, true); assert.deepEqual(h.calls, []); assert.equal(h.state.open, 7);
  h.fallback.focus(); h.key('Escape');
  assert.equal(h.document.activeElement, h.document.body); assert.deepEqual(h.calls, []);
  h.key('Escape', h.document.body);
  assert.deepEqual(h.calls, [['setDrawer', false]]); assert.equal(h.state.open, 7);
  h.key('Escape', h.document.body);
  assert.equal(h.state.open, null); assert.equal(h.state.pick.size, 1);
  h.key('Escape', h.document.body);
  assert.equal(h.state.pick.size, 0); assert.equal(h.state.cur, 0);
  h.key('Escape', h.document.body);
  assert.equal(h.state.cur, -1);
  h.state.view = 'map'; h.map.focus = { id: 7 }; h.state.pick.add(7);
  h.key('Escape', h.document.body);
  assert.equal(h.map.focus, null); assert.equal(h.state.pick.size, 1);
});

test('disposing focus management releases the page and removes focus and resize listeners', () => {
  const h = fixture(); h.open(); h.access.dispose();
  assert.equal(h.list.inert, false); assert.equal(h.access.isModal(), false);
  h.opener.focus(); h.resize(true);
  assert.equal(h.document.activeElement, h.opener);
  assert.equal(h.document.listeners.get('focusin').size, 0);
  assert.equal(h.window.listeners.get('resize').size, 0);
});
