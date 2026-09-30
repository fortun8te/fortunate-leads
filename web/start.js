// Accounts shows setup only when something needs attention.
(() => {
  const E = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
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
  window.StartView = { stepHTML };
  if (typeof document === 'undefined' || !document.getElementById('account-setup')) return;
  const root = document.getElementById('account-setup');
  let data = null, pending = null, error = false, timer = 0;
  const visible = () => document.getElementById('view-accounts')?.classList.contains('on');
  function render() {
    if (!data) {
      root.hidden = !error;
      root.innerHTML = error ? '<p class="muted" role="status">Setup status unavailable. <button class="btn" data-st-retry>Retry</button></p>' : '';
      return;
    }
    const missing = data.steps.filter(s => s.status !== 'ok');
    const required = missing.filter(s => !s.optional);
    const expanded = root.querySelector('details')?.open ?? false;
    root.hidden = !missing.length && !error;
    const html = `<details class="adv account-setup" ${expanded ? 'open' : ''}><summary>${required.length ? 'Connection needed' : 'Optional setup'}</summary>${error ? '<p class="muted" role="status">Showing saved setup. <button class="btn" data-st-retry>Retry</button></p>' : ''}<ul class="st-steps">${(required.length ? required : missing).map(stepHTML).join('')}</ul></details>`;
    if (root.innerHTML !== html) root.innerHTML = html;
  }
  async function refresh() {
    if (pending) return pending;
    pending = (async () => {
      try {
        const response = await fetch('/api/onboarding', {cache:'no-store'});
        if (!response.ok) throw Error('Unavailable');
        const next = await response.json();
        if (!Array.isArray(next.steps)) throw Error('Unavailable');
        data = next; error = false;
      } catch { error = true; }
      if (visible()) render();
    })();
    try { await pending; } finally { pending = null; }
  }
  function schedule() {
    clearTimeout(timer);
    if (visible() && !document.hidden) refresh();
    timer = setTimeout(schedule, 20000);
  }
  root.addEventListener('click', async event => {
    const target = event.target;
    if (target.closest('[data-st-retry]')) return refresh();
    const skip = target.closest('[data-st-skip]');
    if (skip) {
      try {
        const response = await fetch('/api/onboarding', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({skip:skip.dataset.stSkip})});
        if (!response.ok) throw Error('Unavailable');
        await refresh();
      } catch { window.toast?.("Couldn't save setup. Try again."); }
      return;
    }
    const copy = target.closest('[data-st-copy]');
    if (copy) { window.copyText?.(copy.dataset.stCopy, copy); return; }
    if (target.closest('[data-st-wizard]')) document.getElementById('acc-add')?.click();
  });
  window.AccountSetup = {show() { render(); refresh(); clearTimeout(timer); timer = setTimeout(schedule, 20000); }, refresh};
  window.addEventListener('fl:control-changed', () => { if (visible()) refresh(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && visible()) schedule(); });
  if (visible()) window.AccountSetup.show();
})();
