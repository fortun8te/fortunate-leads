/* Dev-only stand-in for /api/map/view, /api/map/edges and /api/map/search.
 * Loaded by index.html when the address has ?mock=1 (after mock.js) or ?mapmock=1 (alone).
 * The synthetic world in map-world.js answers inside a Worker, after a short pretend
 * network delay, and honours AbortController like a real request. */
(function () {
  'use strict';
  const previous = window.fetch.bind(window);
  const realFetch = previous;
  const params = new URLSearchParams(location.search);
  const delay = Math.max(0, Number(params.get('maplatency')) || 60);
  let ready = null, worker = null, local = null, seq = 0;
  const pending = new Map();

  function start() {
    if (ready) return ready;
    ready = realFetch('map-world.js').then((r) => r.text()).then((source) => {
      try {
        worker = new Worker(URL.createObjectURL(new Blob([source], { type: 'text/javascript' })));
        worker.onmessage = (e) => {
          const p = pending.get(e.data.id); if (!p) return;
          pending.delete(e.data.id);
          if (e.data.error) p.reject(new Error(e.data.error)); else p.resolve(e.data.result);
        };
        worker.onerror = () => { worker = null; };
      } catch (_) {
        worker = null;
      }
      if (!worker) { const box = {}; new Function('window', 'self', source)(box, box); local = box.MapWorld.createWorld(); }
    });
    return ready;
  }
  function ask(kind, args) {
    return start().then(() => new Promise((resolve, reject) => {
      if (local) { try { resolve(local[kind](args)); } catch (e) { reject(e); } return; }
      const id = ++seq; pending.set(id, { resolve, reject }); worker.postMessage({ id, kind, args });
    }));
  }
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  const wait = (ms, signal) => new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException('Aborted', 'AbortError'));
    const t = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => { clearTimeout(t); reject(new DOMException('Aborted', 'AbortError')); }, { once: true });
  });
  window.__mapMock = { requests: [], failNext: 0, offlineNext: 0 };

  window.fetch = function (url, opts = {}) {
    const s = String(url);
    const m = s.match(/^\/api\/map\/(view|edges|search)(?:\?(.*))?$/);
    if (!m) return previous(url, opts);
    const args = Object.fromEntries(new URLSearchParams(m[2] || ''));
    const log = window.__mapMock;
    log.requests.push({ kind: m[1], args, at: performance.now() });
    if (log.offlineNext > 0) { log.offlineNext--; return Promise.reject(new TypeError('Failed to fetch')); }
    return wait(delay * (0.6 + Math.random() * 0.8), opts.signal).then(() => {
      if (log.failNext > 0) { log.failNext--; return json({ ok: false, error: 'The map could not be read.' }, 500); }
      return ask(m[1], args).then((result) => { if (opts.signal?.aborted) throw new DOMException('Aborted', 'AbortError'); return json(result); });
    });
  };
})();
