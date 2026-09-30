import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../../web/index.html', import.meta.url), 'utf8');
const start = source.indexOf('function collectionReason(');
const end = source.indexOf('\nfunction renderAccounts()', start);
assert.ok(start >= 0 && end > start);
const snippet = source.slice(start, end);
const view = vm.runInNewContext(`${snippet}\n({ accountAccess, accountRow })`, {
  Date, ST_DOT: {}, ROLES: [['lists', 'Lists'], ['bios', 'Bios'], ['both', 'Both']],
  A: { confirm: null, renaming: null },
  esc: (x) => String(x ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
  ago: () => '2m', left: () => '8m', int: (x) => Number(x || 0).toLocaleString('en-US'),
  ucf: (x) => x[0].toUpperCase() + x.slice(1),
  jobText: () => 'Waiting for work', backIn: () => 'in 8 min',
});
const account = (changes = {}) => ({
  lane_id: 'lane-1', handle: 'sample', name: '@sample', label: null,
  role: 'lists', status: 'running', online: true, paused: false,
  last_seen: new Date().toISOString(), budget: { list: 0, profile: 0 },
  today: { list: 12, profile: 3 }, hour: { people: 60 }, ...changes,
});

test('account card leads with access and work, with counts and controls in account settings', () => {
  const html = view.accountRow(account());
  assert.match(html, /Connected[\s\S]*Collect/);
  assert.ok(html.indexOf('Account settings') > html.indexOf('Collect'));
  assert.ok(html.indexOf('Today · workspace caps') > html.indexOf('Account settings'));
  assert.match(html, /data-pause>Pause/);
  assert.match(html, /data-role="lists"/);
  assert.match(html, /data-bud/);
  assert.match(html, /data-remove/);
  assert.match(html, /No cap/);
  assert.doesNotMatch(html, /no limit|unlimited/i);
});

test('Accounts uses one concise summary instead of KPI tiles', () => {
  assert.match(html, /id="acc-summary"[^>]*aria-live="polite"/);
  assert.doesNotMatch(html, /id="acc-kpis"/);
  assert.match(source, /need attention/);
});

test('polling keeps each account Advanced state and focused control', () => {
  const document = { activeElement: null };
  const list = {
    rows: [],
    contains(node) { return this.rows.some((row) => row.controls.includes(node)); },
    querySelectorAll() { return this.rows; },
    set innerHTML(markup) {
      this.rows = [...markup.matchAll(/data-lane="([^"]+)"/g)].map((match) => {
        const row = { dataset: { lane: match[1] }, details: { open: false } };
        row.controls = ['SUMMARY', 'BUTTON'].map((tagName) => ({
          tagName,
          matches(selector) { return selector === 'button, summary'; },
          closest() { return row; },
          focus() { document.activeElement = this; },
        }));
        row.querySelector = () => row.details;
        row.querySelectorAll = () => row.controls;
        return row;
      });
    },
  };
  const elements = new Map([['#acc-list', list]]);
  const $ = (selector) => {
    if (!elements.has(selector)) elements.set(selector, { disabled: false, textContent: '', innerHTML: '' });
    return elements.get(selector);
  };
  const renderSource = source.slice(source.indexOf('function renderAccounts() {'), source.indexOf('\nasync function editAccount(', source.indexOf('function renderAccounts() {')));
  const context = vm.createContext({
    $, document, renderScraper(){}, syncSeed(){}, A: { starting: false, wiz: null, dismissed: false, renaming: null },
    S: { sc: { accounts: [{ lane_id: 'first', online: true }, { lane_id: 'second', online: true }], alerts: [], rate: { people_last_hour: 12 } }, scStale: false },
    accountRow: (account) => `<section data-lane="${account.lane_id}"></section>`,
    esc: String, int: String, accountAccess: view.accountAccess, collectionCoverageHTML: () => '',
  });
  vm.runInContext(`${renderSource}\nrenderAccounts()`, context);
  list.rows[0].details.open = true;
  list.rows[0].controls[1].focus();
  vm.runInContext('renderAccounts()', context);
  assert.equal(list.rows[0].details.open, true);
  assert.equal(list.rows[1].details.open, false);
  assert.equal(document.activeElement, list.rows[0].controls[1]);
  list.rows[0].controls[0].focus();
  vm.runInContext('renderAccounts()', context);
  assert.equal(list.rows[0].details.open, true);
  assert.equal(document.activeElement, list.rows[0].controls[0]);
});

test('navigation uses a settings gear and removes the keyboard help button while keeping the shortcut', () => {
  assert.match(html, /data-view="settings"[^>]*aria-label="Settings"><svg[^>]*><path/);
  assert.doesNotMatch(html, /id="help-btn"/);
  assert.match(source, /if \(k === '\?'\) \{ e\.preventDefault\(\); setHelp\(true\); return; \}/);
});

test('Accounts Start local models opens the configured startup path and reports its progress', () => {
  assert.match(html, /id="acc-start">Start local models/);
  assert.match(html, /id="acc-start-status"[^>]*role="status"/);

  assert.match(source, /A\.starting \|\| !sc \|\| !!S\.scStale/);
  assert.match(source, /api\.post\('\/api\/engine\/start', \{\}\)/);
  assert.match(source, /Laya is ready\. The installed notes model was checked\. Scraping and checking mode stay as selected/);
});

test('cooldowns, login holds and offline profiles have distinct access instructions', () => {
  assert.match(view.accountRow(account({
    status: 'cooldown', cooldown_until: new Date(Date.now() + 8 * 60000).toISOString(),
    list_endpoint_until: new Date(Date.now() + 8 * 60000).toISOString(),
  })), /Follower lists paused/);
  const overlapping = view.accountRow(account({
    status: 'cooldown', cooldown_until: new Date(Date.now() + 2 * 60 * 60000).toISOString(),
    list_endpoint_until: new Date(Date.now() + 30 * 60000).toISOString(),
  }));
  assert.match(overlapping, /Instagram also requested a wait[\s\S]*when that wait ends/);
  assert.doesNotMatch(overlapping, /retry 8m/);
  assert.match(view.accountRow(account({
    status: 'cooldown', cooldown_until: new Date(Date.now() + 8 * 60000).toISOString(),
  })), /Instagram limit active[\s\S]*Resumes in 8 min/);
  assert.match(view.accountRow(account({ status: 'needs_login', hold: 'login' })), /Open this Chrome profile and sign in/);
  assert.match(view.accountRow(account({ status: 'offline', online: false })), /Offline/);
  assert.doesNotMatch(view.accountRow(account({ status: 'running' })), /private source|private list/i);
});


test('account collection mode stays visible and a shared wait replaces ready text', () => {
  const html = view.accountRow(account({status:'online', collection_wait:'Instagram wait until 22:08'}));
  assert.match(html, /Waiting for Instagram to allow requests/);
  assert.doesNotMatch(html, /Ready for collection/);
  assert.ok(html.indexOf('data-role="lists"') < html.indexOf('Account settings'));
});

 test('a shared Instagram rest is shown as a wait, not a manual pause', () => {
 const row = view.accountRow(account({collection_wait:'Instagram collection is resting'}));
 assert.match(row, /Waiting for Instagram to allow requests/);
 assert.doesNotMatch(row, /Scraping paused/);
 });
