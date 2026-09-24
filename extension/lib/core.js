// Pure logic shared by the service worker (importScripts) and node tests (import).
(function (root) {
  const MIN = 60e3, HOUR = 60 * MIN, DAY = 24 * HOUR;
  const PACE = {
    listGap: [7e3, 12e3], breakEvery: [40, 60], breakLen: [90e3, 180e3],
    profileGap: [35e3, 70e3], cooldownBase: 10 * MIN, cooldownCap: DAY, strikes: 3,
  };
  const BUDGET = { list: 2000, profile: 150 }, PROFILE_CAP = 300; // pages/day, reads/day; profile hard cap
  const rand = (lo, hi, r = Math.random) => Math.round(lo + (hi - lo) * r());

  // ---- Instagram response parsing & classification ----------------------
  // Tolerant: strips the `for (;;);` anti-JSON-hijack prefix and ignores content-type.
  function parseBody(text) {
    const t = String(text || '').replace(/^\uFEFF/, '').trim().replace(/^\s*(for\s*\(;;\);|while\s*\(1\);|\)\]\}',?)\s*/, '');
    if (!/^[{[]/.test(t)) return null;
    try { const j = JSON.parse(t); return j && typeof j === 'object' ? j : null; } catch { return null; }
  }
  // Users array from v1 (`users`), wrapped v1 (`data.users`) or GraphQL edges (`data.user.edge_followed_by.edges`).
  function usersOf(json) {
    if (!json || typeof json !== 'object') return null;
    if (Array.isArray(json.users)) return json.users;
    if (Array.isArray(json.data?.users)) return json.data.users;
    const u = json.data?.user || json.user, e = u && (u.edge_followed_by || u.edge_follow);
    return e && Array.isArray(e.edges) ? e.edges.map((x) => x && x.node) : null;
  }
  // Short, loggable description of an unusable response so the owner sees what Instagram actually sent.
  const sampleOf = (res, n = 1500) => 'HTTP ' + (Number(res.status) || 0) + ' ' + (res.contentType || '?') +
    (res.url ? ' ' + res.url : '') + (res.redirected ? ' (redirected)' : '') + ' body: ' + String(res.text || '').slice(0, n).replace(/\s+/g, ' ');
  // res = {status, json, text, retryAfter}. Returns {code, retryAt} or null when the response is usable.
  function classify(res, kind, now = Date.now()) {
    const status = Number(res.status) || 0, json = res.json;
    const ok = status >= 200 && status < 300;
    const text = (ok && json ? [json.message, json.error_title, json.error_type, typeof json.error === 'string' ? json.error : '',
      json.checkpoint_url ? 'checkpoint_required' : ''].join(' ') : String(res.text || '')).slice(0, 2000).toLowerCase();
    const ra = res.retryAfter, n = Number(ra);
    const retryAt = ra ? (Number.isFinite(n) ? now + n * 1000 : Date.parse(ra) || null) : null;
    const out = (code) => ({ code, retryAt });
    const path = (() => { try { return new URL(res.url).pathname; } catch { return ''; } })();
    if (/^\/challenge\//.test(path)) return out('challenge');
    if (/^\/accounts\/login/.test(path)) return out('login');
    if (/checkpoint_required|challenge_required|\/challenge\//.test(text)) return out('challenge');
    if (status === 429 || /please wait a few minutes|rate.?limit|too many requests/.test(text)) return out('rate_limit');
    if (/feedback_required|spam/.test(text)) return out('soft_block');
    if (status === 401 || /login_required/.test(text) || (json && json.require_login)) return out('login');
    if (status === 404 || (json && /user not found|not.found/.test(text))) return out('not_found');
    if (json && /not authorized to view|private/.test(text)) return out('private');
    if (!ok) return out('other');
    if (!json) return out(/login|password/.test(text) ? 'login' : 'other');
    if (json.status && json.status !== 'ok') return out('soft_block');
    if (kind === 'list') {
      const users = usersOf(json);
      if (!users) return out('other');
      if (!users.length && (json.has_more || json.next_max_id)) return out('soft_block');
    }
    return null;
  }

  // ---- Mapping to contract fields ---------------------------------------
  function mapUser(u) {
    if (!u || typeof u !== 'object' || !u.username) return null;
    return { ig_id: String(u.pk || u.pk_id || u.id || '') || null, handle: String(u.username), name: u.full_name || '',
      pic_url: u.profile_pic_url || '', is_private: !!u.is_private, is_verified: !!u.is_verified };
  }

  function parsePage(json) {
    const users = (usersOf(json) || []).map(mapUser).filter(Boolean);
    const pi = json.data?.user?.edge_followed_by?.page_info || json.data?.user?.edge_follow?.page_info;
    const cur = json.next_max_id ?? (pi && pi.has_next_page ? pi.end_cursor : null);
    const next = json.has_more !== false && cur != null && cur !== '' && users.length ? String(cur) : null;
    const limited = !!json.should_limit_list_of_followers;
    return { users, next_cursor: limited ? null : next, done: limited || !next, limited };
  }

  const count = (v) => (v == null || v === '' || !Number.isFinite(Number(v)) ? null : Math.floor(Number(v)));
  function mapProfile(u) {
    if (!u || typeof u !== 'object' || !u.username) return null;
    const link = (Array.isArray(u.bio_links) ? u.bio_links : []).map((l) => l && (l.url || l.lynx_url)).find(Boolean);
    return {
      ig_id: String(u.pk || u.pk_id || u.id || '') || null, handle: String(u.username), name: u.full_name || '',
      bio: typeof u.biography === 'string' ? u.biography : null, website: u.external_url || link || null,
      category: u.category || u.category_name || u.business_category_name || null,
      followers: count(u.follower_count ?? u.edge_followed_by?.count), following: count(u.following_count ?? u.edge_follow?.count),
      posts: count(u.media_count ?? u.edge_owner_to_timeline_media?.count),
      is_private: !!u.is_private, is_verified: !!u.is_verified,
      is_business: !!(u.is_business || u.is_business_account || u.account_type === 2),
      pic_url: u.hd_profile_pic_url_info?.url || u.profile_pic_url_hd || u.profile_pic_url || null,
    };
  }
  // Pulls the user object out of either /users/{id}/info/ or web_profile_info.
  const userOf = (json) => (json && (json.user || json.data?.user)) || null;

  // ---- Pacing, budgets, cooldowns (state lives in chrome.storage.local) ----
  const dayKey = (t) => { const d = new Date(t); return d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate(); };
  const nextMidnight = (t) => { const d = new Date(t); d.setHours(24, 0, 0, 0); return d.getTime(); };

  function fresh() {
    return { day: '', today: { list: 0, profile: 0, people: 0, bios: 0 }, nextAt: 0, profileNextAt: 0, pages: 0, breakEvery: 25,
      cooldownUntil: 0, hits: [], hold: null, lastError: null, log: [] };
  }
  function rollDay(st, now) {
    const k = dayKey(now);
    if (st.day !== k) { st.day = k; st.today = { list: 0, profile: 0, people: 0, bios: 0 }; }
    return st;
  }
  // Called after every Instagram request of `kind` ('list' | 'profile').
  function afterRequest(st, kind, now, r = Math.random) {
    rollDay(st, now);
    st.today[kind] = (st.today[kind] || 0) + 1;
    if (kind === 'profile') {
      st.nextAt = now + rand(...PACE.profileGap, r);
      st.profileNextAt = st.nextAt;
      return st;
    }
    st.nextAt = now + rand(...PACE.listGap, r);
    if (++st.pages >= st.breakEvery) {
      st.nextAt += rand(...PACE.breakLen, r);
      st.pages = 0; st.breakEvery = rand(...PACE.breakEvery, r);
    }
    return st;
  }
  // rate_limit / soft_block: escalate 30 min, doubling per hit in 24 h, cap 24 h; 3 hits = done for today.
  function applyHit(st, now, retryAt) {
    st.hits = (st.hits || []).filter((t) => now - t < DAY).concat(now);
    const n = st.hits.length;
    let until = now + Math.min(PACE.cooldownBase * 2 ** (n - 1), PACE.cooldownCap);
    if (retryAt && retryAt > until) until = retryAt;
    if (n >= PACE.strikes) until = Math.max(until, nextMidnight(now));
    st.cooldownUntil = Math.max(st.cooldownUntil || 0, until);
    return st;
  }
  function budgetOf(budget) {
    const b = { ...BUDGET, ...(budget || {}) };
    return { list: Math.max(0, Number(b.list) || 0), profile: Math.min(PROFILE_CAP, Math.max(0, Number(b.profile) || 0)) };
  }
  // Rows gained today, for the popup ("N people · M bios").
  function tally(st, now, people, bios) {
    rollDay(st, now);
    st.today.people = (st.today.people || 0) + people; st.today.bios = (st.today.bios || 0) + bios;
    return st;
  }
  // Soak-test rate: list pages and people in the last hour, plus the last rate_limit/soft_block hit.
  function logPage(st, now, people) {
    st.log = (st.log || []).filter((e) => now - e[0] < HOUR).concat([[now, people]]);
    return st;
  }
  function rateOf(st, now) {
    const log = (st.log || []).filter((e) => now - e[0] < HOUR), hits = st.hits || [];
    return { pages_hour: log.length, people_hour: log.reduce((a, e) => a + (e[1] || 0), 0),
      last_hit_at: hits.length ? new Date(Math.max(...hits)).toISOString() : null };
  }
  function budgetLeft(st, budget, now) {
    rollDay(st, now);
    const b = budgetOf(budget);
    return { list: Math.max(0, b.list - st.today.list), profile: Math.max(0, b.profile - st.today.profile) };
  }

  // ---- Outbox: every result is queued first, then flushed in order -------
  const enqueue = (box, path, body) => (box || []).concat({ path, body });
  // send(item) → 'ok' | 'drop' (server rejected; never retry) | 'retry' (offline). Returns remaining items.
  async function flush(box, send) {
    const rest = (box || []).slice();
    while (rest.length) {
      const r = await send(rest[0]);
      if (r === 'retry') break;
      rest.shift();
    }
    return rest;
  }

  // ---- Status for popup, badge and heartbeat -----------------------------
  function statusOf(st, ctx, now) {
    const t = (ms) => new Date(ms).toTimeString().slice(0, 5);
    if (st.hold) return { state: 'paused', text: 'Needs attention: ' + st.hold.message, badge: '!' };
    if (ctx.localPaused) return { state: 'paused', text: 'Paused', badge: '‖' };
    if (ctx.serverPaused) return { state: 'paused', text: 'Paused in workspace', badge: '‖' };
    if (st.cooldownUntil > now) {
      const m = Math.ceil((st.cooldownUntil - now) / MIN);
      return { state: 'cooldown', text: 'Cooldown until ' + t(st.cooldownUntil), badge: m >= 60 ? Math.ceil(m / 60) + 'h' : m + 'm' };
    }
    if (ctx.offline) return { state: 'idle', text: 'Server offline', badge: '!' };
    if (ctx.noTab) return { state: 'idle', text: 'Open Instagram', badge: '!' };
    if (ctx.budgetDone) return { state: 'idle', text: 'Daily budget reached', badge: '' };
    if (ctx.job) return { state: 'running', text: 'Scraping', badge: '' };
    if (st.nextAt > now) return { state: 'running', text: 'Next request in ' + Math.ceil((st.nextAt - now) / 1e3) + 's', badge: '' };
    return { state: 'idle', text: 'Idle, queue empty', badge: '' };
  }

  const api = { PACE, BUDGET, PROFILE_CAP, budgetOf, tally, MIN, HOUR, DAY, classify, parseBody, usersOf, sampleOf, logPage, rateOf, mapUser, parsePage, mapProfile, userOf, dayKey, nextMidnight,
    fresh, rollDay, afterRequest, applyHit, budgetLeft, enqueue, flush, statusOf };
  root.FL = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
