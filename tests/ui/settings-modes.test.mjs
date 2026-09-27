import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
function harness({ confirmed = true, failRead = false, reorder = false } = {}) {
  const nodes = new Map(), calls = [], messages = [];
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, { disabled: false, classList: { toggle() {} }, setAttribute() {}, querySelectorAll: () => [], addEventListener(name, fn) { this[name] = fn; } });
    return nodes.get(id);
  };
  const S = { sc: { qualify: false, local_laya: true } };
  const SET = { models: ['stealth/example', 'vendor/model:free'], dirty: true };
  const context = vm.createContext({ $, S, SET, confirm: () => confirmed, toast: value => messages.push(value), renderSettings() {}, renderScout() {}, renderModels() {}, ucf: s => s,
    api: { post: async (url, body) => { calls.push({ url, body }); if (url === '/api/llm/models') return { models: reorder ? [...body.models].reverse() : body.models }; if (url === '/api/settings/qualify') S.sc = { qualify: body.on, local_laya: body.local_laya }; return { on: false }; }, get: async () => { if (failRead) throw Error('offline'); return S.sc; } }
  });
  vm.runInContext(source.slice(source.indexOf('function settingsMode('), source.indexOf('// Leadscout:')), context);
  const start = source.indexOf("$('#set-models-save').onclick = async () => {");
  vm.runInContext(source.slice(start, source.indexOf("$('#view-settings').addEventListener", start)), context);
  return { S, SET, calls, messages, $, context, choose: mode => $('#set-mode').click({ target: { closest: () => ({ dataset: { mode }, disabled: false }) } }) };
}
test('local and rules selections switch off both external routes and automatic activation', async () => {
  for (const mode of ['local', 'rules']) {
    const h = harness(); h.S.sc = { qualify: true, local_laya: false };
    await h.choose(mode);
    assert.equal(h.calls[0].url, '/api/settings/scout'); assert.equal(h.calls[0].body.on, false);
    assert.deepEqual(JSON.parse(JSON.stringify(h.calls[1].body)), { on: false, auto: false, local_laya: mode === 'local' });
    assert.equal(h.messages.at(-1), 'Checking mode saved');
  }
});
test('external AI cannot start when explicit confirmation is declined', async () => {
  const h = harness({ confirmed: false }); await h.choose('external'); assert.equal(h.calls.length, 0);
});
test('readback failure does not claim success and disables uncertain controls', async () => {
  const h = harness({ failRead: true }); await h.choose('external');
  assert.equal(h.S.scStale, true); assert.doesNotMatch(h.messages.at(-1), /saved/);
  h.context.renderCheckingMode(); assert.match(h.$('#set-mode-status').textContent, /could not be confirmed/);
});
test('model selection preserves exact order without changing operating mode', async () => {
  const h = harness(); await h.$('#set-models-save').onclick();
  assert.equal(h.calls.length, 1); assert.equal(h.calls[0].url, '/api/llm/models');
  assert.deepEqual([...h.calls[0].body.models], ['stealth/example', 'vendor/model:free']);
  assert.equal(h.SET.dirty, false); assert.equal(h.S.sc.qualify, false);
});
test('unexpected saved order leaves changes available and reports failure', async () => {
  const h = harness({ reorder: true }); await h.$('#set-models-save').onclick();
  assert.equal(h.SET.dirty, true); assert.match(h.messages.at(-1), /Could not confirm/);
});
