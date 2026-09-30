import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const appSource = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const helperSource = readFileSync(new URL('../../web/workflow.js', import.meta.url), 'utf8');
const start = appSource.indexOf('// Date-only follow-ups');
const end = appSource.indexOf('// ---------- keyboard ----------', start);
assert.ok(start >= 0 && end > start, 'the actual UI workflow block must be present');
const workflowSource = appSource.slice(start, end);
const clone = (value) => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const interactionIds = ['activity-kind', 'activity-body', 'activity-when', 'activity-status', 'activity-followup', 'activity-followup-date', 'activity-followup-note'];
const decode = (value = '') => value.replace(/&quot;/g, '"').replace(/&#39;|&#x27;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const escapeHTML = (value = '') => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');

function attributes(source) {
  const result = {};
  for (const match of source.matchAll(/([^\s=]+)(?:="([^"]*)")?/g)) result[match[1]] = decode(match[2] ?? '');
  return result;
}

function fixture(id) {
  return { id, status: 'contacted', follow_up: { due_on: '2026-09-27', note: `Check reply ${id}`, completed_at: null }, activity: { rows: [], next_cursor: null } };
}

// The VM runs the real renderer, listeners, save functions, and draft helpers.
// This small DOM double supplies only their form fields and event dispatch;
// each mount reads defaults from the actual generated HTML rather than copying them.
function createUI() {
  const server = new Map([[1, fixture(1)], [2, fixture(2)]]);
  const S = { person: null, open: null, rows: [...server.values()].map(clone) };
  const handlers = new Map(), nodes = new Map(), calls = [];
  const clock = { now: new Date(2026, 8, 26, 11, 15).getTime() };
  class TestDate extends Date {
    constructor(...args) { super(...(args.length ? args : [clock.now])); }
    static now() { return clock.now; }
  }
  const detail = {
    addEventListener(type, callback) {
      if (!handlers.has(type)) handlers.set(type, []);
      handlers.get(type).push(callback);
    },
  };
  function element(tagName, attrs) {
    const dataset = {};
    for (const [key, value] of Object.entries(attrs)) {
      if (key.startsWith('data-')) dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = value;
    }
    return {
      id: attrs.id, tagName: tagName.toUpperCase(), dataset, value: attrs.value || '',
      disabled: 'disabled' in attrs, required: 'required' in attrs, open: 'open' in attrs,
      hidden: 'hidden' in attrs, isConnected: true, textContent: '', innerHTML: '',
      setAttribute(key, value) { attrs[key] = String(value); },
      getAttribute(key) { return attrs[key] ?? null; },
      matches(selector) { return selector === 'details[data-workflow-disclosure]' && tagName === 'details' && 'data-workflow-disclosure' in attrs; },
      closest(selector) {
        if (selector === '.workflow-form') return nodes.get(this.id.startsWith('activity-') ? 'activity-form' : 'followup-form');
        const key = selector.match(/^\[([^\]]+)\]$/)?.[1];
        return key && key in attrs ? this : null;
      },
      focus() {},
    };
  }
  function mount() {
    for (const node of nodes.values()) node.isConnected = false;
    nodes.clear();
    const html = vm.runInContext('workflowSummaryHTML(S.person) + workflowHTML(S.person)', context);
    for (const [, tag, source] of html.matchAll(/<([a-z][a-z\d-]*)\b([^>]*)>/gi)) {
      const attrs = attributes(source);
      if (attrs.id) nodes.set(attrs.id, element(tag, attrs));
    }
    for (const [, source, contents] of html.matchAll(/<select\b([^>]*)>([\s\S]*?)<\/select>/g)) {
      const options = [...contents.matchAll(/<option\b([^>]*)>/g)].map((match) => attributes(match[1]));
      nodes.get(attributes(source).id).value = (options.find((option) => 'selected' in option) || options[0]).value;
    }
    for (const [, source, contents] of html.matchAll(/<textarea\b([^>]*)>([\s\S]*?)<\/textarea>/g)) {
      nodes.get(attributes(source).id).value = decode(contents);
    }
    vm.runInContext('wireWorkflow(S.person)', context);
  }
  const context = vm.createContext({
    S, Date: TestDate, URLSearchParams, setTimeout, clearTimeout,
    $: (selector) => selector === '#detail' ? detail : nodes.get(selector.slice(1)) || null,
    esc: escapeHTML, slabel: (value) => value,
    STATUSES: ['interested', 'contacted', 'talking', 'client', 'no'],
    api: {
      post(url, body) {
        let resolve, reject;
        const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
        calls.push({ url, body: clone(body), resolve, reject });
        return promise;
      },
      async get(url) {
        const id = Number(url.match(/\/person\/(\d+)/)[1]);
        return clone(url.includes('/activity') ? server.get(id).activity : server.get(id));
      },
    },
    M: { patch() {} },
    renderDetail() { vm.runInContext('rememberWorkflowForm()', context); mount(); },
    renderRows() {}, loadCounts() {}, loadFacetsSoon() {}, resetLeads() {},
  });
  context.noteQueue = {reconcile() {}};
  context.document = {hidden:false};
  const personRefresh = appSource.slice(appSource.indexOf('const personReads ='), appSource.indexOf('function closeDetail()'));
  vm.runInContext(appSource.slice(appSource.indexOf('function shortDate('), appSource.indexOf('function connectionEvidenceHTML(')) + helperSource + '\n' + personRefresh + '\n' + workflowSource, context);
  function emit(type, target) {
    const event = { target, submitter: nodes.get(target.id === 'activity-form' ? 'activity-save' : 'followup-save'), preventDefault() {} };
    return Promise.all((handlers.get(type) || []).map((handler) => handler(event)));
  }
  return {
    S, calls, clock,
    field: (id) => nodes.get(id),
    open(id) {
      vm.runInContext('rememberWorkflowForm()', context);
      S.person = clone(server.get(id)); S.open = id; mount();
    },
    render() { vm.runInContext('rememberWorkflowForm()', context); mount(); },
    edit(id, value) {
      assert.ok(nodes.has(id), `rendered field ${id} exists`);
      nodes.get(id).value = value;
      return emit('input', nodes.get(id));
    },
    submit: (id = 'activity-form') => emit('submit', nodes.get(id)),
    click: (id) => emit('click', nodes.get(id)),
    draft: (id) => clone(vm.runInContext(`workflowDrafts.get(${id})`, context)),
    busy: (id) => vm.runInContext(`workflowBusy(${id})`, context),
    values: () => Object.fromEntries(interactionIds.map((id) => [id, nodes.get(id).value])),
    resolve(index, patch = {}) {
      const call = calls[index], id = Number(call.url.match(/\/person\/(\d+)/)[1]);
      Object.assign(server.get(id), clone(patch));
      const response = { ...clone(patch), rows: server.get(id).activity.rows, next_cursor: null };
      call.resolve(response);
    },
    reject(index, message = 'Offline') { calls[index].reject(new Error(message)); },
  };
}

function fillInteraction(ui, overrides = {}) {
  const values = {
    'activity-kind': 'reply', 'activity-body': 'They asked for the portfolio',
    'activity-when': '2026-09-25T10:30', 'activity-status': 'talking',
    'activity-followup': 'complete_and_schedule', 'activity-followup-date': '2026-10-02',
    'activity-followup-note': 'Send selected projects', ...overrides,
  };
  for (const [id, value] of Object.entries(values)) ui.edit(id, value);
  return values;
}

test('a delayed reminder save retains newer edits after switching away and back', async () => {
  const ui = createUI(); ui.open(1);
  ui.edit('followup-date', '2026-09-28'); ui.edit('followup-note', 'Submitted reminder');
  const saved = ui.submit('followup-form');
  assert.equal(ui.calls.length, 1);
  ui.open(2); ui.edit('activity-body', 'Independent draft for person two');
  ui.open(1); ui.edit('followup-date', '2026-10-05'); ui.edit('followup-note', 'Newer reminder');
  ui.edit('activity-body', 'Unrelated interaction draft');
  ui.resolve(0, { follow_up: { due_on: '2026-09-28', note: 'Submitted reminder', completed_at: null } });
  await saved;
  assert.equal(ui.field('followup-date').value, '2026-10-05');
  assert.equal(ui.field('followup-note').value, 'Newer reminder');
  assert.equal(ui.field('activity-body').value, 'Unrelated interaction draft');
  assert.equal(ui.draft(2).body, 'Independent draft for person two');
  assert.equal(ui.S.person.follow_up.note, 'Submitted reminder');
  assert.equal(ui.busy(1), false);
});

test('a delayed interaction save retains the whole newer draft after switch and return, even with identical retyped text', async () => {
  const ui = createUI(); ui.open(1);
  const submitted = fillInteraction(ui), saved = ui.submit();
  const submittedVersion = ui.draft(1)._versions.body;
  ui.open(2); ui.edit('activity-body', 'Person two is separate');
  ui.open(1);
  ui.edit('activity-body', 'A different interaction');
  ui.edit('activity-body', submitted['activity-body']);
  assert.ok(ui.draft(1)._versions.body > submittedVersion);
  ui.resolve(0, { status: 'talking', follow_up: { due_on: '2026-10-02', note: 'Send selected projects', completed_at: null } });
  await saved;
  assert.deepEqual(ui.values(), submitted, 'newer edits retain their original kind, time, and optional changes');
  assert.equal(ui.draft(2).body, 'Person two is separate');
  assert.equal(ui.S.person.status, 'talking');
});

test('successful Clear reconciles the offscreen reminder draft without changing the visible person', async () => {
  const ui = createUI(); ui.open(1);
  ui.edit('activity-body', 'Keep this unrelated interaction');
  const saved = ui.click('followup-clear');
  ui.open(2); ui.edit('followup-note', 'Person two reminder draft');
  ui.resolve(0, { follow_up: null }); await saved;
  assert.equal(ui.S.person.id, 2);
  assert.equal(ui.field('followup-note').value, 'Person two reminder draft');
  assert.equal(ui.draft(1).due, undefined);
  assert.equal(ui.draft(1).action, undefined);
  ui.open(1);
  assert.equal(ui.field('followup-date').value, '');
  assert.equal(ui.field('followup-note').value, '');
  assert.equal(ui.field('activity-body').value, 'Keep this unrelated interaction');
});

test('Clear preserves an already edited reminder while its saved reminder is removed', async () => {
  const ui = createUI(); ui.open(1);
  ui.edit('followup-date', '2026-10-09'); ui.edit('followup-note', 'Unsaved replacement');
  const saved = ui.click('followup-clear');
  ui.open(2); ui.resolve(0, { follow_up: null }); await saved; ui.open(1);
  assert.equal(ui.S.person.follow_up, null);
  assert.equal(ui.field('followup-date').value, '2026-10-09');
  assert.equal(ui.field('followup-note').value, 'Unsaved replacement');
});

test('a per-person guard prevents duplicate and conflicting submissions after rerender but allows another person', async () => {
  const ui = createUI(); ui.open(1); fillInteraction(ui);
  const first = ui.submit(), oldButton = ui.field('activity-save');
  ui.render();
  assert.notEqual(ui.field('activity-save'), oldButton);
  assert.equal(oldButton.isConnected, false);
  assert.equal(ui.field('activity-save').disabled, true);
  assert.equal(ui.field('followup-save').disabled, true);
  await ui.submit(); await ui.submit('followup-form'); await ui.click('followup-clear');
  assert.equal(ui.calls.length, 1, 'calling handlers directly cannot bypass the busy guard');
  ui.open(2); fillInteraction(ui, { 'activity-body': 'Another person can save independently' });
  const second = ui.submit(); assert.equal(ui.calls.length, 2);
  assert.deepEqual(ui.calls.map((call) => call.url), ['/api/person/1/activity', '/api/person/2/activity']);
  ui.resolve(1); await second; assert.equal(ui.busy(1), true); assert.equal(ui.busy(2), false);
  ui.open(1); await ui.submit(); assert.equal(ui.calls.length, 2);
  ui.resolve(0); await first;
  assert.equal(ui.busy(1), false); assert.equal(ui.field('activity-save').disabled, false);
});

test('a failed atomic interaction keeps every option and retries the same complete request', async () => {
  const ui = createUI(); ui.open(1);
  const submitted = fillInteraction(ui), initialFollowUp = clone(ui.S.person.follow_up), saved = ui.submit();
  assert.deepEqual(ui.calls[0].body, {
    kind: 'reply', body: submitted['activity-body'], happened_at: new Date(submitted['activity-when']).toISOString(),
    status: 'talking', follow_up: { action: 'complete_and_schedule', due_on: '2026-10-02', note: 'Send selected projects' },
  });
  ui.render(); ui.reject(0, 'Atomic save rolled back'); await saved;
  assert.deepEqual(ui.values(), submitted);
  assert.equal(ui.S.person.status, 'contacted'); assert.deepEqual(ui.S.person.follow_up, initialFollowUp);
  assert.match(ui.field('activity-state').textContent, /Could not save: Atomic save rolled back/);
  assert.equal(ui.field('activity-editor').open, true); assert.equal(ui.busy(1), false);
  ui.open(2); ui.open(1); assert.deepEqual(ui.values(), submitted);
  const retry = ui.submit(); assert.equal(ui.calls.length, 2);
  assert.deepEqual(ui.calls[1].body, ui.calls[0].body);
  ui.resolve(1); await retry;
});

test('an unchanged successful interaction resets the submitted fields and time while retaining a separate reminder draft', async () => {
  const ui = createUI(); ui.open(1);
  fillInteraction(ui); ui.edit('followup-date', '2026-10-12'); ui.edit('followup-note', 'Separate unsaved reminder');
  const saved = ui.submit();
  ui.clock.now = new Date(2026, 8, 26, 15, 45).getTime();
  ui.resolve(0, { status: 'talking', follow_up: { due_on: '2026-10-02', note: 'Send selected projects', completed_at: null } });
  await saved;
  assert.deepEqual(ui.values(), {
    'activity-kind': 'dm', 'activity-body': '', 'activity-when': '2026-09-26T15:45', 'activity-status': '',
    'activity-followup': '', 'activity-followup-date': '', 'activity-followup-note': '',
  });
  assert.equal(ui.field('activity-editor').open, false); assert.equal(ui.field('activity-options').open, false);
  assert.equal(ui.field('followup-date').value, '2026-10-12'); assert.equal(ui.field('followup-note').value, 'Separate unsaved reminder');
  assert.equal(ui.draft(1).when, undefined); assert.equal(ui.draft(1).body, undefined);
  assert.equal(ui.S.person.status, 'talking');
  assert.match(ui.field('activity-state').textContent, /Interaction saved/);
});

test('a successful offscreen interaction is cleared before returning and receives a fresh timestamp', async () => {
  const ui = createUI(); ui.open(1); fillInteraction(ui);
  const saved = ui.submit(); ui.open(2);
  ui.edit('activity-body', 'Visible draft must remain');
  const visible = ui.values();
  ui.resolve(0, { status: 'talking', follow_up: null }); await saved;
  assert.deepEqual(ui.values(), visible);
  assert.equal(ui.draft(1).body, undefined); assert.equal(ui.draft(1).when, undefined);
  ui.clock.now = new Date(2026, 8, 27, 8, 10).getTime(); ui.open(1);
  assert.equal(ui.field('activity-body').value, ''); assert.equal(ui.field('activity-when').value, '2026-09-27T08:10');
  assert.equal(ui.field('activity-status').value, ''); assert.equal(ui.field('activity-followup').value, '');
  assert.equal(ui.field('followup-date').value, '');
  assert.equal(ui.draft(2).body, 'Visible draft must remain');
});
