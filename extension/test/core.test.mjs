import test from 'node:test';
import assert from 'node:assert/strict';
import FL from '../lib/core.js';

const MIN = 60e3, HOUR = 60 * MIN, DAY = 24 * HOUR;
const T0 = new Date(2026, 8, 24, 10, 0, 0).getTime(); // local 10:00
const res = (json, status = 200, extra = {}) => ({ status, json, text: JSON.stringify(json), ...extra });

test('classify: ok list page and ok profile', () => {
  assert.equal(FL.classify(res({ users: [{ pk: 1, username: 'a' }], next_max_id: '25', status: 'ok' }), 'list'), null);
  assert.equal(FL.classify(res({ users: [], status: 'ok' }), 'list'), null); // genuine end
  assert.equal(FL.classify(res({ user: { pk: 1, username: 'a', biography: '' }, status: 'ok' }), 'profile'), null);
});
test('classify: soft blocks', () => {
  assert.equal(FL.classify(res({ users: [], has_more: true, status: 'ok' }), 'list').code, 'soft_block');
  assert.equal(FL.classify(res({ users: [], next_max_id: 'x', status: 'ok' }), 'list').code, 'soft_block');
  assert.equal(FL.classify(res({ status: 'fail', message: 'something' }), 'list').code, 'soft_block');
  assert.equal(FL.classify(res({ status: 'fail', message: 'feedback_required' }), 'profile').code, 'soft_block');
});
test('classify: rate limits incl. Retry-After and 401 please-wait', () => {
  const r = FL.classify({ status: 429, text: '', retryAfter: '120' }, 'list', T0);
  assert.deepEqual(r, { code: 'rate_limit', retryAt: T0 + 120e3, reason: 'http_429' });
  assert.equal(FL.classify(res({ status: 'fail', message: 'Please wait a few minutes before you try again.' }), 'list').code, 'rate_limit');
  assert.equal(FL.classify({ status: 401, text: '{"message":"Please wait a few minutes"}' }, 'list').code, 'rate_limit');
});
test('classify: challenge and login', () => {
  assert.equal(FL.classify(res({ message: 'challenge_required', status: 'fail' }), 'list').code, 'challenge');
  assert.equal(FL.classify(res({ message: 'checkpoint_required', checkpoint_url: '/challenge/x' }), 'list').code, 'challenge');
  assert.equal(FL.classify({ status: 401, text: '' }, 'list').code, 'login');
  assert.equal(FL.classify(res({ message: 'login_required', status: 'fail' }), 'list').code, 'login');
  assert.equal(FL.classify(res({ require_login: true, status: 'fail' }), 'profile').code, 'login');
  assert.equal(FL.classify({ status: 200, text: '<html>Log in password</html>', json: null }, 'list').code, 'login');
});
test('classify: not found, private, network', () => {
  assert.equal(FL.classify({ status: 404, text: '' }, 'profile').code, 'not_found');
  assert.equal(FL.classify(res({ message: 'Not authorized to view user', status: 'fail' }), 'list').code, 'private');
  assert.equal(FL.classify({ status: 0, text: 'TypeError' }, 'list').code, 'network'); // local: never posted as a job error
});

test('parsePage maps users to contract fields', () => {
  const p = FL.parsePage({ users: [{ pk: 5, username: 'x', full_name: 'X Y', profile_pic_url: 'p', is_private: true, is_verified: 0 }, {}],
    next_max_id: 'c1', has_more: true });
  assert.deepEqual(p.users, [{ ig_id: '5', handle: 'x', name: 'X Y', pic_url: 'p', is_private: true, is_verified: false }]);
  assert.equal(p.next_cursor, 'c1'); assert.equal(p.done, false);
  assert.equal(FL.parsePage({ users: [{ pk: 1, username: 'a' }], next_max_id: 'c', has_more: false }).done, true);
  const capped = FL.parsePage({ users: [{ pk: 1, username: 'a' }], next_max_id: 'c', should_limit_list_of_followers: true });
  assert.deepEqual([capped.done, capped.limited, capped.next_cursor], [true, true, null]);
});
test('mapProfile: /info/ shape', () => {
  const p = FL.mapProfile({ pk: 9, username: 'brand', full_name: 'Brand', biography: 'We sell', external_url: '',
    bio_links: [{ url: 'https://b.co' }], category: 'Shopping', follower_count: 1200, following_count: 80, media_count: 40,
    is_business: true, is_private: false, hd_profile_pic_url_info: { url: 'hd' }, profile_pic_url: 'sd' });
  assert.deepEqual(p, { ig_id: '9', handle: 'brand', name: 'Brand', bio: 'We sell', website: 'https://b.co', category: 'Shopping',
    followers: 1200, following: 80, posts: 40, is_private: false, is_verified: false, is_business: true, pic_url: 'hd' });
});
test('mapProfile: web_profile_info / GraphQL shape, private', () => {
  const u = FL.userOf({ data: { user: { id: '7', username: 'p', biography: '', external_url: 'https://x', category_name: 'Artist',
    edge_followed_by: { count: 10 }, edge_follow: { count: 3 }, edge_owner_to_timeline_media: { count: 0 }, is_private: true,
    is_business_account: false, profile_pic_url: 'sd' } } });
  const p = FL.mapProfile(u);
  assert.equal(p.ig_id, '7'); assert.equal(p.website, 'https://x'); assert.equal(p.category, 'Artist');
  assert.deepEqual([p.followers, p.following, p.posts, p.is_private, p.pic_url], [10, 3, 0, true, 'sd']);
  assert.equal(FL.mapProfile({ pk: 1 }), null);
});

test('pacing: list gaps 7-12 s, break of 90-180 s every 40-60 pages', () => {
  const st = FL.fresh(); st.breakEvery = 40;
  let t = T0;
  for (let i = 1; i <= 39; i++) {
    FL.afterRequest(st, 'list', t);
    assert.ok(st.nextAt - t >= 7e3 && st.nextAt - t <= 12e3);
    t = st.nextAt;
  }
  FL.afterRequest(st, 'list', t);
  assert.ok(st.nextAt - t >= 7e3 + 90e3 && st.nextAt - t <= 12e3 + 180e3);
  assert.ok(st.breakEvery >= 40 && st.breakEvery <= 60);
  assert.equal(st.pages, 0); assert.equal(st.today.list, 40);
});
test('pacing: profile gap 35-70 s', () => {
  for (const r of [() => 0, () => 1, Math.random]) {
    const st = FL.afterRequest(FL.fresh(), 'profile', T0, r);
    assert.ok(st.nextAt - T0 >= 35e3 && st.nextAt - T0 <= 70e3);
    assert.equal(st.profileNextAt, st.nextAt);
  }
});
test('cooldown escalates 10 → 20 min, then 3 strikes stops until midnight', () => {
  const st = FL.fresh();
  FL.applyHit(st, T0, null); assert.equal(st.cool.list.until, T0 + 10 * MIN);
  FL.applyHit(st, T0 + HOUR, null); assert.equal(st.cool.list.until, T0 + HOUR + 20 * MIN);
  FL.applyHit(st, T0 + 3 * HOUR, null);
  assert.equal(st.cool.list.until, FL.nextMidnight(T0)); // 10:00 + 3h + 2h < midnight
  assert.equal(st.cool.profile.until, 0); // bios have their own bucket
});
test('cooldown: old hits expire after 24 h, cap 24 h, Retry-After wins when longer', () => {
  const st = FL.fresh();
  FL.applyHit(st, T0, null);
  FL.applyHit(st, T0 + DAY + 1, null);
  assert.equal(st.cool.list.hits.length, 1); assert.equal(st.cool.list.until, T0 + DAY + 1 + 10 * MIN);
  const s2 = FL.fresh(); FL.applyHit(s2, T0, T0 + 5 * HOUR); assert.equal(s2.cool.list.until, T0 + 5 * HOUR);
  const s3 = FL.fresh(); s3.cool.list.hits = [T0 - 2 * HOUR, T0 - 3 * HOUR, T0 - 4 * HOUR, T0 - 5 * HOUR, T0 - 6 * HOUR, T0 - 7 * HOUR, T0 - 8 * HOUR];
  FL.applyHit(s3, T0, null); assert.ok(s3.cool.list.until <= T0 + DAY);
});
test('budget: defaults, server override, reset at local midnight', () => {
  const st = FL.fresh();
  for (let i = 0; i < 150; i++) FL.afterRequest(st, 'profile', T0);
  assert.deepEqual(FL.budgetLeft(st, null, T0), { list: 2000, profile: Infinity }); // default 0 = no daily number
  assert.deepEqual(FL.budgetLeft(st, { profile: 200 }, T0), { list: 2000, profile: 50 });
  assert.deepEqual(FL.budgetLeft(st, { profile: 150 }, T0), { list: 2000, profile: 0 });
  assert.deepEqual(FL.budgetOf({ list: 600, profile: 5000 }), { list: 600, profile: 5000 }); // no hard cap
  assert.deepEqual(FL.budgetLeft(st, { profile: 150 }, FL.nextMidnight(T0) + 1), { list: 2000, profile: 150 });
  // unlimited still paces: the next bio waits the 35-70 s gap
  const p = FL.plan(st, FL.budgetLeft(st, null, T0), T0);
  assert.ok(!p.kinds.includes('profile') && p.wait >= 35e3 - 1 && p.why === 'pace');
  FL.tally(st, T0 + DAY, 25, 1); FL.tally(st, T0 + DAY, 50, 0);
  assert.deepEqual([st.today.people, st.today.bios], [75, 1]);
});
test('outbox: keeps order, stops on offline, drops rejected', async () => {
  let box = FL.enqueue(FL.enqueue(FL.enqueue([], '/a', 1), '/b', 2), '/c', 3);
  const sent = [];
  box = await FL.flush(box, async (it) => (it.path === '/b' ? 'retry' : (sent.push(it.path), 'ok')));
  assert.deepEqual(box.map((x) => x.path), ['/b', '/c']); assert.deepEqual(sent, ['/a']);
  box = await FL.flush(box, async (it) => (it.path === '/b' ? 'drop' : 'ok'));
  assert.deepEqual(box, []);
});
test('statusOf: badge and state', () => {
  const st = FL.fresh();
  assert.deepEqual(FL.statusOf(st, { job: {} }, T0), { state: 'running', text: 'Scraping', badge: '', key: 'run' });
  assert.equal(FL.statusOf({ ...st, nextAt: T0 + 8e3 }, {}, T0).text, 'Next request in 8s');
  assert.equal(FL.statusOf(st, { localPaused: true }, T0).badge, '‖');
  assert.equal(FL.statusOf(st, { noTab: true }, T0).text, 'Open Instagram');
  const cool = (l, p) => ({ ...st, cool: { list: { until: l, hits: [] }, profile: { until: p, hits: [] } } });
  assert.equal(FL.statusOf(cool(T0 + 25 * MIN, T0 + 25 * MIN), {}, T0).badge, '25m');
  assert.equal(FL.statusOf(cool(T0 + 5 * HOUR, T0 + 5 * HOUR), {}, T0).state, 'cooldown');
  assert.equal(FL.statusOf({ ...st, hold: { message: 'x' } }, {}, T0).badge, '!');
});

test('parseBody: prefixes, non-json content, garbage', () => {
  assert.deepEqual(FL.parseBody('for (;;);{"users":[],"status":"ok"}'), { users: [], status: 'ok' });
  assert.deepEqual(FL.parseBody('﻿  {"a":1}'), { a: 1 });
  assert.equal(FL.parseBody('<!DOCTYPE html><html>login</html>'), null);
  assert.equal(FL.parseBody(''), null);
  assert.equal(FL.parseBody('{bad'), null);
});
test('classify/parsePage: alternate list shapes', () => {
  const wrapped = { data: { users: [{ pk: 1, username: 'a' }] }, next_max_id: 'QVF', status: 'ok' };
  assert.equal(FL.classify(res(wrapped), 'list'), null);
  assert.equal(FL.parsePage(wrapped).next_cursor, 'QVF');
  const gql = { data: { user: { edge_followed_by: { count: 9, page_info: { has_next_page: true, end_cursor: 'C1' },
    edges: [{ node: { id: '5', username: 'b' } }] } } }, status: 'ok' };
  assert.equal(FL.classify(res(gql), 'list'), null);
  assert.deepEqual(FL.parsePage(gql), { users: [{ ig_id: '5', handle: 'b', name: '', pic_url: '', is_private: false, is_verified: false }],
    next_cursor: 'C1', done: false, limited: false });
  assert.equal(FL.parsePage({ users: [{ pk: 1, username: 'a' }], next_max_id: 25 }).next_cursor, '25'); // numeric cursor
});
test('classify: redirects to login/challenge, html 200 is other with a sample', () => {
  assert.equal(FL.classify({ status: 200, text: '<html>', json: null, url: 'https://www.instagram.com/accounts/login/?next=x' }, 'list').code, 'login');
  assert.equal(FL.classify({ status: 200, text: '<html>', json: null, url: 'https://www.instagram.com/challenge/abc/' }, 'list').code, 'challenge');
  const r = { status: 200, text: '<!DOCTYPE html>' + 'x'.repeat(3000), json: null, contentType: 'text/html', url: 'https://www.instagram.com/api/v1/friendships/1/followers/' };
  assert.equal(FL.classify(r, 'list').code, 'other');
  const s = FL.sampleOf(r);
  assert.match(s, /^HTTP 200 text\/html https:\/\/www\.instagram\.com\/api\/v1\/friendships\/1\/followers\/ body: <!DOCTYPE html>x/);
  assert.ok(s.length < 1700);
  assert.equal(FL.classify(res({ status: 'ok', big_list: true }), 'list').code, 'other');
});
test('rate: pages/people in last hour and last hit', () => {
  const st = FL.fresh();
  FL.logPage(st, T0 - 2 * HOUR, 50);
  FL.logPage(st, T0 - 10 * MIN, 25);
  FL.logPage(st, T0, 20);
  assert.deepEqual(FL.rateOf(st, T0), { pages_hour: 2, people_hour: 45, bios_hour: 0, requests_hour: 0, hits_24h: 0, last_hit_at: null });
  assert.equal(st.log.length, 2);
  FL.applyHit(st, T0 - 5 * MIN);
  assert.equal(FL.rateOf(st, T0).last_hit_at, new Date(T0 - 5 * MIN).toISOString());
  assert.deepEqual(FL.rateOf({}, T0), { pages_hour: 0, people_hour: 0, bios_hour: 0, requests_hour: 0, hits_24h: 0, last_hit_at: null });
});

test('lanes: stable id format and a start offset inside its range', () => {
  const ids = new Set(Array.from({ length: 50 }, () => FL.newLaneId()));
  assert.equal(ids.size, 50);
  for (const id of ids) assert.match(id, /^ln_[a-z2-9]{12}$/);
  for (let i = 0; i < 50; i++) { const o = FL.startOffset(); assert.ok(o >= FL.START_OFFSET[0] && o <= FL.START_OFFSET[1]); }
});
test('accountFrom: ds_user_id plus the nearest username in the page JSON', () => {
  const page = '{"viewer":{"user":{"id":"4242","username":"fortun8te","full_name":"M"}},"other":{"pk":"1","username":"someone"}}';
  assert.deepEqual(FL.accountFrom('csrftoken=x; ds_user_id=4242; sessionid=y', page), { ig_id: '4242', handle: 'fortun8te' });
  assert.deepEqual(FL.accountFrom('csrftoken=x; ds_user_id=4242', '{"username":"far","x":"' + 'a'.repeat(900) + '","id":"4242"}'), { ig_id: '4242', handle: null });
  assert.deepEqual(FL.accountFrom('csrftoken=x', page), { ig_id: null, handle: null }); // logged out
  assert.equal(FL.handleFrom('{"username":"Mixed.Case_1","pk":"9"}', '9'), 'mixed.case_1');
});
