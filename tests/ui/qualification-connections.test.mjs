import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const start = source.indexOf('function qualificationConnections(');
const end = source.indexOf('const Q =', start);
const context = vm.createContext({ esc: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;') });
vm.runInContext(source.slice(start, end), context);
const show = r => context.qualificationConnections(r);

test('qualification translates observed list direction into plain follow relationships', () => {
  const html = show({ connection_edges: [
    { handle: 'fortun8te', direction: 'followers', is_me: true },
    { handle: 'fortun8te', direction: 'following', is_me: true },
    { handle: 'alice', direction: 'followers' },
    { handle: 'bob', direction: 'following' },
  ] });
  assert.match(html, /You \(@fortun8te\) follow each other/);
  assert.match(html, /They follow @alice/);
  assert.match(html, /@bob follows them/);
  assert.doesNotMatch(html, /Observed you following|In \d+ lists/);
});

test('qualification does not infer a follow from a source-only legacy list', () => {
  assert.match(show({ via: ['alice'] }), /Seen in @alice's list/);
  assert.match(show({ connection_edges: [], via: ['alice'] }), /No observed follows yet/);
  assert.match(show({ connection_edges: [{ handle: '<bad&', direction: 'followers' }] }), /@&lt;bad&amp;/);
});

test('review keeps every observed connection visible without a disclosure', () => {
  const html = show({connection_edges:['a','b','c','d'].map(handle=>({handle,direction:'followers'}))});
  assert.match(html, /They follow @d/);
  assert.doesNotMatch(html, /details|summary|Show.*more/);
});
