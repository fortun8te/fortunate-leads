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
class ControlPaused extends Error {}

// Short ring of recent events (requests, failures, restarts) for the popup's "Copy debug".
const trail = (what, extra) => edit('trail', (t) => (t || []).concat({ at: hhmmss(Date.now()), what, ...(extra || {}) }).slice(-40)).catch(() => {});

// ---- lane identity: this install (laneId) and the Instagram account logged in to this Chrome profile ------
// Several Chrome profiles can each run this extension against the same server; the server leases jobs per lane.
async function ident() {
  let { laneId, account } = await chrome.storage.local.get(['laneId', 'account']);
  if (!laneId) {
    laneId = await locked(async () => {
      const cur = await get('laneId');
      if (cur) return cur;
      const id = FL.newLaneId();
      await set({ laneId: id, startOffset: FL.startOffset() });
      return id;
    });
  }
  return { lane_id: laneId, account: account ? { ig_id: account.ig_id || null, handle: account.handle || null } : undefined };
}
// Adds lane + account to a server body (outbox items are tagged when queued, so a retry sends the same bytes).
async function tagged(body) {
  const id = await ident();
  return body && body.lane_id === undefined ? { ...body, lane_id: id.lane_id, ...(id.account ? { account: id.account } : {}) } : body;
}
// Reads ds_user_id and the handle from an already loaded instagram.com tab (no network request). Throttled.
async function whoami(force) {
  const acc = await get('account'), now = Date.now();
  if (!force && acc && now - acc.at < (acc.ig_id && acc.handle ? 10 * FL.MIN : FL.MIN)) return acc;
  let tabs = [];
  try { tabs = (await chrome.tabs.query({ url: IG + '/*' })).filter((t) => t.status === 'complete' && !t.discarded && !mem.lookups.has(t.id) && FL.pageKind(t.url) === 'ok'); } catch {}
  if (!tabs.length) return acc;
  let res = null;
  try {
    const [r] = await withTimeout(5e3, chrome.scripting.executeScript({ target: { tabId: tabs[0].id }, world: 'MAIN', func: () => {
      const m = document.cookie.match(/(?:^|;\s*)ds_user_id=(\d+)/), uid = m && m[1], snips = [];
      if (uid) {
        document.querySelectorAll('script[type="application/json"]').forEach((s) => {
          const t = s.textContent || '';
          for (let i = t.indexOf('"' + uid + '"'); i >= 0 && snips.length < 40; i = t.indexOf('"' + uid + '"', i + 1)) snips.push(t.slice(Math.max(0, i - 400), i + 400));
        });
      }
      return { host: location.hostname, ready: document.readyState, cookie: uid ? 'ds_user_id=' + uid : '', text: snips.join('\n') };
    } }));
    res = r && r.result;
  } catch {}
  if (!res || res.host !== 'www.instagram.com' || res.ready !== 'complete') return acc;
  const a = FL.accountFrom(res.cookie, res.text);
  if (a.ig_id && !a.handle) { // the page did not name it: the id cache (passive capture) or what we knew before may
    if (acc && acc.ig_id === a.ig_id && acc.handle) a.handle = acc.handle;
    else { const ids = (await get('ids')) || {}; a.handle = Object.keys(ids).find((h) => ids[h].ig_id === a.ig_id) || null; }
  }
  const next = { ...a, at: now };
  // Keep pacing state per Instagram id. A switch must neither replay the old
  // account's limits on the new one nor erase them if the old account returns.
  // The active state stays in `st`; inactive states live in `accountStates`.
  const { changed, switched } = await locked(async () => {
    const current = await get('account'), stored = await get('st');
    const owner = stored?.accountIgId || current?.ig_id || null;
    const switched = !!next.ig_id && owner !== next.ig_id;
    const values = { account: next };
    if (switched) {
      const states = (await get('accountStates')) || {};
      if (owner && stored) states[owner] = { st: { ...stored, accountIgId: owner }, at: now };
      const saved = states[next.ig_id];
      delete states[next.ig_id];
      values.accountStates = states;
      values.st = { ...(saved ? FL.normalize(saved.st, now) : FL.fresh()), accountIgId: next.ig_id };
      values.cur = null;
      values.prog = {};
      values.seen = {};
    } else if (stored && owner && stored.accountIgId !== owner) {
      values.st = { ...stored, accountIgId: owner };
    }
    await set(values);
    return { switched, changed: !current || current.ig_id !== next.ig_id || current.handle !== next.handle };
  });
  if (switched) { ++mem.gen; mem.looping = false; mem.job = null; mem.label = ''; }
  if (changed) { await trail('account', { ig_id: next.ig_id, handle: next.handle }); mem.lastBeat = 0; }
  return next;
}
// First request after a quiet spell (browser start, install, > 30 min without a request): wait this lane's own offset.
async function startOffset(reason) {
  await ident();
  const st = await loadSt(), last = st.rlog.length ? st.rlog[st.rlog.length - 1][0] : 0, now = Date.now();
  if (now - last < 30 * FL.MIN) return;
  const off = ((await get('startOffset')) ?? FL.startOffset()) + Math.round(Math.random() * 10e3);
  await editSt((s) => { s.nextAt = Math.max(s.nextAt || 0, now + off); });
  await trail('start offset', { s: Math.round(off / 1e3), reason });
}

// ---- server I/O (every call has a timeout: a hung local server must never stall the loop) ------
async function api(path, body, ms = 15e3) {
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), ms);
  const id = await ident();
  if (body === undefined) {
    const p = new URLSearchParams({ lane: id.lane_id });
    if (id.account) { p.set('ig_id', id.account.ig_id || ''); if (id.account.handle) p.set('handle', id.account.handle); }
    path += (path.includes('?') ? '&' : '?') + p;
  } else body = await tagged(body);
  try {
    const r = await fetch(SERVER + path, body === undefined ? { headers: { 'X-FL': '1' }, signal: ctl.signal } :
      { method: 'POST', headers: { 'content-type': 'application/json', 'X-FL': '1' }, body: JSON.stringify(body), signal: ctl.signal });
    return { status: r.status, json: await r.json().catch(() => null) };
  } finally { clearTimeout(timer); }
}
async function sendItem(item) {
  try {
    const r = await api(item.path, item.body);
    if (r.status >= 500 || r.status === 408 || r.status === 429) return 'retry';
    if (r.status !== 200 || r.json?.stale || r.json?.ok === false) return 'reject';
    // A successful HTTP status alone does not acknowledge a durable result.
    if (!r.json || typeof r.json !== 'object' || Array.isArray(r.json)) return 'retry';
    if (item.path === '/api/ext/list-page' && !Number.isSafeInteger(r.json.received)) return 'retry';
    if (item.path === '/api/ext/profile' && !Number.isSafeInteger(r.json.id)) return 'retry';
    return 'ok';
  } catch { return 'retry'; }
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
        const sending = box[0], r = await sendItem(sending);
        if (r === 'retry') { mem.offline = true; return false; }
        mem.offline = false;
        const path = box[0].path;
        const removed = await locked(async () => {
          const b = (await get('box')) || [];
          if (!sameItem(b[0], sending)) return false;
          const item = b.shift();
          await set(r !== 'ok' ? { box: b, dead: FL.park((await get('dead')) || [], { ...item, reason: r }) } : { box: b });
          return true;
        });
        if (!removed) continue; // another enqueue shed a passive head while it was in flight
        if (r === 'ok' && path === '/api/ext/list-page' && sending.body?.direction === 'followers')
          await editSt((st) => FL.listPageSucceeded(st, 'followers'));
        if (r !== 'ok') {
          await editSt((s) => { s.lastError = 'Server rejected a saved result; see debug log'; });
          await trail('outbox parked', { path, reason: r });
        }
        mem.beat = Date.now();
      }
    } finally { flushing = null; }
  })();
  return flushing;
}
const sameItem = (a, b) => !!a && !!b && (a.qid && b.qid ? a.qid === b.qid :
  a.path === b.path && JSON.stringify(a.body) === JSON.stringify(b.body));
async function queue(path, body) {
  body = await tagged(body);
  await locked(async () => set({ box: FL.enqueue(await get('box'), path, body) }));
  await flushBox();
}
// Enqueue a job's result and clear `cur` in one storage write, so a worker restarted after this point never
// re-runs a request whose result is already stored (step() flushes the outbox before leasing again).
async function queueDone(path, body, success = true, gen = mem.gen) {
  if (gen !== mem.gen) throw new Superseded();
  body = await tagged(body);
  await locked(async () => {
    if (gen !== mem.gen) throw new Superseded();
    await set({ box: FL.enqueue(await get('box'), path, body), cur: null });
  });
  if (success) await editSt((st) => { if (gen === mem.gen) FL.succeeded(st); });
  await flushBox();
}
async function applyServer(j) {
  if (j && j.budget && typeof j.budget === 'object') await set({ budget: j.budget });
  if (j && j.stages) mem.stages = j.stages;
  if (!j || typeof j.paused !== 'boolean') return;
  mem.serverPaused = j.paused;
  const st = await loadSt();
  if (!st.hold) return;
  // Workspace resume: the server flag went paused → not paused after the hold began.
  if (j.paused && !st.hold.sawPause) await editSt((s) => { if (s.hold) s.hold.sawPause = true; });
  if (!j.paused && st.hold.sawPause) await editSt((s) => { s.hold = null; s.lastError = null; });
}
// Auto-update: the extension is loaded unpacked from the repo, so a new version on disk (git pull) is picked up by reloading.
async function selfUpdate() {
  try {
    const m = await (await fetch(chrome.runtime.getURL('manifest.json'), { cache: 'no-store' })).json();
    if (m.version && m.version !== VERSION) { console.log('update', VERSION, '->', m.version); chrome.runtime.reload(); return true; }
  } catch {}
  return false;
}
async function heartbeat(force) {
  if (!force && Date.now() - mem.lastBeat < 25e3) return;
  mem.lastBeat = Date.now();
  if (await selfUpdate()) return;
  await whoami(true).catch(() => {});
  const st = await loadSt(), s = await status(st), now = Date.now(), cd = FL.cooldownUntil(st, now);
  const cool = { list: st.cool.list.until > now ? iso(st.cool.list.until) : null, profile: st.cool.profile.until > now ? iso(st.cool.profile.until) : null };
  try {
    const r = await api('/api/ext/heartbeat', { version: VERSION, state: s.state, cooldown_until: cd ? iso(cd) : null, cool,
      hold: st.hold ? st.hold.code : null, list_endpoint_until: st.listEndpointUntil > now ? iso(st.listEndpointUntil) : null,
      day: FL.dayKey(now), today: { list: st.today.list, profile: st.today.profile }, budget: FL.budgetOf(await get('budget')),
      last_error: st.hold ? st.hold.message : st.lastError, activity: mem.label || null, text: s.text, people_today: st.today.people || 0,
      rate: FL.rateOf(st, now), ready: { list: iso(FL.readyAt(st, 'list')), profile: iso(FL.readyAt(st, 'profile')) } }, 10e3);
    if (r.status !== 200 || typeof r.json?.paused !== 'boolean') throw new Error('Control state unavailable');
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
  const bucket = (k) => ({ until: st.cool[k].until > now ? st.cool[k].until : 0,
    hits: st.cool[k].hits.filter((t) => now - t < FL.DAY).length,
    left: Number.isFinite(left[k]) ? left[k] : null, readyAt: FL.readyAt(st, k) });
  await set({ view: { ...s, today: st.today, budget, job: mem.job ? mem.label : '', nextAt: Math.min(FL.readyAt(st, 'list'), FL.readyAt(st, 'profile')),
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
async function assertControl(kind) {
  await heartbeat(true);
  if (!FL.controlAllows(mem, kind) || await get('localPaused')) throw new ControlPaused();
}
async function igRequest(gen, tab, url, kind, ctx) {
  await assertControl(kind);
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
  if (gen !== mem.gen) throw new Superseded();
  const now = Date.now();
  if (res.sent) await editSt((st) => { if (gen !== mem.gen) throw new Superseded(); FL.afterRequest(st, kind, now); });
  if (res.tabError) mem.badTab = { id: tab.id, until: now + 2 * FL.MIN };
  const bad = FL.classify(res, kind, now, ctx);
  await trail(kind + ' ' + url.replace(IG, '').replace(/\?.*/, ''), { status: res.status, ms: res.ms, code: bad ? bad.code : 'ok', reason: bad ? bad.reason : undefined });
  return { res, bad };
}

const HOLD_MSG = { challenge: 'Instagram security check: complete it in the Instagram tab, then Resume',
  login: 'Log in to Instagram, then Resume' };
// Records a failed job step. Local codes (network, unsupported, busy) keep the job for a retry and tell the server nothing.
async function fail(job, bad, what, res, bucket, publicTarget = false, gen = mem.gen) {
  if (gen !== mem.gen) throw new Superseded();
  const now = Date.now(), sample = res ? FL.sampleOf(res, 1500) : '';
  const local = bad.code === 'network' || bad.code === 'unsupported' || bad.code === 'busy';
  const homeRedirect = bad.reason === 'list_html_home_redirect';
  const line = bad.code + (bad.reason ? ' (' + bad.reason + ')' : '') + ' on ' + what +
    (homeRedirect ? ': Instagram returned its home page for the list API; target will retry later.' : '');
  const st = await editSt((st) => {
    if (gen !== mem.gen) throw new Superseded();
    if (homeRedirect) FL.recordListRedirect(st, job.seed, job.direction, publicTarget, now);
    if (bad.code === 'rate_limit' || bad.code === 'soft_block') FL.applyHit(st, now, bad.retryAt, bucket);
    else if (HOLD_MSG[bad.code]) st.hold = { code: bad.code, message: HOLD_MSG[bad.code], at: now };
    else if (bad.code === 'other' && !homeRedirect) FL.backoff(st, now, 'other');
    else if (bad.code === 'network') FL.backoff(st, now, 'net');
    else if (bad.code === 'unsupported') st.infoOffUntil = now + 6 * FL.HOUR;
    if (bad.code !== 'busy') st.lastError = hhmmss(now).slice(0, 5) + ' ' + line +
      (homeRedirect && st.listEndpointUntil > now ? ' List API unavailable for this account until ' + hhmmss(st.listEndpointUntil).slice(0, 5) + '.' : '') +
      (sample ? ' | ' + sample.slice(0, 300) : '');
  });
  if (bad.code !== 'busy') {
    const d = { at: iso(now), version: VERSION, what, code: bad.code, reason: bad.reason || null, status: res ? res.status : 0, sample };
    await locked(async () => set({ debug: d, debugLog: ((await get('debugLog')) || []).concat(d).slice(-5) }));
  }
  if (local) return;
  const cd = st.cool[bucket] && st.cool[bucket].until > now ? st.cool[bucket].until : 0;
  await queueDone('/api/ext/error', { job_id: job.id, lease_token: job.lease_token, code: bad.code, retry_at: cd ? iso(cd) : null,
    event_id: Date.now().toString(36) + Math.random().toString(36).slice(2), kind: bucket,
    direction: job.direction || null, http_status: res ? res.status : 0,
    retry_after: bad.retryAt ? iso(bad.retryAt) : null,
    reason: bad.reason || null,
    message: String(line + ' (HTTP ' + (res ? res.status : 0) + ')' + (sample ? ' | ' + sample : '')) }, false, gen);
  if (homeRedirect && st.listEndpointUntil > now) await heartbeat(true);
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
const editProg = (key, fn, gen = mem.gen) => edit('prog', (all) => {
  if (gen !== mem.gen) throw new Superseded();
  all = all || {}; all[key] = fn(all[key] || {}); return all;
});
async function runList(gen, job, tab) {
  const key = job.seed.toLowerCase() + '/' + job.direction, cursor = job.cursor || null;
  let prog = FL.listProgress(job, ((await get('prog')) || {})[key]);
  mem.label = '@' + job.seed + ' ' + job.direction + ' · page ' + ((cursor ? prog.pages || 0 : 0) + 1);
  const cached = await knownId(job.seed);
  let igId = job.ig_id || (cached && cached.ig_id), total = FL.count(prog.total) ?? FL.count(cached && cached[job.direction]);
  // Counts from the handle cache guide progress but cannot prove this run saw everyone.
  let totalSource = total == null ? 'unknown' : FL.count(prog.total) != null && prog.jobId === job.id && prog.totalSource === 'current_run' ? 'current_run' : 'cached';
  // Refresh the seed once at the start of each run: an ID cache cannot prove a current count.
  if (!igId || !prog.countAttempted) {
    // web_profile_info 429s for scripts (RESEARCH.md); let Instagram load the profile page itself and read its own data.
    const r = await lookupViaPage(gen, job.seed, 'list', tab);
    if (FL.privateWall(r.info, job.seed, r.p))
      return fail(job, { code: 'private', reason: 'profile_private_wall' }, '@' + job.seed + ' ' + job.direction, r.res, 'list', false, gen);
    if (!r.p || !r.p.ig_id) return fail(job, r.bad || { code: 'other', reason: 'no_ig_id' }, '@' + job.seed + ' lookup', r.res, 'list', false, gen);
    igId = r.p.ig_id;
    total = FL.count(job.direction === 'followers' ? r.p.followers : r.p.following);
    totalSource = total == null ? 'unknown' : 'current_run';
    prog = { ...prog, jobId: job.id, total, totalSource, countAttempted: true };
    await editProg(key, () => prog, gen);
    if (!(await waitUntil(gen, FL.readyAt(await loadSt(), 'list')))) return; // job stays in `cur` and resumes
  }
  const requestedCount = job.direction === 'following' ? 50 : job.page_size === 50 ? 50 : 25;
  const url = IG + '/api/v1/friendships/' + igId + '/' + job.direction + '/?count=' + requestedCount +
    (cursor ? '&max_id=' + encodeURIComponent(cursor) : '') + (job.direction === 'followers' ? '&search_surface=follow_list_page' : '');
  // Another lane may have moved this list on since we last saw it: the server's count is then the one to trust.
  const ctx = FL.listContext(job, prog, total, totalSource);
  const { res, bad } = await igRequest(gen, tab, url, 'list', ctx);
  if (bad) {
    // Report the warning before any further Instagram request, including privacy checks.
    if (bad.reason === 'list_html_home_redirect')
      return fail(job, bad, mem.label, res, 'list', false, gen);
    if (bad.reason === 'empty_page_before_total') {
      // Keep this lease and cursor. The request already advanced the normal list
      // clock; a second empty terminal page is sent to the server as partial.
      await editProg(key, () => ({ ...prog, jobId: job.id, next: cursor, emptyAt: cursor }), gen);
      return;
    }
    if (bad.code === 'private' || (bad.code === 'soft_block' && /^empty_/.test(bad.reason || '')) ||
        (bad.code === 'other' && /^empty_/.test(bad.reason || ''))) {
      if (!(await waitUntil(gen, FL.readyAt(await loadSt(), 'list')))) return;
      const proof = await lookupViaPage(gen, job.seed, 'list', tab);
      if (FL.privateWall(proof.info, job.seed, proof.p))
        return fail(job, { code: 'private', reason: 'profile_private_wall' }, mem.label, proof.res || res, 'list', false, gen);

    }
    if (/^empty_/.test(bad.reason || '')) await editProg(key, () => ({ ...prog, jobId: job.id, next: cursor, emptyAt: cursor || '' }), gen);
    return fail(job, bad, mem.label, res, 'list', false, gen);
  }
  const freshTotal = FL.pageTotal(res.json);
  if (freshTotal != null) { total = freshTotal; totalSource = 'current_run'; }
  const page = FL.parsePage(res.json), now = Date.now();
  const stalled = !!(cursor && page.next_cursor === cursor && !page.done);
  await editProg(key, (p) => ({ ...(cursor && p.jobId === job.id ? p : {}), jobId: job.id, cursor, next: page.next_cursor, received: ctx.received + page.users.length,
    pages: (cursor ? p.pages || 0 : 0) + 1, total, totalSource, countAttempted: !!prog.countAttempted, emptyAt: null, limited: page.limited, at: now }), gen);
  await editSt((st) => {
    if (gen !== mem.gen) throw new Superseded();
    FL.logPage(FL.tally(st, now, page.users.length, 0), now, page.users.length);
    if (page.limited) st.note = '@' + job.seed + ' ' + job.direction + ' capped by Instagram; kept what it returned and moved on';
    if (stalled) st.note = '@' + job.seed + ' ' + job.direction + ' repeated its page cursor; saved the returned people, but the list is partial';
  });
  if (page.limited) await trail('list capped', { seed: job.seed, direction: job.direction });
  if (stalled) await trail('list cursor repeated', { seed: job.seed, direction: job.direction, cursor });
  await queueDone('/api/ext/list-page', { job_id: job.id, lease_token: job.lease_token, requested_cursor: job.cursor || null, seed: job.seed, ig_id: igId, direction: job.direction, users: page.users,
    next_cursor: page.next_cursor, done: page.done, total, total_source: totalSource, limited: page.limited || undefined,
    has_more: page.has_more, requested_count: requestedCount, http_status: res.status }, true, gen);
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
    if (bad) return fail(job, bad, mem.label, res, 'profile', false, gen);
    p = FL.mapProfile(FL.userOf(res.json));
  } else {
    const r = await lookupViaPage(gen, job.handle, 'profile', tab);
    if (!r.p) return fail(job, r.bad, mem.label + ' (page)', r.res, 'profile', false, gen);
    p = r.p;
  }
  await remember(p);
  await markSeen(p.handle);
  await editSt((st) => { if (gen !== mem.gen) throw new Superseded(); FL.tally(st, Date.now(), 0, 1); });
  await queueDone('/api/ext/profile', { job_id: job.id, lease_token: job.lease_token, profile: p }, true, gen);
}

// ---- the loop ----------------------------------------------------------------
async function nextJob(kinds) {
  await heartbeat(true);
  if (mem.offline || mem.serverPaused) return { job: null, wait: 15e3 };
  kinds = kinds.filter((kind) => FL.controlAllows(mem, kind));
  if (!kinds.length) return { job: null, wait: 15e3 };
  const cur = await get('cur'), now = Date.now();
  if (cur && cur.job && now - cur.at < CUR_TTL) {
    if (kinds.includes(cur.job.kind)) return { job: cur.job };
    return { job: null, wait: 15e3 }; // our leased job's bucket isn't ready yet; don't lease another meanwhile
  }
  if (cur) await set({ cur: null });
  let next;
  try {
    const r = await api('/api/ext/next?kinds=' + kinds.join(','));
    if (r.status !== 200 || !r.json || typeof r.json.paused !== 'boolean' || !r.json.stages || !Object.hasOwn(r.json, 'job'))
      throw new Error('Lease response unavailable');
    next = r.json;
    mem.offline = false; mem.backoff = 5e3;
  } catch { mem.offline = true; return { job: null, wait: (mem.backoff = Math.min(mem.backoff * 2, 60e3)) }; }
  await applyServer(next);
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
  if (!(await flushBox())) return (mem.backoff = Math.min(mem.backoff * 2, 60e3));
  if (st.hold || (await get('localPaused'))) return 15e3;
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
  await booted;
  if (mem.looping && Date.now() - mem.beat < LOOP_STALE) return;
  if (mem.looping) await trail('loop stalled; restarting', { since: hhmmss(mem.beat) });
  const gen = ++mem.gen;
  mem.looping = true; mem.beat = Date.now();
  try {
    while (gen === mem.gen) {
      let wait = 30e3;
      try { wait = await step(gen); } catch (e) {
        if (e instanceof Superseded) break;
        if (e instanceof ControlPaused) { await sleep(15e3); continue; }
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
      { const body = document.body && document.body.innerText || '';
        return { url: location.href, title: document.title, text: body.slice(0, 400),
          privateWall: /this (?:account|profile) is private/i.test(body) }; } }));
    return (r && r.result) || { url: tab.url, title: tab.title };
  } catch { try { const tab = await chrome.tabs.get(tabId); return { url: tab.url, title: tab.title }; } catch { return null; } }
}
// Instagram's profile UI can render after the passive profile data arrives. A wall only counts
// when two separate DOM reads on the exact target URL agree for this viewer.
async function checkedProfileDom(tabId, handle, profile) {
  let previous = await tabDom(tabId);
  for (let i = 0; i < 2; i++) {
    if (i && !previous?.privateWall) break;
    if (!previous?.privateWall && profile && !profile.is_private) break;
    await sleep(300);
    const current = await tabDom(tabId);
    if (FL.privateWall({ ...previous, privateWallRechecked: true }, handle, profile) &&
        FL.privateWall({ ...current, privateWallRechecked: true }, handle, profile))
      return { ...current, privateWallRechecked: true };
    previous = current;
  }
  return previous;
}
async function lookupViaPage(gen, handle, kind, near) {
  await assertControl(kind);
  if (gen !== mem.gen) throw new Superseded();
  if (FL.laneBusy(await get('lane'), Date.now())) return { p: null, bad: { code: 'busy' } };
  const key = handle.toLowerCase(), url = IG + '/' + encodeURIComponent(handle) + '/', t0 = Date.now();
  let tab;
  await set({ lane: { until: Date.now() + 60e3, url } });
  try {
    const got = new Promise((r) => { waiters[key] = r; setTimeout(() => r(null), 25e3); });
    const where = near ? { windowId: near.windowId, index: near.index + 1 } : {};
    tab = await chrome.tabs.create({ url, active: false, ...where });
    if (gen !== mem.gen) throw new Superseded();
    mem.lookups.add(tab.id);
    await set({ lookupTab: tab.id });
    await editSt((st) => { if (gen !== mem.gen) throw new Superseded(); FL.afterRequest(st, kind, Date.now()); });
    const p = await got;
    if (gen !== mem.gen) throw new Superseded();
    if (p) {
      const info = await checkedProfileDom(tab.id, handle, p);
      await trail(kind + ' page /' + handle + '/', { ms: Date.now() - t0, code: 'ok' });
      return { p, info, res: { status: 0, url: info && info.url, text: info && info.text, ms: Date.now() - t0 } };
    }
    const info = await checkedProfileDom(tab.id, handle, null), bad = FL.pageVerdict(info);
    const res = { status: 0, url: info && info.url, text: info ? 'title: ' + info.title + ' | ' + (info.text || '') : 'lookup tab closed', ms: Date.now() - t0 };
    await trail(kind + ' page /' + handle + '/', { ms: res.ms, code: bad.code, reason: bad.reason });
    return { p: null, info, bad, res };
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
  const beforeAccount = (await get('account'))?.ig_id || null;
  await remember(p);
  const me = await get('account'); // our own profile went by: now we know this account's handle
  if (me && me.ig_id === p.ig_id && me.handle !== p.handle.toLowerCase()) { await set({ account: { ...me, handle: p.handle.toLowerCase() } }); mem.lastBeat = 0; }
  const w = waiters[p.handle.toLowerCase()];
  if (w) return w(p); // a lookup asked for this one; its job posts it
  if (p.bio === null) return;
  // Passive data from browsing is a new bio import. Respect the current stage
  // control even though this path makes no extra Instagram request.
  await heartbeat(true);
  if (beforeAccount !== ((await get('account'))?.ig_id || null)) return;
  if (!FL.controlAllows(mem, 'profile') || await get('localPaused') || (await loadSt()).hold) return;
  if (!(await markSeen(p.handle))) return;
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
  if (msg && msg.type === 'fl-view') {
    chrome.storage.local.get(['view', 'account']).then((o) => reply({ view: o.view || null, account: o.account || null }), () => reply(null));
    return true;
  }
  // Control strip in the widget: the server's three stages (lists, bios, AI), read and switched through /api/ext/control.
  if (msg && msg.type === 'fl-control') {
    const body = msg.stage && (msg.action === 'pause' || msg.action === 'resume') ? { stage: msg.stage, action: msg.action } : undefined;
    api('/api/ext/control', body).then((r) => {
      const ctl = r.status === 200 && r.json && r.json.stages ? { ...r.json, got: Date.now() } : null;
      if (ctl) {
        set({ control: ctl });
        const stages = Object.fromEntries(ctl.stages.map((s) => [s.id === 'lists' ? 'list' : s.id === 'bios' ? 'profile' : s.id, !s.paused]));
        mem.stages = { ...(mem.stages || {}), ...stages };
      }
      if (body) { trail('control ' + body.stage + ' ' + body.action); mem.lastBeat = 0; loop(); }
      reply({ control: ctl });
    }, () => get('control').then((c) => reply({ control: c || null, offline: true }), () => reply(null)));
    return true;
  }
  if (msg && msg.type === 'fl-open') chrome.tabs.create({ url: SERVER + '/' }).catch(() => {});
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
  await startOffset(reason).catch(() => {});
}
chrome.runtime.onInstalled.addListener((d) => { trail('installed', { reason: d && d.reason }); ensureAlarm(); loop(); });
chrome.runtime.onStartup.addListener(() => { trail('browser startup'); ensureAlarm(); loop(); });
chrome.alarms.onAlarm.addListener(() => { ensureAlarm(); heartbeat(); loop(); });
const booted = wake('worker').catch(() => {});
booted.then(loop);

// The workspace can reload the extension after an update (loopback origin only).
chrome.runtime.onMessageExternal.addListener((msg, sender, respond) => {
  if (sender.origin !== SERVER || msg?.type !== 'RELOAD') return false;
  respond({ ok: true, version: VERSION });
  // clearCooldown: only for hits that weren't account limits (e.g. the retired web_profile_info lookup).
  const clear = msg.clearCooldown ? editSt((st) => { for (const k of FL.KINDS) st.cool[k] = { until: 0, hits: [] }; st.lastError = null; }) : Promise.resolve();
  clear.finally(() => setTimeout(() => chrome.runtime.reload(), 200));
  return false;
});
