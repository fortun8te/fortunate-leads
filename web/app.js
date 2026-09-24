'use strict';
/* Fortunate Leads UI. Plain JS, no build step. */

const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (n) => (n == null ? '–' : n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M' : n >= 1e4 ? Math.round(n / 1e3) + 'k' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n));
const ig = (h) => 'https://instagram.com/' + encodeURIComponent(h);
const initials = (name, handle) => {
  const s = (name || handle || '?').trim().replace(/^@/, '');
  const p = s.split(/[\s._-]+/).filter(Boolean);
  return ((p[0] || '?')[0] + (p.length > 1 ? p[p.length - 1][0] : '')).toUpperCase();
};
const GROUPS = ['role', 'niche', 'signal', 'size', 'source'];
const GROUP_LABEL = { role: 'Role', niche: 'Niche', signal: 'Signal', size: 'Size', source: 'Source' };
const TIERS = ['hot', 'warm', 'cold', 'unread'];

const I = {
  check: '<svg viewBox="0 0 16 16"><path d="M3.5 8.5 6.5 11.5 12.5 4.5"/></svg>',
  maybe: '<svg viewBox="0 0 16 16"><path d="M6 6a2 2 0 1 1 3 1.7c-.6.4-1 .8-1 1.5V10"/><circle cx="8" cy="12.3" r=".4" fill="currentColor"/></svg>',
  x: '<svg viewBox="0 0 16 16"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/></svg>',
  ext: '<svg viewBox="0 0 16 16"><path d="M6.5 3.5h-3v9h9v-3M9.5 3.5h3v3M12.5 3.5 7.5 8.5"/></svg>',
  close: '<svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
  sun: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="3"/><path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M3.4 12.6l1-1M11.6 4.4l1-1"/></svg>',
  moon: '<svg viewBox="0 0 16 16"><path d="M13 9.5A5.5 5.5 0 0 1 6.5 3a5.5 5.5 0 1 0 6.5 6.5z"/></svg>',
};

/* ---------- API ---------- */
const api = {
  async get(p) {
    const r = await fetch(p, { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error(r.status + ' ' + p);
    return r.json();
  },
  async post(p, body) {
    const r = await fetch(p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    const j = await r.json().catch(() => ({ ok: r.ok }));
    if (!r.ok || j.ok === false) throw new Error(j.error || r.status);
    return j;
  },
};
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), 2400);
}

function avatar(p, size = 48) {
  const src = p.pic ? `<img src="${esc(p.pic)}" alt="" loading="lazy" decoding="async" onerror="this.remove()">` : '';
  return `<span class="av" style="--s:${size}px">${esc(initials(p.name || p.label, p.handle))}${src}</span>`;
}
const tagChip = (t, removable) =>
  `<span class="tag g-${esc(t.grp || 'source')}${t.source === 'manual' ? ' manual' : ''}">${esc(t.tag)}${removable ? `<button class="x" data-rmtag="${esc(t.tag)}" title="Remove">${I.x}</button>` : ''}</span>`;
const sortTags = (tags) => [...(tags || [])].sort((a, b) => GROUPS.indexOf(a.grp) - GROUPS.indexOf(b.grp));
const scoreBadge = (r) => `<span class="score t-${esc(r.tier || 'cold')}" title="${esc(r.tier || '')}"><i></i>${r.tier === 'unread' && r.score == null ? '–' : esc(r.score ?? '–')}</span>`;

/* ---------- State ---------- */
const S = {
  view: 'leads',
  tier: 'hot',
  status: '',
  q: '',
  sort: 'score',
  tags: new Set(),
  rows: [],
  total: 0,
  loading: false,
  done: false,
  sel: -1,
  drawerId: null,
  counts: {},
  allTags: [],
  moreOpen: {},
  scraper: null,
  reqSeq: 0,
};
try {
  const saved = JSON.parse(localStorage.getItem('fl-filters') || '{}');
  if (saved.tier) S.tier = saved.tier;
  if (saved.sort) S.sort = saved.sort;
} catch (e) {}
const saveFilters = () => { try { localStorage.setItem('fl-filters', JSON.stringify({ tier: S.tier, sort: S.sort })); } catch (e) {} };

/* ---------- Theme ---------- */
function effectiveTheme() {
  const t = document.documentElement.dataset.theme;
  if (t) return t;
  return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
function paintThemeBtn() { $('#theme-btn').innerHTML = effectiveTheme() === 'dark' ? I.sun : I.moon; }
$('#theme-btn').onclick = () => {
  const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('fl-theme', next); } catch (e) {}
  paintThemeBtn(); MapView.themeChanged();
};
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { paintThemeBtn(); MapView.themeChanged(); });
paintThemeBtn();

/* ---------- Routing ---------- */
function route() {
  const v = (location.hash.match(/^#\/(\w+)/) || [])[1] || 'leads';
  S.view = ['leads', 'map', 'scraper'].includes(v) ? v : 'leads';
  document.querySelectorAll('.view').forEach((el) => el.classList.toggle('on', el.id === 'view-' + S.view));
  document.querySelectorAll('.nav a').forEach((a) => a.classList.toggle('on', a.dataset.view === S.view));
  if (S.view === 'map') MapView.show(); else MapView.hide();
  if (S.view === 'scraper') renderScraper();
}
window.addEventListener('hashchange', route);

/* ---------- Counts + tags ---------- */
async function loadCounts() {
  try { S.counts = await api.get('/api/counts'); } catch (e) { return; }
  renderTierSeg(); renderStatusSeg();
}
async function loadTags() {
  try { S.allTags = await api.get('/api/tags'); } catch (e) { return; }
  renderTagRail();
  renderMapTagPop();
  $('#tag-options').innerHTML = S.allTags.filter((t) => t.grp !== 'source' && t.grp !== 'size').map((t) => `<option value="${esc(t.tag)}">`).join('');
}

function renderTierSeg() {
  const c = S.counts;
  const items = [['hot', 'Hot'], ['warm', 'Warm'], ['cold', 'Cold'], ['unread', 'Unread'], ['', 'All']];
  $('#tier-seg').innerHTML = items.map(([k, l]) => {
    const n = k ? c[k] : c.total;
    return `<button data-tier="${k}" class="${S.tier === k ? 'on' : ''}">${k ? `<i class="t t-${k}"></i>` : ''}${l}<span class="c">${n ?? ''}</span></button>`;
  }).join('');
}
function renderStatusSeg() {
  const c = S.counts;
  const items = [['', 'Any'], ['good', 'Good'], ['maybe', 'Maybe'], ['contacted', 'Contacted']];
  $('#status-seg').innerHTML = items.map(([k, l]) =>
    `<button data-status="${k}" class="${S.status === k ? 'on' : ''}">${l}${k && c[k] != null ? `<span class="c">${c[k]}</span>` : ''}</button>`).join('');
}
$('#tier-seg').onclick = (e) => {
  const b = e.target.closest('button'); if (!b) return;
  S.tier = b.dataset.tier; saveFilters(); renderTierSeg(); reloadLeads();
};
$('#status-seg').onclick = (e) => {
  const b = e.target.closest('button'); if (!b) return;
  S.status = b.dataset.status; renderStatusSeg(); reloadLeads();
};

function tagGroupsHTML(limit = 8) {
  const by = {};
  S.allTags.forEach((t) => (by[t.grp] = by[t.grp] || []).push(t));
  return GROUPS.filter((g) => by[g]).map((g) => {
    const list = by[g].sort((a, b) => (S.tags.has(b.tag) - S.tags.has(a.tag)) || b.count - a.count);
    const open = S.moreOpen[g];
    const shown = open ? list : list.slice(0, limit);
    const hidden = list.length - shown.length;
    return `<div class="tgrp"><h4><i class="tag g-${g}" style="padding:0;height:6px"></i>${GROUP_LABEL[g]}</h4><div class="chips">${shown.map((t) =>
      `<button class="fchip${S.tags.has(t.tag) ? ' on' : ''}" data-tag="${esc(t.tag)}" title="${esc(t.tag)}"><span>${esc(t.tag)}</span><b>${t.count}</b></button>`).join('')}
      ${hidden > 0 ? `<button class="more" data-more="${g}">+${hidden} more</button>` : open && list.length > limit ? `<button class="more" data-more="${g}">Less</button>` : ''}</div></div>`;
  }).join('');
}
function renderTagRail() { $('#tag-rail').innerHTML = tagGroupsHTML(8); renderActiveTags(); }
function renderMapTagPop() { $('#map-tags-pop').innerHTML = tagGroupsHTML(10); }
function renderActiveTags() {
  const html = [...S.tags].map((t) => `<button class="fchip on" data-untag="${esc(t)}">${esc(t)}${I.x}</button>`).join('')
    + (S.tags.size > 1 ? `<button class="more" data-cleartags>Clear</button>` : '');
  $('#active-tags').innerHTML = html;
  $('#map-active-tags').innerHTML = html;
  $('#map-tags-btn').textContent = S.tags.size ? `Tags · ${S.tags.size}` : 'Tags';
}
function onTagClick(e) {
  const t = e.target.closest('[data-tag],[data-more],[data-untag],[data-cleartags]'); if (!t) return;
  if (t.dataset.more) { S.moreOpen[t.dataset.more] = !S.moreOpen[t.dataset.more]; renderTagRail(); renderMapTagPop(); return; }
  if (t.hasAttribute('data-cleartags')) S.tags.clear();
  else {
    const tag = t.dataset.tag ?? t.dataset.untag;
    S.tags.has(tag) ? S.tags.delete(tag) : S.tags.add(tag);
  }
  renderTagRail(); renderMapTagPop(); reloadLeads(); MapView.filtersChanged();
}
['#tag-rail', '#active-tags', '#map-tags-pop', '#map-active-tags'].forEach((s) => $(s).addEventListener('click', onTagClick));

/* ---------- Leads list ---------- */
function leadsURL(offset, limit) {
  const p = new URLSearchParams();
  if (S.tier) p.set('tier', S.tier);
  if (S.tags.size) p.set('tags', [...S.tags].join(','));
  p.set('status', S.status);
  if (S.q) p.set('q', S.q);
  p.set('sort', S.sort);
  p.set('offset', offset); p.set('limit', limit);
  return '/api/leads?' + p;
}
async function reloadLeads() {
  S.rows = []; S.done = false; S.sel = -1; S.total = 0;
  $('#list').innerHTML = '';
  $('#list-scroll').scrollTop = 0;
  await loadMore(true);
}
async function loadMore(fresh) {
  if (S.loading && !fresh) return;
  if (S.done) return;
  const seq = ++S.reqSeq;
  S.loading = true;
  if (!S.rows.length) $('#list').innerHTML = '<div class="loadrow">Loading</div>';
  try {
    const j = await api.get(leadsURL(S.rows.length, 50));
    if (seq !== S.reqSeq) return;
    if (!S.rows.length) $('#list').innerHTML = '';
    S.total = j.total;
    S.rows.push(...j.rows);
    if (j.rows.length < 50 || S.rows.length >= j.total) S.done = true;
    $('#list').insertAdjacentHTML('beforeend', j.rows.map(rowHTML).join(''));
    renderListMeta();
  } catch (e) {
    if (seq === S.reqSeq && !S.rows.length) $('#list').innerHTML = '<div class="empty">Server not reachable</div>';
  } finally {
    if (seq === S.reqSeq) S.loading = false;
  }
  // Fill viewport if the list is short.
  const sc = $('#list-scroll');
  if (!S.done && sc.scrollHeight <= sc.clientHeight + 200) loadMore();
}
function renderListMeta() {
  $('#list-total').textContent = S.total ? `${S.total.toLocaleString()} ${S.total === 1 ? 'person' : 'people'}` : '';
  if (!S.rows.length && !S.loading) $('#list').innerHTML = `<div class="empty">No one matches${S.tags.size || S.q ? ' these filters' : ''}</div>`;
}
function rowHTML(r) {
  const tags = sortTags(r.tags).filter((t) => !(t.grp === 'source' && /^(via|follows|followed by) @/.test(t.tag)));
  const via = (r.via || []).slice(0, 3).map((s) => '@' + esc(s)).join(' · ') + ((r.via || []).length > 3 ? ` +${r.via.length - 3}` : '');
  const st = r.status;
  return `<div class="row${S.rows[S.sel]?.id === r.id ? ' sel' : ''}" data-id="${r.id}">
    ${avatar(r, 48)}
    <div class="who">
      <div class="l1"><span class="nm">${esc(r.name || r.handle)}</span><a class="handle" href="${ig(r.handle)}" target="_blank" rel="noopener" data-stop>@${esc(r.handle)}</a><span class="fl num">${r.followers != null ? fmt(r.followers) + ' followers' : ''}</span></div>
      <div class="bio${r.bio ? '' : ' none'}">${r.bio ? esc(r.bio) : 'No bio read yet'}</div>
    </div>
    <div class="why">
      <div class="tags">${tags.map((t) => tagChip(t)).join('')}</div>
      ${r.reason ? `<div class="reason">${esc(r.reason)}</div>` : ''}${via ? `<div class="via">via ${via}</div>` : ''}
    </div>
    <div class="side">
      ${scoreBadge(r)}
      <div class="acts">
        <button class="act good${st === 'good' ? ' on' : ''}" data-mark="good" title="Good (g)">${I.check}</button>
        <button class="act maybe${st === 'maybe' ? ' on' : ''}" data-mark="maybe" title="Maybe (m)">${I.maybe}</button>
        <button class="act no${st === 'no' ? ' on' : ''}" data-mark="no" title="No (x)">${I.x}</button>
      </div>
      ${st && !['good', 'maybe', 'no'].includes(st) ? `<span class="stlabel ${esc(st)}">${esc(st[0].toUpperCase() + st.slice(1))}</span>` : ''}
    </div>
  </div>`;
}
function rowEl(id) { return $(`#list .row[data-id="${id}"]`); }
function replaceRow(r) {
  const el = rowEl(r.id); if (!el) return;
  el.outerHTML = rowHTML(r);
}
function select(i, scroll = true) {
  if (!S.rows.length) return;
  i = Math.max(0, Math.min(S.rows.length - 1, i));
  const prev = S.rows[S.sel]; if (prev) rowEl(prev.id)?.classList.remove('sel');
  S.sel = i;
  const el = rowEl(S.rows[i].id);
  el?.classList.add('sel');
  if (scroll) el?.scrollIntoView({ block: 'nearest' });
  if (i > S.rows.length - 8) loadMore();
  if (S.drawerId != null) openDrawer(S.rows[i].id);
}
$('#list').addEventListener('click', (e) => {
  if (e.target.closest('[data-stop]')) return;
  const row = e.target.closest('.row'); if (!row) return;
  const id = +row.dataset.id;
  const idx = S.rows.findIndex((r) => r.id === id);
  const m = e.target.closest('[data-mark]');
  if (m) { e.stopPropagation(); const r = S.rows[idx]; mark(id, r.status === m.dataset.mark ? null : m.dataset.mark); return; }
  S.drawerId = id;
  select(idx, false);
  openDrawer(id);
});

async function mark(id, status, note) {
  const idx = S.rows.findIndex((r) => r.id === id);
  const r = S.rows[idx];
  const prevStatus = r ? r.status : undefined;
  if (r) r.status = status;
  const body = { status }; if (note !== undefined) body.note = note;
  const hide = r && status === 'no' && S.status !== 'no';
  if (r && !hide) replaceRow(r);
  if (hide) {
    const el = rowEl(id); el?.classList.add('gone');
    setTimeout(() => {
      const i = S.rows.findIndex((x) => x.id === id); if (i < 0) return;
      S.rows.splice(i, 1); el?.remove(); S.total = Math.max(0, S.total - 1); renderListMeta();
      if (S.sel >= i) { S.sel = Math.min(i, S.rows.length - 1); const nx = S.rows[S.sel]; if (nx) { rowEl(nx.id)?.classList.add('sel'); if (S.drawerId === id) openDrawer(nx.id); } }
    }, 180);
  } else if (r && S.sel === idx) rowEl(id)?.classList.add('sel');
  try {
    await api.post(`/api/person/${id}/mark`, body);
    loadCounts();
    if (S.drawerId === id && !hide) openDrawer(id, true);
  } catch (e) {
    if (r) { r.status = prevStatus; replaceRow(r); }
    toast('Could not save');
  }
}

let qTimer;
$('#q').addEventListener('input', (e) => { clearTimeout(qTimer); qTimer = setTimeout(() => { S.q = e.target.value.trim(); reloadLeads(); }, 220); });
$('#sort').value = S.sort;
$('#sort').onchange = (e) => { S.sort = e.target.value; saveFilters(); reloadLeads(); };
new IntersectionObserver((en) => { if (en[0].isIntersecting && S.rows.length) loadMore(); }, { root: $('#list-scroll'), rootMargin: '600px' }).observe($('#sentinel'));

async function refreshLeads() {
  if (S.view !== 'leads' || document.hidden || S.loading || !S.rows.length) return;
  const seq = S.reqSeq;
  try {
    const n = Math.max(50, S.rows.length);
    const j = await api.get(leadsURL(0, n));
    if (seq !== S.reqSeq || S.loading) return;
    if (JSON.stringify(j.rows) === JSON.stringify(S.rows.slice(0, j.rows.length)) && j.total === S.total) return;
    const sc = $('#list-scroll'); const top = sc.scrollTop;
    const selId = S.rows[S.sel]?.id;
    S.rows = j.rows; S.total = j.total; S.done = j.rows.length < n;
    S.sel = selId != null ? S.rows.findIndex((r) => r.id === selId) : -1;
    $('#list').innerHTML = S.rows.map(rowHTML).join('');
    sc.scrollTop = top;
    renderListMeta();
  } catch (e) {}
}

/* ---------- Keyboard ---------- */
document.addEventListener('keydown', (e) => {
  const tag = e.target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || e.metaKey || e.ctrlKey || e.altKey) {
    if (e.key === 'Escape') e.target.blur();
    return;
  }
  if (e.key === 'Escape') { if (!$('#map-tags-pop').hidden) $('#map-tags-pop').hidden = true; else closeDrawer(); return; }
  if (S.view !== 'leads') return;
  const r = S.rows[S.sel];
  switch (e.key) {
    case 'j': case 'ArrowDown': select(S.sel + 1); break;
    case 'k': case 'ArrowUp': select(S.sel - 1); break;
    case 'g': if (r) mark(r.id, r.status === 'good' ? null : 'good'); break;
    case 'm': if (r) mark(r.id, r.status === 'maybe' ? null : 'maybe'); break;
    case 'x': if (r) mark(r.id, r.status === 'no' ? null : 'no'); break;
    case 'o': if (r) window.open(ig(r.handle), '_blank', 'noopener'); break;
    case 'Enter': if (r) { S.drawerId = r.id; openDrawer(r.id); } break;
    case '/': e.preventDefault(); $('#q').focus(); break;
    default: return;
  }
  e.preventDefault();
});

/* ---------- Drawer ---------- */
const dirWords = (d, seed) => (d === 'followers' ? `follows <b>@${esc(seed)}</b>` : `followed by <b>@${esc(seed)}</b>`);
let drawerSeq = 0;
async function openDrawer(id, keepScroll) {
  S.drawerId = id;
  const seq = ++drawerSeq;
  const dr = $('#drawer');
  const wasOpen = !dr.hidden;
  let p;
  try { p = await api.get('/api/person/' + id); } catch (e) { toast('Could not load person'); return; }
  if (seq !== drawerSeq) return;
  const top = dr.scrollTop;
  dr.hidden = false;
  if (!wasOpen) dr.style.animation = ''; else dr.style.animation = 'none';
  const v = p.verdict || {};
  const tier = p.tier || v.tier;
  const tags = sortTags(p.tags);
  const edges = p.edges || [];
  const status = p.status;
  const stBtn = (k, l) => `<button class="btn sm ${k}${status === k ? ' on' : ''}" data-dmark="${k}">${l}</button>`;
  $('#drawer-body').innerHTML = `
    <div class="d-top">
      <div class="d-actions">
        <a class="btn sm" href="${ig(p.handle)}" target="_blank" rel="noopener">${I.ext}Open Instagram</a>
        ${tier === 'unread' || !p.bio ? `<button class="btn sm" data-read>Read profile now</button>` : ''}
      </div>
      <button class="icon-btn" data-close title="Close (Esc)">${I.close}</button>
    </div>
    <div class="d-hero">
      ${avatar(p, 96)}
      <div style="min-width:0">
        <div class="nm">${esc(p.name || p.handle)}</div>
        <a class="handle" href="${ig(p.handle)}" target="_blank" rel="noopener">@${esc(p.handle)}</a>
        ${p.role || v.role ? `<div class="role">${esc(p.role || v.role)}</div>` : ''}
      </div>
    </div>
    <div class="stats">
      <div class="stat"><b>${fmt(p.followers)}</b><span>Followers</span></div>
      <div class="stat"><b>${fmt(p.following)}</b><span>Following</span></div>
      <div class="stat"><b>${fmt(p.posts)}</b><span>Posts</span></div>
    </div>
    <div class="d-sec" style="border-top:0;padding-top:0">
      <div class="d-bio${p.bio ? '' : ' muted'}">${p.bio ? esc(p.bio) : 'No bio read yet'}</div>
      ${p.website ? `<a class="d-web" href="${esc(/^https?:/i.test(p.website) ? p.website : 'https://' + p.website)}" target="_blank" rel="noopener">${esc(p.website.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, ''))}${I.ext}</a>` : ''}
    </div>
    <div class="d-sec">
      <h5>Why</h5>
      <div class="verdict">${scoreBadge({ tier, score: p.score ?? v.score })}<p>${esc(p.reason || v.reason || '–')}</p></div>
    </div>
    <div class="d-sec">
      <h5>Status</h5>
      <div class="st-btns">${stBtn('good', 'Good')}${stBtn('maybe', 'Maybe')}${stBtn('no', 'No')}${stBtn('contacted', 'Contacted')}${stBtn('client', 'Client')}${stBtn('known', 'Known')}</div>
    </div>
    <div class="d-sec">
      <h5>Tags</h5>
      <div class="d-tags">${tags.map((t) => tagChip(t, t.source === 'manual')).join('') || '<span class="muted">None</span>'}</div>
      <form class="tag-add" data-tagform><input class="inp" name="t" list="tag-options" placeholder="Add tag" autocomplete="off"></form>
    </div>
    <div class="d-sec">
      <h5>Connections <span class="saved num">${edges.length}</span></h5>
      <ul class="conn">${edges.map((e) => `<li><i></i><span>${dirWords(e.direction, e.seed)}</span></li>`).join('') || '<li class="muted">None</li>'}</ul>
    </div>
    <div class="d-sec">
      <h5>Note <span class="saved" id="note-saved"></span></h5>
      <textarea id="note" rows="3" placeholder="Add a note">${esc(p.note || '')}</textarea>
    </div>`;
  if (keepScroll || wasOpen) dr.scrollTop = keepScroll ? top : 0;
  const note = $('#note');
  note.dataset.orig = p.note || '';
  note.addEventListener('blur', () => saveNote(id, p.status));
  // Keep the list row in sync with fresh data.
  const i = S.rows.findIndex((r) => r.id === id);
  if (i >= 0) {
    const { edges: _e, verdict: _v, note: _n, ...row } = p;
    S.rows[i] = { ...S.rows[i], ...row };
    replaceRow(S.rows[i]);
    if (S.sel === i) rowEl(id)?.classList.add('sel');
  }
}
async function saveNote(id, status) {
  const note = $('#note'); if (!note || note.value === note.dataset.orig) return;
  try {
    await api.post(`/api/person/${id}/mark`, { status: status ?? null, note: note.value });
    note.dataset.orig = note.value;
    const s = $('#note-saved'); if (s) s.textContent = 'Saved';
  } catch (e) { toast('Could not save note'); }
}
function closeDrawer() {
  if ($('#drawer').hidden) return;
  if (S.drawerId != null) saveNote(S.drawerId, S.rows.find((r) => r.id === S.drawerId)?.status);
  $('#drawer').hidden = true; S.drawerId = null;
}
$('#drawer').addEventListener('click', async (e) => {
  const id = S.drawerId; if (id == null) return;
  if (e.target.closest('[data-close]')) return closeDrawer();
  const dm = e.target.closest('[data-dmark]');
  if (dm) {
    const cur = $('#drawer .st-btns .on')?.dataset.dmark;
    const next = cur === dm.dataset.dmark ? null : dm.dataset.dmark;
    const note = $('#note')?.value;
    if (S.rows.some((r) => r.id === id)) return mark(id, next, note);
    try { await api.post(`/api/person/${id}/mark`, { status: next, note }); loadCounts(); openDrawer(id, true); } catch (err) { toast('Could not save'); }
    return;
  }
  const rm = e.target.closest('[data-rmtag]');
  if (rm) {
    try { await api.post(`/api/person/${id}/tags`, { remove: [rm.dataset.rmtag] }); openDrawer(id, true); loadTags(); } catch (err) { toast('Could not remove tag'); }
    return;
  }
  if (e.target.closest('[data-read]')) {
    const b = e.target.closest('[data-read]');
    try { await api.post(`/api/person/${id}/read`); b.textContent = 'Queued'; b.disabled = true; } catch (err) { toast('Could not queue read'); }
  }
});
$('#drawer').addEventListener('submit', async (e) => {
  e.preventDefault();
  const inp = e.target.querySelector('input'); const t = inp.value.trim(); if (!t) return;
  try { await api.post(`/api/person/${S.drawerId}/tags`, { add: [t] }); openDrawer(S.drawerId, true); loadTags(); } catch (err) { toast('Could not add tag'); }
});

/* ---------- Scraper ---------- */
const hhmm = (iso) => { const d = new Date(iso); return isNaN(d) ? '' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }); };
function ago(iso) {
  if (!iso) return '–';
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}
function left(iso) {
  const s = Math.max(0, (new Date(iso) - Date.now()) / 1000);
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}:${String(sec).padStart(2, '0')}`;
}
function plainError(e) {
  if (!e) return null;
  const s = String(e);
  const map = [
    [/rate.?limit|429/i, 'Instagram is rate limiting. Waiting it out.'],
    [/challenge|checkpoint/i, 'Instagram wants a security check. Open Instagram in Chrome.'],
    [/login|logged out|401/i, 'Logged out of Instagram in Chrome.'],
    [/private/i, 'That account is private.'],
    [/not.?found|404/i, 'Account not found.'],
  ];
  for (const [re, m] of map) if (re.test(s)) return m;
  return s;
}
function extState(sc) {
  if (!sc || !sc.ext || !sc.ext.online) return { k: 'offline', t: 'Offline' };
  if (sc.paused || sc.ext.state === 'paused') return { k: 'paused', t: 'Paused' };
  if (sc.ext.state === 'cooldown' && sc.ext.cooldown_until && new Date(sc.ext.cooldown_until) > Date.now()) return { k: 'cooldown', t: 'Cooldown until ' + hhmm(sc.ext.cooldown_until) };
  if (sc.ext.state === 'running') return { k: 'running', t: 'Running' };
  return { k: 'idle', t: 'Idle' };
}
async function loadScraper() {
  try { S.scraper = await api.get('/api/scraper'); } catch (e) { S.scraper = null; }
  const st = extState(S.scraper);
  const pt = S.scraper?.people_today;
  $('#ext-status').innerHTML = `<i class="dot ${st.k}"></i><span>${esc(st.t)}</span>${pt != null ? `<span class="pt num">${pt.toLocaleString()} today</span>` : ''}`;
  if (S.view === 'scraper') renderScraper();
}
function meter(label, n, of) {
  const pct = of ? Math.min(100, (n / of) * 100) : 0;
  return `<span>${label}</span><div class="meter"><div class="trk"><i class="${pct >= 100 ? 'full' : ''}" style="width:${pct}%"></i></div><b class="num">${n ?? 0} / ${of ?? '–'}</b></div>`;
}
let budgetDirty = false;
const DAILY_TARGET = 10000;
function renderScraper() {
  const sc = S.scraper;
  const card = $('#ext-card');
  if (!sc) { card.innerHTML = '<div class="card-head"><h3>Extension</h3></div><div class="muted">Server not reachable</div>'; $('#lists-body').innerHTML = ''; return; }
  const x = sc.ext || {};
  const st = extState(sc);
  const bInputsFocused = card.contains(document.activeElement) && document.activeElement.tagName === 'INPUT';
  if (!bInputsFocused && !budgetDirty) {
    card.innerHTML = `
      <div class="card-head"><h3>Extension</h3><div class="grow"></div>
        <button class="btn sm" id="pause-btn">${sc.paused ? 'Resume' : 'Pause'}</button></div>
      <div class="today">
        <div class="today-n"><b class="num">${(sc.people_today ?? 0).toLocaleString()}</b><span class="muted num">/ ${DAILY_TARGET.toLocaleString()} new people today</span></div>
        <div class="meter"><div class="trk big"><i style="width:${Math.min(100, ((sc.people_today ?? 0) / DAILY_TARGET) * 100)}%"></i></div></div>
      </div>
      <div class="kv">
        <span>Status</span><span class="ext-line"><i class="dot ${st.k}"></i><span id="ext-state-text">${esc(st.t)}</span></span>
        <span>Version</span><span class="num">${esc(x.version || '–')}</span>
        <span>Last seen</span><span>${esc(ago(x.last_seen))}</span>
        ${meter('List pages today', x.today?.list, x.budget?.list)}
        ${meter('Profile reads today', x.today?.profile, x.budget?.profile)}
        <span>Queue</span><span class="num">${sc.queue?.list ?? 0} lists · ${sc.queue?.profile ?? 0} profiles</span>
        ${x.last_error ? `<span>Last error</span><span class="err">${esc(plainError(x.last_error))}</span>` : ''}
      </div>
      <form class="budget" id="budget-form">
        <span class="muted">Daily budget</span><div class="grow"></div>
        <label>Lists <input class="inp" name="list" type="number" min="0" value="${esc(x.budget?.list ?? '')}"></label>
        <label>Profiles <input class="inp" name="profile" type="number" min="0" value="${esc(x.budget?.profile ?? '')}"></label>
        <button class="btn sm" type="submit">Save</button>
      </form>`;
  }
  tickCountdown();
  $('#queue-info').textContent = `${(sc.lists || []).length} lists`;
  const lists = [...(sc.lists || [])].sort((a, b) => (a.state === 'running' ? -1 : 0) - (b.state === 'running' ? -1 : 0));
  $('#lists-body').innerHTML = lists.map((l) => {
    const pct = l.total ? Math.min(100, (l.received / l.total) * 100) : 0;
    return `<tr>
      <td><a class="handle" href="${ig(l.seed)}" target="_blank" rel="noopener">@${esc(l.seed)}</a></td>
      <td>${l.direction === 'followers' ? 'Followers' : 'Following'}</td>
      <td><div class="meter"><div class="trk"><i style="width:${pct}%"></i></div><b class="num">${fmt(l.received)}${l.total ? ' / ' + fmt(l.total) : ''}</b></div></td>
      <td><span class="pill ${esc(l.state)}" title="${esc(l.error || '')}"><i></i>${esc(l.state[0].toUpperCase() + l.state.slice(1))}</span></td>
      <td class="r">${esc(ago(l.updated_at))}</td></tr>`;
  }).join('') || '<tr><td colspan="5" class="muted">No lists yet</td></tr>';
}
function tickCountdown() {
  const sc = S.scraper; if (!sc) return;
  const st = extState(sc);
  const el = $('#ext-state-text');
  if (el && st.k === 'cooldown') el.textContent = `${st.t} · ${left(sc.ext.cooldown_until)} left`;
}
setInterval(() => { if (S.view === 'scraper') tickCountdown(); }, 1000);
$('#ext-card').addEventListener('click', async (e) => {
  if (!e.target.closest('#pause-btn')) return;
  const next = !S.scraper?.paused;
  try { await api.post('/api/scraper/pause', { paused: next }); budgetDirty = false; await loadScraper(); } catch (err) { toast('Could not reach server'); }
});
$('#ext-card').addEventListener('input', () => (budgetDirty = true));
$('#ext-card').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try { await api.post('/api/scraper/budget', { list: +f.get('list'), profile: +f.get('profile') }); budgetDirty = false; document.activeElement?.blur(); toast('Budget saved'); loadScraper(); } catch (err) { toast('Could not save budget'); }
});
function parseHandles(txt) {
  const out = [];
  txt.split(/[\s,;]+/).forEach((raw) => {
    let s = raw.trim(); if (!s) return;
    const m = s.match(/instagram\.com\/([^/?#\s]+)/i); if (m) s = m[1];
    s = s.replace(/^@/, '').replace(/\/$/, '').toLowerCase();
    if (/^[a-z0-9._]{1,30}$/.test(s) && !['p', 'reel', 'explore', 'stories'].includes(s) && !out.includes(s)) out.push(s);
  });
  return out;
}
function syncSeedBtn() {
  const h = parseHandles($('#seed-input').value);
  const dirs = ['followers', 'following'].filter((d) => $('#dir-' + d).checked);
  const b = $('#seed-add');
  b.disabled = !h.length || !dirs.length;
  b.textContent = h.length ? `Add ${h.length} seed${h.length > 1 ? 's' : ''}` : 'Add';
  $('#seed-msg').textContent = '';
}
['#seed-input', '#dir-followers', '#dir-following'].forEach((s) => $(s).addEventListener('input', syncSeedBtn));
$('#seed-add').onclick = async () => {
  const handles = parseHandles($('#seed-input').value);
  const directions = ['followers', 'following'].filter((d) => $('#dir-' + d).checked);
  try {
    await api.post('/api/scraper/seeds', { handles, directions });
    $('#seed-input').value = ''; syncSeedBtn();
    $('#seed-msg').textContent = `Queued ${handles.length}`;
    loadScraper();
  } catch (e) { toast('Could not add seeds'); }
};

/* ---------- Map ---------- */
const MapView = (() => {
  const stage = $('#map-stage');
  const canvas = $('#map-canvas');
  const ctx = canvas.getContext('2d');
  const hc = $('#hovercard');
  let W = 0, H = 0, DPR = 1;
  let T = { x: 0, y: 0, k: 1 };
  let nodes = [], links = [], byId = new Map();
  let sim = null, rev = null, visible = false, dirty = true, fitted = false;
  let scope = 'leads', tier = '';
  let hover = null, focus = null, colors = null;
  const imgs = new Map();
  let loadingImgs = 0;
  const imgQueue = [];
  const personCache = new Map();

  const radius = (n) => (n.kind === 'seed' ? Math.min(30, 14 + Math.sqrt(n.degree || 1) * 0.9) : 5 + ((n.score ?? 20) / 100) * 9);

  function readColors() {
    const cs = getComputedStyle(document.documentElement);
    const g = (v) => cs.getPropertyValue(v).trim();
    colors = { bg: g('--bg'), text: g('--text'), text2: g('--text-2'), text3: g('--text-3'), surface: g('--surface'), surface3: g('--surface-3'), border: g('--border-2'), link: g('--link'), accent: g('--accent'),
      hot: g('--hot'), warm: g('--warm'), cold: g('--cold'), unread: g('--unread') };
  }

  function resize() {
    const r = stage.getBoundingClientRect();
    DPR = window.devicePixelRatio || 1;
    const nw = Math.max(1, r.width), nh = Math.max(1, r.height);
    if (W && H) { T.x += (nw - W) / 2; T.y += (nh - H) / 2; }
    W = nw; H = nh;
    canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
    dirty = true;
  }
  new ResizeObserver(() => { if (visible) resize(); }).observe(stage);

  function matches(n) {
    if (n.kind === 'seed') return true;
    if (tier && n.tier !== tier) return false;
    if (S.tags.size) {
      if (!n.tags) return true; // server did not send tags for nodes
      for (const t of S.tags) if (!n.tags.includes(t)) return false;
    }
    return true;
  }
  function refilter() {
    nodes.forEach((n) => (n._on = matches(n)));
    const q = $('#map-q').value.trim().toLowerCase();
    nodes.forEach((n) => (n._hit = q.length > 1 && ((n.label || '').toLowerCase().includes(q) || (n.handle || '').toLowerCase().includes(q))));
    const on = nodes.filter((n) => n.kind === 'lead' && n._on).length;
    $('#map-legend').innerHTML = ['hot', 'warm', 'cold', 'unread'].map((t) => `<span><i style="border-color:var(--${t})"></i>${t[0].toUpperCase() + t.slice(1)}</span>`).join('')
      + `<span class="num">${on.toLocaleString()} people · ${nodes.filter((n) => n.kind === 'seed').length} seeds</span>`;
    dirty = true;
  }

  function img(url) {
    let e = imgs.get(url);
    if (e) return e;
    e = { state: 'idle', c: null };
    imgs.set(url, e);
    return e;
  }
  function requestImg(url) {
    const e = img(url);
    if (e.state !== 'idle') return;
    e.state = 'queued'; imgQueue.push(url); pumpImgs();
  }
  function pumpImgs() {
    while (loadingImgs < 8 && imgQueue.length) {
      const url = imgQueue.pop();
      const e = imgs.get(url);
      loadingImgs++;
      const im = new Image();
      im.decoding = 'async';
      im.onload = () => {
        const s = 64, c = document.createElement('canvas'); c.width = c.height = s;
        const x = c.getContext('2d');
        x.beginPath(); x.arc(s / 2, s / 2, s / 2, 0, Math.PI * 2); x.clip();
        const m = Math.min(im.naturalWidth, im.naturalHeight);
        x.drawImage(im, (im.naturalWidth - m) / 2, (im.naturalHeight - m) / 2, m, m, 0, 0, s, s);
        e.c = c; e.state = 'ok'; loadingImgs--; dirty = true; pumpImgs();
      };
      im.onerror = () => { e.state = 'err'; loadingImgs--; pumpImgs(); };
      im.src = url;
    }
  }

  function toWorld(px, py) { return [(px - T.x) / T.k, (py - T.y) / T.k]; }

  function draw() {
    if (!colors) readColors();
    const k = T.k;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.setTransform(DPR * k, 0, 0, DPR * k, DPR * T.x, DPR * T.y);
    const [x0, y0] = toWorld(0, 0), [x1, y1] = toWorld(W, H);
    const pad = 40;
    const inView = (n, r) => n.x + r > x0 - pad && n.x - r < x1 + pad && n.y + r > y0 - pad && n.y - r < y1 + pad;
    const hl = hover || focus;
    const hlSet = new Set();
    if (hl) { hlSet.add(hl.id); links.forEach((l) => { if (l.source === hl || l.target === hl) { hlSet.add(l.source.id); hlSet.add(l.target.id); } }); }

    // links
    ctx.lineWidth = 1 / k;
    ctx.strokeStyle = colors.link;
    ctx.beginPath();
    for (const l of links) {
      if (!l.target._on || (hl && (l.source === hl || l.target === hl))) continue;
      ctx.moveTo(l.source.x, l.source.y); ctx.lineTo(l.target.x, l.target.y);
    }
    ctx.stroke();
    if (hl) {
      ctx.strokeStyle = colors.accent; ctx.globalAlpha = 0.55; ctx.lineWidth = 1.25 / k;
      ctx.beginPath();
      for (const l of links) if (l.source === hl || l.target === hl) { ctx.moveTo(l.source.x, l.source.y); ctx.lineTo(l.target.x, l.target.y); }
      ctx.stroke(); ctx.globalAlpha = 1;
    }

    // leads
    const showNames = k > 2.4;
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    for (const n of nodes) {
      if (n.kind !== 'lead') continue;
      const r = n._r;
      if (!inView(n, r)) continue;
      const dim = !n._on || (hl && !hlSet.has(n.id));
      ctx.globalAlpha = dim ? (n._on ? 0.25 : 0.08) : 1;
      const screenR = r * k;
      let drawn = false;
      if (n.pic && screenR >= 5) {
        const e = img(n.pic);
        if (e.state === 'ok') { ctx.drawImage(e.c, n.x - r, n.y - r, r * 2, r * 2); drawn = true; }
        else if (e.state === 'idle' && n._on) requestImg(n.pic);
      }
      if (!drawn) {
        ctx.fillStyle = colors.surface3;
        ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, Math.PI * 2); ctx.fill();
        if (screenR > 11) {
          ctx.fillStyle = colors.text2; ctx.textBaseline = 'middle';
          ctx.font = `600 ${r * 0.72}px Inter, system-ui, sans-serif`;
          ctx.fillText(initials(n.label, n.handle), n.x, n.y + r * 0.04);
          ctx.textBaseline = 'top';
        }
      }
      const ring = Math.max(1.5 / k, r * 0.16);
      ctx.lineWidth = ring;
      ctx.strokeStyle = colors[n.tier] || colors.cold;
      ctx.beginPath(); ctx.arc(n.x, n.y, r - ring / 2 + ring * 0.5, 0, Math.PI * 2); ctx.stroke();
      if (n._hit || n === hl) {
        ctx.lineWidth = 2 / k; ctx.strokeStyle = colors.accent;
        ctx.beginPath(); ctx.arc(n.x, n.y, r + 3 / k, 0, Math.PI * 2); ctx.stroke();
      }
      if ((showNames && !dim) || n._hit || n === hl) {
        ctx.fillStyle = colors.text2; ctx.font = `500 ${11 / k}px Inter, system-ui, sans-serif`;
        ctx.fillText(n.label || n.handle || '', n.x, n.y + r + 3 / k);
      }
    }
    ctx.globalAlpha = 1;

    // seeds on top
    for (const n of nodes) {
      if (n.kind !== 'seed') continue;
      const r = n._r;
      if (!inView(n, r + 100 / k)) continue;
      const dim = hl && !hlSet.has(n.id);
      ctx.globalAlpha = dim ? 0.35 : 1;
      let drawn = false;
      if (n.pic) { const e = img(n.pic); if (e.state === 'ok') { ctx.drawImage(e.c, n.x - r, n.y - r, 2 * r, 2 * r); drawn = true; } else if (e.state === 'idle') requestImg(n.pic); }
      if (!drawn) {
        ctx.fillStyle = colors.text;
        ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = colors.bg; ctx.textBaseline = 'middle';
        ctx.font = `600 ${r * 0.62}px Inter, system-ui, sans-serif`;
        ctx.fillText(initials(null, n.label), n.x, n.y + r * 0.04);
      }
      ctx.lineWidth = 3 / k; ctx.strokeStyle = colors.bg;
      ctx.beginPath(); ctx.arc(n.x, n.y, r + 1.5 / k, 0, Math.PI * 2); ctx.stroke();
      // label pill
      const fs = 12 / k;
      ctx.font = `600 ${fs}px Inter, system-ui, sans-serif`;
      const txt = '@' + n.label;
      const tw = ctx.measureText(txt).width;
      const ly = n.y + r + 6 / k;
      ctx.fillStyle = colors.surface; ctx.strokeStyle = colors.border; ctx.lineWidth = 1 / k;
      const bw = tw + 12 / k, bh = fs + 8 / k;
      roundRect(n.x - bw / 2, ly, bw, bh, 5 / k); ctx.fill(); ctx.stroke();
      ctx.fillStyle = colors.text; ctx.textBaseline = 'top';
      ctx.fillText(txt, n.x, ly + 4 / k);
      ctx.globalAlpha = 1;
    }
  }
  function roundRect(x, y, w, h, r) {
    ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }
  function loop() {
    if (!visible) return;
    if (dirty) { dirty = false; draw(); }
    requestAnimationFrame(loop);
  }

  function makeSim() {
    sim = d3.forceSimulation()
      .force('link', d3.forceLink().id((d) => d.id).distance((l) => 60 + l.target._r * 2))
      .force('charge', d3.forceManyBody().strength((d) => (d.kind === 'seed' ? -900 : -14)).distanceMax(700).theta(0.95))
      .force('collide', d3.forceCollide().radius((d) => d._r + (d.kind === 'seed' ? 10 : 1.5)).iterations(1))
      .force('x', d3.forceX(0).strength(0.015))
      .force('y', d3.forceY(0).strength(0.015))
      .alphaDecay(0.03)
      .on('tick', () => (dirty = true));
    sim.stop();
  }

  function merge(data) {
    if (!sim) makeSim();
    const fresh = byId.size === 0;
    const adj = new Map();
    data.links.forEach((l) => {
      (adj.get(l.target) || adj.set(l.target, []).get(l.target)).push(l.source);
      (adj.get(l.source) || adj.set(l.source, []).get(l.source)).push(l.target);
    });
    const next = new Map();
    const seeds = data.nodes.filter((n) => n.kind === 'seed');
    seeds.forEach((n, i) => {
      let o = byId.get(n.id);
      if (o) Object.assign(o, n);
      else {
        const a = (i / seeds.length) * Math.PI * 2;
        const R = 260 + seeds.length * 30;
        o = { ...n, x: Math.cos(a) * R, y: Math.sin(a) * R };
      }
      o._r = radius(o); next.set(o.id, o);
    });
    data.nodes.forEach((n) => {
      if (n.kind === 'seed') return;
      let o = byId.get(n.id);
      if (o) Object.assign(o, n);
      else {
        const nb = (adj.get(n.id) || []).map((id) => next.get(id)).filter(Boolean);
        let x = 0, y = 0;
        nb.forEach((s) => { x += s.x; y += s.y; });
        if (nb.length) { x /= nb.length; y /= nb.length; }
        const j = nb.length > 1 ? 30 : 90;
        o = { ...n, x: x + (Math.random() - 0.5) * j, y: y + (Math.random() - 0.5) * j };
      }
      o._r = radius(o); next.set(o.id, o);
    });
    byId = next;
    nodes = [...next.values()];
    links = data.links.filter((l) => next.has(l.source) && next.has(l.target)).map((l) => ({ source: l.source, target: l.target, direction: l.direction }));
    sim.nodes(nodes);
    sim.force('link').links(links);
    sim.force('collide').radius((d) => d._r + (d.kind === 'seed' ? 10 : 1.5));
    if (hover && !byId.has(hover.id)) hover = null;
    refilter();
    if (fresh) {
      sim.alpha(1);
      for (let i = 0; i < 160; i++) sim.tick();
      fit(false);
      sim.alpha(0.08).restart();
    } else {
      sim.alpha(Math.max(sim.alpha(), 0.12)).restart();
    }
  }

  async function load(force) {
    if (!visible && !force) return;
    let data;
    try { data = await api.get(`/api/map?scope=${scope}&limit=${scope === 'all' ? 1500 : 400}`); } catch (e) { return; }
    if (!force && data.rev === rev) return;
    rev = data.rev;
    merge(data);
  }

  function fit(animate) {
    const ns = nodes.filter((n) => n._on !== false);
    if (!ns.length || !W) return;
    let a = Infinity, b = Infinity, c = -Infinity, d = -Infinity;
    ns.forEach((n) => { a = Math.min(a, n.x - n._r); b = Math.min(b, n.y - n._r); c = Math.max(c, n.x + n._r); d = Math.max(d, n.y + n._r + 20); });
    const k = Math.min(3, 0.92 * Math.min(W / (c - a), H / (d - b)));
    const to = { k, x: W / 2 - ((a + c) / 2) * k, y: H / 2 - ((b + d) / 2) * k };
    animate ? animateTo(to) : (T = to);
    dirty = true;
  }
  function animateTo(to) {
    const from = { ...T }; const t0 = performance.now();
    const step = (t) => {
      const p = Math.min(1, (t - t0) / 420), e = 1 - Math.pow(1 - p, 3);
      T = { x: from.x + (to.x - from.x) * e, y: from.y + (to.y - from.y) * e, k: from.k + (to.k - from.k) * e };
      dirty = true; if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function hit(px, py) {
    const [x, y] = toWorld(px, py);
    let best = null, bd = Infinity;
    for (const n of nodes) {
      if (n.kind === 'lead' && !n._on) continue;
      const dx = n.x - x, dy = n.y - y, d = Math.sqrt(dx * dx + dy * dy);
      const lim = n._r + 3 / T.k;
      if (d <= lim && (d - n._r) < bd) { best = n; bd = d - n._r; }
    }
    // seeds are drawn on top; prefer them when hit
    return best;
  }

  async function showCard(n, px, py) {
    if (!n || n.kind === 'seed') {
      if (n) {
        hc.innerHTML = `<div class="hc-top">${avatar({ handle: n.label, pic: n.pic }, 44)}<div><div class="nm">@${esc(n.label)}</div><div class="muted num">${n.degree} people linked</div></div></div>`;
        placeCard(px, py); hc.hidden = false;
      } else hc.hidden = true;
      return;
    }
    const pid = n.id.slice(2);
    let p = n.handle && n.reason && n.tags ? { name: n.label, handle: n.handle, pic: n.pic, reason: n.reason, tags: n.tags.map((t) => ({ tag: t, grp: tagGroup(t) })) } : personCache.get(pid);
    const render = (p) => {
      if (hover !== n) return;
      hc.innerHTML = `<div class="hc-top">${avatar({ name: p.name || n.label, handle: p.handle, pic: n.pic }, 44)}<div style="min-width:0"><div class="nm">${esc(p.name || n.label)}</div>${p.handle ? `<div class="handle">@${esc(p.handle)}</div>` : ''}</div>
        <div class="grow"></div>${scoreBadge(n)}</div>
        ${p.tags ? `<div class="tags">${sortTags(p.tags).filter((t) => !/^via @/.test(t.tag)).slice(0, 8).map((t) => tagChip(t)).join('')}</div>` : ''}
        ${p.reason ? `<div class="reason">${esc(p.reason)}</div>` : ''}`;
      placeCard(px, py); hc.hidden = false;
    };
    if (p) return render(p);
    render({ name: n.label });
    try { p = await api.get('/api/person/' + pid); personCache.set(pid, p); render(p); } catch (e) {}
  }
  function tagGroup(t) { return (S.allTags.find((x) => x.tag === t) || {}).grp || (/^(via|follows|followed by|in \d|knows)/.test(t) ? 'source' : 'signal'); }
  function placeCard(px, py) {
    const w = 280, h = hc.offsetHeight || 120;
    let x = px + 16, y = py + 16;
    if (x + w > W - 8) x = px - w - 16;
    if (y + h > H - 8) y = Math.max(8, H - h - 8);
    hc.style.left = x + 'px'; hc.style.top = y + 'px';
  }

  // Pointer interaction
  let drag = null;
  const P = (e) => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  canvas.addEventListener('pointerdown', (e) => {
    canvas.setPointerCapture(e.pointerId);
    const n = hit(P(e)[0], P(e)[1]);
    drag = { n, sx: P(e)[0], sy: P(e)[1], lx: P(e)[0], ly: P(e)[1], moved: false };
    if (!n) canvas.classList.add('dragging');
  });
  canvas.addEventListener('pointermove', (e) => {
    if (drag) {
      const dx = P(e)[0] - drag.lx, dy = P(e)[1] - drag.ly;
      drag.lx = P(e)[0]; drag.ly = P(e)[1];
      if (Math.abs(P(e)[0] - drag.sx) + Math.abs(P(e)[1] - drag.sy) > 3) drag.moved = true;
      if (!drag.moved) return;
      hc.hidden = true;
      if (drag.n) {
        const [x, y] = toWorld(P(e)[0], P(e)[1]);
        drag.n.fx = x; drag.n.fy = y;
        sim.alphaTarget(0.25).restart();
      } else { T.x += dx; T.y += dy; }
      dirty = true;
      return;
    }
    const n = hit(P(e)[0], P(e)[1]);
    canvas.classList.toggle('pointer', !!n);
    if (n !== hover) { hover = n; dirty = true; showCard(n, P(e)[0], P(e)[1]); }
    else if (n) placeCard(P(e)[0], P(e)[1]);
  });
  canvas.addEventListener('pointerup', (e) => {
    canvas.classList.remove('dragging');
    if (!drag) return;
    const d = drag; drag = null;
    if (d.n && d.moved) { d.n.fx = null; d.n.fy = null; sim.alphaTarget(0); }
    if (!d.moved) {
      if (d.n && d.n.kind === 'lead') openDrawer(+d.n.id.slice(2));
      else if (d.n && d.n.kind === 'seed') { focus = focus === d.n ? null : d.n; dirty = true; }
      else { focus = null; dirty = true; }
    }
  });
  canvas.addEventListener('pointerleave', () => { if (!drag) { hover = null; hc.hidden = true; dirty = true; } });
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const f = Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0018));
    const k = Math.max(0.08, Math.min(8, T.k * f));
    const [wx, wy] = toWorld(P(e)[0], P(e)[1]);
    T = { k, x: P(e)[0] - wx * k, y: P(e)[1] - wy * k };
    hc.hidden = true; hover = null; dirty = true;
  }, { passive: false });

  // Toolbar
  function renderTools() {
    $('#map-scope').innerHTML = [['leads', 'Leads'], ['all', 'All']].map(([k, l]) => `<button data-scope="${k}" class="${scope === k ? 'on' : ''}">${l}</button>`).join('');
    $('#map-tier').innerHTML = [['', 'All tiers'], ['hot', 'Hot'], ['warm', 'Warm'], ['cold', 'Cold'], ['unread', 'Unread']].map(([k, l]) =>
      `<button data-mtier="${k}" class="${tier === k ? 'on' : ''}">${k ? `<i class="t t-${k}"></i>` : ''}${l}</button>`).join('');
  }
  $('#map-scope').onclick = (e) => { const b = e.target.closest('button'); if (!b || b.dataset.scope === scope) return; scope = b.dataset.scope; renderTools(); byId = new Map(); rev = null; load(true); };
  $('#map-tier').onclick = (e) => { const b = e.target.closest('button'); if (!b) return; tier = b.dataset.mtier; renderTools(); refilter(); };
  $('#map-tags-btn').onclick = (e) => { e.stopPropagation(); $('#map-tags-pop').hidden = !$('#map-tags-pop').hidden; };
  document.addEventListener('click', (e) => { if (!e.target.closest('.pop-wrap')) $('#map-tags-pop').hidden = true; });
  $('#map-fit').onclick = () => fit(true);
  $('#map-q').addEventListener('input', refilter);
  $('#map-q').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const n = nodes.find((n) => n._hit);
    if (!n) return;
    focus = n;
    const k = Math.max(T.k, 2.6);
    animateTo({ k, x: W / 2 - n.x * k, y: H / 2 - n.y * k });
  });
  renderTools();

  let pollT = null;
  return {
    show() {
      visible = true; resize(); readColors();
      if (!byId.size) load(true); else { load(); dirty = true; }
      requestAnimationFrame(loop);
      clearInterval(pollT); pollT = setInterval(() => { if (!document.hidden) load(); }, 10000);
    },
    hide() { visible = false; clearInterval(pollT); hc.hidden = true; if (sim) sim.stop(); },
    themeChanged() { readColors(); dirty = true; },
    filtersChanged() { refilter(); },
  };
})();

/* ---------- Boot ---------- */
route();
loadCounts();
loadTags();
reloadLeads();
loadScraper();
syncSeedBtn();
setInterval(() => { if (!document.hidden) loadScraper(); }, 5000);
setInterval(() => { if (!document.hidden) { loadCounts(); refreshLeads(); } }, 15000);
setInterval(() => { if (!document.hidden) loadTags(); }, 60000);
