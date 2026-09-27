// Offline experiment only. Not imported by background.js and not enabled in
// manifest.json. A caller must hold the normal request permit before open().
// Chrome's declarativeNetRequest permission is needed to block CDN resources;
// WithHostAccess alone cannot block CDN hosts absent from host_permissions.
export const RESOURCE_TYPES = Object.freeze(['image', 'media', 'font']);

export function profileResourceRule(tabId, ruleId) {
  if (!Number.isSafeInteger(tabId) || tabId < 0 || !Number.isSafeInteger(ruleId) || ruleId < 1)
    throw new TypeError('An owned tab ID and reserved positive rule ID are required');
  return {id: ruleId, priority: 1, action: {type: 'block'},
    condition: {tabIds: [tabId], resourceTypes: [...RESOURCE_TYPES]}};
}

// Owns exactly the tab it creates. Controls and generation must be checked
// immediately before navigation through assertCurrent; no permit is acquired,
// renewed, released or retried here. A failed close must retain the caller's
// permit exactly as lookupViaPage currently does.
export async function openProfileTab({chrome, url, ruleId, assertCurrent, near}) {
  const target = new URL(url);
  if (target.origin !== 'https://www.instagram.com' || target.username || target.password || target.search || target.hash ||
      !/^\/[a-zA-Z0-9._]{1,30}\/$/.test(target.pathname))
    throw new TypeError('Only a normal Instagram profile URL is allowed');
  profileResourceRule(0, ruleId);
  if (typeof assertCurrent !== 'function' || !chrome.declarativeNetRequest?.updateSessionRules)
    throw new Error('Resource experiment unavailable');
  await assertCurrent();
  const where = near ? {windowId: near.windowId, index: near.index + 1} : {};
  const tab = await chrome.tabs.create({url: 'about:blank', active: false, ...where});
  let closed = false, installed = false;
  async function close() {
    // Do not remove the resource guard until the owned page has stopped.
    if (!closed) { await chrome.tabs.remove(tab.id); closed = true; }
    if (installed) {
      await chrome.declarativeNetRequest.updateSessionRules({removeRuleIds: [ruleId]});
      installed = false;
    }
    return {closed};
  }
  try {
    // addRules cannot overwrite an existing rule: collision fails without
    // changing another tab. The caller reserves the ID for this experiment.
    await chrome.declarativeNetRequest.updateSessionRules({addRules: [profileResourceRule(tab.id, ruleId)]});
    installed = true;
    await assertCurrent();
    await chrome.tabs.update(tab.id, {url});
    return {tab, close};
  } catch (cause) {
    try { await close(); }
    catch (cleanupError) {
      throw Object.assign(new Error('Profile experiment cleanup unconfirmed', {cause}),
        {tabId: tab.id, closed, cleanupError});
    }
    throw cause;
  }
}

// Pure HAR replay. No URLs, headers, cookies or response bodies escape in the
// report. Transfer savings are an upper bound: cache/service workers and
// browser request ordering need a subsequent real paired capture.
export function resourceBudget(entries) {
  const result = {requests: 0, blockedRequests: 0, transferBytes: 0,
    avoidableTransferBytes: 0, unknownTransferSizes: 0, preservedDataRequests: 0};
  for (const entry of entries) {
    const type = String(entry._resourceType || '').toLowerCase();
    const blocked = RESOURCE_TYPES.includes(type);
    const raw = entry.response?._transferSize;
    const known = Number.isFinite(raw) && raw >= 0;
    result.requests++;
    if (blocked) result.blockedRequests++;
    if (!known) result.unknownTransferSizes++;
    else {
      result.transferBytes += raw;
      if (blocked) result.avoidableTransferBytes += raw;
    }
    if (['document', 'script', 'xhr', 'fetch', 'stylesheet'].includes(type)) result.preservedDataRequests++;
  }
  return {...result, remainingTransferBytes: result.transferBytes - result.avoidableTransferBytes};
}
