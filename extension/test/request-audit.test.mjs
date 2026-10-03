import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import FL from '../lib/core.js';

const source = fs.readFileSync(new URL('../background.js', import.meta.url), 'utf8').split('// ---- lifecycle')[0];

function harness({result = {status: 200, sent: true, json: {}}, throwing = false, shortGrant = false} = {}) {
  const calls = [], data = {};
  const ctx = vm.createContext({FL, Date, Set, URLSearchParams, AbortController, importScripts() {},
    setTimeout: (fn, ms) => ms === 25000 ? 0 : setTimeout(fn, ms), clearTimeout,
    chrome: {runtime: {getManifest: () => ({version: '3.9.31'}), onMessage: {addListener() {}}},
      tabs: {}, storage: {local: {get: async key => ({[key]: data[key]}), set: async values => Object.assign(data, values)}}}});
  vm.runInContext(source, ctx);
  Object.assign(ctx, {note: value => calls.push(structuredClone(value)), fixture: result, throwing, shortGrant});
  vm.runInContext(`
    heartbeat = async () => { mem.offline = false; };
    api = async (path, body) => { note({path, body}); return {status: 200, json: {
      granted: true, token: 'ab'.repeat(24), expires_at: new Date(Date.now() + (shortGrant ? 20000 : 90000)).toISOString()}}; };
    inTab = async () => { if (throwing) throw new Error('transport uncertain'); return fixture; };
    checkedProfileDom = async () => ({url: 'private-url', text: 'SECRET_PAGE_TEXT'});
    chrome.tabs.create = async () => { waiters.alice({ig_id: '12', handle: 'alice'}); return {id: 7}; };
    chrome.tabs.remove = async () => {};
    mem.job = {id: 9, lease_token: 'lease-token'};
    globalThis.list = () => igRequest(mem.gen, {id: 1}, 'SECRET_URL', 'list');
    globalThis.lookup = kind => lookupViaPage(mem.gen, 'alice', kind, null);
    globalThis.rawRelease = data => releaseSharedRequest('ab'.repeat(24), data);
  `, ctx);
  return {ctx, calls, data, messages: () => calls.filter(call => call.path === '/api/ext/request').map(call => call.body)};
}

test('list page audit separates purpose and records actual HTTP response only', async () => {
  const h = harness({result: {status: 429, sent: true, json: {}, text: 'SECRET_BODY'}});
  await h.ctx.list();
  const [grant, release] = h.messages();
  assert.equal(grant.kind, 'list'); assert.equal(grant.purpose, 'list_page');
  assert.equal(release.sent, true); assert.equal(release.http_status, 429);
  assert.equal(JSON.stringify(h.messages()).includes('SECRET'), false);
});

test('seed lookup and profile view are browser operations with unknown HTTP counts', async () => {
  for (const [kind, purpose] of [['list', 'seed_lookup'], ['profile', 'profile_view']]) {
    const h = harness(); await h.ctx.lookup(kind);
    const [grant, release] = h.messages();
    assert.equal(grant.kind, kind); assert.equal(grant.purpose, purpose);
    assert.equal(release.sent, null); assert.equal(release.http_status, null);
    assert.equal(JSON.stringify(h.messages()).includes('SECRET'), false);
  }
});

test('transport exception is unknown and preserves existing release behavior', async () => {
  const h = harness({throwing: true});
  await assert.rejects(h.ctx.list(), /transport uncertain/);
  assert.equal(h.messages()[1].sent, null);
  assert.equal(h.messages()[1].http_status, null);
});

test('explicit no-fetch result and pre-action expired grant record not sent', async () => {
  const noFetch = harness({result: {status: 0, sent: false, tabError: true, auditSent: false}});
  await noFetch.ctx.list();
  assert.equal(noFetch.messages()[1].sent, false);
  const expired = harness({shortGrant: true});
  await assert.rejects(expired.ctx.list());
  assert.equal(expired.messages()[1].sent, false);
});

test('body read failure retains known HTTP status without changing fetch classification', async () => {
  const h = harness({result: {status: 0, sent: true, aborted: true, auditHttpStatus: 200}});
  await h.ctx.list();
  assert.equal(h.messages()[1].sent, true);
  assert.equal(h.messages()[1].http_status, 200);
});

test('in-flight timeout still keeps permit instead of fabricating release', async () => {
  const h = harness({result: {status: 0, sent: false, timedOut: true, tabError: true}});
  await h.ctx.list();
  assert.equal(h.messages().length, 1);
  assert.equal(h.data.lane.after, 'timeout');
});

test('release metadata allows no response bodies, URLs or arbitrary fields', async () => {
  const h = harness();
  await h.ctx.rawRelease({sent: 'yes', http_status: 0, body: 'SECRET_BODY', url: 'SECRET_URL'});
  assert.deepEqual(Object.keys(h.messages()[0]).sort(), ['action', 'http_status', 'sent', 'token']);
  assert.equal(h.messages()[0].sent, null);
  assert.equal(JSON.stringify(h.messages()).includes('SECRET'), false);
});
