import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const start = source.indexOf('function edgeDay(');
const end = source.indexOf('const modelLabel =', start);
assert.ok(start >= 0 && end > start);
const context = vm.createContext({ esc: (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])) });
vm.runInContext(source.slice(start, end), context);
const render = (current, historical) => vm.runInContext(`connectionEvidenceHTML(${JSON.stringify(current)}, ${JSON.stringify(historical)})`, context);

test('expanded connections retain direction and the date of each current observation', () => {
  const html = render([
    { seed: 'fortun8te', direction: 'following', observed_at: '2026-09-16T12:00:00Z' },
    { seed: 'fortun8te', direction: 'followers', observed_at: '2026-09-25T12:00:00Z' },
  ], []);
  assert.match(html, /@fortun8te followed them\. <time datetime="2026-09-16">Seen Sep 16<\/time>/);
  assert.match(html, /They followed @fortun8te\. <time datetime="2026-09-25">Seen Sep 25<\/time>/);
});

test('earlier evidence remains inspectable with absence and unverified dates', () => {
  const html = render([], [
    { seed: 'oldseed', direction: 'followers', state: 'absent', checked_at: '2026-09-27T02:00:00Z', first_seen: '2026-09-20T02:00:00Z' },
    { seed: 'unknownseed', direction: 'following', state: 'unverified', first_seen: '2026-09-18T02:00:00Z' },
  ]);
  assert.match(html, /No recent list evidence/);
  assert.match(html, /Earlier observations/);
  assert.match(html, /Not found when checked Sep 27/);
  assert.match(html, /First seen Sep 20/);
  assert.match(html, /Previously seen Sep 18/);
  assert.match(html, /not reverified/);
});

test('connection names are escaped and missing dates are labeled as unavailable', () => {
  const html = render([{ seed: '<script>', direction: 'following' }], []);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /@&lt;script&gt;/);
  assert.match(html, /Seen date unavailable/);
});
