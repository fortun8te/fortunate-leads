// Pure logic shared by the service worker (importScripts) and node tests (import).
(function (root) {
  const MIN = 60e3, HOUR = 60 * MIN, DAY = 24 * HOUR;
  // Lists and bios keep their own clocks (listNextAt, profileNextAt), so bios fill the list gaps and breaks; nextAt is the
  // floor for any request (a short spacing after each one, hit pauses, backoffs, start offset). A sliding window caps the
  // combined rate per account.
  const PACE = {
    listGap: [7e3, 12e3], breakEvery: [40, 60], breakLen: [90e3, 180e3],
    profileGap: [35e3, 70e3], spacing: [2e3, 5e3], window: 11 * MIN, windowMax: 72,
    cooldownBase: 10 * MIN, cooldownCap: 6 * HOUR, strikes: 3, strikePause: 2 * HOUR,
    netBase: 30e3, netCap: 10 * MIN,   // tab / network trouble: local backoff, never counted as an Instagram limit
    otherBase: 2 * MIN, otherCap: 5 * MIN, // target retries are delayed on the server; keep unrelated work moving
  };
  const KINDS = ['list', 'profile'];
  // Per account per day: list pages, profile reads. profile 0 = no daily number (paced only by the gaps and the window).
  const BUDGET = { list: 3000, profile: 300 };
  const BOX_MAX = 3000;
  const rand = (lo, hi, r = Math.random) => Math.round(lo + (hi - lo) * r());

  // ---- Instagram response parsing & classification ----------------------
  // Tolerant: strips the `for (;;);` anti-JSON-hijack prefix and ignores content-type.
  function parseBody(text) {
    const t = String(text || '').replace(/^﻿/, '').trim().replace(/^\s*(for\s*\(;;\);|while\s*\(1\);|\)\]\}',?)\s*/, '');
    if (!/^[{[]/.test(t)) return null;
    try { const j = JSON.parse(t); return j && typeof j === 'object' ? j : null; } catch { return null; }
  }
  const edgeOf = (json) => { const u = json && (json.data?.user || json.user); return u && (u.edge_followed_by || u.edge_follow) || null; };
  // Users array from v1 (`users`), wrapped v1 (`data.users`) or GraphQL edges (`data.user.edge_followed_by.edges`).
  function usersOf(json) {
    if (!json || typeof json !== 'object') return null;
    if (Array.isArray(json.users)) return json.users;
    if (Array.isArray(json.data?.users)) return json.data.users;
    const e = edgeOf(json);
    return e && Array.isArray(e.edges) ? e.edges.map((x) => x && x.node) : null;
  }
  // Wrapped v1 pages carry their continuation fields beside data.users.
  const listEnvelope = (json) => Array.isArray(json?.users) ? json : Array.isArray(json?.data?.users) ? json.data : json;
  const listField = (json, key) => {
    const page = listEnvelope(json);
    return page?.[key] !== undefined ? page[key] : json?.[key];
  };
  const moreOf = (json) => listField(json, 'has_more') ?? edgeOf(json)?.page_info?.has_next_page;
  const limitedOf = (json) => !!listField(json, 'should_limit_list_of_followers');
  const rawCursorOf = (json) => listField(json, 'next_max_id') ?? edgeOf(json)?.page_info?.end_cursor;
  const validCursor = (c) => c == null || typeof c === 'string' || (typeof c === 'number' && Number.isSafeInteger(c) && c >= 0);
  const cursorOf = (json) => {
    const c = rawCursorOf(json);
    return !validCursor(c) || c == null || String(c).trim() === '' ? null : String(c);
  };
  const pageTotal = (json) => count(edgeOf(json)?.count);
  // Short, loggable description of an unusable response so the owner sees what Instagram actually sent.
  function sampleOf(res, n = 1500) {
    const e = res.env;
    return 'HTTP ' + (Number(res.status) || 0) + ' ' + (res.contentType || '?') +
      (res.url ? ' ' + res.url : '') + (res.redirected ? ' (redirected)' : '') + ' body: ' + String(res.text || '').slice(0, n).replace(/\s+/g, ' ') +
      (e ? ' | tab ' + (e.path || '?') + ' ' + (e.vis || '?') + ' csrf=' + (e.csrf ? 1 : 0) + ' uid=' + (e.uid ? 1 : 0) : '') +
      (res.ms != null ? ' | ' + res.ms + ' ms' : '');
  }
  const pathOf = (u) => { try { return new URL(u).pathname; } catch { return ''; } };
  // Which kind of Instagram page a tab (or a redirected request) is on.
  function pageKind(url) {
    const p = pathOf(url);
    if (/^\/challenge\//.test(p) || /^\/accounts\/(suspended|disabled)/.test(p)) return 'challenge';
    if (/^\/accounts\/login/.test(p)) return 'login';
    return 'ok';
  }

  // Empty list pages: Instagram's quiet way of blocking (RESEARCH.md §3).
  // ctx = {cursor, total, totalSource, received, emptyAt}.
  // emptyAt tracks the cursor that already returned an empty page. A repeat with
  // has_more is an error; a repeat terminal empty page is saved as partial coverage.
  function listCheck(json, ctx, out) {
    const users = usersOf(json);
    if (!users) return out('other', 'no_users_field');
    if (moreOf(json) != null && typeof moreOf(json) !== 'boolean') return out('other', 'invalid_has_more');
    if (!validCursor(rawCursorOf(json))) return out('other', 'invalid_cursor');
    if (limitedOf(json)) return null; // capped: parsePage records done + limited
    if (users.length && !users.some((u) => mapUser(u))) return out('other', 'unusable_users');
    if (users.length && moreOf(json) === true && !cursorOf(json)) return out('other', 'missing_cursor');
    if (users.length) return null;
    const again = ctx.emptyAt != null && ctx.emptyAt === (ctx.cursor || '');
    const edge = edgeOf(json), total = count(ctx.total) ?? count(edge && edge.count) ?? 0;
    if (!ctx.cursor && ctx.total != null && count(ctx.total) == null && !count(edge && edge.count))
      return out('other', 'invalid_total');
    const more = moreOf(json) === true || (moreOf(json) !== false && cursorOf(json) != null);
    if (more) {
      return again ? out('other', 'empty_page_again') : out('soft_block', 'empty_page_with_more');
    }
    if (!ctx.cursor && total > 0) return again ? out('other', 'empty_first_page_again') : out('soft_block', 'empty_first_page');
    if (moreOf(json) !== false) return out('other', 'empty_page_without_end');
    // Only a count verified in this run (or carried by this response) can contradict
    // a terminal page. The first contradiction is retried locally after normal pacing;
    // the second reaches the server, which records the incomplete run as partial.
    const verifiedTotal = count(edge && edge.count) ?? (ctx.totalSource === 'current_run' ? count(ctx.total) : null);
    if (ctx.cursor && verifiedTotal && Number(ctx.received) < verifiedTotal * 0.98 && !again)
      return out('other', 'empty_page_before_total');
    return null; // genuine end: empty tail, or a list that really is empty
  }

  // res = {status, json, text, retryAfter, url}. Returns {code, retryAt, reason} or null when the response is usable.
  // Codes: rate_limit | soft_block | challenge | login | private | not_found | other (server contract),
  // plus local-only network (tab/transport trouble) and unsupported (endpoint refuses web clients).
  function classify(res, kind, now = Date.now(), ctx = {}) {
    const status = Number(res.status) || 0, json = res.json !== undefined ? res.json : parseBody(res.text);
    const ok = status >= 200 && status < 300;
    const raw = String(res.text || '').slice(0, 4000).toLowerCase();
    const msg = json ? [json.message, json.error_title, json.error_type, json.error_body, typeof json.error === 'string' ? json.error : '',
      json.feedback_title, json.checkpoint_url ? 'checkpoint_required' : ''].join(' ').toLowerCase() : raw;
    const ra = res.retryAfter, n = Number(ra);
    const retryAt = ra ? (Number.isFinite(n) ? now + n * 1000 : Date.parse(ra) || null) : null;
    const out = (code, reason) => ({ code, retryAt, reason });
    const page = res.url ? pageKind(res.url) : 'ok';
    if (page === 'challenge') return out('challenge', 'challenge_redirect');
    if (page === 'login') return out('login', 'login_redirect');
    if (/checkpoint_required|challenge_required|\/challenge\//.test(msg)) return out('challenge', 'checkpoint');
    if (status === 429) return out('rate_limit', 'http_429');
    if (/please wait a few minutes|wait a few minutes before|rate.?limit|too many requests/.test(msg)) return out('rate_limit', 'please_wait');
    if (/feedback_required/.test(msg) || (json && json.spam === true)) return out('soft_block', 'feedback_required');
    if (/useragent mismatch/.test(msg)) return out('unsupported', 'useragent_mismatch');
    if (status === 401 || /login_required/.test(msg) || (json && json.require_login)) return out('login', 'require_login');
    if (status === 404 || (json && /user not found|not.found/.test(msg))) return out('not_found', 'not_found');
    if (json && /not authorized to view|private/.test(msg)) return out('private', 'private');
    if (!status) return out('network', res.aborted ? 'timeout' : res.tabError ? 'tab' : 'fetch');
    if (!ok) return out('other', 'http_' + status);
    if (!json) {
      if (res.env?.uid === false && /html/i.test(res.contentType || '')) return out('login', 'html_logged_out');
      // A list API fetch can finish with HTTP 200 after Instagram redirects it to its HTML home page.
      // This says nothing about access to the target until the viewer's profile page is checked.
      if (kind === 'list' && /html/i.test(res.contentType || '')) {
        try {
          const u = new URL(res.url);
          if (u.hostname === 'www.instagram.com' && u.pathname === '/') return out('other', 'list_html_home_redirect');
        } catch {}
      }
      return /login|password/.test(raw) ? out('login', 'html_login') : out('other', 'not_json');
    }
    if (json.status && json.status !== 'ok') return out('other', 'status_' + String(json.status).slice(0, 20));
    if (kind === 'list') return listCheck(json, ctx || {}, out);
    if (kind === 'profile' && !mapProfile(userOf(json))) return out('other', 'no_user');
    return null;
  }

  // What a profile page loaded in a lookup tab shows when no profile data came out of it.
  // info = {url, title, text} read from the tab's DOM (no network).
  function pageVerdict(info) {
    if (!info || !info.url) return { code: 'network', reason: 'lookup_tab_gone' };
    const k = pageKind(info.url);
    if (k !== 'ok') return { code: k, reason: k + '_page' };
    const t = String(info.title || '') + ' ' + String(info.text || '');
    if (/page not found|isn.t available|may have been removed/i.test(t)) return { code: 'not_found', reason: 'page_not_found' };
    if (/please wait a few minutes/i.test(t)) return { code: 'rate_limit', reason: 'please_wait_page' };
    return { code: 'other', reason: 'no_profile_data' };
  }

  function privateWall(info, handle, profile) {
    // The database's is_private flag is not viewer access proof. Require two live wall observations
    // on this handle's actual profile page; an explicitly public live profile is contradictory.
    if (!info || !info.privateWall || !info.privateWallRechecked || profile?.is_private === false) return false;
    try {
      const u = new URL(info.url);
      return u.hostname === 'www.instagram.com' && u.pathname.toLowerCase() === '/' + String(handle).toLowerCase() + '/';
    } catch { return false; }
  }

  // ---- Mapping to contract fields ---------------------------------------
  function mapUser(u) {
    if (!u || typeof u !== 'object' || !u.username) return null;
    return { ig_id: String(u.pk || u.pk_id || u.id || '') || null, handle: String(u.username), name: u.full_name || '',
      pic_url: u.profile_pic_url || '', is_private: !!u.is_private, is_verified: !!u.is_verified };
  }

  function parsePage(json) {
    const users = (usersOf(json) || []).map(mapUser).filter(Boolean);
    const cur = cursorOf(json);
    const next = moreOf(json) !== false && cur != null && users.length ? cur : null;
    const limited = limitedOf(json);
    return { users, next_cursor: limited ? null : next, done: limited || !next, limited,
      has_more: typeof moreOf(json) === 'boolean' ? moreOf(json) : null };
  }
  function listProgress(job, prog) {
    if (prog && prog.jobId != null && prog.jobId !== job.id) return {};
    if (!job.cursor && prog && prog.next) return {};
    return prog || {};
  }
  function listContext(job, prog, total, totalSource) {
    const cursor = job.cursor || null;
    return { cursor, total, totalSource, received: cursor ? (Number.isFinite(job.received) ? job.received : prog.received || 0) : 0,
      emptyAt: (prog.next ?? null) === cursor ? prog.emptyAt ?? null : null };
  }

  // Instagram occasionally gives an unavailable or malformed count. Match the
  // server's accepted range so a bogus total cannot make a list look complete.
  const count = (v) => {
    if (typeof v === 'string') {
      if (!/^\s*\d[\d,]*\s*$/.test(v)) return null;
      v = Number(v.replaceAll(',', ''));
    }
    return typeof v === 'number' && Number.isFinite(v) && v >= 0 && v < 1e12 ? Math.floor(v) : null;
  };
  function mapProfile(u) {
    if (!u || typeof u !== 'object' || !u.username) return null;
    const link = (Array.isArray(u.bio_links) ? u.bio_links : []).map((l) => l && (l.url || l.lynx_url)).find(Boolean);
    return {
      ig_id: String(u.pk || u.pk_id || u.id || '') || null, handle: String(u.username), name: u.full_name || '',
      bio: typeof u.biography === 'string' ? u.biography : null, website: u.external_url || link || null,
      category: u.category || u.category_name || u.business_category_name || null,
      followers: count(u.follower_count ?? u.edge_followed_by?.count), following: count(u.following_count ?? u.edge_follow?.count),
      posts: count(u.media_count ?? u.edge_owner_to_timeline_media?.count),
      // Missing privacy is unknown, not evidence that this target is public.
      is_private: typeof u.is_private === 'boolean' ? u.is_private : null, is_verified: !!u.is_verified,
      is_business: !!(u.is_business || u.is_business_account || u.account_type === 2),
      pic_url: u.hd_profile_pic_url_info?.url || u.profile_pic_url_hd || u.profile_pic_url || null,
    };
  }
  // Pulls the user object out of either /users/{id}/info/ or web_profile_info.
  const userOf = (json) => (json && (json.user || json.data?.user)) || null;

  // ---- Pacing, budgets, cooldowns (state lives in chrome.storage.local) ----
  const dayKey = (t) => { const d = new Date(t); return d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate(); };
  const nextMidnight = (t) => { const d = new Date(t); d.setHours(24, 0, 0, 0); return d.getTime(); };
  const bucket = () => ({ until: 0, hits: [], retryUntil: 0 });
  // The midnight-strike policy was replaced on 2026-09-27 at 00:20 UTC.
  // Old state saved only the resulting deadline, so migrate only a midnight
  // hold supported by strikes from before that change. Mark even a no-op so a
  // later provider Retry-After ending at midnight is never reconsidered.
  const MIDNIGHT_POLICY_END = Date.UTC(2026, 8, 27, 0, 20);
  function migrateMidnightHolds(st, now) {
    if (st.midnightHoldMigration === 1) return;
    const events = KINDS.flatMap((kind) => (Array.isArray(st.cool[kind].hits) ? st.cool[kind].hits : [])
      .filter((at) => Number.isFinite(at) && at > 0 && at < MIDNIGHT_POLICY_END)
      .map((at) => ({ at, kind }))).sort((a, b) => a.at - b.at);
    const replay = fresh();
    let pending = false;
    for (const { at, kind } of events) applyHit(replay, at, null, kind);
    for (const kind of KINDS) {
      const b = st.cool[kind], until = Number(b.until);
      if (!Number.isFinite(until) || until <= now || !events.length ||
          (Number(b.retryUntil) || 0) >= until) continue;
      // Only an exact local-midnight deadline from a strike on that day is
      // identifiable as the old policy. A single hit with a midnight
      // Retry-After, or an unrelated hold, is left alone.
      const sameDay = events.filter((e) => nextMidnight(e.at) === until);
      // This migration permits one new probe only after a full six-hour rest.
      // The old state cannot reveal whether Instagram sent Retry-After.
      if (!sameDay.length) continue;
      if (now < Math.max(...sameDay.map((e) => e.at)) + PACE.cooldownCap) {
        pending = true;
        continue;
      }
      const bucketStrike = sameDay.some((e) => e.kind === kind &&
        events.filter((x) => x.kind === kind && x.at <= e.at && e.at - x.at < DAY).length >= PACE.strikes);
      const laneStrike = sameDay.some((e) =>
        events.filter((x) => x.at <= e.at && e.at - x.at < HOUR).length >= PACE.strikes);
      if (bucketStrike || laneStrike) b.until = Math.min(until, replay.cool[kind].until);
    }
    if (!pending) st.midnightHoldMigration = 1;
  }

  function fresh() {
    return { day: '', today: { list: 0, profile: 0, people: 0, bios: 0 }, nextAt: 0, listNextAt: 0, profileNextAt: 0, pages: 0, breakEvery: 25,
      cool: { list: bucket(), profile: bucket() }, hold: null, lastError: null, note: null,
      log: [], plog: [], rlog: [], streak: { other: 0, net: 0 }, infoOffUntil: 0,
      listRedirects: [], listEndpointUntil: 0, listEndpointStrikes: 0 };
  }
  function rollDay(st, now) {
    const k = dayKey(now);
    if (st.day !== k) { st.day = k; st.today = { list: 0, profile: 0, people: 0, bios: 0 }; }
    return st;
  }
  // Fills missing fields and migrates the 3.2 shape (one global cooldownUntil/hits) into per-bucket cooldowns.
  function normalize(stored, now) {
    const st = { ...fresh(), ...(stored || {}) };
    if (stored && stored.listNextAt === undefined) st.listNextAt = Number(stored.nextAt) || 0; // 3.4: nextAt was the list clock
    st.cool = { list: { ...bucket(), ...(st.cool && st.cool.list) }, profile: { ...bucket(), ...(st.cool && st.cool.profile) } };
    if ('cooldownUntil' in st || 'hits' in st) {
      const until = Number(st.cooldownUntil) || 0, hits = Array.isArray(st.hits) ? st.hits : [];
      st.cool.list.until = Math.max(st.cool.list.until, until); st.cool.list.hits = st.cool.list.hits.concat(hits);
      st.cool.profile.until = Math.max(st.cool.profile.until, until); // an old global cooldown held bios too
      delete st.cooldownUntil; delete st.hits;
    }
    migrateMidnightHolds(st, now);
    st.streak = { other: 0, net: 0, ...(st.streak || {}) };
    for (const k of ['log', 'plog', 'rlog']) if (!Array.isArray(st[k])) st[k] = [];
    if (!Array.isArray(st.listRedirects)) st.listRedirects = [];
    st.listEndpointUntil = Number(st.listEndpointUntil) || 0;
    st.listEndpointStrikes = Number(st.listEndpointStrikes) || 0;
    // 3.9.5 held both list directions after follower-only redirects. Its
    // entries had no direction, so that hold is not evidence against following.
    if (st.listEndpointUntil && st.listRedirects.length && st.listRedirects.every((x) => !x.direction)) {
      st.listRedirects = [];
      st.listEndpointUntil = 0;
      st.listEndpointStrikes = 0;
    }
    return rollDay(st, now);
  }
  // A home redirect on one target may be an access issue. Three different, freshly
  // confirmed public followers targets pause only that direction for this viewer.
  function recordListRedirect(st, handle, direction, publicProfile, now) {
    st.listRedirects = (st.listRedirects || []).filter((x) => x && now - x.at < HOUR);
    if (!publicProfile || !handle || !['followers', 'following'].includes(direction)) return st;
    const h = String(handle).toLowerCase();
    st.listRedirects = st.listRedirects.filter((x) => x.handle !== h || x.direction !== direction)
      .concat({ handle: h, direction, at: now });
    if (direction === 'followers' && st.listRedirects.filter((x) => x.direction === 'followers').length >= 3) {
      st.listEndpointStrikes = Math.min(st.listEndpointStrikes + 1, 4);
      st.listEndpointUntil = now + Math.min(30 * MIN * 2 ** (st.listEndpointStrikes - 1), 2 * HOUR);
    }
    return st;
  }
  function listPageSucceeded(st, direction) {
    if (direction !== 'followers') return st;
    st.listRedirects = []; st.listEndpointUntil = 0; st.listEndpointStrikes = 0;
    return st;
  }
  // Called after every Instagram request of `kind` ('list' | 'profile'), page loads included.
  function afterRequest(st, kind, now, r = Math.random) {
    rollDay(st, now);
    st.today[kind] = (st.today[kind] || 0) + 1;
    st.rlog = (st.rlog || []).filter((e) => now - e[0] < HOUR).concat([[now, kind]]);
    st.nextAt = Math.max(st.nextAt || 0, now + rand(...PACE.spacing, r));
    if (kind === 'profile') {
      st.profileNextAt = now + rand(...PACE.profileGap, r);
      return st;
    }
    st.listNextAt = now + rand(...PACE.listGap, r);
    if (++st.pages >= st.breakEvery) {
      st.listNextAt += rand(...PACE.breakLen, r);
      st.pages = 0; st.breakEvery = rand(...PACE.breakEvery, r);
    }
    return st;
  }
  // When a request of `kind` may go, pacing only (cooldowns and the window are checked in plan).
  const readyAt = (st, kind) => Math.max(st.nextAt || 0, (kind === 'profile' ? st.profileNextAt : st.listNextAt) || 0);
  // Sliding window over every Instagram request of this account: {n, until} (until = when the oldest one leaves it).
  function windowOf(st, now) {
    const inWin = (st.rlog || []).map((e) => e[0]).filter((t) => now - t < PACE.window).sort((a, b) => a - b);
    return { n: inWin.length, until: inWin.length >= PACE.windowMax ? inWin[inWin.length - PACE.windowMax] + PACE.window : 0 };
  }
  const allHits = (st) => KINDS.flatMap((k) => (st.cool && st.cool[k] && st.cool[k].hits) || []);
  // rate_limit / soft_block on bucket `kind`: start at 10 min, doubling per hit in 24 h (cap 6 h).
  // Three hits require at least 2 h of rest for that request kind.
  // Other kinds keep their normal pace; Instagram's Retry-After always wins.
  function applyHit(st, now, retryAt, kind = 'list') {
    const b = st.cool[kind];
    b.hits = (b.hits || []).filter((t) => now - t < DAY).concat(now);
    const n = b.hits.length;
    let until = now + Math.min(PACE.cooldownBase * 2 ** (n - 1), PACE.cooldownCap);
    if (Number.isFinite(retryAt) && retryAt > now) {
      b.retryUntil = Math.max(b.retryUntil || 0, retryAt);
      if (retryAt > until) until = retryAt;
    }
    if (n >= PACE.strikes) until = Math.max(until, now + PACE.strikePause);
    b.until = Math.max(b.until || 0, until);
    return st;
  }
  // Earliest cooldown still running (for heartbeat/server display), or 0.
  function cooldownUntil(st, now) {
    const c = KINDS.map((k) => st.cool[k].until).filter((t) => t > now);
    return c.length ? (st.cool.list.until > now ? st.cool.list.until : Math.min(...c)) : 0;
  }
  // Local backoffs. 'net' = tab or transport failed (request maybe never reached Instagram); 'other' = unknown answer.
  function backoff(st, now, which) {
    st.streak[which] = (st.streak[which] || 0) + 1;
    const base = which === 'net' ? PACE.netBase : PACE.otherBase, cap = which === 'net' ? PACE.netCap : PACE.otherCap;
    st.nextAt = Math.max(st.nextAt || 0, now + Math.min(base * 2 ** (st.streak[which] - 1), cap));
    return st;
  }
  function succeeded(st) { st.streak = { other: 0, net: 0 }; return st; }

  // Which job kinds may be asked for right now. Returns {kinds, wait, why}: kinds empty → sleep `wait` ms.
  function plan(st, left, now) {
    const unavailableUntil = (k) => st.cool[k].until || 0;
    const eligible = KINDS.filter((k) => left[k] > 0 && !(unavailableUntil(k) > now));
    if (!eligible.length) {
      const cooling = KINDS.filter((k) => left[k] > 0);
      if (!cooling.length) return { kinds: [], wait: 10 * MIN, why: 'budget' };
      return { kinds: [], wait: Math.max(1e3, Math.min(...cooling.map(unavailableUntil)) - now),
        why: 'cooldown' };
    }
    const win = windowOf(st, now);
    if (win.until > now) return { kinds: [], wait: Math.max(1e3, win.until - now), why: 'window' };
    const kinds = eligible.filter((k) => readyAt(st, k) <= now);
    if (kinds.length) return { kinds, wait: 0, why: 'ready' };
    return { kinds: [], wait: Math.max(1e3, Math.min(...eligible.map((k) => readyAt(st, k))) - now), why: 'pace' };
  }
  // The in-flight Instagram request marker in storage. A restarted worker waits it out instead of double-firing.
  const laneBusy = (lane, now) => !!(lane && lane.until > now);

  function budgetOf(budget) {
    const b = { ...BUDGET, ...(budget || {}) };
    return { list: Math.max(0, Number(b.list) || 0), profile: Math.max(0, Number(b.profile) || 0) };
  }
  // Rows gained today, for the popup ("N people · M bios").
  function tally(st, now, people, bios) {
    rollDay(st, now);
    st.today.people = (st.today.people || 0) + people; st.today.bios = (st.today.bios || 0) + bios;
    if (bios) st.plog = (st.plog || []).filter((t) => now - t < HOUR).concat(now);
    return st;
  }
  // Soak-test rate: list pages and people in the last hour, plus the last rate_limit/soft_block hit.
  function logPage(st, now, people) {
    st.log = (st.log || []).filter((e) => now - e[0] < HOUR).concat([[now, people]]);
    return st;
  }
  function rateOf(st, now) {
    const log = (st.log || []).filter((e) => now - e[0] < HOUR);
    const hits = st.cool ? allHits(st) : st.hits || [];
    return { pages_hour: log.length, people_hour: log.reduce((a, e) => a + (e[1] || 0), 0),
      bios_hour: (st.plog || []).filter((t) => now - t < HOUR).length,
      requests_hour: (st.rlog || []).filter((e) => now - e[0] < HOUR).length,
      hits_24h: hits.filter((t) => now - t < DAY).length,
      last_hit_at: hits.length ? new Date(Math.max(...hits)).toISOString() : null };
  }
  function budgetLeft(st, budget, now) {
    rollDay(st, now);
    const b = budgetOf(budget);
    // 0 = no daily limit (request pacing and Instagram cooldowns still apply).
    return { list: b.list ? Math.max(0, b.list - st.today.list) : Infinity, profile: b.profile ? Math.max(0, b.profile - st.today.profile) : Infinity };
  }

  // ---- Instagram tab choice ------------------------------------------------
  // tabs = [{id, url, status, discarded, frozen, active, pinned}]; opts = {preferId, avoid:{id, until}}.
  // Returns {use:id} | {reload:id} | {wait:ms, why} | {open:true, why:'no_tab'}.
  function chooseTab(tabs, opts = {}, now = Date.now()) {
    const avoid = opts.avoid && opts.avoid.until > now ? opts.avoid.id : null;
    const ig = (tabs || []).filter((t) => t && /^https:\/\/www\.instagram\.com\//.test(t.url || t.pendingUrl || ''));
    const good = ig.filter((t) => pageKind(t.url || t.pendingUrl) === 'ok');
    const sleeping = (t) => t.discarded || t.frozen || t.status === 'unloaded';
    const usable = good.filter((t) => !sleeping(t) && t.status === 'complete' && t.id !== avoid);
    const rank = (t) => (t.id === opts.preferId ? 0 : t.pinned ? 1 : !t.active ? 2 : 3);
    if (usable.length) return { use: usable.sort((a, b) => rank(a) - rank(b))[0].id };
    if (good.some((t) => !sleeping(t) && t.status === 'loading')) return { wait: 5e3, why: 'tab_loading' };
    const wake = good.filter((t) => (sleeping(t) || t.id === avoid) && !t.active).sort((a, b) => rank(a) - rank(b))[0];
    if (wake) return { reload: wake.id };
    if (good.length) return { wait: 30e3, why: 'tab_busy' }; // only Michael's active tab is left and it misbehaves: don't reload it
    if (ig.some((t) => pageKind(t.url || t.pendingUrl) === 'challenge')) return { wait: 30e3, why: 'tab_challenge' };
    if (ig.length) return { wait: 30e3, why: 'tab_login' };
    return { open: true, why: 'no_tab' };
  }

  // Cache of handle → {ig_id, followers, following, at}, filled by passive capture and lookups (saves page loads).
  function rememberId(ids, p, now, max = 5000) {
    if (!p || !p.handle || !p.ig_id) return ids || {};
    const out = { ...(ids || {}) };
    out[p.handle.toLowerCase()] = { ig_id: String(p.ig_id), followers: p.followers ?? null, following: p.following ?? null, at: now };
    const keys = Object.keys(out);
    if (keys.length > max) keys.sort((a, b) => out[a].at - out[b].at).slice(0, keys.length - max).forEach((k) => delete out[k]);
    return out;
  }

  // ---- Outbox: every result is queued first, then flushed in order -------
  // Passive bios may be shed during a long outage. Never discard leased job results:
  // that would acknowledge a request locally while silently losing its saved page.
  function enqueue(box, path, body, max = BOX_MAX) {
    const out = (box || []).concat({ path, body, qid: Date.now().toString(36) + Math.random().toString(36).slice(2) });
    while (out.length > max) {
      const i = out.findIndex((x) => x.path === '/api/ext/profile' && x.body && x.body.job_id == null);
      if (i < 0) break;
      out.splice(i, 1);
    }
    return out;
  }
  // Permanent rejections need inspection, but must not erase a leased page's only copy.
  function park(dead, item, maxPassive = 50) {
    const out = (dead || []).concat(item);
    let passive = out.filter((x) => x.body?.job_id == null).length;
    return out.filter((x) => x.body?.job_id != null || passive-- <= maxPassive);
  }
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
  // key drives the popup square: run | wait | cool | stop | off.
  const TAB_TEXT = { no_tab: 'Open Instagram', tab_loading: 'Instagram tab loading', tab_busy: 'Instagram tab not responding',
    tab_login: 'Instagram tab is on the login page', tab_challenge: 'Instagram tab shows a security check', tab_waking: 'Reloading Instagram tab' };
  function statusOf(st, ctx, now) {
    const t = (ms) => new Date(ms).toTimeString().slice(0, 5);
    const badgeFor = (until) => { const m = Math.ceil((until - now) / MIN); return m >= 60 ? Math.ceil(m / 60) + 'h' : m + 'm'; };
    if (st.hold) return { state: 'paused', text: 'Needs attention: ' + st.hold.message, badge: '!', key: 'off' };
    if (ctx.localPaused) return { state: 'paused', text: 'Paused', badge: '‖', key: 'stop' };
    if (ctx.serverPaused) return { state: 'paused', text: 'Paused in workspace', badge: '‖', key: 'stop' };
    const issue = st.listEndpointUntil > now;
    const lc = st.cool.list.until > now, pc = st.cool.profile.until > now;
    const listUntil = st.cool.list.until || 0;
    const listIssueText = 'Followers paused after home redirects until ' + t(st.listEndpointUntil);
    if (lc && pc) {
      const until = Math.min(listUntil, st.cool.profile.until);
      return { state: 'cooldown', text: issue ? listIssueText + ' · Instagram list limit until ' + t(st.cool.list.until) + ' · Bios cooling until ' + t(st.cool.profile.until) :
        'Cooldown until ' + t(until), badge: badgeFor(until), key: 'cool' };
    }
    if (ctx.offline) return { state: 'idle', text: 'Server offline', badge: '!', key: 'off' };
    if (ctx.noTab) return { state: 'idle', text: TAB_TEXT[ctx.noTab] || TAB_TEXT.no_tab, badge: '!', key: 'off' };
    if (ctx.budgetDone) return { state: 'idle', text: 'Daily budget reached', badge: '', key: 'stop' };
    const pre = [issue ? listIssueText : null,
      lc ? 'Instagram list limit until ' + t(st.cool.list.until) : null,
      pc ? 'Bios cooling until ' + t(st.cool.profile.until) : null].filter(Boolean).join(' · ');
    const prefix = pre ? pre + ' · ' : '';
    const badge = lc ? badgeFor(listUntil) : '';
    if (ctx.job) return { state: 'running', text: prefix + 'Scraping', badge, key: 'run' };
    if (ctx.laneWait) return { state: 'running', text: prefix + 'Waiting for the last request to finish', badge, key: 'wait' };
    const next = Math.min(...KINDS.filter((k) => !(st.cool[k].until > now)).map((k) => readyAt(st, k)));
    if (next > now && next < Infinity) return { state: 'running', text: prefix + 'Next request in ' + Math.ceil((next - now) / 1e3) + 's', badge, key: 'wait' };
    if (lc) return { state: 'cooldown', text: pre, badge, key: 'cool' };
    if (issue) return { state: 'idle', text: prefix + 'No other work ready', badge: '', key: 'wait' };
    return { state: 'idle', text: prefix + 'Idle, queue empty', badge: '', key: 'stop' };
  }

  // ---- Lanes: one install = one Chrome profile = one Instagram account ----
  // A stable per-install id the server leases jobs to (kept in chrome.storage.local, survives updates).
  const newLaneId = (r = Math.random) => 'ln_' + Array.from({ length: 12 }, () => 'abcdefghijkmnpqrstuvwxyz23456789'[Math.floor(r() * 32)]).join('');
  // Each lane starts its first request after its own random offset, so profiles opened together never fire in step.
  const START_OFFSET = [15e3, 120e3];
  const startOffset = (r = Math.random) => rand(...START_OFFSET, r);
  // The logged-in account of this profile: ds_user_id from the cookie, the handle from the page's own JSON
  // (the "username" nearest to that id). ig_id null = no Instagram session in this profile.
  function handleFrom(text, uid) {
    if (!text || !uid) return null;
    const s = String(text), id = String(uid), rx = /"username"\s*:\s*"([A-Za-z0-9._]{1,30})"/g;
    let best = null, bestD = Infinity;
    for (let i = s.indexOf('"' + id + '"'); i >= 0 && bestD > 0; i = s.indexOf('"' + id + '"', i + 1)) {
      const lo = Math.max(0, i - 400), win = s.slice(lo, i + 400);
      rx.lastIndex = 0;
      for (let m; (m = rx.exec(win));) { const d = Math.abs(lo + m.index - i); if (d < bestD) { bestD = d; best = m[1].toLowerCase(); } }
    }
    return best;
  }
  function accountFrom(cookie, text) {
    const m = String(cookie || '').match(/(?:^|;\s*)ds_user_id=(\d+)/);
    if (!m) return { ig_id: null, handle: null };
    return { ig_id: m[1], handle: handleFrom(text, m[1]) };
  }

  function controlAllows(state, kind) {
    return !state.offline && !state.serverPaused && (!state.stages || state.stages[kind] !== false);
  }

  const api = { controlAllows, listProgress, listContext, count, PACE, BUDGET, newLaneId, startOffset, START_OFFSET, handleFrom, accountFrom, BOX_MAX, KINDS, budgetOf, tally, MIN, HOUR, DAY, classify, parseBody, usersOf, cursorOf, pageTotal, sampleOf,
    pageKind, pageVerdict, logPage, rateOf, mapUser, parsePage, mapProfile, userOf, dayKey, nextMidnight, fresh, rollDay, normalize,
    afterRequest, readyAt, windowOf, applyHit, cooldownUntil, backoff, succeeded, recordListRedirect, listPageSucceeded,
    plan, laneBusy, budgetLeft, chooseTab, rememberId, enqueue, park, flush, statusOf, privateWall };
  // ---- Control strip (widget): the server's three stages as short rows. ctl = GET /api/control, now = ms ----
  const STAGE_SHORT = { lists: 'Lists', bios: 'Bios', ai: 'AI' };
  function stageClock(sec) {
    sec = Math.max(0, Math.round(sec));
    return sec < 60 ? sec + ' s' : sec < 3600 ? Math.ceil(sec / 60) + ' min' : (sec / 3600).toFixed(1) + ' h';
  }
  // Why a stage waits and until when, in plain words: "daily limit · back 00:00", "next request 9 s".
  function waitWord(why, sec, clockFn) {
    why = why || '';
    const back = sec == null ? '' : sec > 3600
      ? ' · back ' + new Date(Date.now() + sec * 1e3).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : ' · ' + clockFn(sec);
    if (/daily/i.test(why)) return 'daily limit' + back;
    if (/slow down/i.test(why)) return 'Instagram limit' + back;
    if (/log in/i.test(why)) return 'needs login';
    if (/security check/i.test(why)) return 'security check';
    if (/break/i.test(why)) return 'short pause' + back;
    if (/between requests/i.test(why)) return 'next request' + (sec == null ? '' : ' in ' + clockFn(sec));
    return 'waiting' + back;
  }
  function stagesView(ctl, now) {
    if (!ctl || !Array.isArray(ctl.stages)) return [];
    const age = ctl.got ? Math.max(0, (now - ctl.got) / 1e3) : 0;
    return ctl.stages.map((s) => {
      let word;
      if (s.state === 'paused') word = 'paused';
      else if (s.state === 'waiting' && s.wait) {
        word = waitWord(s.wait.why, s.wait.seconds != null ? Math.max(0, s.wait.seconds - age) : null, stageClock);
      } else if (s.state === 'idle') word = /no instagram account/i.test(s.now || '') ? 'no account online' : 'nothing to do';
      else if (s.id === 'ai') word = 'running · ' + Math.round(s.minute || 0).toLocaleString('en-US') + '/min · '
        + Math.round(s.hour || 0).toLocaleString('en-US') + '/h';
      else word = 'running' + (s.hour ? ' · ' + Math.round(s.hour).toLocaleString('en-US') + '/h' : '');
      return { id: s.id, name: STAGE_SHORT[s.id] || s.label, label: s.label, word, on: s.state === 'running' || s.state === 'waiting',
        paused: !!s.paused, action: s.paused ? 'resume' : 'pause', now: s.now || '', help: s.help || '' };
    });
  }
  api.stagesView = stagesView;
  root.FL = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
