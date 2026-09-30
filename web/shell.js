// App shell: phone navigation keeps five destinations in the bar and puts the rest under More.
(() => {
  const nav = document.querySelector('.tabs');
  if (!nav || nav.querySelector('.tab-more')) return;
  const items = ['start', 'tags', 'settings'].map((v) => nav.querySelector(`a[data-view="${v}"]`)).filter(Boolean);
  items.forEach((a) => a.classList.add('more-item'));
  const btn = document.createElement('button');
  btn.type = 'button'; btn.className = 'tab-more';
  btn.setAttribute('aria-haspopup', 'true'); btn.setAttribute('aria-expanded', 'false');
  btn.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="3.5" cy="8" r="1"/><circle cx="8" cy="8" r="1"/><circle cx="12.5" cy="8" r="1"/></svg><span>More</span>';
  nav.appendChild(btn);
  const scrim = document.createElement('div');
  scrim.className = 'more-scrim'; scrim.hidden = true;
  nav.parentNode.appendChild(scrim);
  const shown = () => items.filter((a) => !a.hidden);
  const sync = () => { btn.classList.toggle('on', items.some((a) => a.classList.contains('on'))); };
  function set(open, focus) {
    if (open) {
      const list = shown();
      list.forEach((a, i) => a.style.setProperty('--i', i));
      nav.style.setProperty('--n', list.length);
      nav.style.setProperty('--navb', Math.round(nav.parentNode.getBoundingClientRect().bottom) + 'px');
    }
    nav.classList.toggle('more-open', open); scrim.hidden = !open;
    btn.setAttribute('aria-expanded', String(open));
    if (open) shown()[0]?.focus(); else if (focus) btn.focus();
  }
  btn.addEventListener('click', () => set(!nav.classList.contains('more-open')));
  scrim.addEventListener('click', () => set(false));
  nav.addEventListener('click', (e) => { if (e.target.closest('a.more-item')) set(false); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && nav.classList.contains('more-open')) set(false, true); });
  window.addEventListener('resize', () => { if (nav.classList.contains('more-open')) set(false); });
  new MutationObserver(sync).observe(nav, { attributes: true, subtree: true, attributeFilter: ['class'] });
  sync();
  // Map load failures get a real retry, not a line of canvas text.
  document.addEventListener('click', (e) => { if (e.target.closest('#map-retry') && typeof M !== 'undefined') { M.status('Loading'); M.load(); } });
})();
