// Isolated world: forwards profiles found by bridge.js to the service worker (which wakes up for it if asleep).
window.addEventListener('message', (e) => {
  if (e.source !== window || !e.data || e.data.__fl !== 'profile') return;
  try { const p = chrome.runtime.sendMessage({ type: 'fl-profile', user: e.data.user }); if (p && p.catch) p.catch(() => {}); } catch { /* extension reloaded */ }
});

// A small local diagnostic ring, separate from collected profiles and coverage.
// Rebuild the whitelist here: page scripts can also post window messages.
let nativeDiagnosticWrite = Promise.resolve();
let nativeDiagnosticAt = 0;
window.addEventListener('message', (e) => {
  if (e.source !== window || e.origin !== location.origin || e.data?.__fl !== 'native-list-diagnostic') return;
  const p = e.data.sample;
  if (!p || !['followers', 'following'].includes(p.direction) || !['rest', 'graphql'].includes(p.transport)) return;
  const now = Date.now();
  if (now - nativeDiagnosticAt < 1000) return; // samples, never a complete request ledger
  nativeDiagnosticAt = now;
  const count = (n, max) => Number.isInteger(n) && n >= 0 && n <= max ? n : null;
  const sample = { at: new Date(now).toISOString(), source: 'passive_page_sample',
    query_keys: ['count', 'max_id', 'search_surface', 'query_hash', 'doc_id', 'variables', 'fb_api_req_friendly_name']
      .filter((key) => Array.isArray(p.query_keys) && p.query_keys.includes(key)),
    doc_id: typeof p.doc_id === 'string' && /^\d{1,30}$/.test(p.doc_id) ? p.doc_id : null,
    operation: typeof p.operation === 'string' && /^Polaris[A-Za-z0-9_]{1,120}Query$/.test(p.operation) ? p.operation : null,
    schema: ['rest_users', 'edge_followed_by', 'edge_follow', 'friendships_followers', 'friendships_following'].includes(p.schema) ? p.schema : 'unknown',
    transport: p.transport, direction: p.direction, method: p.method === 'POST' ? 'POST' : 'GET',
    status: count(p.status, 599), requested_count: count(p.requested_count, 1000),
    returned_count: count(p.returned_count, 10000),
    final_route: ['home', 'login', 'challenge', 'api'].includes(p.final_route) ? p.final_route : 'other',
    format: ['html', 'list'].includes(p.format) ? p.format : 'other',
    request_cursor: p.request_cursor === true, response_cursor: p.response_cursor === true,
    has_query_hash: p.has_query_hash === true, has_doc_id: p.has_doc_id === true,
    has_more: typeof p.has_more === 'boolean' ? p.has_more : null, limited: p.limited === true };
  nativeDiagnosticWrite = nativeDiagnosticWrite.then(async () => {
    const saved = await chrome.storage.local.get('nativeListDiagnostics');
    const previous = Array.isArray(saved.nativeListDiagnostics) ? saved.nativeListDiagnostics : [];
    await chrome.storage.local.set({ nativeListDiagnostics: previous.concat(sample).slice(-30) });
  }).catch(() => {});
});
