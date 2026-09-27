import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const FL = require('../extension/lib/core.js');

test('private access requires matching profile URL and two visible wall observations', () => {
  const info = { url: 'https://www.instagram.com/seed/', privateWall: true, privateWallRechecked: true };
  assert.equal(FL.privateWall(info, 'seed', { is_private: true }), true);
  assert.equal(FL.privateWall(info, 'other', { is_private: true }), false);
  assert.equal(FL.privateWall({ ...info, privateWall: false }, 'seed', { is_private: true }), false);
  assert.equal(FL.privateWall({ ...info, privateWallRechecked: false }, 'seed', { is_private: true }), false);
  assert.equal(FL.privateWall(info, 'seed', { is_private: false }), false);
});

test('an empty JSON first page remains a soft block without private-wall proof', () => {
  const bad = FL.classify({ status: 200, json: { status: 'ok', users: [], has_more: false } },
                          'list', Date.now(), { cursor: null, total: 10, received: 0 });
  assert.equal(bad.code, 'soft_block');
  assert.equal(bad.reason, 'empty_first_page');
  assert.equal(FL.classify({ status: 429, json: {} }, 'list').code, 'rate_limit');
});

test('HTML list response at Instagram home is a target retry signal, not a privacy verdict', () => {
  const bad = FL.classify({ status: 200, text: '<!doctype html><html></html>', json: null,
    contentType: 'text/html', url: 'https://www.instagram.com/', redirected: true }, 'list');
  assert.deepEqual([bad.code, bad.reason], ['other', 'list_html_home_redirect']);
  assert.equal(FL.classify({ status: 200, text: '<html>login link</html>', json: null,
    contentType: 'text/html', url: 'https://www.instagram.com/', redirected: true }, 'list').reason,
    'list_html_home_redirect');
  assert.equal(FL.classify({ status: 200, text: '<html></html>', json: null,
    contentType: 'text/html', url: 'https://www.instagram.com/' }, 'profile').reason, 'not_json');
  assert.equal(FL.classify({ status: 200, text: '<html>login</html>', json: null,
    contentType: 'text/html', url: 'https://www.instagram.com/', env: { uid: false } }, 'list').code, 'login');
});

test('a repeated live private wall can prove this viewer lacks access without profile JSON', () => {
  const info = { url: 'https://www.instagram.com/seed/', privateWall: true, privateWallRechecked: true };
  assert.equal(FL.privateWall(info, 'seed', null), true);
  assert.equal(FL.privateWall({ ...info, privateWallRechecked: false }, 'seed', null), false);
  assert.equal(FL.privateWall(info, 'other', null), false);
  assert.equal(FL.privateWall(info, 'seed', { is_private: false }), false);
});
