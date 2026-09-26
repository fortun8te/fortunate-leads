import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import '../../web/daily.js';
import '../../web/workflow.js';

const daily = globalThis.LeadDaily;
const appSource = await readFile(new URL('../../web/app.js', import.meta.url), 'utf8');
const mockSource = await readFile(new URL('../../web/mock.js', import.meta.url), 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));

// Execute the production functions. Keep DOM setup out of these isolated tests.
function appPart(start, end) {
  const first = appSource.indexOf(start), last = appSource.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `App function boundaries exist: ${start}`);
  return appSource.slice(first, last);
}

function queryApp() {
  const nodes = new Map(), changes = [];
  const state = {};
  const select = (name) => {
    if (!nodes.has(name)) nodes.set(name, { value: '' });
    return nodes.get(name);
  };
  const context = vm.createContext({
    URLSearchParams, S: state, $: select,
    filtersChanged: () => changes.push({ query: select('#q').value, sort: select('#sort').value }),
  });
  vm.runInContext([
    appPart('const TIER_FIT =', 'const isFitTag ='),
    appPart('function toQuery(', 'const modeOf ='),
    appPart('function applyQuery(', 'async function saveView('),
  ].join('\n'), context);
  const functions = vm.runInContext('({ toQuery, fromQuery, applyQuery })', context);
  return { ...functions, state, select, changes };
}

test('opening Due follow-ups replaces every stale criterion through the real query/apply path', () => {
  const app = queryApp();
  const staleQuery = 'tags=Founder&any=Brand,Store&not=Agency&status=contacted&tier=hot&q=stale&min_lists=4&has_bio=0&seed=old_seed&followers_min=0&followers_max=999&follow_up=completed&sort=followers';
  Object.assign(app.state, app.fromQuery(staleQuery));
  const previousFilter = app.state.f;
  app.select('#q').value = 'stale';
  app.select('#sort').value = 'followers';

  app.applyQuery(daily.dueQuery());

  assert.deepEqual(plain(app.state.f), {
    tags: [], any: [], not: [], status: 'all', tier: '', q: '', min: 0,
    follow_up: 'due', bio: '', seed: '', fmin: null, fmax: null,
  });
  assert.equal(app.state.sort, 'follow_up');
  assert.notEqual(app.state.f, previousFilter);
  assert.equal(previousFilter.q, 'stale', 'opening the view does not mutate a previous filter snapshot');
  assert.deepEqual(app.changes, [{ query: '', sort: 'follow_up' }]);
  assert.equal(app.toQuery(app.state.f, app.state.sort).toString(), 'status=all&follow_up=due&sort=follow_up');

  const saved = app.fromQuery(`${app.toQuery(app.state.f, app.state.sort)}&today=2000-01-01`);
  assert.equal(app.toQuery(saved.f, saved.sort).toString(), daily.dueQuery());
  assert.equal(daily.isDueView(saved.f, saved.sort), true);
});

test('custom filters and other orderings never claim to be the complete daily view', () => {
  const app = queryApp();
  const due = app.fromQuery(daily.dueQuery());
  assert.match(daily.context(due.f, due.sort), /All statuses/);
  assert.match(daily.context(due.f, due.sort), /Oldest first/);
  assert.equal(daily.empty(due.f, due.sort).title, 'No follow-ups due');

  for (const [field, value] of [
    ['tags', ['Founder']], ['any', ['Brand']], ['not', ['Agency']],
    ['status', ''], ['status', 'no'], ['status', 'contacted'],
    ['follow_up', 'overdue'], ['follow_up', 'scheduled'], ['follow_up', 'completed'],
    ['tier', 'hot'], ['q', 'maya'], ['min', 2], ['bio', '0'], ['bio', '1'],
    ['seed', 'founders'], ['fmin', 0], ['fmax', 0],
  ]) {
    const narrowed = { ...due.f, [field]: value };
    assert.equal(daily.isDueView(narrowed, due.sort), false, `${field}=${value}`);
    assert.equal(daily.context(narrowed, due.sort), '', `${field} must not display global scope`);
    assert.equal(daily.empty(narrowed, due.sort), null, `${field} must not imply all due work is done`);
  }
  for (const sort of ['fit', 'followers', '', undefined]) {
    assert.equal(daily.isDueView(due.f, sort), false, `sort=${sort}`);
    assert.equal(daily.empty(due.f, sort), null);
  }
  assert.equal(daily.isDueView(null, 'follow_up'), false);
  assert.equal(daily.empty(undefined, 'follow_up'), null);
});

test('daily query includes scheduled work on rejected people, keeps their status, and sorts oldest first', async () => {
  const window = { fetch: () => { throw Error('Unexpected external request'); } };
  const context = vm.createContext({
    window, URL, URLSearchParams, Response, console,
    location: { origin: 'http://127.0.0.1:8777' },
    setTimeout: (callback) => callback(), setInterval: () => 0,
  });
  vm.runInContext(mockSource, context);
  const json = async (url, body) => {
    const response = await window.fetch(url, body === undefined ? {} : { method: 'POST', body: JSON.stringify(body) });
    assert.equal(response.ok, true, url);
    return response.json();
  };
  const fixtures = [
    { id: 3196, status: 'no', due: '2026-09-26' },
    { id: 3197, status: 'no', due: '2026-09-20' },
    { id: 3198, status: 'contacted', due: '2026-09-23' },
    { id: 3199, status: 'no', due: '2026-09-27' },
    { id: 3200, status: 'no', due: '2026-09-19', complete: true },
  ];
  for (const fixture of fixtures) {
    await json(`/api/person/${fixture.id}/mark`, { status: fixture.status });
    await json(`/api/person/${fixture.id}/follow-up`, { due_on: fixture.due, note: 'Send the requested examples' });
    if (fixture.complete) await json(`/api/person/${fixture.id}/follow-up`, { action: 'complete' });
  }
  const app = queryApp(), parsed = app.fromQuery(daily.dueQuery());
  const query = globalThis.LeadWorkflow.runtimeQuery(app.toQuery(parsed.f, parsed.sort), new Date(2026, 8, 26, 12));
  query.set('limit', '500');
  const due = await json(`/api/leads?${query}`);
  const ownIds = new Set(fixtures.map(({ id }) => id));
  const shown = due.rows.filter(({ id }) => ownIds.has(id));
  assert.deepEqual(shown.map(({ id }) => id), [3197, 3198, 3196]);
  assert.deepEqual(shown.map(({ status }) => status), ['no', 'contacted', 'no']);
  assert.equal(due.rows.every(({ follow_up }) => !follow_up.completed_at && follow_up.due_on <= '2026-09-26'), true);
  for (let index = 1; index < due.rows.length; index++) {
    assert.ok(due.rows[index - 1].follow_up.due_on <= due.rows[index].follow_up.due_on, 'oldest due date comes first');
  }

  query.delete('status');
  const open = await json(`/api/leads?${query}`);
  assert.deepEqual(open.rows.filter(({ id }) => ownIds.has(id)).map(({ id }) => id), [3198]);
  assert.equal((await json('/api/person/3197')).status, 'no', 'opening the view must not silently change a rejected status');
});

test('only an active reminder becomes a next action', () => {
  for (const person of [
    undefined, {}, { follow_up: null }, { follow_up: {} },
    { follow_up: { due_on: '2026-09-26', note: 'Already sent', completed_at: '2026-09-26T12:00:00Z' } },
  ]) {
    assert.equal(daily.rowAction(person, '2026-09-26'), null);
  }
  for (const note of ['', ' \n\t ', null, undefined, 42]) {
    const result = daily.rowAction({ follow_up: { due_on: '2026-09-26', note } }, '2026-09-26');
    assert.equal(result.text, 'Follow up');
    assert.equal(result.label, 'Follow up');
    assert.equal(result.dueLabel, 'Due today');
  }
});

test('action labels distinguish overdue, today and future across month and year boundaries', () => {
  for (const [dueOn, today, label, overdue] of [
    ['2026-09-25', '2026-09-26', 'Overdue · 2026-09-25', true],
    ['2026-09-26', '2026-09-26', 'Due today', false],
    ['2026-09-27', '2026-09-26', 'Due 2026-09-27', false],
    ['2026-12-31', '2027-01-01', 'Overdue · 2026-12-31', true],
    ['2027-01-01', '2026-12-31', 'Due 2027-01-01', false],
    ['2024-02-29', '2024-03-01', 'Overdue · 2024-02-29', true],
  ]) {
    const action = daily.rowAction({ follow_up: { due_on: dueOn, note: 'Send examples' } }, today);
    assert.equal(action.dueLabel, label);
    assert.equal(action.dueOn, dueOn);
    assert.equal(action.overdue, overdue);
  }
});

test('long plain-text actions remain intact and whitespace normalization leaves saved notes unchanged', () => {
  const note = ` \nSend\t the  portfolio & ask about "autumn" <examples> ${'詳細 '.repeat(125)}END\n `;
  const person = { follow_up: { due_on: '2026-09-26', note } };
  const original = structuredClone(person);
  const action = daily.rowAction(person, '2026-09-26');
  const expected = `Send the portfolio & ask about "autumn" <examples> ${'詳細 '.repeat(125)}END`;
  assert.equal(action.text, expected);
  assert.equal(action.label, `Next: ${expected}`);
  assert.ok(action.label.endsWith('END'), 'long actions are not truncated before rendering');
  assert.deepEqual(person, original);
});

test('default due labels follow the local calendar on both sides of UTC and through DST', () => {
  const moduleURL = new URL('../../web/daily.js', import.meta.url).href;
  for (const [timezone, instant, today, previous] of [
    ['Pacific/Kiritimati', '2026-09-25T10:05:00Z', '2026-09-26', '2026-09-25'],
    ['Pacific/Pago_Pago', '2026-09-27T10:55:00Z', '2026-09-26', '2026-09-25'],
    ['Europe/Amsterdam', '2026-03-28T23:30:00Z', '2026-03-29', '2026-03-28'],
    ['America/New_York', '2026-11-01T05:30:00Z', '2026-11-01', '2026-10-31'],
  ]) {
    const script = `
      const NativeDate = Date;
      globalThis.Date = class extends NativeDate {
        constructor(...args) { super(...(args.length ? args : [${JSON.stringify(instant)}])); }
      };
      await import(${JSON.stringify(moduleURL)});
      console.log(JSON.stringify([
        LeadDaily.rowAction({ follow_up: { due_on: ${JSON.stringify(today)} } }),
        LeadDaily.rowAction({ follow_up: { due_on: ${JSON.stringify(previous)} } })
      ]));
    `;
    const [due, overdue] = JSON.parse(execFileSync(process.execPath, ['--input-type=module', '-e', script], {
      env: { ...process.env, TZ: timezone }, encoding: 'utf8',
    }));
    assert.equal(due.dueLabel, 'Due today', timezone);
    assert.equal(due.overdue, false, timezone);
    assert.equal(overdue.overdue, true, timezone);
  }
});
