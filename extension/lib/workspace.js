// User-initiated navigation in this extension's Chrome profile. No collection or message reads.
async function openWorkspaceInstagram(chrome, message) {
  const home = 'https://www.instagram.com/';
  const urls = {home, inbox: home + 'direct/inbox/'};
  const url = urls[message.destination];
  if (!url) throw new Error('Choose Instagram or Inbox.');
  const stored = await chrome.storage.local.get(['laneId']);
  if (message.expected_lane_id && message.expected_lane_id !== stored.laneId)
    throw new Error('Open this workspace in the Chrome profile connected to your main account.');
  const cookie = await chrome.cookies.get({url: home, name: 'ds_user_id'});
  const accountId = cookie?.value || null;
  if (message.expected_account_id && String(message.expected_account_id) !== accountId)
    throw new Error('This Chrome profile is signed in to a different account or is signed out.');
  if (message.destination === 'inbox' && (!message.expected_account_id || !accountId))
    throw new Error('Connect your main Instagram account before opening its Inbox.');
  const tabs = await chrome.tabs.query({url: home + '*'});
  const existing = tabs.find(tab => !tab.discarded && (message.destination === 'home' || tab.url === url));
  // Reuse an Inbox already open; otherwise leave the collector's tab where it is.
  const tab = existing ? await chrome.tabs.update(existing.id, {active: true}) : await chrome.tabs.create({url, active: true});
  if (tab.windowId != null) await chrome.windows.update(tab.windowId, {focused: true});
  return {ok: true, opened: message.destination, account_verified: !!message.expected_account_id};
}
if (typeof module !== 'undefined') module.exports = {openWorkspaceInstagram};
