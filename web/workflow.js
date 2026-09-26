/* Shared browser workflow primitives; also testable without a DOM. */
(function (root) {
  const localToday = (date = new Date()) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  function runtimeQuery(query, date = new Date()) {
    const params = new URLSearchParams(query);
    params.set('today', localToday(date));
    return params;
  }
  function createNoteQueue({ save, change = () => {}, persist = () => {}, initial = {}, delay = 600 }) {
    const entries = new Map(Object.entries(initial).map(([id, draft]) => [Number(id), { draft, saved: null, error: null, timer: null, pending: null }]));
    function snapshot() { return Object.fromEntries([...entries].filter(([, e]) => e.pending || e.draft !== e.saved).map(([id, e]) => [id, e.draft])); }
    function notify(id) { persist(snapshot()); change(id, entries.get(id)); }
    function state(id, saved = '') { if (!entries.has(id)) entries.set(id, { draft: saved, saved, error: null, timer: null, pending: null }); return entries.get(id); }
    function reconcile(id, saved = '') { const e = state(id, saved); if (!e.pending && e.draft === e.saved) { e.saved = saved; e.draft = saved; } return e; }
    async function flush(id) {
      const e = entries.get(id); if (!e) return;
      clearTimeout(e.timer); e.timer = null;
      if (e.pending) { await e.pending; return flush(id); }
      if (e.draft === e.saved) return;
      const value = e.draft;
      e.error = null;
      // Set pending before invoking callbacks, so UI cannot claim an early save.
      e.pending = Promise.resolve().then(() => save(id, value)); notify(id);
      try { await e.pending; e.saved = value; }
      catch (error) { e.error = error; throw error; }
      finally { e.pending = null; notify(id); }
      if (e.draft !== e.saved) return flush(id);
    }
    function edit(id, draft, saved = '') { const e = state(id, saved); e.draft = draft; e.error = null; clearTimeout(e.timer); e.timer = setTimeout(() => flush(id).catch(() => {}), delay); notify(id); }
    async function flushAll() { const results = await Promise.allSettled([...entries.keys()].map(flush)); const fail = results.find((r) => r.status === 'rejected'); if (fail) throw fail.reason; }
    return { state, reconcile, peek: (id) => entries.get(id), edit, flush, flushAll, snapshot, dirty: () => [...entries.values()].some((e) => e.pending || e.draft !== e.saved) };
  }
  root.LeadWorkflow = { localToday, runtimeQuery, createNoteQueue };
})(typeof window === 'undefined' ? globalThis : window);
