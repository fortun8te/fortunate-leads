/* A focused, evidence-first comparison. Intentionally independent of the canvas. */
(() => {
  'use strict';
  const panel = document.getElementById('connections-panel');
  const toggle = document.getElementById('connections-toggle');
  const mapPane = document.getElementById('pane-map');
  if (!panel || !toggle) return;
  const el = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const handle = node => '@' + (node?.handle || node?.id || 'unknown');
  const normalize = value => value.trim().replace(/^@/, '').toLowerCase();
  const date = value => {
    if (!value) return 'unknown';
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? 'unknown' : d.toLocaleString();
  };
  const heading = el('h2', 'Compare profiles');
  const intro = el('p', 'See direct follows and shared accounts in the lists collected so far. A follow does not prove friendship or willingness to introduce you. Comparison uses all collected lists; overview filters do not apply.', 'connections-note');
  const form = el('form', undefined, 'connections-form');
  const inputs = ['Starting profile', 'Target profile'].map((title, i) => {
    const label = el('label', title);
    const input = el('input', undefined, 'input');
    input.id = i ? 'connection-target' : 'connection-source';
    input.name = i ? 'target' : 'source';
    input.placeholder = i ? '@target' : '@startingprofile';
    input.required = true;
    input.maxLength = 31;
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.setAttribute('autocapitalize', 'none');
    label.append(input);
    form.append(label);
    return input;
  });
  const submit = el('button', 'Compare', 'btn solid');
  submit.type = 'submit';
  form.append(submit);
  const status = el('p', 'Enter two handles to compare collected evidence.', 'connections-note');
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  const results = el('div', undefined, 'connections-results');
  panel.append(heading, intro, form, status, results);
  let request = 0;
  let controller;
  toggle.addEventListener('click', () => {
    panel.hidden = !panel.hidden;
    toggle.setAttribute('aria-expanded', String(!panel.hidden));
    toggle.classList.toggle('on', !panel.hidden);
    mapPane?.classList.toggle('comparing', !panel.hidden);
    toggle.textContent = panel.hidden ? 'Compare profiles' : 'Back to overview';
    if (!panel.hidden) inputs[0].focus();
  });

  function evidenceDetails(links, nodes) {
    const details = el('details', undefined, 'connections-evidence');
    details.append(el('summary', 'Source-list evidence'));
    const list = el('ul');
    const byId = new Map(nodes.map(node => [node.id, node]));
    for (const link of links) {
      const from = handle(byId.get(link.source) || { id: link.source });
      const to = handle(byId.get(link.target) || { id: link.target });
      const item = el('li');
      item.append(el('strong', `${from} follows ${to}`));
      for (const observation of link.evidence || []) {
        const seed = String(observation.seed || 'unknown').replace(/^@/, '');
        item.append(el('p', `Source: @${seed} · ${observation.direction || 'unknown'} list. First observed: ${date(observation.first_observed)}. Last observed: ${date(observation.last_observed)}.`));
        item.append(el('p', `Recorded observations: ${observation.observation_count ?? 'unknown'}. Latest import job: ${observation.last_job_id ?? 'unknown'}; page: ${observation.last_page_key ?? 'unknown'}.`));
      }
      if (!link.evidence?.length) item.append(el('p', 'Source-list details unavailable.'));
      list.append(item);
    }
    details.append(list);
    return details;
  }

  function diagram(nodes, links) {
    const ns = 'http://www.w3.org/2000/svg';
    const svgEl = (tag, attrs, text) => {
      const node = document.createElementNS(ns, tag);
      for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
      if (text !== undefined) node.textContent = text;
      return node;
    };
    const svg = svgEl('svg', { viewBox: '0 0 660 145', role: 'img', 'aria-label': 'Observed follows. Each arrow points from the follower to the account they follow.', class: 'connections-diagram' });
    const defs = svgEl('defs', {});
    const marker = svgEl('marker', { id: 'connection-arrow', viewBox: '0 0 10 10', refX: '9', refY: '5', markerWidth: '7', markerHeight: '7', orient: 'auto-start-reverse' });
    marker.append(svgEl('path', { d: 'M 0 0 L 10 5 L 0 10 z', fill: 'currentColor' }));
    defs.append(marker);
    svg.append(defs);
    const positions = new Map(nodes.map((node, i) => [node.id, { x: nodes.length === 2 ? 145 + i * 370 : 80 + i * 250, y: 60 }]));
    for (const link of links) {
      const a = positions.get(link.source), b = positions.get(link.target);
      if (!a || !b) continue;
      const direction = Math.sign(b.x - a.x);
      const reciprocal = links.some(other => other.source === link.target && other.target === link.source);
      const y = a.y + (reciprocal ? direction * -10 : 0);
      svg.append(svgEl('line', { x1: a.x + direction * 30, y1: y, x2: b.x - direction * 33, y2: y, stroke: 'currentColor', 'stroke-width': '2', 'marker-end': 'url(#connection-arrow)' }));
    }
    for (const node of nodes) {
      const p = positions.get(node.id);
      svg.append(svgEl('circle', { cx: p.x, cy: p.y, r: '25', class: 'connections-node' }));
      svg.append(svgEl('text', { x: p.x, y: p.y + 5, 'text-anchor': 'middle', class: 'connections-node-initial' }, (node.handle || '?').slice(0, 1).toUpperCase()));
      // Full handles remain available below; shortened labels keep this small drawing legible.
      const full = handle(node);
      svg.append(svgEl('text', { x: p.x, y: '111', 'text-anchor': 'middle' }, full.length > 19 ? full.slice(0, 17) + '…' : full));
    }
    return svg;
  }

  function render(data) {
    results.replaceChildren();
    const connectors = data.connectors || [];
    const direct = data.direct_relationships || [];
    const total = data.total_candidates ?? connectors.length;
    status.textContent = `${direct.length} direct follow${direct.length === 1 ? '' : 's'} observed · Showing ${connectors.length} of ${total} shared accounts${data.truncated ? ' (limited to the top results)' : ''}.`;
    results.append(el('p', 'Experimental ordering: follow patterns first; fewer observed connections breaks ties. Not a friendship score.', 'connections-note'));
    if (!connectors.length && !direct.length) {
      results.append(el('p', 'No direct follows or shared accounts were observed in the collected lists. The relationship is unknown; missing or incomplete lists can hide connections.', 'connections-empty'));
    }
    const layout = el('div', undefined, 'connections-layout');
    const choices = el('div', undefined, 'connections-choices');
    choices.setAttribute('aria-label', 'Observed connection patterns');
    const detail = el('section', undefined, 'connections-detail');
    detail.setAttribute('aria-label', 'Selected connection evidence');
    const options = [];
    if (direct.length) options.push({ title: 'Direct follows', nodes: [data.source, data.target], links: direct });
    for (const connector of connectors) {
      options.push({ title: handle(connector.node), nodes: [data.source, connector.node, data.target], links: connector.links || [], connector });
    }
    const names = {
      reciprocal_support: 'Two-way follows on both sides',
      directed_path: 'Starting profile follows this account; this account follows the target',
      reverse_path: 'Target follows this account; this account follows the starting profile',
      shared_follower: 'This account follows both profiles',
      shared_followee: 'Both profiles follow this account'
    };
    function select(option, button) {
      for (const child of choices.children) child.setAttribute('aria-pressed', String(child === button));
      detail.replaceChildren(el('h3', option.title), diagram(option.nodes, option.links));
      detail.append(el('p', 'Arrows mean “follows”. They show the direction observed in collected lists.', 'connections-note'));
      const byId = new Map(option.nodes.map(node => [node.id, node]));
      const directions = el('ul');
      for (const link of option.links) directions.append(el('li', `${handle(byId.get(link.source) || { id: link.source })} → ${handle(byId.get(link.target) || { id: link.target })} (follows)`));
      detail.append(directions);
      if (option.connector?.motifs?.length) {
        const patterns = el('details', undefined, 'connections-evidence');
        patterns.append(el('summary', 'All observed follow patterns'));
        const list = el('ul');
        for (const motif of option.connector.motifs) list.append(el('li', names[motif] || motif));
        patterns.append(list);
        detail.append(patterns);
      }
      if (option.connector?.manual_known) detail.append(el('p', 'Michael tagged this account as someone he knows. This does not mean they know the target.', 'connections-note'));
      if (option.connector) detail.append(el('p', `${option.connector.rank?.observed_degree ?? 'Unknown'} observed connections in this dataset. This is not their Instagram follower count.`, 'connections-note'));
      detail.append(evidenceDetails(option.links, option.nodes));
    }
    for (const option of options) {
      const button = el('button', undefined, 'connections-choice');
      button.type = 'button';
      button.append(el('strong', option.title));
      if (option.connector) {
        const motifs = option.connector.motifs || [];
        if (motifs.length) button.append(el('span', names[motifs[0]] || motifs[0]));
        if (motifs.length > 1) button.append(el('span', `+${motifs.length - 1} more follow pattern${motifs.length === 2 ? '' : 's'}`));
        if (option.connector.manual_known) button.append(el('span', 'Tagged: Michael knows this account'));
      } else button.append(el('span', 'Follow evidence between these two profiles'));
      button.setAttribute('aria-pressed', 'false');
      button.addEventListener('click', () => select(option, button));
      choices.append(button);
    }
    if (options.length) {
      layout.append(choices, detail);
      results.append(layout);
      select(options[0], choices.firstElementChild);
    }
    const coverage = el('details', undefined, 'connections-evidence');
    coverage.append(el('summary', 'Collection coverage and limits'));
    coverage.append(el('p', 'Coverage describes collection progress, not a guarantee that Instagram returned every account. First observed is an import timestamp, not the date a follow began. Unknown last observation means freshness is not established.'));
    const rows = el('ul');
    const coverageNames = { uncollected: 'Not collected', partial: 'Partially collected', count_mismatch: 'Recorded and reported counts differ', reported_complete: 'Collection reported complete' };
    const stateNames = { done: 'Finished', pending: 'Waiting', queued: 'Queued', running: 'Collecting', active: 'Collecting', paused: 'Paused', error: 'Collection error', failed: 'Collection failed', blocked: 'Collection blocked' };
    for (const row of data.coverage || []) rows.append(el('li', `@${String(row.seed || 'unknown').replace(/^@/, '')} · ${row.direction || 'unknown'}: ${coverageNames[row.status] || 'Coverage unknown'} · ${row.received ?? '?'} recorded across imports; ${row.total ?? 'unknown'} total reported · collection: ${stateNames[row.state] || (row.state ? String(row.state).replaceAll('_', ' ') : 'unknown')} · updated ${date(row.updated_at)}`));
    coverage.append(rows);
    for (const limitation of data.limitations || []) coverage.append(el('p', typeof limitation === 'string' ? limitation : (limitation.message || limitation.code || 'Additional data limitations apply.')));
    results.append(coverage);
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    const current = ++request;
    controller?.abort();
    panel.removeAttribute('aria-busy');
    const source = normalize(inputs[0].value), target = normalize(inputs[1].value);
    results.replaceChildren();
    if (![source, target].every(value => /^[a-z0-9._]{1,30}$/.test(value)) || source === target) {
      status.textContent = 'Enter two different Instagram handles (letters, numbers, dots or underscores).';
      return;
    }
    if (new URLSearchParams(location.search).get('mock') === '1') {
      status.textContent = 'Comparison requires collected data. Open the app without demo mode to compare real profiles.';
      return;
    }
    controller = new AbortController();
    status.textContent = 'Comparing collected follow evidence…';
    panel.setAttribute('aria-busy', 'true');
    try {
      const query = new URLSearchParams({ source, target, limit: '20' });
      const response = await fetch(`/api/connections?${query}`, { signal: controller.signal });
      const data = await response.json();
      if (current !== request) return;
      if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : 'Comparison is unavailable. Please try again.');
      if (!data.source || !data.target || !Array.isArray(data.connectors)) throw new Error('The comparison response was incomplete. Please try again.');
      render(data);
    } catch (error) {
      if (current !== request || error.name === 'AbortError') return;
      status.textContent = `Could not compare profiles: ${error.message}. No conclusion about their connection can be drawn.`;
    } finally {
      if (current === request) panel.removeAttribute('aria-busy');
    }
  });
})();
