/* Instagram actions for people on the connection map: "Open on Instagram" and "Copy link".
 * Self-contained: it watches the selected-person card and the search results, and adds a
 * right-click / long-press menu on bubbles. It reads only the DOM and the view's pick(). */
(function (root) {
  'use strict';
  const HANDLE = /^[A-Za-z0-9._]{1,30}$/;
  const clean = (h) => String(h || '').trim().replace(/^@/, '');
  const igUrl = (handle) => { const h = clean(handle); return HANDLE.test(h) ? `https://www.instagram.com/${encodeURIComponent(h)}/` : null; };

  async function copyText(text, nav = root.navigator, doc = root.document) {
    try { if (nav?.clipboard?.writeText) { await nav.clipboard.writeText(text); return true; } } catch (e) { /* fall back */ }
    try {
      const t = doc.createElement('textarea'); t.value = text; t.setAttribute('readonly', ''); t.style.cssText = 'position:fixed;opacity:0';
      doc.body.append(t); t.select(); const ok = doc.execCommand('copy'); t.remove(); return !!ok;
    } catch (e) { return false; }
  }

  // Copy a profile link, then say so briefly on the control that was used.
  async function copyLink(handle, button, say) {
    const url = igUrl(handle);
    if (!url) return false;
    const ok = await copyText(url);
    if (button) {
      const label = button.dataset.label || button.textContent;
      button.dataset.label = label; button.textContent = ok ? 'Copied' : 'Couldn’t copy';
      clearTimeout(button._t); button._t = setTimeout(() => { button.textContent = label; }, 1600);
    }
    if (say) say(ok ? 'Copied' : 'Couldn’t copy the link');
    return ok;
  }

  // app.js declares M, api and toast with const: global to every script, but not properties of window.
  const global = (name) => { try { return (0, root.eval)(name); } catch (e) { return undefined; } };
  const el = (tag, attrs, ...kids) => {
    const n = root.document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    n.append(...kids);
    return n;
  };
  const say = (text) => { const toast = global('toast'); if (typeof toast === 'function') toast(text); const live = root.document.getElementById('map-live'); if (live) live.textContent = text; };

  // The two buttons on the selected-person card.
  function actions(handle) {
    const url = igUrl(handle);
    const open = el('a', { class: 'btn mv-link-open', href: url || '#', target: '_blank', rel: 'noopener noreferrer', 'data-link': 'open' }, 'Open on Instagram');
    const copy = el('button', { type: 'button', class: 'btn mv-link-copy', 'data-link': 'copy' }, 'Copy link');
    if (!url) { open.setAttribute('aria-disabled', 'true'); copy.disabled = true; }
    copy.addEventListener('click', () => copyLink(handle, copy));
    return el('div', { class: 'mv-links' }, open, copy);
  }

  let menu = null;
  function closeMenu() { if (menu) { menu.remove(); menu = null; } }
  // A small menu at the pointer: one click each, no dialog.
  function openMenu(x, y, handle, host) {
    closeMenu();
    const url = igUrl(handle);
    if (!url) return;
    const open = el('a', { role: 'menuitem', class: 'mv-menu-item', href: url, target: '_blank', rel: 'noopener noreferrer' }, 'Open on Instagram');
    const copy = el('button', { type: 'button', role: 'menuitem', class: 'mv-menu-item' }, 'Copy link');
    menu = el('div', { class: 'mv-menu', role: 'menu', 'aria-label': '@' + clean(handle) }, open, copy);
    open.addEventListener('click', closeMenu);
    copy.addEventListener('click', async () => { await copyLink(handle, null, say); closeMenu(); });
    host.append(menu);
    const box = host.getBoundingClientRect();
    menu.style.left = Math.max(4, Math.min(x - box.left, box.width - menu.offsetWidth - 4)) + 'px';
    menu.style.top = Math.max(4, Math.min(y - box.top, box.height - menu.offsetHeight - 4)) + 'px';
    open.focus();
  }

  function init() {
    const doc = root.document;
    const card = doc.getElementById('map-card'), results = doc.getElementById('map-search-results'), canvas = doc.getElementById('map-canvas');
    if (!card || !canvas) return;

    // Card: keep the actions under the person's name, and follow the handle when it arrives.
    const syncCard = () => {
      const head = card.querySelector('.mv-c-head');
      if (!head) return;
      const handle = clean(head.querySelector('.mv-who span')?.textContent);
      const have = card.querySelector('.mv-links');
      if (have && have.dataset.handle === handle) return;
      have?.remove();
      const links = actions(handle); links.dataset.handle = handle;
      head.after(links);
    };
    new MutationObserver(syncCard).observe(card, { childList: true, subtree: true, characterData: true });

    // Search results: the same two actions appear on the highlighted or hovered row.
    const syncResults = () => results.querySelectorAll('.mv-r:not([data-links])').forEach((row) => {
      row.dataset.links = '1';
      const handle = clean(row.querySelector('b')?.textContent);
      row.append(el('span', { class: 'mv-r-links', 'aria-hidden': 'true' },
        el('span', { class: 'mv-r-link', 'data-link': 'open', title: 'Open on Instagram', 'data-handle': handle }, 'Open'),
        el('span', { class: 'mv-r-link', 'data-link': 'copy', title: 'Copy link', 'data-handle': handle }, 'Copy')));
    });
    if (results) {
      new MutationObserver(syncResults).observe(results, { childList: true });
      results.addEventListener('click', (e) => {
        const a = e.target.closest('[data-link]'); if (!a) return;
        e.preventDefault(); e.stopPropagation();
        if (a.dataset.link === 'open') root.open(igUrl(a.dataset.handle), '_blank', 'noopener');
        else copyLink(a.dataset.handle, a, say);
      }, true);
    }

    // Bubbles: right-click, or press and hold on touch.
    const view = () => global('M')?.impl;
    const handleOf = async (hit) => {
      const d = hit?.it?.d; if (!d || hit.kind === 'c') return '';
      if (d.handle) return d.handle;
      try { return (await global('api').get('/api/person/' + encodeURIComponent(d.id))).handle || ''; } catch (e) { return ''; }
    };
    const at = (e) => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    const show = async (e) => {
      const v = view(); if (!v) return false;
      const hit = v.pick(...at(e)); const handle = await handleOf(hit);
      if (!handle) return false;
      openMenu(e.clientX, e.clientY, handle, canvas.parentElement);
      return true;
    };
    canvas.addEventListener('contextmenu', (e) => { e.preventDefault(); show(e); });
    let hold = null;
    canvas.addEventListener('pointerdown', (e) => {
      closeMenu();
      if (e.pointerType === 'mouse') return;
      const start = [e.clientX, e.clientY], ev = { clientX: e.clientX, clientY: e.clientY };
      hold = setTimeout(() => { hold = null; show(ev); }, 550);
      const cancel = (m) => { if (m.type !== 'pointermove' || Math.hypot(m.clientX - start[0], m.clientY - start[1]) > 8) { clearTimeout(hold); canvas.removeEventListener('pointermove', cancel); canvas.removeEventListener('pointerup', cancel); } };
      canvas.addEventListener('pointermove', cancel); canvas.addEventListener('pointerup', cancel);
    });
    doc.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMenu(); });
    doc.addEventListener('pointerdown', (e) => { if (menu && !menu.contains(e.target) && e.target !== canvas) closeMenu(); });
    // Hovering a bubble says the menu exists.
    const tip = doc.getElementById('hover');
    if (tip) new MutationObserver(() => { if (!tip.hidden && !tip.querySelector('.mv-tip-hint') && tip.querySelector('b') && !/people|person/.test(tip.textContent.slice(0, 20))) tip.append(el('span', { class: 'mv-tip-hint' }, 'Right-click for Instagram')); }).observe(tip, { childList: true });
    syncCard(); if (results) syncResults();
  }

  const api = { igUrl, copyText, copyLink, actions, openMenu, closeMenu, init };
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root.document && root.document.readyState !== undefined && !root.__MAP_LINKS_TEST__) {
    root.MapLinks = api;
    if (root.document.readyState === 'loading') root.document.addEventListener('DOMContentLoaded', init); else init();
  }
})(typeof window !== 'undefined' ? window : globalThis);
