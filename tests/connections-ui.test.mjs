import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// Execute the shipped script without changing or exporting its private state.
// This deliberately small DOM supports the input/focus/click contract in index.html.
// It does not parse result HTML or claim to test layout, native keyboard defaults,
// assistive technology, network transport, or route/evidence rendering.
const source = readFileSync(new URL('../web/connections.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');

class Element {
  constructor(document, tagName = 'div', id = '') {
    Object.assign(this, {
      ownerDocument: document, tagName, id, parentElement: null,
      children: [], listeners: new Map(), attributes: new Map(),
      hidden: false, disabled: false, value: '', _html: '', _text: '',
      classList: { toggle() {} },
    });
  }
  addEventListener(name, listener) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(listener);
  }
  dispatch(name, options = {}) {
    const event = {
      type: name, target: this, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; }, ...options,
    };
    const targets = [this];
    if (name !== 'focus') {
      for (let parent = this.parentElement; parent; parent = parent.parentElement) targets.push(parent);
      if (targets.at(-1) !== this.ownerDocument) targets.push(this.ownerDocument);
    }
    for (const target of targets) {
      for (const listener of target.listeners.get(name) || []) listener.call(target, event);
    }
    return event;
  }
  focus() {
    if (this.ownerDocument.activeElement === this || this.disabled) return;
    this.ownerDocument.activeElement = this;
    this.dispatch('focus');
    this.dispatch('focusin');
  }
  click() {
    if (this.disabled) return;
    // Real mouse activation focuses the button before delivering its click.
    this.focus();
    this.dispatch('click');
  }
  append(child) { child.parentElement = this; this.children.push(child); }
  contains(other) {
    for (let node = other; node; node = node.parentElement) if (node === this) return true;
    return false;
  }
  replaceChildren(...children) {
    for (const child of this.children) child.parentElement = null;
    this.children = [];
    this._html = this._text = '';
    for (const child of children) this.append(child);
  }
  set innerHTML(value) { this.replaceChildren(); this._html = String(value); }
  get innerHTML() { return this._html; }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get textContent() { return this._text; }
  insertAdjacentHTML(position, value) {
    assert.equal(position, 'beforeend');
    this._html += value;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  querySelector(selector) {
    assert.equal(selector, 'button', 'Add actual DOM behavior before testing another selector');
    return this.children.find((child) => child.tagName === 'button') || null;
  }
}

function harness() {
  const document = { listeners: new Map(), activeElement: null };
  document.addEventListener = Element.prototype.addEventListener;
  const elements = new Map();
  const body = new Element(document, 'body');
  document.activeElement = body;
  document.createElement = (tag) => new Element(document, tag);
  document.getElementById = (id) => elements.get(id) || null;
  document.querySelector = (selector) => {
    assert.equal(selector, '#connection-panel');
    return document.getElementById('connection-panel');
  };
  const add = (name, tag = 'div', parent = body) => {
    const id = 'connection-' + name;
    assert.ok(html.includes(`id="${id}"`), `Fixture element ${id} exists in actual HTML`);
    const element = new Element(document, tag, id);
    elements.set(id, element);
    parent.append(element);
    return element;
  };
  const panel = add('panel');
  add('explore', 'button');
  add('overview', 'button');
  const form = add('form', 'form', panel);
  for (const which of ['from', 'to']) {
    // A suggestion button and its input share the real markup's field wrapper.
    const field = new Element(document);
    form.append(field);
    add(which, 'input', field);
    add(which + '-results', 'div', field).hidden = true;
  }
  add('mode', 'select', form).value = 'all';
  add('run', 'button', form).textContent = 'Find paths';
  add('status', 'div', panel).textContent = 'Choose a starting account and a target to begin.';
  add('results', 'div', panel);
  const el = (name) => elements.get('connection-' + name);

  const timers = new Map();
  let now = 0, nextTimer = 0;
  const requests = [];
  const context = {
    document, URLSearchParams,
    // These presentation helpers are not the behavior under test.
    esc: (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    int: (value) => String(value),
    setTimeout(fn, delay) { const id = ++nextTimer; timers.set(id, { fn, at: now + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    api: { get(url) {
      let resolve, reject;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
      requests.push({ url: new URL(url, 'http://fixture.invalid'), resolve, reject });
      return promise;
    } },
  };
  vm.runInNewContext(source, context, { filename: 'web/connections.js' });
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  const apiCalls = (kind) => requests.filter((request) => request.url.pathname === '/api/connections' + (kind === 'search' ? '/search' : ''));
  const type = (which, value) => { el(which).focus(); el(which).value = value; el(which).dispatch('input'); };
  return {
    el, document, requests, apiCalls, type, settle,
    async resolve(request, payload) { request.resolve(payload); await settle(); },
    async reject(request, error = new Error('offline')) { request.reject(error); await settle(); },
    async advance(ms) {
      const until = now + ms;
      while (true) {
        const due = [...timers].filter(([, timer]) => timer.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        now = due[1].at;
        timers.delete(due[0]);
        due[1].fn();
        await settle();
      }
      now = until;
    },
    submit() {
      const event = form.dispatch('submit');
      assert.equal(event.defaultPrevented, true);
      return apiCalls('paths').at(-1);
    },
    async choose(which, account) {
      el(which).focus();
      const request = apiCalls('search').at(-1);
      assert.equal(request.url.searchParams.get('q'), el(which).value.trim());
      await this.resolve(request, { accounts: [account] });
      assert.equal(el(which + '-results').hidden, false);
      const button = el(which + '-results').querySelector('button');
      assert.ok(button, 'The real script created a selectable account');
      button.click();
      assert.equal(el(which).value, '@' + account.handle);
    },
  };
}

const account = (id, handle) => ({ id, ig_id: id.slice(3), handle, identity_basis: 'instagram_id' });
const noPath = (marker) => ({
  source: account('ig:101', 'start'), target: account('ig:202', 'target'),
  paths: [], common_neighbors: {}, coverage: [{ seed: marker, direction: 'following', captured: 0, total: null, status: 'missing' }],
  search: { search_truncated: false },
});

test('the latest query wins even when an older search succeeds last', async () => {
  const h = harness();
  h.type('from', 'al');
  await h.advance(180);
  const old = h.apiCalls('search').at(-1);
  h.type('from', 'alex');
  await h.advance(180);
  const latest = h.apiCalls('search').at(-1);
  await h.resolve(latest, { accounts: [account('ig:2', 'alex_current')] });
  const result = h.el('from-results').children[0];
  assert.match(result.innerHTML, /alex_current/);
  await h.resolve(old, { accounts: [account('ig:1', 'al_obsolete')] });
  assert.equal(h.el('from-results').children[0], result);
  assert.equal(h.el('from-results').hidden, false);
});

test('editing invalidates search responses immediately, before the debounce fires', async () => {
  const h = harness();
  h.type('from', 'old');
  await h.advance(180);
  const old = h.apiCalls('search').at(-1);
  h.type('from', 'new');
  const status = h.el('status').textContent;
  await h.resolve(old, { accounts: [account('ig:1', 'old')] });
  assert.equal(h.el('from-results').hidden, true);
  assert.equal(h.el('from-results').children.length, 0);
  await h.advance(180);
  const latest = h.apiCalls('search').at(-1);
  await h.resolve(latest, { accounts: [account('ig:2', 'new')] });
  // The initial focus also issued a request. Its late failure must stay silent.
  await h.reject(h.apiCalls('search')[0]);
  assert.match(h.el('from-results').children[0].innerHTML, /@new/);
  assert.equal(h.el('from-results').hidden, false);
  assert.equal(h.el('status').textContent, status);
});

test('Escape and leaving the field cancel pending suggestions', async (t) => {
  for (const cancellation of ['escape', 'outside focus']) {
    for (const phase of ['request in flight', 'debounce queued']) {
      await t.test(cancellation + ': ' + phase, async () => {
        const h = harness();
        h.type('from', 'pending');
        if (phase === 'request in flight') await h.advance(180);
        const inflight = h.apiCalls('search').at(-1);
        if (cancellation === 'escape') h.el('from').dispatch('keydown', { key: 'Escape' });
        else h.el('mode').focus();
        const count = h.apiCalls('search').length;
        await h.resolve(inflight, { accounts: [account('ig:1', 'pending')] });
        await h.advance(1000);
        assert.equal(h.el('from-results').hidden, true);
        assert.equal(h.apiCalls('search').length, count, 'The queued debounce must also be cancelled');
      });
    }
  }
});

test('submitting before debounce uses current text and cannot reopen suggestions', async () => {
  const h = harness();
  h.el('to').value = ' @target ';
  h.type('from', ' @fresh ');
  const suggestions = [...h.apiCalls('search')];
  const query = h.submit();
  assert.equal(query.url.searchParams.get('from'), '@fresh');
  assert.equal(query.url.searchParams.get('to'), '@target');
  assert.equal(h.el('run').disabled, true);
  await h.advance(1000);
  for (const request of suggestions) await h.resolve(request, { accounts: [account('ig:1', 'stale')] });
  assert.equal(h.apiCalls('search').length, suggestions.length);
  assert.equal(h.el('from-results').hidden, true);
  assert.equal(h.el('to-results').hidden, true);
  assert.equal(h.el('run').disabled, true);
  await h.resolve(query, noPath('current_submission'));
  assert.equal(h.el('run').disabled, false);
  assert.match(h.el('results').innerHTML, /current_submission/);
});

test('selected account IDs survive refocusing and duplicate display handles', async () => {
  const h = harness();
  await h.choose('from', account('ig:101', 'same_name'));
  await h.choose('to', account('ig:202', 'same_name'));
  h.el('from').focus();
  await h.resolve(h.apiCalls('search').at(-1), { accounts: [account('ig:303', 'same_name')] });
  h.el('mode').value = 'following';
  h.el('mode').dispatch('change');
  const query = h.submit();
  assert.equal(query.url.searchParams.get('from'), 'ig:101');
  assert.equal(query.url.searchParams.get('to'), 'ig:202');
  assert.equal(query.url.searchParams.get('mode'), 'following');
});

test('editing one selected handle removes only that selection, including while a request is pending', async () => {
  const h = harness();
  await h.choose('from', account('ig:101', 'source'));
  await h.choose('to', account('ig:202', 'target'));
  const old = h.submit();
  assert.equal(h.el('run').disabled, true);
  h.type('from', '@someone_else');
  assert.equal(h.el('run').disabled, false);
  assert.equal(h.el('run').textContent, 'Find paths');
  assert.equal(h.el('results').innerHTML, '');
  const current = h.submit();
  assert.equal(current.url.searchParams.get('from'), '@someone_else');
  assert.equal(current.url.searchParams.get('to'), 'ig:202');
  await h.resolve(old, noPath('obsolete_selection'));
  assert.equal(h.el('results').innerHTML, '');
  assert.equal(h.el('run').disabled, true, 'An obsolete completion cannot enable the current submit');
  await h.resolve(current, noPath('updated_selection'));
  assert.match(h.el('results').innerHTML, /updated_selection/);
  assert.doesNotMatch(h.el('results').innerHTML, /obsolete_selection/);
  assert.equal(h.el('run').disabled, false);
});

test('changing direction invalidates pending paths and a late failure cannot overwrite the next result', async () => {
  const h = harness();
  h.el('from').value = '@source';
  h.el('to').value = '@target';
  const old = h.submit();
  h.el('mode').value = 'following';
  h.el('mode').dispatch('change');
  assert.equal(h.el('run').disabled, false);
  const current = h.submit();
  assert.equal(current.url.searchParams.get('mode'), 'following');
  await h.resolve(current, noPath('forward_only'));
  const status = h.el('status').textContent;
  const results = h.el('results').innerHTML;
  await h.reject(old, Object.assign(new Error('gone'), { status: 404 }));
  assert.equal(h.el('status').textContent, status);
  assert.equal(h.el('results').innerHTML, results);
  assert.equal(h.el('run').disabled, false);
});

test('an obsolete failure cannot clear the busy state of a newer request', async () => {
  const h = harness();
  h.el('from').value = '@source';
  h.el('to').value = '@target';
  const old = h.submit();
  h.type('to', '@replacement');
  const current = h.submit();
  const status = h.el('status').textContent;
  await h.reject(old);
  assert.equal(h.el('status').textContent, status);
  assert.equal(h.el('run').disabled, true);
  await h.resolve(current, noPath('replacement'));
  assert.equal(h.el('run').disabled, false);
});

test('path failures restore the form and a retry can succeed', async (t) => {
  for (const status of [404, 503]) {
    await t.test(String(status), async () => {
      const h = harness();
      h.el('from').value = '@source';
      h.el('to').value = '@target';
      await h.reject(h.submit(), Object.assign(new Error('temporary failure'), { status }));
      assert.equal(h.el('run').disabled, false);
      assert.equal(h.el('run').textContent, 'Find paths');
      assert.match(h.el('status').textContent, status === 404 ? /Account not found/ : /Could not find paths/);
      await h.resolve(h.submit(), noPath('recovered'));
      assert.match(h.el('results').innerHTML, /recovered/);
      assert.equal(h.el('status').textContent, 'No path found in the searched records within three steps.');
      assert.equal(h.el('run').disabled, false);
    });
  }
});

test('account search recovers after a failed request and the next selection submits its ID', async () => {
  const h = harness();
  h.el('from').focus();
  await h.reject(h.apiCalls('search').at(-1));
  assert.equal(h.el('from-results').hidden, true);
  assert.match(h.el('status').textContent, /Account search unavailable/);
  h.type('from', 'recovered');
  await h.advance(180);
  await h.resolve(h.apiCalls('search').at(-1), { accounts: [account('ig:909', 'recovered')] });
  assert.equal(h.el('from-results').hidden, false);
  h.el('from-results').children[0].click();
  h.el('to').value = '@target';
  const query = h.submit();
  assert.equal(query.url.searchParams.get('from'), 'ig:909');
  assert.doesNotMatch(h.el('status').textContent, /unavailable/);
  await h.resolve(query, noPath('search_recovered'));
  assert.match(h.el('results').innerHTML, /search_recovered/);
});
