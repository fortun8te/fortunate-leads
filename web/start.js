// Get started: a live checklist (shown only while something is missing) and the one-handle flow.
// Pure text builders are exported on window.StartView so tests can run them without a browser.
(() => {
  const E = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const N = (n) => Number(n || 0).toLocaleString('en-US');
  const DOT = { ok: 'live', todo: 'need', warn: 'hollow', wait: 'hollow', bad: 'need' };

  function stepHTML(s) {
    const a = s.action;
    const btn = !a ? '' : a.href ? `<a class="btn" href="${E(a.href)}" target="_blank" rel="noopener">${E(a.label)}</a>`
      : a.kind === 'wizard' ? `<button class="btn solid" data-st-wizard>${E(a.label)}</button>`
      : a.kind === 'copy' ? `<button class="btn" data-st-copy="${E(a.copy)}">${E(a.label)}</button>`
      : `<a class="btn${s.status === 'todo' && !s.optional ? ' solid' : ''}" href="${E(a.route)}">${E(a.label)}</a>`;
    const skip = s.optional && s.status !== 'ok' ? `<button class="btn ghost" data-st-skip="${E(s.id)}">Skip</button>` : '';
    const items = (s.items || []).length > 1 || (s.items || []).some((i) => i.state !== 'ok')
      ? `<ul class="st-items">${s.items.map((i) => `<li><i class="dot ${DOT[i.state] || ''}"></i><span>${E(i.name)}</span><span class="muted">${E(i.text)}</span></li>`).join('')}</ul>` : '';
    return `<li class="st-step" data-step="${E(s.id)}" data-status="${E(s.status)}">
      <i class="dot ${DOT[s.status] || ''}" aria-hidden="true"></i>
      <div class="st-txt"><b>${E(s.title)}${s.optional ? ' <span class="muted">optional</span>' : ''}</b><p class="muted">${E(s.detail)}</p>${items}</div>
      <div class="st-act">${skip}${btn}</div></li>`;
  }
  function collectionHTML(control) {
    const stages = control?.stages?.filter(s => ['lists', 'bios'].includes(s.id)) || [];
    if (stages.length !== 2) return '<p class="st-headline" role="status">Collection status unavailable.</p>';
    return `<div class="st-stage-progress">${stages.map(s => `<div><b>${s.id === 'lists' ? 'Lists' : 'Bios'} <span class="muted">${E(({idle:'Ready',running:'Collecting',waiting:'Waiting',paused:'Stopped',stopping:'Stopping…'})[s.state] || 'Unknown')}</span></b><p class="muted">${E(s.attention?.message || s.now || '')}</p><small class="muted">${N(s.today)} ${s.id === 'lists' ? 'list entries saved today' : 'bios read today'} · ${N(s.queue)} ${s.id === 'lists' ? 'list jobs' : 'bio jobs'} remaining</small></div>`).join('')}</div>`;
  }
  function flowHTML(f, control) {
    const bar = (l) => {
      const pct = l.total ? Math.min(100, Math.round(100 * (l.received || 0) / l.total)) : (l.state === 'done' ? 100 : 0);
      return `<li><span>@${E(l.seed)} <span class="muted">${l.direction === 'followers' ? 'followers' : 'following'}</span></span>
        <span class="bar-p ${l.state === 'done' ? 'done' : l.state === 'running' ? 'run' : l.total ? '' : 'unknown'}"><i style="width:${pct}%"></i></span>
        <span class="num muted">${N(l.received)}${l.total ? ' of ' + N(l.total) : ' saved'}<small>${E(({done:'List ended',running:'Collecting',queued:'Queued',paused:'Stopped',error:'Needs review'})[l.state] || l.state)}${l.error ? ' · Needs review' : ''}</small></span></li>`;
    };
    return `${control ? collectionHTML(control) : ''}<p ${control ? 'hidden' : ''} class="st-headline" role="status" aria-live="polite" data-state="${E(f.state)}">${E(f.state === 'paused' ? 'Collection is stopped. Continue collecting where you left off.' : f.headline)}</p>
      <dl class="st-stats"><div><dt>Saved people</dt><dd class="num">${N(f.people)}</dd></div><div><dt>Saved bios</dt><dd class="num">${N(f.bios)}</dd></div><div><dt>Saved assessments</dt><dd class="num">${N(f.ranked)}</dd></div></dl>
      <p class="st-saved-help muted">Totals across your saved workspace.</p>${f.lists.length ? `<h3>Recent list activity</h3><ul class="st-lists">${f.lists.slice(0, 6).map(bar).join('')}</ul>` : ''}`;
  }
  function leadsHTML(rows) {
    if (!rows.length) return '';
    return `<h3>Best matches so far</h3><ol class="st-leads">${rows.map((r) => `<li><a href="#/leads?q=${encodeURIComponent(r.handle)}"><b>${E(r.name || '@' + r.handle)}</b><span class="muted">@${E(r.handle)}${r.followers != null ? ' · ' + N(r.followers) + ' followers' : ''}</span></a></li>`).join('')}</ol>`;
  }
  window.StartView = { stepHTML, flowHTML, leadsHTML, collectionHTML };

  if (typeof document === 'undefined' || !document.getElementById('view-start')) return;
  const root = document.getElementById('view-start'), tab = document.getElementById('tab-start');
  const St = { data: null, leads: [], timer: 0, busy: false, error: false, landed: false, input: '', pending: null, changing: false, control: null, directions: ['followers', 'following'] };

  function render() {
    const d = St.data;
    if (!d) { root.querySelector('#st-body').innerHTML = `<p class="muted">${St.error ? 'The server is not running. Open ops/start-all.command to start it.' : 'Checking…'}</p>`; return; }
    const stages = St.control?.stages?.filter(s => ['lists','bios'].includes(s.id)) || [];
    const paused = stages.length === 2 && stages.every(s => s.paused), stopping = stages.some(s => s.state === 'stopping' || s.paused && s.active), going = stages.some(s => ['running','waiting'].includes(s.state));
    root.querySelector('#st-body').innerHTML = `
      ${St.error ? '<p class="st-err" role="alert">Cannot update collection status. Showing the last saved details. <button type="button" class="btn" data-st-retry>Retry</button></p>' : ''}
      <section class="panel st-flow"><div class="p-body">
        <div><h2>Find leads from an account</h2>
        <p class="muted">Paste a brand or competitor your customers follow. Choose which lists to collect. Bios are read at Instagram's safe pace.</p></div>
        <fieldset class="st-scope"><legend>Collect</legend>${['followers','following'].map(direction => `<label><input type="checkbox" data-st-direction="${direction}" ${St.directions.includes(direction) ? 'checked' : ''}>${direction === 'followers' ? 'Followers' : 'Following'}</label>`).join('')}</fieldset><form class="st-form" id="st-form"><input class="input" id="st-in" value="${E(St.input)}" aria-label="Instagram handle or link" placeholder="@handle or instagram.com/handle" autocomplete="off" spellcheck="false">
          <button class="btn solid" id="st-go" ${St.error || St.busy || St.changing || !St.directions.length ? 'disabled' : ''}>${St.busy ? 'Starting…' : 'Start collection'}</button>
          ${going || paused || stopping ? `<button type="button" class="btn" id="st-pause" ${St.error || St.changing || stopping ? 'disabled' : ''}>${stopping ? 'Stopping…' : St.changing ? paused ? 'Starting…' : 'Stopping…' : paused ? 'Continue collecting' : 'Stop collecting'}</button>` : ''}</form>
        ${going || paused || stopping ? '<p class="st-control-help muted">Stop keeps your progress. Continue picks up where you left off.</p>' : ''}
        <p class="st-err muted" id="st-err" role="alert" hidden></p>
        ${flowHTML(d.flow, St.control)}${leadsHTML(St.leads)}
      </div></section>
      <section class="panel"><div class="p-head"><h3>Setup</h3><span class="muted">${d.ready ? 'Everything needed is in place.' : `${d.blocking} thing${d.blocking === 1 ? '' : 's'} to fix before collecting.`}</span></div>
        <ul class="st-steps">${d.steps.map(stepHTML).join('')}</ul></section>`;
  }
  root.addEventListener('change', (e) => { if (e.target.dataset?.stDirection) { const direction = e.target.dataset.stDirection; St.directions = e.target.checked ? [...new Set([...St.directions, direction])] : St.directions.filter(d => d !== direction); root.querySelector('#st-go').disabled = St.error || St.busy || St.changing || !St.directions.length; } });
  root.addEventListener('input', (e) => { if (e.target.id === 'st-in') St.input = e.target.value; });
  function badge() {
    const d = St.data;
    const show = !!d && d.open > 0;
    tab.hidden = !show && !location.hash.startsWith('#/start');
    const n = document.getElementById('n-start');
    n.textContent = d && d.blocking ? String(d.blocking) : '';
    n.hidden = !(d && d.blocking);
  }
  async function refresh() {
    if (St.pending) return St.pending;
    St.pending = readStatus();
    try { await St.pending; } finally { St.pending = null; }
  }
  async function readStatus() {
    const wasError = St.error;
    try {
      const r = await fetch('/api/onboarding', {cache:'no-store'});
      if (!r.ok) throw new Error('Unavailable');
      const data = await r.json();
      let control = data.control;
      if (!control) { const c = await fetch('/api/control', {cache:'no-store'}); if (!c.ok) throw new Error('Unavailable'); control = await c.json(); }
      if (control.stages?.filter(s => ['lists','bios'].includes(s.id)).length !== 2) throw new Error('Unavailable');
      St.data = data; St.control = control; St.error = false;
      if (root.classList.contains('on') && St.data.flow.people) {
        const l = await fetch('/api/leads?sort=fit&limit=5', { cache: 'no-store' }).then((x) => x.ok ? x.json() : { rows: [] }).catch(() => ({ rows: [] }));
        St.leads = l.rows || [];
      }
    } catch (e) { St.error = true; }
    badge();
    if (root.classList.contains('on') && !St.busy && (wasError !== St.error || document.activeElement?.id !== 'st-in')) render();
    else if (root.classList.contains('on') && St.data && !document.querySelector('#st-form')) render();
    // First visit with a broken setup and no leads: land here once.
    if (!St.landed && St.data) {
      St.landed = true;
      if (!St.data.ready && !St.data.flow.people && !/^#\/(?!leads\b)/.test(location.hash) && location.hash.indexOf('q=') < 0) location.hash = '#/start';
    }
  }
  function schedule() {
    clearTimeout(St.timer);
    if (!document.hidden) refresh();
    St.timer = setTimeout(schedule, root.classList.contains('on') ? 4000 : 20000);
  }
  async function post(url, body) {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error('Request failed');
    return j;
  }
  root.addEventListener('submit', async (e) => {
    if (e.target.id !== 'st-form') return;
    if (St.error || St.busy || St.changing || !St.directions.length) { e.preventDefault(); return; }
    e.preventDefault();
    St.input = root.querySelector('#st-in').value;
    const handles = window.parseHandles ? parseHandles(St.input) : [];
    const err = root.querySelector('#st-err');
    if (!handles.length) { err.textContent = 'That does not look like an Instagram handle or link.'; err.hidden = false; return; }
    err.hidden = true; St.busy = true; render();
    try {
      const r = await post('/api/start', { handles, directions: St.directions });
      if (window.toast) toast(r.started ? (r.queued ? 'Started. Lists collect at a safe pace.' : 'Already queued. Collection is on.') : 'Lists queued. Check collection status before starting.');
      window.dispatchEvent(new Event('fl:control-changed'));
      St.busy = false; await refresh();
    } catch (x) { St.busy = false; render(); const e2 = root.querySelector('#st-err'); e2.textContent = "Couldn't start collection. Try again."; e2.hidden = false; }
  });
  root.addEventListener('click', async (e) => {
    const t = e.target;
    if (t.closest('[data-st-retry]')) return refresh();
    if (t.closest('#st-pause')) {
      if (St.error || St.changing || St.busy) return;
      St.changing = true; render();
      const paused = St.control?.stages?.filter(s => ['lists','bios'].includes(s.id)).every(s => s.paused);
      try { const result = await post('/api/control', { stage: 'collection', action: paused ? 'resume' : 'pause' }); const stages = result.stages?.filter(s => ['lists','bios'].includes(s.id)); if (stages?.length !== 2 || stages.some(s => s.paused !== !paused)) throw new Error('Unconfirmed'); St.control = result; window.dispatchEvent(new Event('fl:control-changed')); } catch (x) { if (window.toast) toast(paused ? "Couldn't continue collecting. Try again." : "Couldn't stop collecting. Try again."); }
      St.changing = false; await refresh(); return;
    }
    const skip = t.closest('[data-st-skip]');
    if (skip) { try { await post('/api/onboarding', { skip: skip.dataset.stSkip }); } catch (x) { if (window.toast) toast("Couldn't save setup. Try again."); } refresh(); return; }
    const copy = t.closest('[data-st-copy]');
    if (copy) { if (window.copyText) copyText(copy.dataset.stCopy, copy); return; }
    if (t.closest('[data-st-wizard]')) { location.hash = '#/accounts'; setTimeout(() => document.getElementById('acc-add')?.click(), 50); }
  });
  window.Start = { show() { render(); refresh(); clearTimeout(St.timer); St.timer = setTimeout(schedule, 4000); }, refresh };
  window.addEventListener('fl:control-changed', () => { if (!St.busy && !St.changing) refresh(); });
  window.addEventListener('hashchange', badge);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) schedule(); });
  schedule();
})();
