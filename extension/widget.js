// Isolated world: a small status widget on instagram.com (bottom-right). Closed Shadow DOM, so Instagram's CSS can't
// reach it and ours can't leak. Data comes only from the service worker (chrome.runtime messaging): zero Instagram requests.
(() => {
  if (window.__flWidget || window.top !== window) return;
  window.__flWidget = true;
  const host = document.createElement('fl-widget');
  host.style.cssText = 'all:initial;position:fixed;right:16px;bottom:16px;z-index:2147483646;display:block;';
  const root = host.attachShadow({ mode: 'closed' });
  root.innerHTML = `<style>
:host { all: initial; }
* { box-sizing: border-box; margin: 0; }
.w { font: 12px/1.4 Inter, -apple-system, system-ui, sans-serif; color: #ededed; -webkit-font-smoothing: antialiased; font-variant-numeric: tabular-nums; }
.pill { display: flex; align-items: center; gap: 7px; height: 32px; padding: 0 12px 0 10px; border-radius: 999px; border: 1px solid #333;
  background: #161616; color: #ededed; font: inherit; cursor: pointer; box-shadow: 0 4px 16px rgba(0,0,0,.35); }
.pill:hover { background: #1f1f1f; }
.card { width: 304px; border: 1px solid #333; border-radius: 12px; background: #161616; padding: 12px; box-shadow: 0 8px 28px rgba(0,0,0,.45); }
.top { display: flex; align-items: center; gap: 8px; }
.name { flex: 1; font-weight: 600; font-size: 13px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #5a5a5a; flex: none; }
.dot.on { background: #22c55e; }
.x { border: 0; background: none; color: #8a8a8a; font: 16px/1 inherit; cursor: pointer; padding: 2px 4px; border-radius: 6px; }
.x:hover { color: #ededed; background: #262626; }
.now { color: #bdbdbd; margin: 8px 0 10px; min-height: 17px; }
.nums { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-bottom: 10px; }
.nums div { background: #1f1f1f; border-radius: 8px; padding: 6px 8px; }
.nums b { display: block; font-size: 14px; font-weight: 600; }
.nums span { color: #8a8a8a; font-size: 11px; }
.row { display: flex; gap: 6px; }
.btn { flex: 1; white-space: nowrap; height: 28px; border-radius: 8px; border: 1px solid #333; background: #1f1f1f; color: #ededed; font: inherit; cursor: pointer; }
.btn:hover { background: #2a2a2a; }
.stages { display: grid; gap: 4px; margin-bottom: 10px; }
.stage { display: flex; align-items: center; gap: 7px; height: 30px; padding: 0 4px 0 8px; background: #1f1f1f; border-radius: 8px; }
.stage b { font-weight: 600; width: 34px; }
.stage .word { flex: 1; color: #a3a3a3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.stage.paused .word { color: #707070; }
.sbtn { height: 22px; padding: 0 9px; border-radius: 999px; border: 1px solid #333; background: #262626; color: #ededed; font: inherit; cursor: pointer; }
.sbtn:hover { border-color: #707070; }
.sbtn:disabled { opacity: .5; cursor: wait; }
.lbl { color: #8a8a8a; font-size: 11px; margin-bottom: 4px; }
[hidden] { display: none !important; }
</style>
<div class="w">
  <button class="pill" id="pill" title="Fortunate Leads"><span class="dot" id="pdot"></span><span id="ptext">Leads</span></button>
  <div class="card" id="card" hidden>
    <div class="top"><span class="dot" id="dot"></span><span class="name">Fortunate Leads</span><button class="x" id="min" title="Collapse">&#8211;</button></div>
    <p class="now" id="now">Connecting…</p>
    <div class="nums"><div><b id="people">0</b><span>people</span></div><div><b id="pages">0</b><span>pages</span></div><div><b id="bios">0</b><span>bios</span></div></div>
    <p class="lbl">All accounts</p>
    <div class="stages" id="stages"><div class="stage"><span class="word">Loading…</span></div></div>
    <div class="row"><button class="btn" id="toggle" title="Pause only this Chrome profile's Instagram account. The stages above are for all accounts.">Pause this account</button><button class="btn" id="open">Open app</button></div>
  </div>
</div>`;
  const $ = (id) => root.getElementById(id);
  const n = (v) => Math.round(v || 0).toLocaleString('en-US');
  let collapsed = true, view = null, alive = true;

  const send = (msg) => new Promise((ok) => {
    try { chrome.runtime.sendMessage(msg, (r) => { void chrome.runtime.lastError; ok(r || null); }); } catch { alive = false; ok(null); }
  });
  function sentence(v) {
    if (!v) return 'Extension is starting…';
    if (v.stale) return 'No update from the extension for a while.';
    let t = String(v.text || '');
    if (/^Needs attention/.test(t)) return t.replace(/^Needs attention:\s*/, '');
    if (v.job) return (v.key === 'run' ? 'Reading ' : 'Next: ') + v.job;
    t = t.split(' · ').pop();
    return t || 'Idle';
  }
  function render() {
    const v = view, online = !!v && !v.stale && (v.key === 'run' || v.key === 'wait');
    $('dot').className = $('pdot').className = 'dot' + (online ? ' on' : '');
    const today = (v && v.today) || {};
    $('ptext').textContent = online ? n(today.people) + ' today' : v && v.key === 'stop' && /Paused/.test(v.text || '') ? 'Paused' : 'Leads';
    $('now').textContent = sentence(v);
    $('people').textContent = n(today.people); $('pages').textContent = n(today.list); $('bios').textContent = n(today.bios);
    const paused = !!v && v.state === 'paused' && !/workspace/i.test(v.text || '');
    $('toggle').textContent = paused ? 'Resume this account' : 'Pause this account';
    renderStages();
    $('toggle').dataset.cmd = paused ? 'resume' : 'pause';
    $('pill').hidden = !collapsed; $('card').hidden = collapsed;
  }
  // Workspace stages (Collect lists, Read bios, AI scoring) from the server's /api/control, via the service worker.
  let control = null, stageBusy = false, stageKey = '';
  const escH = (t) => String(t == null ? '' : t).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  function renderStages() {
    const rows = FLV(control);
    const key = JSON.stringify(rows.map((r) => [r.id, r.paused, r.on, r.now])) + stageBusy;
    if (!rows.length) { $('stages').innerHTML = '<div class="stage"><span class="word">' + (control === false ? 'Workspace offline' : 'Loading…') + '</span></div>'; stageKey = ''; return; }
    if (key !== stageKey) {   // rebuild only when something changed, so a button under the pointer stays put
      stageKey = key;
      $('stages').innerHTML = rows.map((r) => `<div class="stage${r.paused ? ' paused' : ''}" title="${escH(r.label + ': ' + r.now + '\n\n' + r.help)}">
        <span class="dot${r.on ? ' on' : ''}"></span><b>${escH(r.name)}</b><span class="word"></span>
        <button class="sbtn" data-stage="${r.id}" data-action="${r.action}" title="${escH((r.paused ? 'Resume ' : 'Pause ') + r.label.toLowerCase() + ' for all accounts. ' + r.help)}"${stageBusy ? ' disabled' : ''}>${r.paused ? 'Resume' : 'Pause'}</button></div>`).join('');
    }
    root.querySelectorAll('.stage .word').forEach((w, i) => { if (rows[i]) w.textContent = rows[i].word; });
  }
  const FLV = (c) => { try { return globalThis.FL.stagesView(c, Date.now()); } catch { return []; } };
  async function loadControl(body) {
    const r = await send(Object.assign({ type: 'fl-control' }, body || {}));
    control = r && r.control ? r.control : control || false;
    renderStages();
  }
  $('stages').addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-stage]');
    if (!b || b.disabled) return;
    stageBusy = true; renderStages();
    await loadControl({ stage: b.dataset.stage, action: b.dataset.action });
    stageBusy = false; renderStages();
  });
  async function refresh(force) {
    if (!alive) { clearInterval(timer); host.remove(); return; } // extension reloaded: the new build injects its own widget
    if (!force && document.visibilityState !== 'visible') return; // background tabs don't poll
    const r = await send({ type: 'fl-view' });
    if (r && r.view) { view = r.view; view.stale = Date.now() - (view.at || 0) > 3 * 60e3; }
    render();
    if (!collapsed) loadControl();
  }
  const setCollapsed = (c) => { collapsed = c; render(); try { chrome.storage.local.set({ widgetCollapsed: c }); } catch {} if (!c) refresh(true); };
  $('pill').addEventListener('click', () => setCollapsed(false));
  $('min').addEventListener('click', () => setCollapsed(true));
  $('toggle').addEventListener('click', async () => { await send({ cmd: $('toggle').dataset.cmd || 'pause' }); refresh(true); });
  $('open').addEventListener('click', () => send({ type: 'fl-open' }));
  // Keys typed in the widget must not reach Instagram's shortcuts.
  for (const ev of ['keydown', 'keyup', 'keypress']) root.addEventListener(ev, (e) => e.stopPropagation());

  const mount = () => { if (!host.isConnected) (document.body || document.documentElement).appendChild(host); };
  try { chrome.storage.local.get('widgetCollapsed', (o) => { collapsed = !o || o.widgetCollapsed !== false; render(); }); } catch {}
  render();
  mount();
  // Instagram is a single-page app and sometimes rebuilds <body>: put the widget back if it was removed.
  new MutationObserver(mount).observe(document.documentElement, { childList: true });
  const timer = setInterval(() => { mount(); refresh(); }, 5e3);
  document.addEventListener('visibilitychange', () => refresh());
  refresh(true);
})();
