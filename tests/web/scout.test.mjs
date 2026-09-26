import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

test('older Leadscout research is visible without a current strong-lead claim', async () => {
  const source = await readFile(new URL('../../web/app.js', import.meta.url), 'utf8');
  const render = source.slice(source.indexOf('function scoutHTML(sc) {'), source.indexOf('function websiteEvidence(site) {'));
  const scout = { stale: true, verdict: 'strong', reachable: true, summary: 'Earlier skincare read.',
                  sources: ['https://example.com/about'] };
  const context = vm.createContext({ URL, esc: (s) => String(s), safeUrl: (s) => s });
  const html = vm.runInContext(`${render}\nscoutHTML(${JSON.stringify(scout)})`, context);
  assert.match(html, /Older read · unverified/);
  assert.match(html, /needs a new review/);
  assert.match(html, /Earlier skincare read/);
  assert.doesNotMatch(html, /Strong lead/);
  const current = vm.runInContext(`${render}\nscoutHTML(${JSON.stringify({ ...scout, stale: false })})`, context);
  assert.match(current, /Strong lead/);
});
