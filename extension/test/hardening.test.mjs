// 3.3.0: unattended-run hardening (empty-page soft blocks, per-bucket cooldowns, plan, tabs, outbox cap, migration).
import test from 'node:test';
import assert from 'node:assert/strict';
import FL from '../lib/core.js';

const MIN = 60e3, HOUR = 60 * MIN, DAY = 24 * HOUR;
const T0 = new Date(2026, 8, 24, 10, 0, 0).getTime();
const res = (json, status = 200, extra = {}) => ({ status, json, text: JSON.stringify(json), ...extra });
const u = (i) => ({ pk: i, username: 'u' + i });
const code = (r) => (r ? r.code : null);

test('list: soft-block signs from RESEARCH §3 map to the right codes', () => {
  const L = (j, ctx) => FL.classify(res(j), 'list', T0, ctx);
  // users:[] while Instagram promises more
  assert.equal(L({ users: [], has_more: true, status: 'ok' }).reason, 'empty_page_with_more');
  // page 1 empty while the list has people
  assert.deepEqual([code(L({ users: [], status: 'ok' }, { total: 500 })), L({ users: [], status: 'ok' }, { total: 500 }).reason],
    ['soft_block', 'empty_first_page']);
  // GraphQL count>0 with zero edges (dead query_hash)
  assert.equal(code(L({ data: { user: { edge_followed_by: { count: 9, edges: [], page_info: { has_next_page: false } } } }, status: 'ok' })), 'soft_block');
  // please wait, require_login, feedback_required, checkpoint, spam flag
  assert.equal(code(FL.classify(res({ message: 'Please wait a few minutes before you try again.', require_login: true, status: 'fail' }, 400), 'list')), 'rate_limit');
  assert.equal(code(L({ require_login: true, status: 'fail', message: '' })), 'login');
  assert.equal(code(FL.classify(res({ message: 'feedback_required', spam: true, status: 'fail' }, 400), 'list')), 'soft_block');
  assert.equal(code(FL.classify(res({ message: 'checkpoint_required', checkpoint_url: 'https://i.instagram.com/challenge/1/' }, 400), 'list')), 'challenge');
  assert.equal(code(FL.classify({ status: 200, text: '<html>', json: null, url: 'https://www.instagram.com/accounts/suspended/' }, 'list')), 'challenge');
});
test('list: capped lists are usable (done + limited), never a block', () => {
  for (const j of [{ users: [u(1), u(2)], should_limit_list_of_followers: true, has_more: false, status: 'ok' },
    { users: [], should_limit_list_of_followers: true, status: 'ok' }]) {
    assert.equal(FL.classify(res(j), 'list', T0, { total: 686e6 }), null);
    const p = FL.parsePage(j);
    assert.deepEqual([p.done, p.limited, p.next_cursor], [true, true, null]);
  }
});
test('list: end detection (has_more false, empty tail, missing has_more)', () => {
  assert.equal(FL.parsePage({ users: [u(1)], next_max_id: 'c', has_more: false }).done, true);
  assert.equal(FL.parsePage({ users: [u(1)] }).done, true);
  assert.equal(FL.parsePage({ users: [u(1)], next_max_id: 'c' }).next_cursor, 'c'); // has_more missing mid-list: keep going
  // empty tail after a cursor: genuine end
  assert.equal(FL.classify(res({ users: [], status: 'ok' }), 'list', T0, { cursor: 'c9', total: 500, received: 480 }), null);
  assert.equal(FL.parsePage({ users: [], status: 'ok' }).done, true);
  // empty page that still promises more, but we already hold ~all of the list: tail, not a block
  assert.equal(FL.classify(res({ users: [], next_max_id: 'x', status: 'ok' }), 'list', T0, { cursor: 'c9', total: 500, received: 495 }), null);
  // a list that is really empty (total 0 / unknown) is fine
  assert.equal(FL.classify(res({ users: [], status: 'ok' }), 'list', T0, { total: 0 }), null);
});
test('list: contradictory or unusable pages cannot silently finish a crawl', () => {
  const noCursor = { users: [u(1)], has_more: true, status: 'ok' };
  assert.deepEqual([FL.classify(res(noCursor), 'list').code, FL.classify(res(noCursor), 'list').reason], ['other', 'missing_cursor']);
  const noUsableUsers = { users: [{ pk: 1 }, { username: '' }], has_more: false, status: 'ok' };
  assert.deepEqual([FL.classify(res(noUsableUsers), 'list').code, FL.classify(res(noUsableUsers), 'list').reason], ['other', 'unusable_users']);
});
test('invalid Instagram totals stay unknown rather than claiming coverage', () => {
  for (const value of [-1, Infinity, 'NaN', '0xFF', '1e12', 1e12, true]) assert.equal(FL.count(value), null);
  assert.equal(FL.count('1,234'), 1234);
  assert.equal(FL.classify(res({ users: [], status: 'ok' }), 'list', T0, { total: '0xFF' }).reason, 'invalid_total');
});
test('list: a new run resets local history and server count wins on resume', () => {
  const old = { jobId: 11, next: 'same', received: 400, emptyAt: 'same', pages: 20 };
  assert.deepEqual(FL.listProgress({ id: 12, cursor: null, received: 0 }, old), {});
  assert.equal(FL.listContext({ id: 11, cursor: 'same', received: 350 }, old, 1000).received, 350);
  assert.equal(FL.listContext({ id: 11, cursor: 'different', received: 350 }, old, 1000).emptyAt, null);
});
test('list: the same empty page again after the cooldown is other, not another hit', () => {
  const j = { users: [], has_more: true, next_max_id: 'n', status: 'ok' };
  assert.equal(code(FL.classify(res(j), 'list', T0, { cursor: 'c5', emptyAt: 'c5' })), 'other');
  assert.equal(code(FL.classify(res(j), 'list', T0, { cursor: 'c5', emptyAt: 'c4' })), 'soft_block');
  assert.equal(FL.classify(res({ users: [] }), 'list', T0, { total: 10, emptyAt: '' }).reason, 'empty_first_page_again');
});
test('profile: /info/ checks, unsupported, network', () => {
  assert.equal(code(FL.classify(res({ status: 'ok' }), 'profile')), 'other'); // no user object
  assert.equal(code(FL.classify(res({ message: 'useragent mismatch', status: 'fail' }, 400), 'profile')), 'unsupported');
  const t = FL.classify({ status: 0, text: 'aborted', aborted: true }, 'profile');
  assert.deepEqual([t.code, t.reason], ['network', 'timeout']);
  assert.equal(FL.classify({ status: 0, text: 'x', tabError: true }, 'profile').reason, 'tab');
  assert.equal(code(FL.classify({ status: 500, text: 'oops' }, 'profile')), 'other');
  assert.equal(code(FL.classify({ status: 200, text: '<html>Something went wrong</html>', json: null }, 'profile')), 'other');
});
test('pageVerdict: lookup tab outcomes', () => {
  assert.equal(FL.pageVerdict({ url: 'https://www.instagram.com/accounts/login/?next=%2Fx%2F', title: 'Login' }).code, 'login');
  assert.equal(FL.pageVerdict({ url: 'https://www.instagram.com/challenge/?next=/x/', title: '' }).code, 'challenge');
  assert.equal(FL.pageVerdict({ url: 'https://www.instagram.com/gone/', title: 'Page not found • Instagram' }).code, 'not_found');
  assert.equal(FL.pageVerdict({ url: 'https://www.instagram.com/gone/', title: 'Instagram', text: "Sorry, this page isn't available." }).code, 'not_found');
  assert.equal(FL.pageVerdict({ url: 'https://www.instagram.com/x/', title: 'x • Instagram' }).code, 'other');
  assert.equal(FL.pageVerdict(null).code, 'network');
});

test('per-bucket cooldowns: a list hit does not stop bios beyond the 5 min lane pause', () => {
  const st = FL.fresh();
  FL.applyHit(st, T0, null, 'list');
  assert.equal(st.cool.list.until, T0 + 10 * MIN);
  assert.equal(st.cool.profile.until, 0);
  assert.equal(st.nextAt, T0 + 5 * MIN);
  const left = { list: 100, profile: 100 };
  assert.deepEqual(FL.plan(st, left, T0 + MIN), { kinds: [], wait: 4 * MIN, why: 'pace' });
  assert.deepEqual(FL.plan(st, left, T0 + 5 * MIN).kinds, ['profile']);
  assert.deepEqual(FL.plan(st, left, T0 + 11 * MIN).kinds, ['list', 'profile']);
  FL.applyHit(st, T0 + 6 * MIN, null, 'profile');
  assert.equal(st.cool.profile.until, T0 + 16 * MIN); // its own ladder starts at 10 min
});
test('3 hits within an hour across buckets stop everything until midnight', () => {
  const st = FL.fresh();
  FL.applyHit(st, T0, null, 'list');
  FL.applyHit(st, T0 + 20 * MIN, null, 'profile');
  assert.ok(st.cool.list.until < FL.nextMidnight(T0));
  FL.applyHit(st, T0 + 40 * MIN, null, 'list');
  assert.equal(st.cool.list.until, FL.nextMidnight(T0));
  assert.equal(st.cool.profile.until, FL.nextMidnight(T0));
  assert.equal(FL.rateOf(st, T0 + 41 * MIN).hits_24h, 3);
  assert.equal(FL.cooldownUntil(st, T0 + 41 * MIN), FL.nextMidnight(T0));
});
test('plan: pacing gaps, profile gap, budgets, cooldown waits', () => {
  const st = FL.fresh(), left = { list: 10, profile: 10 };
  FL.afterRequest(st, 'profile', T0, () => 0); // bio: its own 35 s gap; lists only wait the 2 s spacing
  assert.deepEqual(FL.plan(st, left, T0 + 1e3), { kinds: [], wait: 1e3, why: 'pace' });
  assert.deepEqual(FL.plan(st, left, T0 + 10e3).kinds, ['list']);
  assert.deepEqual(FL.plan(st, left, T0 + 35e3).kinds, ['list', 'profile']);
  FL.afterRequest(st, 'list', T0 + 35e3, () => 0); // list gap 7 s; a bio may go in it after the spacing
  assert.deepEqual(FL.plan(st, left, T0 + 38e3).kinds, ['profile']);
  st.profileNextAt = T0 + 100e3;
  assert.deepEqual(FL.plan(st, left, T0 + 43e3).kinds, ['list']);
  assert.deepEqual(FL.plan(st, { list: 0, profile: 0 }, T0), { kinds: [], wait: 10 * MIN, why: 'budget' });
  const c = FL.fresh(); c.cool.list.until = T0 + 7 * MIN;
  assert.deepEqual(FL.plan(c, { list: 5, profile: 0 }, T0), { kinds: [], wait: 7 * MIN, why: 'cooldown' });
});
test('pacing unchanged: list 7-12 s, profile 35-70 s; requests logged for the hourly rate', () => {
  const st = FL.fresh();
  FL.afterRequest(st, 'list', T0, () => 1); assert.equal(st.listNextAt, T0 + 12e3);
  FL.afterRequest(st, 'profile', T0, () => 1); assert.equal(st.profileNextAt, T0 + 70e3);
  assert.equal(FL.readyAt(st, 'list'), T0 + 12e3);
  assert.equal(FL.rateOf(st, T0 + MIN).requests_hour, 2);
  assert.equal(FL.rateOf(st, T0 + 2 * HOUR).requests_hour, 0);
});
test('backoff: network 30 s → 10 min cap, other 2 → 30 min cap, success resets', () => {
  const st = FL.fresh();
  FL.backoff(st, T0, 'net'); assert.equal(st.nextAt, T0 + 30e3);
  FL.backoff(st, T0, 'net'); assert.equal(st.nextAt, T0 + 60e3);
  for (let i = 0; i < 10; i++) FL.backoff(st, T0, 'net');
  assert.equal(st.nextAt, T0 + 10 * MIN);
  const o = FL.fresh();
  FL.backoff(o, T0, 'other'); assert.equal(o.nextAt, T0 + 2 * MIN);
  for (let i = 0; i < 10; i++) FL.backoff(o, T0, 'other');
  assert.equal(o.nextAt, T0 + 30 * MIN);
  FL.succeeded(o); assert.deepEqual(o.streak, { other: 0, net: 0 });
});
test('lane: busy until expiry', () => {
  assert.equal(FL.laneBusy(null, T0), false);
  assert.equal(FL.laneBusy({ until: T0 + 1 }, T0), true);
  assert.equal(FL.laneBusy({ until: T0 }, T0), false);
});
test('normalize: migrates the 3.2 global cooldown into both buckets and fills defaults', () => {
  const st = FL.normalize({ cooldownUntil: T0 + HOUR, hits: [T0 - MIN], today: { list: 3, profile: 1, people: 9, bios: 1 }, day: FL.dayKey(T0) }, T0);
  assert.equal(st.cool.list.until, T0 + HOUR); assert.equal(st.cool.profile.until, T0 + HOUR);
  assert.deepEqual(st.cool.list.hits, [T0 - MIN]); assert.deepEqual(st.cool.profile.hits, []);
  assert.ok(!('cooldownUntil' in st) && !('hits' in st));
  assert.equal(st.today.list, 3);
  assert.deepEqual(st.streak, { other: 0, net: 0 });
  const n = FL.normalize(undefined, T0);
  assert.equal(n.cool.list.until, 0); assert.equal(n.day, FL.dayKey(T0));
});

test('chooseTab: prefers the work tab, skips login/challenge/discarded, wakes or opens safely', () => {
  const tab = (id, path, x = {}) => ({ id, url: 'https://www.instagram.com' + path, status: 'complete', active: false, ...x });
  assert.deepEqual(FL.chooseTab([tab(1, '/'), tab(2, '/x/')], { preferId: 2 }, T0), { use: 2 });
  assert.deepEqual(FL.chooseTab([tab(1, '/', { active: true }), tab(2, '/x/')], {}, T0), { use: 2 });
  assert.deepEqual(FL.chooseTab([tab(1, '/accounts/login/'), tab(2, '/')], {}, T0), { use: 2 });
  assert.deepEqual(FL.chooseTab([tab(1, '/', { discarded: true })], {}, T0), { reload: 1 });
  assert.deepEqual(FL.chooseTab([tab(1, '/', { frozen: true })], {}, T0), { reload: 1 });
  assert.deepEqual(FL.chooseTab([tab(1, '/', { discarded: true, active: true })], {}, T0).why, 'tab_busy');
  assert.equal(FL.chooseTab([tab(1, '/', { status: 'loading' })], {}, T0).why, 'tab_loading');
  assert.equal(FL.chooseTab([tab(1, '/accounts/login/')], {}, T0).why, 'tab_login');
  assert.equal(FL.chooseTab([tab(1, '/challenge/abc/')], {}, T0).why, 'tab_challenge');
  assert.deepEqual(FL.chooseTab([], {}, T0), { open: true, why: 'no_tab' });
  assert.deepEqual(FL.chooseTab([{ id: 9, url: 'https://example.com/', status: 'complete' }], {}, T0), { open: true, why: 'no_tab' });
  // a tab that just timed out is avoided for a while: another one is used, or it is reloaded if in the background
  assert.deepEqual(FL.chooseTab([tab(1, '/'), tab(2, '/')], { avoid: { id: 1, until: T0 + MIN } }, T0), { use: 2 });
  assert.deepEqual(FL.chooseTab([tab(1, '/')], { avoid: { id: 1, until: T0 + MIN } }, T0), { reload: 1 });
  assert.deepEqual(FL.chooseTab([tab(1, '/')], { avoid: { id: 1, until: T0 - 1 } }, T0), { use: 1 });
});
test('outbox cap drops passive bios first, keeps job results', () => {
  let box = [];
  box = FL.enqueue(box, '/api/ext/list-page', { job_id: 1 }, 3);
  box = FL.enqueue(box, '/api/ext/profile', { job_id: null }, 3);
  box = FL.enqueue(box, '/api/ext/profile', { job_id: 7 }, 3);
  box = FL.enqueue(box, '/api/ext/error', { job_id: 8 }, 3);
  assert.deepEqual(box.map((x) => x.body.job_id), [1, 7, 8]);
  box = FL.enqueue(box, '/api/ext/list-page', { job_id: 9 }, 3);
  assert.deepEqual(box.map((x) => x.body.job_id), [1, 7, 8, 9]);
  box = FL.enqueue(box, '/api/ext/profile', { job_id: null }, 3);
  assert.deepEqual(box.map((x) => x.body.job_id), [1, 7, 8, 9]);
});
test('rememberId: pk cache keyed by handle, pruned oldest first', () => {
  let ids = FL.rememberId({}, { handle: 'Brand', ig_id: 5, followers: 10, following: 2 }, T0);
  assert.deepEqual(ids.brand, { ig_id: '5', followers: 10, following: 2, at: T0 });
  ids = FL.rememberId(ids, { handle: 'b', ig_id: '6' }, T0 + 1, 1);
  assert.deepEqual(Object.keys(ids), ['b']);
  assert.equal(FL.rememberId(ids, { handle: 'c' }, T0), ids); // no pk: unchanged
});
test('statusOf: one bucket cooling keeps the other running and says so', () => {
  const st = FL.fresh(); st.cool.list.until = T0 + 30 * MIN;
  const s = FL.statusOf(st, { job: {} }, T0);
  assert.equal(s.state, 'running'); assert.match(s.text, /^Lists cooling until \d\d:\d\d · Scraping$/); assert.equal(s.badge, '30m');
  assert.equal(FL.statusOf(st, {}, T0).state, 'cooldown');
  const p = FL.fresh(); p.cool.profile.until = T0 + 30 * MIN; p.nextAt = T0 + 5e3;
  assert.match(FL.statusOf(p, {}, T0).text, /^Bios cooling until .* · Next request in 5s$/);
  assert.equal(FL.statusOf(FL.fresh(), { noTab: 'tab_login' }, T0).text, 'Instagram tab is on the login page');
  assert.equal(FL.statusOf(FL.fresh(), { laneWait: true }, T0).key, 'wait');
});
test('sampleOf: carries tab context and timing for self-diagnosis', () => {
  const s = FL.sampleOf({ status: 200, contentType: 'application/json', url: 'https://www.instagram.com/api/v1/users/1/info/', text: '{"status":"ok"}',
    env: { path: '/', vis: 'hidden', csrf: true, uid: false }, ms: 812 });
  assert.match(s, /^HTTP 200 application\/json https:\/\/www\.instagram\.com\/api\/v1\/users\/1\/info\/ body: \{"status":"ok"\} \| tab \/ hidden csrf=1 uid=0 \| 812 ms$/);
});

test('window: at most 72 requests of any kind in 11 min per account, whatever the gaps say', () => {
  const st = FL.fresh(), left = { list: 1000, profile: 1000 };
  for (let i = 0; i < 72; i++) FL.afterRequest(st, i % 5 ? 'list' : 'profile', T0 + i * 8e3, () => 0);
  const t = T0 + 72 * 8e3; // 9.6 min: every clock says go
  assert.deepEqual(FL.plan(st, left, t), { kinds: [], wait: T0 + 11 * MIN - t, why: 'window' });
  assert.ok(FL.plan(st, left, T0 + 11 * MIN).kinds.length);
  assert.equal(FL.windowOf(st, T0 + 11 * MIN).n, 71);
});
test('3.4 state migrates: its nextAt was the list clock', () => {
  const st = FL.normalize({ nextAt: T0 + 9e3, profileNextAt: T0 + 40e3 }, T0);
  assert.equal(FL.readyAt(st, 'list'), T0 + 9e3);
  assert.equal(FL.readyAt(st, 'profile'), T0 + 40e3);
});

test('workspace controls stop cached list and bio jobs, including offline control checks', () => {
  assert.equal(FL.controlAllows({serverPaused: true}, 'list'), false);
  assert.equal(FL.controlAllows({serverPaused: true}, 'profile'), false);
  assert.equal(FL.controlAllows({offline: true}, 'profile'), false);
  const state = {stages: {list: true, profile: false}};
  assert.equal(FL.controlAllows(state, 'list'), true);
  assert.equal(FL.controlAllows(state, 'profile'), false);
  assert.equal(FL.controlAllows({stages: {list: false, profile: true}}, 'list'), false);
  assert.equal(FL.controlAllows({stages: {list: false, profile: true}}, 'profile'), true);
});
