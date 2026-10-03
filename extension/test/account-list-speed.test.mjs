import test from 'node:test';
import assert from 'node:assert/strict';
import FL from '../lib/core.js';

const NOW = Date.UTC(2026, 9, 3, 19, 0);
const selectedId = '64189916997';
function state(id) {
  const st = FL.fresh();
  st.accountIgId = id;
  st.breakEvery = 50;
  return st;
}
function withoutIdentity(st) {
  const { accountIgId, ...rest } = st;
  return rest;
}

test('only the exact verified string identity receives the shorter list gap', () => {
  for (const random of [0, 0.5, 1]) {
    const selected = state(selectedId);
    FL.afterRequest(selected, 'list', NOW, () => random);
    assert.equal(selected.listNextAt - NOW, Math.round((7000 + 5000 * random) * 0.65));
    for (const identity of ['55966675376', '25764895364', undefined, null, 64189916997,
                            '64189916997 ', '064189916997', 'other']) {
      const ordinary = state(identity);
      FL.afterRequest(ordinary, 'list', NOW, () => random);
      assert.equal(ordinary.listNextAt - NOW, Math.round(7000 + 5000 * random));
      assert.equal(ordinary.nextAt, selected.nextAt);
      assert.equal(ordinary.today.list, selected.today.list);
      assert.deepEqual(ordinary.rlog, selected.rlog);
    }
  }
});

test('periodic list break length and next break interval remain unchanged', () => {
  for (const random of [0, 0.5, 1]) {
    const selected = state(selectedId), ordinary = state('other');
    selected.pages = ordinary.pages = 49;
    FL.afterRequest(selected, 'list', NOW, () => random);
    FL.afterRequest(ordinary, 'list', NOW, () => random);
    const breakMs = Math.round(90000 + 90000 * random);
    assert.equal(selected.listNextAt - NOW, Math.round((7000 + 5000 * random) * 0.65) + breakMs);
    assert.equal(ordinary.listNextAt - NOW, Math.round(7000 + 5000 * random) + breakMs);
    assert.equal(selected.pages, 0);
    assert.equal(selected.breakEvery, ordinary.breakEvery);
  }
});

test('profile request timing and counters are identical for both identities', () => {
  const selected = state(selectedId), ordinary = state('other');
  FL.afterRequest(selected, 'profile', NOW, () => 0.5);
  FL.afterRequest(ordinary, 'profile', NOW, () => 0.5);
  assert.deepEqual(withoutIdentity(selected), withoutIdentity(ordinary));
  assert.equal(selected.profileNextAt - NOW, 52500);
});

test('rolling request window still blocks the selected account at its existing cap', () => {
  const st = state(selectedId);
  st.rlog = Array.from({ length: 71 }, () => [NOW - 1000, 'list']);
  FL.afterRequest(st, 'list', NOW, () => 0);
  assert.equal(FL.windowOf(st, NOW).n, 72);
  const plan = FL.plan(st, { list: Infinity, profile: Infinity }, NOW + 8000);
  assert.equal(plan.why, 'window');
  assert.deepEqual(plan.kinds, []);
  assert.equal(FL.PACE.window, 11 * FL.MIN);
  assert.equal(FL.PACE.windowMax, 72);
});

test('cooldowns, warning state and backoffs survive the account list-gap change', () => {
  const selected = state(selectedId), ordinary = state('other');
  for (const st of [selected, ordinary]) {
    FL.applyHit(st, NOW, NOW + FL.HOUR, 'list');
    st.hold = { code: 'scraping_warning', at: NOW };
    FL.afterRequest(st, 'list', NOW, () => 0.5);
    FL.backoff(st, NOW, 'net');
  }
  assert.deepEqual(selected.cool, ordinary.cool);
  assert.deepEqual(selected.hold, ordinary.hold);
  assert.deepEqual(selected.streak, ordinary.streak);
  assert.equal(selected.nextAt, ordinary.nextAt);
  assert.equal(FL.plan(selected, { list: Infinity, profile: 0 }, NOW + 10000).why, 'cooldown');
});

test('normalization preserves already stored pacing waits without accelerating them', () => {
  const st = state(selectedId);
  st.nextAt = NOW + 90000;
  st.listNextAt = NOW + 180000;
  st.profileNextAt = NOW + 240000;
  const restored = FL.normalize(st, NOW);
  assert.equal(restored.nextAt, NOW + 90000);
  assert.equal(restored.listNextAt, NOW + 180000);
  assert.equal(restored.profileNextAt, NOW + 240000);
  assert.equal(FL.readyAt(restored, 'list'), NOW + 180000);
});
