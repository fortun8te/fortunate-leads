import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { igUrl, copyText, copyLink } = require('../../web/map-links.js');

test('profile links use the handle only and refuse anything else', () => {
  assert.equal(igUrl('@some.brand_1'), 'https://www.instagram.com/some.brand_1/');
  assert.equal(igUrl('  name '), 'https://www.instagram.com/name/');
  for (const bad of ['', null, 'a b', 'x/y', 'javascript:alert(1)', 'a'.repeat(31)]) assert.equal(igUrl(bad), null, String(bad));
});

test('copy uses the clipboard and reports failure instead of throwing', async () => {
  let copied;
  assert.equal(await copyText('x', { clipboard: { writeText: async (t) => { copied = t; } } }), true);
  assert.equal(copied, 'x');
  const doc = { createElement: () => ({ setAttribute() {}, style: {}, select() {}, remove() {} }), body: { append() {} }, execCommand: () => true };
  assert.equal(await copyText('x', { clipboard: { writeText: async () => { throw new Error('denied'); } } }, doc), true, 'falls back to the older copy command');
  assert.equal(await copyText('x', {}, { ...doc, execCommand: () => { throw new Error('no'); } }), false);
});

test('Copy link copies the profile URL and the button says Copied, then goes back', async () => {
  let copied; const said = [];
  globalThis.navigator ??= undefined;
  const button = { textContent: 'Copy link', dataset: {} };
  const realCopy = globalThis.navigator;
  Object.defineProperty(globalThis, 'navigator', { value: { clipboard: { writeText: async (t) => { copied = t; } } }, configurable: true });
  try {
    assert.equal(await copyLink('@brand', button, (m) => said.push(m)), true);
  } finally { Object.defineProperty(globalThis, 'navigator', { value: realCopy, configurable: true }); }
  assert.equal(copied, 'https://www.instagram.com/brand/');
  assert.equal(button.textContent, 'Copied'); assert.deepEqual(said, ['Copied']);
  clearTimeout(button._t);
  assert.equal(await copyLink('not a handle', button), false, 'a person without a usable handle copies nothing');
});
