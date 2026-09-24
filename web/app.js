'use strict';
/* Fortunate Leads UI. Plain JS, no build step. */

// ---------- utilities ----------
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (n) => n == null ? '–' : n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M' : n >= 1e4 ? Math.round(n / 1e3) + 'k' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n);
const int = (n) => n == null ? '–' : Number(n).toLocaleString('en-US');
const initials = (s) => (s || '?').replace(/[^\p{L}\p{N} ]/gu, ' ').trim().split(/\s+/).slice(0, 2).map((w) => w[0]).join('') || '?';
const ago = (t) => {
  if (!t) return '–';
  const s = Math.max(0, (Date.now() - Date.parse(t)) / 1000);
  return s < 60 ? Math.round(s) + 's' : s < 3600 ? Math.round(s / 60) + 'm' : s < 86400 ? Math.round(s / 3600) + 'h' : Math.round(s / 86400) + 'd';
};
const left = (t) => {
  const s = Math.max(0, Math.round((Date.parse(t) - Date.now()) / 1000));
  return s >= 3600 ? Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm' : Math.floor(s / 60) + 'm ' + String(s % 60).padStart(2, '0') + 's';
};
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const store = {
  get(k, d) { try { const v = localStorage.getItem('fl-' + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem('fl-' + k, JSON.stringify(v)); } catch (e) { /* storage unavailable */ } },
};

let offlineSince = null;
function setOnline(ok) {
  if (ok) { offlineSince = null; $('#offline').hidden = true; return; }
  offlineSince = offlineSince || Date.now();
  $('#offline').hidden = false;
  $('#offline-t').textContent = ago(new Date(offlineSince).toISOString());
}
const api = {
  async req(url, opts) {
    let r;
    try { r = await fetch(url, { cache: 'no-store', ...opts }); } catch (e) { setOnline(false); throw e; }
    setOnline(true);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  },
  get(url) { return this.req(url); },
  post(url, body) { return this.req(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }); },
};

let toastT;
function toast(msg) {
  const el = $('#toast');
  el.textContent = msg; el.hidden = false;
  clearTimeout(toastT); toastT = setTimeout(() => { el.hidden = true; }, 2200);
}

function avatar(pic, name, cls = '') {
  const i = esc(initials(name));
  if (!pic) return `<span class="av ${cls}">${i}</span>`;
  return `<span class="av ${cls}" data-i="${i}"><img src="${esc(pic)}" alt="" loading="lazy"></span>`;
}
// Broken pictures fall back to initials (error events do not bubble, so capture).
document.addEventListener('error', (e) => {
  const img = e.target;
  if (img.tagName === 'IMG' && img.parentElement?.classList.contains('av')) img.parentElement.textContent = img.parentElement.dataset.i || '?';
}, true);

// ---------- constants ----------
const STATUSES = ['good', 'maybe', 'no', 'contacted', 'client', 'known'];
const LABEL = { good: 'Good', maybe: 'Maybe', no: 'No', contacted: 'Contacted', client: 'Client', known: 'Known' };
const CYCLE = [null, 'good', 'maybe', 'no'];
const GROUPS = [['role', 'Role'], ['niche', 'Niche'], ['signal', 'Signal'], ['size', 'Size']];
const PAGE = 100;
const isListTag = (t) => /^in \d+ lists$/.test(t);
const isViaTag = (t) => t.startsWith('via @');

// ---------- state ----------
const S = {
  view: 'leads',
  f: { q: '', sort: store.get('sort', 'connected'), status: '', min: 0, tags: [] },
  tags: [], counts: null, sc: null,
  rows: [], total: null, done: false, loading: false, error: false, gen: 0,
  sel: -1, open: null, person: null,
  tagMore: {},
};

// ---------- theme ----------
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  $('#theme-btn').textContent = t === 'dark' ? 'Light' : 'Dark';
  store.set('theme', t);
  if (M.sim) M.draw();
}
$('#theme-btn').onclick = () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');

// ---------- routing ----------
function route() {
  const v = (location.hash.match(/^#\/(\w+)/) || [])[1];
  S.view = ['leads', 'map', 'scraper'].includes(v) ? v : 'leads';
  $$('.view').forEach((el) => el.classList.toggle('on', el.id === 'view-' + S.view));
  $$('.tabs a').forEach((a) => a.classList.toggle('on', a.dataset.view === S.view));
  const detail = $('#detail');
  if (S.view === 'map') { $('.map-wrap').appendChild(detail); M.show(); }
  else { $('#view-leads').appendChild(detail); M.hide(); }
  if (S.view === 'scraper') renderScraper();
}
window.addEventListener('hashchange', route);

// ---------- tags & counts ----------
function mergedTags() {
  const m = new Map();
  for (const t of S.tags) {
    const e = m.get(t.tag) || { tag: t.tag, grp: t.grp, count: 0, manual: false };
    e.count += t.count; if (t.source === 'manual') e.manual = true;
    m.set(t.tag, e);
  }
  return [...m.values()];
}
async function loadTags() {
  try { S.tags = await api.get('/api/tags'); } catch (e) { return; }
  renderFilters();
  const dl = $('#tag-dl') || document.body.appendChild(Object.assign(document.createElement('datalist'), { id: 'tag-dl' }));
  dl.innerHTML = mergedTags().filter((t) => t.grp !== 'source').map((t) => `<option value="${esc(t.tag)}">`).join('');
}
async function loadCounts() {
  try { S.counts = await api.get('/api/counts'); } catch (e) { return; }
  $('#n-leads').textContent = fmt(S.counts.total);
  renderFilters();
}

// ---------- filters ----------
function fitem(key, val, label, count, on, box = '') {
  return `<button class="fitem${on ? ' on' : ''}" data-k="${key}" data-v="${esc(val)}">${box ? `<i class="box ${box}"></i>` : ''}<span>${esc(label)}</span>${count != null ? `<b class="num">${fmt(count)}</b>` : ''}</button>`;
}
function tagSection(key, title, list, box) {
  if (!list.length) return '';
  const lim = S.tagMore[key] ? 200 : 8;
  const sel = list.filter((t) => S.f.tags.includes(t.tag));
  const shown = list.slice(0, lim);
  sel.forEach((t) => { if (!shown.includes(t)) shown.push(t); });
  return `<div class="fsec"><h4>${esc(title)}</h4>${shown.map((t) => fitem('tag', t.tag, isViaTag(t.tag) ? t.tag.slice(4) : t.tag, t.count, S.f.tags.includes(t.tag), box)).join('')}
    ${list.length > lim ? `<button class="fmore" data-more="${key}">${list.length - lim} more</button>` : ''}</div>`;
}
function renderFilters() {
  const c = S.counts || {};
  const tags = mergedTags();
  const own = tags.filter((t) => t.manual);
  const via = tags.filter((t) => isViaTag(t.tag));
  let h = `<div class="fsec"><h4>Status ${S.f.status || S.f.min || S.f.tags.length ? '<button id="f-reset">Reset</button>' : ''}</h4>
    ${fitem('status', '', 'Open', c.total, !S.f.status)}
    ${STATUSES.map((s) => fitem('status', s, LABEL[s], c[s] ?? null, S.f.status === s)).join('')}</div>
    <div class="fsec"><h4>Lists</h4>
    ${[[0, 'Any'], [2, 'In 2+ lists'], [3, 'In 3+ lists'], [5, 'In 5+ lists']].map(([n, l]) => fitem('min', n, l, null, S.f.min === n)).join('')}</div>`;
  h += tagSection('own', 'Your tags', own, '');
  h += tagSection('via', 'Seeds', via, 'dash');
  for (const [g, title] of GROUPS) h += tagSection(g, title, tags.filter((t) => t.grp === g && !t.manual), 'dash');
  if (!own.length) h += `<div class="fsec"><h4>Your tags</h4><span class="muted">None yet</span></div>`;
  $('#filters').innerHTML = h;
  renderChips();
}
$('#filters').addEventListener('click', (e) => {
  const more = e.target.closest('[data-more]');
  if (more) { S.tagMore[more.dataset.more] = true; renderFilters(); return; }
  if (e.target.id === 'f-reset') { Object.assign(S.f, { status: '', min: 0, tags: [] }); return refilter(); }
  const b = e.target.closest('.fitem');
  if (!b) return;
  const { k, v } = b.dataset;
  if (k === 'status') S.f.status = v;
  else if (k === 'min') S.f.min = +v;
  else if (k === 'tag') S.f.tags = S.f.tags.includes(v) ? S.f.tags.filter((t) => t !== v) : [...S.f.tags, v];
  refilter();
});
function renderChips() {
  const chips = [];
  if (S.f.status) chips.push(['status', LABEL[S.f.status]]);
  if (S.f.min) chips.push(['min', `${S.f.min}+ lists`]);
  S.f.tags.forEach((t) => chips.push(['tag:' + t, t]));
  $('#chips').innerHTML = chips.map(([k, l]) => `<button class="chip-x" data-k="${esc(k)}">${esc(l)}</button>`).join('');
}
$('#chips').addEventListener('click', (e) => {
  const b = e.target.closest('[data-k]');
  if (!b) return;
  const k = b.dataset.k;
  if (k === 'status') S.f.status = ''; else if (k === 'min') S.f.min = 0; else S.f.tags = S.f.tags.filter((t) => 'tag:' + t !== k);
  refilter();
});
function refilter() { renderFilters(); resetLeads(); }
$('#filters-btn').onclick = () => $('#filters').classList.toggle('show');
document.addEventListener('click', (e) => {
  const f = $('#filters');
  if (f.classList.contains('show') && !f.contains(e.target) && e.target.id !== 'filters-btn') f.classList.remove('show');
});

$('#q').addEventListener('input', debounce((e) => { S.f.q = e.target.value.trim(); resetLeads(); }, 180));
$('#sort').value = S.f.sort;
$('#sort').onchange = (e) => { S.f.sort = e.target.value; store.set('sort', S.f.sort); resetLeads(); };

// ---------- leads list ----------
function leadParams() {
  const p = new URLSearchParams();
  if (S.f.q) p.set('q', S.f.q);
  if (S.f.status) p.set('status', S.f.status);
  if (S.f.min) p.set('min_lists', S.f.min);
  if (S.f.tags.length) p.set('tags', S.f.tags.join(','));
  p.set('sort', S.f.sort);
  return p;
}
const lists = (r) => r.lists ?? (r.via ? new Set(r.via).size : 0);

async function resetLeads(keep) {
  const gen = ++S.gen;
  if (!keep) { S.rows = []; S.total = null; S.sel = -1; $('#scroll').scrollTop = 0; }
  S.done = false; S.error = false; S.loading = false;
  renderRows();
  await loadMore(gen, keep ? Math.max(PAGE, S.rows.length) : PAGE, keep);
}
async function loadMore(gen = S.gen, n = PAGE, replace = false) {
  if (S.loading || (S.done && !replace)) return;
  S.loading = true; renderRows();
  const p = leadParams();
  p.set('offset', replace ? 0 : S.rows.length); p.set('limit', Math.min(500, n));
  try {
    const d = await api.get('/api/leads?' + p);
    if (gen !== S.gen) return;
    S.total = d.total;
    if (replace) S.rows = d.rows; else S.rows.push(...d.rows);
    S.done = S.rows.length >= d.total || !d.rows.length;
  } catch (e) {
    if (gen === S.gen) S.error = true;
  } finally {
    if (gen === S.gen) { S.loading = false; renderRows(); }
  }
}

const rowH = () => parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--row')) || 44;
function rowHTML(r, i) {
  const n = lists(r);
  const tags = (r.tags || []).filter((t) => t.grp !== 'source' || t.tag === 'knows you')
    .sort((a, b) => (b.source === 'manual') - (a.source === 'manual')).slice(0, 5);
  const cls = ['row', i === S.sel ? 'sel' : '', S.open === r.id ? 'open' : '', r.status === 'no' ? 'st-no' : ''].join(' ');
  return `<div class="${cls}" data-i="${i}" style="top:${i * rowH()}px">
    ${avatar(r.pic, r.name || r.handle)}
    <div class="who"><b>${esc(r.name || r.handle)}</b><span>@${esc(r.handle)}</span></div>
    <div class="bio${r.bio ? '' : ' none'}">${r.bio ? esc(r.bio) : 'No bio'}</div>
    <span class="num r">${fmt(r.followers)}</span>
    <span class="num r lists-n${n >= 2 ? ' hi' : ''}">${n}</span>
    <div class="tags c-tags">${tags.map((t) => `<span class="tag${t.source === 'manual' ? ' man' : ''}">${esc(t.tag)}</span>`).join('')}</div>
    <span class="stat c-st ${r.status || ''}">${r.status ? `<b></b>${LABEL[r.status]}` : ''}</span>
  </div>`;
}
function renderRows() {
  const box = $('#rows'), sc = $('#scroll'), h = rowH();
  $('#count').textContent = S.total == null ? '' : int(S.total) + (S.total === 1 ? ' person' : ' people');
  if (!S.rows.length) {
    box.style.height = '100%';
    if (S.loading || S.total == null && !S.error) {
      box.innerHTML = Array.from({ length: 14 }, (_, i) => `<div class="skel" style="top:${i * h}px"><i></i><i style="width:${120 + (i * 37) % 80}px"></i><i style="width:${200 + (i * 53) % 160}px"></i></div>`).join('');
    } else if (S.error) {
      box.innerHTML = `<div class="empty"><b>Could not load leads</b><button class="btn" id="retry">Retry</button></div>`;
    } else {
      const filtered = S.f.q || S.f.status || S.f.min || S.f.tags.length;
      box.innerHTML = `<div class="empty"><b>${filtered ? 'No matches' : 'No leads yet'}</b>${filtered ? '<button class="btn" id="clear-all">Clear filters</button>' : '<a class="btn" href="#/scraper">Add seeds</a>'}</div>`;
    }
    return;
  }
  box.style.height = S.rows.length * h + 'px';
  const from = Math.max(0, Math.floor(sc.scrollTop / h) - 8);
  const to = Math.min(S.rows.length, Math.ceil((sc.scrollTop + sc.clientHeight) / h) + 8);
  let out = '';
  for (let i = from; i < to; i++) out += rowHTML(S.rows[i], i);
  box.innerHTML = out;
  if (!S.done && to >= S.rows.length - 20) loadMore();
}
$('#scroll').addEventListener('scroll', () => requestAnimationFrame(renderRows), { passive: true });
window.addEventListener('resize', debounce(() => { renderRows(); M.resize(); }, 60));
$('#rows').addEventListener('click', (e) => {
  if (e.target.id === 'retry') return resetLeads();
  if (e.target.id === 'clear-all') { Object.assign(S.f, { q: '', status: '', min: 0, tags: [] }); $('#q').value = ''; return refilter(); }
  const row = e.target.closest('.row');
  if (!row) return;
  select(+row.dataset.i);
  openDetail(S.rows[S.sel].id);
});

function select(i, scroll) {
  if (!S.rows.length) return;
  S.sel = Math.max(0, Math.min(S.rows.length - 1, i));
  if (scroll) {
    const sc = $('#scroll'), h = rowH(), top = S.sel * h;
    if (top < sc.scrollTop) sc.scrollTop = top;
    else if (top + h > sc.scrollTop + sc.clientHeight) sc.scrollTop = top + h - sc.clientHeight;
  }
  renderRows();
}
function patchRow(id, patch) {
  const r = S.rows.find((x) => x.id === id);
  if (r) Object.assign(r, patch);
  if (S.person && S.person.id === id) Object.assign(S.person, patch);
  renderRows();
}

// ---------- marking ----------
async function mark(id, status) {
  const r = S.rows.find((x) => x.id === id) || (S.person?.id === id ? S.person : null);
  const prev = r ? r.status : null;
  patchRow(id, { status });
  if (S.person?.id === id) renderDetail();
  try { await api.post(`/api/person/${id}/mark`, { status }); loadCounts(); }
  catch (e) { patchRow(id, { status: prev }); if (S.person?.id === id) renderDetail(); toast('Could not save'); }
}
function current() { return S.open ? S.person || S.rows.find((r) => r.id === S.open) : S.rows[S.sel]; }

// ---------- detail ----------
async function openDetail(id) {
  S.open = id;
  const base = S.rows.find((r) => r.id === id);
  S.person = base ? { ...base, loading: true } : { id, loading: true, handle: '', tags: [] };
  $('#detail').hidden = false;
  renderDetail(); renderRows();
  try {
    const p = await api.get('/api/person/' + id);
    if (S.open !== id) return;
    S.person = p;
  } catch (e) {
    if (S.open !== id) return;
    S.person.loading = false; S.person.failed = true;
  }
  renderDetail();
}
function closeDetail() {
  S.open = null; S.person = null;
  $('#detail').hidden = true;
  renderRows();
}
function renderDetail() {
  const p = S.person;
  if (!p) return;
  const edges = p.edges || (p.via || []).map((s) => ({ seed: s }));
  const n = p.lists ?? new Set(edges.map((e) => e.seed)).size;
  const tags = (p.tags || []).filter((t) => t.grp !== 'source' || t.source === 'manual');
  const site = p.website ? p.website.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, '') : '';
  $('#detail').innerHTML = `
    <div class="d-head">${avatar(p.pic, p.name || p.handle, 'lg')}
      <div class="who"><b>${esc(p.name || p.handle || '…')}</b><span>@${esc(p.handle)}</span></div>
      <button class="d-close" id="d-close" title="Close (esc)">&times;</button></div>
    <div class="d-stats">
      <div><b>${fmt(p.followers)}</b><span>Followers</span></div><div><b>${fmt(p.following)}</b><span>Following</span></div>
      <div><b>${fmt(p.posts)}</b><span>Posts</span></div><div><b>${n || '–'}</b><span>Lists</span></div></div>
    <div class="d-sec"><div class="d-bio${p.bio ? '' : ' muted'}">${p.bio ? esc(p.bio) : p.loading ? '' : 'No bio read yet'}</div>
      <div class="d-links" style="margin-top:10px">
        <a class="btn" href="https://www.instagram.com/${encodeURIComponent(p.handle)}/" target="_blank" rel="noopener">Instagram <kbd>o</kbd></a>
        ${site ? `<a class="btn" href="${esc(p.website)}" target="_blank" rel="noopener">${esc(site)}</a>` : ''}
        ${!p.bio && !p.loading ? '<button class="btn" id="d-read">Read profile</button>' : ''}</div></div>
    <div class="d-sec"><h4>Status</h4><div class="marks">${STATUSES.map((s, i) => `<button data-s="${s}" class="${p.status === s ? 'on' : ''}">${LABEL[s]}<kbd>${i + 1}</kbd></button>`).join('')}</div></div>
    <div class="d-sec"><h4>Tags</h4><div class="d-tags">${tags.length ? tags.map((t) => t.source === 'manual'
      ? `<span class="tag man">${esc(t.tag)}<button data-rm="${esc(t.tag)}" title="Remove">&times;</button></span>`
      : `<span class="tag">${esc(t.tag)}</span>`).join('') : '<span class="muted">None</span>'}</div>
      <form class="tag-add" id="tag-form"><input class="input" id="tag-in" list="tag-dl" placeholder="Add tag" autocomplete="off"><button class="btn">Add</button></form></div>
    <div class="d-sec"><h4>Found in</h4><div class="edges">${edges.length ? edges.map((e) => `<div><b>@${esc(e.seed)}</b><span>${e.direction === 'following' ? 'followed by' : e.direction === 'followers' ? 'follows' : ''}</span></div>`).join('') : '<span class="muted">–</span>'}</div></div>
    <div class="d-sec"><h4>Note</h4><textarea class="input" id="note" placeholder="Note">${esc(p.note || '')}</textarea><div class="d-note" id="note-st"></div></div>`;
}
$('#detail').addEventListener('click', async (e) => {
  const p = S.person;
  if (!p) return;
  if (e.target.id === 'd-close') return closeDetail();
  const m = e.target.closest('[data-s]');
  if (m) return mark(p.id, p.status === m.dataset.s ? null : m.dataset.s);
  const rm = e.target.closest('[data-rm]');
  if (rm) return editTags(p.id, [], [rm.dataset.rm]);
  if (e.target.id === 'd-read') {
    try { await api.post(`/api/person/${p.id}/read`); toast('Profile read queued'); } catch (err) { toast('Could not queue'); }
  }
});
$('#detail').addEventListener('submit', (e) => {
  e.preventDefault();
  const v = $('#tag-in').value.trim();
  if (v && S.person) editTags(S.person.id, [v], []);
});
const saveNote = debounce(async (id, note) => {
  try {
    await api.post(`/api/person/${id}/mark`, { status: S.person?.id === id ? S.person.status || null : null, note });
    if (S.person?.id === id) { S.person.note = note; $('#note-st').textContent = 'Saved'; }
  } catch (e) { if ($('#note-st')) $('#note-st').textContent = 'Not saved'; }
}, 600);
$('#detail').addEventListener('input', (e) => {
  if (e.target.id === 'note' && S.person) { $('#note-st').textContent = ''; saveNote(S.person.id, e.target.value); }
});
async function editTags(id, add, remove) {
  try { await api.post(`/api/person/${id}/tags`, { add, remove }); } catch (e) { toast('Could not save tag'); return; }
  try {
    const p = await api.get('/api/person/' + id);
    if (S.person?.id === id) { S.person = p; renderDetail(); $('#tag-in')?.focus(); }
    patchRow(id, { tags: p.tags });
  } catch (e) { /* offline banner covers it */ }
  loadTags();
}

// ---------- keyboard ----------
let gPending = 0;
document.addEventListener('keydown', (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if (e.key === 'Escape') {
    if (!$('#help').hidden) { $('#help').hidden = true; return; }
    if (typing) { e.target.blur(); return; }
    if ($('#filters').classList.contains('show')) { $('#filters').classList.remove('show'); return; }
    if (S.open) { closeDetail(); return; }
    if (S.view === 'leads' && S.sel >= 0) { S.sel = -1; renderRows(); }
    return;
  }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  if (gPending && Date.now() - gPending < 800) {
    gPending = 0;
    const to = { l: 'leads', m: 'map', s: 'scraper' }[e.key];
    if (to) { location.hash = '#/' + to; e.preventDefault(); }
    return;
  }
  const k = e.key;
  if (k === 'g') { gPending = Date.now(); return; }
  if (k === '?') { $('#help').hidden = !$('#help').hidden; return; }
  if (k === '/') { e.preventDefault(); (S.view === 'map' ? $('#map-q') : $('#q')).focus(); return; }
  if (S.view !== 'leads') {
    const p = S.person;
    if (p && /^[0-6]$/.test(k)) mark(p.id, k === '0' ? null : STATUSES[+k - 1]);
    if (p && k === 'o') window.open(`https://www.instagram.com/${encodeURIComponent(p.handle)}/`, '_blank', 'noopener');
    return;
  }
  if (k === 'j' || k === 'ArrowDown' || k === 'k' || k === 'ArrowUp') {
    e.preventDefault();
    select(S.sel < 0 ? 0 : S.sel + (k === 'j' || k === 'ArrowDown' ? 1 : -1), true);
    if (S.open && S.rows[S.sel]) openDetail(S.rows[S.sel].id);
    return;
  }
  const r = current();
  if (k === 'Enter' && S.rows[S.sel]) { openDetail(S.rows[S.sel].id); return; }
  if (!r) return;
  if (k === 'm') { mark(r.id, CYCLE[(CYCLE.indexOf(r.status ?? null) + 1) % CYCLE.length]); return; }
  if (/^[0-6]$/.test(k)) { mark(r.id, k === '0' ? null : STATUSES[+k - 1]); return; }
  if (k === 'o') { window.open(`https://www.instagram.com/${encodeURIComponent(r.handle)}/`, '_blank', 'noopener'); return; }
  if (k === 't') { e.preventDefault(); if (!S.open) openDetail(r.id).then(() => $('#tag-in')?.focus()); else $('#tag-in')?.focus(); }
});
$('#help-btn').onclick = () => { $('#help').hidden = false; };
$('#help').onclick = () => { $('#help').hidden = true; };

// ---------- scraper ----------
function scState() {
  const sc = S.sc;
  if (!sc) return { label: 'Connecting', dot: '' };
  const x = sc.ext || {};
  if (!x.online) return { label: 'Extension offline', dot: 'hollow' };
  if (sc.paused || x.state === 'paused') return { label: 'Paused', dot: '' };
  if (x.cooldown_until && Date.parse(x.cooldown_until) > Date.now()) return { label: 'Cooldown ' + left(x.cooldown_until), dot: 'hollow' };
  if (x.state === 'running') return { label: 'Running', dot: 'run' };
  return { label: 'Idle', dot: 'on' };
}
function renderStatus() {
  const sc = S.sc, x = sc?.ext || {}, st = scState();
  $('#st-dot').className = 'dot ' + st.dot;
  $('#st-label').textContent = st.label;
  const run = sc?.lists?.find((l) => l.state === 'running');
  $('#st-act').textContent = x.last_error && !x.online ? x.last_error : x.activity || (run ? `@${run.seed} ${run.direction}` : x.text || '');
  const t = x.today?.list, b = x.budget?.list;
  $('#st-today').textContent = t == null ? '–' : `${int(t)}/${b == null ? '–' : int(b)}`;
  $('#st-meter').style.width = t != null && b ? Math.min(100, (t / b) * 100) + '%' : '0';
  $('#st-pph').textContent = x.rate?.pages_hour != null ? int(Math.round(x.rate.pages_hour)) : '–';
  $('#st-peh').textContent = x.rate?.people_hour != null ? int(Math.round(x.rate.people_hour)) : '–';
  $('#st-hit').textContent = x.rate?.last_hit_at ? ago(x.rate.last_hit_at) + ' ago' : 'none';
  $('#pause-btn').textContent = sc?.paused ? 'Resume' : 'Pause';
  $('#pause-btn').classList.toggle('solid', !!sc?.paused);
  $('#qualify-btn').classList.toggle('on', !!sc?.qualify);
  const q = sc ? (sc.queue?.list || 0) + (sc.queue?.profile || 0) : 0;
  $('#n-queue').textContent = q ? fmt(q) : '';
}
async function loadScraper() {
  try { S.sc = await api.get('/api/scraper'); } catch (e) { /* keep last */ }
  renderStatus();
  if (S.view === 'scraper') renderScraper();
}
$('#pause-btn').onclick = async () => {
  if (!S.sc) return;
  const paused = !S.sc.paused;
  S.sc.paused = paused; renderStatus();
  try { await api.post('/api/scraper/pause', { paused }); } catch (e) { S.sc.paused = !paused; renderStatus(); toast('Could not reach server'); }
  loadScraper();
};
$('#qualify-btn').onclick = async () => {
  if (!S.sc) return;
  const on = !S.sc.qualify;
  S.sc.qualify = on; renderStatus();
  try { await api.post('/api/settings/qualify', { on }); toast(on ? 'Qualify on' : 'Qualify off'); }
  catch (e) { S.sc.qualify = !on; renderStatus(); toast('Could not save'); }
};

let listFilter = 'all';
function renderScraper() {
  const sc = S.sc;
  if (!sc) { $('#kpis').innerHTML = '<div class="kpi"><span>Loading</span><b>–</b></div>'; return; }
  const x = sc.ext || {}, st = scState(), ls = sc.lists || [];
  const done = ls.filter((l) => l.state === 'done').length;
  const recv = ls.reduce((a, l) => a + (l.received || 0), 0);
  const tl = x.today?.list, bl = x.budget?.list, tp = x.today?.profile, bp = x.budget?.profile;
  const kpi = (label, val, sub, pct) => `<div class="kpi"><span>${label}</span><b>${val}</b>${sub ? `<small>${sub}</small>` : ''}${pct != null ? `<div class="meter"><i style="width:${Math.min(100, pct)}%"></i></div>` : ''}</div>`;
  $('#kpis').innerHTML = [
    kpi('State', esc(st.label.split(' ')[0]), x.cooldown_until && Date.parse(x.cooldown_until) > Date.now() ? left(x.cooldown_until) : x.online ? 'seen ' + ago(x.last_seen) + ' ago' : 'last ' + ago(x.last_seen)),
    kpi('Pages today', int(tl), bl != null ? 'of ' + int(bl) : '', bl ? (tl / bl) * 100 : null),
    kpi('Profiles today', int(tp), bp != null ? 'of ' + int(bp) : '', bp ? (tp / bp) * 100 : null),
    kpi('Pages / hour', x.rate?.pages_hour != null ? int(Math.round(x.rate.pages_hour)) : '–', x.rate?.last_hit_at ? 'limit hit ' + ago(x.rate.last_hit_at) + ' ago' : 'no limit hits'),
    kpi('People / hour', x.rate?.people_hour != null ? int(Math.round(x.rate.people_hour)) : '–', sc.people_today != null ? int(sc.people_today) + ' new today' : ''),
    kpi('Lists', `${done}/${ls.length}`, int(recv) + ' received', ls.length ? (done / ls.length) * 100 : null),
  ].join('');
  $('#ext-ver').textContent = x.version ? 'v' + x.version : '';
  $('#ext-kv').innerHTML = [
    ['Connection', x.online ? 'Online' : 'Offline'],
    ['Activity', x.activity || x.text || '–'],
    ['Queue', `${int(sc.queue?.list || 0)} lists, ${int(sc.queue?.profile || 0)} profiles`],
    ['Qualify', sc.qualify ? 'On' : 'Off'],
    ['Last error', x.last_error || 'None'],
  ].map(([k, v]) => `<span>${k}</span><b>${esc(v)}</b>`).join('');
  const bL = $('#b-list'), bP = $('#b-profile');
  if (document.activeElement !== bL && document.activeElement !== bP) { bL.value = bl ?? ''; bP.value = bp ?? ''; }

  const groups = { all: ls, active: ls.filter((l) => l.state === 'running' || l.state === 'queued'), done: ls.filter((l) => l.state === 'done'), issues: ls.filter((l) => ['error', 'private', 'paused'].includes(l.state)) };
  $('#lists-f').innerHTML = Object.entries(groups).map(([k, v]) => `<button data-v="${k}" class="${listFilter === k ? 'on' : ''}">${k[0].toUpperCase() + k.slice(1)} ${v.length}</button>`).join('');
  $('#lists-n').textContent = '';
  const order = { running: 0, queued: 1, paused: 2, error: 3, private: 4, done: 5 };
  const rows = [...groups[listFilter]].sort((a, b) => (order[a.state] ?? 9) - (order[b.state] ?? 9) || (b.updated_at || '').localeCompare(a.updated_at || ''));
  $('#lists-body').innerHTML = rows.length ? rows.map((l) => {
    const pct = l.total ? Math.min(100, (l.received / l.total) * 100) : null;
    return `<tr><td><b>@${esc(l.seed)}</b></td><td class="hide-sm muted">${esc(l.direction)}</td>
      <td class="prog"><div class="bar-p ${pct == null ? 'unknown' : l.state === 'done' ? 'done' : l.state === 'running' ? 'run' : ''}"><i style="width:${pct ?? 0}%"></i></div></td>
      <td class="r num">${int(l.received)}${l.total ? ' / ' + int(l.total) : ''}</td>
      <td><span class="state ${esc(l.state)}" title="${esc(l.error || '')}">${l.state === 'running' ? '<i class="dot run"></i>' : ''}${esc(l.state)}</span></td>
      <td class="r num muted hide-sm">${ago(l.updated_at)}</td></tr>`;
  }).join('') : `<tr><td colspan="6" class="muted">No lists</td></tr>`;
}
$('#lists-f').addEventListener('click', (e) => { const b = e.target.closest('[data-v]'); if (b) { listFilter = b.dataset.v; renderScraper(); } });
$('#b-save').onclick = async () => {
  const list = +$('#b-list').value, profile = +$('#b-profile').value;
  try { await api.post('/api/scraper/budget', { list, profile }); toast('Budget saved'); loadScraper(); } catch (e) { toast('Could not save'); }
};
function parseHandles(s) {
  const out = new Set();
  for (let t of s.split(/[\s,;]+/)) {
    t = t.trim().replace(/^https?:\/\/(www\.)?instagram\.com\//i, '').replace(/^@/, '').split(/[/?#]/)[0].toLowerCase();
    if (/^[a-z0-9._]{1,30}$/.test(t)) out.add(t);
  }
  return [...out];
}
const seedDirs = () => $$('#seed-dir button.on').map((b) => b.dataset.v);
function syncSeed() {
  const n = parseHandles($('#seed-in').value).length;
  $('#seed-n').textContent = n ? n + (n === 1 ? ' account' : ' accounts') : '';
  $('#seed-add').disabled = !n || !seedDirs().length;
}
$('#seed-in').addEventListener('input', syncSeed);
$('#seed-dir').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) { b.classList.toggle('on'); syncSeed(); } });
$('#seed-add').onclick = async () => {
  const handles = parseHandles($('#seed-in').value);
  if (!handles.length) return;
  try {
    const r = await api.post('/api/scraper/seeds', { handles, directions: seedDirs() });
    toast(r.queued != null ? `${r.queued} lists queued` : 'Queued');
    $('#seed-in').value = ''; syncSeed(); loadScraper();
  } catch (e) { toast('Could not queue'); }
};

// ---------- map ----------
const M = {
  sim: null, nodes: [], links: [], byId: new Map(), rev: null, scope: 'leads', k: 1, x: 0, y: 0,
  w: 0, h: 0, hover: null, match: new Set(), timer: null, loaded: false, fitted: false,
  show() {
    this.resize();
    if (!this.loaded) this.load();
    clearInterval(this.timer); this.timer = setInterval(() => this.load(), 30000);
    if (this.sim && this.sim.alpha() > this.sim.alphaMin()) this.sim.restart();
  },
  hide() { clearInterval(this.timer); if (this.sim) this.sim.stop(); $('#hover').hidden = true; },
  resize() {
    const c = $('#canvas'), st = $('#stage');
    if (!st.clientWidth) return;
    const dpr = window.devicePixelRatio || 1;
    this.w = st.clientWidth; this.h = st.clientHeight;
    c.width = this.w * dpr; c.height = this.h * dpr;
    this.ctx = c.getContext('2d'); this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.draw();
  },
  async load() {
    let d;
    try { d = await api.get(`/api/map?scope=${this.scope}&limit=${this.scope === 'all' ? 2000 : 600}`); } catch (e) { this.status('Could not load map'); return; }
    this.loaded = true;
    if (d.rev === this.rev && this.nodes.length) return;
    this.rev = d.rev;
    const old = this.byId;
    this.nodes = d.nodes.map((n) => {
      const o = old.get(n.id);
      const seed = n.kind === 'seed';
      return Object.assign(n, { r: seed ? 7 + Math.min(10, Math.sqrt(n.degree || 0) / 3) : n.degree >= 2 ? 4 : 2.5 }, o ? { x: o.x, y: o.y, vx: o.vx, vy: o.vy } : {});
    });
    this.byId = new Map(this.nodes.map((n) => [n.id, n]));
    this.links = d.links.filter((l) => this.byId.has(l.source) && this.byId.has(l.target)).map((l) => ({ ...l }));
    const leads = this.nodes.filter((n) => n.kind === 'lead').length;
    $('#map-count').textContent = `${int(leads)} people, ${this.nodes.length - leads} seeds`;
    if (!leads) this.status('No connections yet');
    this.simulate(old.size ? 0.3 : 1);
    this.search();
  },
  status(t) {
    const c = this.ctx; if (!c) return;
    c.clearRect(0, 0, this.w, this.h);
    c.fillStyle = css('--fg3'); c.font = '13px ' + css('--sans'); c.textAlign = 'center';
    c.fillText(t, this.w / 2, this.h / 2);
  },
  simulate(alpha) {
    if (this.sim) this.sim.stop();
    const F = window.d3;
    if (!F?.forceSimulation) { this.status('Map library missing'); return; }
    this.sim = F.forceSimulation(this.nodes)
      .force('link', F.forceLink(this.links).id((n) => n.id).distance((l) => l.source.kind === 'seed' && l.target.kind === 'seed' ? 120 : 40).strength((l) => 0.6 / Math.max(1, l.target.degree || 1)))
      .force('charge', F.forceManyBody().strength((n) => n.kind === 'seed' ? -260 : -14).distanceMax(400).theta(0.95))
      .force('collide', F.forceCollide((n) => n.r + 1.5).iterations(1))
      .force('x', F.forceX(0).strength(0.03)).force('y', F.forceY(0).strength(0.03))
      .alpha(alpha).alphaDecay(0.035).velocityDecay(0.45)
      .on('tick', () => this.draw())
      .on('end', () => { if (!this.fitted) { this.fit(); this.fitted = true; } });
    if (alpha >= 1) {
      for (let i = 0; i < 120; i++) this.sim.tick();
      this.fit(); this.fitted = true;
    }
    if (S.view !== 'map') this.sim.stop();
  },
  fit() {
    if (!this.nodes.length || !this.w) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of this.nodes) { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x); y1 = Math.max(y1, n.y); }
    const pad = 40;
    this.k = Math.min(4, Math.max(0.1, Math.min((this.w - pad * 2) / (x1 - x0 || 1), (this.h - pad * 2) / (y1 - y0 || 1))));
    this.x = this.w / 2 - ((x0 + x1) / 2) * this.k; this.y = this.h / 2 - ((y0 + y1) / 2) * this.k;
    this.draw();
  },
  draw() {
    const c = this.ctx;
    if (!c || S.view !== 'map') return;
    const fg = css('--fg'), fg2 = css('--fg2'), fg3 = css('--fg3'), bg = css('--bg'), line = css('--line2');
    c.clearRect(0, 0, this.w, this.h);
    c.save(); c.translate(this.x, this.y); c.scale(this.k, this.k);
    const hov = this.hover, hl = hov ? new Set([hov.id]) : null;
    c.lineWidth = 0.6 / this.k;
    c.strokeStyle = line; c.globalAlpha = hov || this.match.size ? 0.25 : 0.6;
    c.beginPath();
    for (const l of this.links) { c.moveTo(l.source.x, l.source.y); c.lineTo(l.target.x, l.target.y); }
    c.stroke();
    if (hov) {
      c.globalAlpha = 1; c.strokeStyle = fg2; c.lineWidth = 1 / this.k; c.beginPath();
      for (const l of this.links) if (l.source === hov || l.target === hov) { c.moveTo(l.source.x, l.source.y); c.lineTo(l.target.x, l.target.y); hl.add(l.source.id); hl.add(l.target.id); }
      c.stroke();
    }
    c.globalAlpha = 1;
    const dim = hov || this.match.size;
    for (const n of this.nodes) {
      if (n.kind === 'seed') continue;
      const on = hl ? hl.has(n.id) : this.match.size ? this.match.has(n.id) : true;
      const openN = S.open && n.id === 'p:' + S.open;
      c.globalAlpha = on || openN ? 1 : dim ? 0.2 : 1;
      const r = n.r, s = r * 2;
      if (n.degree >= 2) { c.fillStyle = bg; c.fillRect(n.x - r, n.y - r, s, s); c.strokeStyle = fg; c.lineWidth = 1.2 / this.k; c.strokeRect(n.x - r, n.y - r, s, s); }
      else { c.fillStyle = on && dim ? fg : fg3; c.fillRect(n.x - r, n.y - r, s, s); }
      if (openN) { c.strokeStyle = fg; c.lineWidth = 2 / this.k; c.strokeRect(n.x - r - 3, n.y - r - 3, s + 6, s + 6); }
    }
    c.globalAlpha = 1;
    const fs = 11 / this.k;
    c.font = `500 ${fs}px ${css('--sans')}`; c.textBaseline = 'middle';
    for (const n of this.nodes) {
      if (n.kind !== 'seed') continue;
      const r = n.r;
      c.globalAlpha = hl && !hl.has(n.id) ? 0.35 : 1;
      c.fillStyle = fg; c.fillRect(n.x - r, n.y - r, r * 2, r * 2);
      const t = '@' + n.label, w = c.measureText(t).width;
      c.fillStyle = bg; c.fillRect(n.x + r + 3 / this.k, n.y - fs * 0.7, w + 6 / this.k, fs * 1.4);
      c.fillStyle = fg; c.fillText(t, n.x + r + 6 / this.k, n.y);
    }
    // Labels for multi-list leads when zoomed in, and for search matches.
    c.font = `${10 / this.k}px ${css('--sans')}`; c.fillStyle = fg2; c.globalAlpha = 1;
    for (const n of this.nodes) {
      if (n.kind === 'seed') continue;
      if (this.match.has(n.id) || (this.k > 1.8 && n.degree >= 2 && !dim)) c.fillText(n.handle || n.label, n.x + n.r + 3 / this.k, n.y);
    }
    c.restore();
  },
  at(px, py) {
    const x = (px - this.x) / this.k, y = (py - this.y) / this.k;
    let best = null, bd = (8 / this.k) ** 2;
    for (const n of this.nodes) {
      const d = (n.x - x) ** 2 + (n.y - y) ** 2 - n.r * n.r;
      if (d < bd) { bd = d; best = n; }
    }
    return best;
  },
  search() {
    const q = $('#map-q').value.trim().toLowerCase();
    this.match = new Set(q ? this.nodes.filter((n) => (n.label + ' ' + (n.handle || '') + ' ' + (n.name || '')).toLowerCase().includes(q)).map((n) => n.id) : []);
    this.draw();
  },
};
function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }
(function mapInput() {
  const c = $('#canvas');
  let drag = null, moved = false;
  c.addEventListener('pointerdown', (e) => {
    const n = M.at(e.offsetX, e.offsetY);
    drag = { x: e.clientX, y: e.clientY, ox: M.x, oy: M.y, n };
    moved = false; c.setPointerCapture(e.pointerId); c.classList.add('drag');
    if (n) { n.fx = n.x; n.fy = n.y; M.sim?.alphaTarget(0.1).restart(); }
  });
  c.addEventListener('pointermove', (e) => {
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
      if (drag.n) { drag.n.fx = drag.n.x + dx / M.k; drag.n.fy = drag.n.y + dy / M.k; drag.x = e.clientX; drag.y = e.clientY; }
      else { M.x = drag.ox + dx; M.y = drag.oy + dy; M.draw(); }
      return;
    }
    const n = M.at(e.offsetX, e.offsetY);
    if (n !== M.hover) { M.hover = n; M.draw(); }
    const h = $('#hover');
    if (!n) { h.hidden = true; c.style.cursor = ''; return; }
    c.style.cursor = 'pointer';
    h.innerHTML = n.kind === 'seed' ? `<b>@${esc(n.label)}</b><span>Seed, ${int(n.degree)} people</span>`
      : `<b>${esc(n.name || n.handle || n.label)}</b><span>@${esc(n.handle || n.label)}</span><span>In ${n.degree} list${n.degree === 1 ? '' : 's'}</span>`;
    h.hidden = false;
    const x = Math.min(e.offsetX + 14, M.w - h.offsetWidth - 8), y = Math.min(e.offsetY + 14, M.h - h.offsetHeight - 8);
    h.style.left = x + 'px'; h.style.top = y + 'px';
  });
  c.addEventListener('pointerup', () => {
    if (drag?.n) { drag.n.fx = null; drag.n.fy = null; M.sim?.alphaTarget(0); }
    if (drag && !moved && drag.n && drag.n.kind === 'lead') { openDetail(+drag.n.id.slice(2)).then(() => M.draw()); }
    drag = null; c.classList.remove('drag');
  });
  c.addEventListener('pointerleave', () => { if (M.hover) { M.hover = null; M.draw(); } $('#hover').hidden = true; });
  c.addEventListener('wheel', (e) => {
    e.preventDefault();
    const f = Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015));
    const k = Math.min(8, Math.max(0.05, M.k * f));
    M.x = e.offsetX - ((e.offsetX - M.x) * k) / M.k; M.y = e.offsetY - ((e.offsetY - M.y) * k) / M.k; M.k = k;
    M.draw();
  }, { passive: false });
})();
$('#map-fit').onclick = () => M.fit();
$('#map-q').addEventListener('input', debounce(() => M.search(), 120));
$('#map-q').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  const n = M.nodes.find((x) => M.match.has(x.id));
  if (!n) return;
  M.k = Math.max(M.k, 2); M.x = M.w / 2 - n.x * M.k; M.y = M.h / 2 - n.y * M.k; M.draw();
  if (n.kind === 'lead') openDetail(+n.id.slice(2));
});
$('#map-scope').addEventListener('click', (e) => {
  const b = e.target.closest('[data-v]');
  if (!b || b.dataset.v === M.scope) return;
  $$('#map-scope button').forEach((x) => x.classList.toggle('on', x === b));
  M.scope = b.dataset.v; M.rev = null; M.byId = new Map(); M.fitted = false; M.load();
});

// ---------- boot ----------
applyTheme(store.get('theme', document.documentElement.dataset.theme || 'dark'));
route();
renderFilters();
resetLeads();
loadTags(); loadCounts(); loadScraper();
setInterval(loadScraper, 3000);
setInterval(() => { loadCounts(); loadTags(); }, 30000);
setInterval(() => { if (S.view === 'leads' && !document.hidden && S.rows.length && $('#scroll').scrollTop < 5 && !S.open) resetLeads(true); }, 45000);
setInterval(() => { if (offlineSince) setOnline(false); if (S.view === 'scraper') renderScraper(); else renderStatus(); }, 1000);
