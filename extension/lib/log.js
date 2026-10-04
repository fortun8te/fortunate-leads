// Structured events for the server log (docs/LOGGING.md). Shared by the service worker (importScripts) and node tests.
//
// Each event is a small object {t, lv, ev, ...scalar fields}. A bounded ring lives in memory and in
// chrome.storage.local (key 'elog') so a stopped service worker loses almost nothing; batches go to
// POST /api/ext-log, where the server appends them to its own log tagged with this lane.
//
// What may leave this file: statuses, durations, counts, classifications, short error names. Never cookies,
// tokens, page or API content, message text, or URL query strings. sanitize() enforces that as a second line
// of defence; the call sites also only pass safe fields.
(function (root) {
  const MAX = 300, BATCH = 100, KEY = 'elog', STR = 120, FIELDS = 12;
  const LEVELS = ['debug', 'info', 'warn', 'error'];
  // Field names that are never sent, whatever the value (mirror of server/obs.py).
  const DENY = /cookie|session|csrf|token|secret|passw|auth|bearer|credential|^(?:text|body|html|bio|note|notes|dm|sample|message|response|headers?|content|prompt|caption)$/i;
  const NAME = /^[a-z][a-z0-9_]{0,31}$/;
  const EVENT = /^[a-z][a-z0-9_.:-]{0,59}$/;
  const SECRET = /\b(sessionid|csrftoken|ds_user_id|ig_did|mid|rur|authorization|cookie|token)\b["']?\s*[:=]\s*["']?[^\s"';,&]{3,}/gi;
  const state = { buf: [], dropped: 0, loaded: false, saving: null, lastFlush: 0, flushing: null, limits: new Map(), onUrgent: null };

  const text = (value) => String(value).replace(SECRET, '$1=[redacted]').replace(/\?[^\s]*/g, '').slice(0, STR);

  // Keeps only short scalar fields with safe names; strings lose query strings and credential-looking pairs.
  function sanitize(fields) {
    const out = {};
    let n = 0;
    for (const key of Object.keys(fields || {})) {
      const value = fields[key];
      if (n >= FIELDS || !NAME.test(key) || DENY.test(key) || value === undefined || value === null) continue;
      if (typeof value === 'number') { if (Number.isFinite(value)) { out[key] = Math.round(value * 1000) / 1000; n++; } }
      else if (typeof value === 'boolean') { out[key] = value; n++; }
      else if (typeof value === 'string') { out[key] = text(value); n++; }
    }
    return out;
  }

  const store = () => (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) || null;

  async function load() {
    if (state.loaded) return;
    state.loaded = true;
    try {
      const s = store();
      const saved = s && (await s.get(KEY))[KEY];
      if (saved && Array.isArray(saved.events)) {
        state.buf = saved.events.filter((e) => e && EVENT.test(e.ev || '')).concat(state.buf).slice(-MAX);
        state.dropped += Number(saved.dropped) || 0;
      }
    } catch { /* storage unavailable: memory only */ }
  }

  function persist(now) {
    const s = store();
    if (!s || state.saving) return;
    const run = () => {
      state.saving = null;
      try { s.set({ [KEY]: { events: state.buf, dropped: state.dropped } }).catch(() => {}); } catch { /* ignore */ }
    };
    if (now) run();
    else state.saving = setTimeout(run, 1500);
  }

  // event('warn', 'ig.req', {status: 429, ms: 812}) - never throws, never waits.
  function event(level, ev, fields, opts) {
    try {
      if (!EVENT.test(ev)) return;
      const lv = LEVELS.includes(level) ? level : 'info';
      if (opts && opts.every) {
        const key = opts.key || ev, now = Date.now(), last = state.limits.get(key) || 0;
        if (now - last < opts.every) return;
        state.limits.set(key, now);
        if (state.limits.size > 200) state.limits.clear();
      }
      state.buf.push({ t: Date.now(), lv, ev, ...sanitize(fields) });
      if (state.buf.length > MAX) { state.dropped += state.buf.length - MAX; state.buf.splice(0, state.buf.length - MAX); }
      const urgent = lv === 'error' || lv === 'warn';
      persist(urgent);
      if (urgent && state.onUrgent) state.onUrgent();
    } catch { /* logging must never break the worker */ }
  }

  // post(body) -> {status, json}. Sends up to BATCH events; removes them only after the server accepted them.
  function flush(post, opts) {
    if (state.flushing) return state.flushing;
    const force = !!(opts && opts.force);
    state.flushing = (async () => {
      try {
        await load();
        if (!state.buf.length && !state.dropped) return 0;
        if (!force && Date.now() - state.lastFlush < 20e3) return 0;
        state.lastFlush = Date.now();
        const events = state.buf.slice(0, BATCH), dropped = state.dropped;
        const r = await post({ v: opts && opts.version, events, dropped });
        if (!r || r.status !== 200 || !r.json || !Number.isFinite(r.json.accepted)) return 0;
        // Events were appended behind our batch while the request ran: remove exactly what was sent.
        const sent = new Set(events);
        state.buf = state.buf.filter((e) => !sent.has(e));
        state.dropped = Math.max(0, state.dropped - dropped);
        persist(true);
        return events.length;
      } catch { return 0; }
      finally { state.flushing = null; }
    })();
    return state.flushing;
  }

  const peek = () => ({ events: state.buf.slice(), dropped: state.dropped });
  function reset() { Object.assign(state, { buf: [], dropped: 0, loaded: false, saving: null, lastFlush: 0, flushing: null, onUrgent: null }); state.limits.clear(); }

  root.FLLog = { event, flush, sanitize, peek, reset, load, MAX, BATCH, set onUrgent(fn) { state.onUrgent = fn; } };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.FLLog;
})(typeof globalThis !== 'undefined' ? globalThis : this);
