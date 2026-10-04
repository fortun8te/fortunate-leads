import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import FL from '../lib/core.js';

const background = fs.readFileSync(new URL('../background.js', import.meta.url), 'utf8');
const root = new URL('../../', import.meta.url).pathname;
const SHIPPED = { list_gap: [7, 12], profile_gap: [35, 70], spacing: [2, 5], break_every: [40, 60], break_len: [90, 180], window_max: 72 };
const clone = (v) => JSON.parse(JSON.stringify(v));
const reset = () => FL.resetPace();
const MIN = 60e3;

test('the shipped pace is exactly what it was before the control existed', () => {
  reset();
  assert.deepEqual(FL.paceDefaults(), SHIPPED);
  assert.deepEqual(clone(FL.PACE), { window: 11 * MIN, cooldownBase: 10 * MIN, cooldownCap: 6 * 60 * MIN, strikes: 3, strikePause: 120 * MIN,
    netBase: 30e3, netCap: 10 * MIN, otherBase: 2 * MIN, otherCap: 5 * MIN, listGap: [7000, 12000], profileGap: [35000, 70000],
    spacing: [2000, 5000], breakEvery: [40, 60], breakLen: [90000, 180000], windowMax: 72 });
  assert.equal(FL.getPace().id, 'lg7-12|pg35-70|sp2-5|be40-60|bl90-180|wm72');
  assert.deepEqual(FL.BUDGET, { list: 3000, profile: 300 });
});

test('the bounds allow nothing faster than the shipped pacing', () => {
  const fast = FL.clampPace({ list_gap: [1, 2], profile_gap: [1, 2], spacing: [0, 1], break_every: [500, 900], break_len: [1, 2], window_max: 9999 }).values;
  assert.deepEqual(fast, SHIPPED);
  assert.deepEqual(FL.clampPace(FL.PACE_LIMITS.presets.normal.values).values, SHIPPED);
  assert.deepEqual(FL.clampPace(FL.PACE_LIMITS.presets.fast.values).values, SHIPPED);
});

test('every value is clamped into its own bounds, keeps its jitter, and clamping twice changes nothing', () => {
  let seed = 7;
  const rnd = () => (seed = (seed * 48271) % 2147483647) / 2147483647;
  for (let i = 0; i < 500; i++) {
    const ask = {};
    for (const [f, spec] of Object.entries(FL.PACE_LIMITS.fields)) ask[f] = spec.kind === 'int' ? rnd() * 300 - 5 : [rnd() * 1500 - 50, rnd() * 1500 - 50];
    const { values } = FL.clampPace(ask), again = FL.clampPace(values);
    assert.deepEqual(again.values, values);
    assert.deepEqual(again.notes, []);
    for (const [f, spec] of Object.entries(FL.PACE_LIMITS.fields)) {
      if (spec.kind === 'int') { assert.ok(values[f] >= spec.floor && values[f] <= spec.ceiling); continue; }
      const [a, b] = values[f];
      assert.ok(a >= spec.floor[0] && a <= spec.ceiling[0] && b >= spec.floor[1] && b <= spec.ceiling[1], f + ' ' + values[f]);
      assert.ok(b >= a * spec.min_spread - 1e-9, f + ' spread');
    }
  }
});

test('unusable values never apply: the field keeps what it had', () => {
  const base = { ...SHIPPED, list_gap: [20, 30] };
  for (const bad of [null, 'fast', true, [1], [1, 2, 3], [NaN, 5], [Infinity, 9], {}, [ '7', '12' ]]) {
    const { values, notes } = FL.clampPace({ list_gap: bad }, base);
    assert.deepEqual(values.list_gap, [20, 30], JSON.stringify(bad));
    assert.equal(notes[0].why, 'unusable');
  }
  assert.equal(FL.clampPace({ window_max: NaN }).values.window_max, 72);
});

test('JavaScript and Python clamp identically (same values, same id) on 150 random inputs', () => {
  let seed = 11;
  const rnd = () => (seed = (seed * 48271) % 2147483647) / 2147483647;
  for (let i = 0; i < 150; i++) {
    const ask = {};
    for (const [f, spec] of Object.entries(FL.PACE_LIMITS.fields)) {
      if (rnd() < 0.25) continue;
      const draw = () => Math.round((rnd() * 400 - 20) * (rnd() < 0.5 ? 10 : 1)) / 10;
      ask[f] = spec.kind === 'int' ? Math.round(draw()) : [draw(), draw()];
    }
    const js = FL.clampPace(ask).values;
    const py = JSON.parse(execFileSync('python3', [root + 'server/pace_limits.py', JSON.stringify(ask)]).toString());
    assert.deepEqual(js, py.values, JSON.stringify(ask));
    assert.equal(FL.paceId(js), py.id);
  }
});

test('setPace changes the gaps the very next request draws, in both directions, and resetPace restores the shipped ones', () => {
  reset();
  const gaps = (kind, r) => { const st = FL.fresh(); FL.afterRequest(st, kind, 1e6, r); return (kind === 'list' ? st.listNextAt : st.profileNextAt) - 1e6; };
  assert.deepEqual([gaps('list', () => 0), gaps('list', () => 1)], [7000, 12000]);
  FL.setPace({ ...SHIPPED, list_gap: [14, 24], profile_gap: [70, 140] });
  assert.deepEqual([gaps('list', () => 0), gaps('list', () => 1), gaps('profile', () => 0), gaps('profile', () => 1)], [14000, 24000, 70000, 140000]);
  FL.setPace({ ...SHIPPED, list_gap: [9, 14] });
  assert.deepEqual([gaps('list', () => 0), gaps('list', () => 1)], [9000, 14000]);
  reset();
  assert.deepEqual([gaps('list', () => 0), gaps('list', () => 1)], [7000, 12000]);
});

test('breaks follow the pace: how often, how long, and the window ceiling', () => {
  reset();
  FL.setPace({ ...SHIPPED, break_every: [10, 13], break_len: [300, 400], window_max: 20 });
  const st = FL.fresh();
  st.breakEvery = 10; st.pages = 9;
  FL.afterRequest(st, 'list', 0, () => 0);
  assert.equal(st.listNextAt, 7000 + 300000);
  assert.equal(st.listBreakUntil, st.listNextAt);
  assert.ok(st.breakEvery >= 10 && st.breakEvery <= 13);
  st.rlog = Array.from({ length: 20 }, (_, i) => [i * 1000, 'list']);
  assert.ok(FL.windowOf(st, 20000).until > 20000, 'a lowered ceiling blocks at 20 requests in 11 minutes');
  assert.equal(FL.windowOf({ rlog: st.rlog.slice(0, 19) }, 20000).until, 0);
  reset();
  assert.equal(FL.windowOf(st, 20000).until, 0, 'the shipped ceiling is 72');
});

const stateWithHistory = (now) => {
  const st = FL.fresh();
  st.day = FL.dayKey(now); st.today = { list: 40, profile: 3, people: 900, bios: 3 };
  st.rlog = [[now - 30e3, 'list'], [now - 5e3, 'list'], [now - 20e3, 'profile']];
  st.listNextAt = now + 9e3; st.profileNextAt = now + 40e3; st.nextAt = now + 3e3;
  st.cool.list = { until: now + 3600e3, hits: [now - 60e3], retryUntil: now + 1800e3 };
  st.hold = { code: 'login', message: 'x', at: now - 1000 };
  return st;
};

test('repace redraws the waiting gap from the last request: slower lengthens it now, faster shortens it now', () => {
  reset();
  const now = 1e9, st = stateWithHistory(now);
  FL.setPace({ ...SHIPPED, list_gap: [60, 90] });
  FL.repace(st, now, () => 0);
  assert.equal(st.listNextAt, now - 5e3 + 60e3);
  FL.setPace({ ...SHIPPED, list_gap: [7, 12] });
  FL.repace(st, now, () => 0);
  assert.equal(st.listNextAt, now - 5e3 + 7e3, 'back to normal: ready again 2 s from now, not 55 s');
  assert.ok(st.profileNextAt >= now - 20e3 + 35e3);
});

test('repace never touches cooldowns, holds, counters or the request log, and never shortens a backoff or a break', () => {
  reset();
  const now = 1e9, st = stateWithHistory(now);
  st.nextAt = now + 90e3;                          // a backoff or hit pause
  st.listNextAt = now + 200e3; st.listBreakUntil = now + 200e3;   // a break in progress
  const keep = clone({ cool: st.cool, hold: st.hold, today: st.today, rlog: st.rlog, streak: st.streak, listEndpointUntil: st.listEndpointUntil });
  FL.setPace({ ...SHIPPED });                       // the fastest the bounds allow
  FL.repace(st, now, () => 0);
  assert.deepEqual(clone({ cool: st.cool, hold: st.hold, today: st.today, rlog: st.rlog, streak: st.streak, listEndpointUntil: st.listEndpointUntil }), keep);
  assert.equal(st.nextAt, now + 90e3, 'spacing only ever moves later');
  assert.equal(st.listNextAt, now + 200e3, 'a break in progress is not cut short');
});

test('hits, cooldown lengths and the strike rule are identical at the slowest and the fastest pace', () => {
  const run = () => {
    const st = FL.fresh(), t = Date.parse('2026-10-04T10:00:00Z');
    const untils = [];
    for (let i = 0; i < 4; i++) { FL.applyHit(st, t + i * 60e3, null, 'list'); untils.push(st.cool.list.until - (t + i * 60e3)); }
    return untils;
  };
  reset(); const normal = run();
  FL.setPace(FL.PACE_LIMITS.presets.slow.values); const slow = run();
  assert.deepEqual(slow, normal);
  assert.ok(normal[2] >= 2 * 60 * MIN - 120e3, 'third hit rests at least two hours');
  reset();
});

test('plan: a cooldown, the window and a spent budget decide first; the pace only decides the wait between requests', () => {
  reset();
  const now = 5e9, st = FL.fresh();
  FL.rollDay(st, now);
  const left = { list: 10, profile: 10 };
  assert.equal(FL.plan(st, left, now).why, 'ready');
  st.cool.list.until = now + 60e3;
  assert.deepEqual(FL.plan(st, { list: 10, profile: 0 }, now), { kinds: [], wait: 60e3, why: 'cooldown' });
  st.cool.list.until = 0;
  assert.equal(FL.plan(st, { list: 0, profile: 0 }, now).why, 'budget');
  FL.setPace({ ...SHIPPED, list_gap: [96, 120] });
  const s2 = FL.fresh(); FL.rollDay(s2, now); FL.afterRequest(s2, 'list', now, () => 0);
  s2.rlog = [[now, 'list']];
  const p = FL.plan(s2, { list: 10, profile: 0 }, now + 1000);
  assert.equal(p.why, 'pace');
  assert.ok(p.wait > 90e3, 'a slower pace means a longer wait: ' + p.wait);
  reset();
});

test('budgets: 0 still means no daily limit, and nothing can exceed the bound from the one file', () => {
  assert.deepEqual(FL.budgetOf({ list: 0, profile: 0 }), { list: 0, profile: 0 });
  assert.deepEqual(FL.budgetOf({ list: 400, profile: 100 }), { list: 400, profile: 100 });
  assert.deepEqual(FL.budgetOf({ list: 99999, profile: 99999 }), FL.PACE_LIMITS.budget_max);
  assert.deepEqual(FL.budgetOf({ list: -5, profile: 'x' }), { list: 0, profile: 0 });
  const st = FL.fresh(); FL.rollDay(st, 1e9); st.today.list = 399;
  assert.equal(FL.budgetLeft(st, { list: 400, profile: 100 }, 1e9).list, 1);
  assert.equal(FL.budgetLeft(st, { list: 0, profile: 100 }, 1e9).list, Infinity);
});

// ---- the worker: delivery, persistence, malformed input -------------------------------------------------------
function worker(data = {}) {
  const trail = [];
  let chain = Promise.resolve();
  const context = vm.createContext({ Date, Math, JSON, Number, mem: { gen: 0 }, FL,
    get: async (k) => data[k], set: async (v) => Object.assign(data, v), trail: async (what, extra) => { trail.push([what, extra]); },
    editSt: (fn) => { const p = chain.then(async () => { const st = FL.normalize(data.st, Date.now()); await fn(st); data.st = st; return st; }); chain = p.catch(() => {}); return p; } });
  vm.runInContext(background.slice(background.indexOf('async function loadPace()'), background.indexOf('async function applyServer(')), context);
  return { context, data, trail };
}
const wire = (values, preset = 'custom', scope = 'global') => ({ v: 1, preset, scope, values });

test('a pace from the server is clamped again here, stored, and rescheduled at once', async () => {
  reset();
  const now = Date.now(), st = FL.fresh();
  st.rlog = [[now - 1000, 'list']]; st.listNextAt = now + 8000;
  const w = worker({ st });
  await w.context.applyPace({ paused: false, pace: wire({ ...SHIPPED, list_gap: [60, 90], profile_gap: [1, 2] }, 'custom') });
  assert.deepEqual(w.data.paceCfg.values.list_gap, [60, 90]);
  assert.deepEqual(w.data.paceCfg.values.profile_gap, [35, 70], 'the extension clamps with its own copy of the bounds');
  assert.equal(FL.getPace().id, FL.paceId(w.data.paceCfg.values));
  assert.ok(w.data.st.listNextAt >= now - 1000 + 60e3, 'the wait already running is redrawn from the last request');
  assert.equal(w.trail[0][0], 'pace changed');
  reset();
});

test('the same pace again changes nothing and writes nothing', async () => {
  reset();
  const w = worker({});
  await w.context.applyPace({ paused: false, pace: wire(SHIPPED, 'normal') });
  assert.equal(w.data.paceCfg, undefined);
  assert.deepEqual(w.trail, []);
});

test('no pace from the server means the shipped defaults; a malformed pace is ignored and what runs keeps running', async () => {
  reset();
  const w = worker({});
  await w.context.applyPace({ paused: false, pace: wire({ ...SHIPPED, list_gap: [30, 40] }) });
  assert.deepEqual(FL.PACE.listGap, [30000, 40000]);
  for (const bad of [{ v: 2, values: SHIPPED }, { v: 1 }, { v: 1, values: [] }, 'slow', 5, { v: 1, values: 'x' }]) {
    await w.context.applyPace({ paused: false, pace: bad });
    assert.deepEqual(FL.PACE.listGap, [30000, 40000], JSON.stringify(bad));
  }
  assert.equal(w.trail.filter(([what]) => what === 'pace ignored').length, 1, 'logged once, not on every heartbeat');
  await w.context.applyPace({ paused: false });                      // an older server: no pace field at all
  assert.deepEqual(FL.PACE.listGap, [7000, 12000]);
  await w.context.applyPace({ error: 'x' });                         // not a control reply: speaks for nobody
  assert.deepEqual(FL.PACE.listGap, [7000, 12000]);
  reset();
});

test('a restarted worker comes back on the stored pace before its first request', async () => {
  reset();
  const first = worker({});
  await first.context.applyPace({ paused: false, pace: wire({ ...SHIPPED, list_gap: [20, 30], break_len: [200, 300] }) });
  const stored = first.data.paceCfg;
  reset();                                                           // a new service worker starts from the defaults
  assert.deepEqual(FL.PACE.listGap, [7000, 12000]);
  const second = worker({ paceCfg: stored });
  await second.context.loadPace();
  assert.deepEqual(FL.PACE.listGap, [20000, 30000]);
  assert.deepEqual(FL.PACE.breakLen, [200000, 300000]);
  const corrupt = worker({ paceCfg: { v: 1, values: { list_gap: 'x', window_max: 100000 } } });
  await corrupt.context.loadPace();
  assert.deepEqual(FL.PACE.listGap, [7000, 12000]);
  assert.equal(FL.PACE.windowMax, 72);
  reset();
});

test('applying a pace does not deadlock the storage lock or clear any protection', async () => {
  reset();
  const now = Date.now(), st = FL.fresh();
  st.hold = { code: 'challenge', manualReview: true, at: now }; st.cool.list = { until: now + 1e6, hits: [now], retryUntil: 0 };
  const data = { st, localPaused: true, sharedRequest: { phase: 'started', body: { token: 'keep' } }, budget: { list: 5, profile: 5 } };
  const w = worker(data);
  await Promise.race([w.context.applyPace({ paused: true, pace: wire({ ...SHIPPED, list_gap: [90, 120] }) }),
    new Promise((_, no) => setTimeout(() => no(Error('deadlock')), 500))]);
  assert.equal(data.st.hold.manualReview, true);
  assert.equal(data.st.cool.list.until, now + 1e6);
  assert.equal(data.localPaused, true);
  assert.equal(data.sharedRequest.body.token, 'keep');
  assert.deepEqual(data.budget, { list: 5, profile: 5 });
  reset();
});

test('the heartbeat reports the running pace and the manifest loads the bounds file before core.js', () => {
  assert.match(background, /pace: \{ id: FL\.getPace\(\)\.id/);
  assert.match(background, /importScripts\('lib\/pace-limits\.js', 'lib\/core\.js'/);
  const manifest = JSON.parse(fs.readFileSync(new URL('../manifest.json', import.meta.url), 'utf8'));
  const widget = manifest.content_scripts.find((c) => c.js.includes('lib/core.js')).js;
  assert.ok(widget.indexOf('lib/pace-limits.js') >= 0 && widget.indexOf('lib/pace-limits.js') < widget.indexOf('lib/core.js'));
  const popup = fs.readFileSync(new URL('../popup.html', import.meta.url), 'utf8');
  assert.ok(popup.indexOf('lib/pace-limits.js') >= 0 && popup.indexOf('lib/pace-limits.js') < popup.indexOf('lib/core.js'));
});

test('core.js refuses to load without the bounds file rather than run with unknown pacing', () => {
  const source = fs.readFileSync(new URL('../lib/core.js', import.meta.url), 'utf8');
  assert.throws(() => vm.runInContext(source, vm.createContext({})), /pace-limits\.js must load before/);
});
