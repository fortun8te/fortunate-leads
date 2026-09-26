import test from 'node:test';
import assert from 'node:assert/strict';
import '../../web/refresh.js';

const { createRefreshCoordinator } = globalThis.LeadRefresh;
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const turn = () => new Promise((resolve) => setImmediate(resolve));
function setup(options = {}) {
  const state = { hidden: false, view: 'leads', followUp: 'due', loading: false, openId: null, personPending: false, personLoading: false };
  let date = new Date(2026, 8, 26, 23, 59);
  const calls = { lists: 0, people: [], days: [] };
  const coordinator = createRefreshCoordinator({
    getState: () => state,
    now: () => date,
    refreshLeads: () => { calls.lists++; },
    loadPerson: (id) => { calls.people.push(id); },
    onDayChange: (day) => { calls.days.push(day); },
    ...options,
  });
  return { coordinator, state, calls, date: (value) => { date = value; } };
}

test('local midnight refreshes an empty relative list once, even with a person open', async () => {
  const h = setup();
  h.state.rows = [];
  h.state.openId = 7;
  h.state.pick = new Set([7]);
  await h.coordinator.tick();
  assert.equal(h.calls.lists, 0);
  h.date(new Date(2026, 8, 27, 0, 1));
  await h.coordinator.tick();
  await h.coordinator.tick();
  assert.equal(h.calls.lists, 1);
  assert.deepEqual(h.calls.days, ['2026-09-27']);
  assert.deepEqual([...h.state.pick], [7], 'refresh does not change selection');
});

test('overdue lists refresh by the local date across month and year boundaries', async () => {
  const h = setup();
  h.state.followUp = 'overdue';
  h.date(new Date(2026, 11, 31, 23, 59));
  await h.coordinator.tick();
  h.date(new Date(2027, 0, 1, 0, 1));
  await h.coordinator.tick();
  assert.equal(h.calls.lists, 2);
  assert.deepEqual(h.calls.days, ['2026-12-31', '2027-01-01']);
});

test('rollover follows the local calendar even when the UTC date has not changed', async () => {
  let localDate = 26;
  const h = setup({ now: () => ({
    getFullYear: () => 2026, getMonth: () => 8, getDate: () => localDate,
    toISOString: () => '2026-09-26T22:01:00.000Z',
  }) });
  localDate = 27;
  await h.coordinator.tick();
  assert.equal(h.calls.lists, 1);
  assert.deepEqual(h.calls.days, ['2026-09-27']);
});

test('hidden tabs do no work and refresh both the pending profile and empty list on return', async () => {
  const h = setup();
  Object.assign(h.state, { hidden: true, openId: 7, personPending: true });
  h.date(new Date(2026, 8, 27));
  await h.coordinator.tick();
  await h.coordinator.visibilityChanged();
  assert.equal(h.calls.lists, 0);
  assert.deepEqual(h.calls.people, []);
  assert.deepEqual(h.calls.days, []);
  h.state.hidden = false;
  await h.coordinator.visibilityChanged();
  assert.equal(h.calls.lists, 1);
  assert.deepEqual(h.calls.people, [7]);
  assert.deepEqual(h.calls.days, ['2026-09-27']);
});

test('returning to a visible relative list refreshes it even on the same day', async () => {
  const h = setup();
  await h.coordinator.visibilityChanged();
  await h.coordinator.tick();
  assert.equal(h.calls.lists, 1);
  assert.deepEqual(h.calls.days, []);
});

test('rollover waits until the leads view is active and an existing list request completes', async () => {
  const h = setup();
  Object.assign(h.state, { view: 'map', loading: true });
  h.date(new Date(2026, 8, 27));
  await h.coordinator.tick();
  h.state.view = 'leads';
  await h.coordinator.tick();
  assert.equal(h.calls.lists, 0);
  h.state.loading = false;
  await h.coordinator.tick();
  assert.equal(h.calls.lists, 1);
});

test('date-independent lists only receive the day-change callback', async () => {
  for (const followUp of ['', 'scheduled', 'completed', 'none']) {
    const h = setup();
    h.state.followUp = followUp;
    h.date(new Date(2026, 8, 27));
    await h.coordinator.tick();
    assert.equal(h.calls.lists, 0);
    assert.deepEqual(h.calls.days, ['2026-09-27']);
  }
});

test('overlapping ticks share the list request and preserve another midnight during it', async () => {
  const gate = deferred();
  let lists = 0;
  const h = setup({ refreshLeads: () => { lists++; return lists === 1 ? gate.promise : undefined; } });
  h.date(new Date(2026, 8, 27));
  const first = h.coordinator.tick();
  const second = h.coordinator.tick();
  await turn();
  assert.equal(lists, 1);
  h.date(new Date(2026, 8, 28));
  const third = h.coordinator.tick();
  gate.resolve();
  await Promise.all([first, second, third]);
  await h.coordinator.tick();
  assert.equal(lists, 2);
});

test('failed list refreshes retry, including failures already handled by the app', async () => {
  let lists = 0;
  const h = setup({ refreshLeads: () => {
    lists++;
    if (lists === 1) throw new Error('offline');
    return lists === 2 ? false : true;
  } });
  h.date(new Date(2026, 8, 27));
  const failure = await h.coordinator.tick();
  assert.equal(failure.some((result) => result.status === 'rejected'), true);
  await h.coordinator.tick();
  await h.coordinator.tick();
  await h.coordinator.tick();
  assert.equal(lists, 3);
});

test('pending profile polls have one in-flight request and stop once the profile is ready', async () => {
  const gate = deferred();
  let requests = 0;
  const h = setup({ loadPerson: async () => { requests++; await gate.promise; h.state.personPending = false; } });
  Object.assign(h.state, { openId: 7, personPending: true });
  const first = h.coordinator.tick();
  const second = h.coordinator.tick();
  const visible = h.coordinator.visibilityChanged();
  await turn();
  assert.equal(requests, 1);
  gate.resolve();
  await Promise.all([first, second, visible]);
  await h.coordinator.tick();
  assert.equal(requests, 1);
});

test('initial detail loads and closed details are never polled', async () => {
  const h = setup();
  Object.assign(h.state, { openId: 7, personPending: true, personLoading: true });
  await h.coordinator.tick();
  Object.assign(h.state, { openId: null, personLoading: false });
  await h.coordinator.tick();
  assert.deepEqual(h.calls.people, []);
});

test('pending profile refresh retries after failure without an unhandled rejection', async () => {
  let requests = 0;
  const h = setup({ loadPerson: () => { requests++; if (requests === 1) throw new Error('offline'); } });
  Object.assign(h.state, { openId: 7, personPending: true });
  await h.coordinator.tick();
  await h.coordinator.tick();
  assert.equal(requests, 2);
});

test('an explicit refresh after saving waits for the old poll then reads fresh state', async () => {
  const first = deferred(), second = deferred();
  const requests = [];
  const h = setup({ loadPerson: (id) => { requests.push(id); return requests.length === 1 ? first.promise : second.promise; } });
  Object.assign(h.state, { openId: 7, personPending: true });
  const poll = h.coordinator.tick();
  await turn();
  const save = h.coordinator.refreshPerson(7);
  const anotherSave = h.coordinator.refreshPerson(7);
  assert.equal(save, anotherSave, 'changes saved before the queued fetch share that fetch');
  await turn();
  assert.deepEqual(requests, [7]);
  first.resolve();
  await turn();
  const anotherPoll = h.coordinator.tick();
  assert.deepEqual(requests, [7, 7]);
  second.resolve();
  await Promise.all([poll, save, anotherSave, anotherPoll]);
  assert.deepEqual(requests, [7, 7]);
});

test('polling shares an explicit detail load and changing people never reuses another profile request', async () => {
  const gate = deferred();
  const requests = [];
  const h = setup({ loadPerson: (id) => { requests.push(id); return gate.promise; } });
  Object.assign(h.state, { openId: 7, personPending: true });
  const manual = h.coordinator.refreshPerson(7);
  const first = h.coordinator.tick();
  h.state.openId = 8;
  const second = h.coordinator.tick();
  await turn();
  assert.deepEqual(requests, [7, 8]);
  gate.resolve();
  await Promise.all([manual, first, second]);
});

test('a queued explicit refresh remains shared before the first fetch starts', async () => {
  const gate = deferred();
  let requests = 0;
  const h = setup({ loadPerson: () => { requests++; return gate.promise; } });
  const first = h.coordinator.refreshPerson(7);
  const second = h.coordinator.refreshPerson(7);
  await turn();
  const third = h.coordinator.refreshPerson(7);
  assert.equal(second, third);
  gate.resolve();
  await Promise.all([first, second, third]);
  assert.equal(requests, 2);
});
