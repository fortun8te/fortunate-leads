// Offline experiment only. Not imported by the extension and contains no network IO.
// A capture supplies verified request identity; response data alone cannot prove the viewer.
(function (root) {
  'use strict';
  const BINDING = ['viewer_id', 'target_id', 'direction', 'transport', 'operation', 'session_key'];
  const own = (o, k) => Object.prototype.hasOwnProperty.call(o || {}, k);
  const id = value => typeof value === 'string' && /^\d+$/.test(value) ? value :
    Number.isSafeInteger(value) && value >= 0 ? String(value) : null;
  function cursor(value) {
    if (value == null || value === '') return null;
    if (typeof value === 'string' && value.trim()) return value;
    if (Number.isSafeInteger(value) && value >= 0) return String(value);
    throw new Error('invalid_cursor');
  }
  function normalizePage(body, direction, transport) {
    if (!body || typeof body !== 'object' || !['followers', 'following'].includes(direction)) throw new Error('invalid_page');
    if ((body.errors && (!Array.isArray(body.errors) || body.errors.length)) || body.status === 'fail' || body.require_login || body.spam) throw new Error('response_error');
    const data = body.data || body;
    let envelope, rows, schema, more, next;
    if (transport === 'rest') {
      envelope = Array.isArray(body.users) ? body : data;
      if (!Array.isArray(envelope.users)) throw new Error('unknown_schema');
      rows = envelope.users; schema = 'rest_users'; more = envelope.has_more; next = envelope.next_max_id;
    } else if (transport === 'web_graphql') {
      const user = data.user || body.user;
      schema = direction === 'followers' ? 'edge_followed_by' : 'edge_follow';
      envelope = user?.[schema];
      if (!Array.isArray(envelope?.edges)) throw new Error('unknown_schema');
      rows = envelope.edges.map(edge => edge?.node);
      more = envelope.page_info?.has_next_page; next = envelope.page_info?.end_cursor;
    } else if (transport === 'mobile_graphql') {
      schema = 'xdt_api__v1__friendships__' + direction;
      // Only the exact root is accepted. Similar recommendation roots are not lists.
      envelope = data[schema];
      if (!Array.isArray(envelope?.users)) throw new Error('unknown_schema');
      rows = envelope.users; more = envelope.has_more; next = envelope.next_max_id;
    } else throw new Error('unknown_transport');
    if (more != null && typeof more !== 'boolean') throw new Error('invalid_has_more');
    next = cursor(next);
    if (more === true && next == null) throw new Error('missing_cursor');
    const ids = rows.map(user => id(user?.pk ?? user?.id));
    if (ids.some(value => value == null)) throw new Error('invalid_user_id');
    const flags = [body, data, envelope];
    for (const o of flags) if (own(o, 'should_limit_list_of_followers') && typeof o.should_limit_list_of_followers !== 'boolean') throw new Error('invalid_limit_flag');
    return { schema, ids, rows: rows.length, next_cursor: next, has_more: more ?? null,
      terminal: more === false, limited: flags.some(o => o.should_limit_list_of_followers === true) };
  }
  function evaluateRun(run) {
    const reasons = [], accepted = new Set(), cursors = new Set();
    let rows = 0, pages = 0, expectedCursor = null, terminal = false, limited = false;
    if (!run || BINDING.some(k => typeof run[k] !== 'string' || !run[k])) throw new Error('missing_run_binding');
    if (!id(run.viewer_id) || !id(run.target_id)) throw new Error('invalid_run_identity');
    const started = Date.parse(run.started_at), ended = Date.parse(run.ended_at);
    if (!Number.isFinite(started) || !Number.isFinite(ended) || ended < started) throw new Error('invalid_run_time');
    let lastAt = started;
    for (const capture of run.pages || []) {
      const request = capture.request || {};
      if (BINDING.some(k => request[k] !== run[k])) { reasons.push('request_binding_changed'); break; }
      if (terminal) { reasons.push('page_after_terminal'); break; }
      let requested;
      try { requested = cursor(request.cursor); } catch (e) { reasons.push(e.message); break; }
      if (requested !== expectedCursor) { reasons.push('cursor_chain_broken'); break; }
      const at = Date.parse(capture.captured_at);
      if (!Number.isFinite(at) || at < lastAt || at > ended) { reasons.push('invalid_capture_time'); break; }
      lastAt = at;
      if (capture.status !== 200 || capture.redirected === true) { reasons.push('transport_failure'); break; }
      let page;
      try { page = normalizePage(capture.response, run.direction, run.transport); }
      catch (e) { reasons.push(e.message); break; }
      pages++; rows += page.rows;
      for (const value of page.ids) accepted.add(value);
      limited ||= page.limited;
      if (page.limited) { reasons.push('platform_limited'); break; }
      if (!page.terminal && page.next_cursor != null && cursors.has(page.next_cursor)) { reasons.push('cursor_cycle'); break; }
      if (page.next_cursor != null) cursors.add(page.next_cursor);
      terminal = page.terminal;
      expectedCursor = page.next_cursor;
      if (!terminal && !expectedCursor) { reasons.push('end_unproven'); break; }
      if (!terminal && page.rows === 0) { reasons.push('empty_page_with_more'); break; }
    }
    const countAt = Date.parse(run.count_at);
    const freshCount = run.count_source === 'current_run' && Number.isSafeInteger(run.expected_count) && run.expected_count >= 0 &&
      Number.isFinite(countAt) && countAt >= started && countAt <= ended;
    if (!freshCount) reasons.push('fresh_count_missing');
    else if (accepted.size !== run.expected_count) reasons.push('count_mismatch');
    if (!terminal && !reasons.includes('end_unproven')) reasons.push('terminal_page_missing');
    const status = !reasons.length && pages > 0 ? 'complete' : pages > 0 ? 'partial' : 'unknown';
    return { run_id: run.run_id ?? null, target_id: run.target_id, direction: run.direction, viewer_id: run.viewer_id,
      status, reasons: [...new Set(reasons)], pages, rows, unique_people: accepted.size, duplicate_rows: rows - accepted.size,
      expected_count: freshCount ? run.expected_count : null, terminal, limited, ids: [...accepted] };
  }
  function evaluateExperiment(experiment) {
    const start = Date.parse(experiment.started_at), end = Date.parse(experiment.ended_at), hours = (end - start) / 3600000;
    if (!Number.isFinite(hours) || hours <= 0) throw new Error('invalid_observation_window');
    const existing = new Set((experiment.existing_ids || []).map(id));
    if (existing.has(null)) throw new Error('invalid_existing_id');
    const seen = new Set(), completeLists = new Set();
    const runs = (experiment.runs || []).map(run => {
      if (Date.parse(run.started_at) < start || Date.parse(run.ended_at) > end) throw new Error('run_outside_observation_window');
      return evaluateRun(run);
    });
    for (const run of runs) {
      for (const value of run.ids) seen.add(value);
      if (run.status === 'complete') completeLists.add(run.target_id + '/' + run.direction);
    }
    const newPeople = [...seen].filter(value => !existing.has(value)).length;
    const pages = runs.reduce((n, r) => n + r.pages, 0), rows = runs.reduce((n, r) => n + r.rows, 0);
    return { evidence: experiment.evidence === 'captured' ? 'captured_unverified_metadata' : 'synthetic',
      observed_hours: hours, complete_target_lists: completeLists.size, unique_people: seen.size, new_people: newPeople,
      new_people_per_hour: newPeople / hours, completed_lists_per_24h_equivalent: completeLists.size * 24 / hours,
      accepted_pages: pages, returned_rows: rows, rows_per_page: pages ? rows / pages : 0,
      // Short-window rate is descriptive. It does not predict future holds or daily yield.
      runs: runs.map(({ ids, ...summary }) => summary) };
  }
  // Formula only: accepted_page_size is measured, never the count parameter requested.
  function pageBudgetProjection({ target_size, accepted_page_size, usable_pages_per_day }) {
    if (![target_size, accepted_page_size, usable_pages_per_day].every(x => Number.isSafeInteger(x) && x > 0)) throw new Error('invalid_projection');
    const pages_per_list = Math.ceil(target_size / accepted_page_size);
    return { pages_per_list, complete_lists_per_day: Math.floor(usable_pages_per_day / pages_per_list),
      assumptions: 'All pages succeed, all rows are unique, targets have equal size, no extra terminal page, no holds.' };
  }
  const api = { normalizePage, evaluateRun, evaluateExperiment, pageBudgetProjection };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.FLNativeListExperiment = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
