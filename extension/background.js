// Fortunate Leads: paced executor. Server owns the queue; this worker owns pacing, budgets and cooldowns.
// MV3 lifecycle: every piece of state that must survive the worker being stopped lives in chrome.storage.local
// (st, box, cur, prog, lane, ids, seen, trail, debug). The in-memory loop is single-flight with a staleness expiry;
// the 30 s alarm restarts it after the worker was stopped or if it stalls. Instagram requests are serialised by the
// stored `lane` marker, so a restarted worker never fires while an earlier request may still be in flight.
importScripts('lib/core.js');
const SERVER = 'http://127.0.0.1:8777';
const IG = 'https://www.instagram.com';
const VERSION = chrome.runtime.getManifest().version;
const LOOP_STALE = 4 * FL.MIN;  // a loop that hasn't beaten this long is considered dead and replaced
const CUR_TTL = 8 * FL.MIN;     // server leases jobs for 10 min; resume our own job only well inside that
const mem = { gen: 0, looping: false, beat: 0, serverPaused: false, offline: false, noTab: null, budgetDone: false, laneWait: false,
  job: null, label: '', backoff: 5e3, lastBeat: 0, badTab: null, lookups: new Set() };

const sleep = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms)));
const get = async (k) => (await chrome.storage.local.get(k))[k];
const set = (o) => chrome.storage.local.set(o);
let chain = Promise.resolve();
function locked(fn) { const p = chain.then(fn); chain = p.catch(() => {}); return p; }
const edit = (key, fn) => locked(async () => { const v = await fn(await get(key)); await set({ [key]: v }); return v; });
async function loadSt() { return FL.normalize(await get('st'), Date.now()); }
const editSt = (fn) => locked(async () => { const st = await loadSt(); await fn(st); await set({ st }); return st; });
const iso = (t) => (t ? new Date(t).toISOString() : null);
const hhmmss = (t) => new Date(t).toTimeString().slice(0, 8);
class Superseded extends Error {}

// Short ring of recent events (requests, failures, restarts) for the popup's "Copy debug".
const trail = (what, extra) => edit('trail', (t) => (t || []).concat({ at: hhmmss(Date.now()), what, ...(extra || {}) }).slice(-40)).catch(() => {});

// ---- server I/O (every call has a timeout: a hung local server must never stall the loop) ------
async function api(path, body, ms = 15e3) {
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(SERVER + path, body === undefined ? { headers: { 'X-FL': '1' }, signal: ctl.signal } :
      { method: 'POST', headers: { 'content-type': 'application/json', 'X-FL': '1' }, body: JSON.stringify(body), signal: ctl.signal });
    return { status: r.status, json: await r.json().catch(() => ({})) };
  } finally { clearTimeout(timer); }
}
async function sendItem(item) {
  try { const r = await api(item.path, item.body); return r.status >= 500 ? 'fail' : 'ok'; } catch { return 'retry'; }
}
// Outbox: items stay in storage until the server took them; single-flight, never holds the storage lock over the network.
let flushing = null;
function flushBox() {
  if (flushing) return flushing;
  flushing = (async () => {
    try {
      for (;;) {
        const box = (await get('box')) || [];
        if (!box.length) { mem.offline = false; return true; }
        const r = await sendItem(box[0]);
        if (r === 'retry') { mem.offline = true; return false; }
        mem.offline = false;
        // Server error (5xx): retry a few rounds, then park the item so one bad result can't block the queue forever.
        const tries = (box[0].tries || 0) + 1;
        if (r === 'fail' && tries < 10) {
          await locked(async () => { const b = (await get('box')) || []; if (b[0]) { b[0].tries = tries; await set({ box: b }); } });
          return false;
        }
        await locked(async () => {
          const b = (await get('box')) || [], item = b.shift();
          await set(r === 'fail' && item ? { box: b, dead: ((await get('dead')) || []).concat(item).slice(-50) } : { box: b });
        });
        mem.beat = Date.now();
      }
    } finally { flushing = null; }
  })();
  return flushing;
}
async function queue(path, body) {
  await locked(async () => set({ box: FL.enqueue(await get('box'), path, body) }));
  await flushBox();
}
// Enqueue a job's result and clear `cur` in one storage write, so a worker restarted after this point never
// re-runs a request whose result is already stored (step() flushes the outbox before leasing again).
async function queueDone(path, body) {
  await locked(async () => set({ box: FL.enqueue(await get('box'), path, body), cur: null }));
  await editSt((st) => FL.succeeded(st));
  await flushBox();
}
async function applyServer(j) {
  if (j && j.budget && typeof j.budget === 'object') await set({ budget: j.budget });
  if (!j || typeof j.paused !== 'boolean') return;
  mem.serverPaused = j.paused;
  const st = await loadSt();
  if (!st.hold) return;
  // Workspace resume: the server flag went paused → not paused after the hold began.
  if (j.paused && !st.hold.sawPause) await editSt((s) => { if (s.hold) s.hold.sawPause = true; });
  if (!j.paused && st.hold.sawPause) await editSt((s) => { s.hold = null; s.lastError = null; });
}
async function heartbeat(force) {
  if (!force && Date.now() - mem.lastBeat < 25e3) return;
  mem.lastBeat = Date.now();
  const st = await loadSt(), s = await status(st), now = Date.now(), cd = FL.cooldownUntil(st, now);
  try {
    const r = await api('/api/ext/heartbeat', { version: VERSION, state: s.state, cooldown_until: cd ? iso(cd) : null,
      today: { list: st.today.list, profile: st.today.profile }, budget: FL.budgetOf(await get('budget')),
      last_error: st.hold ? st.hold.message : st.lastError, activity: mem.label || null, text: s.text, people_today: st.today.people || 0,
      rate: FL.rateOf(st, now) }, 10e3);
    mem.offline = false;
    await applyServer(r.json);
  } catch { mem.offline = true; }
}

// ---- status, badge, popup view ------------------------------------------
async function status(st) {
  st = st || await loadSt();
  const now = Date.now(), budget = FL.budgetOf(await get('budget')), left = FL.budgetLeft(st, budget, now);
  const s = FL.statusOf(st, { ...mem, localPaused: !!(await get('localPaused')) }, now);
  chrome.action.setBadgeText({ text: s.badge });
  chrome.action.setBadgeBackgroundColor({ color: s.badge === '!' ? '#b3261e' : '#555' });
  const bucket = (k) => ({ until: st.cool[k].until > now ? st.cool[k].until : 0, hits: st.cool[k].hits.filter((t) => now - t < FL.DAY).length,
    left: Number.isFinite(left[k]) ? left[k] : null, readyAt: k === 'profile' ? Math.max(st.nextAt, st.profileNextAt) : st.nextAt });
  await set({ view: { ...s, today: st.today, budget, job: mem.job ? mem.label : '', nextAt: st.nextAt,
    lastError: st.hold ? st.hold.message : st.lastError, note: st.note, rate: FL.rateOf(st, now), at: now,
    buckets: { list: bucket('list'), profile: { ...bucket('profile'), infoOff: st.infoOffUntil > now } },
    box: ((await get('box')) || []).length, tab: mem.noTab || 'ok' } });
  return s;
}

// ---- Instagram tab ---------------------------------------------------------
// Picks a usable, awake instagram.com tab; wakes a discarded/frozen one or opens a pinned background tab if none is left
// (both rate-limited). Returns {tab} or {wait}. Never touches the tab Michael is looking at.
async function pickTab() {
  const now = Date.now(), wt = (await get('workTab')) || {};
  let tabs = [];
  try { tabs = (await chrome.tabs.query({ url: IG + '/*' })).filter((t) => !mem.lookups.has(t.id)); } catch {}
  const c = FL.chooseTab(tabs, { preferId: wt.id, avoid: mem.badTab }, now);
  if (c.use) {
    const tab = tabs.find((t) => t.id === c.use);
    mem.noTab = null;
    if (tab.autoDiscardable !== false) chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => {}); // Memory Saver must not drop it
    if (wt.id !== tab.id) await set({ workTab: { ...wt, id: tab.id } });
    return { tab };
  }
  if (c.reload) {
    if (now - (wt.reloadAt || 0) < 3 * FL.MIN) { mem.noTab = 'tab_waking'; return { wait: 15e3 }; }
    await set({ workTab: { ...wt, id: c.reload, reloadAt: now } });
    mem.noTab = 'tab_waking'; mem.badTab = null;
    await trail('reload tab', { id: c.reload });
    try { await chrome.tabs.reload(c.reload); } catch (e) { await trail('reload failed', { err: String(e.message || e) }); }
    return { wait: 10e3 };
  }
  if (c.open) {
    mem.noTab = 'no_tab';
    if (now - (wt.openAt || 0) < 10 * FL.MIN) return { wait: 30e3 };
    await set({ workTab: { ...wt, openAt: now } });
    let tab = null;
    try { tab = await chrome.tabs.create({ url: IG + '/', active: false, pinned: true }); } catch {
      try { const w = await chrome.windows.create({ url: IG + '/', focused: false, state: 'minimized' }); tab = w.tabs && w.tabs[0]; } catch (e) {
        await trail('open tab failed', { err: String(e.message || e) });
      }
    }
    if (tab) { await set({ workTab: { ...wt, id: tab.id, openAt: now } }); await trail('opened instagram tab', { id: tab.id }); }
    return { wait: 15e3 };
  }
  mem.noTab = c.why;
  return { wait: c.wait };
}

// ---- Instagram requests: one lane, inside a real instagram.com tab ---------
const withTimeout = (ms, p) => Promise.race([p, new Promise((_, no) => setTimeout(() => no(new Error('Instagram tab did not respond in ' + ms / 1e3 + ' s')), ms))]);
async function inTab(tabId, url) {
  const t0 = Date.now();
  try {
    const [r] = await withTimeout(45e3, chrome.scripting.executeScript({ target: { tabId }, world: 'MAIN', args: [url, 30e3], func: async (u, ms) => {
      const ck = document.cookie;
      const csrf = decodeURIComponent((ck.match(/(?:^|; )csrftoken=([^;]+)/) || [])[1] || '');
      const env = { path: location.pathname, vis: document.visibilityState, csrf: !!csrf, uid: /(?:^|; )ds_user_id=/.test(ck) };
      if (location.hostname !== 'www.instagram.com') return { status: 0, text: 'tab left instagram.com', env, tabError: true };
      let claim = '0'; try { claim = sessionStorage.getItem('www-claim-v2') || '0'; } catch {}
      const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), ms); // also covers a body that never finishes
      try {
        const res = await (window.__flFetch || fetch)(u, { credentials: 'include', signal: ctl.signal, headers: {
          'X-IG-App-ID': '936619743392459', 'X-CSRFToken': csrf, 'X-ASBD-ID': '129477', 'X-IG-WWW-Claim': claim,
          'X-Requested-With': 'XMLHttpRequest', 'Accept': '*/*' } });
        const claimOut = res.headers.get('x-ig-set-www-claim');
        if (claimOut) try { sessionStorage.setItem('www-claim-v2', claimOut); } catch {}
        let text = await res.text();
        const contentType = res.headers.get('content-type') || '';
        if (!/json|javascript/.test(contentType) && text.length > 200e3) text = text.slice(0, 200e3);
        return { status: res.status, text, retryAfter: res.headers.get('retry-after'), contentType, url: res.url, redirected: res.redirected, env };
      } catch (e) {
        const aborted = e && e.name === 'AbortError';
        return { status: 0, text: aborted ? 'no answer from Instagram within ' + ms / 1e3 + ' s (aborted)' : String(e), aborted, env };
      } finally { clearTimeout(timer); }
    } }));
    const p = (r && r.result) || { status: 0, text: 'no result from tab', tabError: true };
    p.sent = !p.tabError; p.ms = Date.now() - t0;
    p.json = FL.parseBody(p.text);
    return p;
  } catch (e) {
    const msg = String((e && e.message) || e);
    return { status: 0, text: msg, json: null, tabError: true, sent: false, timedOut: /did not respond/.test(msg), ms: Date.now() - t0 };
  }
}
// One Instagram request. kind = bucket ('list' | 'profile'). ctx = list context for classify.
async function igRequest(gen, tab, url, kind, ctx) {
  if (gen !== mem.gen) throw new Superseded();
  if (FL.laneBusy(await get('lane'), Date.now())) return { res: { status: 0, text: 'lane busy' }, bad: { code: 'busy' } };
  await set({ lane: { until: Date.now() + 90e3, url } });
  let res, keepLane = false;
  try {
    res = await inTab(tab.id, url);
    // A timed-out executeScript may still have its fetch running in the page: hold the lane until that fetch aborted too.
    keepLane = !!res.timedOut;
  } finally {
    await set({ lane: keepLane ? { until: Date.now() + 60e3, url, after: 'timeout' } : null });
  }
  const now = Date.now();
  if (res.sent) await editSt((st) => FL.afterRequest(st, kind, now));
  if (res.tabError) mem.badTab = { id: tab.id, until: now + 2 * FL.MIN };
  const bad = FL.classify(res, kind, now, ctx);
  await trail(kind + ' ' + url.replace(IG, '').replace(/\?.*/, ''), { status: res.status, ms: res.ms, code: bad ? bad.code : 'ok', reason: bad ? bad.reason : undefined });
  return { res, bad };
}

const HOLD_MSG = { challenge: 'Instagram security check: complete it in the Instagram tab, then Resume',
  login: 'Log in to Instagram, then Resume' };
// Records a failed job step. Local codes (network, unsupported, busy) keep the job for a retry and tell the server nothing.
async function fail(job, bad, what, res, bucket) {
  const now = Date.now(), sample = res ? FL.sampleOf(res, 1500) : '';
  const local = bad.code === 'network' || bad.code === 'unsupported' || bad.code === 'busy';
  const line = bad.code + (bad.reason ? ' (' + bad.reason + ')' : '') + ' on ' + what;
  const st = await editSt((st) => {
    if (bad.code === 'rate_limit' || bad.code === 'soft_block') FL.applyHit(st, now, bad.retryAt, bucket);
    else if (HOLD_MSG[bad.code]) st.hold = { code: bad.code, message: HOLD_MSG[bad.code], at: now };
    else if (bad.code === 'other') FL.backoff(st, now, 'other');
    else if (bad.code === 'network') FL.backoff(st, now, 'net');
    else if (bad.code === 'unsupported') st.infoOffUntil = now + 6 * FL.HOUR;
    if (bad.code !== 'busy') st.lastError = hhmmss(now).slice(0, 5) + ' ' + line + (sample ? ' | ' + sample.slice(0, 300) : '');
  });
  if (bad.code !== 'busy') {
    const d = { at: iso(now), version: VERSION, what, code: bad.code, reason: bad.reason || null, status: res ? res.status : 0, sample };
    await locked(async () => set({ debug: d, debugLog: ((await get('debugLog')) || []).concat(d).slice(-5) }));
  }
  if (local) return;
  await set({ cur: null });
  const cd = st.cool[bucket] && st.cool[bucket].until > now ? st.cool[bucket].until : 0;
  await queue('/api/ext/error', { job_id: job.id, code: bad.code, retry_at: cd ? iso(cd) : null,
    message: String(line + ' (HTTP ' + (res ? res.status : 0) + ')' + (sample ? ' | ' + sample : '')) });
}
// Waits for the pace gap while keeping heartbeats going; false if paused, blocked or superseded meanwhile.
async function waitUntil(gen, ts) {
  while (Date.now() < ts) {
    await sleep(Math.min(15e3, ts - Date.now()));
    if (gen !== mem.gen) return false;
    mem.beat = Date.now();
    await heartbeat();
    const st = await loadSt();
    if (st.hold || (await get('localPaused'))) return false;
  }
  return true;
}
const markSeen = (handle) => locked(async () => {
  const seen = (await get('seen')) || {}, now = Date.now(), key = handle.toLowerCase();
  for (const k in seen) if (now - seen[k] > 6 * FL.HOUR) delete seen[k];
  const fresh = !seen[key];
  seen[key] = now;
  await set({ seen });
  return fresh;
});
// Only writes when something changed (passive capture can fire many times per page).
const remember = (p) => locked(async () => {
  if (!p || !p.ig_id || !p.handle) return;
  const ids = (await get('ids')) || {}, old = ids[p.handle.toLowerCase()], now = Date.now();
  if (old && old.ig_id === String(p.ig_id) && old.followers === (p.followers ?? null) && now - old.at < FL.DAY) return;
  await set({ ids: FL.rememberId(ids, p, now, 3000) });
});
const knownId = async (handle) => ((await get('ids')) || {})[String(handle || '').toLowerCase()] || null;
async function done(job) { await set({ cur: null }); await editSt((st) => FL.succeeded(st)); }

// ---- list pages --------------------------------------------------------------
// prog[seed/direction] = {cursor (last requested), next, received, pages, total, emptyAt}: local progress, so a
// restarted worker knows where the crawl is and the empty-page guard can tell a repeat from a first block.
const editProg = (key, fn) => edit('prog', (all) => { all = all || {}; all[key] = fn(all[key] || {}); return all; });
async function runList(gen, job, tab) {
  const key = job.seed.toLowerCase() + '/' + job.direction, cursor = job.cursor || null;
  let prog = ((await get('prog')) || {})[key] || {};
  if (!cursor && prog.next) prog = {}; // the server restarted this list from the top
  mem.label = '@' + job.seed + ' ' + job.direction + ' · page ' + ((cursor ? prog.pages || 0 : 0) + 1);
  const cached = await knownId(job.seed);
  let igId = job.ig_id || (cached && cached.ig_id), total = prog.total ?? (cached ? cached[job.direction] : null) ?? null;
  if (!igId) {
    // web_profile_info 429s for scripts (RESEARCH.md); let Instagram load the profile page itself and read its own data.
    const r = await lookupViaPage(gen, job.seed, 'list', tab);
    if (!r.p || !r.p.ig_id) return fail(job, r.bad || { code: 'other', reason: 'no_ig_id' }, '@' + job.seed + ' lookup', r.res, 'list');
    igId = r.p.ig_id;
    total = job.direction === 'followers' ? r.p.followers : r.p.following;
    await editProg(key, (p) => ({ ...p, total }));
    if (!(await waitUntil(gen, (await loadSt()).nextAt))) return; // job stays in `cur` and resumes
  }
  // Followers: the web app sends search_surface=follow_list_page; Instagram caps follower pages at ~25 whatever count says.
  const url = IG + '/api/v1/friendships/' + igId + '/' + job.direction + '/?count=' + (job.direction === 'following' ? 50 : 25) +
    (cursor ? '&max_id=' + encodeURIComponent(cursor) : '') + (job.direction === 'followers' ? '&search_surface=follow_list_page' : '');
  const ctx = { cursor, total, received: cursor ? prog.received || 0 : 0, emptyAt: prog.emptyAt ?? null };
  const { res, bad } = await igRequest(gen, tab, url, 'list', ctx);
  if (bad) {
    if (/^empty_/.test(bad.reason || '')) await editProg(key, (p) => ({ ...p, emptyAt: cursor || '' }));
    return fail(job, bad, mem.label, res, 'list');
  }
  const page = FL.parsePage(res.json), now = Date.now();
  await editProg(key, (p) => ({ ...(cursor ? p : {}), cursor, next: page.next_cursor, received: (cursor ? p.received || 0 : 0) + page.users.length,
    pages: (cursor ? p.pages || 0 : 0) + 1, total, emptyAt: null, limited: page.limited, at: now }));
  await editSt((st) => {
    FL.logPage(FL.tally(st, now, page.users.length, 0), now, page.users.length);
    if (page.limited) st.note = '@' + job.seed + ' ' + job.direction + ' capped by Instagram; kept what it returned and moved on';
  });
  if (page.limited) await trail('list capped', { seed: job.seed, direction: job.direction });
  await queueDone('/api/ext/list-page', { job_id: job.id, seed: job.seed, ig_id: igId, direction: job.direction, users: page.users,
    next_cursor: page.next_cursor, done: page.done, total, limited: page.limited || undefined });
}

// ---- profile reads (bios) ----------------------------------------------------
// /api/v1/users/{pk}/info/ from the tab. Without a pk: the pk cache from passive capture, else a profile page load
// (never web_profile_info). If /info/ turns out to refuse web clients it is switched off for 6 h and page loads are used.
async function runProfile(gen, job, tab) {
  mem.label = '@' + job.handle + ' profile';
  const st = await loadSt(), cached = await knownId(job.handle);
  const pk = job.ig_id || (cached && cached.ig_id);
  let p;
  if (pk && !(st.infoOffUntil > Date.now())) {
    const { res, bad } = await igRequest(gen, tab, IG + '/api/v1/users/' + encodeURIComponent(pk) + '/info/', 'profile');
    if (bad) return fail(job, bad, mem.label, res, 'profile');
    p = FL.mapProfile(FL.userOf(res.json));
  } else {
    const r = await lookupViaPage(gen, job.handle, 'profile', tab);
    if (!r.p) return fail(job, r.bad, mem.label + ' (page)', r.res, 'profile');
    p = r.p;
  }
  await remember(p);
  await markSeen(p.handle);
  await editSt((st) => FL.tally(st, Date.now(), 0, 1));
  await queueDone('/api/ext/profile', { job_id: job.id, profile: p });
}

// ---- the loop ----------------------------------------------------------------
async function nextJob(kinds) {
  const cur = await get('cur'), now = Date.now();
  if (cur && cur.job && now - cur.at < CUR_TTL) {
    if (kinds.includes(cur.job.kind)) return { job: cur.job };
    return { job: null, wait: 15e3 }; // our leased job's bucket isn't ready yet; don't lease another meanwhile
  }
  if (cur) await set({ cur: null });
  let next;
  try {
    next = (await api('/api/ext/next?kinds=' + kinds.join(','))).json || {};
    mem.offline = false; mem.backoff = 5e3;
  } catch { mem.offline = true; return { job: null, wait: (mem.backoff = Math.min(mem.backoff * 2, 60e3)) }; }
  await applyServer({ paused: !!next.paused, budget: next.budget });
  if (next.paused) return { job: null, wait: 15e3 };
  if (!next.job) return { job: null, wait: 30e3 };
  await set({ cur: { job: next.job, at: now } });
  return { job: next.job };
}
// One step of the loop: returns how long to wait before the next step.
async function step(gen) {
  const now = Date.now();
  const st = await loadSt();
  mem.budgetDone = false; mem.laneWait = false;
  if (st.hold || (await get('localPaused'))) return 15e3;
  if (!(await flushBox())) return (mem.backoff = Math.min(mem.backoff * 2, 60e3));
  const lane = await get('lane');
  mem.laneWait = FL.laneBusy(lane, now);
  if (mem.laneWait) return lane.until - now;
  const pl = FL.plan(st, FL.budgetLeft(st, await get('budget'), now), now);
  if (!pl.kinds.length) { mem.budgetDone = pl.why === 'budget'; return pl.wait; }
  const { job, wait } = await nextJob(pl.kinds);
  mem.job = job || null;
  if (!job) { mem.noTab = null; return wait; }
  const t = await pickTab(); // after leasing: never opens or wakes a tab while the queue is empty
  if (!t.tab) { mem.job = null; return t.wait; }
  if (st.lastError && /^Open instagram/i.test(st.lastError)) await editSt((s) => { s.lastError = null; });
  await status();
  try {
    await (job.kind === 'list' ? runList(gen, job, t.tab) : runProfile(gen, job, t.tab));
  } finally { mem.job = null; }
  return 1e3;
}
async function loop() {
  if (mem.looping && Date.now() - mem.beat < LOOP_STALE) return;
  if (mem.looping) await trail('loop stalled; restarting', { since: hhmmss(mem.beat) });
  const gen = ++mem.gen;
  mem.looping = true; mem.beat = Date.now();
  try {
    while (gen === mem.gen) {
      let wait = 30e3;
      try { wait = await step(gen); } catch (e) {
        if (e instanceof Superseded) break;
        const m = String((e && e.message) || e).slice(0, 200);
        await editSt((s) => { s.lastError = 'extension error: ' + m; });
        await trail('step threw', { err: m });
      }
      if (gen !== mem.gen) break;
      mem.beat = Date.now();
      await heartbeat();
      await status();
      await sleep(Math.max(1e3, Math.min(wait, 15e3)));
    }
  } finally { if (gen === mem.gen) mem.looping = false; }
}

// ---- profile page loads (seed ig_id lookup, bios without a pk) ---------------
const waiters = {};
async function tabDom(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId);
    const [r] = await withTimeout(5e3, chrome.scripting.executeScript({ target: { tabId }, func: () =>
      ({ url: location.href, title: document.title, text: (document.body && document.body.innerText || '').slice(0, 400) }) }));
    return (r && r.result) || { url: tab.url, title: tab.title };
  } catch { try { const tab = await chrome.tabs.get(tabId); return { url: tab.url, title: tab.title }; } catch { return null; } }
}
async function lookupViaPage(gen, handle, kind, near) {
  if (gen !== mem.gen) throw new Superseded();
  if (FL.laneBusy(await get('lane'), Date.now())) return { p: null, bad: { code: 'busy' } };
  const key = handle.toLowerCase(), url = IG + '/' + encodeURIComponent(handle) + '/', t0 = Date.now();
  let tab;
  await set({ lane: { until: Date.now() + 60e3, url } });
  try {
    const got = new Promise((r) => { waiters[key] = r; setTimeout(() => r(null), 25e3); });
    const where = near ? { windowId: near.windowId, index: near.index + 1 } : {};
    tab = await chrome.tabs.create({ url, active: false, ...where });
    mem.lookups.add(tab.id);
    await set({ lookupTab: tab.id });
    await editSt((st) => FL.afterRequest(st, kind, Date.now()));
    const p = await got;
    if (p) { await trail(kind + ' page /' + handle + '/', { ms: Date.now() - t0, code: 'ok' }); return { p }; }
    const info = await tabDom(tab.id), bad = FL.pageVerdict(info);
    const res = { status: 0, url: info && info.url, text: info ? 'title: ' + info.title + ' | ' + (info.text || '') : 'lookup tab closed', ms: Date.now() - t0 };
    await trail(kind + ' page /' + handle + '/', { ms: res.ms, code: bad.code, reason: bad.reason });
    return { p: null, bad, res };
  } catch (e) {
    if (e instanceof Superseded) throw e;
    return { p: null, bad: { code: 'network', reason: 'lookup_tab' }, res: { status: 0, text: String((e && e.message) || e) } };
  } finally {
    delete waiters[key];
    if (tab) { mem.lookups.delete(tab.id); chrome.tabs.remove(tab.id).catch(() => {}); await set({ lookupTab: null }); }
    await set({ lane: null });
  }
}

// ---- passive capture from normal browsing (zero extra requests) -----------
async function passive(user) {
  const p = FL.mapProfile(user);
  if (!p || !p.ig_id) return;
  await remember(p);
  const w = waiters[p.handle.toLowerCase()];
  if (w) return w(p); // a lookup asked for this one; its job posts it
  if (p.bio === null || !(await markSeen(p.handle))) return;
  await editSt((st) => FL.tally(st, Date.now(), 0, 1));
  await queue('/api/ext/profile', { job_id: null, profile: p });
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg && msg.type === 'fl-profile' && sender.tab) passive(msg.user).catch(() => {});
  if (msg && msg.cmd === 'pause') set({ localPaused: true }).then(() => status()).then(() => reply({ ok: true }));
  if (msg && msg.cmd === 'resume') {
    set({ localPaused: false }).then(() => editSt((s) => { s.hold = null; s.lastError = null; FL.succeeded(s); }))
      .then(() => { mem.lastBeat = 0; mem.badTab = null; return status(); }).then(() => { reply({ ok: true }); loop(); });
  }
  return !!(msg && msg.cmd);
});

// ---- lifecycle -------------------------------------------------------------
async function ensureAlarm() {
  if (!(await chrome.alarms.get('tick'))) await chrome.alarms.create('tick', { periodInMinutes: 0.5 });
}
async function wake(reason) {
  await edit('boot', (b) => ({ n: ((b && b.n) || 0) + 1, at: iso(Date.now()), reason, version: VERSION })).catch(() => {});
  // A lookup tab left behind by a stopped worker.
  const stale = await get('lookupTab');
  if (stale != null && !mem.lookups.size) { chrome.tabs.remove(stale).catch(() => {}); await set({ lookupTab: null }); }
  await ensureAlarm();
}
chrome.runtime.onInstalled.addListener((d) => { trail('installed', { reason: d && d.reason }); ensureAlarm(); loop(); });
chrome.runtime.onStartup.addListener(() => { trail('browser startup'); ensureAlarm(); loop(); });
chrome.alarms.onAlarm.addListener(() => { ensureAlarm(); heartbeat(); loop(); });
wake('worker').finally(loop);

// The workspace can reload the extension after an update (loopback origin only).
chrome.runtime.onMessageExternal.addListener((msg, sender, respond) => {
  if (sender.origin !== SERVER || msg?.type !== 'RELOAD') return false;
  respond({ ok: true, version: VERSION });
  // clearCooldown: only for hits that weren't account limits (e.g. the retired web_profile_info lookup).
  const clear = msg.clearCooldown ? editSt((st) => { for (const k of FL.KINDS) st.cool[k] = { until: 0, hits: [] }; st.lastError = null; }) : Promise.resolve();
  clear.finally(() => setTimeout(() => chrome.runtime.reload(), 200));
  return false;
});
