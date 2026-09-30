import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import FL from '../lib/core.js';

test('heartbeat reports blocked tabs without a job and clears them after the page is usable', async () => {
  const data = {};
  let url = 'https://www.instagram.com/accounts/suspended/';
  const context = vm.createContext({ FL, Date, Set, URLSearchParams, AbortController, setTimeout, clearTimeout,
    importScripts() {}, chrome: {
      runtime: { getManifest: () => ({ version: '3.9.28' }), onMessage: { addListener() {} } },
      tabs: { query: async () => [{ id: 1, url, status: 'complete' }] },
      action: { setBadgeText() {}, setBadgeBackgroundColor() {} },
      storage: { local: { get: async key => ({ [key]: data[key] }), set: async values => Object.assign(data, values) } },
    } });
  const source = fs.readFileSync(new URL('../background.js', import.meta.url), 'utf8').split('// ---- lifecycle')[0];
  vm.runInContext(source, context);
  vm.runInContext(`selfUpdate = async () => false; whoami = async () => {};
    globalThis.beat = async () => {
      let sent;
      api = async (path, body) => { sent = body; return {status: 200, json: {paused: false}}; };
      await heartbeat(true);
      return sent;
    };`, context);
  assert.equal((await context.beat()).tab, 'tab_challenge');
  assert.match(data.view.text, /security check/);
  url = 'https://www.instagram.com/accounts/scraping_warning/';
  assert.equal((await context.beat()).tab, 'tab_scraping_warning');
  assert.match(data.view.text, /scraping warning/);
  url = 'https://www.instagram.com/accounts/login/';
  assert.equal((await context.beat()).tab, 'tab_login');
  url = 'https://www.instagram.com/';
  assert.equal((await context.beat()).tab, 'ok');
  assert.doesNotMatch(data.view.text, /security check|login page/);
});
