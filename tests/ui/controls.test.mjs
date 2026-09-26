import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../../web/controls.js', import.meta.url), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const state = (paused = false) => ({
  all_paused: false,
  stages: ['lists', 'bios', 'ai'].map(id => ({
    id, label: { lists: 'Collect lists', bios: 'Read bios', ai: 'AI scoring' }[id],
    paused: id === 'lists' && paused,
    state: id === 'lists' && paused ? 'paused' : 'running', now: 'Working', help: 'Stage help',
  })),
});

// A small DOM/fetch harness isolates response ordering without a browser or a running server.
function harness() {
  const requests = [], events = {}, listeners = {};
  let interval, html = '', buttons = [];
  const document = {
    body: {}, activeElement: null, readyState: 'complete', visibilityState: 'visible',
    createElement: () => el,
    addEventListener: (name, callback) => { listeners[name] = callback; },
  };
  document.activeElement = document.body;
  const el = {
    isConnected: true, dataset: {}, setAttribute() {},
    contains: node => buttons.includes(node),
    querySelectorAll: () => [],
    querySelector: selector => buttons.find(button => selector.includes(`"${button.dataset.stage}"`)) || null,
    addEventListener: (name, callback) => { events[name] = callback; },
    get innerHTML() { return html; },
    set innerHTML(value) {
      if (buttons.includes(document.activeElement)) document.activeElement = document.body;
      html = value;
      buttons = [...value.matchAll(/<button\b([^>]*)>/g)].map(([, attributes]) => ({
        dataset: { stage: /data-stage="([^"]+)"/.exec(attributes)[1], action: /data-action="([^"]+)"/.exec(attributes)[1] },
        disabled: /\bdisabled\b/.test(attributes),
        focus() { if (!this.disabled) document.activeElement = this; },
      }));
    },
  };
  vm.runInNewContext(source, {
    window: {}, document, Date, confirm: () => true,
    clearInterval() {}, setInterval(callback) { interval = callback; return 1; },
    fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); },
  });
  return {
    requests, el, document,
    respond(index, data = state(), ok = true) { requests[index].resolve({ ok, status: ok ? 200 : 500, json: async () => data }); },
    fail(index) { requests[index].reject(new Error('Network failure')); },
    poll() { for (let i = 0; i < 5; i++) interval(); },
    visible() { listeners.visibilitychange(); },
    button(stage) { return buttons.find(button => button.dataset.stage === stage); },
    click(stage = 'lists') { const button = this.button(stage); button.focus(); events.click({ target: { closest: () => button } }); },
  };
}
async function ready() { const h = harness(); h.respond(0); await settle(); return h; }

for (const outcome of ['success', 'failure']) {
  test(`a stale poll ${outcome} cannot overwrite a completed pause`, async () => {
    const h = await ready();
    h.poll(); // Request 1 predates the pause.
    h.click(); h.respond(2, state(true)); await settle();
    h.respond(3, state(true)); await settle();
    if (outcome === 'success') h.respond(1); else h.fail(1);
    await settle();
    assert.equal(h.button('lists').dataset.action, 'resume');
    assert.equal(h.button('bios').dataset.action, 'pause');
    assert.doesNotMatch(h.el.innerHTML, /Server offline/);
    assert.equal(h.document.activeElement, h.button('lists'));
  });
}

test('newer polling results win even when earlier reads finish last', async () => {
  const h = await ready(); h.poll(); h.poll();
  h.respond(2, state(true)); await settle(); h.respond(1); await settle();
  assert.equal(h.button('lists').dataset.action, 'resume');
});

test('controls stay locked through refresh and background reads are skipped', async () => {
  const h = await ready(); h.click();
  assert.deepEqual(JSON.parse(h.requests[1].options.body), { stage: 'lists', action: 'pause' });
  h.poll(); h.visible(); assert.equal(h.requests.length, 2);
  h.respond(1, state(true)); await settle();
  assert.equal(h.button('bios').disabled, true);
  h.click('bios'); h.poll(); h.visible(); assert.equal(h.requests.length, 3);
  h.respond(2, state(true)); await settle();
  assert.equal(h.button('bios').disabled, false);
  assert.equal(h.button('lists').dataset.action, 'resume');
});

for (const failure of ['HTTP', 'network']) {
  test(`${failure} action failure is announced, survives a healthy refresh, and clears on retry`, async () => {
    const h = await ready(); h.click();
    if (failure === 'HTTP') h.respond(1, { error: 'Failed' }, false); else h.fail(1);
    await settle(); h.respond(2); await settle();
    assert.match(h.el.innerHTML, /role="alert" aria-atomic="true"/);
    assert.match(h.el.innerHTML, /Couldn’t confirm pause for Lists\. Try again\./);
    assert.equal(h.button('lists').dataset.action, 'pause');
    assert.equal(h.button('lists').disabled, false);
    h.poll(); h.respond(3); await settle();
    assert.match(h.el.innerHTML, /Couldn’t confirm/);
    h.click(); assert.doesNotMatch(h.el.innerHTML, /Couldn’t confirm/);
    h.respond(4, state(true)); await settle(); h.respond(5, state(true)); await settle();
    assert.equal(h.button('lists').dataset.action, 'resume');
    assert.doesNotMatch(h.el.innerHTML, /role="alert"/);
  });
}

test('failed refresh leaves no request in flight and preserves the confirmed action state', async () => {
  const h = await ready(); h.click(); h.respond(1, state(true)); await settle();
  h.fail(2); await settle();
  // Offline: the buttons wait for the server instead of sending actions that cannot arrive.
  assert.equal(h.button('lists').disabled, true);
  assert.equal(h.button('lists').dataset.action, 'resume');
  assert.match(h.el.innerHTML, /Server offline: showing the last known state/);
});

test('completion does not steal focus when the user moves elsewhere', async () => {
  const h = await ready(); h.click();
  const elsewhere = {};
  h.document.activeElement = elsewhere;
  h.respond(1, state(true)); await settle(); h.respond(2, state(true)); await settle();
  assert.equal(h.document.activeElement, elsewhere);
});
