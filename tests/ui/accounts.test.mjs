import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../../web/index.html', import.meta.url), 'utf8');
const start = source.indexOf('function accountAccess(a) {');
const end = source.indexOf('\nfunction renderAccounts()', start);
assert.ok(start >= 0 && end > start);
const snippet = source.slice(start, end);
const view = vm.runInNewContext(`${snippet}\n({ accountAccess, accountRow })`, {
  Date, ST_DOT: {}, ROLES: [['lists', 'Lists'], ['bios', 'Bios'], ['both', 'Both']],
  A: { confirm: null, renaming: null },
  esc: (x) => String(x ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
  ago: () => '2m', left: () => '8m', int: (x) => Number(x || 0).toLocaleString('en-US'),
  jobText: () => 'Waiting for work',
});
const account = (changes = {}) => ({
  lane_id: 'lane-1', handle: 'sample', name: '@sample', label: null,
  role: 'lists', status: 'running', online: true, paused: false,
  last_seen: new Date().toISOString(), budget: { list: 0, profile: 0 },
  today: { list: 12, profile: 3 }, hour: { people: 60 }, ...changes,
});

test('account card separates workspace budgets from Instagram access', () => {
  const html = view.accountRow(account());
  assert.match(html, /Instagram access[\s\S]*Connected/);
  assert.match(html, /Today · workspace caps[\s\S]*Lists/);
  assert.match(html, /No cap/);
  assert.doesNotMatch(html, /no limit|unlimited/i);
});

test('navigation uses a settings gear and removes the keyboard help button while keeping the shortcut', () => {
  assert.match(html, /data-view="settings"[^>]*aria-label="Settings"><svg[^>]*><path/);
  assert.doesNotMatch(html, /id="help-btn"/);
  assert.match(source, /if \(k === '\?'\) \{ e\.preventDefault\(\); setHelp\(true\); return; \}/);
});

test('cooldowns, login holds and offline profiles have distinct access instructions', () => {
  assert.match(view.accountRow(account({
    status: 'cooldown', cooldown_until: new Date(Date.now() + 8 * 60000).toISOString(),
    list_endpoint_until: new Date(Date.now() + 8 * 60000).toISOString(),
  })), /List API unavailable/);
  const overlapping = view.accountRow(account({
    status: 'cooldown', cooldown_until: new Date(Date.now() + 2 * 60 * 60000).toISOString(),
    list_endpoint_until: new Date(Date.now() + 30 * 60000).toISOString(),
  }));
  assert.match(overlapping, /Instagram also requested a wait[\s\S]*later retry time/);
  assert.doesNotMatch(overlapping, /retry 8m/);
  assert.match(view.accountRow(account({
    status: 'cooldown', cooldown_until: new Date(Date.now() + 8 * 60000).toISOString(),
  })), /Instagram limit active[\s\S]*See the top status bar for the wait time/);
  assert.match(view.accountRow(account({ status: 'needs_login', hold: 'login' })), /Open this Chrome profile and sign in/);
  assert.match(view.accountRow(account({ status: 'offline', online: false })), /Offline/);
  assert.doesNotMatch(view.accountRow(account({ status: 'running' })), /private source|private list/i);
});
