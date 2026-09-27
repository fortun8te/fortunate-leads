import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
function harness(sc) {
  const nodes = new Map();
  const $ = selector => {
    if (!nodes.has(selector)) nodes.set(selector, { textContent: '', innerHTML: '', scrollIntoView() {}, querySelector() { return null; } });
    return nodes.get(selector);
  };
  const context = vm.createContext({ $, S: { sc }, location: {}, setView(view) { context.view = view; }, int: String, fmt: String, plural: (n, word) => n + ' ' + word, esc: String, eta: () => '1 h' });
  const start = source.indexOf('  renderProg() {');
  const end = source.indexOf('\n  card(r)', start);
  vm.runInContext('const Q = {' + source.slice(start, end) + '};', context);
  return { $, context, render() { vm.runInContext('Q.renderProg()', context); } };
}
test('qualification uses latest external setting over stale full progress', () => {
  const h = harness({ qualify: false, progress: { qualify: { on: true, left: 10 } } });
  h.render();
  assert.match(h.$('#ql-prog').innerHTML, /External AI is off/);
  assert.equal(h.$('#n-qual').textContent, '');
  h.context.S.scStale = true; h.render();
  assert.match(h.$('#ql-prog').innerHTML, /Checking status unavailable/);
});
test('qualification mode action navigates to settings without changing AI state', () => {
  const h = harness({ qualify: false, local_laya: true });
  const start = source.indexOf("$('#ql-toggle').onclick = () => {");
  const end = source.indexOf("$('#ql-list').addEventListener", start);
  vm.runInContext(source.slice(start, end), h.context);
  h.$('#ql-toggle').onclick();
  assert.equal(h.context.location.hash, '#/settings');
  assert.equal(h.context.view, 'settings');
  assert.equal(h.context.S.sc.qualify, false);
});
