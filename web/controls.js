// Scraping action and direct links to the queue and checking settings.
// Reads GET /api/control and writes POST /api/control without touching app.js state.
(() => {
  if (window.__flControls) return;
  window.__flControls = true;
  const POLL = 5e3;
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const SHORT = { collection: 'scraping', lists: 'Lists', bios: 'Bios', ai: 'External AI' };
  let data = null, busy = false, offline = false, timer = 0, tick = 0;
  let requestVersion = 0, actionError = '';

  const el = document.createElement('div');
  el.className = 'fl-ctl';
  el.setAttribute('role', 'region');
  el.setAttribute('aria-label', 'What is running');

  function mount() {
    if (el.isConnected) return;
    const slot = document.querySelector('#stage-controls');
    if (slot) slot.appendChild(el);
    else {
      const app = document.querySelector('.app');
      if (app) app.insertBefore(el, app.firstChild); else document.body.insertBefore(el, document.body.firstChild);
    }
  }

  function render() {
    mount();
    const active = el.contains(document.activeElement) ? document.activeElement : null;
    const focusStage = active?.dataset.stage;
    if (!data) { el.innerHTML = `<span class="fl-ctl-msg">${offline ? 'Server offline, controls unavailable.' : 'Loading scraping status…'}</span>`; return; }
    const running = data.stages.filter((s) => !s.paused).length;
    const failed = data.stages.some((s) => s.state === 'error' || s.state === 'failed');
    const attention = data.instagram_request_attention?.message || data.stages.find((s) => s.attention?.message)?.attention.message || '';
    const notice = [actionError, offline ? 'Server offline: showing the last known state.' : attention || (failed ? 'A stage needs attention.' : '')].filter(Boolean).join(' ');
    const collection = data.stages.filter((s) => s.id === 'lists' || s.id === 'bios');
    const collectionPaused = collection.length === 2 && collection.every((s) => s.paused);
    const collectionAction = collectionPaused ? 'resume' : 'pause';
    const collectionLabel = collectionPaused ? 'Start scraping' : 'Pause scraping';
    const sharedWait = collection.find(s => s.state === 'waiting' && s.wait?.scope === 'workspace')?.wait;
    const resumeAt = sharedWait?.until && Number.isFinite(Date.parse(sharedWait.until))
      ? new Date(sharedWait.until).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : null;
    const collectionState = offline ? 'Unknown' : attention ? 'Needs attention' : collection.every((s) => s.paused) ? 'Off'
      : sharedWait ? `Instagram wait${resumeAt ? ' · until ' + resumeAt : ''}`
      : collection.some((s) => ['error', 'failed'].includes(s.state)) ? 'Needs attention'
      : collection.some((s) => s.state === 'running') ? 'On'
      : collection.some((s) => s.state === 'waiting') ? 'Waiting' : 'Idle';
    const mode = offline ? 'Unknown' : ({R:'R · Rules',RLAI:'RLAI · Local AI',RLEAI:'RLEAI · External AI'})[data.processing?.mode] || 'Unknown';
    el.innerHTML = `<div class="fl-ctl-top">
      <a class="fl-ctl-summary-item fl-ctl-status-link" href="#/accounts" aria-label="Scraping ${collectionState}. View accounts and progress."><span>Scraping</span><b>${collectionState}</b></a>
      <a class="fl-ctl-summary-item fl-ctl-status-link" href="#/settings" title="Change how saved profiles and notes are checked"><span>Mode</span><b>${mode}</b></a>
      <button class="fl-ctl-direct btn" data-stage="collection" data-action="${collectionAction}" title="${collectionLabel}. Local and external checking settings stay unchanged." ${busy || offline || collection.length !== 2 ? 'disabled' : ''}>${busy ? 'Saving…' : collectionLabel}</button></div>${notice ? `<span class="fl-ctl-now" role="alert" aria-atomic="true">${esc(notice)}${attention && !offline ? ' <a href="#/accounts">Check accounts</a>' : failed && !offline && !actionError ? ' <a href="#/accounts">View collection details.</a>' : ''}</span>` : ''}`;
    el.dataset.running = String(running);
    if (focusStage) el.querySelector(`[data-stage="${focusStage}"]`)?.focus({ preventScroll: true });
  }

  async function load(afterAction = false) {
    if (busy && !afterAction) return;
    const version = ++requestVersion;
    try {
      const r = await fetch('/api/control', { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      const j = await r.json();
      if (!Array.isArray(j.stages)) throw new Error('Invalid status');
      const key = JSON.stringify(j.stages) + j.all_paused + j.local_laya + JSON.stringify(j.instagram_request_attention) + JSON.stringify(j.processing);
      if (version !== requestVersion) return;
      const same = data && !offline && key === data.key;   // unchanged: preserve buttons and focus
      data = Object.assign(j, { got: Date.now(), key });
      offline = false;
      if (same) return;
    } catch {
      if (version !== requestVersion) return;
      offline = true;
    }
    render();
  }
  async function send(body) {
    if (busy || offline) return;
    ++requestVersion; // A response from before this action must never replace its result.
    actionError = '';
    // Disabled buttons cannot retain focus while the request is in flight.
    const focusStage = el.contains(document.activeElement) ? document.activeElement.dataset.stage : null;
    busy = true; render();
    try {
      const r = await fetch('/api/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const j = await r.json();
      if (!r.ok || !Array.isArray(j.stages)) throw new Error('Control action failed');
      data = Object.assign(j, { got: Date.now() });
    } catch {
      const target = body.stage === 'all' ? 'all stages' : SHORT[body.stage];
      actionError = `Couldn’t confirm ${body.action} for ${target}. Try again.`;
    }
    // Keep every action locked until the authoritative refresh has settled.
    await load(true);
    busy = false;
    // An unchanged response can skip rendering; always unlock.
    render();
    if (focusStage && document.activeElement === document.body) {
      el.querySelector(`[data-stage="${focusStage}"]`)?.focus({ preventScroll: true });
    }
  }

  el.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-stage]');
    if (!b || b.disabled) return;
    const body = { stage: b.dataset.stage, action: b.dataset.action };
    send(body);
  });

  function start() {
    render();
    load();
    window.addEventListener('fl:control-changed', () => load());
    clearInterval(timer);
    timer = setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      if (++tick % (POLL / 1e3) === 0) load(); // Fetch every five seconds while visible.
    }, 1e3);
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') load(); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
