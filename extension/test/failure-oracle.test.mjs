import test from 'node:test';
import assert from 'node:assert/strict';
import FL from '../lib/core.js';

test('three distinct follower home redirects pause only that viewer and success restores followers', () => {
  const now = Date.now();
  const brokenViewer = FL.normalize(null, now);
  const otherViewer = FL.normalize(null, now);
  for (const [i, handle] of ['first', 'second', 'third'].entries()) {
    FL.recordListRedirect(brokenViewer, handle, 'followers', true, now + i * 1000);
  }
  assert.equal(brokenViewer.listRedirects.length, 3);
  assert.ok(brokenViewer.listEndpointUntil > now + 2000);
  assert.equal(brokenViewer.cool.list.until, 0);
  assert.equal(otherViewer.listEndpointUntil, 0);
  assert.deepEqual(FL.plan(brokenViewer, {list: 1, profile: 1}, now + 2000).kinds, ['list', 'profile']);
  FL.listPageSucceeded(brokenViewer, 'following');
  assert.ok(brokenViewer.listEndpointUntil > now + 2000);
  FL.listPageSucceeded(brokenViewer, 'followers');
  assert.equal(brokenViewer.listEndpointUntil, 0);
  assert.deepEqual(brokenViewer.listRedirects, []);
});
