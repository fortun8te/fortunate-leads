// MAIN world, document_start: passively reads profiles Instagram already loads while Michael browses. Zero extra requests.
(() => {
  if (window.__flBridge) return;
  Object.defineProperty(window, '__flBridge', { value: true });
  const origFetch = window.fetch;
  Object.defineProperty(window, '__flFetch', { value: origFetch }); // extension's own requests skip the hook
  const KEYS = ['pk', 'pk_id', 'id', 'username', 'full_name', 'biography', 'external_url', 'bio_links', 'category', 'category_name',
    'business_category_name', 'follower_count', 'following_count', 'media_count', 'edge_followed_by', 'edge_follow',
    'edge_owner_to_timeline_media', 'is_private', 'is_verified', 'is_business', 'is_business_account', 'account_type',
    'hd_profile_pic_url_info', 'profile_pic_url_hd', 'profile_pic_url'];
  function scan(text) {
    if (typeof text !== 'string' || text.length > 8e6 || !text.includes('biography')) return;
    for (const line of text.split('\n')) {
      let root; try { root = JSON.parse(line); } catch { continue; }
      const stack = [root];
      for (let n = 0; stack.length && n < 30000; n++) {
        const o = stack.pop();
        if (!o || typeof o !== 'object') continue;
        if (typeof o.username === 'string' && typeof o.biography === 'string' && (o.pk || o.id)) {
          const user = {};
          for (const k of KEYS) if (o[k] !== undefined) user[k] = k.startsWith('edge_') ? { count: o[k] && o[k].count } : o[k];
          window.postMessage({ __fl: 'profile', user }, location.origin);
        }
        for (const k in o) if (o[k] && typeof o[k] === 'object') stack.push(o[k]);
      }
    }
  }
  const relevant = (u) => { try { const p = new URL(u, location.href); return p.origin === location.origin && /^\/(api\/graphql|graphql\/query|api\/v1\/users\/)/.test(p.pathname); } catch { return false; } };
  window.fetch = function (...args) {
    return origFetch.apply(this, args).then((res) => {
      if (res.ok && relevant(res.url)) res.clone().text().then(scan).catch(() => {});
      return res;
    });
  };
  const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send, want = new WeakSet();
  XMLHttpRequest.prototype.open = function (m, u, ...rest) { if (relevant(u)) want.add(this); return open.call(this, m, u, ...rest); };
  XMLHttpRequest.prototype.send = function (...args) {
    if (want.has(this)) this.addEventListener('load', () => { try { if (this.status === 200) scan(this.responseText); } catch {} }, { once: true });
    return send.apply(this, args);
  };
  const seenScripts = new WeakSet();
  const scanScripts = () => document.querySelectorAll('script[type="application/json"]').forEach((s) => {
    if (!seenScripts.has(s)) { seenScripts.add(s); scan(s.textContent); }
  });
  document.addEventListener('DOMContentLoaded', scanScripts, { once: true });
  window.addEventListener('load', scanScripts, { once: true });
})();
