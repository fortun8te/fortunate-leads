import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import '../../web/workflow.js';

const { createNoteQueue, addLocalDays, reconcileWorkflowDraft } = globalThis.LeadWorkflow;
const tick = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
};

test('date shortcuts use calendar days and leave their source date unchanged', () => {
  const date = new Date(2024, 1, 28, 23, 30);
  const original = date.getTime();
  assert.equal(addLocalDays(1, date), '2024-02-29');
  assert.equal(addLocalDays(2, date), '2024-03-01');
  assert.equal(addLocalDays(-29, date), '2024-01-30');
  assert.equal(addLocalDays(0, date), '2024-02-28');
  assert.equal(addLocalDays(7, new Date(2026, 11, 28)), '2027-01-04');
  assert.equal(date.getTime(), original);
});

test('date shortcuts follow the local day through DST and far-from-UTC timezones', () => {
  const moduleURL = new URL('../../web/workflow.js', import.meta.url).href;
  for (const [timezone, values, expected] of [
    ['Europe/Amsterdam', [2026, 2, 28, 23, 30], '2026-03-29'],
    ['Europe/Amsterdam', [2026, 9, 25, 0, 30], '2026-10-26'],
    ['America/New_York', [2026, 2, 7, 23, 30], '2026-03-08'],
    ['America/New_York', [2026, 10, 1, 0, 30], '2026-11-02'],
    ['Pacific/Kiritimati', [2026, 8, 26, 0, 1], '2026-09-27'],
    ['Pacific/Pago_Pago', [2026, 8, 26, 23, 59], '2026-09-27'],
  ]) {
    const result = execFileSync(process.execPath, ['--input-type=module', '-e', `
      await import(${JSON.stringify(moduleURL)});
      console.log(LeadWorkflow.addLocalDays(1, new Date(...${JSON.stringify(values)})));
    `], { env: { ...process.env, TZ: timezone }, encoding: 'utf8' }).trim();
    assert.equal(result, expected, timezone);
  }
});

test('workflow acknowledgement clears only submitted unchanged fields without mutating inputs', () => {
  const current = { due: '2026-09-28', action: 'Send work', body: 'Next interaction', _versions: { due: 1, action: 2, body: 3 } };
  const submitted = structuredClone(current);
  const result = reconcileWorkflowDraft(current, submitted, ['due', 'action']);
  assert.deepEqual(result, { draft: { body: 'Next interaction', _versions: { body: 3 } }, unchanged: true });
  assert.deepEqual(current, submitted);
  assert.notEqual(result.draft, current);
  assert.notEqual(result.draft._versions, current._versions);
});

test('workflow acknowledgement preserves newer edits including retyping the submitted value', () => {
  const submitted = { kind: 'dm', body: 'Sent portfolio', when: '2026-09-26T12:00', _versions: { kind: 1, body: 1, when: 1 } };
  const current = { ...submitted, kind: 'call', _versions: { ...submitted._versions, kind: 2, body: 3 } };
  assert.deepEqual(reconcileWorkflowDraft(current, submitted, ['kind', 'body', 'when']), {
    draft: { kind: 'call', body: 'Sent portfolio', _versions: { kind: 2, body: 3 } }, unchanged: false,
  });
});

test('workflow draft acknowledgement supports stored drafts and complete cleanup', () => {
  const stored = JSON.parse(JSON.stringify({ body: 'Keep after navigation', _versions: { body: 5 } }));
  assert.deepEqual(reconcileWorkflowDraft(stored, structuredClone(stored), ['body']), { draft: {}, unchanged: true });
  assert.deepEqual(reconcileWorkflowDraft({ body: 'Legacy draft' }, { body: 'Legacy draft' }, ['body']), { draft: {}, unchanged: true });
  assert.deepEqual(reconcileWorkflowDraft(undefined, { body: 'Old request' }, ['body']), { draft: {}, unchanged: false });
});

test('a failed old note save still drains the newer draft for every waiting flush', async (t) => {
  const first = deferred(), calls = [], persisted = [];
  const queue = createNoteQueue({ delay: 100000, persist: (drafts) => persisted.push(drafts), save: async (id, note) => {
    calls.push([id, note]);
    if (calls.length === 1) await first.promise;
  } });
  t.after(() => clearTimeout(queue.peek(1)?.timer));
  queue.edit(1, 'Earlier');
  const firstFlush = queue.flush(1);
  await tick();
  queue.edit(1, 'Latest');
  const secondFlush = queue.flushAll();
  const result = Promise.allSettled([firstFlush, secondFlush]);
  first.reject(new Error('Lost connection on earlier save'));
  const statuses = (await result).map((entry) => entry.status);
  assert.deepEqual(statuses, ['fulfilled', 'fulfilled']);
  assert.deepEqual(calls, [[1, 'Earlier'], [1, 'Latest']]);
  assert.equal(queue.state(1).saved, 'Latest');
  assert.equal(queue.state(1).error, null);
  assert.equal(queue.dirty(), false);
  assert.deepEqual(persisted.at(-1), {});
});

test('reverting during an unacknowledged write still saves the intended original note', async (t) => {
  const first = deferred(), calls = [];
  let serverNote = 'Original';
  const queue = createNoteQueue({ delay: 100000, save: async (id, note) => {
    calls.push(note);
    serverNote = note;
    if (calls.length === 1) await first.promise;
  } });
  t.after(() => clearTimeout(queue.peek(1)?.timer));
  queue.state(1, 'Original');
  queue.edit(1, 'Intermediate');
  const done = queue.flush(1);
  await tick();
  queue.edit(1, 'Original');
  const result = Promise.allSettled([done]);
  first.reject(new Error('Response lost after server write'));
  assert.equal((await result)[0].status, 'fulfilled');
  assert.equal(serverNote, 'Original');
  assert.deepEqual(calls, ['Intermediate', 'Original']);
  assert.equal(queue.dirty(), false);
});

test('a failure of the current note is reported once and keeps its recoverable draft', async (t) => {
  const gate = deferred(), calls = [];
  const queue = createNoteQueue({ delay: 100000, save: async (id, note) => { calls.push([id, note]); await gate.promise; } });
  t.after(() => clearTimeout(queue.peek(1)?.timer));
  queue.edit(1, 'Keep this');
  const results = Promise.allSettled([queue.flush(1), queue.flush(1), queue.flushAll()]);
  gate.reject(new Error('Offline'));
  assert.deepEqual((await results).map((entry) => entry.status), ['rejected', 'rejected', 'rejected']);
  assert.deepEqual(calls, [[1, 'Keep this']]);
  assert.deepEqual(queue.snapshot(), { 1: 'Keep this' });
  assert.equal(queue.state(1).pending, null);
  assert.equal(queue.state(1).error.message, 'Offline');
  assert.equal(queue.dirty(), true);
});
