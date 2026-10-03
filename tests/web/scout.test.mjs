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

test('unverified research cannot turn unknown reachability into a negative claim', async () => {
  const source=await readFile(new URL('../../web/app.js',import.meta.url),'utf8');
  const render=source.slice(source.indexOf('function scoutHTML(sc) {'),source.indexOf('function websiteEvidence(site) {'));
  const context=vm.createContext({URL,esc:s=>String(s).replaceAll('<','&lt;'),safeUrl:s=>s});
  for(const reachable of [false,null,undefined]) {
    const html=vm.runInContext(`${render}\nscoutHTML(${JSON.stringify({verified:false,reachable,verdict:'possible',verification_reason:'No checked source supports this claim.'})})`,context);
    assert.doesNotMatch(html,/not reachable/);assert.match(html,/No checked source supports this claim/);
  }
  const current=vm.runInContext(`${render}\nscoutHTML(${JSON.stringify({verified:true,reachable:false,verdict:'no'})})`,context);
  assert.match(current,/not reachable/);
});
