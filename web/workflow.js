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
  function createNoteQueue({ save, change = () => {}, persist = () => {}, initial = {}, delay = 600, requireRevision = false }) {
    const entries = new Map(Object.entries(initial).map(([id, value]) => [Number(id), { ...(value && typeof value === 'object' ? value : { draft: value, saved: null }), error: null, timer: null, pending: null }]));
    function snapshot() { return Object.fromEntries([...entries].filter(([, e]) => e.pending || e.draft !== e.saved).map(([id, e]) => [id, requireRevision ? { draft: e.draft, saved: e.saved, revision: e.revision ?? null } : e.draft])); }
    function notify(id) { persist(snapshot()); change(id, entries.get(id)); }
    function state(id, saved = '', revision) { if (!entries.has(id)) entries.set(id, { draft: saved, saved, revision, error: null, timer: null, pending: null }); return entries.get(id); }
    function conflict(id, saved, revision) {
      const e = state(id); e.remoteNote = saved; e.remoteRevision = revision; e.conflict = true;
      e.error = Object.assign(new Error('Note changed elsewhere. Your draft is kept.'), { status: 409 });
      clearTimeout(e.timer); e.timer = null; notify(id); return e;
    }
    function reconcile(id, saved = '', revision) {
      const e = state(id, saved, revision);
      if (!e.pending && e.draft === e.saved) { e.saved = saved; e.draft = saved; e.revision = revision; }
      else if (requireRevision && !e.pending && revision !== undefined) {
        if (e.revision == null || (e.revision !== revision && e.saved !== saved)) conflict(id, saved, revision);
        else if (!e.conflict) e.revision = revision;
      }
      return e;
    }
    function resolve(id, keepDraft) {
      const e = entries.get(id); if (!e?.conflict || e.remoteRevision == null) return;
      e.saved = e.remoteNote; e.revision = e.remoteRevision;
      if (!keepDraft) e.draft = e.remoteNote;
      e.conflict = false; e.error = null; notify(id);
      return keepDraft ? flush(id) : Promise.resolve();
    }
    async function flush(id) {
      const e = entries.get(id); if (!e) return;
      clearTimeout(e.timer); e.timer = null;
      if (e.pending) return e.pending;
      if (e.draft === e.saved) return;
      if (e.conflict || requireRevision && e.revision == null) {
        e.error = Object.assign(new Error('Review the saved note before saving this draft.'), { status: 409 });
        notify(id); throw e.error;
      }
      e.error = null;
      // All callers await the same drain, including edits made during a save.
      // Set pending before invoking callbacks, so UI cannot claim an early save.
      e.pending = Promise.resolve().then(async () => {
        while (e.draft !== e.saved) {
          clearTimeout(e.timer); e.timer = null;
          const value = e.draft;
          try { const result = await save(id, value, e.revision); e.saved = value; if (result?.mark_rev != null) e.revision = result.mark_rev; }
          catch (error) {
            // A lost response may follow a committed write. Even a revert to
            // the previous saved value must be sent again in that case.
            if (error.status !== 409) e.saved = null;
            if (error.status === 409 || e.draft === value) { e.error = error; throw error; }
          }
          notify(id);
        }
      }).finally(() => { e.pending = null; notify(id); });
      notify(id);
      return e.pending;
    }
    function edit(id, draft, saved = '', revision) { const e = state(id, saved, revision); e.draft = draft; if (!e.conflict) e.error = null; clearTimeout(e.timer); e.timer = setTimeout(() => flush(id).catch(() => {}), delay); notify(id); }
    async function flushAll() { const results = await Promise.allSettled([...entries.keys()].map(flush)); const fail = results.find((r) => r.status === 'rejected'); if (fail) throw fail.reason; }
    return { state, reconcile, conflict, resolve, peek: (id) => entries.get(id), edit, flush, flushAll, snapshot, dirty: () => [...entries.values()].some((e) => e.pending || e.draft !== e.saved) };
  }
  root.LeadWorkflow = { localToday, addLocalDays, runtimeQuery, reconcileWorkflowDraft, createNoteQueue };
})(typeof window === 'undefined' ? globalThis : window);
