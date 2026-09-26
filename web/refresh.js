/* Refresh checks share the app's poll; this module creates no timers. */
(function (root) {
  const localDay = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  const relative = (filter) => filter === 'due' || filter === 'overdue';

  function createRefreshCoordinator({ getState, refreshLeads, loadPerson, onDayChange = () => {}, now = () => new Date() }) {
    let observedDay = localDay(now()), listDay = observedDay;
    let listPending = null, resumed = false;
    const people = new Map();

    // Polls share a request. An explicit refresh after a save must read after
    // the older request, otherwise its response could hide the saved change.
    function personRequest(id, explicit) {
      let entry = people.get(id);
      if (entry && (!explicit || entry.queued)) return entry.promise;
      if (!entry) { entry = { promise: null, queued: false }; people.set(id, entry); }
      const previous = entry.promise;
      entry.queued = !!previous;
      const request = (previous ? previous.catch(() => {}) : Promise.resolve())
        .then(() => { if (entry.promise === request) entry.queued = false; return loadPerson(id); })
        .finally(() => { if (entry.promise === request) people.delete(id); });
      entry.promise = request;
      return request;
    }

    function tick() {
      const state = getState();
      if (state.hidden) return Promise.resolve([]);
      const day = localDay(now()), tasks = [];
      if (day !== observedDay) {
        observedDay = day;
        tasks.push(Promise.resolve().then(() => onDayChange(day)));
      }
      if (state.view === 'leads') {
        if (!relative(state.followUp)) { listDay = day; resumed = false; }
        else if ((listDay !== day || resumed) && !state.loading && !listPending) {
          resumed = false;
          // False signals a handled load failure. Keep the refresh due for
          // the next poll, just as with a rejected request.
          listPending = Promise.resolve().then(() => refreshLeads())
            .then((result) => { if (result === false) resumed = true; else listDay = day; })
            .catch((error) => { resumed = true; throw error; })
            .finally(() => { listPending = null; });
        }
      }
      if (listPending) tasks.push(listPending);
      if (state.openId != null && state.personPending && !state.personLoading) {
        tasks.push(personRequest(state.openId, false));
      }
      // Background failures should neither disable retries nor cause unhandled
      // rejections when the app invokes this from setInterval.
      return Promise.allSettled(tasks);
    }

    function visibilityChanged() {
      if (!getState().hidden) resumed = true;
      return tick();
    }

    return { tick, visibilityChanged, refreshPerson: (id) => personRequest(id, true) };
  }

  root.LeadRefresh = { createRefreshCoordinator };
})(typeof window === 'undefined' ? globalThis : window);
