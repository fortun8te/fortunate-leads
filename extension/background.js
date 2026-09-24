// Fortunate Leads: paced executor. Server owns the queue; this worker owns pacing, budgets and cooldowns.
importScripts('lib/core.js');
const SERVER = 'http://127.0.0.1:8777';
const IG = 'https://www.instagram.com';
const VERSION = chrome.runtime.getManifest().version;
const mem = { looping: false, serverPaused: false, offline: false, noTab: false, budgetDone: false, job: null, label: '',
  backoff: 5e3, lastBeat: 0, pages: {} };

const sleep = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms)));
const get = async (k) => (await chrome.storage.local.get(k))[k];
const set = (o) => chrome.storage.local.set(o);
let chain = Promise.resolve();
function locked(fn) { const p = chain.then(fn); chain = p.catch(() => {}); return p; }
async function loadSt() { return FL.rollDay({ ...FL.fresh(), ...(await get('st')) }, Date.now()); }
const editSt = (fn) => locked(async () => { const st = await loadSt(); await fn(st); await set({ st }); return st; });
const iso = (t) => (t ? new Date(t).toISOString() : null);

// ---- server I/O ----------------------------------------------------------
async function api(path, body) {
  const r = await fetch(SERVER + path, body === undefined ? { headers: { 'X-FL': '1' } } :
    { method: 'POST', headers: { 'content-type': 'application/json', 'X-FL': '1' }, body: JSON.stringify(body) });
  return { status: r.status, json: await r.json().catch(() => ({})) };
}
async function sendItem(item) {
  try { const r = await api(item.path, item.body); return r.status >= 500 ? 'retry' : 'ok'; } catch { return 'retry'; }
}
const flushBox = () => locked(async () => {
  const box = await FL.flush(await get('box'), sendItem);
  await set({ box });
  mem.offline = box.length > 0;
  return !box.length;
});
async function queue(path, body) {
  await locked(async () => set({ box: FL.enqueue(await get('box'), path, body) }));
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
  const st = await loadSt(), s = await status(st);
  try {
    const r = await api('/api/ext/heartbeat', { version: VERSION, state: s.state, cooldown_until: st.cooldownUntil > Date.now() ? iso(st.cooldownUntil) : null,
      today: { list: st.today.list, profile: st.today.profile }, budget: FL.budgetOf(await get('budget')), last_error: st.hold ? st.hold.message : st.lastError,
      activity: mem.label || null, text: s.text, people_today: st.today.people || 0,
      rate: FL.rateOf(st, Date.now()) });
    mem.offline = false;
    await applyServer(r.json);
  } catch { mem.offline = true; }
}

// ---- status, badge, popup view ------------------------------------------
async function status(st) {
  st = st || await loadSt();
  const s = FL.statusOf(st, { ...mem, localPaused: !!(await get('localPaused')) }, Date.now());
  chrome.action.setBadgeText({ text: s.badge });
  chrome.action.setBadgeBackgroundColor({ color: s.badge === '!' ? '#b3261e' : '#555' });
  await set({ view: { ...s, today: st.today, budget: FL.budgetOf(await get('budget')), job: mem.job ? mem.label : '',
    lastError: st.hold ? st.hold.message : st.lastError, rate: FL.rateOf(st, Date.now()), at: Date.now() } });
  return s;
}

// ---- Instagram requests: one lane, inside a real instagram.com tab ---------
// A frozen or navigating tab can leave executeScript pending forever; never let that stall the loop.
const withTimeout = (ms, p) => Promise.race([p, new Promise((_, no) => setTimeout(() => no(new Error('Instagram tab did not respond')), ms))]);
async function igTab() {
  const tabs = (await chrome.tabs.query({ url: IG + '/*' })).filter((t) => !t.discarded && !t.frozen && t.status === 'complete');
  return tabs.find((t) => t.active) || tabs[0] || null;
}
async function inTab(tabId, url) {
  try {
    const [r] = await withTimeout(40e3, chrome.scripting.executeScript({ target: { tabId }, world: 'MAIN', args: [url], func: async (u) => {
      const csrf = decodeURIComponent((document.cookie.match(/(?:^|; )csrftoken=([^;]+)/) || [])[1] || '');
      let claim = '0'; try { claim = sessionStorage.getItem('www-claim-v2') || '0'; } catch {}
      const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 30e3);
      try {
        const res = await (window.__flFetch || fetch)(u, { credentials: 'include', signal: ctl.signal, headers: {
          'X-IG-App-ID': '936619743392459', 'X-CSRFToken': csrf, 'X-ASBD-ID': '129477', 'X-IG-WWW-Claim': claim,
          'X-Requested-With': 'XMLHttpRequest' } });
        const claimOut = res.headers.get('x-ig-set-www-claim');
        if (claimOut) try { sessionStorage.setItem('www-claim-v2', claimOut); } catch {}
        return { status: res.status, text: await res.text(), retryAfter: res.headers.get('retry-after'),
          contentType: res.headers.get('content-type') || '', url: res.url, redirected: res.redirected };
      } catch (e) { return { status: 0, text: String(e) }; } finally { clearTimeout(timer); }
    } }));
    const p = (r && r.result) || { status: 0, text: '' };
    p.json = FL.parseBody(p.text);
    return p;
  } catch (e) { return { status: 0, text: String(e), json: null }; }
}
async function igRequest(tab, url, kind) {
  await set({ lane: { until: Date.now() + 60e3 } }); // a restarted worker waits this out instead of double-firing
  let res;
  try { res = await inTab(tab.id, url); } finally {
    await editSt((st) => FL.afterRequest(st, kind, Date.now()));
    await set({ lane: null });
  }
  return { res, bad: FL.classify(res, url.includes('/friendships/') ? 'list' : 'profile') };
}
async function fail(job, bad, what) {
  const now = Date.now(), msg = { challenge: 'Instagram security check: complete it, then Resume', login: 'Log in to Instagram, then Resume' };
  const st = await editSt((st) => {
    if (bad.code === 'rate_limit' || bad.code === 'soft_block') FL.applyHit(st, now, bad.retryAt);
    else if (msg[bad.code]) st.hold = { code: bad.code, message: msg[bad.code], at: now };
    else if (bad.code === 'other') st.nextAt = Math.max(st.nextAt, now + 2 * FL.MIN);
    st.lastError = bad.code + ' on ' + what + (bad.sample ? ' | ' + bad.sample.slice(0, 300) : '');
  });
  // Full raw sample kept locally too (chrome.storage.local 'debug') for inspection from the service-worker console.
  if (bad.sample) await set({ debug: { at: iso(now), what, code: bad.code, sample: bad.sample } });
  await queue('/api/ext/error', { job_id: job.id, code: bad.code, retry_at: st.cooldownUntil > now ? iso(st.cooldownUntil) : null,
    message: String(bad.code + ' on ' + what + ' (HTTP ' + (bad.status || 0) + ')' + (bad.sample ? ' | ' + bad.sample : '')) });
}
// Waits for the pace gap while keeping heartbeats going; false if paused or blocked meanwhile.
async function waitUntil(ts) {
  while (Date.now() < ts) {
    await sleep(Math.min(15e3, ts - Date.now()));
    await heartbeat();
    const st = await loadSt();
    if (st.hold || st.cooldownUntil > Date.now() || (await get('localPaused'))) return false;
  }
  return true;
}
async function markSeen(handle) {
  const seen = (await get('seen')) || {}, now = Date.now();
  for (const k in seen) if (now - seen[k] > 6 * FL.HOUR) delete seen[k];
  const fresh = !seen[handle.toLowerCase()];
  seen[handle.toLowerCase()] = now;
  await set({ seen });
  return fresh;
}

async function runList(job, tab) {
  const key = job.seed + '/' + job.direction;
  let igId = job.ig_id, total = null;
  mem.label = '@' + job.seed + ' ' + job.direction + ' · page ' + ((mem.pages[key] || 0) + 1);
  if (!igId) {
    // web_profile_info 429s for scripts (RESEARCH.md); let Instagram load the profile page itself and read its own data.
    const p = await lookupViaPage(job.seed);
    if (!p || !p.ig_id) return fail(job, { code: 'other', status: 0 }, '@' + job.seed + ' lookup');
    igId = p.ig_id;
    total = job.direction === 'followers' ? p.followers : p.following;
    if (!(await waitUntil((await loadSt()).nextAt))) return;
  }
  // Followers: the web app sends search_surface=follow_list_page; Instagram caps follower pages at ~25 whatever count says.
  const url = IG + '/api/v1/friendships/' + igId + '/' + job.direction + '/?count=' + (job.direction === 'following' ? 50 : 25) +
    (job.cursor ? '&max_id=' + encodeURIComponent(job.cursor) : '') + (job.direction === 'followers' ? '&search_surface=follow_list_page' : '');
  const { res, bad } = await igRequest(tab, url, 'list');
  if (bad) return fail(job, { ...bad, status: res.status, sample: bad.code === 'other' ? FL.sampleOf(res) : '' }, mem.label);
  const page = FL.parsePage(res.json);
  mem.pages[key] = page.done ? 0 : (mem.pages[key] || 0) + 1;
  if (page.limited) await editSt((st) => { st.lastError = '@' + job.seed + ' list capped by Instagram; kept what it returned'; });
  await editSt((st) => FL.logPage(FL.tally(st, Date.now(), page.users.length, 0), Date.now(), page.users.length));
  await queue('/api/ext/list-page', { job_id: job.id, seed: job.seed, ig_id: igId, direction: job.direction, users: page.users,
    next_cursor: page.next_cursor, done: page.done, total });
}
async function runProfile(job, tab) {
  mem.label = '@' + job.handle + ' profile';
  let p;
  if (job.ig_id) {
    const { res, bad } = await igRequest(tab, IG + '/api/v1/users/' + job.ig_id + '/info/', 'profile');
    p = !bad && FL.mapProfile(FL.userOf(res.json));
    if (bad || !p) return fail(job, { ...(bad || { code: 'other' }), status: res.status, sample: !bad || bad.code === 'other' ? FL.sampleOf(res, 600) : '' }, mem.label);
  } else {
    p = await lookupViaPage(job.handle);
    if (!p) return fail(job, { code: 'not_found', status: 0 }, mem.label);
  }
  await markSeen(p.handle);
  await editSt((st) => FL.tally(st, Date.now(), 0, 1));
  await queue('/api/ext/profile', { job_id: job.id, profile: p });
}

// One step of the loop: returns how long to wait before the next step.
async function step() {
  const now = Date.now();
  let st = await loadSt();
  mem.budgetDone = false;
  if (st.hold || (await get('localPaused'))) return 15e3;
  if (!(await flushBox())) return (mem.backoff = Math.min(mem.backoff * 2, 60e3));
  if (st.cooldownUntil > now) return st.cooldownUntil - now;
  const lane = await get('lane');
  if (Math.max(st.nextAt, (lane && lane.until) || 0) > now) return Math.max(st.nextAt, (lane && lane.until) || 0) - now;
  const left = FL.budgetLeft(st, await get('budget'), now);
  if (!left.list && !left.profile) { mem.budgetDone = true; return 10 * FL.MIN; }
  const tab = await igTab();
  mem.noTab = !tab;
  if (!tab) { await editSt((s) => { s.lastError = 'Open instagram.com'; }); return 30e3; }
  let next;
  try {
    next = (await api('/api/ext/next?kinds=' + ['list', 'profile'].filter((k) => left[k]).join(','))).json;
    mem.offline = false; mem.backoff = 5e3;
  } catch { mem.offline = true; return (mem.backoff = Math.min(mem.backoff * 2, 60e3)); }
  await applyServer({ paused: !!next.paused, budget: next.budget });
  if (next.paused) return 15e3;
  const job = next.job;
  mem.job = job || null;
  if (!job) return 30e3;
  if (!left[job.kind]) {
    await queue('/api/ext/error', { job_id: job.id, code: 'other', retry_at: iso(FL.nextMidnight(now)), message: 'daily ' + job.kind + ' budget reached' });
    return 5 * FL.MIN;
  }
  if (st.lastError === 'Open instagram.com') await editSt((s) => { s.lastError = null; });
  if (job.kind === 'profile' && !(await waitUntil(st.profileNextAt))) return 1e3;
  await status();
  await (job.kind === 'list' ? runList(job, tab) : runProfile(job, tab));
  mem.job = null;
  return 1e3;
}
async function loop() {
  if (mem.looping) return;
  mem.looping = true;
  try {
    for (;;) {
      let wait = 30e3;
      try { wait = await step(); } catch (e) { await editSt((s) => { s.lastError = String(e && e.message || e).slice(0, 200); }); }
      await heartbeat();
      await status();
      await sleep(Math.min(wait, 15e3));
    }
  } finally { mem.looping = false; }
}

// ---- passive capture from normal browsing (zero extra requests) -----------
const waiters = {};
async function lookupViaPage(handle) {
  const key = handle.toLowerCase();
  let tab;
  try {
    const got = new Promise((r) => { waiters[key] = r; setTimeout(() => r(null), 25e3); });
    tab = await chrome.tabs.create({ url: IG + '/' + encodeURIComponent(handle) + '/', active: false });
    await editSt((st) => FL.afterRequest(st, 'list', Date.now()));
    return await got;
  } finally {
    delete waiters[key];
    if (tab) chrome.tabs.remove(tab.id).catch(() => {});
  }
}
async function passive(user) {
  const p = FL.mapProfile(user);
  if (p && p.handle && waiters[p.handle.toLowerCase()]) waiters[p.handle.toLowerCase()](p);
  if (!p || !p.ig_id || p.bio === null || !(await markSeen(p.handle))) return;
  await editSt((st) => FL.tally(st, Date.now(), 0, 1));
  await queue('/api/ext/profile', { job_id: null, profile: p });
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg && msg.type === 'fl-profile' && sender.tab) passive(msg.user).catch(() => {});
  if (msg && msg.cmd === 'pause') set({ localPaused: true }).then(() => status()).then(() => reply({ ok: true }));
  if (msg && msg.cmd === 'resume') {
    set({ localPaused: false }).then(() => editSt((s) => { s.hold = null; s.lastError = null; }))
      .then(() => { mem.lastBeat = 0; return status(); }).then(() => { reply({ ok: true }); loop(); });
  }
  return !!(msg && msg.cmd);
});
const boot = () => { chrome.alarms.create('tick', { periodInMinutes: 0.5 }); loop(); };
chrome.runtime.onInstalled.addListener(boot);
chrome.runtime.onStartup.addListener(boot);
chrome.alarms.onAlarm.addListener(() => { heartbeat(); loop(); });
loop();

// The workspace can reload the extension after an update (loopback origin only).
chrome.runtime.onMessageExternal.addListener((msg, sender, respond) => {
  if (sender.origin !== SERVER || msg?.type !== 'RELOAD') return false;
  respond({ ok: true, version: VERSION });
  // clearCooldown: only for hits that weren't account limits (e.g. the retired web_profile_info lookup).
  const clear = msg.clearCooldown ? editSt((st) => { st.cooldownUntil = 0; st.hits = []; st.lastError = null; }) : Promise.resolve();
  clear.finally(() => setTimeout(() => chrome.runtime.reload(), 200));
  return false;
});
