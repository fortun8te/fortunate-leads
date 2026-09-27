import test from 'node:test';
import assert from 'node:assert/strict';
import {profileResourceRule, openProfileTab, resourceBudget} from '../experiments/profile-page-budget.mjs';

function harness({denyAt = 0, addFails = false, closeFails = false, cleanupFails = false} = {}) {
  const calls = []; let checks = 0;
  const options = {url: 'https://www.instagram.com/target/', ruleId: 9000319,
    assertCurrent: async () => {calls.push('control'); if (++checks === denyAt) throw Error('paused');},
    chrome: {tabs: {
      create: async data => {calls.push(['create', data]); return {id: 17};},
      update: async (id, data) => {calls.push(['navigate', id, data]);},
      remove: async id => {calls.push(['close', id]); if (closeFails) throw Error('tab remains');},
    }, declarativeNetRequest: {updateSessionRules: async data => {
      calls.push(['rules', data]);
      if (data.addRules && addFails) throw Error('rule collision');
      if (data.removeRuleIds && cleanupFails) throw Error('rule cleanup failed');
    }}}};
  return {calls, options};
}

test('only images, media and fonts in the owned tab are eligible for blocking', () => {
  const r = profileResourceRule(17, 9000319);
  assert.deepEqual(r.condition, {tabIds: [17], resourceTypes: ['image', 'media', 'font']});
  assert.equal(r.action.type, 'block');
  for (const tab of [-1, NaN, 1.5]) assert.throws(() => profileResourceRule(tab, 1));
  for (const id of [0, -1, 1.5]) assert.throws(() => profileResourceRule(17, id));
});

test('guard installation precedes any Instagram navigation and closure precedes cleanup', async () => {
  const h = harness(); const opened = await openProfileTab(h.options);
  assert.deepEqual(h.calls.map(c => Array.isArray(c) ? c[0] : c), ['control', 'create', 'rules', 'control', 'navigate']);
  assert.deepEqual(h.calls[1][1], {url: 'about:blank', active: false});
  assert.deepEqual(h.calls[2][1].addRules[0].condition.tabIds, [17]);
  await opened.close(); await opened.close();
  assert.deepEqual(h.calls.slice(-2), [['close', 17], ['rules', {removeRuleIds: [9000319]}]]);
});

test('missing capability and initial hold do not open a tab', async () => {
  const h = harness({denyAt: 1});
  await assert.rejects(openProfileTab(h.options), /paused/);
  assert.deepEqual(h.calls, ['control']);
  delete h.options.chrome.declarativeNetRequest;
  await assert.rejects(openProfileTab(h.options), /unavailable/);
  assert.deepEqual(h.calls, ['control']);
});

test('a hold arriving during rule setup closes the blank tab without navigating', async () => {
  const h = harness({denyAt: 2});
  await assert.rejects(openProfileTab(h.options), /paused/);
  assert.equal(h.calls.some(c => c[0] === 'navigate'), false);
  assert.deepEqual(h.calls.slice(-2), [['close', 17], ['rules', {removeRuleIds: [9000319]}]]);
});

test('rule collision closes only the new blank tab and does not remove the existing rule', async () => {
  const h = harness({addFails: true});
  await assert.rejects(openProfileTab(h.options), /rule collision/);
  assert.equal(h.calls.some(c => c[0] === 'navigate'), false);
  assert.equal(h.calls.filter(c => c[0] === 'rules').length, 1);
  assert.deepEqual(h.calls.at(-1), ['close', 17]);
});

test('failed close leaves the guard in place and signals uncertainty to the permit owner', async () => {
  const h = harness({closeFails: true}); const opened = await openProfileTab(h.options);
  await assert.rejects(opened.close(), /tab remains/);
  assert.equal(h.calls.filter(c => c[0] === 'rules').length, 1);
  const cancelled = harness({denyAt: 2, closeFails: true});
  await assert.rejects(openProfileTab(cancelled.options), e => e.tabId === 17 && e.closed === false);
});

test('non-profile URLs are rejected before browser operations', async () => {
  for (const url of ['https://www.instagram.com/accounts/login/', 'https://evil.example/target/',
    'http://www.instagram.com/target/', 'https://www.instagram.com/target/?next=x']) {
    const h = harness();
    await assert.rejects(openProfileTab({...h.options, url})); assert.deepEqual(h.calls, []);
  }
});

test('HAR replay preserves documents, scripts, CSS and all API payloads', () => {
  const resources = ['document', 'script', 'stylesheet', 'xhr', 'fetch', 'image', 'media', 'font', 'other'];
  const report = resourceBudget(resources.map(_resourceType => ({_resourceType, response: {_transferSize: 100}})));
  assert.equal(report.blockedRequests, 3); assert.equal(report.avoidableTransferBytes, 300);
  assert.equal(report.preservedDataRequests, 5); assert.equal(report.remainingTransferBytes, 600);
});

test('cache hits and unknown sizes cannot fabricate byte savings', () => {
  const report = resourceBudget([{_resourceType: 'image', response: {_transferSize: 0}},
    {_resourceType: 'image', response: {_transferSize: -1, bodySize: 2000000}},
    {_resourceType: 'media', response: {bodySize: 9000000}}]);
  assert.equal(report.avoidableTransferBytes, 0); assert.equal(report.unknownTransferSizes, 2);
});
