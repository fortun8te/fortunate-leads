import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const start = source.indexOf('function scoutHTML(');
const end = source.indexOf('function websiteEvidence(', start);
const context = vm.createContext({esc: (s) => String(s ?? '').replaceAll('<', '&lt;'), safeUrl: (s) => /^https?:\/\//.test(s) ? s : null, URL});
vm.runInContext(source.slice(start, end), context);
test('owner override makes old research historical while preserving the cited source', () => {
  const html = context.scoutHTML({overridden_by_owner:true, verdict:'no', reachable:false, summary:'Earlier assessment', sources:['https://example.com/about']});
  assert.match(html, /Earlier research/);
  assert.match(html, /relationship update takes priority/);
  assert.match(html, /Earlier assessment/);
  assert.match(html, /href="https:\/\/example.com\/about"/);
  assert.doesNotMatch(html, /not reachable|Not a lead/);
});
