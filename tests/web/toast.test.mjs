import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
function fixture() {
  const timers = new Map(); let next = 0;
  const toast = { hidden: true, dataset: {}, addEventListener() {}, querySelector: () => ({}), matches: () => false, contains: () => false,
    removeAttribute(name) { if (name === 'data-leaving') delete this.dataset.leaving; } };
  const context = vm.createContext({ $: () => toast, document: { activeElement: null }, icon: () => '', esc: String,
    setTimeout(fn) { timers.set(++next, fn); return next; }, clearTimeout(id) { timers.delete(id); } });
  vm.runInContext(source.slice(source.indexOf('let toastT,'), source.indexOf('function avatar(')), context);
  return { context, toast, timers };
}
test('a replacement toast remains visible when the previous message was fading away', () => {
  const h = fixture();
  vm.runInContext("toast('Saved'); hideToast();", h.context);
  assert.equal(h.toast.dataset.leaving, '');
  vm.runInContext("toast(\"Couldn't save. Try again.\");", h.context);
  assert.equal(h.toast.hidden, false);
  assert.equal(h.toast.dataset.leaving, undefined);
  assert.equal(h.toast.className, 'toast is-error');
  assert.equal(h.timers.size, 1, 'the old dismissal timer is cancelled');
});
