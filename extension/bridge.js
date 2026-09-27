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
      let root; try { root = JSON.parse(line.replace(/^\s*for\s*\(;;\);/, '')); } catch { continue; }
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
  // Diagnostic samples only, not a complete request ledger. Never store URLs,
  // opaque query values, raw bodies, identities, cursors, cookies or headers.
  // Public operation identifiers are allowlisted. No extra requests are made.
  function nativeMeta(input, method) {
    try {
      const u = new URL(typeof input === 'string' ? input : input.url, location.href);
      if (u.origin !== location.origin) return null;
      const match = u.pathname.match(/^\/api\/v1\/friendships\/\d+\/(followers|following)\/?$/);
      const graphql = /^\/(?:api\/graphql|graphql\/query)\/?$/.test(u.pathname);
      if (!match && !graphql) return null;
      const count = Number(u.searchParams.get('count'));
      const queryKeys = ['count', 'max_id', 'search_surface', 'query_hash', 'doc_id', 'variables', 'fb_api_req_friendly_name'];
      const doc = u.searchParams.get('doc_id'), operation = u.searchParams.get('fb_api_req_friendly_name');
      return { transport: match ? 'rest' : 'graphql', direction: match ? match[1] : null,
        method: String(method || input?.method || 'GET').toUpperCase() === 'POST' ? 'POST' : 'GET',
        query_keys: queryKeys.filter((key) => u.searchParams.has(key)),
        doc_id: /^\d{1,30}$/.test(doc || '') ? doc : null,
        operation: /^Polaris[A-Za-z0-9_]{1,120}Query$/.test(operation || '') ? operation : null,
        requested_count: Number.isInteger(count) && count > 0 && count <= 1000 ? count : null,
        request_cursor: u.searchParams.has('max_id'), has_query_hash: u.searchParams.has('query_hash'),
        has_doc_id: u.searchParams.has('doc_id') };
    } catch { return null; }
  }
  function nativeOperation(meta, body) {
    if (!meta || meta.transport !== 'graphql' || meta.method !== 'POST') return;
    try {
      // Only already-materialized bodies. Never read a Request stream, headers,
      // variables or tokens. Only these two public operation fields may escape.
      let fields = null;
      if (typeof body === 'string' && body.length <= 65536) fields = new URLSearchParams(body);
      else if (typeof URLSearchParams !== 'undefined' && body instanceof URLSearchParams) fields = body;
      else if (typeof FormData !== 'undefined' && body instanceof FormData) fields = body;
      if (!fields) return;
      const doc = fields.get('doc_id'), operation = fields.get('fb_api_req_friendly_name');
      if (typeof doc === 'string' && /^\d{1,30}$/.test(doc)) { meta.doc_id = doc; meta.has_doc_id = true; }
      if (typeof operation === 'string' && /^Polaris[A-Za-z0-9_]{1,120}Query$/.test(operation)) meta.operation = operation;
    } catch {}
  }
  function nativeSample(meta, status, finalUrl, text) {
    if (!meta || typeof text !== 'string' || text.length > 8e6) return;
    let final = 'other';
    try {
      const u = new URL(finalUrl, location.href);
      if (u.origin === location.origin) final = u.pathname === '/' ? 'home' :
        /^\/accounts\/login/.test(u.pathname) ? 'login' :
        /^\/(challenge|checkpoint)/.test(u.pathname) ? 'challenge' : 'api';
    } catch {}
    const emit = (direction, data, schema = 'rest_users') => {
      const users = Array.isArray(data?.users) ? data.users : Array.isArray(data?.edges) ? data.edges : null;
      const more = data?.has_more ?? data?.page_info?.has_next_page;
      window.postMessage({ __fl: 'native-list-diagnostic', sample: { ...meta, direction,
        schema, status: Number.isInteger(status) && status >= 0 && status <= 599 ? status : 0,
        final_route: final, format: /^\s*</.test(text) ? 'html' : users ? 'list' : 'other',
        returned_count: users ? Math.min(users.length, 10000) : null,
        has_more: typeof more === 'boolean' ? more : null,
        response_cursor: !!(data?.next_max_id || data?.page_info?.end_cursor),
        limited: data?.should_limit_list_of_followers === true } }, location.origin);
    };
    let emitted = false;
    for (const line of text.split('\n')) {
      let root; try { root = JSON.parse(line.replace(/^\s*for\s*\(;;\);/, '')); } catch { continue; }
      if (meta.direction) { emit(meta.direction, root); return; }
      const stack = [root];
      for (let n = 0; stack.length && n < 10000; n++) {
        const obj = stack.pop();
        if (!obj || typeof obj !== 'object') continue;
        for (const [key, value] of Object.entries(obj)) {
          const direction = key === 'edge_followed_by' || /friendships.*followers/.test(key) ? 'followers' :
            key === 'edge_follow' || /friendships.*following/.test(key) ? 'following' : null;
          if (direction && value && (Array.isArray(value.edges) || Array.isArray(value.users))) {
            emit(direction, value, key === 'edge_followed_by' || key === 'edge_follow' ? key : 'friendships_' + direction); emitted = true;
          } else if (value && typeof value === 'object') stack.push(value);
        }
      }
    }
    if (meta.direction && !emitted) emit(meta.direction, null);
  }
  const relevant = (u) => { try { const p = new URL(u, location.href); return p.origin === location.origin && /^\/(api\/graphql|graphql\/query|api\/v1\/users\/)/.test(p.pathname); } catch { return false; } };
  window.fetch = function (...args) {
    const meta = nativeMeta(args[0], args[1]?.method);
    try { nativeOperation(meta, args[1]?.body); } catch {}
    return origFetch.apply(this, args).then((res) => {
      if (meta || (res.ok && relevant(res.url))) res.clone().text().then((text) => {
        if (res.ok && (relevant(res.url) || meta?.direction)) scan(text);
        nativeSample(meta, res.status, res.url, text);
      }).catch(() => {});
      return res;
    });
  };
  const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send, want = new WeakSet(), native = new WeakMap();
  XMLHttpRequest.prototype.open = function (m, u, ...rest) { want.delete(this); native.delete(this); if (relevant(u)) want.add(this); const meta = nativeMeta(u, m); if (meta) native.set(this, meta); return open.call(this, m, u, ...rest); };
  XMLHttpRequest.prototype.send = function (...args) {
    nativeOperation(native.get(this), args[0]);
    if (want.has(this) || native.has(this)) this.addEventListener('load', () => { try {
      if ((want.has(this) || native.get(this)?.direction) && this.status === 200) scan(this.responseText);
      nativeSample(native.get(this), this.status, this.responseURL, this.responseText);
    } catch {} }, { once: true });
    return send.apply(this, args);
  };
  const seenScripts = new WeakMap();
  let lateScriptReads = 0;
  const scanScript = (script, late = false) => {
    if (!script?.matches?.('script[type="application/json"]')) return;
    const text = script.textContent;
    if (!text || text.length > 8e6) return;
    let hash = 2166136261;
    for (let i = 0; i < text.length; i++) hash = Math.imul(hash ^ text.charCodeAt(i), 16777619);
    const stamp = text.length + ':' + hash;
    if (seenScripts.get(script) === stamp) return;
    if (late && lateScriptReads >= 200) return;
    if (late) lateScriptReads++;
    seenScripts.set(script, stamp);
    scan(text);
  };
  const scanScripts = () => document.querySelectorAll('script[type="application/json"]').forEach((s) => scanScript(s));
  document.addEventListener('DOMContentLoaded', scanScripts, { once: true });
  window.addEventListener('load', scanScripts, { once: true });
  // Instagram can add complete profile JSON after load. Inspect only added or
  // changed JSON scripts, with a per-document cap; never rescan the whole DOM.
  if (typeof MutationObserver !== 'undefined') {
    const observer = new MutationObserver((records) => {
      let checked = 0;
      for (const record of records) {
        const nodes = record.type === 'characterData' ? [[record.target.parentElement, false]] :
          [[record.target, false], ...Array.from(record.addedNodes, (node) => [node, true])];
        for (const [node, descendants] of nodes) {
          if (++checked > 200) return;
          scanScript(node, true);
          if (descendants && node?.querySelectorAll) {
            for (const script of node.querySelectorAll('script[type="application/json"]')) {
              if (++checked > 200) return;
              scanScript(script, true);
            }
          }
          if (lateScriptReads >= 200) { observer.disconnect(); return; }
        }
      }
    });
    observer.observe(document, { childList: true, subtree: true, characterData: true });
  }
})();
