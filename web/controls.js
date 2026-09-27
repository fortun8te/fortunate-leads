// Control strip: always on top of every page. Shows what each stage (Collect lists, Read bios, AI scoring) is doing right
// now in one sentence, with a pause/resume button per stage and a "Stop all". Self-contained: reads GET /api/control every
// 5 s, writes POST /api/control. It lives inside the existing status area and never touches app.js state.
(() => {
  if (window.__flControls) return;
  window.__flControls = true;
  const POLL = 5e3;
  const n = (v) => Math.round(v || 0).toLocaleString('en-US');
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const SHORT = { lists: 'Lists', bios: 'Bios', ai: 'AI' };
  const PER = { lists: 'people', bios: 'bios', ai: 'scores' };
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

  function clock(sec) {
    if (sec == null) return '';
    sec = Math.max(0, Math.round(sec));
    return sec < 60 ? sec + ' s' : sec < 3600 ? Math.ceil(sec / 60) + ' min' : (sec / 3600).toFixed(1) + ' h';
  }
  // live seconds left, counted down between polls
  function left(s) {
    if (!s.wait || s.wait.seconds == null) return null;
    return s.wait.seconds - (Date.now() - data.got) / 1e3;
  }
  // The stage pill is the single compact place for a wait reason and its countdown.
  function waitWord(why, sec, clockFn) {
    why = why || '';
    const back = sec == null ? '' : sec > 3600
      ? ' · back ' + new Date(Date.now() + sec * 1e3).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : ' · ' + clockFn(sec);
    if (/daily/i.test(why)) return 'daily cap' + back;
    if (/slow down|instagram limit/i.test(why)) return 'Instagram limit' + back;
    if (/log in/i.test(why)) return 'needs login';
    if (/security check/i.test(why)) return 'security check';
    if (/break|between requests/i.test(why)) return 'request gap' + (sec == null ? '' : ' · next in ' + clockFn(sec));
    return 'waiting' + back;
  }
  function word(s) {
    if (offline) return 'last known: ' + s.state;
    if (s.state === 'paused') return 'paused';
    if (s.state === 'waiting') {
      return waitWord(s.wait?.why, left(s), clock);
    }
    if (s.state === 'idle') return /no instagram account/i.test(s.now) ? 'no account online' : /paused or offline/i.test(s.now) ? 'no account free' : 'nothing to do';
    if (s.state !== 'running') return ({off: 'off', error: 'needs attention', failed: 'failed', starting: 'starting'})[s.state] || 'status unavailable';
    if (s.id === 'ai') return `running · ${n(s.minute)}/min · ${n(s.hour)}/h`;
    return 'running' + (s.hour ? ' · ' + n(s.hour) + '/h' : '');
  }
  function tip(s) {
    const minute = s.id === 'ai' ? `Last minute ${n(s.minute)} scores · ` : '';
    return `${s.label}\n\n${s.help}\n\n${n(s.queue)} waiting.\n${minute}Last hour ${n(s.hour)} ${PER[s.id]} · today ${n(s.today)}.`;
  }

  function render() {
    mount();
    const active = el.contains(document.activeElement) ? document.activeElement : null;
    const focusStage = active?.dataset.stage;
    if (!data) { el.innerHTML = `<span class="fl-ctl-msg">${offline ? 'Server offline, controls unavailable.' : 'Loading controls…'}</span>`; return; }
    const pills = data.stages.map((s) => {
      const on = !offline && s.state === 'running';
      const act = s.paused ? 'resume' : 'pause';
      const what = s.paused ? `Resume ${s.label.toLowerCase()}` : `Pause ${s.label.toLowerCase()}`;
      return `<div class="fl-ctl-pill ${esc(s.state)}" role="group" aria-label="${esc(s.label + ': ' + word(s) + '. ' + tip(s).replace(/\n+/g, ' '))}" title="${esc(tip(s))}">
        <i class="fl-ctl-dot${on ? ' on' : ''}"></i><b>${SHORT[s.id]}</b><span class="fl-ctl-word">${esc(word(s))}</span>
        <button class="fl-ctl-btn" data-stage="${s.id}" data-action="${act}" aria-label="${esc(what)}" title="${esc(what + '. ' + s.help)}" ${busy || offline ? 'disabled' : ''}>${s.paused ? 'Resume' : 'Pause'}</button>
      </div>`;
    }).join('');
    const running = data.stages.filter((s) => !s.paused).length;
    const failed = data.stages.some((s) => s.state === 'error' || s.state === 'failed');
    const notice = actionError || (offline ? 'Server offline: showing the last known state.' :
      failed ? 'A stage needs attention.' : '');
    const all = data.all_paused
      ? `<button class="fl-ctl-all" data-stage="all" data-action="resume" title="Resume list and bio collection. AI scoring has its own Resume button." ${busy || offline ? 'disabled' : ''}>Resume collection</button>`
      : `<button class="fl-ctl-all stop" data-stage="all" data-action="pause" title="Pause all three: no more Instagram requests and no more AI calls. Nothing is deleted; Resume picks up where it left off." ${busy || offline ? 'disabled' : ''}>Stop all</button>`;
    el.innerHTML = `<div class="fl-ctl-pills">${pills}</div>${all}${notice ? `<span class="fl-ctl-now" role="alert" aria-atomic="true">${esc(notice)}${failed && !offline && !actionError ? ' <a href="#/scraper">Open Scraper for details.</a>' : ''}</span>` : ''}`;
    el.dataset.running = String(running);
    if (focusStage) el.querySelector(`[data-stage="${focusStage}"]`)?.focus({ preventScroll: true });
  }

  function countdown() {   // only the words change between polls, so a button under the pointer is never replaced
    el.querySelectorAll('.fl-ctl-word').forEach((w, i) => { const s = data.stages[i]; if (s) w.textContent = word(s); });
  }
  async function load(afterAction = false) {
    if (busy && !afterAction) return;
    const version = ++requestVersion;
    try {
      const r = await fetch('/api/control', { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      const j = await r.json();
      if (!Array.isArray(j.stages)) throw new Error('Invalid status');
      const key = JSON.stringify(j.stages) + j.all_paused;
      if (version !== requestVersion) return;
      const same = data && !offline && key === data.key;   // unchanged: keep the buttons, just count down
      data = Object.assign(j, { got: Date.now(), key });
      offline = false;
      if (same) return countdown();
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
    // An unchanged response can take load's countdown-only path; always unlock.
    render();
    if (focusStage && document.activeElement === document.body) {
      el.querySelector(`[data-stage="${focusStage}"]`)?.focus({ preventScroll: true });
    }
  }

  el.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-stage]');
    if (!b || b.disabled) return;
    const body = { stage: b.dataset.stage, action: b.dataset.action };
    if (body.stage === 'all' && body.action === 'pause' &&
        !confirm('Stop everything?\n\nNo more list pages, no more bios, no more AI scoring until you resume. Nothing is deleted.')) return;
    send(body);
  });

  function start() {
    render();
    load();
    window.addEventListener('fl:control-changed', () => load());
    clearInterval(timer);
    timer = setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      if (++tick % (POLL / 1e3) === 0) load(); else if (data) countdown();   // countdown every second, fetch every 5 s
    }, 1e3);
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') load(); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
