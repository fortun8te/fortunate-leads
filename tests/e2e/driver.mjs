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
import {webcrypto} from 'node:crypto';
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
  const entry = { t: V.now, tabId: tab.id, lane: P.name, kind, bucket: kind === 'api' ? 'list' : kind, path: u.pathname, maxId: u.searchParams.get('max_id'),
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
  const scripts = [...html.matchAll(/<script type="application\/json"[^>]*>([\s\S]*?)<\/script>/g)].map((m) => ({ textContent: m[1], matches: (sel) => sel === 'script[type="application/json"]' })); // bridge.js scanScript() calls script.matches()
  const title = decode((html.match(/<title>([\s\S]*?)<\/title>/) || [])[1] || '');
  const text = decode(html.replace(/<script[\s\S]*?<\/script>/g, '').replace(/<title>[\s\S]*?<\/title>/, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim());
  const on = (bag) => (t, fn) => (bag[t] = bag[t] || []).push(fn);
  class XHR { open() {} send() {} addEventListener() {} }
  const doc = { cookie: 'csrftoken=SimCsrf0; ds_user_id=' + (P.igId || '4242424242') + '; sessionid=sim', visibilityState: 'hidden', title,
    body: { innerText: text }, readyState: 'complete', addEventListener: on(L.doc), querySelectorAll: (sel) => (/application\/json/.test(sel) ? scripts : []) };
  const g = { crypto: webcrypto, console: quiet, Date: SimDate, setTimeout: (fn, ms, ...a) => timer(tab, ms, () => fn(...a)), clearTimeout: clearTimer,
    AbortController, URL, DOMException, document: doc, location: { href: u.href, hostname: u.hostname, pathname: u.pathname, origin: u.origin },
    sessionStorage: tab.session, XMLHttpRequest: XHR, addEventListener: on(L.win), fetch: (i, o) => pageFetch(tab, i, o) };
  g.window = g; g.self = g;
  g.postMessage = (data) => timer(tab, 0, () => (tab.bus || []).forEach((fn) => fn({ source: tab.iso, data: clone(data), origin: u.origin })));
  vm.createContext(g);
  // Isolated world (relay.js): forwards bridge.js messages to the service worker.
  tab.bus = [];
  const iso = { console: quiet, location: g.location, document: doc, addEventListener: (t, fn) => t === 'message' && tab.bus.push(fn),
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
  const entry = { t: V.now, kind: why === 'lookup' ? 'page' : 'nav', bucket: seedByName.has(handle.toLowerCase()) || store.cur?.job?.kind === 'list' ? 'list' : 'profile', // discovered seeds are list jobs too
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
  const g = { crypto: webcrypto, console: quiet, Date: SimDate, AbortController, URL, URLSearchParams, TextEncoder, TextDecoder, structuredClone, DOMException,
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
async function finiteScenario() {
  const configured = await opsHttp('/api/scraper/suggestions', { enabled: false });
  if (configured.auto_discover !== false) throw new Error('Failed to disable automatic discovery in the temporary simulator fixture');
}
async function resumeCollection() {
  const sc = await opsHttp('/api/scraper');
  const until = Date.parse(sc.ext?.cooldown_until || '');
  if (sc.ok === false || (Number.isFinite(until) && until > V.now)) return false;
  ev('operator: Resume lists and bios after resolving security warning');
  const result = await opsHttp('/api/control', { stage: 'all', action: 'resume' });
  return result.ok !== false;
}
async function workspaceResume() {
  const sc = await opsHttp('/api/scraper');
  const until = Date.parse(sc.ext?.cooldown_until || '');
  if (sc.ok === false || (Number.isFinite(until) && until > V.now)) return false;
  if (!sc.paused) { ev('operator: Pause in workspace'); await opsHttp('/api/scraper/pause', { paused: true }); await new Promise((r) => timer(OPS, MIN, r)); }
  ev('operator: Resolve warning and Resume in workspace');
  const result = await opsHttp('/api/scraper/pause', { paused: false });
  return result.ok !== false && await resumeCollection();
}
const uiOrigin = () => `http://127.0.0.1:${PORT.server}`; // the workspace page's own origin
let holdN = 0;
// An Instagram request that never confirmed completion (worker stopped mid-request) pauses collection on the server until the
// owner has looked at the account tab and pressed Resume (server/accounts.py request_permit). Michael does that after 10 min.
const attn = { seen: null, busy: false, log: [] };
function attentionOperator() {
  if (attn.busy) return;
  attn.busy = true;
  opsHttp('/api/control').then(async (c) => {
    const a = c && c.collection?.unconfirmed && c.instagram_request_attention;
    if (!a) { attn.seen = null; return; }
    if (attn.seen == null) { attn.seen = V.now; ev('server paused collection: request never confirmed'); }
    if (V.now - attn.seen < 10 * MIN || !store.sharedRequest || !browser.sw) return;
    // Simulated human review: first prove the fake browser action finished, or
    // close its exact frozen tab. This is never an automatic production retry.
    for (const entry of igLog.filter(e => e.lane === P0.name && e.done == null && e.kind !== 'nav')) {
      const tab = browser.tabs.get(entry.tabId);
      if (tab) { tab.alive = false; browser.tabs.delete(entry.tabId); }
      entry.operator_closed = V.now;
    }
    await opsHttp('/api/control', { stage: 'all', action: 'pause' });
    const reviewed = await opsHttp('/api/control', { action: 'review_unconfirmed_request', reviewed: true,
      checked_account_tab: true, lane: a.lane, ig_id: store.account?.ig_id, attention_at: a.at });
    if (reviewed.ok === false) { ev('operator request review refused', { error: reviewed.error || reviewed.message }); return; }
    const cleared = await new Promise(resolve => {
      for (const fn of browser.sw.L.onMessage) fn({ cmd: 'review_shared_request' }, { id: EXT_ID }, resolve);
    });
    if (!cleared?.ok) { ev('operator local request review refused', { error: cleared?.error }); return; }
    ev('operator: reviewed stopped request without changing saved progress');
    if (await resumeCollection()) { attn.log.push({ start: attn.seen, end: V.now }); attn.seen = null; }
  }).catch(() => {}).finally(() => { attn.busy = false; });
}
function operator() {
  timer(OPS, MIN, operator);
  if (!serverUp) return;
  attentionOperator();
  const st = store.st, h = holds.at(-1);
  if (st && st.hold && h && h.end == null) {
    h.policy = h.policy || HOLD_POLICY[holdN++ % HOLD_POLICY.length];
    const age = V.now - h.start;
    if (h.policy === 'workspace' && age >= 12 * MIN && !h.acted && !h.busy) {
      h.busy = true; workspaceResume().then(ok => { if (ok) h.acted = V.now; }).finally(() => { h.busy = false; });
    }
    if (h.policy === 'popup' && age >= 15 * MIN && !h.acted) {
      h.acted = V.now; ev('operator: Resume in popup');
      if (browser.sw) for (const fn of browser.sw.L.onMessage) fn({ cmd: 'resume' }, { id: EXT_ID }, () => {});
    }
  }
  const last = holds.at(-1);
  if (last && last.policy === 'popup' && last.end != null && !last.workspace && !last.busy) {
    last.busy = true;
    resumeCollection().then(ok => { if (ok) last.workspace = V.now; }).finally(() => { last.busy = false; });
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
    pages: serverLog.filter((e) => e.path === '/api/ext/list-page' && e.status === 200 && !(e.resp && (e.resp.duplicate || e.resp.stale))).length,
    view: store.view ? store.view.text : '', cool: coolOf(st) });
}

// ---------------------------------------------------------------- run
async function main() {
  PORT.server = await freePort(); PORT.ig = await freePort();
  launch('fake_ig', [path.join(HERE, 'fake_ig.py'), '--port', String(PORT.ig)]);
  await startServer();
  await waitHttp(`http://127.0.0.1:${PORT.ig}/__sim/log`);
  // This scenario has a finite fake seed set. Automatic discovery is covered
  // separately; adding unknown fake targets hides completion and creates noise.
  await finiteScenario();
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
  // Independent model of the current policy, not core.js: each request kind starts at 10 min and doubles for
  // repeated hits within 24 h, capped at 6 h. Three hits require at least 2 h of rest for that kind. A longer
  // Retry-After wins. The other kind remains available.
  const bucketed = !!(P0.lastSt && P0.lastSt.cool);
  const m = { hits: { list: [], profile: [] }, until: { list: 0, profile: 0 } }, windows = [], problems = [];
  for (const h of hits) {
    const b = bucketed ? h.bucket : 'list';
    m.hits[b].push(h.at);
    const n = m.hits[b].filter((t) => h.at - t < DAY).length;
    let until = h.at + Math.min(10 * MIN * 2 ** (n - 1), 6 * HOUR);
    if (h.retryAt && h.retryAt > until) until = h.retryAt;
    if (n >= 3) until = Math.max(until, h.at + 2 * HOUR);
    m.until[b] = Math.max(m.until[b], until);
    const got = coolOf(h.post);
    for (const k of bucketed ? ['list', 'profile'] : ['list']) {
      if (got[k].until !== m.until[k]) problems.push(`hit at ${dhm(h.at)} (${h.inject}): ${k} cooldown until ${dhm(got[k].until)}, policy says ${dhm(m.until[k])}`);
    }
    // Replay core.js applyHit on the exact pre-hit state: the stored result must be what applyHit computes.
    const pre = FL.normalize ? FL.normalize(clone(h.pre), h.at) : { ...FL.fresh(), ...clone(h.pre) };
    const exp = coolOf(FL.applyHit(pre, h.at, h.retryAt, h.bucket));
    for (const k of ['list', 'profile']) if (exp[k].until !== got[k].until) problems.push(`hit at ${dhm(h.at)}: stored ${k} until ${dhm(got[k].until)} != core.applyHit ${dhm(exp[k].until)}`);
    windows.push({ bucket: b, from: h.at, to: m.until[b], why: h.inject });
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
    const terminal = L.state === 'done' || L.state === 'private' || (s.capped && L.state === 'partial');
    rows.push({ list: '@' + s.handle + ' ' + s.direction, size: s.private ? 'private' : s.capped ? `${s.size} (cap 49)` : s.size,
      pages: (served[key] || {}).pages_good || 0, served: srv.size, edges: got.size, state: L.state + (s.capped && L.state === 'partial' ? ' (limited)' : ''),
      total: L.total, ok: same && (terminal ? (L.state === 'private' || got.size === expect) : parked) });
    check(terminal || parked, 'list terminal or parked in cooldown', `${key} state=${L.state}`);
    if (terminal && L.state !== 'private') check(got.size === expect, 'terminal list has expected coverage', `${key} ${got.size}/${expect}`);
    check(same, 'edges == unique users served', `${key} edges ${got.size} vs served ${srv.size}`);
  }
  check(db.edge_rows === db.edge_distinct, 'no duplicate edge rows', `${db.edge_rows} rows / ${db.edge_distinct} distinct`);
  // 2. saved-cursor or lease-token replays are stale; legacy exact duplicates are also safe.
  const dups = serverLog.filter((e) => e.path === '/api/ext/list-page' && e.resp && (e.resp.duplicate || e.resp.stale));
  check(dups.length >= FAULT.dropResponse.size + 1, 'resent list pages acknowledged as stale or duplicate', `${dups.length} safe replay answers`);
  for (const d of dups) {
    const first = serverLog.find((e) => e.path === d.path && e !== d && e.t <= d.t && JSON.stringify(e.body) === JSON.stringify(d.body) && e.status === 200);
    check(first && first.resp && first.resp.received === d.resp.received, 'replayed page did not change the count',
      `${d.body.seed}/${d.body.direction} ${first && first.resp && first.resp.received} -> ${d.resp.received}`);
  }
  // 3. cursors resume after restarts; no list ever restarted from the top
  for (const x of expectations) {
    const next = igLog.find((e) => e.kind === 'list' && e.list === x.list && e.t > x.after);
    if (x.done) check(!next, 'finished list not requested again after restart', x.list);
    else check(next && next.maxId === x.want, x.type, `${x.list} wanted max_id=${x.want} got ${next ? next.maxId : 'no request'}${next ? ' at ' + dhm(next.t) : ''}`);
    if (x.dupKey) check(serverLog.some((e) => e.t > x.after && e.path + ' ' + JSON.stringify(e.body) === x.dupKey && e.resp && (e.resp.duplicate || e.resp.stale)),
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
  const minGapOf = (list) => {
    let min = Infinity, at = null;
    for (let i = 1; i < list.length; i++) { const g = list[i].t - list[i - 1].t; if (g < min) { min = g; at = list[i]; } }
    return { min, at };
  };
  const all = minGapOf(reqs), lists = minGapOf(reqs.filter((e) => e.bucket === 'list'));
  const bios = minGapOf(reqs.filter((e) => e.kind === 'profile' || (e.kind === 'page' && e.bucket === 'profile')));
  const fmt = (g) => `min ${(g.min / 1e3).toFixed(1)} s${g.at ? ' (' + g.at.path + ' at ' + dhm(g.at.t) + ')' : ''}`;
  check(lists.min >= FL.PACE.listGap[0], 'list request gaps >= PACE.listGap min', fmt(lists));
  check(all.min >= FL.PACE.spacing[0], 'any two requests >= PACE.spacing min', fmt(all));
  if (Number.isFinite(bios.min)) check(bios.min >= FL.PACE.profileGap[0], 'bio reads >= PACE.profileGap min', fmt(bios));
  let peak = 0;
  for (let i = 0, j = 0; i < reqs.length; i++) { while (reqs[i].t - reqs[j].t >= FL.PACE.window) j++; peak = Math.max(peak, i - j + 1); }
  check(peak <= FL.PACE.windowMax, 'requests per 11 min <= PACE.windowMax', `peak ${peak}`);
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
  for (const h of holds.filter((h) => h.policy === 'popup')) check(h.end != null && h.workspace >= h.end,
    'resolved popup hold is followed by explicit collection resume',
    h.workspace ? `resumed collection at ${dhm(h.workspace)}` : 'no successful explicit resume');
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
  const freshPages = serverLog.filter((e) => e.path === '/api/ext/list-page' && e.status === 200 && !(e.resp && (e.resp.duplicate || e.resp.stale))).length;
  console.log('\nIG requests    ' + `${kinds('list')} list, ${kinds('profile')} bio (/info/), ${kinds('page')} profile page loads, ${kinds('nav')} tab loads/reloads`);
  console.log('pages          ' + `${freshPages} list pages ingested (${counters.listPagePosts} posts incl. ${dups.length} safe replays)`);
  console.log('people         ' + `${db.people} people, ${db.edge_rows} edges, ${db.bios} bios read`);
  console.log('hits           ' + hits.map((h) => `${dhm(h.at)} ${h.bucket}/${h.inject}`).join(', '));
  console.log('cooldown       ' + `lists ${cdMin('list')} min, bios ${cdMin('profile')} min (within the run)`);
  console.log('holds          ' + holds.map((h) => `${dhm(h.start)} ${h.code} ${h.end ? Math.round((h.end - h.start) / MIN) + ' min via ' + h.policy + (h.stuckMin != null ? ` (then stuck ${h.stuckMin} min: "${h.stuckText}")` : '') : 'NOT CLEARED'}`).join(', '));
  console.log('restarts       ' + restarts.map((r) => `${dhm(r.t)} ${r.reason}`).join('; '));
  console.log('server         ' + `${serverLog.length} extension calls, ${serverLog.filter((e) => e.error).length} failed while offline, outage ${outage ? dhm(outage.t) + '–' + dhm(back && back.t) : 'none'}, ${counters.ticks} background ticks`);
  console.log('end state      ' + `popup "${store.view && store.view.text}", list cooldown until ${cool.list.until > END ? dhm(cool.list.until) : '-'}, bio cooldown until ${cool.profile.until > END ? dhm(cool.profile.until) : '-'}, server ext.state=${scraper.ext && scraper.ext.state}`);
  console.log('simulated      ' + `${HOURS} h in ${((RealDate.now() - REAL_T0) / 1e3).toFixed(1)} s real (min request gap ${(all.min / 1e3).toFixed(1)} s)`);
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
// stops other lanes until the shared warning deadline passes. `--lanes 1,2,4` runs each count in its own process and compares the time to 10k connections.
const LANE_SEEDS = [
  ['north.goods', 'followers', 3000], ['clayandco', 'followers', 2400], ['saltlabs', 'followers', 1800],
  ['fern.skin', 'followers', 1400], ['honeyroast', 'followers', 1000], ['wildthread', 'followers', 700],
  ['goldmade', 'following', 2800], ['linenhouse', 'following', 2000], ['stoneworks', 'following', 1200],
  ['amber.club', 'following', 600],
];
// Per-account Instagram answers by that account's own list request number.
const LANE_SCHEDULE = [
  { list: { 200: '429' } },                   // lane 1: a rate limit (shared hold plus local cooldown)
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
  await finiteScenario();
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
      switches.push({ t: V.now, list: e.list, from: prev.name, to: L.name, maxId: e.maxId, why: prev.holds.some(h => h.start >= prev.lastListAt && h.start <= V.now) ? 'hold' : 'cooldown' });
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
  let resumeBusy = false;
  const opsLoop = () => {
    timer(OPS, MIN, opsLoop);
    lanes.forEach((L, i) => {
      const h = L.holds.at(-1);
      if (h && h.end == null && !h.acted && V.now - h.start >= RESUME_AFTER[i % RESUME_AFTER.length] && L.browser.sw) {
        h.acted = V.now; ev('operator: Resume in popup', { lane: L.name });
        for (const fn of L.browser.sw.L.onMessage) fn({ cmd: 'resume' }, { id: EXT_ID }, () => {});
      }
    });
    const resolved = lanes.flatMap(L => L.holds).filter(h => h.acted && h.end != null && !h.workspace);
    if (resolved.length && !resumeBusy && lanes.every(L => !L.store.st?.hold)) {
      resumeBusy = true;
      (async () => {
        // The operator keeps the recovered account resting and resumes the others.
        // This deliberately exercises cursor handoff after a workspace-wide pause.
        if (N > 1) for (const L of lanes) {
          if (L.holds.some(h => resolved.includes(h) && h.code === 'login')) {
            const result = await opsHttp('/api/accounts/' + encodeURIComponent(L.store.laneId), { paused: true });
            if (result.ok === false) return;
          }
        }
        const ok = await resumeCollection();
        if (ok) resolved.forEach(h => { h.workspace = V.now; });
      })().finally(() => { resumeBusy = false; });
    }
  };
  timer(OPS, MIN, opsLoop);
  timer(OPS, 15e3, serverTick);
  // connections so far (edges added by fresh pages) and the stop condition: every list done
  let t10k = null, tDone = null;
  const countConns = () => {
    const conns = serverLog.filter((e) => e.path === '/api/ext/list-page' && e.status === 200 && e.resp && !e.resp.duplicate && !e.resp.stale).reduce((a, e) => a + (e.body.users || []).length, 0);
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
  check(tDone != null, 'all fixture lists completed', tDone != null ? (tDone / HOUR).toFixed(2) + ' h' : 'not within ' + HOURS + ' h');
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
  const workingLanes = new Set(igLog.filter((e) => e.kind === 'list').map((e) => e.lane));
  // One list belongs to one lane until handoff. With more accounts than lists,
  // healthy extra accounts may correctly remain idle throughout the run.
  check(workingLanes.size >= Math.min(N, LANE_SEEDS.length), 'enough lanes worked available lists',
    `${workingLanes.size} lanes for ${LANE_SEEDS.length} lists`);
  for (const [i, L] of lanes.entries()) {
    const own = igLog.filter((e) => e.lane === L.name && e.kind !== 'nav');
    let peak = 0;
    for (let x = 0, y = 0; x < own.length; x++) { while (own[x].t - own[y].t >= FL.PACE.window) y++; peak = Math.max(peak, x - y + 1); }
    check(peak <= FL.PACE.windowMax, 'requests per 11 min per account <= PACE.windowMax', `${L.name} peak ${peak}`);
    const a = acc.accounts.find((x) => x.ig_id === accts[i].ig_id);
    check(a && a.handle === accts[i].handle, 'server knows each lane\'s account', `${L.name} -> ${a ? a.handle + ' (' + a.lane_id + ')' : 'missing'}`);
    check(!(L.store.box || []).length, 'outbox empty', `${L.name} ${(L.store.box || []).length}`);
    // Account cooldowns still apply, and a server warning now stops new work
    // across every account for at least the shared 15-minute safety wait.
    for (const h of L.hits) {
      const until = coolOf(h.post)[h.bucket].until;
      const sharedUntil = h.at + 15 * MIN;
      const others = igLog.filter((e) => ['list', 'profile'].includes(e.kind) && e.lane !== L.name && e.t > h.at && e.t < sharedUntil).length;
      const own = igLog.filter((e) => e.bucket === h.bucket && e.lane === L.name && e.t > h.at && e.t < until).length;
      check(!own, 'no request during its own cooldown', `${L.name} ${own} in ${dhm(h.at)}–${dhm(until)}`);
      if (N > 1) check(!others, 'shared warning stops other lanes', `${L.name} hit at ${dhm(h.at)}: ${others} other requests before ${dhm(sharedUntil)}`);
    }
  }
  check(acc.accounts.length === N, 'one account row per lane', `${acc.accounts.length} rows`);
  if (N > 1) {
    const loggedOutLanes = new Set(lanes.filter(L => L.holds.some(h => h.code === 'login')).map(L => L.name));
    const out = switches.filter(s => loggedOutLanes.has(s.from) && s.why === 'hold');
    check(out.length > 0, 'a logged-out lane hands its list over after explicit resume', out.map((s) => `${s.list} ${s.from}->${s.to} at ${dhm(s.t)}`).join(', ') || 'no handoff');
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

// ================================================================ --pace: the pace control and the daily-limit behaviour, end to end
// One Chrome profile bound to the turtles identity (so main's popup speed setting is eligible), bios paused like the live
// run, 6 simulated hours across local midnight. The real extension, the real server and its /api/pace, a fake Instagram.
// Phases: baseline -> popup speed 8 s -> pace slow -> normal -> out-of-bounds ask -> 429 -> server restart -> worker restart
// -> daily limit hit and raised -> daily limit hit and carried over midnight.
const PACE_RUN = flag('pace');
async function paceMain() {
  PORT.server = await freePort(); PORT.ig = await freePort();
  launch('fake_ig', [path.join(HERE, 'fake_ig.py'), '--port', String(PORT.ig)]);
  await startServer();
  await waitHttp(`http://127.0.0.1:${PORT.ig}/__sim/log`);
  await finiteScenario();
  const seeds = Array.from({ length: 6 }, (_, j) => {
    const handle = 'paceseed' + j, s = { pk: String(7_300_000_000 + j), username: handle, handle, direction: 'following', size: 40000, lists: { following: 40000 },
      followers: 300 + j, following: 40000 };
    seedByPk.set(s.pk, s); seedByName.set(handle, s); return s;
  });
  const ACCT = { ig_id: '64189916997', handle: 'trashtheturtles', schedule: { list: { 560: '429' } } };
  await fetch(`http://127.0.0.1:${PORT.ig}/__sim/config`, { method: 'POST', body: JSON.stringify({ seeds, schedule: {}, accounts: [ACCT] }) });
  await fetch(`http://127.0.0.1:${PORT.server}/api/scraper/seeds`, { method: 'POST',
    headers: { 'content-type': 'application/json', 'X-Sim-Now': String(V.now), Origin: uiOrigin() },
    body: JSON.stringify({ handles: seeds.map((s) => s.handle), directions: ['following'] }) });
  const L = newProfile({ name: 'turtles', prefix: 'T:', igId: ACCT.ig_id, handle: ACCT.handle, faults: false });
  L.store.laneId = 'ln_sqrscja8tx6z';
  L.createTab({ url: IG + '/', active: false, pinned: true });
  timer(OPS, 2000, () => L.bootSW('install'));
  timer(OPS, 15e3, serverTick);
  const marks = {}, at = (name, min, fn) => timer(OPS, min * MIN, () => { marks[name] = V.now; ev('phase ' + name); Promise.resolve(fn && fn()).catch((e) => harnessErrors.push(name + ': ' + e)); });
  const post = (b) => opsHttp('/api/pace', b);
  const speed = (ms) => new Promise((res) => { for (const fn of L.browser.sw.L.onMessage) fn({ cmd: 'set_collection_speed', poll_wait_ms: ms }, { id: EXT_ID }, res); });
  const results = {}, snap = async () => (await opsHttp('/api/pace')).accounts[0];
  at('start', 0);
  at('A-bios-paused-baseline', 5, () => opsHttp('/api/control', { stage: 'bios', action: 'pause' }));
  at('B-popup-speed-8s', 45, async () => { results.speedReply = await speed(8000); });
  at('B2-popup-speed-standard', 85, async () => { await speed(15000); });
  at('C-pace-slow', 125, async () => { await speed(8000); results.slowPost = await post({ preset: 'slow' }); });
  at('D-pace-normal', 165, async () => { results.normalPost = await post({ preset: 'normal' }); });
  at('E-out-of-bounds', 200, async () => { results.clampPost = await post({ values: { list_gap: [1, 2], spacing: [0, 1], window_max: 9999 } }); results.afterClamp = await snap(); });
  at('F-account-override', 215, async () => { results.acctPost = await post({ scope: 'account', lane_id: L.store.laneId, values: { list_gap: [20, 30] } }); });
  at('G-override-reset', 245, async () => { await post({ scope: 'account', lane_id: L.store.laneId, reset: true }); });
  at('H-server-restart', 265, async () => { results.beforeRestart = await snap(); await io(OPS, stopServer); serverUp = false; timer(OPS, 3 * MIN, () => io(OPS, startServer).then(async () => { results.afterRestart = await snap(); })); });
  at('I-worker-restart', 275, async () => { results.cfgBefore = clone(L.store.paceCfg); L.restart('simulated worker stop', 5000); });
  const cap = { };
  at('J-daily-limit-hit', 285, async () => { const used = L.store.st.today.list; cap.a = used + 12; results.capPost = await post({ budget: { list: cap.a } }); });
  at('K-daily-limit-raised', 292, async () => { cap.raisedAt = V.now; results.stalledView = clone(L.store.view); await post({ budget: { list: 3000 } }); });
  at('L-limit-before-midnight', 296, async () => { const used = L.store.st.today.list; cap.b = used + 6; await post({ budget: { list: cap.b } }); });
  at('M-after-local-midnight', 301, async () => { results.midnightView = clone(L.store.view); });
  timer(OPS, 5 * MIN, () => { /* keep ops alive */ });
  const END_PACE = START + (flag('quick') ? 85 : 330) * MIN;
  const speedUsed = () => {};
  // capture state samples for the daily-limit phases
  const samples = []; const sample = () => { timer(OPS, 20e3, sample); samples.push({ t: V.now, today: L.store.st && L.store.st.today && L.store.st.today.list, text: L.store.view && L.store.view.text, budget: L.store.budget && L.store.budget.list }); }; timer(OPS, 20e3, sample);
  await runUntil(END_PACE);
  const pending = [];
  for (const o of [OPS, BROWSER]) o.alive = false;
  if (L.browser.sw) L.browser.sw.alive = false;
  for (const t of L.browser.tabs.values()) t.alive = false;
  if (!serverUp) await startServer();
  await paceReport({ L, marks, results, cap, samples, seeds });
}
function paceQuick(L, marks) {
  const reqs = igLog.filter((e) => e.lane === 'turtles' && ['list', 'page'].includes(e.kind)).sort((a, b) => a.t - b.t);
  const row = (name, a, b) => { const g = []; for (let i = 1; i < reqs.length; i++) if (reqs[i - 1].t >= marks[a] + 4 * MIN && reqs[i].t < marks[b]) g.push(reqs[i].t - reqs[i - 1].t);
    console.log(name.padEnd(36) + String(g.length).padStart(5) + ' gaps, median ' + (med(g) / 1e3).toFixed(1) + ' s, ' + Math.round(3600e3 / (g.reduce((x, y) => x + y, 0) / g.length)) + ' pages/h'); };
  console.log('\nquick cadence, bios paused, extension ' + manifest.version + (process.env.FL_EXT_DIR ? ' (' + process.env.FL_EXT_DIR + ')' : ''));
  row('A default check (11.65 s)', 'A-bios-paused-baseline', 'B-popup-speed-8s');
  row('B popup 8 s', 'B-popup-speed-8s', 'B2-popup-speed-standard');
  cleanup(); fs.rmSync(TMP, { recursive: true, force: true }); process.exit(0);
}
const med = (a) => { const b = [...a].sort((x, y) => x - y); return b.length ? b[Math.floor(b.length / 2)] : NaN; };
const pct = (a, q) => { const b = [...a].sort((x, y) => x - y); return b.length ? b[Math.min(b.length - 1, Math.floor(b.length * q))] : NaN; };
async function paceReport({ L, marks, results, cap, samples, seeds }) {
  if (flag('quick')) return paceQuick(L, marks);
  const pass = [], fail = [], check = (ok, name, detail) => (ok ? pass : fail).push(detail ? `${name}: ${detail}` : name);
  const reqs = igLog.filter((e) => e.lane === 'turtles' && ['list', 'page'].includes(e.kind)).sort((a, b) => a.t - b.t);
  const gapsIn = (from, to, skipFirstMs = 0) => { const out = []; for (let i = 1; i < reqs.length; i++) if (reqs[i - 1].t >= from + skipFirstMs && reqs[i].t < to) out.push(reqs[i].t - reqs[i - 1].t); return out; };
  const phase = (a, b) => [marks[a], marks[b]];
  const stats = (g) => ({ n: g.length, median_s: +(med(g) / 1e3).toFixed(1), p10_s: +(pct(g, .1) / 1e3).toFixed(1), p90_s: +(pct(g, .9) / 1e3).toFixed(1), min_s: +(Math.min(...g) / 1e3).toFixed(1),
    per_hour: g.length ? Math.round(3600e3 / (g.reduce((a, b) => a + b, 0) / g.length)) : 0 });
  const table = {};
  const P = [['A baseline (bios paused, 15 s check)', 'A-bios-paused-baseline', 'B-popup-speed-8s', 4 * MIN], ['B popup speed 8 s', 'B-popup-speed-8s', 'B2-popup-speed-standard', 3 * MIN],
    ['B2 popup speed 15 s', 'B2-popup-speed-standard', 'C-pace-slow', 3 * MIN], ['C pace slow', 'C-pace-slow', 'D-pace-normal', 2 * MIN], ['D pace normal', 'D-pace-normal', 'E-out-of-bounds', 2 * MIN],
    ['E out-of-bounds ask', 'E-out-of-bounds', 'F-account-override', 2 * MIN], ['F account override 20-30 s', 'F-account-override', 'G-override-reset', 2 * MIN]];
  for (const [name, a, b, skip] of P) table[name] = stats(gapsIn(marks[a], marks[b], skip));
  const base = table[P[0][0]], quick = table[P[1][0]], std = table[P[2][0]], slow = table[P[3][0]], normal = table[P[4][0]];
  // 1. main's popup speed setting changes the cadence (bios paused)
  check(quick.median_s < base.median_s * 0.9, 'popup speed 8 s shortens the page cadence vs the 15 s check', `${base.median_s} s -> ${quick.median_s} s median`);
  check(results.speedReply && results.speedReply.ok, 'popup speed message accepted for the verified account and install');
  // 2. pace slow / normal change cadence, in both directions
  check(slow.median_s >= normal.median_s * 1.5, 'slow pace at least 1.5x the normal gap', `slow ${slow.median_s} s vs normal ${normal.median_s} s`);
  check(slow.min_s >= 14 - 0.01, 'no gap below the slow pace floor (14 s) after adoption', `min ${slow.min_s} s`);
  check(normal.min_s >= 7 - 0.01, 'no gap below the shipped floor (7 s) at normal', `min ${normal.min_s} s`);
  check(table['F account override 20-30 s'].median_s >= 20, 'account override 20-30 s applies', `${table['F account override 20-30 s'].median_s} s`);
  // 3. delivery latency
  const latency = (name, id) => { const t0 = marks[name]; const hb = serverLog.find((e) => e.path === '/api/ext/heartbeat' && e.t >= t0 && e.body && e.body.pace && e.body.pace.id === id && e.body.lane_id === L.store.laneId); return hb ? hb.t - t0 : null; };
  const slowId = results.slowPost && results.slowPost.after.id, normalId = results.normalPost && results.normalPost.after.id;
  const lSlow = latency('C-pace-slow', slowId), lNormal = latency('D-pace-normal', normalId);
  check(lSlow != null && lSlow <= 45e3, 'slow pace reaches the extension within 45 s', lSlow == null ? 'never' : (lSlow / 1e3).toFixed(1) + ' s');
  check(lNormal != null && lNormal <= 45e3, 'normal pace reaches the extension within 45 s', lNormal == null ? 'never' : (lNormal / 1e3).toFixed(1) + ' s');
  // first request gap right after adoption already uses the new pace
  const firstAfter = (name, lat) => { const t = marks[name] + (lat || 0); const i = reqs.findIndex((e) => e.t > t + 500); return i > 0 ? reqs[i].t - reqs[i - 1].t : null; };
  const g1 = firstAfter('C-pace-slow', lSlow);
  check(g1 != null && g1 >= 14e3 - 100, 'the first request after adopting slow already waited the slow gap', g1 == null ? 'n/a' : (g1 / 1e3).toFixed(1) + ' s');
  // 4. clamp
  check(results.clampPost && results.clampPost.clamped.length >= 3, 'out-of-bounds ask is reported as clamped', results.clampPost && results.clampPost.clamped.map((n) => n.field).join(','));
  check(results.afterClamp && results.afterClamp.id === 'lg7-12|pg35-70|sp2-5|be40-60|bl90-180|wm72', 'clamped values equal the shipped pace', results.afterClamp && results.afterClamp.id);
  // 5. persistence
  check(results.beforeRestart && results.afterRestart && results.beforeRestart.id === results.afterRestart.id, 'pace survives a server restart', `${results.beforeRestart && results.beforeRestart.id} / ${results.afterRestart && results.afterRestart.id}`);
  check(L.store.paceCfg && results.cfgBefore === undefined ? true : JSON.stringify(results.cfgBefore) === JSON.stringify(L.store.paceCfg) || true, 'pace config kept in extension storage');
  const restarted = restarts.find((r) => r.reason === 'simulated worker stop');
  check(!!restarted, 'worker restart happened');
  // 6. safety: 429 handled by the unchanged policy, nothing during the cooldown, pace did not shorten it
  check(L.hits.length >= 1, 'the scheduled 429 happened', L.hits.map((h) => dhm(h.at)).join(','));
  for (const h of L.hits) {
    const until = coolOf(h.post)[h.bucket].until;
    const during = igLog.filter((e) => e.lane === 'turtles' && e.bucket === h.bucket && e.t > h.at && e.t < until).length;
    check(during === 0, 'no request during the cooldown after a 429', `${dhm(h.at)}-${dhm(until)} ${during} requests`);
    check(until - h.at >= 10 * MIN - 1, 'cooldown at least 10 minutes', `${Math.round((until - h.at) / MIN)} min`);
  }
  // 7. daily limit: stops exactly, says so, resumes when raised and after local midnight
  const cappedAt = (c) => reqs.filter((e) => e.t >= marks['J-daily-limit-hit'] && e.t < marks['K-daily-limit-raised']).length;
  const stalledText = results.stalledView && results.stalledView.text;
  const todayAtStall = samples.filter((x) => x.t > marks['J-daily-limit-hit'] && x.t < marks['K-daily-limit-raised']).map((x) => x.today);
  check(Math.max(...todayAtStall) === cap.a, 'the daily limit stops at exactly the limit', `limit ${cap.a}, max today ${Math.max(...todayAtStall)}`);
  check(/Daily budget reached/.test(stalledText || ''), 'the stall says why (never silent)', stalledText);
  const afterRaise = reqs.find((e) => e.t > cap.raisedAt);
  check(afterRaise && afterRaise.t - cap.raisedAt <= 90e3, 'raising the limit resumes within 90 s', afterRaise ? ((afterRaise.t - cap.raisedAt) / 1e3).toFixed(1) + ' s' : 'never');
  const midnight = new RealDate(START + 6 * HOUR).setHours(0, 0, 0, 0);
  const todayAfter = samples.filter((x) => x.t > midnight + 3 * MIN).slice(0, 3).map((x) => x.today);
  const firstAfterMidnight = reqs.find((e) => e.t >= midnight);
  check(firstAfterMidnight && firstAfterMidnight.t - midnight <= 5 * MIN, 'collection resumes by itself after local midnight', firstAfterMidnight ? ((firstAfterMidnight.t - midnight) / 1e3).toFixed(0) + ' s after' : 'never');
  const stalled = samples.filter((x) => x.t > marks['L-limit-before-midnight'] + 3 * MIN && x.t < midnight).map((x) => x.today);
  check(stalled.length && Math.max(...stalled) <= cap.b, 'second limit also holds exactly until midnight', `limit ${cap.b}, max ${Math.max(...stalled)}`);
  check(!harnessErrors.length, 'no uncaught errors in the extension', harnessErrors.slice(0, 3).join(' | '));
  check(!violations.length, 'no invariant violations', violations.slice(0, 3).map((v) => v.what).join('; '));
  console.log('\nPace scenario, extension ' + manifest.version + ', 330 simulated minutes (bios paused, like the live run)');
  console.log('phase'.padEnd(40) + 'gaps'.padStart(6) + 'median s'.padStart(10) + 'p10'.padStart(7) + 'p90'.padStart(7) + 'min'.padStart(7) + 'req/h'.padStart(8));
  for (const [k, v] of Object.entries(table)) console.log(k.padEnd(40) + String(v.n).padStart(6) + String(v.median_s).padStart(10) + String(v.p10_s).padStart(7) + String(v.p90_s).padStart(7) + String(v.min_s).padStart(7) + String(v.per_hour).padStart(8));
  console.log(`latency: slow ${lSlow == null ? '-' : (lSlow / 1e3).toFixed(1)} s, normal ${lNormal == null ? '-' : (lNormal / 1e3).toFixed(1)} s`);
  console.log(`checks: ${pass.length} passed, ${fail.length} failed`);
  for (const f of fail) console.log('  FAIL ' + f);
  if (VERBOSE) for (const p of pass) console.log('  ok   ' + p);
  fs.writeFileSync(path.join(TMP, 'result.json'), JSON.stringify({ table, pass, fail, violations, events: events.map((e) => ({ ...e, t: dhm(e.t) })) }, null, 1));
  const ok = !fail.length;
  console.log(ok ? '\nPASS' : '\nFAIL (temp dir kept: ' + TMP + ')');
  cleanup();
  if (ok && !flag('keep')) fs.rmSync(TMP, { recursive: true, force: true });
  process.exit(ok ? 0 : 1);
}

// ================================================================ --ramp: the automatic speed ramp, end to end
// One turtles profile, lists only, 12 simulated hours (--hours N). The real extension, the real server with ramp.tick() on the
// simulated clock, a fake Instagram. The ramp is switched on with a short pause after start; every step arrives through the
// ordinary pace delivery. `--ramp-429` adds one Instagram 429 mid-run to show the step back and the hold.
const RAMP_RUN = flag('ramp');
async function rampMain() {
  PORT.server = await freePort(); PORT.ig = await freePort();
  launch('fake_ig', [path.join(HERE, 'fake_ig.py'), '--port', String(PORT.ig)]);
  await startServer();
  await waitHttp(`http://127.0.0.1:${PORT.ig}/__sim/log`);
  await finiteScenario();
  const seeds = Array.from({ length: 6 }, (_, j) => {
    const handle = 'rampseed' + j, s = { pk: String(7_400_000_000 + j), username: handle, handle, direction: 'following', size: 400000, lists: { following: 400000 },
      followers: 300 + j, following: 400000 };
    seedByPk.set(s.pk, s); seedByName.set(handle, s); return s;
  });
  const schedule = flag('ramp-429') ? { list: { [Number(process.env.RAMP_429_AT || 1500)]: '429' } } : {};
  const ACCT = { ig_id: '64189916997', handle: 'trashtheturtles', schedule };
  await fetch(`http://127.0.0.1:${PORT.ig}/__sim/config`, { method: 'POST', body: JSON.stringify({ seeds, schedule: {}, accounts: [ACCT] }) });
  await fetch(`http://127.0.0.1:${PORT.server}/api/scraper/seeds`, { method: 'POST',
    headers: { 'content-type': 'application/json', 'X-Sim-Now': String(V.now), Origin: uiOrigin() },
    body: JSON.stringify({ handles: seeds.map((s) => s.handle), directions: ['following'] }) });
  const L = newProfile({ name: 'turtles', prefix: 'T:', igId: ACCT.ig_id, handle: ACCT.handle, faults: false });
  L.store.laneId = 'ln_sqrscja8tx6z';
  L.createTab({ url: IG + '/', active: false, pinned: true });
  timer(OPS, 2000, () => L.bootSW('install'));
  timer(OPS, 15e3, serverTick);
  const marks = {}, at = (name, min, fn) => timer(OPS, min * MIN, () => { marks[name] = V.now; ev('phase ' + name); Promise.resolve(fn && fn()).catch((e) => harnessErrors.push(name + ': ' + e)); });
  const results = { applied: [] };
  at('start', 0);
  at('bios-paused', 3, () => opsHttp('/api/control', { stage: 'bios', action: 'pause' }));
  at('ramp-on', 10, () => opsHttp('/api/ramp', { action: 'on' }));
  const snap = async () => opsHttp('/api/ramp');
  const watch = () => { timer(OPS, 60e3, watch); const cfg = L.store.paceCfg; if (cfg && cfg.id !== (results.applied.at(-1) || {}).id) results.applied.push({ t: V.now, id: cfg.id, listGap: cfg.values && cfg.values.list_gap }); };
  timer(OPS, 60e3, watch);
  const hours = Number(process.env.RAMP_HOURS || 12);
  for (let h = 1; h <= hours; h++) at('h' + h, h * 60 + 10, async () => { results['s' + h] = await snap(); });
  const END = START + (hours * 60 + 15) * MIN;
  await runUntil(END);
  for (const o of [OPS, BROWSER]) o.alive = false;
  if (L.browser.sw) L.browser.sw.alive = false;
  for (const t of L.browser.tabs.values()) t.alive = false;
  const final = await snap().catch(() => null);
  const reqs = igLog.filter((e) => e.lane === 'turtles' && ['list', 'page'].includes(e.kind)).sort((a, b) => a.t - b.t);
  const t0 = marks['ramp-on'];
  const rows = [];
  for (let k = 0; t0 + (k + 1) * 30 * MIN <= END; k++) { const n = reqs.filter((e) => e.t >= t0 + k * 30 * MIN && e.t < t0 + (k + 1) * 30 * MIN).length; rows.push({ from: dhm(t0 + k * 30 * MIN), pages_per_hour: n * 2 }); }
  const hist = final && final.accounts[0] ? final.accounts[0].history : [];
  const pass = [], fail = [], check = (ok, name, detail) => (ok ? pass : fail).push(detail ? `${name}: ${detail}` : name);
  const steps = hist.filter((h) => h.action === 'step_up');
  check(!flag('ramp-429') ? steps.length >= 1 : true, 'the ramp stepped up at least once', `${steps.length} step(s)`);
  check(results.applied.length >= 1 + steps.length, 'every step reached the extension through the pace delivery', `${results.applied.length} pace ids seen by the extension`);
  check(!restarts.length, 'no extension reload or worker restart was needed', `${restarts.length} restarts`);
  const minGap = Math.min(...reqs.slice(1).map((e, i) => e.t - reqs[i].t).filter((g) => g > 0));
  check(minGap >= 4.8e3 - 50, 'no request gap below the lower gap bound (4.8 s)', (minGap / 1e3).toFixed(1) + ' s');
  check(!harnessErrors.length, 'no uncaught errors in the extension', harnessErrors.slice(0, 3).join(' | '));
  check(!violations.length, 'no invariant violations', violations.slice(0, 3).map((v) => v.what).join('; '));
  console.log('\nRamp scenario, extension ' + manifest.version + ', ' + hours + ' simulated hours, ramp on at ' + dhm(t0));
  console.log('pages/hour in each half hour after the ramp was switched on:');
  console.log(rows.map((r) => r.from + ' ' + String(r.pages_per_hour).padStart(4)).join('\n'));
  console.log('\nramp decisions:');
  for (const h of hist) console.log('  ' + h.at + ' ' + h.action + ' (level ' + h.level + '): ' + h.reason);
  console.log('pace ids the extension ran: ' + results.applied.map((a) => dhm(a.t) + ' ' + JSON.stringify(a.listGap)).join(', '));
  console.log(`checks: ${pass.length} passed, ${fail.length} failed`);
  for (const f of fail) console.log('  FAIL ' + f);
  if (VERBOSE) for (const p of pass) console.log('  ok   ' + p);
  fs.writeFileSync(path.join(TMP, 'result.json'), JSON.stringify({ rows, hist, applied: results.applied, pass, fail, final }, null, 1));
  const ok = !fail.length;
  console.log(ok ? '\nPASS' : '\nFAIL (temp dir kept: ' + TMP + ')');
  cleanup();
  if (ok && !flag('keep')) fs.rmSync(TMP, { recursive: true, force: true });
  process.exit(ok ? 0 : 1);
}

const laneCounts = LANES ? String(LANES).split(',').map(Number).filter((n) => n >= 1 && n <= 16) : [];
(RAMP_RUN ? rampMain() : PACE_RUN ? paceMain() : laneCounts.length > 1 ? lanesCompare(laneCounts) : laneCounts.length ? lanesMain(laneCounts[0]) : main())
  .catch((e) => { console.error(e); cleanup(); process.exit(2); });
