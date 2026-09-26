import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const FL = require('../extension/lib/core.js');

test('private access requires matching profile URL, profile flag, and visible wall', () => {
  const info = { url: 'https://www.instagram.com/seed/', privateWall: true };
  assert.equal(FL.privateWall(info, 'seed', { is_private: true }), true);
  assert.equal(FL.privateWall(info, 'other', { is_private: true }), false);
  assert.equal(FL.privateWall({ ...info, privateWall: false }, 'seed', { is_private: true }), false);
  assert.equal(FL.privateWall(info, 'seed', { is_private: false }), false);
});

test('an empty JSON first page remains a soft block without private-wall proof', () => {
  const bad = FL.classify({ status: 200, json: { status: 'ok', users: [], has_more: false } },
                          'list', Date.now(), { cursor: null, total: 10, received: 0 });
  assert.equal(bad.code, 'soft_block');
  assert.equal(bad.reason, 'empty_first_page');
  assert.equal(FL.classify({ status: 429, json: {} }, 'list').code, 'rate_limit');
});
