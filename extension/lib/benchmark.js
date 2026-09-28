// Isolated raw-list benchmark. No profile lookup, fallback, navigation or automatic retry.
(function (root) {
  function url(task, viewerId) {
    if (!task || task.route !== 'web_rest' || task.transport !== 'chrome' ||
        !task.task_id || !/^\d+$/.test(String(task.target_id || '')) ||
        !/^\d+$/.test(String(task.viewer_id || '')) || String(task.viewer_id) !== String(viewerId) ||
        !['followers', 'following'].includes(task.direction) || ![50, 100, 200, 300, 500, 1500].includes(task.page_size) ||
        (task.cursor != null && typeof task.cursor !== 'string')) throw new Error('Invalid benchmark task');
    return 'https://www.instagram.com/api/v1/friendships/' + task.target_id + '/' + task.direction + '/?count=' + task.page_size +
      (task.cursor ? '&max_id=' + encodeURIComponent(task.cursor) : '') +
      (task.direction === 'followers' ? '&search_surface=follow_list_page' : '');
  }
  // Serialized by executeScript into MAIN. Keep this function self-contained.
  async function fetchOnce(u, expectedViewer, ms) {
    let actual_http_requests = 0, fetch_dispatches = 0, receivedResponse = false;
    const start = performance.now();
    const ck = document.cookie, viewer = (ck.match(/(?:^|;\s*)ds_user_id=(\d+)/) || [])[1];
    const base = () => ({ actual_http_requests, fetch_dispatches, duration_ms: Math.max(0, performance.now() - start), observed_viewer_id: viewer || null });
    if (location.hostname !== 'www.instagram.com' || document.readyState !== 'complete' ||
        viewer !== String(expectedViewer) || /^\/(?:accounts\/login|challenge|checkpoint)/.test(location.pathname))
      return { ...base(), status: 0, error: 'identity_or_tab_blocked', text: '' };
    const endpoint = u.match(/^https:\/\/www\.instagram\.com\/api\/v1\/friendships\/\d+\/(following|followers)\/\?count=(?:50|100|200|300|500|1500)(?:&max_id=[^&]*)?(&search_surface=follow_list_page)?$/);
    if (!endpoint || (endpoint[1] === 'followers') !== !!endpoint[2])
      return { ...base(), status: 0, error: 'invalid_url', text: '' };
    const csrf = decodeURIComponent((ck.match(/(?:^|;\s*)csrftoken=([^;]+)/) || [])[1] || '');
    if (!csrf || typeof window.__flFetch !== 'function') return { ...base(), status: 0, error: 'missing_original_fetch_or_csrf', text: '' };
    let claim = '0'; try { claim = sessionStorage.getItem('www-claim-v2') || '0'; } catch {}
    const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), ms);
    try {
      fetch_dispatches++;
      const res = await window.__flFetch(u, { credentials: 'include', cache: 'no-store', redirect: 'manual', signal: ctl.signal, headers: {
        'X-IG-App-ID': '936619743392459', 'X-CSRFToken': csrf, 'X-ASBD-ID': '129477', 'X-IG-WWW-Claim': claim,
        'X-Requested-With': 'XMLHttpRequest', 'Accept': '*/*' } });
      receivedResponse = true; actual_http_requests = 1;
      const claimOut = res.headers.get('x-ig-set-www-claim');
      if (claimOut) try { sessionStorage.setItem('www-claim-v2', claimOut); } catch {}
      const text = await res.text();
      return { ...base(), status: res.status, text, contentType: res.headers.get('content-type') || '',
        retryAfter: res.headers.get('retry-after'), url: res.url, redirect: res.type === 'opaqueredirect' || res.redirected || (res.status >= 300 && res.status < 400) };
    } catch (e) {
      actual_http_requests = receivedResponse ? 1 : null;
      return { ...base(), status: 0, text: '', error: e?.name === 'AbortError' ? 'timeout' : 'transport_error', uncertain: true };
    } finally { clearTimeout(timer); }
  }
  function result(task, res, FL) {
    const json = FL.parseBody(res.text || '');
    const page = FL.parsePage(json);
    const rawCursor = json?.next_max_id || null;
    const rawWarning = !json || json.status !== 'ok' || !Array.isArray(json.users) ? 'invalid_rest_page' :
      json.users.some(u => !u || !/^\d+$/.test(String(u.pk || u.pk_id || u.id || '')) || !/^[a-zA-Z0-9._]{1,30}$/.test(String(u.username || ''))) ? 'invalid_rest_user' :
      rawCursor !== null && (typeof rawCursor !== 'string' || rawCursor.length > 4096) ? 'invalid_cursor' :
      json.has_more === false && rawCursor ? 'conflicting_end' : null;
    const classified = FL.classify({ ...res, json }, 'list', Date.now(), { cursor: task.cursor });
    const warning = res.error || (res.redirect ? 'redirect' : null) ||
      classified?.reason || rawWarning ||
      (task.cursor && task.cursor === page.next_cursor ? 'cursor_repeated' : null) || (page.limited ? 'limited' : null);
    return { rows: page.users, next_cursor: page.next_cursor, has_more: page.has_more ?? !!page.next_cursor, reported_has_more: page.has_more,
      status: warning ? (res.error || (res.redirect ? 'redirect' : classified?.code) || 'pagination_warning') : 'ok', terminal_warning: warning || null, actual_http_requests: res.actual_http_requests ?? null,
      http_status: res.status || 0, retry_after: res.retryAfter || null, duration_ms: res.duration_ms ?? null, fetch_dispatches: res.fetch_dispatches ?? null,
      uncertain: !!res.uncertain, transport_completed: !res.uncertain && [0, 1].includes(res.actual_http_requests), observed_viewer_id: res.observed_viewer_id || null };
  }
  root.FLBenchmark = { url, fetchOnce, result };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.FLBenchmark;
})(typeof globalThis !== 'undefined' ? globalThis : this);
