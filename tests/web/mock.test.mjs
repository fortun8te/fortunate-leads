import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

test('demo supports follow-ups, history, profile refresh', async () => {
  const window = { fetch: () => { throw Error('Unexpected external request'); } };
  const context = vm.createContext({ window, URL, URLSearchParams, Response, console, location: { origin: 'http://127.0.0.1:8777' }, setTimeout: (fn) => fn(), setInterval: () => 0 });
  vm.runInContext(await readFile(new URL('../../web/mock.js', import.meta.url), 'utf8'), context);
  const request = (url, body) => window.fetch(url, body ? { method: 'POST', body: JSON.stringify(body) } : {});
  const json = async (url, body) => (await request(url, body)).json();
  await json('/api/person/20/mark', { status: 'contacted' });
  await json('/api/person/20/mark', { note: 'Keep the status when editing notes' });
  await json('/api/person/20/follow-up', { due_on: '2026-09-26', note: 'Send portfolio' });
  let person = await json('/api/person/20');
  assert.equal(person.status, 'contacted');
  assert.equal(person.follow_up.due_on, '2026-09-26');
  assert.equal(person.activity.rows[0].after_value.due_on, '2026-09-26');
  const due = await json('/api/leads?follow_up=due&today=2026-09-26&limit=500');
  assert.ok(due.rows.some((p) => p.id === 20));
  await json('/api/person/20/activity', { kind: 'reply', body: 'Asked for examples' });
  await json('/api/person/20/follow-up', { action: 'complete' });
  await json('/api/person/20/read', {});
  person = await json('/api/person/20');
  assert.ok(person.follow_up.completed_at);
  assert.ok(person.profile_read_pending);
  assert.ok(person.activity.rows.some((a) => a.body === 'Asked for examples'));
  await json('/api/person/20/follow-up', { action: 'clear' });
  assert.equal((await json('/api/person/20')).follow_up, null);
  const converted = await json('/api/person/20/tags', { add: ['Client', 'Friend'] });
  assert.equal(converted.converted_to_status, 'client');
  person = await json('/api/person/20');
  assert.equal(person.status, 'client');
  assert.equal(person.note, 'Keep the status when editing notes');
  assert.ok(!person.tags.some((t) => t.tag.toLowerCase() === 'client'));
  for (const [path, body] of [['/api/people/bulk', {ids:[20]}], ['/api/leads/export', {ids:[20]}], ['/api/views', undefined]]) {
    assert.equal((await request(path, body)).status, 404);
  }

});
