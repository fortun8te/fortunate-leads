/* Shared browser workflow primitives; also testable without a DOM. */
(function (root) {
  const localToday = (date = new Date()) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  function addLocalDays(offset, date = new Date()) {
    const next = new Date(date.getTime());
    next.setDate(next.getDate() + offset);
    return localToday(next);
  }
  function runtimeQuery(query, date = new Date()) {
    const params = new URLSearchParams(query);
    params.set('today', localToday(date));
    return params;
  }
  function reconcileWorkflowDraft(current = {}, submitted = {}, fields = []) {
    const draft = { ...current }, versions = { ...current._versions };
    let unchanged = true;
    for (const field of fields) {
      if (current[field] === submitted[field] && current._versions?.[field] === submitted._versions?.[field]) {
        delete draft[field]; delete versions[field];
      } else unchanged = false;
    }
    if (Object.keys(versions).length) draft._versions = versions;
    else delete draft._versions;
    return { draft, unchanged };
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
      if (e.pending) return e.pending;
      if (e.draft === e.saved) return;
      e.error = null;
      // All callers await the same drain, including edits made during a save.
      // Set pending before invoking callbacks, so UI cannot claim an early save.
      e.pending = Promise.resolve().then(async () => {
        while (e.draft !== e.saved) {
          clearTimeout(e.timer); e.timer = null;
          const value = e.draft;
          try { await save(id, value); e.saved = value; }
          catch (error) {
            // A lost response may follow a committed write. Even a revert to
            // the previous saved value must be sent again in that case.
            e.saved = null;
            if (e.draft === value) { e.error = error; throw error; }
          }
          notify(id);
        }
      }).finally(() => { e.pending = null; notify(id); });
      notify(id);
      return e.pending;
    }
    function edit(id, draft, saved = '') { const e = state(id, saved); e.draft = draft; e.error = null; clearTimeout(e.timer); e.timer = setTimeout(() => flush(id).catch(() => {}), delay); notify(id); }
    async function flushAll() { const results = await Promise.allSettled([...entries.keys()].map(flush)); const fail = results.find((r) => r.status === 'rejected'); if (fail) throw fail.reason; }
    return { state, reconcile, peek: (id) => entries.get(id), edit, flush, flushAll, snapshot, dirty: () => [...entries.values()].some((e) => e.pending || e.draft !== e.saved) };
  }
  root.LeadWorkflow = { localToday, addLocalDays, runtimeQuery, reconcileWorkflowDraft, createNoteQueue };
})(typeof window === 'undefined' ? globalThis : window);
