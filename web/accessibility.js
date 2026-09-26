/* Keyboard boundaries and focus for the detail panel. No DOM dependency at load time. */
(function (root) {
  'use strict';
  const element = (target) => target?.nodeType === 3 ? target.parentElement : target;
  function isEditableTarget(target) {
    const el = element(target);
    if (!el) return false;
    if (el.closest?.('input, textarea, select, [role="textbox"], [role="searchbox"], [role="combobox"]')) return true;
    if (typeof el.isContentEditable === 'boolean') return el.isContentEditable;
    const editable = el.closest?.('[contenteditable]');
    return !!editable && editable.getAttribute('contenteditable')?.toLowerCase() !== 'false';
  }
  function isInteractiveTarget(target) {
    const el = element(target);
    return isEditableTarget(el) || !!el?.closest?.('button, a[href], area[href], summary, details, label, audio[controls], video[controls], [role="button"], [role="link"], [role="checkbox"], [role="radio"], [role="switch"], [role="slider"], [role="tab"], [role="menuitem"], [role="listbox"], [tabindex]:not([tabindex="-1"])');
  }
  function revealAndFocus(target) {
    if (!target) return false;
    for (let parent = target.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === 'DETAILS') parent.open = true;
    }
    target.focus();
    return true;
  }
  function createDetailFocus({ panel, isMobile, fallbackFocus = () => null }) {
    const doc = panel.ownerDocument, win = doc.defaultView;
    const tabbable = 'a[href], area[href], button, input, select, textarea, summary, [contenteditable], [tabindex]';
    let opened = false, modal = false, returnTo = null, masked = [], attributes = null;
    function visible(node) {
      if (!node || node.isConnected === false || node.closest?.('[hidden], [inert], [aria-hidden="true"]')) return false;
      for (let ancestor = node.parentElement; ancestor; ancestor = ancestor.parentElement) {
        if (ancestor.tagName === 'DETAILS' && !ancestor.open && !ancestor.querySelector('summary')?.contains(node)) return false;
      }
      const style = win?.getComputedStyle?.(node);
      return style?.visibility !== 'hidden' && style?.visibility !== 'collapse' && (!node.getClientRects || !!node.getClientRects().length);
    }
    function focus(node) {
      if (!visible(node) || node.disabled) return false;
      if (node.tabIndex < 0 && !node.hasAttribute('tabindex')) node.setAttribute('tabindex', '-1');
      node.focus({ preventScroll: true });
      return doc.activeElement === node;
    }
    function focusInitial() {
      return focus(panel.querySelector('#d-person-title')) || focus(panel.querySelector('#d-close')) || focus(panel);
    }
    function open() {
      if (opened) return;
      const active = doc.activeElement;
      returnTo = active && active !== doc.body && !panel.contains(active) ? { node: active, id: active.id } : null;
      opened = true;
    }
    function releaseModal() {
      if (!modal) return;
      modal = false;
      for (const [node, inert] of masked) node.inert = inert;
      masked = [];
      for (const [name, value] of Object.entries(attributes)) {
        if (value == null) panel.removeAttribute(name); else panel.setAttribute(name, value);
      }
      attributes = null;
    }
    function close() {
      if (!opened && !modal) return;
      opened = false;
      releaseModal();
      const origin = returnTo;
      returnTo = null;
      if (focus(origin?.node)) return;
      if (origin?.id && focus(doc.getElementById(origin.id))) return;
      focus(fallbackFocus());
    }
    function sync() {
      if (panel.hidden) { close(); return; }
      open();
      if (!isMobile()) { releaseModal(); return; }
      if (!modal) {
        modal = true;
        attributes = Object.fromEntries(['role', 'aria-modal', 'aria-label', 'aria-labelledby'].map((name) => [name, panel.getAttribute(name)]));
        panel.setAttribute('role', 'dialog');
        panel.setAttribute('aria-modal', 'true');
        // Mask siblings at each level, without ever making the panel's ancestor inert.
        for (let branch = panel; branch.parentElement && branch !== doc.body; branch = branch.parentElement) {
          for (const sibling of branch.parentElement.children) {
            if (sibling === branch || /^(SCRIPT|STYLE|LINK)$/.test(sibling.tagName)) continue;
            masked.push([sibling, !!sibling.inert]);
            sibling.inert = true;
          }
        }
      }
      if (panel.querySelector('#d-person-title')) {
        panel.setAttribute('aria-labelledby', 'd-person-title');
        panel.removeAttribute('aria-label');
      } else {
        panel.removeAttribute('aria-labelledby');
        panel.setAttribute('aria-label', 'Details');
      }
      if (!panel.contains(doc.activeElement)) focusInitial();
    }
    function handleKeydown(event) {
      if (!modal || panel.hidden || event.key !== 'Tab') return false;
      const items = [...panel.querySelectorAll(tabbable)].filter((node) => node.tabIndex >= 0 && !node.disabled && !node.matches?.(':disabled') && visible(node));
      const index = items.indexOf(doc.activeElement);
      const next = event.shiftKey ? (index <= 0 ? items.at(-1) : null) : (index < 0 || index === items.length - 1 ? items[0] : null);
      if (!items.length || next) { event.preventDefault(); focus(next || panel); return true; }
      return false;
    }
    function capture() {
      const node = doc.activeElement;
      if (!panel.contains(node)) return null;
      const names = ['data-s', 'data-addtag', 'data-rmtag', 'data-seed', 'data-follow-action'];
      const attribute = names.find((name) => node.hasAttribute(name));
      return { node, id: node.id, owner: panel.dataset.owner, attribute, value: attribute ? node.getAttribute(attribute) : null,
        section: node.tagName === 'SUMMARY' ? node.parentElement?.dataset.detailSection : null,
        start: node.selectionStart, end: node.selectionEnd, direction: node.selectionDirection };
    }
    function restore(saved) {
      if (!saved || saved.owner !== panel.dataset.owner) { if (modal && !panel.contains(doc.activeElement)) focusInitial(); return; }
      let node = panel.contains(saved.node) ? saved.node : saved.id ? doc.getElementById(saved.id) : null;
      if (!node && saved.attribute) node = [...panel.querySelectorAll('[' + saved.attribute + ']')].find((el) => el.getAttribute(saved.attribute) === saved.value);
      if (!node && saved.section) node = [...panel.querySelectorAll('details[data-detail-section]')].find((el) => el.dataset.detailSection === saved.section)?.querySelector('summary');
      if (node && panel.contains(node) && focus(node)) {
        if (typeof saved.start === 'number' && node.setSelectionRange) node.setSelectionRange(saved.start, saved.end, saved.direction);
      } else focusInitial();
    }
    const containFocus = (event) => { if (modal && !panel.hidden && !panel.contains(event.target)) focusInitial(); };
    doc.addEventListener('focusin', containFocus);
    win?.addEventListener('resize', sync);
    // Seed details and route changes also toggle this shared panel.
    const observer = win?.MutationObserver ? new win.MutationObserver(sync) : null;
    observer?.observe(panel, { attributes: true, attributeFilter: ['hidden'] });
    return { open, close, sync, focusInitial, capture, restore, handleKeydown, isModal: () => modal && !panel.hidden,
      dispose() { releaseModal(); observer?.disconnect(); doc.removeEventListener('focusin', containFocus); win?.removeEventListener('resize', sync); } };
  }
  root.LeadAccessibility = { isEditableTarget, isInteractiveTarget, revealAndFocus, createDetailFocus };
})(typeof window === 'undefined' ? globalThis : window);
