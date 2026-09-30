// Persistent, server-confirmed controls for collection and the two local engines.
(() => {
  if (window.__flControls) return;
  window.__flControls = true;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const POLL_MS = 5000, DEADLINE_MS = 9000;
  const names = {k2:'K2', laya:'Laya'};
  let control = null, engines = null, local = null, controlError = false, engineError = false;
  let error = '', busy = '', open = false, pollPromise = null, timer = 0, revision = 0;
  const el = document.createElement('div');
  el.className = 'fl-ctl';
  el.setAttribute('role', 'region');
  el.setAttribute('aria-label', 'Engine controls');

  function mount() {
    if (el.isConnected) return;
    (document.querySelector('#stage-controls') || document.querySelector('.app') || document.body).appendChild(el);
  }
  async function request(url, options = {}) {
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), DEADLINE_MS);
    try {
      const response = await fetch(url, {cache:'no-store', ...options, signal:controller.signal});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
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
      detail = `${active ? `Checking @${active} · ` : ''}${reviewed.toLocaleString()} bios reviewed · ${queue.toLocaleString()} waiting${notes ? ` · ${notes.toLocaleString()} notes waiting` : ''}${detail && item.state === 'waiting' ? ` · ${detail}` : ''}`;
    }
    return {label, detail, enabled:!!item.enabled, available:true, modeOff, item};
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
    const detail = attention || (progress?.description || progress?.summary || collection.find(stage => stage.activity || stage.now)?.activity || collection.find(stage => stage.now)?.now || 'Collection of saved list jobs and bios.');
    return {state, detail, paused, available:!controlError && collection.length === 2};
  }
  function render() {
    mount();
    const focus = el.contains(document.activeElement) ? document.activeElement.dataset.focus : '';
    const scroll = el.querySelector('.fl-ctl-panel')?.scrollTop || 0;
    const scrape = scraping(), k2 = engineState('k2'), laya = engineState('laya');
    const states = [scrape.state, laya.label, k2.label];
    const overall = states.includes('Needs attention') ? 'Needs attention' : states.includes('Stopping') ? 'Stopping' : states.includes('Running') ? 'Running' : states.includes('Waiting') ? 'Waiting' : states.every(state => state === 'Off' || state === 'Off in R mode' || state === 'Paused') ? 'Off' : 'Ready';
    const mode = ({R:'Rules',RLAI:'Rules + local AI',RLEAI:'Rules + local + external AI'})[engines?.processing?.mode || control?.processing?.mode] || 'Mode unavailable';
    const engineRow = (id, state) => {
      const verb = state.modeOff ? state.enabled ? 'Exclude' : 'Include' : state.enabled ? 'Turn off' : 'Turn on';
      return `<div class="fl-engine-row"><div class="fl-engine-copy"><strong><span class="fl-state-dot" data-state="${esc(state.item?.state || 'unknown')}"></span>${names[id]} <span class="fl-engine-state">${esc(state.label)}</span></strong><small>${esc(state.detail)}</small></div><button type="button" class="btn fl-engine-action" data-engine="${id}" data-focus="${id}" aria-label="${verb} ${names[id]}${state.modeOff ? ' in AI modes' : ''}" ${busy || !state.available || state.item?.state === 'stopping' ? 'disabled' : ''}>${busy === id ? 'Saving…' : verb}</button></div>`;
    };
    const short = state => state === 'Off in R mode' ? 'Off' : state;
    el.innerHTML = `<div class="fl-ctl-top"><button type="button" class="fl-ctl-summary" data-focus="panel" aria-expanded="${open}" aria-controls="fl-engine-panel"><span class="fl-state-dot" data-state="${esc(overall.toLowerCase())}"></span><span>Engines</span><b>${esc(overall)}</b><span aria-hidden="true" class="fl-ctl-chevron"></span></button><div class="fl-ctl-inline"><span>Scraping <b>${esc(scrape.state)}</b></span><span>Laya <b>${esc(short(laya.label))}</b></span><span>K2 <b>${esc(short(k2.label))}</b></span></div><button type="button" class="btn fl-ctl-direct" data-stage="collection" data-focus="collection" data-action="${scrape.paused ? 'resume' : 'pause'}" ${busy || !scrape.available ? 'disabled' : ''}>${busy === 'collection' ? 'Saving…' : scrape.paused ? 'Start scraping' : 'Pause scraping'}</button></div><div class="fl-ctl-panel" id="fl-engine-panel" ${open ? '' : 'hidden'}><div class="fl-ctl-panel-head"><b>What is running</b><span>${esc(mode)}</span></div><div class="fl-engine-row"><div class="fl-engine-copy"><strong><span class="fl-state-dot" data-state="${esc(scrape.state.toLowerCase())}"></span>Scraping <span class="fl-engine-state">${esc(scrape.state)}</span></strong><small>${esc(scrape.detail)}</small></div><a href="#/accounts">View progress</a></div>${engineRow('laya', laya)}${engineRow('k2', k2)}<div class="fl-ctl-panel-foot"><span>Laya adds ranking hints. K2 checks saved bios and your notes for business fit.</span><a href="#/settings">Mode and K2 setup</a></div>${error || controlError || engineError ? `<p class="fl-ctl-error" role="alert">${esc(error || 'Some statuses could not be confirmed. Retrying…')}</p>` : ''}</div>`;
    el.querySelector('.fl-ctl-panel').scrollTop = scroll;
    if (focus) el.querySelector(`[data-focus="${focus}"]`)?.focus({preventScroll:true});
  }
  async function poll(force = false) {
    if (pollPromise) { const pending = pollPromise; await pending; return force ? poll(true) : undefined; }
    const version = revision;
    pollPromise = (async () => {
      const [c, e, l] = await Promise.allSettled([request('/api/control'), request('/api/engines'), ...(open ? [request('/api/local-processing')] : [])]);
      if (version !== revision) return;
      if (c.status === 'fulfilled' && Array.isArray(c.value.stages)) { control = c.value; controlError = false; } else controlError = true;
      if (e.status === 'fulfilled' && e.value.engines) { engines = e.value; engineError = false; } else engineError = true;
      if (l?.status === 'fulfilled') local = l.value;
      render();
    })();
    try { await pollPromise; }
    finally { pollPromise = null; clearTimeout(timer); timer = setTimeout(() => { if (document.visibilityState === 'visible') poll(); }, POLL_MS); }
  }
  async function act(target, body) {
    if (busy) return;
    busy = target; error = ''; ++revision; render();
    try {
      const result = await request(target === 'collection' ? '/api/control' : '/api/engines', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if (target === 'collection') { if (!Array.isArray(result.stages)) throw new Error('Unconfirmed'); control = result; controlError = false; }
      else { if (!result.engines?.[target] || result.engines[target].enabled !== body.enabled) throw new Error('Unconfirmed'); engines = result; engineError = false; }
      render();
      window.dispatchEvent(new Event('fl:control-changed'));
    } catch { error = `Could not confirm ${target === 'collection' ? 'scraping' : names[target]} change. Check status and try again.`; }
    finally { busy = ''; render(); await poll(true); }
  }
  el.addEventListener('click', event => {
    // Rendering replaces the clicked button before this click reaches document.
    // Keep the outside-click handler from treating that detached button as outside.
    event.stopPropagation();
    const summary = event.target.closest('.fl-ctl-summary');
    if (summary) { open = !open; render(); if (open) poll(true); return; }
    const engine = event.target.closest('[data-engine]');
    if (engine && !engine.disabled) { const id = engine.dataset.engine; act(id, {engine:id, enabled:!engineState(id).enabled}); return; }
    const collection = event.target.closest('[data-stage="collection"]');
    if (collection && !collection.disabled) act('collection', {stage:'collection',action:collection.dataset.action});
  });
  document.addEventListener('click', event => { if (open && !el.contains(event.target)) { open = false; render(); } });
  document.addEventListener('keydown', event => { if (open && event.key === 'Escape') { open = false; render(); el.querySelector('.fl-ctl-summary')?.focus(); } });
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') poll(); });
  window.addEventListener('fl:control-changed', () => poll());
  function start() { render(); poll(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
