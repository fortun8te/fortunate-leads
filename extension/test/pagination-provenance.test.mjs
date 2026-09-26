import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import FL from '../lib/core.js';

const user = {pk: 1, username: 'alice'};
const classify = json => FL.classify({status: 200, json}, 'list');

test('wrapped v1 pagination retains continuation and respects explicit end and limit', () => {
  const json = {status: 'ok', data: {users: [user], has_more: true, next_max_id: 'next'}};
  assert.equal(classify(json), null);
  assert.equal(FL.parsePage(json).next_cursor, 'next');
  assert.equal(FL.parsePage(json).done, false);
  json.data.has_more = false;
  assert.equal(FL.parsePage(json).done, true);
  json.data.has_more = true;
  json.data.should_limit_list_of_followers = true;
  assert.equal(FL.parsePage(json).limited, true);
  assert.equal(FL.parsePage(json).done, true);
});

test('every supported pagination shape rejects a promised page without its cursor', () => {
  for (const json of [
    {users: [user], has_more: true},
    {data: {users: [user], has_more: true}},
    {data: {user: {edge_follow: {edges: [{node: user}], page_info: {has_next_page: true}}}}},
    {data: {user: {edge_followed_by: {edges: [{node: user}], page_info: {has_next_page: true, end_cursor: ''}}}}},
  ]) assert.equal(classify(json)?.reason, 'missing_cursor');
});

test('empty GraphQL and wrapped pages promising more remain soft blocks', () => {
  for (const json of [
    {data: {users: [], has_more: true}},
    {data: {user: {edge_follow: {edges: [], page_info: {has_next_page: true}}}}},
  ]) assert.equal(classify(json)?.reason, 'empty_page_with_more');
});

async function collectedSource({cached = null, prog = null, lookup = null, cursor = null, id = 9} = {}) {
  const data = {ids: cached ? {seed: cached} : {}, prog: prog ? {'seed/followers': prog} : {}};
  const context = vm.createContext({FL, Date, Set, URLSearchParams, AbortController, importScripts() {}, setTimeout, clearTimeout,
    chrome: {runtime: {getManifest: () => ({version: 'test'}), onMessage: {addListener() {}}},
      storage: {local: {get: async k => ({[k]: data[k]}), set: async x => Object.assign(data, x)}}}});
  const source = fs.readFileSync(new URL('../background.js', import.meta.url), 'utf8').split('// ---- lifecycle')[0];
  vm.runInContext(source, context);
  context.fixture = {lookup, cursor, id};
  await vm.runInContext(`(async () => {
    igRequest = async () => ({res: {json: {users: [{pk: 1, username: 'alice'}], has_more: false}}, bad: null});
    lookupViaPage = async () => ({p: fixture.lookup});
    waitUntil = async () => true;
    queueDone = async (path, body) => { globalThis.result = body; };
    await runList(mem.gen, {id: fixture.id, seed: 'seed', direction: 'followers', cursor: fixture.cursor, received: 0}, {id: 1});
  })()`, context);
  return {result: context.result, data};
}

test('cached counts cannot authorize current-run coverage', async () => {
  const {result} = await collectedSource({cached: {ig_id: '12', followers: 100}, cursor: 'next'});
  assert.equal(result.total, 100);
  assert.equal(result.total_source, 'cached');
});

test('fresh lookup count provenance survives later pages of the same run only', async () => {
  const {result, data} = await collectedSource({lookup: {ig_id: '12', handle: 'seed', followers: 100}});
  assert.equal(result.total_source, 'current_run');
  const prog = {...data.prog['seed/followers'], next: 'next'};
  const same = await collectedSource({cached: {ig_id: '12', followers: 90}, prog, cursor: 'next'});
  assert.equal(same.result.total, 100);
  assert.equal(same.result.total_source, 'current_run');
  const fresh = await collectedSource({cached: {ig_id: '12', followers: 90}, prog, id: 10, lookup: {ig_id: '12', handle: 'seed', followers: 80}});
  assert.equal(fresh.result.total, 80);
  assert.equal(fresh.result.total_source, 'current_run');
});

test('legacy progress counts are cached, and missing fresh counts remain unknown', async () => {
  const old = await collectedSource({cached: {ig_id: '12'}, prog: {jobId: 9, total: 100}, cursor: 'next'});
  assert.equal(old.result.total_source, 'cached');
  const unknown = await collectedSource({lookup: {ig_id: '12', handle: 'seed', followers: null}});
  assert.equal(unknown.result.total, null);
  assert.equal(unknown.result.total_source, 'unknown');
});

test('malformed continuation flags and cursors cannot manufacture an end or next page', () => {
  for (const has_more of ['false', 'true', 0, 1, {}]) {
    assert.equal(classify({data: {users: [user], has_more}})?.reason, 'invalid_has_more');
  }
  for (const next_max_id of [{bad: true}, [], true, -1, Infinity]) {
    assert.equal(classify({data: {users: [user], has_more: true, next_max_id}})?.reason, 'invalid_cursor');
  }
  assert.equal(classify({users: [user], has_more: true, next_max_id: '  '})?.reason, 'missing_cursor');
});


test('a cached seed ID still refreshes the count on the first page', async () => {
  const {result, data} = await collectedSource({cached: {ig_id: '12', followers: 100},
    lookup: {ig_id: '12', handle: 'seed', followers: 120}});
  assert.equal(result.total, 120);
  assert.equal(result.total_source, 'current_run');
  assert.equal(data.prog['seed/followers'].countAttempted, true);
});
