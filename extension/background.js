// Fortunate Leads: paced executor. Server owns the queue; this worker owns pacing, budgets and cooldowns.
// MV3 lifecycle: every piece of state that must survive the worker being stopped lives in chrome.storage.local
// (st, box, cur, prog, lane, ids, seen, trail, debug). The in-memory loop is single-flight with a staleness expiry;
// the 30 s alarm restarts it after the worker was stopped or if it stalls. Instagram requests are serialised by the
// stored `lane` marker, so a restarted worker never fires while an earlier request may still be in flight.
importScripts('lib/core.js', 'lib/benchmark.js', 'lib/follower-capture.js', 'lib/workspace.js');
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
        const sending = box[0], sendingGen = mem.gen, r = await sendItem(sending);
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
          await editSt((st) => {
            // A delayed ACK belongs to the account and worker generation that sent it.
            // It cannot clear a later login/challenge hold or another account's redirect hold.
            const captured = Date.parse(sending.body?.captured_at || '');
            const newerRedirect = (st.listRedirects || []).some((entry) =>
              !Number.isFinite(captured) || entry.at > captured);
            if (sendingGen === mem.gen && !st.hold && sending.body?.account?.ig_id &&
                st.accountIgId === sending.body.account.ig_id && !newerRedirect)
              FL.listPageSucceeded(st, 'followers');
          });
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
  if (j?.upgrade_required) await editSt(st => { st.lastError = 'Reload this extension to version ' + (j.minimum_version || '3.9.17') + ' before collecting.'; });
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
  // Inspect existing tabs only: a retained lease is not proof that Instagram is usable.
  try {
    const choice = FL.chooseTab(await chrome.tabs.query({ url: IG + '/*' }));
    if (['tab_login', 'tab_challenge'].includes(choice.why)) mem.noTab = choice.why;
    else if (['tab_login', 'tab_challenge'].includes(mem.noTab)) mem.noTab = null;
  } catch {}
  const st = await loadSt(), s = await status(st), now = Date.now(), cd = FL.cooldownUntil(st, now);
  const cool = { list: st.cool.list.until > now ? iso(st.cool.list.until) : null, profile: st.cool.profile.until > now ? iso(st.cool.profile.until) : null };
  try {
    const r = await api('/api/ext/heartbeat', { version: VERSION, state: s.state, tab: mem.noTab || 'ok', cooldown_until: cd ? iso(cd) : null, cool,
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
async function pickTab(kind = 'list') {
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
    try { await navigateWithPermit(kind, async () => { await chrome.tabs.reload(c.reload); return { id: c.reload }; }); } catch (e) { if (e instanceof ControlPaused) throw e; await trail('reload failed', { err: String(e.message || e) }); }
    return { wait: 10e3 };
  }
  if (c.open) {
    mem.noTab = 'no_tab';
    if (now - (wt.openAt || 0) < 10 * FL.MIN) return { wait: 30e3 };
    await set({ workTab: { ...wt, openAt: now } });
    let tab = null;
    try { tab = await navigateWithPermit(kind, () => chrome.tabs.create({ url: IG + '/', active: false, pinned: true })); } catch (e) {
      if (e instanceof ControlPaused) throw e;
      try { tab = await navigateWithPermit(kind, async () => { const w = await chrome.windows.create({ url: IG + '/', focused: false, state: 'minimized' }); return w.tabs && w.tabs[0]; }); } catch (e) {
        if (e instanceof ControlPaused) throw e;
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
async function acquireSharedRequest(kind) {
  await assertControl(kind);
  let response;
  try { response = await api('/api/ext/request', { action: 'acquire', kind, job_id: mem.job?.id, lease_token: mem.job?.lease_token }); }
  catch { mem.offline = true; throw new ControlPaused(); }
  if (response.status !== 200 || response.json?.ok === false) { mem.offline = true; throw new ControlPaused(); }
  const grant = response.json;
  if (grant?.stale) { await set({ cur: null }); throw new ControlPaused(); }
  if (grant?.lease_renewed) {
    const cur = await get('cur');
    if (cur?.job?.id === mem.job?.id) await set({ cur: { ...cur, at: Date.now() } });
  }
  if (!grant?.granted || !grant.token || !Number.isFinite(Date.parse(grant.expires_at))) {
    mem.sharedWaitUntil = Date.now() + Math.max(1000, Math.min(15000, Number(grant?.wait_ms) || 15000));
    throw new ControlPaused();
  }
  if (await get('localPaused') || !FL.controlAllows(mem, kind)) {
    await releaseSharedRequest(grant.token);
    throw new ControlPaused();
  }
  // A delayed response must leave enough time for the longest normal action.
  if (Date.parse(grant.expires_at) - Date.now() < 45000) {
    await releaseSharedRequest(grant.token); // no browser action has started
    mem.sharedWaitUntil = Date.now() + 15000;
    throw new ControlPaused();
  }
  return grant.token;
}
async function releaseSharedRequest(token) {
  try { await api('/api/ext/request', { action: 'release', token }); }
  catch { mem.offline = true; } // the server lease expires; never assume an uncertain release succeeded
}
async function navigateWithPermit(kind, action) {
  const gen = mem.gen;
  const token = await acquireSharedRequest(kind);
  if (gen !== mem.gen) { await releaseSharedRequest(token); throw new ControlPaused(); }
  let completed = false;
  try {
    let tab;
    try { tab = await action(); } catch (error) { completed = true; throw error; }
    const until = Date.now() + 45000;
    while (Date.now() < until) {
      const current = await chrome.tabs.get(tab.id);
      if (current.status === 'complete') { completed = true; return tab; }
      await sleep(500);
    }
    return tab;
  } finally {
    // A navigation still loading keeps its lease until expiry.
    if (completed) await releaseSharedRequest(token);
  }
}
async function igRequest(gen, tab, url, kind, ctx) {
  await assertControl(kind);
  if (gen !== mem.gen) throw new Superseded();
  if (FL.laneBusy(await get('lane'), Date.now())) return { res: { status: 0, text: 'lane busy' }, bad: { code: 'busy' } };
  const permit = await acquireSharedRequest(kind);
  if (gen !== mem.gen) { await releaseSharedRequest(permit); throw new Superseded(); }
  await set({ lane: { until: Date.now() + 90e3, url } });
  let res, keepLane = false;
  try {
    res = await inTab(tab.id, url);
    // A timed-out executeScript may still have its fetch running in the page: hold the lane until that fetch aborted too.
    keepLane = !!res.timedOut;
  } finally {
    await set({ lane: keepLane ? { until: Date.now() + 60e3, url, after: 'timeout' } : null });
    if (!keepLane) await releaseSharedRequest(permit);
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
// Ordinary transient failures may retry. Trial failures are reported and parked.
async function fail(job, bad, what, res, bucket, publicTarget = false, gen = mem.gen, route = null) {
  if (gen !== mem.gen) throw new Superseded();
  const now = Date.now(), sample = res ? FL.sampleOf(res, 1500) : '';
  const local = bad.code === 'busy' || (!job.experiment_viewer_ig_id &&
    (bad.code === 'network' || bad.code === 'unsupported'));
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
    route: route || undefined,
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
  const requestedCount = FL.listPageSize(job, (await get('account'))?.ig_id);
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
  const url = IG + '/api/v1/friendships/' + igId + '/' + job.direction + '/?count=' + requestedCount +
    (cursor ? '&max_id=' + encodeURIComponent(cursor) : '') + (job.direction === 'followers' ? '&search_surface=follow_list_page' : '');
  // Another lane may have moved this list on since we last saw it: the server's count is then the one to trust.
  const ctx = FL.listContext(job, prog, total, totalSource);
  const { res, bad } = await igRequest(gen, tab, url, 'list', ctx);
  if (bad) {
    // Report the warning before any further Instagram request, including privacy checks.
    if (bad.reason === 'list_html_home_redirect')
      return fail(job, bad, mem.label, res, 'list', false, gen);
    if (bad.reason === 'empty_page_before_total' && !job.experiment_viewer_ig_id) {
      // Keep this lease and cursor. The request already advanced the normal list
      // clock; a second empty terminal page is sent to the server as partial.
      await editProg(key, () => ({ ...prog, jobId: job.id, next: cursor, emptyAt: cursor }), gen);
      return;
    }
    if (bad.code === 'private' && !job.experiment_viewer_ig_id) {
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
    has_more: page.has_more, requested_count: requestedCount, http_status: res.status, captured_at: iso(now) }, true, gen);
}

// ---- profile reads (bios) ----------------------------------------------------
// Read the normal profile page and capture the data Instagram loads for it.
// Choose this route before any request: a warning never triggers a second route.
async function runProfile(gen, job, tab) {
  mem.label = '@' + job.handle + ' profile';
  const route = 'profile_page';
  if ((await loadSt()).hold) throw new ControlPaused();
  const r = await lookupViaPage(gen, job.handle, 'profile', tab);
  const capturedAt = iso(Date.now()), p = r.p;
  const warning = r.bad || (r.info ? FL.pageVerdict(r.info) : null);
  if (warning && ['rate_limit', 'soft_block', 'login', 'challenge', 'not_found'].includes(warning.code))
    return fail(job, warning, mem.label + ' (page)', r.res, 'profile', false, gen, route);
  if (!p) return fail(job, r.bad || { code: 'other', reason: 'no_profile_data' }, mem.label + ' (page)', r.res, 'profile', false, gen, route);
  const expectedId = job.target_ig_id || job.ig_id;
  if (expectedId && String(p.ig_id || '') !== String(expectedId))
    return fail(job, { code: 'other', reason: 'profile_identity_mismatch' }, mem.label + ' (page)', r.res, 'profile', false, gen, route);
  if (typeof p.bio !== 'string')
    return fail(job, { code: 'other', reason: 'no_bio' }, mem.label + ' (page)', r.res, 'profile', false, gen, route);
  await remember(p);
  await markSeen(p.handle);
  await editSt((st) => { if (gen !== mem.gen) throw new Superseded(); FL.tally(st, Date.now(), 0, 1); });
  await queueDone('/api/ext/profile', { job_id: job.id, lease_token: job.lease_token, profile: p, captured_at: capturedAt, route,
    event_id: Date.now().toString(36) + Math.random().toString(36).slice(2) }, true, gen);
}

// Benchmark has its own durable outbox: even a rejection cannot discard an unacknowledged attempt.
async function flushBenchmark() {
  let pending = await get('benchmarkPending');
  if (!pending) return true;
  // A permit may have committed even if its reply never reached this worker.
  // Without its token there is no safe result upload or permit retry.
  if (pending.phase === 'permit_requested' && !pending.body?.token) return false;
  if (!pending.result) {
    pending = { ...pending, result: { ...pending.body, rows: [], next_cursor: null, has_more: null,
      status: 'error', terminal_warning: 'transport_ambiguous', actual_http_requests: null,
      http_status: 0, duration_ms: null, uncertain: true } };
    await set({ benchmarkPending: pending });
  }
  try {
    const r = await api('/api/benchmark/result', pending.result);
    if (r.status !== 200 || r.json?.ok !== true || r.json?.acknowledged !== true || r.json?.request_id !== pending.body.request_id) return false;
    await set({ benchmarkPending: null, benchmarkTask: null });
    return true;
  } catch { return false; }
}
function benchmarkWait(st, now, direction = 'following') {
  const options = [[FL.readyAt(st, 'list'), 'local_pacing'], [FL.windowOf(st, now).until, 'local_window'],
    [st.cool.list.until, 'local_cooldown'], [direction === 'followers' ? st.listEndpointUntil : 0, 'local_endpoint_hold']];
  const [until, reason] = options.sort((a, b) => (b[0] || 0) - (a[0] || 0))[0];
  return { wait_ms: Math.max(0, (until || 0) - now), reason };
}
// Cookie values stay in memory; only a SHA256 fingerprint leaves this function.
async function benchmarkFingerprint(viewerId) {
  try {
    const [session, viewer] = await Promise.all([
      chrome.cookies.get({ url: IG + '/', name: 'sessionid' }),
      chrome.cookies.get({ url: IG + '/', name: 'ds_user_id' })
    ]);
    if (!session?.value || !viewer?.value || viewer.value !== String(viewerId)) return null;
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify([session.value, viewer.value])));
    return Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('');
  } catch { return null; }
}
async function benchmarkStep(gen) {
  if (await get('benchmarkPending')) return 15000;
  await heartbeat(true);
  if (gen !== mem.gen || mem.offline) return 15e3;
  const st = await loadSt(), now = Date.now(), local = benchmarkWait(st, now);
  const q = new URLSearchParams({ transport: 'chrome', version: VERSION, client_wait_ms: String(local.wait_ms), client_wait_reason: local.reason });
  let next;
  try {
    const r = await api('/api/benchmark/next?' + q);
    if (r.status !== 200 || typeof r.json?.enabled !== 'boolean') return 15e3;
    next = r.json;
  } catch { return 15e3; }
  if (!next.enabled && !next.reserved) { await set({ benchmarkTask: null }); return null; }
  if (!next.enabled || next.stopped || !next.task || local.wait_ms || st.hold || await get('localPaused'))
    return Math.max(1000, Math.min(30000, local.wait_ms || next.wait_ms || 15000));
  const task = next.task, id = await ident();
  let url, recipe = null, preflightError = null;
  if (['web_modal','web_search'].includes(task.route)) {
    if (task.transport !== 'chrome' || task.direction !== 'followers' || !task.task_id ||
        !/^\d+$/.test(String(task.target_id || '')) || String(task.viewer_id) !== String(id.account?.ig_id)) return 15000;
    try { ({url,recipe} = await FLFollowerCapture.prepare(task,id.account.ig_id,await get('followerRequestTemplates'))); }
    catch (error) { preflightError = String(error.message || 'missing_verified_followers_capture'); }
  } else {
    try { url = FLBenchmark.url(task, id.account?.ig_id); } catch { return 15000; }
  }
  // Never create, reload or navigate a tab for a benchmark.
  const tabs = (await chrome.tabs.query({ url: IG + '/*' })).filter(t => t.status === 'complete' && !t.discarded && !t.incognito &&
    !mem.lookups.has(t.id) && FL.pageKind(t.url) === 'ok');
  if (!tabs.length) return 15000;
  let saved = await get('benchmarkTask');
  if (!saved || saved.task_id !== task.task_id) {
    saved = { task_id: task.task_id, request_id: crypto.randomUUID() };
    await set({ benchmarkTask: saved });
  }
  const fingerprint = await benchmarkFingerprint(task.viewer_id);
  if (!fingerprint) {
    await editSt(s => { s.lastError = 'Benchmark needs matching Instagram session cookies and extension cookie permission.'; });
    return 15000;
  }
  const body = { lane_id: id.lane_id, account: id.account, transport: 'chrome', task_id: task.task_id, request_id: saved.request_id, fingerprints: { session: fingerprint } };
  if (recipe) body.capture_shape_hash = recipe.shape_hash;
  const pending = { phase: 'permit_requested', body };
  await set({ benchmarkPending: pending });
  let grant;
  try { grant = await api('/api/benchmark/permit', body); }
  catch {
    await set({ benchmarkPending: { ...pending, permit_error: 'permit_response_lost' } });
    return 15000;
  }
  if (grant.status !== 200 || grant.json?.granted !== true || !grant.json.token || grant.json.request_id !== saved.request_id) {
    // These server denials run only after checking for an outstanding benchmark.
    // Unknown denials, inflight, and an already recorded request remain parked.
    const noOutstandingReasons = ['task_changed', 'warmup_complete', 'session_or_device_changed',
      'attention', 'paused', 'invalid_hold', 'cooldown', 'local_role', 'local_handoff',
      'local_identity_owner', 'provider_cooldown', 'local_budget', 'budget_or_account',
      'local_pacing', 'invalid_pacing', 'local_profile_drain', 'local_background_unverified',
      'local_background_drain', 'permit'];
    const denied = grant.status === 200 && grant.json?.granted === false && !grant.json.token &&
      grant.json.outstanding !== true &&
      (grant.json.outstanding === false || noOutstandingReasons.includes(grant.json.reason));
    await set({ benchmarkPending: denied ? null : { ...pending, permit_error: 'permit_response_unconfirmed', permit_http_status: grant.status } });
    return Math.max(1000, Math.min(30000, grant.json?.wait_ms || 15000));
  }
  body.token = grant.json.token;
  // Persist before MAIN dispatch. If MV3 dies at any point, report uncertainty instead of repeating GET.
  await set({ benchmarkPending: { phase: 'permit_granted', body } });
  let res;
  const fingerprintAfter = await benchmarkFingerprint(task.viewer_id);
  const fresh = await loadSt();
  const permitValid = Number.isFinite(Date.parse(grant.json.expires_at)) && Date.parse(grant.json.expires_at) - Date.now() >= 45000;
  if (preflightError) {
    res = { actual_http_requests: 0, duration_ms: 0, status: 0, error: preflightError };
  } else if (fingerprintAfter !== fingerprint) {
    res = { actual_http_requests: 0, duration_ms: 0, status: 0, error: 'session_fingerprint_changed' };
  } else if (!permitValid) {
    res = { actual_http_requests: 0, duration_ms: 0, status: 0, error: 'permit_expired' };
  } else if (gen !== mem.gen || fresh.hold || await get('localPaused') || benchmarkWait(fresh, Date.now(), task.direction).wait_ms || FL.laneBusy(await get('lane'), Date.now())) {
    res = { actual_http_requests: 0, duration_ms: 0, status: 0, error: 'local_control_blocked' };
  } else {
    await set({ lane: { until: Date.now() + 90000, url, benchmark: true, request_id: body.request_id } });
    try {
      const [r] = await withTimeout(45000, chrome.scripting.executeScript({ target: { tabId: tabs[0].id }, world: 'MAIN',
        func: FLBenchmark.fetchOnce, args: [url, String(task.viewer_id), 30000, recipe] }));
      res = r?.result || { status: 0, error: 'transport_ambiguous', uncertain: true };
    } catch { res = { status: 0, error: 'transport_ambiguous', uncertain: true }; }
  }
  const result = { ...body, ...FLBenchmark.result(task, res, FL) };
  await locked(async () => {
    const values = { benchmarkPending: { body, result } }, s = await loadSt(), lane = await get('lane');
    if (!res.uncertain && lane?.benchmark && lane.request_id === body.request_id) values.lane = null;
    if (res.actual_http_requests === 1 && gen === mem.gen &&
        String(s.accountIgId || body.account?.ig_id) === String(body.account?.ig_id)) {
      FL.rollDay(s, Date.now());
      s.today.list = (s.today.list || 0) + 1;
      s.rlog = (s.rlog || []).filter(e => Date.now() - e[0] < FL.HOUR).concat([[Date.now(), 'list']]);
      values.st = s;
    }
    // Outcome and counters survive together; outbox replay never increments them again.
    await set(values);
  });
  await flushBenchmark();
  return 1000;
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
  if (!(await flushBenchmark())) return 15000;
  if (!(await flushBox())) return (mem.backoff = Math.min(mem.backoff * 2, 60e3));
  if (st.hold || (await get('localPaused'))) return 15e3;
  const lane = await get('lane');
  mem.laneWait = FL.laneBusy(lane, now);
  if (mem.laneWait) return lane.until - now;
  const benchmarkWaitMs = await benchmarkStep(gen);
  if (benchmarkWaitMs !== null) return benchmarkWaitMs;
  const pl = FL.plan(st, FL.budgetLeft(st, await get('budget'), now), now);
  if (!pl.kinds.length) { mem.budgetDone = pl.why === 'budget'; return pl.wait; }
  const { job, wait } = await nextJob(pl.kinds);
  mem.job = job || null;
  if (!job) { mem.noTab = null; return wait; }
  const t = await pickTab(job.kind); // after leasing: never opens or wakes a tab while the queue is empty
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
        if (e instanceof ControlPaused) { await sleep(Math.max(1000, Math.min(15000, (mem.sharedWaitUntil || 0) - Date.now()) || 15000)); continue; }
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
  const permit = await acquireSharedRequest(kind);
  if (gen !== mem.gen) { await releaseSharedRequest(permit); throw new Superseded(); }
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
    let closed = !tab;
    if (tab) {
      mem.lookups.delete(tab.id);
      try { await chrome.tabs.remove(tab.id); closed = true; await set({ lookupTab: null }); }
      catch { /* Keep the permit: an unconfirmed close may leave the page running. */ }
    }
    await set({ lane: closed ? null : { until: Date.now() + 90e3, url, after: 'close_failed' } });
    if (closed) await releaseSharedRequest(permit);
  }
}

// ---- passive capture from normal browsing (zero extra requests) -----------
async function passive(user) {
  const p = FL.mapProfile(user);
  if (!p || !p.ig_id) return;
  const capturedAt = iso(Date.now());
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
  await queue('/api/ext/profile', { job_id: null, profile: p, captured_at: capturedAt, route: 'passive',
    event_id: Date.now().toString(36) + Math.random().toString(36).slice(2) });
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
  if (sender.origin !== SERVER) return false;
  if (msg?.type === 'OPEN_INSTAGRAM') {
    openWorkspaceInstagram(chrome, msg).then(respond, error => respond({ok: false, error: error.message}));
    return true;
  }
  if (msg?.type !== 'RELOAD') return false;
  respond({ ok: true, version: VERSION });
  // clearCooldown: only for hits that weren't account limits (e.g. the retired web_profile_info lookup).
  const clear = msg.clearCooldown ? editSt((st) => { for (const k of FL.KINDS) st.cool[k] = { until: 0, hits: [] }; st.lastError = null; }) : Promise.resolve();
  clear.finally(() => setTimeout(() => chrome.runtime.reload(), 200));
  return false;
});
