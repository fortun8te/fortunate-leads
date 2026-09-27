import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
const bridge = fs.readFileSync(new URL('../bridge.js', import.meta.url), 'utf8');
const relay = fs.readFileSync(new URL('../relay.js', import.meta.url), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
function setup(body, finalUrl = 'https://www.instagram.com/api/graphql') {
  const messages = [], listeners = [], saved = {}; let calls = 0, now = 100000;
  class Clock extends Date { static now() { return now; } }
  const response = { ok: true, status: 200, url: finalUrl,
    clone: () => ({ text: async () => typeof body === 'string' ? body : JSON.stringify(body) }) };
  class XHR { open() {} send() {} addEventListener(name, callback) { this.listener = callback; } }
  const location = { origin: 'https://www.instagram.com', href: 'https://www.instagram.com/profile/' };
  const window = { fetch: async () => { calls++; return response; }, addEventListener: (name, cb) => listeners.push(cb),
    postMessage: (data) => { messages.push(data); for (const cb of listeners) cb({ source: window, origin: location.origin, data }); } };
  const context = { window, location, URL, URLSearchParams, FormData, Date: Clock, XMLHttpRequest: XHR, document: { addEventListener() {}, querySelectorAll: () => [] },
    chrome: { runtime: { sendMessage: async () => {} }, storage: { local: { get: async () => saved, set: async data => Object.assign(saved, data) } } } };
  vm.runInNewContext(bridge, context); vm.runInNewContext(relay, context);
  return { window, saved, messages, XHR, tick: () => { now += 1001; }, calls: () => calls };
}
test('native REST sample retains counts but strips all identities and secret values', async () => {
  const x = setup({ users: [{ pk: 'secret-user', username: 'private-name' }], next_max_id: 'secret-next', has_more: true });
  await x.window.fetch('https://www.instagram.com/api/v1/friendships/123/followers/?count=50&max_id=secret-cursor&access_token=secret-token');
  await flush(); await flush();
  const sample = x.saved.nativeListDiagnostics[0];
  assert.equal(x.calls(), 1); assert.equal(sample.returned_count, 1); assert.equal(sample.request_cursor, true);
  assert.equal(sample.has_more, true); assert.equal(sample.direction, 'followers');
  assert.equal(sample.requested_count, 50); assert.equal(sample.schema, 'rest_users');
  assert.doesNotMatch(JSON.stringify(sample), /secret|private-name|access_token|123/);
});
test('native GraphQL connection is recognized without reading POST body or headers', async () => {
  const x = setup({ data: { user: { edge_followed_by: { edges: [{ node: { username: 'hidden' } }], page_info: { has_next_page: true, end_cursor: 'opaque' } } } } });
  const options = { method: 'POST', get body() { throw new Error('must not read body'); }, get headers() { throw new Error('must not read headers'); } };
  await x.window.fetch('https://www.instagram.com/api/graphql?doc_id=123456&fb_api_req_friendly_name=PolarisProfileFollowersQuery', options);
  await flush(); await flush();
  const p = x.saved.nativeListDiagnostics[0];
  assert.equal(p.transport, 'graphql'); assert.equal(p.method, 'POST'); assert.equal(p.doc_id, '123456');
  assert.equal(p.operation, 'PolarisProfileFollowersQuery'); assert.equal(p.schema, 'edge_followed_by');
  assert.equal(p.response_cursor, true); assert.doesNotMatch(JSON.stringify(p), /hidden|opaque/);
});
test('homepage redirect is observed; extension own fetch remains unobserved', async () => {
  const x = setup('<html>secret HTML</html>', 'https://www.instagram.com/');
  await x.window.__flFetch('https://www.instagram.com/api/v1/friendships/123/followers/');
  await flush(); assert.equal(x.messages.length, 0);
  await x.window.fetch('https://www.instagram.com/api/v1/friendships/123/followers/');
  await flush(); await flush();
  assert.equal(x.calls(), 2); assert.equal(x.saved.nativeListDiagnostics[0].final_route, 'home');
  assert.equal(x.saved.nativeListDiagnostics[0].format, 'html');
  assert.doesNotMatch(JSON.stringify(x.saved), /secret/);
});
test('non-list GraphQL responses do not create diagnostic entries', async () => {
  const x = setup({ data: { user: { edge_followed_by: { count: 200 } } } });
  await x.window.fetch('https://www.instagram.com/api/graphql'); await flush();
  assert.equal(x.saved.nativeListDiagnostics, undefined);
});
test('native XHR list loading is observed without replacing request body', async () => {
  const x = setup({}); const xhr = new x.XHR();
  xhr.open('GET', 'https://www.instagram.com/api/v1/friendships/123/following/?count=25'); xhr.send();
  xhr.status = 200; xhr.responseURL = 'https://www.instagram.com/api/v1/friendships/123/following/';
  xhr.responseText = JSON.stringify({ users: [{ username: 'not-stored' }], has_more: false }); xhr.listener();
  await flush(); await flush(); assert.equal(x.saved.nativeListDiagnostics[0].direction, 'following');
  assert.equal(x.saved.nativeListDiagnostics[0].returned_count, 1);
});
test('rich native list responses preserve complete biography objects only', async () => {
  const x = setup({ users: [{ pk: '1', username: 'complete', biography: 'Founder' },
    { pk: '2', username: 'short' }, { pk: '3', biography: 'No handle' }] });
  await x.window.fetch('https://www.instagram.com/api/v1/friendships/123/followers/'); await flush();
  const profiles = x.messages.filter(m => m.__fl === 'profile');
  assert.equal(profiles.length, 1); assert.equal(profiles[0].user.username, 'complete');
  assert.equal(profiles[0].user.biography, 'Founder'); assert.equal(x.calls(), 1);
});
test('late complete JSON scripts are read with a fixed cap and no fragment merge', () => {
  const messages = []; let mutation; let disconnected = false;
  class Observer { constructor(callback) { mutation = callback; } observe() {} disconnect() { disconnected = true; } }
  class XHR { open() {} send() {} }
  const window = { fetch: async () => { throw new Error('must not fetch'); }, postMessage: m => messages.push(m), addEventListener() {} };
  const document = { addEventListener() {}, querySelectorAll: () => [] };
  vm.runInNewContext(bridge, { window, document, MutationObserver: Observer, XMLHttpRequest: XHR, URL,
    location: { href: 'https://www.instagram.com/', origin: 'https://www.instagram.com' } });
  const script = body => ({ textContent: JSON.stringify(body), matches: () => true });
  const send = s => mutation([{ type: 'childList', target: {}, addedNodes: [s] }]);
  send(script({ user: { pk: '1', username: 'late', biography: 'Designer' } }));
  send(script({ user: { pk: '2', username: 'split' }, biography: 'Do not merge' }));
  assert.equal(messages.length, 1); assert.equal(messages[0].user.username, 'late');
  for (let i = 0; i < 210; i++) send(script({ user: { pk: String(i + 10), username: 'late'+i, biography: 'Maker' } }));
  assert.equal(disconnected, true); assert.equal(messages.length, 199);
});

test('diagnostics retain only30 whitelisted local samples and never forward them to background', async () => {
  const x = setup({});
  for (let i = 0; i < 35; i++) {
    x.tick(); x.window.postMessage({ __fl: 'native-list-diagnostic', sample: {
      direction: 'followers', transport: 'rest', status: 200, returned_count: i,
      cookies: 'secret-cookie', query_keys: ['count', 'secret-token'], body: 'private-body' } });
  }
  await flush(); await flush();
  assert.equal(x.saved.nativeListDiagnostics.length, 30);
  assert.equal(x.saved.nativeListDiagnostics[0].returned_count, 5);
  assert.doesNotMatch(JSON.stringify(x.saved), /secret|private-body|cookies/);
});

test('materialized GraphQL bodies expose only public operation allowlist', async () => {
  for (const kind of ['string', 'params', 'form']) {
    const fields = new URLSearchParams({ doc_id: '98765', fb_api_req_friendly_name: 'PolarisProfileFollowersQuery',
      variables: 'secret-profile-variables', fb_dtsg: 'secret-token', access_token: 'secret-auth' });
    let body = kind === 'string' ? fields.toString() : fields;
    if (kind === 'form') { body = new FormData(); for (const [k, v] of fields) body.append(k, v); }
    const x = setup({ data: { user: { edge_followed_by: { edges: [], page_info: { has_next_page: false } } } } });
    await x.window.fetch('https://www.instagram.com/api/graphql', { method: 'POST', body });
    await flush(); await flush();
    const p = x.saved.nativeListDiagnostics[0];
    assert.equal(p.doc_id, '98765'); assert.equal(p.operation, 'PolarisProfileFollowersQuery');
    assert.doesNotMatch(JSON.stringify(p), /secret|variables|fb_dtsg|access_token/);
  }
});
test('Request body streams are never read or cloned', async () => {
  const x = setup({ data: { user: { edge_followed_by: { edges: [] } } } });
  const request = { url: 'https://www.instagram.com/api/graphql', method: 'POST',
    get body() { throw new Error('stream accessed'); }, clone() { throw new Error('stream cloned'); } };
  await x.window.fetch(request); await flush(); await flush();
  assert.equal(x.saved.nativeListDiagnostics[0].doc_id, null); assert.equal(x.calls(), 1);
});
