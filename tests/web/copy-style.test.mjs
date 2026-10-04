import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

// Guards docs/COPY.md against the slop this sweep removed. It reads source text, so it
// also covers strings that only render in rare states. Accounts and map files follow
// COPY.md under their own owners and are not listed here.
const FILES = ['web/index.html', 'web/controls.js', 'web/connections.js', 'web/k2-connection.js',
  'extension/popup.html', 'extension/popup.js', 'extension/widget.js', 'extension/manifest.json'];
const read = (file) => readFileSync(new URL('../../' + file, import.meta.url), 'utf8');
const stripComments = (text) => text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '').replace(/<!--[\s\S]*?-->/g, '');

const BANNED = [
  [/\b[Cc]ould not\b/, 'use "Couldn\'t" (COPY.md rule 5)'],
  [/\b[Pp]lease\b|\bSuccessfully\b|\bsuccessfully\b|\bOops\b|\bsimply\b|\bSimply\b/, 'filler words (COPY.md rule 4)'],
  [/—/, 'em dash'],
  [/[‘’“”]/, 'curly quote'],
  [/\bsafe pace\b|doesn't flag|do not flag|guaranteed/i, 'claim we cannot prove (COPY.md rule 16)'],
  [/>\s*(?:Qualification|Scraping)\s*</, 'use "Checking mode" or "Collection" (COPY.md terms)'],
];

for (const file of FILES) {
  test(`${file} follows the copy rules`, () => {
    const text = stripComments(read(file));
    for (const [pattern, why] of BANNED) {
      const hit = text.split('\n').find((line) => pattern.test(line));
      assert.equal(hit, undefined, `${why}: ${String(hit).trim().slice(0, 140)}`);
    }
  });
}

test('the help dialog lists only shortcuts that have handlers', () => {
  const html = read('web/index.html');
  const headings = [...html.slice(html.indexOf('id="help"')).matchAll(/<section><h3>([^<]+)<\/h3>/g)].map((m) => m[1]);
  assert.deepEqual(headings, ['Leads', 'Filter', 'Connections', 'Anywhere']);
});

test('the removed legacy settings sections stay removed', () => {
  const html = read('web/index.html');
  for (const id of ['set-keys', 'set-models', 'set-q-auto', 'set-free', 'set-limit-f', 'legacy-processing-settings'])
    assert.equal(html.includes(id), false, id);
});
