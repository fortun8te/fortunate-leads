// Persistent, server-confirmed controls for collection and the two local engines.
(() => {
  if (window.__flControls) return;
  window.__flControls = true;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const POLL_MS = 5000, DEADLINE_MS = 9000;
  // The strip speaks in plain words. Model names stay in Settings.
  const names = {k2:'Bio checks', laya:'Ranking hints'};
  let control = null, engines = null, local = null, controlError = false, engineError = false, localError = false;
  let error = '', busy = '', open = false, pollPromise = null, timer = 0, revision = 0;
  const el = document.createElement('div');
  el.className = 'fl-ctl';
  el.setAttribute('role', 'region');
  el.setAttribute('aria-label', 'Collection controls');

  function mount() {
    if (el.isConnected) return;
    (document.querySelector('#stage-controls') || document.querySelector('.app') || document.body).appendChild(el);
  }
  async function request(url, options = {}) {
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), DEADLINE_MS);
    try {
      const response = await fetch(url, {cache:'no-store', ...options, signal:controller.signal});
      if (!response.ok) throw new Error(response.status >= 500 ? 'The server hit a problem.' : 'The server did not accept the request.');
      return await response.json();
    } finally { clearTimeout(deadline); }
  }
  function engineState(id) {
    const item = engines?.engines?.[id];
    if (engineError || !item) return {label:'Unknown', detail:'Status unavailable', enabled:false, available:false};
    const modeOff = engines.processing?.mode === 'R';
    let label = ({off:'Off',starting:'Starting',running:'Running',stopping:'Stopping',waiting:'Waiting'})[item.state] || 'Unknown';
    if (engines.paused && item.enabled) label = item.stop_acknowledged ? 'Paused' : 'Stopping';
    if (modeOff && item.state === 'off') label = 'Off in R mode';
    let detail = item.reason || (item.active ? 'Working now' : item.ready ? 'Ready for saved work' : '');
    if (modeOff) detail = item.enabled ? 'Included when a local or external AI mode is selected.' : 'Excluded from AI modes until you include it.';
    if (id === 'k2' && local && !engineError && !modeOff) {
      const active = local.progress?.active?.handle;
      const queue = Number(local.queue || 0), notes = Number(local.notes_pending || 0), reviewed = Number(local.reviewed || 0);
      detail = `${active ? `Checking @${active}. ` : ''}${reviewed.toLocaleString()} bios reviewed, ${queue.toLocaleString()} waiting${notes ? `, ${notes.toLocaleString()} ${notes === 1 ? 'note' : 'notes'} waiting` : ''}.${detail && item.state === 'waiting' ? ` ${detail}` : ''}`;
    }
    return {label, detail, enabled:!!item.enabled, available:true, modeOff, item};
  }
  function qualification() {
    const mode = engines?.processing?.mode || control?.processing?.mode;
    if (engineError || !mode) return {label:'Status unavailable', disabled:true, detail:'Checking status…'};
    if (mode === 'R') return {label:'Rules only', disabled:true, detail:'Saved profiles are ranked with rules.'};
    const paused = local?.paused ?? engines?.paused;
    const acknowledged = local?.stop_acknowledged ?? engines?.stop_acknowledged;
    const stopping = paused && acknowledged !== true;
    const label = localError ? 'Status unavailable' : stopping ? 'Stopping…' : paused ? 'Stopped' : local?.state === 'working' ? 'Checking' : local?.state === 'ready' ? 'Ready' : local?.state === 'waiting' ? 'Waiting' : local?.state === 'off' ? (engines.external_active || engines.engines?.laya?.active ? 'Checking' : 'Off') : local?.state === 'starting' ? 'Starting…' : local?.state === 'waiting_for_mac' ? 'Waiting for your Mac' : local?.state === 'unavailable' ? 'Needs setup' : 'Checking status…';
    const detail = localError ? 'The checking status could not be refreshed.' : local ? `${Number(local.reviewed || 0).toLocaleString()} bios reviewed · ${Number(local.queue || 0).toLocaleString()} remaining${local.progress?.active?.handle ? ` · Checking @${local.progress.active.handle}` : ''}` : 'Checking saved-work progress…';
    return {label, paused, stopping, detail, disabled:localError || paused === undefined || stopping};
  }
  function scraping() {
    const stages = control?.stages || [];
    const collection = stages.filter(stage => ['lists','bios'].includes(stage.id));
    const paused = collection.length === 2 && collection.every(stage => stage.paused);
    const attention = control?.instagram_request_attention?.message || collection.find(stage => stage.attention?.message)?.attention.message;
    const wait = collection.find(stage => stage.state === 'waiting' && stage.wait?.scope === 'workspace')?.wait;
    const until = wait?.until && Number.isFinite(Date.parse(wait.until)) ? ` until ${new Date(wait.until).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}` : '';
    const state = controlError || collection.length !== 2 ? 'Unknown' : attention ? 'Needs attention' : paused ? collection.some(stage => stage.active || stage.state === 'stopping') ? 'Stopping' : 'Off' : wait ? `Waiting${until}` : collection.some(stage => stage.state === 'running') ? 'Running' : collection.some(stage => stage.state === 'waiting') ? 'Waiting' : 'On';
    const progress = control?.collection?.progress || collection.find(stage => stage.progress)?.progress;
    const detail = attention || (progress?.description || progress?.summary || collection.find(stage => stage.state === 'running')?.now || collection.find(stage => stage.state === 'waiting')?.now || collection.find(stage => stage.activity || stage.now)?.activity || collection.find(stage => stage.now)?.now || 'Reads follower lists and bios at a safe pace.');
    return {state, detail, paused, available:!controlError && collection.length === 2};
  }
  // One calm word for the whole strip. Details live in the popover.
  const OVERALL = {collecting:'Collecting', paused:'Stopped', stopping:'Stopping…', ready:'Ready', needs:'Needs you', waiting:'Waiting', checking:'Checking', unavailable:'Status unavailable'};
  function overallState(scrape) {
    if (!control && !controlError) return 'checking';
    if (!scrape.available) return 'unavailable';
    if (scrape.state === 'Needs attention') return 'needs';
    if (scrape.state === 'Stopping') return 'stopping';
    if (scrape.paused) return 'paused';
    if (/^Waiting/.test(scrape.state)) return 'waiting';
    return scrape.state === 'Running' ? 'collecting' : 'ready';
  }
  const SUMMARY_ICON = '<svg class="ic fl-ctl-chevron" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false"><path d="M4 6.5l4 4 4-4"/></svg>';
  function render() {
    mount();
    const modelsOpen = el.querySelector('.fl-models')?.open || false;
    const focus = el.contains(document.activeElement) ? document.activeElement.dataset.focus : '';
    const scroll = el.querySelector('.fl-ctl-panel')?.scrollTop || 0;
    const qualify = qualification(), scrape = scraping(), k2 = engineState('k2'), laya = engineState('laya');
    const overall = overallState(scrape);
    const dot = state => `<span class="fl-state-dot" data-state="${esc(state)}" aria-hidden="true"></span>`;
    const engineRow = (id, state) => {
      const verb = state.modeOff ? state.enabled ? 'Exclude' : 'Include' : state.enabled ? 'Turn off' : 'Turn on';
      return `<div class="fl-engine-row"><div class="fl-engine-copy"><strong>${dot(state.item?.state || 'unknown')}${names[id]} <span class="fl-engine-state">${esc(state.label)}</span></strong><small>${esc(state.detail)}</small></div><button type="button" class="btn fl-engine-action" data-engine="${id}" data-focus="${id}" aria-label="${verb} ${names[id].toLowerCase()}${state.modeOff ? ' in AI modes' : ''}" ${busy || !state.available || state.item?.state === 'stopping' ? 'disabled' : ''}>${busy === id ? 'Saving…' : verb}</button></div>`;
    };
    const action = busy === 'collection' ? scrape.paused ? 'Starting…' : 'Stopping…' : scrape.state === 'Stopping' ? 'Stopping…' : scrape.paused ? 'Continue collecting' : 'Stop collecting';
    const wait = scrape.paused ? 'Continue collecting' : 'Stop collecting';
    const html = `<div class="fl-ctl-top"><button type="button" class="fl-ctl-summary" data-focus="panel" aria-expanded="${open}" aria-controls="fl-engine-panel" aria-label="${esc(OVERALL[overall])}. Show details">${dot(overall)}<span class="fl-ctl-name">Collection</span><span class="fl-ctl-word">${esc(OVERALL[overall])}</span>${SUMMARY_ICON}</button><span class="fl-ctl-current">${esc(scrape.state === 'Off' ? 'Progress is saved.' : scrape.detail)}</span><span class="grow"></span><button type="button" class="btn fl-ctl-direct" data-stage="collection" data-focus="collection" data-action="${scrape.paused ? 'resume' : 'pause'}" aria-label="${wait}" ${busy || !scrape.available || scrape.state === 'Stopping' ? 'disabled' : ''}>${action}</button></div>`
      + `<div class="fl-ctl-panel" id="fl-engine-panel" role="group" aria-label="What is running" ${open ? '' : 'hidden'}><div class="fl-ctl-panel-head"><b>Activity</b><a href="#/accounts">View progress</a></div>`

      + `<div class="fl-engine-row"><div class="fl-engine-copy"><strong>Qualification <span class="fl-engine-state">${esc(qualify.label)}</span></strong><small>${esc(qualify.detail)}</small></div>${qualify.label === 'Rules only' ? '<a href="#/settings">AI settings</a>' : `<button type="button" class="btn fl-engine-action" data-qualification data-focus="qualification" ${busy || qualify.disabled ? 'disabled' : ''}>${busy === 'qualification' ? 'Saving…' : qualify.stopping ? 'Stopping…' : qualify.paused ? 'Continue checks' : 'Stop checks'}</button>`}</div>${(control?.stages || []).filter(s => ['lists','bios'].includes(s.id)).map(s => `<div class="fl-engine-row"><div class="fl-engine-copy"><strong>${s.id === 'lists' ? 'Follower and following lists' : 'Instagram bios'} <span class="fl-engine-state">${esc(({idle:'Ready',paused:'Stopped',running:'Collecting',waiting:'Waiting',stopping:'Stopping…'})[s.state] || 'Unknown')}</span></strong><small>${esc(s.now || '')}</small><small>${Number(s.today || 0).toLocaleString()} ${s.id === 'lists' ? 'list entries saved today' : 'bios read today'} · ${Number(s.queue || 0).toLocaleString()} ${s.id === 'lists' ? 'list jobs' : 'bio jobs'} remaining</small></div></div>`).join('')}<details class="fl-models" ${modelsOpen ? 'open' : ''}><summary>Model controls</summary>${engineRow('laya', laya)}${engineRow('k2', k2)}<div class="fl-ctl-panel-foot"><span>Checks use saved profiles.</span><a href="#/settings">AI settings</a></div></details>`
      + `${error || controlError || engineError ? `<p class="fl-ctl-error" role="alert">${esc(error || "Some statuses couldn't be confirmed. Retrying…")}</p>` : ''}</div>`;
    if (el.innerHTML === html) return;
    el.innerHTML = html;
    el.querySelector('.fl-ctl-panel').scrollTop = scroll;
    if (focus) el.querySelector(`[data-focus="${focus}"]`)?.focus({preventScroll:true});
  }
  async function poll(force = false) {
    if (pollPromise) { const pending = pollPromise; await pending; return force ? poll(true) : undefined; }
    const version = revision;
    pollPromise = (async () => {
      const [c, e, l] = await Promise.allSettled([request('/api/control'), request('/api/engines'), ...((open || engines?.processing?.mode && engines.processing.mode !== 'R') ? [request('/api/local-processing')] : [])]);
      if (version !== revision) return;
      if (c.status === 'fulfilled' && Array.isArray(c.value.stages)) { control = c.value; controlError = false; } else controlError = true;
      if (e.status === 'fulfilled' && e.value.engines) { engines = e.value; engineError = false; } else engineError = true;
      if (l) { localError = l.status !== 'fulfilled'; local = l.status === 'fulfilled' ? l.value : null; }
      render();
    })();
    try { await pollPromise; }
    finally { pollPromise = null; clearTimeout(timer); timer = setTimeout(() => { if (document.visibilityState === 'visible') poll(); }, POLL_MS); }
  }
  async function act(target, body) {
    if (busy) return;
    busy = target; error = ''; ++revision; render();
    try {
      const result = await request(target === 'collection' ? '/api/control' : target === 'qualification' ? '/api/local-processing' : '/api/engines', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if (target === 'collection') {
        const stages = result.stages?.filter(stage => ['lists', 'bios'].includes(stage.id));
        if (stages?.length !== 2 || stages.some(stage => stage.paused !== (body.action === 'pause'))) throw new Error('Unconfirmed');
        control = result; controlError = false;
      }
      else if (target === 'qualification') { if (result.paused !== body.paused) throw new Error('Unconfirmed'); local = result; localError = false; }
      else { if (!result.engines?.[target] || result.engines[target].enabled !== body.enabled) throw new Error('Unconfirmed'); engines = result; engineError = false; }
      render();
      window.dispatchEvent(new Event('fl:control-changed'));
    } catch { open = true; error = `Couldn't confirm ${target === 'collection' ? 'collection' : target === 'qualification' ? 'bio checks' : names[target].toLowerCase()} change. Check status and try again.`; }
    finally { busy = ''; render(); await poll(true); }
  }
  el.addEventListener('click', event => {
    // Rendering replaces the clicked button before this click reaches document.
    // Keep the outside-click handler from treating that detached button as outside.
    event.stopPropagation();
    const summary = event.target.closest('.fl-ctl-summary');
    if (summary) { open = !open; render(); if (open) poll(true); return; }
    const qualifier = event.target.closest('[data-qualification]');
    if (qualifier && !qualifier.disabled) { act('qualification', {paused:!qualification().paused}); return; }
    const engine = event.target.closest('[data-engine]');
    if (engine && !engine.disabled) { const id = engine.dataset.engine; act(id, {engine:id, enabled:!engineState(id).enabled}); return; }
    const collection = event.target.closest('[data-stage="collection"]');
    if (collection && !collection.disabled) act('collection', {stage:'collection',action:collection.dataset.action});
  });
  document.addEventListener('click', event => { if (open && !el.contains(event.target)) { open = false; render(); } });
  document.addEventListener('keydown', event => { if (open && event.key === 'Escape') { open = false; render(); el.querySelector('.fl-ctl-summary')?.focus(); } });
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') poll(); });
  window.addEventListener('fl:control-changed', () => { if (!busy) poll(true); });
  function start() { render(); poll(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
