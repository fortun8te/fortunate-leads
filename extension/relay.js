// Isolated world: forwards profiles found by bridge.js to the service worker (which wakes up for it if asleep).
window.addEventListener('message', (e) => {
  if (e.source !== window || !e.data || e.data.__fl !== 'profile') return;
  try { const p = chrome.runtime.sendMessage({ type: 'fl-profile', user: e.data.user }); if (p && p.catch) p.catch(() => {}); } catch { /* extension reloaded */ }
});
