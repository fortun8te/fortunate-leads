import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const start = source.indexOf('function leadConnectionLines(');
const end = source.indexOf('function rowHTML(', start);
const context = vm.createContext({ esc: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;') });
vm.runInContext(source.slice(start, end), context);
const lines = value => Array.from(context.leadConnectionLines(value));

test('list directions distinguish followers, following and mutual observations', () => {
  assert.deepEqual(lines({connection_edges: [
    {seed:'alice',direction:'followers'}, {seed:'bob',direction:'following'},
    {seed:'mutual',direction:'followers'}, {seed:'mutual',direction:'following'},
  ]}), ['Follows @alice', '@bob follows them', 'They and @mutual follow each other']);
});
test('owner evidence takes precedence without inventing a path through another seed', () => {
  assert.deepEqual(lines({relationship_owner:'michael',connection_edges:[
    {seed:'alice',direction:'following'}, {seed:'michael',direction:'followers'},
  ]}), ['Follows you', '@alice follows them']);
  assert.doesNotMatch(context.rowConnectionHTML({connection_edges:[{seed:'alice',direction:'following'}]}), /away|introduction|knows|you/);
});
test('legacy source names do not imply direction and current empty evidence wins', () => {
  assert.deepEqual(lines({via:['alice','alice']}), ['Seen in @alice’s list']);
  assert.deepEqual(lines({connection_edges:[],via:['alice']}), []);
});
test('row stays short while its tooltip exposes every observed source safely', () => {
  const html = context.rowConnectionHTML({connection_edges:[{seed:'<alice',direction:'following'},{seed:'bob',direction:'followers'}]});
  assert.match(html, /@&lt;alice follows them \+1/);
  assert.match(html, /Saved follows: @&lt;alice follows them; Follows @bob/);
  assert.doesNotMatch(html, /<alice/);
});
