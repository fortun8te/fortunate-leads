import test from 'node:test';
import assert from 'node:assert/strict';
import FL from '../lib/core.js';

const T0 = Date.UTC(2026, 9, 4, 10, 0, 0), MIN = FL.MIN, HOUR = FL.HOUR;
const noCap = (st, t) => FL.budgetLeft(st, null, t);

test('default budget is no cap, however many requests were made today', () => {
  const st = FL.fresh();
  st.today.list = 1e6; st.today.profile = 1e6;
  assert.deepEqual(noCap(st, T0), { list: Infinity, profile: Infinity });
  assert.deepEqual(FL.budgetOf(null), { list: 0, profile: 0 });
});

test('with no budget the gap between pages still applies', () => {
  const st = FL.fresh();
  FL.afterRequest(st, 'list', T0, () => 0);
  const p = FL.plan(st, noCap(st, T0), T0);
  assert.equal(p.why, 'pace');
  assert.ok(p.wait >= FL.PACE.listGap[0] - 1);
});

test('with no budget the 11 minute window still stops requests', () => {
  const st = FL.fresh();
  for (let i = 0; i < FL.PACE.windowMax; i++) { st.rlog.push([T0 + i, 'list']); }
  st.nextAt = st.listNextAt = st.profileNextAt = 0;
  const p = FL.plan(st, noCap(st, T0 + 1000), T0 + 1000);
  assert.equal(p.why, 'window');
});

test('with no budget three limits in a day still force at least two hours of rest', () => {
  const st = FL.fresh();
  FL.applyHit(st, T0, null, 'list'); FL.applyHit(st, T0 + 20 * MIN, null, 'list'); FL.applyHit(st, T0 + 40 * MIN, null, 'list');
  const p = FL.plan(st, noCap(st, T0 + 41 * MIN), T0 + 41 * MIN, ['list']);
  assert.deepEqual(p.kinds, []);
  assert.ok(st.cool.list.until >= T0 + 40 * MIN + 2 * HOUR);
  assert.equal(p.why, 'cooldown');
});

test('a hold is not a budget question: statusOf reports it first', () => {
  const st = FL.fresh();
  st.hold = { code: 'challenge', message: 'Instagram security check' };
  assert.equal(FL.statusOf(st, {}, T0).key, 'off');
});
