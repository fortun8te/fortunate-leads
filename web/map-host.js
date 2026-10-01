/* Connects the map view to the rest of the workspace: requests, toasts, opening a lead,
 * saving a status or note. Loaded after app.js, which owns api, S, toast and the detail panel. */
(function () {
  'use strict';
  if (typeof M === 'undefined' || !window.MapViewModule || !document.getElementById('map-canvas')) return;
  // Map requests are cancelled all the time as you move. Cancelling is not being offline, so api.req is not used here.
  async function fetchJson(url, opts = {}) {
    let r;
    try { r = await fetch(url, { cache: 'no-store', signal: opts.signal }); }
    catch (e) { if (e.name !== 'AbortError') setOnline(false); throw e; }
    setOnline(true);
    if (!r.ok) { const err = new Error(r.status >= 500 ? 'The server hit a problem. Try again in a moment.' : 'The map could not be read.'); err.status = r.status; throw err; }
    return r.json();
  }
  function saveClassification(id, action, body) {
    return serializeMutation(async () => {
      if (typeof invalidatePersonRead === 'function') invalidatePersonRead(id);
      await api.post(`/api/person/${encodeURIComponent(id)}/${action}`, body);
      const person = await api.get('/api/person/' + encodeURIComponent(id));
      if (typeof patchRow === 'function') {
        patchRow(id, { status: person.status, tags: person.tags, manual_tags: person.manual_tags });
      }
      return person;
    });
  }
  const host = {
    fetchJson,
    statuses: STATUSES,
    openList: () => setView('leads'),
    toast: (text) => toast(text),
    // Same route the review list uses to jump to a person.
    openLead(id) {
      setView('leads');
      setURL(true);
      openDetail(id);
    },
    person: (id) => api.get('/api/person/' + encodeURIComponent(id)),
    setStatus: (id, status) => saveClassification(id, 'mark', { status }),
    tags: () => api.get('/api/tags'),
    tagPresentation(label) {
      const tag = { tag: label, source: 'manual' }, importance = tagImportance(tag);
      return { label: tagLabel(label), importance, tone: tagTone(tag),
        icon: importance === 'exceptional' ? 'sparkles' : ['priority', 'strong'].includes(importance) ? 'verified' : '' };
    },
    editTags: (id, add, remove) => saveClassification(id, 'tags', { add, remove }),
    // If_match makes a stale note fail with a conflict instead of overwriting someone else's edit.
    saveNote: (id, note, rev) => api.post(`/api/person/${encodeURIComponent(id)}/mark`, { note, if_match: rev ?? '' })
  };
  const view = window.MapViewModule.mount(host);
  if (view) M.attach(view);
})();
