// Isolated world: forwards profiles found by bridge.js to the service worker.
window.addEventListener('message', (e) => {
  if (e.source !== window || !e.data || e.data.__fl !== 'profile') return;
  try { chrome.runtime.sendMessage({ type: 'fl-profile', user: e.data.user }); } catch { /* extension reloaded */ }
});
