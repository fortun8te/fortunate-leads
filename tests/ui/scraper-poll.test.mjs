import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const between = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));

test('small scraper status refresh preserves detailed progress and lists', async () => {
  const full = { lists: [{ seed: 'example', state: 'running' }], progress: { bios: { left: 120 } }, paused: false };
  const status = { paused: true, ext: { online: true }, queue: { list: 2, profile: 5 } };
  const c = vm.createContext({
    S: { sc: full, view: 'leads' },
    api: { get: async (path) => {
      assert.equal(path, '/api/scraper/status');
      return status;
    } },
    renderStatus() {}, renderAccounts() {}, renderScraper() {}, renderSettings() {},
    Q: { renderProg() {} },
  });
  vm.runInContext(between('async function loadScraper()', "$('#pause-btn').onclick"), c);
  await c.loadScraperStatus();
  assert.equal(c.S.sc.paused, true);
  assert.equal(c.S.sc.lists, full.lists);
  assert.equal(c.S.sc.progress, full.progress);
  assert.equal(c.S.scStale, false);
});

test('hidden tabs make no scraper requests; detailed refresh is limited to relevant views', () => {
  const timers = [], listeners = {};
  const c = vm.createContext({
    document: { hidden: false, addEventListener: (name, fn) => { listeners[name] = fn; } },
    S: { view: 'leads' },
    SCRAPER_FULL_VIEWS: new Set(['scraper', 'accounts', 'settings', 'qual']),
    loadScraperStatus: () => c.status++, loadScraper: () => c.full++,
    setInterval: (fn, interval) => { timers.push([fn, interval]); },
    status: 0, full: 0,
  });
  vm.runInContext(between('setInterval(() => { if (!document.hidden) loadScraperStatus();',
    'setInterval(() => { if (!document.hidden) { loadCounts();'), c);
  assert.deepEqual(timers.map(([, interval]) => interval), [5000, 15000]);
  timers.forEach(([fn]) => fn());
  assert.equal(c.status, 1);
  assert.equal(c.full, 0);
  c.S.view = 'scraper';
  timers.forEach(([fn]) => fn());
  assert.equal(c.status, 2);
  assert.equal(c.full, 1);
  c.document.hidden = true;
  timers.forEach(([fn]) => fn());
  assert.equal(c.status, 2);
  assert.equal(c.full, 1);
  c.document.hidden = false;
  listeners.visibilitychange();
  assert.equal(c.full, 2);
});
