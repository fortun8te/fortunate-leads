'use strict';
/* Recorded follow paths. No relationship-strength or introduction inference. */
(() => {
  const panel = document.querySelector('#connection-panel');
  if (!panel) return;
  const el = (id) => document.getElementById('connection-' + id);
  const selected = { from: null, to: null };
  const generations = { from: 0, to: 0 };
  const suggestionTimers = { from: null, to: null };
  let generation = 0;
  const date = (value) => {
    if (!value) return 'unknown';
    const d = new Date(value);
    return Number.isNaN(d.valueOf()) ? 'unknown' : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  };
  const handle = (a) => '@' + (a?.handle || a?.id || 'unknown');
  const legacy = (a) => ['legacy_handle', 'legacy_person'].includes(a?.identity_basis);
  const sourceLabel = (a) => legacy(a) ? 'Historical record; account identity not captured'
    : a.handle_basis ? `Historical account ${a.ig_id}; current handle unverified`
    : a.is_me ? 'Your account' : a.is_seed ? 'Source account' : a.name || 'Imported account';
  const accountLink = (a) => {
    const text = esc(handle(a));
    // A legacy or parked handle can belong to someone else now, so it is never a live profile link.
    if (legacy(a) || a?.handle_basis || !/^[a-zA-Z0-9._]{1,30}$/.test(a?.handle || '')) return text;
    return `<a href="https://www.instagram.com/${encodeURIComponent(a.handle)}/" target="_blank" rel="noopener noreferrer">${text}</a>`;
  };

  function mode(overview) {
    panel.hidden = overview;
    el('explore').setAttribute('aria-pressed', String(!overview));
    el('overview').setAttribute('aria-pressed', String(overview));
    el('explore').classList.toggle('on', !overview);
    el('overview').classList.toggle('on', overview);
    if (typeof M !== 'undefined') { if (overview) M.show(); else M.hide(); }
  }
  el('explore').addEventListener('click', () => mode(false));
  el('overview').addEventListener('click', () => mode(true));

  async function suggest(which) {
    const gen = ++generations[which];
    const query = el(which).value.trim();
    const box = el(which + '-results');
    try {
      const data = await api.get('/api/connections/search?' + new URLSearchParams({ q: query }));
      if (gen !== generations[which] || document.activeElement !== el(which)) return;
      box.replaceChildren();
      for (const a of data.accounts || []) {
        const button = document.createElement('button');
        button.type = 'button';
        button.innerHTML = `${esc(handle(a))}<small>${esc(sourceLabel(a))}</small>`;
        button.addEventListener('click', () => {
          selected[which] = a;
          el(which).value = handle(a);
          box.hidden = true;
          ++generations[which];
          invalidate();
          el(which === 'from' ? 'to' : 'run').focus();
        });
        box.append(button);
      }
      if (!box.children.length) box.innerHTML = '<p>No imported accounts match.</p>';
      if (data.truncated) box.insertAdjacentHTML('beforeend', '<p>More matches available. Keep typing to narrow them down.</p>');
      box.hidden = false;
    } catch (e) {
      if (gen !== generations[which]) return;
      box.hidden = true;
      el('status').textContent = 'Account search unavailable. ' + e.message;
    }
  }

  function invalidate() {
    ++generation;
    el('run').disabled = false;
    el('run').textContent = 'Find paths';
    el('results').replaceChildren();
    el('status').textContent = 'Press Find paths to explore these accounts.';
  }
  for (const which of ['from', 'to']) {
    const input = el(which);
    input.addEventListener('input', () => {
      selected[which] = null;
      const gen = ++generations[which];
      el(which + '-results').hidden = true;
      invalidate();
      clearTimeout(suggestionTimers[which]);
      suggestionTimers[which] = setTimeout(() => { if (gen === generations[which]) suggest(which); }, 180);
    });
    input.addEventListener('focus', () => suggest(which));
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { el(which + '-results').hidden = true; ++generations[which]; }
      if (event.key === 'ArrowDown') {
        const first = el(which + '-results').querySelector('button');
        if (!el(which + '-results').hidden && first) { event.preventDefault(); first.focus(); }
      }
    });
  }
  el('mode').addEventListener('change', invalidate);
  document.addEventListener('focusin', (event) => {
    for (const which of ['from', 'to']) if (!el(which).parentElement.contains(event.target)) {
      el(which + '-results').hidden = true;
      ++generations[which];
    }
  });
  document.addEventListener('click', (event) => {
    for (const which of ['from', 'to']) if (!el(which).parentElement.contains(event.target)) el(which + '-results').hidden = true;
  });

  el('form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const from = selected.from?.id || el('from').value.trim();
    const to = selected.to?.id || el('to').value.trim();
    if (!from || !to) { el('status').textContent = 'Choose a starting account and a target.'; el(!from ? 'from' : 'to').focus(); return; }
    const gen = ++generation;
    for (const which of ['from', 'to']) { ++generations[which]; el(which + '-results').hidden = true; }
    el('run').disabled = true;
    el('run').textContent = 'Finding paths…';
    el('status').textContent = 'Checking recorded follows up to three steps apart…';
    el('results').replaceChildren();
    try {
      const data = await api.get('/api/connections?' + new URLSearchParams({ from, to, mode: el('mode').value }));
      if (gen !== generation) return;
      render(data);
    } catch (e) {
      if (gen !== generation) return;
      el('status').textContent = e.status === 404 ? 'Account not found in the imported data. Choose a search result or check the exact handle.' : 'Could not find paths. ' + e.message;
    } finally {
      if (gen === generation) { el('run').disabled = false; el('run').textContent = 'Find paths'; }
    }
  });

  function coverageHTML(rows) {
    if (!rows?.length) return '';
    const labels = { missing: 'Not collected', partial: 'Partial or in progress', ended_unknown: 'Ended; total unverified', ended_short: 'Ended below listed total', ended_count_matched: 'Ended; recorded count matches total', ended_count_exceeded: 'Recorded count exceeds listed total' };
    return `<details class="connection-coverage"><summary>List coverage and missing data (${int(rows.length)} lists)</summary><p class="connection-note">Counts describe saved list records grouped by source handle. They do not verify its historical owner or a current, complete snapshot. List updates are separate from follow recording dates.</p><div class="connection-coverage-scroll"><table><thead><tr><th>Source list</th><th>Recorded / total</th><th>Coverage</th><th>List updated</th></tr></thead><tbody>${rows.map((r) => `<tr><td>@${esc(r.seed)}<br>${esc(r.direction)}</td><td>${int(r.captured)} / ${r.total == null ? 'unknown' : int(r.total)}</td><td>${esc(r.count_relation === 'above' ? 'Recorded count exceeds listed total' : labels[r.status] || r.status)}${r.state ? '<br>' + esc(r.state) : ''}${r.error ? '<br>' + esc(r.error) : ''}</td><td>${date(r.updated_at)}</td></tr>`).join('')}</tbody></table></div></details>`;
  }

  function render(data) {
    const paths = data.paths || [];
    const search = data.search || {};
    const isTruncated = search.search_truncated ?? search.truncated;
    el('status').textContent = data.source.id === data.target.id ? 'Both selections refer to the same account. Choose a different target.' : paths.length ? `${int(paths.length)} recorded ${paths.length === 1 ? 'path' : 'paths'} from ${handle(data.source)} to ${handle(data.target)}.` : 'No path found in the searched records within three steps.';
    const names = { shared_followees: 'accounts both follow', shared_followers: 'accounts following both', forward_chain: 'two-step forward chains', reverse_chain: 'two-step reverse chains' };
    const stats = Object.entries(names).map(([key, label]) => `<span><b>${int(data.common_neighbors?.[key]?.count || 0)}</b>${label}</span>`).join('');
    const warn = isTruncated ? '<p class="connection-warning">Search limit reached. Some accounts or paths were not checked, so a shorter or different path may be missing. Narrow the search by choosing an intermediate account.</p>' : '';
    const history = '<p class="connection-note">These are recorded follows, not confirmed relationships or introductions. They may have been collected on different days and may no longer exist.</p>';
    const identity = [data.source, data.target].some(legacy) ? '<p class="connection-warning">A historical record is selected. Its account ID was not captured, so it is kept separate from today’s account with the same name. Unidentified records cannot act as intermediate connections.</p>' : '';
    el('results').innerHTML = `${history}${identity}${warn}<div class="connection-facts">${stats}</div><p class="connection-note">Overlap counts use the searched records${isTruncated ? ' and are lower bounds because the search was limited' : ''}. Each type describes a different follow pattern.</p>${paths.length ? '<div class="connection-layout"><div><h3>Recorded paths</h3><div class="connection-routes" id="connection-routes"></div></div><div id="connection-path"></div></div>' : '<div class="connection-empty"><b>No recorded route to show</b><p>Missing lists, historical source identities, the direction setting, or the three-step limit can leave a real connection out. Try another starting account or inspect list coverage below.</p></div>'}${coverageHTML(data.coverage)}`;
    if (!paths.length) return;
    const byId = new Map(data.nodes.map((n) => [n.id, n]));
    paths.forEach((path, i) => {
      const button = document.createElement('button');
      button.className = 'connection-route';
      button.type = 'button';
      button.setAttribute('aria-pressed', String(i === 0));
      const intermediates = path.nodes.slice(1, -1).map((id) => handle(byId.get(id))).join(', ');
      button.innerHTML = `<b>${path.length === 1 ? 'Direct observation' : esc(intermediates)}</b><span>${int(path.length)} ${path.length === 1 ? 'step' : 'steps'} · ${int(path.reciprocal_hops)} with follows recorded both ways</span>`;
      button.addEventListener('click', () => {
        el('routes').querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b === button)));
        renderPath(data, path, byId);
      });
      el('routes').append(button);
    });
    const foot = document.createElement('p');
    foot.className = 'connection-note';
    foot.textContent = `Ordered by fewer steps, then follows recorded both ways. This is not a relationship score. Showing ${paths.length} of ${search.paths_found ?? paths.length} paths found${isTruncated ? ' in a limited search' : ''}.`;
    el('routes').after(foot);
    renderPath(data, paths[0], byId);
  }

  function renderPath(data, path, byId) {
    const edgeMap = new Map(data.edges.map((edge) => [edge.id, edge]));
    const hopEdge = (hop) => typeof hop.edge === 'object' ? hop.edge : edgeMap.get(hop.edge);
    let chain = '';
    for (let i = 0; i < path.nodes.length; i++) {
      const account = byId.get(path.nodes[i]);
      if (i) {
        const hop = path.hops[i - 1], edge = hopEdge(hop);
        const forward = edge?.from === path.nodes[i - 1];
        const arrow = hop.reciprocal ? '↔' : forward ? '→' : '←';
        const title = hop.reciprocal ? 'Follows recorded in both directions' : `${handle(byId.get(edge?.from))} follows ${handle(byId.get(edge?.to))}`;
        chain += `<span class="connection-arrow" role="img" aria-label="${esc(title)}" title="${esc(title)}">${arrow}</span>`;
      }
      chain += `<div class="connection-node">${accountLink(account)}<small>${legacy(account) ? 'Unidentified record' : account?.handle_basis ? 'Historical ID ' + esc(account.ig_id) : i === 0 ? 'Start' : i === path.nodes.length - 1 ? 'Target' : 'Intermediate account'}</small></div>`;
    }
    const evidenceHTML = (edge) => !edge ? '' : `<div class="connection-hop"><h4>${esc(handle(byId.get(edge.from)))} → ${esc(handle(byId.get(edge.to)))}</h4>${edge.evidence.map((e) => `<p>@${esc(e.seed)} · ${esc(e.direction)} list · first recorded ${date(e.first_seen)}${e.last_seen ? ' · last recorded ' + date(e.last_seen) : ' · latest recording unknown'}${e.seed_ig_id ? '' : ' · source identity not captured'}${e.person_ig_id ? '' : ' · member identity not captured'}</p>`).join('')}</div>`;
    const used = new Set();
    let evidence = '';
    for (const hop of path.hops) {
      const edge = hopEdge(hop);
      if (edge && !used.has(edge.id)) { evidence += evidenceHTML(edge); used.add(edge.id); }
      if (hop.reciprocal && edge) {
        const reverse = data.edges.find((e) => e.from === edge.to && e.to === edge.from);
        if (reverse && !used.has(reverse.id)) { evidence += evidenceHTML(reverse); used.add(reverse.id); }
      }
    }
    el('path').innerHTML = `<h3>Follow directions on this path</h3><div class="connection-chain" aria-label="Recorded follow path">${chain}</div><p class="connection-note">Arrows show who follows whom, even when exploring a path backwards.</p><div class="connection-evidence"><h3>What was recorded</h3>${evidence}<p class="connection-note">Dates are when the server recorded the evidence. A later import does not prove uninterrupted following.</p></div>`;
  }
})();
