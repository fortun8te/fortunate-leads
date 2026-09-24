#!/usr/bin/env node
// End-to-end simulator: the real extension (background.js + lib/core.js + bridge.js + relay.js, loaded unmodified into
// emulated Chrome contexts) against the real server (server/server.py via sim_server.py's clock shim) and a fake
// Instagram (fake_ig.py), on a virtual clock so 8 hours of pacing and cooldowns run in well under 2 minutes.
//
//   node tests/e2e/driver.mjs [--hours 8] [--keep] [--verbose]
//
// Virtual time: every timer in the service worker, tabs and operator goes into one event queue. Time only moves when
// nothing is in flight (no pending real HTTP), then jumps to the next timer. Real HTTP (to the server and the fake)
// happens at a frozen simulated instant and carries X-Sim-Now so both Python processes share the simulated clock.
process.env.TZ = process.env.SIM_TZ || 'Europe/Amsterdam';

import { spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '../..');
const EXT = process.env.FL_EXT_DIR ? path.resolve(process.env.FL_EXT_DIR) : path.join(ROOT, 'extension'); // override to test a patched copy
const argv = process.argv.slice(2);
const flag = (n) => argv.includes('--' + n);
const opt = (n, d) => { const i = argv.indexOf('--' + n); return i >= 0 ? argv[i + 1] : d; };
const LANES = opt('lanes', null);
const HOURS = Number(opt('hours', LANES ? 12 : 8));
const VERBOSE = flag('verbose');
const EXT_ID = 'fgdbghllamedgihmdcolaggnbhnakjnf';
const EXT_ORIGIN = 'chrome-extension://' + EXT_ID;
const SERVER_CONST = 'http://127.0.0.1:8777';
const IG = 'https://www.instagram.com';
const FL = createRequire(import.meta.url)(path.join(EXT, 'lib', 'core.js')); // reference copy for replaying applyHit
const MIN = 60e3, HOUR = 60 * MIN, DAY = 24 * HOUR;
const REAL_T0 = Date.now();
const RealDate = Date;

// ---------------------------------------------------------------- scenario
const START = new RealDate(2026, 8, 24, 19, 0, 0).getTime(); // Thu 19:00 Amsterdam; local midnight falls inside the run
const END = START + HOURS * HOUR;
const SEEDS = [ // [handle, direction, list size, extra]
  ['glowbrand.co', 'followers', 3000], ['kettleandco', 'followers', 2200], ['dtc.daily', 'followers', 1500],
  ['ritual.goods', 'followers', 900], ['saltwater.skin', 'followers', 600], ['fermentlabs', 'followers', 300],
  ['tinyroaster', 'followers', 120], ['mossandmane', 'followers', 50],
  ['megaverified', 'followers', 2000, { capped: true, verified: true, claimed: 256000 }],
  ['private.founder', 'followers', 400, { private: true }],
  ['operator.nl', 'following', 2800], ['growthfounder', 'following', 1800], ['packdesign.studio', 'following', 1000],
  ['amsterdam.dtc', 'following', 450], ['brandbuilder.us', 'following', 200],
];
// Instagram failures, by request number per kind (fake_ig.py counts them).
const SCHEDULE = {
  list: { 5: 'for_prefix', 12: 'slow', 25: '429', 40: 'hang', 55: 'login_redirect', 70: 'for_prefix', 90: 'please_wait',
    110: 'freeze', 130: '500', 150: 'checkpoint', 175: 'login_html', 200: 'slow', 240: 'soft_block', 300: 'for_prefix', 350: 'hang' },
  profile: { 3: 'for_prefix', 12: 'slow', 20: 'hang', 25: '429_retry', 40: 'useragent' },
  page: { 6: 'hang' },
};
// Harness-side faults, by list-page POST attempt / list request number.
const FAULT = { dropResponse: new Set([30, 120, 260]), restartAfterCommit: 100, outageAt: 200, outageMin: Number(opt('outage-min', 4)),
  restartMidRequest: 65, idleKillAt: START + 3.5 * HOUR, dropProfileResponse: new Set([10]) };
const HOLD_POLICY = ['workspace', 'popup', 'workspace']; // how the operator clears the 1st, 2nd, 3rd hold

// ---------------------------------------------------------------- virtual clock and event queue
const V = { now: START, seq: 0, inflight: 0, waiters: [] };
const heap = [], cancelled = new Set();
const less = (a, b) => a.at < b.at || (a.at === b.at && a.id < b.id);
function hpush(t) {
  heap.push(t);
  for (let i = heap.length - 1; i > 0;) { const p = (i - 1) >> 1; if (!less(heap[i], heap[p])) break; [heap[i], heap[p]] = [heap[p], heap[i]]; i = p; }
}
function hpop() {
  const top = heap[0], last = heap.pop();
  if (heap.length) {
    heap[0] = last;
    for (let i = 0; ;) {
      const l = 2 * i + 1, r = l + 1; let m = i;
      if (l < heap.length && less(heap[l], heap[m])) m = l;
      if (r < heap.length && less(heap[r], heap[m])) m = r;
      if (m === i) break;
      [heap[i], heap[m]] = [heap[m], heap[i]]; i = m;
    }
  }
  return top;
}
function timer(owner, ms, fn) { const t = { at: V.now + Math.max(0, Number(ms) || 0), id: ++V.seq, fn, owner }; hpush(t); return t.id; }
const clearTimer = (id) => { if (id) cancelled.add(id); };
const NEVER = () => new Promise(() => {});
// Real I/O: virtual time is frozen while any is in flight. Results for a dead owner (a stopped worker) never arrive.
function io(owner, fn) {
  V.inflight++;
  const p = (async () => fn())();
  const done = () => { if (--V.inflight === 0) V.waiters.splice(0).forEach((r) => r()); };
  p.then(done, done);
  return p.then((v) => (owner.alive ? v : NEVER()), (e) => (owner.alive ? Promise.reject(e) : NEVER()));
}
const tick = () => new Promise((r) => setImmediate(r));
async function settle() {
  for (;;) {
    await tick();
    if (V.inflight) { await new Promise((r) => V.waiters.push(r)); continue; }
    await tick();
    if (!V.inflight) return;
  }
}
const harnessErrors = [];
async function runUntil(end) {
  await settle();
  while (heap.length && heap[0].at <= end && !V.stop) {
    const t = hpop();
    if (cancelled.delete(t.id) || !t.owner.alive) continue;
    if (t.at > V.now) V.now = t.at;
    try { t.fn(); } catch (e) { harnessErrors.push(`${t.owner.name}: ${e && e.stack || e}`); }
    await settle();
  }
  if (!V.stop) V.now = Math.max(V.now, end);
}
class SimDate extends RealDate {
  constructor(...a) { if (a.length) super(...a); else super(V.now); }
  static now() { return V.now; }
}
const hm = (t) => new RealDate(t).toTimeString().slice(0, 5);
const dhm = (t) => { const d = new RealDate(t); return (d.getDate() !== new RealDate(START).getDate() ? '+' + Math.round((t - START) / DAY) + 'd ' : '') + hm(t); };

// ---------------------------------------------------------------- processes
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'fl-e2e-'));
const DB = path.join(TMP, 'leads.sqlite');
const freePort = () => new Promise((res) => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); }); });
const PORT = { server: 0, ig: 0 };
const procs = {};
async function waitHttp(url, ms = 15000) {
  const t0 = RealDate.now();
  for (;;) {
    try { const r = await fetch(url, { signal: AbortSignal.timeout(1000) }); if (r.status < 500) return; } catch {}
    if (RealDate.now() - t0 > ms) throw new Error('not up: ' + url);
    await new Promise((r) => setTimeout(r, 50));
  }
}
function launch(name, args) {
  const log = fs.openSync(path.join(TMP, name + '.log'), 'a');
  const p = spawn('python3', args, { cwd: ROOT, stdio: ['ignore', log, log] });
  procs[name] = p;
  return p;
}
async function startServer() {
  launch('server', [path.join(HERE, 'sim_server.py'), '--db', DB, '--port', String(PORT.server)]);
  await waitHttp(`http://127.0.0.1:${PORT.server}/api/counts`);
  serverUp = true;
}
async function stopServer() {
  serverUp = false;
  const p = procs.server;
  if (!p || p.exitCode !== null) return;
  await new Promise((r) => { p.once('exit', r); p.kill('SIGTERM'); });
}
let serverUp = false;
function cleanup() { for (const p of Object.values(procs)) try { p.kill('SIGKILL'); } catch {} }
process.on('exit', cleanup);

// ---------------------------------------------------------------- observation state
const OPS = { name: 'ops', alive: true }, BROWSER = { name: 'browser', alive: true };
const igLog = [];          // every request that reached (fake) Instagram: API calls, profile page loads, tab reloads
const serverLog = [];      // every extension → server call
const events = [];         // timeline
const violations = [];
const hits = [];           // observed rate_limit/soft_block hits
const holds = [];
const boxSeen = new Map(); // outbox item key → {first, acked}
const restarts = [];
const counters = { listPagePosts: 0, listReq: 0, profilePosts: 0, swBoots: 0, unreachedMsgs: 0, ticks: 0, dupResponses: 0 };
const expectations = [];   // cursor-resume checks set up by restarts
let lastIg = null;         // last Instagram API response seen by a tab
const viol = (what, extra) => { violations.push({ t: V.now, what, ...(extra || {}) }); if (VERBOSE) console.log('VIOLATION', dhm(V.now), what, extra || ''); };
const ev = (what, extra) => { events.push({ t: V.now, what, ...(extra || {}) }); if (VERBOSE) console.log(dhm(V.now), what, extra ? JSON.stringify(extra) : ''); };
const seedByPk = new Map(), seedByName = new Map();

const clone = (v) => (v === undefined ? undefined : structuredClone(v));
const boxKey = (it) => it.path + ' ' + JSON.stringify(it.body);
const coolOf = (st) => (st && st.cool ? st.cool : { list: { until: (st && st.cooldownUntil) || 0, hits: (st && st.hits) || [] }, profile: { until: 0, hits: [] } });
const read = (f) => fs.readFileSync(f, 'utf8');
const quiet = { log() {}, info() {}, debug() {}, warn(...a) { if (VERBOSE) console.warn('[ext]', ...a); }, error(...a) { if (VERBOSE) console.error('[ext]', ...a); } };
const decode = (s) => s.replace(/&bull;/g, '•').replace(/&amp;/g, '&').replace(/&#x27;|&#39;/g, "'").replace(/&quot;/g, '"');
const fakeUrl = (u) => `http://127.0.0.1:${PORT.ig}${u.pathname}${u.search}`;
const igUrl = (real) => { const u = new URL(real); return IG + u.pathname + u.search; };
const abortErr = () => new DOMException('This operation was aborted', 'AbortError');
const tabInfo = (t) => ({ id: t.id, url: t.url, title: t.title, status: t.status, active: t.active, pinned: t.pinned, discarded: false,
  frozen: false, windowId: 1, index: t.index, autoDiscardable: t.autoDiscardable, highlighted: t.active, incognito: false });

// Everything one Chrome profile has: its own storage, tabs, alarms and service worker (one lane).
function makeProfile(P) {
const store = P.store, browser = P.browser, hits = P.hits, holds = P.holds;
let lastSt = null, lastIg = null;
const acctHdr = () => (P.igId ? { 'X-Sim-Account': P.igId } : {});
// ---------------------------------------------------------------- chrome.storage.local (survives worker restarts)
function storageGet(keys) {
  const ks = keys == null ? Object.keys(store) : typeof keys === 'string' ? [keys] : Array.isArray(keys) ? keys : Object.keys(keys);
  const out = {};
  for (const k of ks) if (k in store) out[k] = clone(store[k]); else if (keys && typeof keys === 'object' && !Array.isArray(keys)) out[k] = keys[k];
  return out;
}
function storageSet(o) {
  for (const [k, v] of Object.entries(o)) {
    store[k] = clone(v);
    if (k === 'st') observeSt(store[k]);
    if (k === 'box') for (const it of v || []) { const key = boxKey(it); if (!boxSeen.has(key)) boxSeen.set(key, { first: V.now, acked: false, path: it.path }); }
  }
}
function observeSt(st) {
  const pre = lastSt;
  lastSt = st; P.lastSt = st;
  const a = coolOf(pre), b = coolOf(st);
  for (const k of ['list', 'profile']) {
    for (const t of b[k].hits || []) {
      if ((a[k].hits || []).includes(t)) continue;
      const retryAt = lastIg && lastIg.retryAfter ? t + Number(lastIg.retryAfter) * 1000 : null;
      hits.push({ at: t, bucket: k, pre: clone(pre), post: clone(st), retryAt, inject: lastIg && lastIg.inject, sw: browser.sw && browser.sw.name });
      ev('hit ' + k, { inject: lastIg && lastIg.inject, until: dhm(b[k].until) });
    }
  }
  if (!(pre && pre.hold) && st.hold) { holds.push({ start: V.now, code: st.hold.code }); ev('hold', { code: st.hold.code }); }
  if (pre && pre.hold && !st.hold && holds.length && holds.at(-1).end == null) { holds.at(-1).end = V.now; ev('hold cleared', { after_min: Math.round((V.now - holds.at(-1).start) / MIN) }); }
}

// ---------------------------------------------------------------- browser: tabs, pages, content scripts

// The tab's own fetch (page JS / executeScript MAIN world). Routes instagram.com to the fake, simulated latency/hangs.
async function pageFetch(tab, input, init = {}) {
  const u = new URL(String(input), tab.url);
  if (u.origin !== IG) throw new TypeError('Failed to fetch');
  const signal = init.signal;
  if (signal && signal.aborted) throw abortErr();
  const m = u.pathname.match(/^\/api\/v1\/friendships\/(\d+)\/(followers|following)\/$/);
  const info = /^\/api\/v1\/users\/[^/]+\/info\/$/.test(u.pathname);
  const kind = m ? 'list' : info ? 'profile' : 'api';
  const entry = { t: V.now, lane: P.name, kind, bucket: kind === 'api' ? 'list' : kind, path: u.pathname, maxId: u.searchParams.get('max_id'),
    list: m ? (seedByPk.get(m[1]) || { handle: '?' + m[1] }).handle + '/' + m[2] : null, sw: browser.sw && browser.sw.name };
  if (kind === 'list') entry.n = ++counters.listReq;
  checkRequest(entry);
  igLog.push(entry);
  const r = await io(tab, async () => {
    const res = await fetch(fakeUrl(u), { headers: { ...(init.headers || {}), 'X-Sim-Now': String(V.now), ...acctHdr() }, redirect: 'follow',
      signal: AbortSignal.timeout(20000) });
    return { status: res.status, url: igUrl(res.url), redirected: res.redirected, headers: res.headers, text: await res.text() };
  });
  entry.status = r.status; entry.inject = r.headers.get('x-sim-inject') || null; entry.retryAfter = r.headers.get('retry-after');
  lastIg = entry; P.lastIg = entry;
  if (P.faults && kind === 'list' && entry.n === FAULT.restartMidRequest) {
    // The worker is stopped while its request is in flight: the tab finishes the fetch, nobody gets the answer.
    expectations.push({ type: 'resume-after-mid-request-restart', list: entry.list, want: entry.maxId, after: V.now });
    restart('stopped mid-request (list request #' + entry.n + ')', 3000);
  }
  const hang = r.headers.get('x-sim-hang');
  if (hang === 'freeze') { entry.outcome = 'tab froze'; return NEVER(); }
  await new Promise((res, rej) => {
    const onAbort = () => { entry.outcome = 'aborted'; rej(abortErr()); };
    if (signal) signal.addEventListener('abort', onAbort, { once: true });
    if (hang !== 'abort') timer(tab, Number(r.headers.get('x-sim-latency')) || 0, () => { if (signal) signal.removeEventListener('abort', onAbort); res(); });
  });
  entry.done = V.now;
  const text = r.text;
  const resp = { status: r.status, ok: r.status >= 200 && r.status < 300, url: r.url, redirected: r.redirected, type: 'basic',
    headers: { get: (k) => r.headers.get(k) }, text: async () => text, json: async () => JSON.parse(text) };
  resp.clone = () => resp;
  return resp;
}

function makePage(tab, html) {
  const u = new URL(tab.url), L = { win: {}, doc: {} };
  const scripts = [...html.matchAll(/<script type="application\/json"[^>]*>([\s\S]*?)<\/script>/g)].map((m) => ({ textContent: m[1] }));
  const title = decode((html.match(/<title>([\s\S]*?)<\/title>/) || [])[1] || '');
  const text = decode(html.replace(/<script[\s\S]*?<\/script>/g, '').replace(/<title>[\s\S]*?<\/title>/, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim());
  const on = (bag) => (t, fn) => (bag[t] = bag[t] || []).push(fn);
  class XHR { open() {} send() {} addEventListener() {} }
  const doc = { cookie: 'csrftoken=SimCsrf0; ds_user_id=' + (P.igId || '4242424242') + '; sessionid=sim', visibilityState: 'hidden', title,
    body: { innerText: text }, readyState: 'complete', addEventListener: on(L.doc), querySelectorAll: (sel) => (/application\/json/.test(sel) ? scripts : []) };
  const g = { console: quiet, Date: SimDate, setTimeout: (fn, ms, ...a) => timer(tab, ms, () => fn(...a)), clearTimeout: clearTimer,
    AbortController, URL, DOMException, document: doc, location: { href: u.href, hostname: u.hostname, pathname: u.pathname, origin: u.origin },
    sessionStorage: tab.session, XMLHttpRequest: XHR, addEventListener: on(L.win), fetch: (i, o) => pageFetch(tab, i, o) };
  g.window = g; g.self = g;
  g.postMessage = (data) => timer(tab, 0, () => (tab.bus || []).forEach((fn) => fn({ source: tab.iso, data: clone(data), origin: u.origin })));
  vm.createContext(g);
  // Isolated world (relay.js): forwards bridge.js messages to the service worker.
  tab.bus = [];
  const iso = { console: quiet, addEventListener: (t, fn) => t === 'message' && tab.bus.push(fn),
    chrome: { runtime: { id: EXT_ID, sendMessage: (msg) => { toWorker(clone(msg), { tab: tabInfo(tab), id: EXT_ID, origin: u.origin }); return Promise.resolve(); } } } };
  iso.window = iso; iso.self = iso;
  vm.createContext(iso);
  tab.iso = vm.runInContext('globalThis', iso); // what `window` is inside the isolated world (e.source === window)
  vm.runInContext(read(path.join(EXT, 'bridge.js')), g, { filename: 'bridge.js' });
  vm.runInContext(read(path.join(EXT, 'relay.js')), iso, { filename: 'relay.js' });
  timer(tab, 0, () => { (L.doc.DOMContentLoaded || []).forEach((f) => f()); (L.win.load || []).forEach((f) => f()); });
  return g;
}
// A navigation: the tab loads `url` from the fake (a real page load, logged as an Instagram request).
function navigate(tab, url, why) {
  tab.url = url; tab.status = 'loading'; tab.title = ''; tab.page = makePage(tab, '');
  const u = new URL(url), handle = decodeURIComponent((u.pathname.match(/^\/([^/]+)\/$/) || [])[1] || '');
  const entry = { t: V.now, kind: why === 'lookup' ? 'page' : 'nav', bucket: seedByName.has(handle.toLowerCase()) ? 'list' : 'profile',
    path: u.pathname, handle, sw: browser.sw && browser.sw.name };
  if (why === 'lookup') checkRequest(entry);
  igLog.push(entry);
  const gen = ++tab.gen;
  timer(tab, 400, () => io(tab, async () => {
    const res = await fetch(fakeUrl(u), { headers: { 'X-Sim-Now': String(V.now), ...acctHdr() }, redirect: 'follow', signal: AbortSignal.timeout(20000) });
    return { status: res.status, url: igUrl(res.url), headers: res.headers, text: await res.text() };
  }).then((r) => {
    entry.status = r.status; entry.inject = r.headers.get('x-sim-inject') || null;
    if (r.headers.get('x-sim-hang')) { entry.outcome = 'page never loaded'; return; }
    timer(tab, Number(r.headers.get('x-sim-latency')) || 0, () => {
      if (tab.gen !== gen || !tab.alive) return;
      tab.url = r.url; tab.status = 'complete'; tab.page = makePage(tab, r.text); tab.title = tab.page.document.title;
    });
  }, (e) => { entry.outcome = String(e); }));
}
function createTab(o) {
  const tab = { id: browser.nextTab++, alive: true, name: 'tab', url: 'about:blank', title: '', status: 'loading', active: !!o.active,
    pinned: !!o.pinned, index: o.index ?? browser.tabs.size, autoDiscardable: true, session: memSession(), gen: 0 };
  browser.tabs.set(tab.id, tab);
  navigate(tab, o.url || IG + '/', o.url && new URL(o.url).pathname !== '/' ? 'lookup' : 'nav');
  return tabInfo(tab);
}
function memSession() { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) }; }
function getTab(id) { const t = browser.tabs.get(id); if (!t) throw new Error('No tab with id: ' + id + '.'); return t; }
async function execScript(inst, inj) {
  const tab = getTab(inj.target && inj.target.tabId);
  const fn = vm.runInContext('(' + inj.func.toString() + ')', tab.page);
  const out = await fn(...clone(inj.args || []));
  return [{ frameId: 0, documentId: 'doc' + tab.gen, result: clone(out) }];
}
// Messages to the service worker. Like Chrome, an event for a stopped worker starts a new one first.
function toWorker(msg, sender) {
  timer(BROWSER, 0, () => {
    if (!browser.sw) { counters.unreachedMsgs++; bootSW('message'); }
    for (const fn of browser.sw.L.onMessage) try { fn(msg, sender, () => {}); } catch (e) { harnessErrors.push('onMessage: ' + e); }
  });
}
function createAlarm(name, info) {
  const old = browser.alarms.get(name);
  if (old) clearTimer(old.timer);
  const period = (info.periodInMinutes || info.delayInMinutes || 0.5) * MIN;
  const a = { name, periodInMinutes: info.periodInMinutes, scheduledTime: V.now + period };
  const fire = () => {
    a.scheduledTime = V.now + period; a.timer = timer(BROWSER, period, fire);
    if (!browser.sw) bootSW('alarm');
    for (const fn of browser.sw.L.onAlarm) try { fn({ name, scheduledTime: V.now, periodInMinutes: a.periodInMinutes }); } catch (e) { harnessErrors.push('onAlarm: ' + e); }
  };
  a.timer = timer(BROWSER, period, fire);
  browser.alarms.set(name, a);
}

// ---------------------------------------------------------------- the service worker
let swSeq = 0;
function makeChrome(inst) {
  const g = (fn) => (inst.alive ? Promise.resolve().then(fn).then((v) => (inst.alive ? v : NEVER())) : NEVER());
  const evt = (name) => ({ addListener: (fn) => inst.L[name].push(fn), removeListener() {}, hasListener: () => true });
  return {
    runtime: { id: EXT_ID, getManifest: () => clone(manifest), getURL: (p) => EXT_ORIGIN + '/' + p, onMessage: evt('onMessage'),
      onInstalled: evt('onInstalled'), onStartup: evt('onStartup'), onMessageExternal: evt('onMessageExternal'),
      reload: () => restart('runtime.reload', 1000), lastError: undefined },
    storage: { local: { get: (k) => g(() => storageGet(k)), set: (o) => g(() => storageSet(o)),
      remove: (k) => g(() => { for (const x of [].concat(k)) delete store[x]; }) } },
    alarms: { get: (n) => g(() => { const a = browser.alarms.get(n); return a ? { name: n, scheduledTime: a.scheduledTime, periodInMinutes: a.periodInMinutes } : undefined; }),
      create: (n, info) => g(() => createAlarm(typeof n === 'string' ? n : '', typeof n === 'string' ? info : n)),
      clear: (n) => g(() => { const a = browser.alarms.get(n); if (a) clearTimer(a.timer); return browser.alarms.delete(n); }), onAlarm: evt('onAlarm') },
    tabs: { query: (q) => g(() => [...browser.tabs.values()].filter((t) => !q || !q.url || t.url.startsWith(String(q.url).replace(/\*$/, ''))).map(tabInfo)),
      get: (id) => g(() => tabInfo(getTab(id))), create: (o) => g(() => createTab(o || {})),
      update: (id, o) => g(() => { const t = getTab(id); Object.assign(t, o.autoDiscardable === undefined ? {} : { autoDiscardable: o.autoDiscardable }); if (o.url) navigate(t, o.url, 'nav'); return tabInfo(t); }),
      reload: (id) => g(() => { const t = getTab(id); ev('tab reload', { id }); navigate(t, t.url.startsWith(IG) ? t.url : IG + '/', 'nav'); }),
      remove: (id) => g(() => { for (const x of [].concat(id)) { const t = browser.tabs.get(x); if (t) { t.alive = false; browser.tabs.delete(x); } } }) },
    windows: { create: (o) => g(() => { const t = createTab({ url: o.url, active: false }); return { id: 2, tabs: [t] }; }) },
    scripting: { executeScript: (inj) => g(() => execScript(inst, inj)) },
    action: { setBadgeText() {}, setBadgeBackgroundColor() {}, setTitle() {} },
  };
}
function bootSW(reason) {
  const inst = { name: (P.prefix || '') + 'sw#' + (++swSeq), alive: true, L: { onMessage: [], onInstalled: [], onStartup: [], onAlarm: [], onMessageExternal: [] } };
  counters.swBoots++;
  const g = { console: quiet, Date: SimDate, AbortController, URL, URLSearchParams, TextEncoder, TextDecoder, structuredClone, DOMException,
    setTimeout: (fn, ms, ...a) => timer(inst, ms, () => fn(...a)), clearTimeout: clearTimer,
    setInterval: (fn, ms) => { const loop = () => { fn(); timer(inst, ms, loop); }; return timer(inst, ms, loop); }, clearInterval: clearTimer,
    fetch: (u, o) => swFetch(inst, u, o), chrome: makeChrome(inst) };
  g.self = g;
  g.importScripts = (...files) => files.forEach((f) => vm.runInContext(read(path.join(EXT, f)), g, { filename: f }));
  vm.createContext(g);
  browser.sw = inst;
  ev('worker start', { sw: inst.name, reason });
  vm.runInContext(read(path.join(EXT, 'background.js')), g, { filename: 'background.js' });
  if (reason === 'install') for (const fn of inst.L.onInstalled) fn({ reason: 'install' });
  return inst;
}
function restart(reason, bootAfter) {
  const sw = browser.sw;
  if (!sw) return;
  sw.alive = false; browser.sw = null;
  restarts.push({ t: V.now, reason, sw: sw.name });
  ev('worker stopped', { sw: sw.name, reason });
  if (bootAfter != null) timer(OPS, bootAfter, () => { if (!browser.sw) bootSW('restart after: ' + reason); });
}

// The worker's fetch: only the local server is reachable (host_permissions). Harness faults hook in here.
async function swFetch(inst, url, init = {}) {
  const u = new URL(String(url));
  if (u.origin !== SERVER_CONST) throw new TypeError('Failed to fetch');
  const method = (init.method || 'GET').toUpperCase();
  const headers = { ...(init.headers || {}), 'X-Sim-Now': String(V.now) };
  if (method !== 'GET') headers.Origin = EXT_ORIGIN; // Chrome sends the extension origin on POSTs; GETs rely on X-FL
  const entry = { t: V.now, method, path: u.pathname, sw: inst.name, body: init.body ? JSON.parse(init.body) : undefined };
  serverLog.push(entry);
  let fault = null;
  if (u.pathname === '/api/ext/list-page') {
    const k = ++counters.listPagePosts;
    if (!P.faults) { /* lanes mode: no harness faults */ } else {
    entry.k = k;
    if (k === FAULT.outageAt) {
      await io(OPS, stopServer);
      ev('server offline', { for_min: FAULT.outageMin });
      timer(OPS, FAULT.outageMin * MIN, () => io(OPS, startServer).then(() => ev('server back')));
    }
    if (FAULT.dropResponse.has(k)) fault = 'drop';
    if (k === FAULT.restartAfterCommit) fault = 'restart';
    }
  }
  if (u.pathname === '/api/ext/profile' && FAULT.dropProfileResponse.has(++counters.profilePosts) && P.faults) fault = 'drop';
  const send = async () => {
    const r = await fetch(`http://127.0.0.1:${PORT.server}${u.pathname}${u.search}`, { method, headers, body: init.body,
      signal: AbortSignal.timeout(20000) });
    return { status: r.status, text: await r.text() };
  };
  let r;
  try { r = await io(fault ? OPS : inst, send); } catch (e) { entry.error = String(e.cause && e.cause.code || e); throw new TypeError('Failed to fetch'); }
  entry.status = r.status;
  try { entry.resp = JSON.parse(r.text); } catch { entry.resp = r.text.slice(0, 200); }
  if (r.status >= 200 && r.status < 300) {
    const key = entry.body !== undefined ? u.pathname + ' ' + init.body : null;
    if (key && boxSeen.has(key)) boxSeen.get(key).acked = true;
    if (entry.resp && entry.resp.duplicate) counters.dupResponses++;
  }
  if (fault === 'drop') {
    entry.fault = 'response lost after commit';
    ev('response lost after server commit', { path: u.pathname, k: entry.k });
    throw new TypeError('Failed to fetch');
  }
  if (fault === 'restart') {
    entry.fault = 'worker stopped after commit';
    const b = entry.body;
    expectations.push({ type: 'resume-after-commit-restart', list: b.seed.toLowerCase() + '/' + b.direction, want: b.next_cursor,
      done: b.done, after: V.now, dupKey: u.pathname + ' ' + init.body });
    restart('stopped after the server committed list-page #' + entry.k + ', before the outbox was updated', 3000);
    return NEVER();
  }
  const text = r.text;
  return { status: r.status, ok: r.status < 300, headers: new Headers(), json: async () => JSON.parse(text), text: async () => text };
}

// ---------------------------------------------------------------- invariants checked at every Instagram request
function checkRequest(e) {
  const st = FL.normalize ? FL.normalize(clone(store.st), V.now) : { ...FL.fresh(), ...clone(store.st) };
  const cool = coolOf(st);
  if (st.hold) viol('Instagram request during a hold', { path: e.path });
  if (store.localPaused) viol('Instagram request while paused', { path: e.path });
  if (cool[e.bucket] && cool[e.bucket].until > e.t) viol('Instagram request during ' + e.bucket + ' cooldown', { path: e.path, until: dhm(cool[e.bucket].until) });
  if (e.kind === 'list' || e.kind === 'profile') {
    if (st.nextAt > e.t) viol('Instagram request before nextAt', { path: e.path, early_ms: st.nextAt - e.t });
    if (e.kind === 'profile' && st.profileNextAt > e.t) viol('profile request before profileNextAt', { early_ms: st.profileNextAt - e.t });
  }
  // The lane marker must be this request's own (set just before it), never an earlier request still in flight.
  if (store.lane && store.lane.until > e.t && store.lane.url && !String(store.lane.url).includes(e.path)) viol('request while another holds the lane', { path: e.path, lane: store.lane.url });
  if (P.onRequest) P.onRequest(e, st);
}

return { bootSW, restart, createTab, toWorker };
}
const newProfile = (o) => { const P = { name: 'p0', store: {}, browser: { sw: null, tabs: new Map(), nextTab: 1, alarms: new Map() }, hits: [], holds: [], ...o }; return Object.assign(P, makeProfile(P)); };
const manifest = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
const P0 = newProfile({ hits, holds, faults: true });
const store = P0.store, browser = P0.browser, { bootSW, restart, createTab } = P0;

// ---------------------------------------------------------------- operator (Michael) and server background work
async function opsHttp(p, body) {
  return io(OPS, async () => {
    const r = await fetch(`http://127.0.0.1:${PORT.server}${p}`, { method: body ? 'POST' : 'GET', signal: AbortSignal.timeout(20000),
      headers: { 'content-type': 'application/json', 'X-Sim-Now': String(V.now), ...(body ? { Origin: uiOrigin() } : {}) },
      body: body ? JSON.stringify(body) : undefined });
    return r.json();
  });
}
// README: "(or Pause→Resume in the workspace)". If the server is already paused (it pauses itself on a security check),
// Resume is enough; otherwise Pause, let a heartbeat see it, then Resume.
async function workspaceResume() {
  const sc = await opsHttp('/api/scraper');
  if (!sc.paused) { ev('operator: Pause in workspace'); await opsHttp('/api/scraper/pause', { paused: true }); await new Promise((r) => timer(OPS, MIN, r)); }
  ev('operator: Resume in workspace');
  await opsHttp('/api/scraper/pause', { paused: false });
}
const uiOrigin = () => `http://127.0.0.1:${PORT.server}`; // the workspace page's own origin
let holdN = 0;
function operator() {
  timer(OPS, MIN, operator);
  if (!serverUp) return;
  const st = store.st, h = holds.at(-1);
  if (st && st.hold && h && h.end == null) {
    h.policy = h.policy || HOLD_POLICY[holdN++ % HOLD_POLICY.length];
    const age = V.now - h.start;
    if (h.policy === 'workspace' && age >= 12 * MIN && !h.acted) { h.acted = V.now; workspaceResume(); }
    if (h.policy === 'popup' && age >= 15 * MIN && !h.acted) {
      h.acted = V.now; ev('operator: Resume in popup');
      if (browser.sw) for (const fn of browser.sw.L.onMessage) fn({ cmd: 'resume' }, { id: EXT_ID }, () => {});
    }
  }
  // README: "paused ... until you press Resume in the popup". If the popup Resume alone doesn't get scraping going again
  // within 30 min (the server paused itself on the security check), Michael resumes in the workspace too, and the
  // report flags it.
  const last = holds.at(-1);
  if (last && last.policy === 'popup' && last.end != null && !last.workspace && V.now - last.end >= 30 * MIN) {
    const moved = igLog.some((e) => e.t > last.end && e.kind !== 'nav');
    if (!moved) {
      last.workspace = V.now; last.stuckMin = Math.round((V.now - last.end) / MIN); last.stuckText = store.view && store.view.text;
      ev('operator: popup still says "' + last.stuckText + '" ' + last.stuckMin + ' min after Resume; Resume in workspace too');
      workspaceResume();
    } else last.workspace = -1;
  }
}
function serverTick() {
  timer(OPS, 30e3, serverTick);
  if (serverUp) { counters.ticks++; opsHttp('/__sim/tick').catch(() => {}); }
}
const hourly = [];
function snapshot() {
  timer(OPS, HOUR, snapshot);
  const st = store.st || {};
  hourly.push({ t: V.now, list: igLog.filter((e) => e.kind === 'list').length, profile: igLog.filter((e) => e.kind === 'profile').length,
    pages: serverLog.filter((e) => e.path === '/api/ext/list-page' && e.status === 200 && !(e.resp && e.resp.duplicate)).length,
    view: store.view ? store.view.text : '', cool: coolOf(st) });
}

// ---------------------------------------------------------------- run
async function main() {
  PORT.server = await freePort(); PORT.ig = await freePort();
  launch('fake_ig', [path.join(HERE, 'fake_ig.py'), '--port', String(PORT.ig)]);
  await startServer();
  await waitHttp(`http://127.0.0.1:${PORT.ig}/__sim/log`);
  const seeds = SEEDS.map(([handle, direction, size, x = {}], j) => {
    const s = { pk: String(7_100_000_000 + j), username: handle, handle, direction, size, private: !!x.private, capped: !!x.capped,
      verified: !!x.verified, lists: { [direction]: size },
      followers: direction === 'followers' ? (x.claimed || size) : 300 + j * 17, following: direction === 'following' ? size : 200 + j * 11 };
    seedByPk.set(s.pk, s); seedByName.set(handle.toLowerCase(), s);
    return s;
  });
  await fetch(`http://127.0.0.1:${PORT.ig}/__sim/config`, { method: 'POST', body: JSON.stringify({ seeds, schedule: SCHEDULE }) });
  // Contract: /api/ext/* must refuse other origins.
  const bad = await fetch(`http://127.0.0.1:${PORT.server}/api/ext/heartbeat`, { method: 'POST', headers: { Origin: 'https://evil.example', 'content-type': 'application/json' }, body: '{}' });
  if (bad.status !== 403) viol('/api/ext/heartbeat accepted a foreign Origin', { status: bad.status });
  for (const dir of ['followers', 'following']) {
    const r = await fetch(`http://127.0.0.1:${PORT.server}/api/scraper/seeds`, { method: 'POST',
      headers: { 'content-type': 'application/json', 'X-Sim-Now': String(V.now), Origin: uiOrigin() },
      body: JSON.stringify({ handles: seeds.filter((s) => s.direction === dir).map((s) => s.handle), directions: [dir] }) });
    if (!r.ok) throw new Error('queueing seeds failed: ' + r.status + ' ' + await r.text());
  }
  // Michael's browser: one pinned background instagram.com tab, logged in.
  createTab({ url: IG + '/', active: false, pinned: true });
  timer(OPS, 2000, () => bootSW('install'));
  timer(OPS, MIN, operator);
  timer(OPS, 15e3, serverTick);
  timer(OPS, HOUR, snapshot);
  timer(OPS, FAULT.idleKillAt - START, () => restart('idle worker stopped by Chrome (no explicit restart; the alarm must wake it)'));
  await runUntil(END);
  for (const o of [OPS, BROWSER]) o.alive = false;
  if (browser.sw) browser.sw.alive = false;
  for (const t of browser.tabs.values()) t.alive = false;
  if (!serverUp) await startServer();
  await report(seeds);
}

// ---------------------------------------------------------------- checks and report
function modelCooldowns() {
  // Independent model of the documented policy (README / RESEARCH.md §4), not core.js: per bucket 10 min doubling per hit
  // in 24 h, cap 24 h, Retry-After wins if longer, 3 hits in 24 h = until local midnight; every hit pauses the lane 5 min;
  // 3 hits within an hour across buckets = both until midnight.
  const P = FL.PACE, bucketed = !!(P0.lastSt && P0.lastSt.cool);
  const m = { hits: { list: [], profile: [] }, until: { list: 0, profile: 0 } }, windows = [], problems = [];
  const midnight = (t) => { const d = new RealDate(t); d.setHours(24, 0, 0, 0); return d.getTime(); };
  for (const h of hits) {
    const b = bucketed ? h.bucket : 'list';
    m.hits[b].push(h.at);
    const n = m.hits[b].filter((t) => h.at - t < DAY).length;
    let until = h.at + Math.min(10 * MIN * 2 ** (n - 1), DAY);
    if (h.retryAt && h.retryAt > until) until = h.retryAt;
    if (n >= 3) until = Math.max(until, midnight(h.at));
    m.until[b] = Math.max(m.until[b], until);
    if (bucketed && [...m.hits.list, ...m.hits.profile].filter((t) => h.at - t < HOUR).length >= 3) for (const k of ['list', 'profile']) m.until[k] = Math.max(m.until[k], midnight(h.at));
    const got = coolOf(h.post);
    for (const k of bucketed ? ['list', 'profile'] : ['list']) {
      if (got[k].until !== m.until[k]) problems.push(`hit at ${dhm(h.at)} (${h.inject}): ${k} cooldown until ${dhm(got[k].until)}, policy says ${dhm(m.until[k])}`);
    }
    if (P.hitPause && !(h.post.nextAt >= h.at + P.hitPause)) problems.push(`hit at ${dhm(h.at)}: lane not paused ${P.hitPause / MIN} min`);
    // Replay core.js applyHit on the exact pre-hit state: the stored result must be what applyHit computes.
    const pre = FL.normalize ? FL.normalize(clone(h.pre), h.at) : { ...FL.fresh(), ...clone(h.pre) };
    const exp = coolOf(FL.applyHit(pre, h.at, h.retryAt, h.bucket));
    for (const k of ['list', 'profile']) if (exp[k].until !== got[k].until) problems.push(`hit at ${dhm(h.at)}: stored ${k} until ${dhm(got[k].until)} != core.applyHit ${dhm(exp[k].until)}`);
    windows.push({ bucket: b, from: h.at, to: m.until[b], why: h.inject }, ...(P.hitPause ? [{ bucket: '*', from: h.at, to: h.at + P.hitPause, why: 'hit pause' }] : []));
  }
  return { windows, problems, bucketed };
}
const pyQuery = (db) => JSON.parse(execFileSync('python3', ['-c', `
import sqlite3, json, sys
c = sqlite3.connect(sys.argv[1]); c.row_factory = sqlite3.Row
edges = {}
for r in c.execute('SELECT e.seed, e.direction, p.ig_id FROM edges e JOIN people p ON p.id=e.person_id'):
    edges.setdefault(r['seed'].lower() + '/' + r['direction'], []).append(r['ig_id'])
print(json.dumps({'lists': [dict(r) for r in c.execute('SELECT * FROM lists')], 'edges': edges,
  'edge_rows': c.execute('SELECT count(*) FROM edges').fetchone()[0],
  'edge_distinct': c.execute('SELECT count(*) FROM (SELECT DISTINCT seed, person_id, direction FROM edges)').fetchone()[0],
  'people': c.execute('SELECT count(*) FROM people').fetchone()[0],
  'bios': c.execute("SELECT count(*) FROM people WHERE bio_at IS NOT NULL").fetchone()[0],
  'jobs': [dict(r) for r in c.execute('SELECT kind, state, count(*) n FROM jobs GROUP BY 1,2')],
  'pages': c.execute('SELECT count(*) FROM pages').fetchone()[0]}))
`, db], { maxBuffer: 256e6 }).toString());

async function report(seeds) {
  const served = await (await fetch(`http://127.0.0.1:${PORT.ig}/__sim/served`)).json();
  const fakeLog = await (await fetch(`http://127.0.0.1:${PORT.ig}/__sim/log`)).json();
  const scraper = await (await fetch(`http://127.0.0.1:${PORT.server}/api/scraper`)).json();
  const db = pyQuery(DB);
  const endSt = FL.normalize ? FL.normalize(clone(store.st), END) : store.st;
  const cool = coolOf(endSt);
  const pass = [], fail = [];
  const check = (ok, name, detail) => (ok ? pass : fail).push(detail ? `${name}: ${detail}` : name);

  // 1. lists terminal or correctly parked
  const rows = [];
  for (const s of seeds) {
    const key = s.handle.toLowerCase() + '/' + s.direction, L = db.lists.find((l) => l.seed.toLowerCase() + '/' + l.direction === key) || {};
    const srv = new Set((served[key] || {}).served || []), got = new Set(db.edges[key] || []);
    const same = srv.size === got.size && [...srv].every((x) => got.has(x));
    const expect = s.private ? 0 : s.capped ? 49 : s.size;
    const parked = ['queued', 'running'].includes(L.state) && (cool.list.until > END || endSt.hold);
    const terminal = L.state === 'done' || L.state === 'private';
    rows.push({ list: '@' + s.handle + ' ' + s.direction, size: s.private ? 'private' : s.capped ? `${s.size} (cap 49)` : s.size,
      pages: (served[key] || {}).pages_good || 0, served: srv.size, edges: got.size, state: L.state + (L.state === 'done' && s.capped ? ' (limited)' : ''),
      total: L.total, ok: same && (terminal ? (L.state !== 'done' || got.size === expect) : parked) });
    check(terminal || parked, 'list terminal or parked in cooldown', `${key} state=${L.state}`);
    if (terminal && L.state === 'done') check(got.size === expect, 'done list complete', `${key} ${got.size}/${expect}`);
    check(same, 'edges == unique users served', `${key} edges ${got.size} vs served ${srv.size}`);
  }
  check(db.edge_rows === db.edge_distinct, 'no duplicate edge rows', `${db.edge_rows} rows / ${db.edge_distinct} distinct`);
  // 2. resent pages: duplicates acknowledged, never double counted
  const dups = serverLog.filter((e) => e.path === '/api/ext/list-page' && e.resp && e.resp.duplicate);
  check(dups.length >= FAULT.dropResponse.size + 1, 'resent list pages answered as duplicates', `${dups.length} duplicate answers`);
  for (const d of dups) {
    const first = serverLog.find((e) => e.path === d.path && e !== d && e.t <= d.t && JSON.stringify(e.body) === JSON.stringify(d.body) && e.status === 200);
    check(first && first.resp && first.resp.received === d.resp.received, 'duplicate did not change the count',
      `${d.body.seed}/${d.body.direction} ${first && first.resp && first.resp.received} -> ${d.resp.received}`);
  }
  // 3. cursors resume after restarts; no list ever restarted from the top
  for (const x of expectations) {
    const next = igLog.find((e) => e.kind === 'list' && e.list === x.list && e.t > x.after);
    if (x.done) check(!next, 'finished list not requested again after restart', x.list);
    else check(next && next.maxId === x.want, x.type, `${x.list} wanted max_id=${x.want} got ${next ? next.maxId : 'no request'}${next ? ' at ' + dhm(next.t) : ''}`);
    if (x.dupKey) check(serverLog.some((e) => e.t > x.after && e.path + ' ' + JSON.stringify(e.body) === x.dupKey && e.resp && e.resp.duplicate),
      'page in flight at restart re-sent from the outbox', x.list);
  }
  for (const s of seeds) {
    const key = s.handle.toLowerCase() + '/' + s.direction, reqs = igLog.filter((e) => e.kind === 'list' && e.list === key);
    let started = false, restartedTop = 0;
    for (const e of reqs) { if (started && !e.maxId) restartedTop++; if (e.status === 200 && !e.inject?.match(/hang|freeze|soft_block|login|please/)) started = true; }
    if (restartedTop) check(false, 'list never restarts from the first page', `${key} requested page 1 again ${restartedTop}x`);
  }
  // 4. cooldown escalation = policy = core.js applyHit
  const model = modelCooldowns();
  const injectedHits = fakeLog.log.filter((e) => ['429', '429_retry', 'please_wait', 'soft_block'].includes(e.inject)).length;
  check(hits.length === injectedHits, 'every rate limit / soft block became exactly one hit', `${hits.length} hits, ${injectedHits} injected`);
  check(!model.problems.length, 'cooldown escalation matches the policy and core.js applyHit', model.problems.join('; ') || `${hits.length} hits checked`);
  // 5. no request inside a cooldown window or during a hold (model windows, independent of stored state)
  const inWin = igLog.filter((e) => e.kind !== 'nav').filter((e) => model.windows.some((w) => (w.bucket === '*' || w.bucket === e.bucket || !model.bucketed) && e.t > w.from && e.t < w.to));
  check(!inWin.length, 'no request during cooldown', inWin.slice(0, 3).map((e) => `${e.path} at ${dhm(e.t)}`).join(', ') || `${model.windows.length} windows`);
  const inHold = igLog.filter((e) => e.kind !== 'nav' && holds.some((h) => e.t > h.start && e.t < (h.end ?? Infinity)));
  check(!inHold.length, 'no request during a hold', inHold.slice(0, 3).map((e) => `${e.path} at ${dhm(e.t)}`).join(', ') || `${holds.length} holds`);
  // 6. pacing
  const reqs = igLog.filter((e) => e.kind !== 'nav');
  let minGap = Infinity, minGapAt = null, minProfile = Infinity, lastProfile = null;
  for (let i = 1; i < reqs.length; i++) { const g = reqs[i].t - reqs[i - 1].t; if (g < minGap) { minGap = g; minGapAt = reqs[i]; } }
  for (const e of reqs.filter((e) => e.kind === 'profile' || (e.kind === 'page' && e.bucket === 'profile'))) { if (lastProfile) minProfile = Math.min(minProfile, e.t - lastProfile.t); lastProfile = e; }
  check(minGap >= FL.PACE.listGap[0], 'request gaps >= PACE.listGap min', `min ${(minGap / 1e3).toFixed(1)} s${minGapAt ? ' (' + minGapAt.path + ' at ' + dhm(minGapAt.t) + ')' : ''}`);
  if (Number.isFinite(minProfile)) check(minProfile >= FL.PACE.profileGap[0], 'bio reads >= PACE.profileGap min', `min ${(minProfile / 1e3).toFixed(1)} s`);
  // 7. outbox
  const lost = [...boxSeen.entries()].filter(([, v]) => !v.acked);
  check(!lost.length, 'outbox: every queued result reached the server', `${boxSeen.size} items, ${lost.length} never acknowledged${lost.length ? ': ' + lost.slice(0, 3).map(([k]) => k.slice(0, 80)).join(' | ') : ''}`);
  check(!(store.box || []).length, 'outbox empty at the end', `${(store.box || []).length} left`);
  const rejected = serverLog.filter((e) => e.status >= 400 && e.status < 500);
  check(!rejected.length, 'server never rejected an extension call (4xx = dropped by the outbox)', rejected.slice(0, 3).map((e) => `${e.path} ${e.status} ${JSON.stringify(e.resp)}`).join('; ') || `${serverLog.length} calls`);
  const outage = events.find((e) => e.what === 'server offline'), back = events.find((e) => e.what === 'server back');
  if (outage && back) {
    const during = serverLog.filter((e) => e.t >= outage.t && e.t < back.t && e.error).length;
    check(during > 0, 'server outage exercised', `${during} failed calls in ${Math.round((back.t - outage.t) / MIN)} min, results held and delivered`);
  }
  // 8. misc
  check(!fakeLog.log.some((e) => e.path === '/api/v1/users/web_profile_info/'), 'never calls the retired web_profile_info');
  check(!harnessErrors.length, 'no uncaught errors in the extension', harnessErrors.slice(0, 3).join(' | '));
  for (const h of holds.filter((h) => h.policy === 'popup')) check(!(h.stuckMin > 0), 'popup Resume restarts scraping after a hold',
    h.stuckMin > 0 ? `after the ${h.code} hold at ${dhm(h.start)} the popup Resume cleared the hold but the extension stayed "${h.stuckText}" for ${h.stuckMin} min (server still paused)` : '');
  // Local progress (prog, used by the empty-page guard) must agree with what the server counted.
  for (const s of seeds) {
    const key = s.handle.toLowerCase() + '/' + s.direction, p = (store.prog || {})[key], L = db.lists.find((l) => l.seed.toLowerCase() + '/' + l.direction === key);
    if (p && L && L.state === 'done' && p.received != null) check(p.received === L.received, 'local list progress == server count', `${key} prog.received=${p.received} server=${L.received}`);
  }
  const stuck = holds.filter((h) => h.end == null);
  check(!stuck.length, 'every hold was cleared after Resume', stuck.map((h) => h.code).join(','));

  // ---- summary
  const cdMin = (b) => { let tot = 0, cur = null; for (const w of model.windows.filter((w) => w.bucket === b).sort((x, y) => x.from - y.from)) {
    const a = Math.max(w.from, START), z = Math.min(w.to, END); if (z <= a) continue;
    if (cur && a <= cur[1]) cur[1] = Math.max(cur[1], z); else { if (cur) tot += cur[1] - cur[0]; cur = [a, z]; } }
    return Math.round(((cur ? cur[1] - cur[0] : 0) + tot) / MIN); };
  const pad = (s, n) => String(s).padEnd(n), lpad = (s, n) => String(s).padStart(n);
  console.log(`\nFortunate Leads e2e — ${HOURS} simulated hours (${dhm(START)} → ${dhm(END)} ${process.env.TZ}), extension ${manifest.version}`);
  console.log(`tmp: ${TMP}\n`);
  console.log(pad('list', 34) + lpad('size', 11) + lpad('pages', 7) + lpad('served', 8) + lpad('edges', 7) + '  ' + pad('state', 16) + 'ok');
  for (const r of rows) console.log(pad(r.list, 34) + lpad(r.size, 11) + lpad(r.pages, 7) + lpad(r.served, 8) + lpad(r.edges, 7) + '  ' + pad(r.state, 16) + (r.ok ? 'yes' : 'NO'));
  const kinds = (k) => igLog.filter((e) => e.kind === k).length;
  const freshPages = serverLog.filter((e) => e.path === '/api/ext/list-page' && e.status === 200 && !(e.resp && e.resp.duplicate)).length;
  console.log('\nIG requests    ' + `${kinds('list')} list, ${kinds('profile')} bio (/info/), ${kinds('page')} profile page loads, ${kinds('nav')} tab loads/reloads`);
  console.log('pages          ' + `${freshPages} list pages ingested (${counters.listPagePosts} posts incl. ${dups.length} duplicates)`);
  console.log('people         ' + `${db.people} people, ${db.edge_rows} edges, ${db.bios} bios read`);
  console.log('hits           ' + hits.map((h) => `${dhm(h.at)} ${h.bucket}/${h.inject}`).join(', '));
  console.log('cooldown       ' + `lists ${cdMin('list')} min, bios ${cdMin('profile')} min, hit pauses ${cdMin('*')} min (within the run)`);
  console.log('holds          ' + holds.map((h) => `${dhm(h.start)} ${h.code} ${h.end ? Math.round((h.end - h.start) / MIN) + ' min via ' + h.policy + (h.stuckMin != null ? ` (then stuck ${h.stuckMin} min: "${h.stuckText}")` : '') : 'NOT CLEARED'}`).join(', '));
  console.log('restarts       ' + restarts.map((r) => `${dhm(r.t)} ${r.reason}`).join('; '));
  console.log('server         ' + `${serverLog.length} extension calls, ${serverLog.filter((e) => e.error).length} failed while offline, outage ${outage ? dhm(outage.t) + '–' + dhm(back && back.t) : 'none'}, ${counters.ticks} background ticks`);
  console.log('end state      ' + `popup "${store.view && store.view.text}", list cooldown until ${cool.list.until > END ? dhm(cool.list.until) : '-'}, bio cooldown until ${cool.profile.until > END ? dhm(cool.profile.until) : '-'}, server ext.state=${scraper.ext && scraper.ext.state}`);
  console.log('simulated      ' + `${HOURS} h in ${((RealDate.now() - REAL_T0) / 1e3).toFixed(1)} s real (min request gap ${(minGap / 1e3).toFixed(1)} s)`);
  console.log('\nhourly         ' + hourly.map((h) => `${hm(h.t)} ${h.pages}p/${h.profile}b`).join('  '));
  console.log(`\nchecks: ${pass.length} passed, ${fail.length} failed`);
  for (const f of fail) console.log('  FAIL ' + f);
  if (VERBOSE) for (const p of pass) console.log('  ok   ' + p);
  if (VERBOSE) for (const x of expectations) console.log('\n' + x.type + ' ' + x.list + ' at ' + dhm(x.after) + ':\n' +
    igLog.filter((e) => e.list === x.list && Math.abs(e.t - x.after) < 5 * MIN).map((e) => `  ${dhm(e.t)}:${new RealDate(e.t).getSeconds()} ${e.sw} max_id=${e.maxId} -> ${e.status}`).join('\n'));
  if (VERBOSE) console.log('\nextension trail (last 40):\n' + (store.trail || []).map((x) => '  ' + JSON.stringify(x)).join('\n') +
    '\nlastError: ' + (store.st && store.st.lastError));
  for (const v of violations.slice(0, 10)) console.log('  VIOLATION ' + dhm(v.t) + ' ' + v.what + ' ' + JSON.stringify(v));
  const slim = (e) => ({ ...e, at: dhm(e.t) + ':' + String(new RealDate(e.t).getSeconds()).padStart(2, '0'),
    body: e.body && e.body.users ? { ...e.body, users: e.body.users.length } : e.body });
  fs.writeFileSync(path.join(TMP, 'log.json'), JSON.stringify({ ig: igLog.map(slim), server: serverLog.map(slim), trail: store.trail }, null, 0));
  fs.writeFileSync(path.join(TMP, 'result.json'), JSON.stringify({ pass, fail, violations, events: events.map((e) => ({ ...e, t: dhm(e.t) })), rows, hits: hits.map((h) => ({ at: dhm(h.at), bucket: h.bucket, inject: h.inject })) }, null, 1));
  const ok = !fail.length && !violations.length;
  console.log(ok ? '\nPASS' : '\nFAIL');
  cleanup();
  if (!flag('keep') && ok) fs.rmSync(TMP, { recursive: true, force: true });
  process.exit(ok ? 0 : 1);
}


// ================================================================ --lanes N: several Chrome profiles, one server
// Each lane is its own emulated Chrome profile (storage, tabs, alarms, service worker) running the real extension,
// logged in to its own fake Instagram account with its own failures. Checks: no page fetched twice, a list is never
// worked by two lanes at once, a logged-out lane's list moves to another lane from the saved cursor, a lane's cooldown
// never stops the others. `--lanes 1,2,4` runs each count in its own process and compares the time to 10k connections.
const LANE_SEEDS = [
  ['north.goods', 'followers', 3000], ['clayandco', 'followers', 2400], ['saltlabs', 'followers', 1800],
  ['fern.skin', 'followers', 1400], ['honeyroast', 'followers', 1000], ['wildthread', 'followers', 700],
  ['goldmade', 'following', 2800], ['linenhouse', 'following', 2000], ['stoneworks', 'following', 1200],
  ['amber.club', 'following', 600],
];
// Per-account Instagram answers by that account's own list request number.
const LANE_SCHEDULE = [
  { list: { 200: '429' } },                   // lane 1: a rate limit (its own cooldown only)
  { list: { 20: 'login_redirect' } },         // lane 2: logged out mid-list -> its list must move on
  { list: { 45: 'soft_block' } },             // lane 3
  { list: { 60: 'please_wait' } },           // lane 4
];
const RESUME_AFTER = [12 * MIN, 45 * MIN, 12 * MIN, 12 * MIN]; // when Michael fixes a held lane (popup Resume)
const TARGET = 10000;

async function lanesCompare(counts) {
  const out = [];
  for (const n of counts) {
    const args = [fileURLToPath(import.meta.url), '--lanes', String(n), '--json', ...(opt('hours') ? ['--hours', opt('hours')] : [])];
    const r = await new Promise((res) => {
      const p = spawn(process.execPath, args, { stdio: ['ignore', 'pipe', 'inherit'] });
      let buf = ''; p.stdout.on('data', (d) => { buf += d; if (!flag('quiet')) process.stdout.write(d); });
      p.on('exit', (code) => res({ code, buf }));
    });
    const line = r.buf.split('\n').find((l) => l.startsWith('RESULT '));
    out.push({ n, code: r.code, res: line ? JSON.parse(line.slice(7)) : null });
  }
  const t = (ms) => (ms == null ? '–' : (ms / HOUR).toFixed(2) + ' h');
  console.log('\nlanes  time to 10k connections  all lists done  pages  limits  checks');
  for (const { n, code, res } of out) {
    console.log(String(n).padStart(5) + '  ' + t(res && res.t10k).padStart(23) + '  ' + t(res && res.tDone).padStart(14) + '  ' +
      String(res ? res.pages : '–').padStart(5) + '  ' + String(res ? res.hits : '–').padStart(6) + '  ' + (code === 0 ? 'pass' : 'FAIL'));
  }
  const base = out[0] && out[0].res && out[0].res.t10k;
  if (base) console.log('speed-up vs ' + out[0].n + ' lane' + (out[0].n > 1 ? 's' : '') + ': ' + out.map((o) => o.n + ' → ' + (o.res && o.res.t10k ? (base / o.res.t10k).toFixed(2) + 'x' : '–')).join(', '));
  cleanup();
  process.exit(out.every((o) => o.code === 0) ? 0 : 1);
}

async function lanesMain(N) {
  PORT.server = await freePort(); PORT.ig = await freePort();
  launch('fake_ig', [path.join(HERE, 'fake_ig.py'), '--port', String(PORT.ig)]);
  await startServer();
  await waitHttp(`http://127.0.0.1:${PORT.ig}/__sim/log`);
  const seeds = LANE_SEEDS.map(([handle, direction, size], j) => {
    const s = { pk: String(7_200_000_000 + j), username: handle, handle, direction, size, lists: { [direction]: size },
      followers: direction === 'followers' ? size : 300 + j * 17, following: direction === 'following' ? size : 200 + j * 11 };
    seedByPk.set(s.pk, s); seedByName.set(handle.toLowerCase(), s);
    return s;
  });
  const accts = Array.from({ length: N }, (_, i) => ({ ig_id: String(5_500_000_000 + i), handle: 'lane' + (i + 1) + '.sim', schedule: LANE_SCHEDULE[i % LANE_SCHEDULE.length] }));
  await fetch(`http://127.0.0.1:${PORT.ig}/__sim/config`, { method: 'POST', body: JSON.stringify({ seeds, schedule: {}, accounts: accts }) });
  for (const dir of ['followers', 'following']) {
    await fetch(`http://127.0.0.1:${PORT.server}/api/scraper/seeds`, { method: 'POST',
      headers: { 'content-type': 'application/json', 'X-Sim-Now': String(V.now), Origin: uiOrigin() },
      body: JSON.stringify({ handles: seeds.filter((s) => s.direction === dir).map((s) => s.handle), directions: [dir] }) });
  }
  // who works which list right now, from the requests themselves
  const owner = new Map(), switches = [], lanes = [];
  // a lane stopped working its list when a limit or login wall hit it after its last request of that list
  const stopped = (L) => !L.browser.sw || L.hits.some((h) => h.at >= L.lastListAt) || L.holds.some((h) => h.start >= L.lastListAt);
  const onRequest = (L) => (e) => {
    if (e.kind !== 'list') return;
    const prev = owner.get(e.list);
    if (prev && prev !== L) {
      // the lane that had it must have stopped (hold, list cooldown, gone) or moved on to another list
      if (prev.lastList === e.list && !stopped(prev)) viol('list worked by two lanes at once', { list: e.list, lanes: [prev.name, L.name] });
      switches.push({ t: V.now, list: e.list, from: prev.name, to: L.name, maxId: e.maxId, why: prev.store.st && prev.store.st.hold ? 'hold' : 'cooldown' });
      ev('list moved', { list: e.list, from: prev.name, to: L.name, max_id: e.maxId });
    }
    owner.set(e.list, L); L.lastList = e.list; L.lastListAt = V.now;
  };
  for (let i = 0; i < N; i++) {
    const L = newProfile({ name: 'lane' + (i + 1), prefix: 'L' + (i + 1) + ':', igId: accts[i].ig_id, handle: accts[i].handle, faults: false });
    L.onRequest = onRequest(L);
    lanes.push(L);
    L.createTab({ url: IG + '/', active: false, pinned: true });
    timer(OPS, 2000 + i * 700, () => L.bootSW('install'));
  }
  // Michael clears a held lane after RESUME_AFTER (popup Resume in that profile)
  const opsLoop = () => {
    timer(OPS, MIN, opsLoop);
    lanes.forEach((L, i) => {
      const h = L.holds.at(-1);
      if (h && h.end == null && !h.acted && V.now - h.start >= RESUME_AFTER[i % RESUME_AFTER.length] && L.browser.sw) {
        h.acted = V.now; ev('operator: Resume in popup', { lane: L.name });
        for (const fn of L.browser.sw.L.onMessage) fn({ cmd: 'resume' }, { id: EXT_ID }, () => {});
      }
    });
  };
  timer(OPS, MIN, opsLoop);
  timer(OPS, 15e3, serverTick);
  // connections so far (edges added by fresh pages) and the stop condition: every list done
  let t10k = null, tDone = null;
  const countConns = () => {
    const conns = serverLog.filter((e) => e.path === '/api/ext/list-page' && e.status === 200 && e.resp && !e.resp.duplicate).reduce((a, e) => a + (e.body.users || []).length, 0);
    if (t10k == null && conns >= TARGET) { t10k = V.now - START; ev('10k connections', { h: (t10k / HOUR).toFixed(2) }); }
  };
  const watch = () => {
    timer(OPS, 5 * MIN, watch);
    countConns();
    if (!serverUp) return;
    opsHttp('/api/scraper').then((sc) => {
      if (sc.lists && sc.lists.length === seeds.length && sc.lists.every((l) => l.state === 'done' || l.state === 'private') && !sc.queue.list) {
        tDone = tDone ?? V.now - START; V.stop = true;
      }
    }).catch(() => {});
  };
  timer(OPS, 5 * MIN, watch);
  await runUntil(END);
  countConns();
  for (const L of lanes) { if (L.browser.sw) L.browser.sw.alive = false; for (const t of L.browser.tabs.values()) t.alive = false; }
  OPS.alive = false; BROWSER.alive = false;
  await lanesReport(N, seeds, lanes, accts, switches, t10k, tDone);
}

async function lanesReport(N, seeds, lanes, accts, switches, t10k, tDone) {
  const served = await (await fetch(`http://127.0.0.1:${PORT.ig}/__sim/served`)).json();
  const fakeLog = await (await fetch(`http://127.0.0.1:${PORT.ig}/__sim/log`)).json();
  const acc = await (await fetch(`http://127.0.0.1:${PORT.server}/api/accounts`)).json();
  const db = pyQuery(DB);
  const handoffs = JSON.parse(execFileSync('python3', ['-c', `import sqlite3, sys; r = sqlite3.connect(sys.argv[1]).execute("SELECT value FROM settings WHERE key='handoffs'").fetchone(); print(r[0] if r else '[]')`, DB]).toString());
  const pass = [], fail = [];
  const check = (ok, name, detail) => (ok ? pass : fail).push(detail ? `${name}: ${detail}` : name);
  const lanesOfList = new Map();
  for (const e of igLog.filter((x) => x.kind === 'list')) { if (!lanesOfList.has(e.list)) lanesOfList.set(e.list, new Set()); lanesOfList.get(e.list).add(e.lane); }
  const rows = [];
  for (const s of seeds) {
    const key = s.handle.toLowerCase() + '/' + s.direction, L = db.lists.find((l) => l.seed.toLowerCase() + '/' + l.direction === key) || {};
    const srv = new Set((served[key] || {}).served || []), got = new Set(db.edges[key] || []);
    const same = srv.size === got.size && [...srv].every((x) => got.has(x));
    rows.push({ list: '@' + s.handle + ' ' + s.direction, size: s.size, edges: got.size, state: L.state, lanes: [...(lanesOfList.get(key) || [])].join('+') });
    check(same, 'edges == unique users served', `${key} ${got.size} vs ${srv.size}`);
    if (tDone != null) check(L.state === 'done' && got.size === s.size, 'list complete', `${key} ${L.state} ${got.size}/${s.size}`);
  }
  check(db.edge_rows === db.edge_distinct, 'no duplicate edge rows', `${db.edge_rows}/${db.edge_distinct}`);
  // no page fetched twice: every usable list answer is for a (list, cursor) nobody fetched before
  const seenPage = new Map(); let dupPages = 0;
  for (const e of fakeLog.log.filter((x) => x.kind === 'list' && x.users != null)) { const k = e.path + ' ' + e.max_id; if (seenPage.has(k)) dupPages++; seenPage.set(k, e.account); }
  check(!dupPages, 'no page fetched twice (across all lanes)', `${seenPage.size} pages, ${dupPages} repeats`);
  check(!counters.dupResponses, 'no page posted twice', `${counters.dupResponses} duplicate posts`);
  // lanes and accounts
  for (const [i, L] of lanes.entries()) {
    check(igLog.some((e) => e.lane === L.name && e.kind === 'list'), 'every lane worked lists', L.name);
    const a = acc.accounts.find((x) => x.ig_id === accts[i].ig_id);
    check(a && a.handle === accts[i].handle, 'server knows each lane\'s account', `${L.name} -> ${a ? a.handle + ' (' + a.lane_id + ')' : 'missing'}`);
    check(!(L.store.box || []).length, 'outbox empty', `${L.name} ${(L.store.box || []).length}`);
    // its own cooldowns: none of its requests inside them (checkRequest too), and the others kept going meanwhile
    for (const h of L.hits.filter((x) => x.bucket === 'list')) {
      const until = coolOf(h.post).list.until;
      const others = igLog.filter((e) => e.kind === 'list' && e.lane !== L.name && e.t > h.at && e.t < until).length;
      const own = igLog.filter((e) => e.kind === 'list' && e.lane === L.name && e.t > h.at && e.t < until).length;
      check(!own, 'no request during its own cooldown', `${L.name} ${own} in ${dhm(h.at)}–${dhm(until)}`);
      if (N > 1) check(others > 0, 'cooldown stays on its lane (others keep going)', `${L.name} hit at ${dhm(h.at)}: ${others} requests by other lanes meanwhile`);
    }
  }
  check(acc.accounts.length === N, 'one account row per lane', `${acc.accounts.length} rows`);
  if (N > 1) {
    const out = switches.filter((s) => s.from === 'lane2' && s.why === 'hold');
    check(out.length > 0, 'logged-out lane hands its list over', out.map((s) => `${s.list} ${s.from}->${s.to} at ${dhm(s.t)}`).join(', ') || 'no handoff');
    check(out.every((s) => s.maxId), 'handoff resumes from the saved cursor, not page 1', out.map((s) => `${s.list} max_id=${s.maxId}`).join(', '));
    check(handoffs.some((h) => h.why === 'login'), 'server recorded the handoff', `${handoffs.length} handoffs`);
  }
  check(!harnessErrors.length, 'no uncaught errors in the extensions', harnessErrors.slice(0, 3).join(' | '));
  check(!violations.length, 'no invariant violations', violations.slice(0, 3).map((v) => v.what + ' ' + JSON.stringify(v)).join('; '));
  check(t10k != null, 'reached 10k connections', t10k != null ? (t10k / HOUR).toFixed(2) + ' h' : 'not within ' + HOURS + ' h');

  const pad = (s, n) => String(s).padEnd(n), lpad = (s, n) => String(s).padStart(n);
  console.log(`\nFortunate Leads e2e — ${N} lane${N > 1 ? 's' : ''}, extension ${manifest.version}, up to ${HOURS} simulated hours`);
  console.log(pad('list', 30) + lpad('size', 6) + lpad('edges', 7) + '  ' + pad('state', 8) + 'lanes');
  for (const r of rows) console.log(pad(r.list, 30) + lpad(r.size, 6) + lpad(r.edges, 7) + '  ' + pad(r.state, 8) + r.lanes);
  console.log('');
  for (const L of lanes) {
    const reqs = igLog.filter((e) => e.lane === L.name && e.kind === 'list').length;
    console.log(pad(L.name + ' @' + L.handle, 22) + `${reqs} list requests, hits ${L.hits.map((h) => dhm(h.at) + ' ' + h.bucket).join(', ') || 'none'}, holds ${L.holds.map((h) => dhm(h.start) + ' ' + h.code + (h.end ? ' ' + Math.round((h.end - h.start) / MIN) + ' min' : ' open')).join(', ') || 'none'}`);
  }
  console.log('handoffs       ' + (switches.map((s) => `${dhm(s.t)} ${s.list} ${s.from}->${s.to} (${s.why}, max_id ${s.maxId ? 'kept' : 'null'})`).join('; ') || 'none'));
  console.log('result         10k connections ' + (t10k != null ? (t10k / HOUR).toFixed(2) + ' h' : '–') + ', all lists done ' + (tDone != null ? (tDone / HOUR).toFixed(2) + ' h' : '–') +
    `, ${db.edge_rows} connections, ${(((RealDate.now() - REAL_T0) / 1e3)).toFixed(1)} s real`);
  console.log(`\nchecks: ${pass.length} passed, ${fail.length} failed`);
  for (const f of fail) console.log('  FAIL ' + f);
  if (VERBOSE) for (const p of pass) console.log('  ok   ' + p);
  const ok = !fail.length;
  if (flag('json')) console.log('RESULT ' + JSON.stringify({ n: N, t10k, tDone, pages: seenPage.size, hits: lanes.reduce((a, L) => a + L.hits.length, 0), ok }));
  console.log(ok ? '\nPASS' : '\nFAIL');
  fs.writeFileSync(path.join(TMP, 'result.json'), JSON.stringify({ pass, fail, violations, switches, events: events.map((e) => ({ ...e, t: dhm(e.t) })) }, null, 1));
  cleanup();
  if (!flag('keep') && ok) fs.rmSync(TMP, { recursive: true, force: true });
  process.exit(ok ? 0 : 1);
}

const laneCounts = LANES ? String(LANES).split(',').map(Number).filter((n) => n >= 1 && n <= 8) : [];
(laneCounts.length > 1 ? lanesCompare(laneCounts) : laneCounts.length ? lanesMain(laneCounts[0]) : main())
  .catch((e) => { console.error(e); cleanup(); process.exit(2); });
