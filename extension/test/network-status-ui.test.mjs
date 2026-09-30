import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';

test('popup and widget keep connection trouble visible after the retry timer expires', () => {
  const elements = new Map();
  const document = {getElementById(id) {if (!elements.has(id)) elements.set(id, {dataset:{}}); return elements.get(id);}};
  const popup = fs.readFileSync(new URL('../popup.js', import.meta.url), 'utf8');
  const ctx = vm.createContext({document, Date, Math});
  vm.runInContext(popup.slice(0, popup.indexOf('const showVer')), ctx);
  const view = {state:'network_wait', key:'wait', text:'Retrying the Instagram connection', nextAt:Date.now()-1000, job:'@example profile'};
  ctx.render(view);
  assert.equal(elements.get('state').textContent, 'Connection trouble');
  assert.match(elements.get('detail').textContent, /Retrying/);
  const widget = fs.readFileSync(new URL('../widget.js', import.meta.url), 'utf8');
  vm.runInContext(widget.slice(widget.indexOf('  function sentence('), widget.indexOf('  function render()')), ctx);
  assert.equal(ctx.sentence(view), 'Retrying the Instagram connection');
});
