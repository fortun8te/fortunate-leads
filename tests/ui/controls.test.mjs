import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../../web/controls.js', import.meta.url), 'utf8');
const page = readFileSync(new URL('../../web/index.html', import.meta.url), 'utf8');
const style = readFileSync(new URL('../../web/app.css', import.meta.url), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const state = (paused = false, waitStage = null) => ({
  all_paused: false,
  local_laya: false,
  stages: ['lists', 'bios', 'ai'].map(id => ({
    id, label: { lists: 'Collect lists', bios: 'Read bios', ai: 'AI scoring' }[id],
    paused: id === 'lists' && paused,
    state: id === 'lists' && paused ? 'paused' : id === waitStage?.id ? 'waiting' : 'running',
    now: id === waitStage?.id ? waitStage.now : 'Working',
    wait: id === waitStage?.id ? waitStage.wait : null, help: 'Stage help',
  })),
});

// A small DOM/fetch harness isolates response ordering without a browser or a running server.
function harness({ detached = false } = {}) {
  const requests = [], events = {}, listeners = {};
  let interval, html = '', buttons = [];
  const slot = { appendChild(node) { node.isConnected = true; this.child = node; } };
  const document = {
    body: {}, activeElement: null, readyState: 'complete', visibilityState: 'visible',
    createElement: () => el,
    querySelector: selector => selector === '#stage-controls' ? slot : null,
    addEventListener: (name, callback) => { listeners[name] = callback; },
  };
  document.activeElement = document.body;
  const el = {
    isConnected: !detached, dataset: {}, setAttribute() {},
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
    window: { addEventListener() {} }, document, Date, confirm: () => true,
    clearInterval() {}, setInterval(callback) { interval = callback; return 1; },
    fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); },
  });
  return {
    requests, el, document, slot,
    respond(index, data = state(), ok = true) { requests[index].resolve({ ok, status: ok ? 200 : 500, json: async () => data }); },
    fail(index) { requests[index].reject(new Error('Network failure')); },
    poll() { for (let i = 0; i < 5; i++) interval(); },
    visible() { listeners.visibilitychange(); },
    button(stage) { return buttons.find(button => button.dataset.stage === stage); },
    click(stage = 'lists') { const button = this.button(stage); button.focus(); events.click({ target: { closest: () => button } }); },
  };
}
async function ready() { const h = harness(); h.respond(0); await settle(); return h; }

test('stage controls replace the removed legacy status controls', async () => {
  assert.match(page, /<div class="status"[^>]*>\s*<div id="stage-controls"><\/div>/);
  assert.doesNotMatch(page, /id="(?:st-act|st-lanes|pause-btn|set-q-on)"/);
  const h = harness({ detached: true });
  assert.equal(h.slot.child, h.el);
  h.respond(0); await settle();
  assert.equal((h.el.innerHTML.match(/class="fl-ctl-pill /g) || []).length, 3);
  assert.doesNotMatch(h.el.innerHTML, /fl-ctl-now/);
  assert.equal((h.requests.filter(request => request.url === '/api/control')).length, 1);
});

test('failed stage has a visible status and a link to details', async () => {
  const h = harness();
  const failed = state(); failed.stages[1].state = 'error';
  h.respond(0, failed); await settle();
  assert.match(h.el.innerHTML, /Read bios: needs attention/);
  assert.match(h.el.innerHTML, /role="alert" aria-atomic="true">A stage needs attention\. <a href="#\/scraper">View collection details\.<\/a>/);
});

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

test('wait pills distinguish Instagram limits, daily caps, and routine request gaps without echoing countdowns', async () => {
  const ig = await ready();
  ig.poll();
  ig.respond(1, state(false, { id: 'lists', now: 'Instagram asked us to slow down, back in 21.0 h.',
    wait: { why: 'Instagram asked us to slow down, resting', seconds: 21 * 3600 } }));
  await settle();
  assert.match(ig.el.innerHTML, /Instagram limit · back/);
  assert.doesNotMatch(ig.el.innerHTML, /back in 21\.0 h/);
  assert.doesNotMatch(ig.el.innerHTML, /fl-ctl-now[^>]*>Collect lists: Instagram asked us to slow down/);

  const cap = await ready(); cap.poll();
  cap.respond(1, state(false, { id: 'bios', now: 'Daily request budget reached, back in 3.0 h.',
    wait: { why: 'Daily request budget reached', seconds: 3 * 3600 } }));
  await settle();
  assert.match(cap.el.innerHTML, /daily cap · back/);
  assert.doesNotMatch(cap.el.innerHTML, /Instagram limit/);

  const gap = await ready(); gap.poll();
  gap.respond(1, state(false, { id: 'lists', now: 'Waiting between requests, back in 45 s.',
    wait: { why: 'Waiting between requests', seconds: 45 } }));
  await settle();
  assert.match(gap.el.innerHTML, /request gap · next in 45 s/);
  assert.doesNotMatch(gap.el.innerHTML, /Instagram limit/);
});


test('local Laya and external AI have separate states and external-only actions', async () => {
  const h = harness();
  const local = state();
  local.local_laya = true;
  local.stages[2].paused = true;
  local.stages[2].state = 'paused';
  h.respond(0, local); await settle();
  assert.match(h.el.innerHTML, /<span>Mode<\/span><b>Local<\/b>/);
  assert.equal((h.el.innerHTML.match(/class="fl-ctl-summary-item"/g) || []).length, 2);
  assert.match(h.el.innerHTML, /aria-label="Turn external AI on"/);
  assert.match(h.el.innerHTML, /<details class="fl-ctl-details">/);
  assert.equal(h.button('ai').dataset.action, 'resume');
  h.click('ai');
  assert.deepEqual(JSON.parse(h.requests[1].options.body), { stage: 'ai', action: 'resume' });
});

test('local setting-only changes refresh the summary, and offline states become unknown', async () => {
  const h = await ready();
  h.poll(); const local = state(); local.local_laya = true; local.stages[2].paused = true; local.stages[2].state = 'paused';
  h.respond(1, local); await settle();
  assert.match(h.el.innerHTML, /<span>Mode<\/span><b>Local<\/b>/);
  h.poll(); h.fail(2); await settle();
  assert.match(h.el.innerHTML, /<span>Collection<\/span><b>Unknown<\/b>/);
  assert.match(h.el.innerHTML, /<span>Mode<\/span><b>Unknown<\/b>/);
});


test('processing mode remains a configuration label when collection is paused', async () => {
  const h = harness();
  const local = state(); local.local_laya = true;
  local.stages.forEach(s => { s.paused = true; s.state = 'paused'; });
  h.respond(0, local); await settle();
  assert.match(h.el.innerHTML, /<span>Collection<\/span><b>Off<\/b>/);
  assert.match(h.el.innerHTML, /<span>Mode<\/span><b>Local<\/b>/);
  h.poll(); local.local_laya = false; h.respond(1, local); await settle();
  assert.match(h.el.innerHTML, /<span>Mode<\/span><b>Rules<\/b>/);
});
