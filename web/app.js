'use strict';
/* Fortunate Leads workspace: scraping progress, people table, map. No qualification UI. */

const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (n) => (n == null ? '–' : n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M' : n >= 1e4 ? Math.round(n / 1e3) + 'k' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n));
const int = (n) => (n == null ? '–' : Math.round(n).toLocaleString('en-US'));
const ig = (h) => 'https://www.instagram.com/' + encodeURIComponent(h) + '/';
const initials = (name, handle) => {
  const src = (name || '').replace(/[^\p{L}\p{N} ]/gu, ' ').trim() || (handle || '').replace(/[^a-z0-9]/gi, ' ').trim();
  const w = src.split(/\s+/).filter(Boolean);
  return ((w[0] || '?')[0] + (w.length > 1 ? w[w.length - 1][0] : (w[0] || '')[1] || '')).toUpperCase();
};
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
};
const api = {
  async get(u) { const r = await fetch(u, { cache: 'no-store' }); if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); },
  async post(u, b) {
    const r = await fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b || {}) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) throw new Error(j.error || 'HTTP ' + r.status);
    return j;
  },
};
let toastT;
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.hidden = false; clearTimeout(toastT); toastT = setTimeout(() => (t.hidden = true), 2400); }

const STATUSES = [['good', 'Good'], ['maybe', 'Maybe'], ['no', 'No'], ['contacted', 'Contacted'], ['client', 'Client'], ['known', 'Known']];
const STATUS_LABEL = Object.fromEntries(STATUSES);
// Bio-derived tags from the qualifier; hidden while qualification is off.
const AUTO_SIGNAL = new Set(['Business', 'Email', 'Founder', 'Hiring', 'Link Hub', 'NL', 'Scaling', 'Shop Link', 'Shopify', 'UK', 'US', 'Verified']);
const LISTS_RX = /^in (\d+) lists$/;
const REL_RX = /^(knows you|you follow|follows you|follows @|followed by @)/;

const S = {
  view: 'leads',
  f: { q: '', via: '', lists: 0, status: '', tag: '', sort: 'connected', ...store.get('fl-filters', {}) },
  tags: [], manual: new Set(store.get('fl-manual-tags', [])),
  counts: null, sc: null,
};
S.f.q = '';

function isShownTag(t) { return t.grp === 'source' || t.source === 'manual'; }
function tagClass(tag, source) {
  if (source === 'manual' || S.manual.has(tag)) return 'man';
  if (LISTS_RX.test(tag)) return 'lists';
  if (REL_RX.test(tag)) return 'rel';
  return 'via';
}
const tagChip = (t, removable) => `<span class="tag ${tagClass(t.tag, t.source)}">${esc(t.tag)}${removable ? `<button data-rm="${esc(t.tag)}" aria-label="Remove">×</button>` : ''}</span>`;
function noteManual(rows) {
  let changed = false;
  rows.forEach((r) => (r.tags || []).forEach((t) => { if (t.source === 'manual' && !S.manual.has(t.tag)) { S.manual.add(t.tag); changed = true; } }));
  if (changed) { store.set('fl-manual-tags', [...S.manual]); renderTagSelects(); }
}
function manualTagList() {
  const counts = new Map(S.tags.map((t) => [t.tag, t.count]));
  const set = new Set(S.manual);
  S.tags.forEach((t) => { if (t.grp === 'signal' && !AUTO_SIGNAL.has(t.tag)) set.add(t.tag); });
  return [...set].map((tag) => ({ tag, count: counts.get(tag) || 0 })).filter((t) => t.count > 0).sort((a, b) => b.count - a.count);
}
const connections = (r) => (r.via || []).length;

function pfp(p, size) {
  const pic = p.pic ? `<img src="${esc(p.pic)}" alt="" loading="lazy" decoding="async" onerror="this.remove()">` : '';
  return `<div class="pfp${size ? ' ' + size : ''}">${esc(initials(p.name, p.handle))}${pic}</div>`;
}

/* ---------- Theme ---------- */
function theme() { return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'; }
function paintTheme() { document.querySelectorAll('#theme-seg button').forEach((b) => b.classList.toggle('on', b.dataset.theme === theme())); }
$('#theme-seg').onclick = (e) => {
  const b = e.target.closest('button'); if (!b) return;
  document.documentElement.dataset.theme = b.dataset.theme;
  try { localStorage.setItem('fl-theme', b.dataset.theme); } catch (err) {}
  paintTheme(); MapView.themeChanged();
};
paintTheme();

/* ---------- Routing ---------- */
function route() {
  const v = (location.hash.match(/^#\/(\w+)/) || [])[1];
  S.view = ['leads', 'map', 'scraper'].includes(v) ? v : 'leads';
  document.querySelectorAll('.view').forEach((el) => el.classList.toggle('on', el.id === 'view-' + S.view));
  document.querySelectorAll('.nav a').forEach((a) => a.classList.toggle('on', a.dataset.view === S.view));
  if (S.view === 'map') MapView.show(); else MapView.hide();
  if (S.view === 'leads') renderList();
  if (S.view === 'scraper') renderScraper();
}
window.addEventListener('hashchange', route);

/* ---------- Tags + counts ---------- */
async function loadTags() {
  try { S.tags = await api.get('/api/tags'); } catch (e) { return; }
  renderTagSelects();
  $('#tag-options').innerHTML = manualTagList().map((t) => `<option value="${esc(t.tag)}">`).join('');
}
function renderTagSelects() {
  const via = S.tags.filter((t) => t.grp === 'source' && /^via @/.test(t.tag)).sort((a, b) => b.count - a.count);
  const viaHTML = (cur) => `<option value="">All sources</option>` + via.map((t) => `<option value="${esc(t.tag)}"${t.tag === cur ? ' selected' : ''}>${esc(t.tag)} · ${int(t.count)}</option>`).join('');
  const man = manualTagList();
  const tagHTML = (cur) => `<option value="">All tags</option>` + man.map((t) => `<option value="${esc(t.tag)}"${t.tag === cur ? ' selected' : ''}>${esc(t.tag)} · ${int(t.count)}</option>`).join('')
    + (cur && !man.some((t) => t.tag === cur) ? `<option value="${esc(cur)}" selected>${esc(cur)}</option>` : '');
  $('#f-via').innerHTML = viaHTML(S.f.via);
  $('#f-tag').innerHTML = tagHTML(S.f.tag);
  $('#f-tag').hidden = !man.length && !S.f.tag;
  $('#m-via').innerHTML = viaHTML(MapView.filters.via);
  $('#m-tag').innerHTML = tagHTML(MapView.filters.tag);
  $('#m-tag').hidden = !man.length && !MapView.filters.tag;
}
async function loadCounts() {
  try { S.counts = await api.get('/api/counts'); } catch (e) { return; }
  $('#nav-leads').textContent = fmt(S.counts.total);
}

/* ---------- Leads: streamed rows, windowed rendering ---------- */
const ROW = 56, PAGE = 200;
const L = { gen: 0, rows: [], ids: new Set(), total: 0, off: 0, done: false, loading: false, sel: -1, error: false };

function leadParams(extraTag) {
  const p = new URLSearchParams();
  const tags = [S.f.via, S.f.tag, extraTag].filter(Boolean);
  if (tags.length) p.set('tags', tags.join(','));
  if (S.f.status) p.set('status', S.f.status);
  if (S.f.q) p.set('q', S.f.q);
  return p;
}
function sortRows(rows) {
  const by = S.f.sort;
  rows.sort((a, b) => (by === 'followers' ? (b.followers ?? -1) - (a.followers ?? -1) || connections(b) - connections(a)
    : by === 'newest' ? b.id - a.id
    : connections(b) - connections(a) || (b.followers ?? -1) - (a.followers ?? -1)) || b.id - a.id);
  return rows;
}
// Everyone in >= min lists, via the server's "in N lists" tags (AND with the other filters).
async function gatherMulti(min, gen) {
  const ks = S.tags.map((t) => +(t.tag.match(LISTS_RX) || [])[1]).filter((k) => k >= min);
  const out = new Map();
  const one = async (k) => {
    for (let off = 0; ;) {
      const p = leadParams(`in ${k} lists`); p.set('sort', 'followers'); p.set('offset', off); p.set('limit', 500);
      const d = await api.get('/api/leads?' + p);
      if (gen !== L.gen) return;
      d.rows.forEach((r) => out.set(r.id, r));
      off += d.rows.length;
      if (!d.rows.length || off >= d.total) break;
    }
  };
  // Two lanes: a handful of small requests, never a burst.
  const q = [...ks];
  await Promise.all([0, 1].map(async () => { while (q.length && gen === L.gen) await one(q.shift()); }));
  return [...out.values()];
}
async function resetLeads(keepScroll) {
  const gen = ++L.gen;
  L.stale = keepScroll && L.rows.length ? L.rows : null;
  const min = S.f.lists >= 2 ? S.f.lists : S.f.sort === 'connected' ? 2 : 0;
  // gathering blocks the scroll-triggered loadMore until the multi-list head is in place.
  Object.assign(L, { rows: [], ids: new Set(), off: 0, done: false, loading: false, error: false, gathering: !!min });
  if (!keepScroll) { $('#list-scroll').scrollTop = 0; L.sel = -1; }
  syncFilterUI();
  renderList();
  try {
    if (!S.tags.length) await loadTags();
    if (min) {
      const head = sortRows(await gatherMulti(min, gen));
      if (gen === L.gen) L.gathering = false;
      if (gen !== L.gen) return;
      L.rows = head; head.forEach((r) => L.ids.add(r.id));
      noteManual(head);
      if (S.f.lists >= 2) { L.total = head.length; L.done = true; }
    }
    if (!L.done) await loadMore(gen);
  } catch (e) { if (gen === L.gen) { L.error = true; L.done = true; L.gathering = false; } }
  if (gen === L.gen) renderList();
}
async function loadMore(gen = L.gen) {
  if (L.loading || L.done || L.gathering) return;
  L.loading = true;
  try {
    const p = leadParams();
    p.set('sort', { connected: 'score', newest: 'recent', followers: 'followers' }[S.f.sort] || 'score');
    p.set('offset', L.off); p.set('limit', PAGE);
    const d = await api.get('/api/leads?' + p);
    if (gen !== L.gen) return;
    L.total = d.total; L.off += d.rows.length;
    for (const r of d.rows) if (!L.ids.has(r.id)) { L.ids.add(r.id); L.rows.push(r); }
    noteManual(d.rows);
    if (!d.rows.length || L.off >= d.total) L.done = true;
  } catch (e) { if (gen === L.gen) { L.error = true; L.done = true; } }
  finally { if (gen === L.gen) L.loading = false; }
  if (gen === L.gen) renderList();
}

function rowHTML(r, i) {
  const tags = (r.tags || []).filter(isShownTag)
    .sort((a, b) => (tagClass(b.tag, b.source) === 'man') - (tagClass(a.tag, a.source) === 'man') || (LISTS_RX.test(b.tag) - LISTS_RX.test(a.tag)));
  const n = connections(r);
  return `<div class="row${i === L.sel ? ' sel' : ''}${r.status === 'no' && S.f.status !== 'no' ? ' muted-row' : ''}" data-i="${i}" style="top:${i * ROW}px">
    ${pfp(r)}
    <div class="who"><div class="nm">${esc(r.name || r.handle)}</div><a class="hd" href="${ig(r.handle)}" target="_blank" rel="noopener">@${esc(r.handle)}</a></div>
    <div class="bio${r.bio ? '' : ' none'}">${r.bio ? esc(r.bio) : ''}</div>
    <div class="r num">${fmt(r.followers)}</div>
    <div class="r num">${n || '–'}</div>
    <div class="tags">${tags.map((t) => tagChip(t)).join('')}</div>
    <div>${r.status ? `<span class="mark ${esc(r.status)}">${esc(STATUS_LABEL[r.status] || r.status)}</span>` : ''}</div>
  </div>`;
}
let lastWin = '';
function renderList(force) {
  if (S.view !== 'leads') return;
  const sc = $('#list-scroll'), box = $('#list');
  if (L.stale && (L.rows.length || L.done)) L.stale = null;
  const rows = L.stale || L.rows;
  const extra = L.done || L.stale ? 0 : 3;
  const n = rows.length;
  box.style.height = (n + extra) * ROW + 'px';
  const h = sc.clientHeight || 800;
  const a = Math.max(0, Math.floor(sc.scrollTop / ROW) - 6), b = Math.min(n + extra, Math.ceil((sc.scrollTop + h) / ROW) + 6);
  const key = [L.gen, a, b, n, L.sel, L.done, L.error, !!L.stale].join(':');
  if (key !== lastWin || force) {
    lastWin = key;
    let html = '';
    for (let i = a; i < b; i++) html += i < n ? rowHTML(rows[i], i) : `<div class="row ph" style="top:${i * ROW}px"><div class="pfp"></div><div><i></i></div><div><i></i></div></div>`;
    if (!n && L.done) html = `<div class="empty">${L.error ? 'Could not load people. Server offline?' : 'No people match these filters.'}</div>`;
    box.innerHTML = html;
  }
  $('#list-total').textContent = L.done || L.total ? `· ${int(Math.max(L.total, n))}` : '';
  if (!L.stale && !L.gathering && !L.done && !L.loading && b >= n - 20) loadMore();
}
$('#list-scroll').addEventListener('scroll', () => renderList(), { passive: true });
new ResizeObserver(() => renderList()).observe($('#list-scroll'));

$('#list').addEventListener('click', (e) => {
  if (e.target.closest('a')) return;
  const row = e.target.closest('.row[data-i]'); if (!row) return;
  select(+row.dataset.i, false);
  openDrawer(L.rows[L.sel].id);
});
function select(i, scroll = true) {
  if (!L.rows.length) return;
  L.sel = Math.max(0, Math.min(L.rows.length - 1, i));
  if (scroll) {
    const sc = $('#list-scroll'), top = L.sel * ROW;
    if (top < sc.scrollTop) sc.scrollTop = top;
    else if (top + ROW > sc.scrollTop + sc.clientHeight) sc.scrollTop = top + ROW - sc.clientHeight;
  }
  renderList(true);
}
function patchRow(id, fields) {
  const r = L.rows.find((x) => x.id === id);
  if (r) { Object.assign(r, fields); renderList(true); }
}

/* filters */
function saveFilters() { const { q, ...rest } = S.f; store.set('fl-filters', rest); }
function syncFilterUI() {
  $('#f-via').value = S.f.via; $('#f-lists').value = String(S.f.lists); $('#f-status').value = S.f.status;
  $('#f-tag').value = S.f.tag; $('#sort').value = S.f.sort;
  [['#f-via', S.f.via], ['#f-lists', S.f.lists], ['#f-status', S.f.status], ['#f-tag', S.f.tag]].forEach(([s, v]) => $(s).classList.toggle('set', !!+v || (!!v && v !== '0')));
  $('#f-clear').hidden = !(S.f.via || S.f.lists || S.f.status || S.f.tag || S.f.q);
}
const onFilter = (key, parse = (v) => v) => (e) => { S.f[key] = parse(e.target.value); saveFilters(); resetLeads(); };
$('#f-via').onchange = onFilter('via');
$('#f-lists').onchange = onFilter('lists', Number);
$('#f-status').onchange = onFilter('status');
$('#f-tag').onchange = onFilter('tag');
$('#sort').onchange = onFilter('sort');
$('#f-clear').onclick = () => { Object.assign(S.f, { via: '', lists: 0, status: '', tag: '', q: '' }); $('#q').value = ''; saveFilters(); resetLeads(); };
let qT;
$('#q').addEventListener('input', (e) => { clearTimeout(qT); qT = setTimeout(() => { S.f.q = e.target.value.trim(); resetLeads(); }, 220); });

async function mark(id, status) {
  const r = L.rows.find((x) => x.id === id);
  const prev = r ? r.status : null;
  const next = prev === status ? null : status;
  patchRow(id, { status: next });
  try { await api.post(`/api/person/${id}/mark`, { status: next }); } catch (e) { patchRow(id, { status: prev }); toast('Could not save'); return; }
  if (D.id === id) { D.p.status = next; renderDrawer(); }
  loadCounts();
}

/* keyboard */
document.addEventListener('keydown', (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
  if (e.key === 'Escape') { if (typing) document.activeElement.blur(); else closeDrawer(); return; }
  if (typing) return;
  if (e.key === '/') { e.preventDefault(); location.hash = '#/leads'; $('#q').focus(); return; }
  if (S.view !== 'leads') return;
  const cur = L.rows[L.sel];
  const keys = { j: () => { select(L.sel + 1); if (!$('#drawer').hidden) openDrawer(L.rows[L.sel].id); },
    k: () => { select(L.sel - 1); if (!$('#drawer').hidden) openDrawer(L.rows[L.sel].id); },
    Enter: () => cur && openDrawer(cur.id), o: () => cur && window.open(ig(cur.handle), '_blank', 'noopener'),
    g: () => cur && mark(cur.id, 'good'), m: () => cur && mark(cur.id, 'maybe'), x: () => cur && mark(cur.id, 'no'), c: () => cur && mark(cur.id, 'contacted') };
  if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
});

/* ---------- Drawer ---------- */
const D = { id: null, p: null, seq: 0 };
async function openDrawer(id) {
  const seq = ++D.seq;
  D.id = id;
  const row = L.rows.find((r) => r.id === id);
  if (row && (!D.p || D.p.id !== id)) { D.p = { ...row, edges: null }; renderDrawer(); }
  $('#drawer').hidden = false;
  try {
    const p = await api.get('/api/person/' + id);
    if (seq !== D.seq) return;
    D.p = p; noteManual([p]); renderDrawer();
  } catch (e) { if (!row) toast('Could not load person'); }
}
function closeDrawer() { $('#drawer').hidden = true; D.id = null; D.p = null; }
function renderDrawer() {
  const p = D.p; if (!p) return;
  const tags = (p.tags || []).filter(isShownTag);
  const manual = tags.filter((t) => t.source === 'manual');
  const auto = tags.filter((t) => t.source !== 'manual');
  const edges = p.edges || (p.via || []).map((s) => ({ seed: s, direction: null }));
  const site = p.website ? p.website.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, '') : '';
  $('#drawer-body').innerHTML = `
    <div class="dw-top"><span class="num">#${p.id}</span><button class="btn sm" id="dw-close">Close</button></div>
    ${pfp(p, 'dw-pfp')}
    <div class="dw-name">${esc(p.name || p.handle)}</div>
    <a class="dw-handle hd" href="${ig(p.handle)}" target="_blank" rel="noopener">@${esc(p.handle)} ↗</a>
    <div class="dw-stats num">
      <div><b>${fmt(p.followers)}</b><span>Followers</span></div>
      <div><b>${fmt(p.following)}</b><span>Following</span></div>
      <div><b>${fmt(p.posts)}</b><span>Posts</span></div>
      <div><b>${connections(p) || '–'}</b><span>Lists</span></div>
    </div>
    ${p.bio ? `<div class="dw-bio">${esc(p.bio)}</div>` : `<div class="muted">No bio yet</div>`}
    ${site ? `<a class="dw-site" href="${esc(p.website)}" target="_blank" rel="noopener">${esc(site)}</a>` : ''}
    ${!p.bio ? `<div style="margin-top:8px"><button class="btn sm" id="dw-read">Read profile</button></div>` : ''}
    <div class="dw-sec"><h4>Status</h4><div class="marks">${STATUSES.map(([k, l]) => `<button data-mark="${k}" class="${p.status === k ? 'on' : ''}">${l}</button>`).join('')}</div></div>
    <div class="dw-sec"><h4>Note</h4><textarea id="dw-note" rows="3">${esc(p.note || '')}</textarea></div>
    <div class="dw-sec"><h4>Lists</h4><div class="dw-edges">${edges.map((e) => `<div><a class="hd" href="${ig(e.seed)}" target="_blank" rel="noopener">@${esc(e.seed)}</a><span>${e.direction === 'followers' ? 'follows them' : e.direction === 'following' ? 'followed by them' : ''}</span></div>`).join('') || '<div><span>None</span></div>'}</div></div>
    <div class="dw-sec"><h4>Tags</h4>
      <div class="dw-tags" id="dw-tags">${manual.map((t) => tagChip(t, true)).join('')}${auto.map((t) => tagChip(t)).join('')}</div>
      <form class="tag-add" id="dw-tag-form"><input id="dw-tag-in" list="tag-options" placeholder="Add tag" autocomplete="off"><button class="btn sm" type="submit">Add</button></form>
    </div>`;
}
$('#drawer').addEventListener('click', async (e) => {
  if (e.target.closest('#dw-close')) return closeDrawer();
  const m = e.target.closest('[data-mark]');
  if (m && D.p) {
    const id = D.p.id, next = D.p.status === m.dataset.mark ? null : m.dataset.mark;
    D.p.status = next; renderDrawer(); patchRow(id, { status: next });
    try { await api.post(`/api/person/${id}/mark`, { status: next }); loadCounts(); } catch (err) { toast('Could not save'); }
    return;
  }
  const rm = e.target.closest('[data-rm]');
  if (rm && D.p) return editTags(D.p.id, [], [rm.dataset.rm]);
  if (e.target.closest('#dw-read') && D.p) {
    try { await api.post(`/api/person/${D.p.id}/read`); toast('Queued'); } catch (err) { toast('Could not queue'); }
  }
});
$('#drawer').addEventListener('submit', (e) => {
  e.preventDefault();
  const v = $('#dw-tag-in').value.trim();
  if (v && D.p) editTags(D.p.id, [v], []);
});
$('#drawer').addEventListener('change', async (e) => {
  if (e.target.id !== 'dw-note' || !D.p) return;
  try { await api.post(`/api/person/${D.p.id}/mark`, { status: D.p.status || null, note: e.target.value }); D.p.note = e.target.value; } catch (err) { toast('Could not save note'); }
});
async function editTags(id, add, remove) {
  try { await api.post(`/api/person/${id}/tags`, { add, remove }); } catch (e) { toast('Could not save tag'); return; }
  add.forEach((t) => S.manual.add(t)); store.set('fl-manual-tags', [...S.manual]);
  const p = await api.get('/api/person/' + id).catch(() => null);
  if (p && D.id === id) { D.p = p; renderDrawer(); $('#dw-tag-in') && $('#dw-tag-in').focus(); }
  if (p) patchRow(id, { tags: p.tags });
  loadTags();
}

/* ---------- Scraper state, speed, ETA ---------- */
const PAGE_SIZE = { followers: 25, following: 50 };
const SPEED_WINDOW = 15 * 60e3;
const SPEED_KEY = /[?&]mock=1\b/.test(location.search) ? 'fl-speed-mock' : 'fl-speed';
const SP = { textAt: 0, samples: store.get(SPEED_KEY, []).filter((s) => Date.now() - s.t < 30 * 60e3), prev: null, active: null, activeAt: 0, idleSince: 0 };
const keyOf = (l) => l.seed + '|' + l.direction;
const hhmm = (d) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
function ago(iso) {
  const s = (Date.now() - new Date(iso)) / 1000;
  if (!isFinite(s)) return '';
  return s < 60 ? Math.max(0, Math.round(s)) + 's ago' : s < 3600 ? Math.round(s / 60) + 'm ago' : s < 86400 ? Math.round(s / 3600) + 'h ago' : Math.round(s / 86400) + 'd ago';
}
function dur(h) {
  if (h == null || !isFinite(h)) return '–';
  const m = Math.round(h * 60);
  if (m < 1) return '<1m';
  if (m < 60) return m + 'm';
  if (m < 48 * 60) return Math.floor(m / 60) + 'h ' + String(m % 60).padStart(2, '0') + 'm';
  return Math.round(m / 1440) + 'd';
}
const pagesLeft = (l) => (l.total == null ? null : Math.ceil(Math.max(0, l.total - (l.received || 0)) / (PAGE_SIZE[l.direction] || 25)));
const isDone = (l) => l.state === 'done' || l.state === 'private';

function sample(sc) {
  const now = Date.now();
  const recv = sc.lists.reduce((a, l) => a + (l.received || 0), 0);
  SP.samples.push({ t: now, recv, pages: sc.ext && sc.ext.today ? sc.ext.today.list || 0 : null, pt: sc.people_today || 0 });
  SP.samples = SP.samples.filter((s) => now - s.t < 30 * 60e3);
  if (SP.samples.length > 600) SP.samples = SP.samples.slice(-600);
  store.set(SPEED_KEY, SP.samples);
  // The list whose received count moved is the one being scraped.
  const cur = new Map(sc.lists.map((l) => [keyOf(l), l.received || 0]));
  if (SP.prev) for (const [k, v] of cur) if (v > (SP.prev.get(k) ?? v)) { SP.active = k; SP.activeAt = now; }
  SP.prev = cur;
}
function rates() {
  const now = Date.now();
  const w = SP.samples.filter((s) => now - s.t <= SPEED_WINDOW);
  if (w.length < 2) return null;
  const min = (w[w.length - 1].t - w[0].t) / 60e3;
  if (min < 1) return null;
  let recv = 0, pages = 0, pt = 0;
  for (let i = 1; i < w.length; i++) {
    recv += Math.max(0, w[i].recv - w[i - 1].recv);
    if (w[i].pages != null && w[i - 1].pages != null) pages += Math.max(0, w[i].pages - w[i - 1].pages);
    pt += Math.max(0, w[i].pt - w[i - 1].pt);
  }
  return { ppm: recv / min, pph: (pages / min) * 60, newpm: pt / min, span: min };
}
function activeList(sc) {
  const byKey = new Map(sc.lists.map((l) => [keyOf(l), l]));
  const m = String((sc.ext && sc.ext.activity) || '').match(/^@([\w.]+)\s+(followers|following)/);
  if (m && byKey.has(m[1].toLowerCase() + '|' + m[2])) return byKey.get(m[1].toLowerCase() + '|' + m[2]);
  if (SP.active && byKey.has(SP.active) && !isDone(byKey.get(SP.active))) return byKey.get(SP.active);
  const run = sc.lists.find((l) => l.state === 'running');
  if (run) return run;
  // Most recently touched list that has progress and is not finished.
  return sc.lists.filter((l) => !isDone(l) && l.state !== 'paused' && l.state !== 'error' && (l.received || 0) > 0)
    .sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))[0] || null;
}
function lastPageAt(sc) {
  const t = Math.max(SP.activeAt, ...sc.lists.filter((l) => (l.received || 0) > 0).map((l) => +new Date(l.updated_at) || 0));
  return t || 0;
}
// Status text from the extension (v3.1+): "Scraping", "Next request in 8s", "Cooldown until 16:40", "Paused", ...
function stateFromText(sc) {
  const e = sc.ext || {}, t = String(e.text || '').trim(), now = Date.now();
  if (!t) return null;
  let m;
  if (/^Scraping/i.test(t)) return { key: 'run', label: 'Scraping', sub: '' };
  if ((m = t.match(/^Next request in (\d+)\s*s/i))) {
    const left = Math.max(0, Math.round((SP.textAt + +m[1] * 1000 - now) / 1000));
    return { key: 'wait', label: left ? `Waiting ${left}s` : 'Scraping', sub: '' };
  }
  if ((m = t.match(/^Next request in (\d+)\s*m/i))) return { key: 'wait', label: `Waiting ${m[1]}m`, sub: '' };
  if (/^Cooldown/i.test(t)) {
    const cd = e.cooldown_until ? new Date(e.cooldown_until) : null;
    return { key: 'cool', label: t, sub: cd && cd > now ? dur((cd - now) / 3600e3) + ' left' : '' };
  }
  if (/^Needs attention:?/i.test(t)) return { key: 'off', label: 'Needs attention', sub: t.replace(/^Needs attention:?\s*/i, '') };
  if (/^Paused/i.test(t)) return { key: 'stop', label: 'Paused', sub: /workspace/i.test(t) ? 'from workspace' : '' };
  if (/Open Instagram/i.test(t)) return { key: 'off', label: 'Open Instagram', sub: '' };
  if (/Server offline/i.test(t)) return { key: 'off', label: 'Server offline', sub: '' };
  if (/^Idle/i.test(t)) return { key: 'stop', label: 'Idle', sub: /queue empty/i.test(t) ? 'queue empty' : '' };
  return { key: e.state === 'running' ? 'run' : 'stop', label: t, sub: '' };
}
function scraperState(sc) {
  const e = sc.ext || {};
  const now = Date.now();
  const cd = e.cooldown_until ? new Date(e.cooldown_until) : null;
  const budgetHit = e.budget && e.today && e.today.list >= e.budget.list;
  if (!e.online) return { key: 'off', label: 'Extension offline', sub: e.last_seen ? 'last seen ' + ago(e.last_seen) : 'not connected' };
  if (sc.paused && !/^Paused|^Needs attention/i.test(e.text || '')) return { key: 'stop', label: 'Paused', sub: 'from workspace' };
  const fromText = stateFromText(sc);
  if (fromText) return fromText;
  // Fallback for extensions older than v3.1 (no status text in the heartbeat).

  if (sc.paused || e.state === 'paused') return { key: 'stop', label: 'Paused', sub: e.last_error && /log in|security|attention/i.test(e.last_error) ? e.last_error : '' };
  if (cd && cd > now) return { key: 'cool', label: 'Cooldown until ' + hhmm(cd), sub: dur((cd - now) / 3600e3) + ' left' };
  // The lists table updates on every page; the heartbeat state can lag by up to 30 s.
  if (e.state === 'running' || sc.lists.some((l) => l.state === 'running')) {
    const since = (now - lastPageAt(sc)) / 1000;
    return since > 25 && isFinite(since) ? { key: 'wait', label: 'Waiting', sub: `last page ${ago(new Date(lastPageAt(sc)))}` } : { key: 'run', label: 'Scraping', sub: '' };
  }
  if (budgetHit) return { key: 'stop', label: 'Daily budget reached', sub: '' };
  const queued = (sc.queue && (sc.queue.list + sc.queue.profile)) || 0;
  if (!queued) return { key: 'stop', label: 'Idle', sub: 'queue empty' };
  // Idle with work queued for a while: the extension has no usable Instagram tab.
  if (SP.idleSince && now - SP.idleSince > 90e3) return { key: 'off', label: 'Open Instagram', sub: 'queue waiting' };
  return { key: 'wait', label: 'Waiting', sub: '' };
}
function activityText(sc, st) {
  if (sc.ext && sc.ext.activity) return esc(sc.ext.activity).replace(/^(@[\w.]+)/, '<b>$1</b>');
  const l = activeList(sc);
  if (!l) return sc.queue && sc.queue.profile && sc.ext && sc.ext.state === 'running' ? '<b>Reading profiles</b>' : '';
  const page = Math.floor((l.received || 0) / (PAGE_SIZE[l.direction] || 25)) + 1;
  const pre = st.key === 'run' || st.key === 'wait' ? '' : 'next · ';
  return `${pre}<b>@${esc(l.seed)}</b> ${esc(l.direction)} · page ${int(page)}${l.total ? ` of ${int(Math.ceil(l.total / (PAGE_SIZE[l.direction] || 25)))}` : ''}`;
}
function totals(sc) {
  const lists = sc.lists;
  const open = lists.filter((l) => !isDone(l));
  const known = open.filter((l) => l.total != null);
  const rem = known.reduce((a, l) => a + pagesLeft(l), 0);
  return { done: lists.length - open.length, all: lists.length, people: lists.reduce((a, l) => a + (l.received || 0), 0), rem, unknown: open.length - known.length };
}

async function loadScraper() {
  let sc;
  try { sc = await api.get('/api/scraper'); } catch (e) { sc = null; }
  if (!sc) { paintStatus(null); return; }
  if (!S.sc || (S.sc.ext || {}).text !== (sc.ext || {}).text) SP.textAt = Date.now();
  S.sc = sc;
  const e = sc.ext || {};
  if (e.online && e.state === 'idle' && !sc.lists.some((l) => l.state === 'running') && !(e.cooldown_until && new Date(e.cooldown_until) > Date.now())) SP.idleSince = SP.idleSince || Date.now();
  else SP.idleSince = 0;
  sample(sc);
  paintStatus(sc);
  if (S.view === 'scraper') renderScraper();
}
function paintStatus(sc) {
  if (!sc) {
    $('#st-sq').className = 'sq off'; $('#st-label').textContent = 'Server offline'; $('#st-activity').textContent = '';
    return;
  }
  const st = scraperState(sc), r = rates(), t = totals(sc);
  $('#st-sq').className = 'sq ' + st.key;
  $('#st-label').textContent = st.label;
  const act = activityText(sc, st);
  $('#st-activity').innerHTML = [act, st.sub ? esc(st.sub) : ''].filter(Boolean).join(' · ');
  $('#st-ppm').textContent = r ? (r.ppm >= 10 ? int(r.ppm) : r.ppm.toFixed(1)) : '–';
  $('#st-pph').textContent = r ? int(r.pph) : '–';
  $('#st-today').textContent = int(sc.people_today);
  $('#st-eta').textContent = r && r.pph > 0 ? dur(t.rem / r.pph) : '–';
  $('#st-pause').textContent = sc.paused ? 'Resume' : 'Pause';
  $('#nav-scraper').textContent = t.all - t.done ? String(t.all - t.done) : '';
  document.title = (st.key === 'run' ? 'Scraping · ' : '') + 'Fortunate Leads';
}
$('#st-pause').onclick = async () => {
  if (!S.sc) return;
  try { await api.post('/api/scraper/pause', { paused: !S.sc.paused }); } catch (e) { toast('Could not reach server'); }
  loadScraper();
};

let listFilter = store.get('fl-list-filter', 'open');
function stateCell(l, isActive, st) {
  if (isActive && (st.key === 'run' || st.key === 'wait')) return `<span class="stt"><i class="sq ${st.key}"></i>${st.key === 'run' ? 'Scraping' : 'Waiting'}</span>`;
  const m = { done: 'Done', private: 'Private', paused: 'Paused', error: 'Error', running: 'Running', queued: (l.received || 0) > 0 ? 'Partial' : 'Queued' };
  const cls = { done: 'stop', private: 'off', paused: 'stop', error: 'off', running: 'run', queued: (l.received || 0) > 0 ? 'wait' : 'stop' };
  return `<span class="stt"><i class="sq ${cls[l.state] || 'stop'}"></i>${m[l.state] || esc(l.state)}</span>`;
}
function renderScraper() {
  const sc = S.sc;
  if (!sc) { $('#kpis').innerHTML = `<div class="kpi"><span>Server</span><b>Offline</b></div>`; return; }
  const st = scraperState(sc), r = rates(), t = totals(sc), e = sc.ext || {};
  const act = activeList(sc);
  const eta = r && r.pph > 0 ? t.rem / r.pph : null;
  $('#kpis').innerHTML = [
    ['State', st.label, st.sub || (act ? '@' + act.seed + ' ' + act.direction : '')],
    ['Lists', `${int(t.done)} / ${int(t.all)}`, `${int(t.all - t.done)} open`],
    ['People collected', int(t.people), `${int(sc.people_today)} new today`],
    ['Pages left', int(t.rem), t.unknown ? `+ ${t.unknown} lists without total` : 'known totals'],
    ['Queue ETA', dur(eta), eta != null ? 'done ~' + hhmm(new Date(Date.now() + eta * 3600e3)) + (eta > 24 ? ' +' + Math.floor(eta / 24) + 'd' : '') : 'needs speed data'],
    ['Speed', r ? `${r.ppm >= 10 ? int(r.ppm) : r.ppm.toFixed(1)}/min` : '–', r ? `${int(r.pph)} pages/h · last ${Math.round(r.span)}m` : 'measuring'],
  ].map(([k, v, s], i) => `<div class="kpi"><span>${k}</span><b class="num"${i ? '' : ' id="kpi-state"'}>${esc(v)}</b><small class="num">${esc(s)}</small></div>`).join('');

  const budget = e.budget || {}, today = e.today || {};
  $('#ext-panel').innerHTML = `<div class="panel-head"><h3>Extension</h3><span class="stt"><i class="sq ${e.online ? 'run' : 'off'}" style="animation:none"></i>${e.online ? 'Online' : 'Offline'}</span></div>
    <dl class="kv num">
      <dt>List pages today</dt><dd>${int(today.list || 0)} / ${int(budget.list)}</dd>
      <dt>Profile reads today</dt><dd>${int(today.profile || 0)} / ${int(budget.profile)}</dd>
      <dt>Queue</dt><dd>${int(sc.queue.list)} lists · ${int(sc.queue.profile)} profiles</dd>
      <dt>Cooldown</dt><dd>${e.cooldown_until && new Date(e.cooldown_until) > Date.now() ? 'until ' + hhmm(new Date(e.cooldown_until)) : 'none'}</dd>
      <dt>Last seen</dt><dd>${e.last_seen ? ago(e.last_seen) : '–'}${e.version ? ' · v' + esc(e.version) : ''}</dd>
      ${e.last_error ? `<dd class="err">${esc(e.last_error)}</dd>` : ''}
    </dl>
    <div class="seed-row"><div class="grow"></div><button class="btn" id="ext-pause">${sc.paused ? 'Resume' : 'Pause'}</button></div>`;

  const actKey = act ? keyOf(act) : null;
  const rank = (l) => (keyOf(l) === actKey ? 0 : l.state === 'running' ? 1 : isDone(l) ? 6 : l.state === 'error' ? 4 : l.state === 'paused' ? 5 : (l.received || 0) > 0 ? 2 : 3);
  const groups = { open: (l) => !isDone(l), done: isDone, issues: (l) => ['error', 'private', 'paused'].includes(l.state), all: () => true };
  const counts = Object.fromEntries(Object.entries(groups).map(([k, f]) => [k, sc.lists.filter(f).length]));
  $('#list-filter').innerHTML = [['open', 'Open'], ['done', 'Done'], ['issues', 'Issues'], ['all', 'All']]
    .map(([k, l]) => `<button data-lf="${k}" class="${listFilter === k ? 'on' : ''}">${l}<b class="num">${counts[k]}</b></button>`).join('');
  const rows = sc.lists.filter(groups[listFilter] || groups.all).sort((a, b) => rank(a) - rank(b) || (b.received || 0) - (a.received || 0) || a.seed.localeCompare(b.seed));
  $('#lists-body').innerHTML = rows.map((l) => {
    const pct = l.total ? Math.min(100, ((l.received || 0) / l.total) * 100) : isDone(l) ? 100 : null;
    const left = isDone(l) ? 0 : pagesLeft(l);
    const isAct = keyOf(l) === actKey;
    return `<tr class="${isAct ? 'active' : ''}${isDone(l) ? ' done' : ''}">
      <td><a class="hd" href="${ig(l.seed)}" target="_blank" rel="noopener">@${esc(l.seed)}</a></td>
      <td>${esc(l.direction)}</td>
      <td><div class="prog"><div class="bar${pct == null ? ' unknown' : ''}"><i style="width:${pct == null ? ((l.received || 0) > 0 ? 4 : 0) : pct}%"></i></div><span>${int(l.received || 0)} / ${l.total == null ? '?' : int(l.total)}</span></div></td>
      <td class="r">${pct == null ? '–' : Math.floor(pct) + '%'}</td>
      <td class="r">${left == null ? '–' : int(left)}</td>
      <td class="r">${isDone(l) ? '–' : left != null && r && r.pph > 0 ? dur(left / r.pph) : '–'}</td>
      <td>${stateCell(l, isAct, st)}${l.error ? ` <span class="muted">${esc(l.error)}</span>` : ''}</td>
      <td class="r muted">${l.updated_at ? ago(l.updated_at) : ''}</td>
    </tr>`;
  }).join('') || `<tr><td colspan="8" class="muted">No lists</td></tr>`;
}
$('#list-filter').onclick = (e) => { const b = e.target.closest('button'); if (!b) return; listFilter = b.dataset.lf; store.set('fl-list-filter', listFilter); renderScraper(); };
$('#ext-panel').addEventListener('click', (e) => { if (e.target.closest('#ext-pause')) $('#st-pause').click(); });

/* bulk add */
function parseHandles(txt) {
  const out = [];
  for (let s of txt.split(/[\s,;]+/)) {
    s = s.trim(); if (!s) continue;
    const m = s.match(/instagram\.com\/([A-Za-z0-9._]+)/i);
    const h = (m ? m[1] : s.replace(/^@/, '')).replace(/[/?#].*$/, '').toLowerCase();
    if (/^[a-z0-9._]{1,30}$/.test(h) && !['p', 'reel', 'reels', 'stories', 'explore'].includes(h) && !out.includes(h)) out.push(h);
  }
  return out;
}
const dirs = () => [...document.querySelectorAll('#dir-seg button.on')].map((b) => b.dataset.dir);
function syncSeedBtn() {
  const n = parseHandles($('#seed-input').value).length;
  $('#seed-count').textContent = n ? `${n} account${n === 1 ? '' : 's'}${n > 50 ? ' · max 50' : ''}` : '';
  $('#seed-add').disabled = !n || n > 50 || !dirs().length;
}
$('#seed-input').addEventListener('input', () => { syncSeedBtn(); $('#seed-msg').textContent = ''; });
$('#dir-seg').onclick = (e) => { const b = e.target.closest('button'); if (!b) return; b.classList.toggle('on'); syncSeedBtn(); };
$('#seed-add').onclick = async () => {
  const handles = parseHandles($('#seed-input').value);
  if (!handles.length || handles.length > 50) return;
  $('#seed-add').disabled = true;
  try {
    const r = await api.post('/api/scraper/seeds', { handles, directions: dirs() });
    $('#seed-input').value = '';
    $('#seed-msg').textContent = `Queued ${r.queued ?? handles.length * dirs().length} lists`;
    listFilter = 'open'; loadScraper();
  } catch (e) { toast('Could not queue accounts'); }
  syncSeedBtn();
};

/* ---------- Map ---------- */
const MapView = (() => {
  const stage = $('#map-stage'), canvas = $('#map-canvas'), ctx = canvas.getContext('2d'), hc = $('#hovercard');
  const filters = { via: '', tag: '', lists: 0, ...store.get('fl-map-filters', {}) };
  let scope = store.get('fl-map-scope', 'leads');
  let W = 0, H = 0, DPR = 1, T = { x: 0, y: 0, k: 1 };
  let nodes = [], links = [], byId = new Map();
  let sim = null, rev = null, visible = false, dirty = true, colors = null, hover = null, focus = null;
  let lastLoad = 0, lastAdded = 0;
  const imgs = new Map(), imgQueue = []; let loadingImgs = 0;

  const radius = (n) => (n.kind === 'seed' ? (n.degree ? Math.min(32, 12 + Math.sqrt(n.degree) * 0.7) : 7) : Math.min(15, 3.5 + (n.degree || 1) * 2.2));

  function readColors() {
    const cs = getComputedStyle(document.documentElement), g = (v) => cs.getPropertyValue(v).trim();
    colors = { bg: g('--bg'), text: g('--text'), text2: g('--text-2'), text3: g('--text-3'), surface: g('--surface'), surface3: g('--surface-3'), border: g('--border-2'), link: g('--link') };
  }
  function resize() {
    const r = stage.getBoundingClientRect();
    DPR = window.devicePixelRatio || 1;
    const nw = Math.max(1, r.width), nh = Math.max(1, r.height);
    if (W && H) { T.x += (nw - W) / 2; T.y += (nh - H) / 2; }
    W = nw; H = nh; canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR); dirty = true;
  }
  new ResizeObserver(() => { if (visible) resize(); }).observe(stage);

  function matches(n) {
    if (n.kind === 'seed') return true;
    if (filters.lists && (n.degree || 0) < filters.lists) return false;
    if (filters.via && !(n.tags || []).includes(filters.via)) return false;
    if (filters.tag && !(n.tags || []).includes(filters.tag)) return false;
    return true;
  }
  function refilter() {
    nodes.forEach((n) => (n._on = matches(n)));
    const q = $('#map-q').value.trim().toLowerCase().replace(/^@/, '');
    nodes.forEach((n) => (n._hit = q.length > 1 && ((n.label || '').toLowerCase().includes(q) || (n.name || '').toLowerCase().includes(q))));
    paintFoot(); dirty = true;
  }
  function paintFoot() {
    const people = nodes.filter((n) => n.kind === 'lead'), on = people.filter((n) => n._on).length;
    const s = lastLoad ? Math.round((Date.now() - lastLoad) / 1000) : null;
    $('#map-foot').textContent = `${int(on)}${on !== people.length ? ' / ' + int(people.length) : ''} people · ${nodes.length - people.length} seeds`
      + (lastAdded ? ` · +${lastAdded} new` : '') + (s != null ? ` · updated ${s < 5 ? 'now' : s + 's ago'}` : '');
    $('#nav-map').textContent = people.length ? fmt(people.length) : '';
  }

  function requestImg(url) {
    let e = imgs.get(url);
    if (!e) imgs.set(url, (e = { state: 'idle', c: null }));
    if (e.state !== 'idle') return e;
    e.state = 'queued'; imgQueue.push(url); pumpImgs(); return e;
  }
  function pumpImgs() {
    while (loadingImgs < 8 && imgQueue.length) {
      const url = imgQueue.pop(), e = imgs.get(url);
      loadingImgs++;
      const im = new Image(); im.decoding = 'async';
      im.onload = () => {
        const s = 64, c = document.createElement('canvas'); c.width = c.height = s;
        const m = Math.min(im.naturalWidth, im.naturalHeight);
        c.getContext('2d').drawImage(im, (im.naturalWidth - m) / 2, (im.naturalHeight - m) / 2, m, m, 0, 0, s, s);
        e.c = c; e.state = 'ok'; loadingImgs--; dirty = true; pumpImgs();
      };
      im.onerror = () => { e.state = 'err'; loadingImgs--; pumpImgs(); };
      im.src = url;
    }
  }
  const toWorld = (px, py) => [(px - T.x) / T.k, (py - T.y) / T.k];

  function draw() {
    if (!colors) readColors();
    const k = T.k;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = colors.bg; ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.setTransform(DPR * k, 0, 0, DPR * k, DPR * T.x, DPR * T.y);
    const [x0, y0] = toWorld(0, 0), [x1, y1] = toWorld(W, H);
    const inView = (n, r) => n.x + r > x0 - 40 && n.x - r < x1 + 40 && n.y + r > y0 - 40 && n.y - r < y1 + 40;
    const hl = hover || focus, hlSet = new Set();
    if (hl) { hlSet.add(hl.id); links.forEach((l) => { if (l.source === hl || l.target === hl) { hlSet.add(l.source.id); hlSet.add(l.target.id); } }); }

    ctx.lineWidth = 1 / k; ctx.strokeStyle = colors.link; ctx.beginPath();
    for (const l of links) {
      if (!l.target._on || (hl && (l.source === hl || l.target === hl))) continue;
      ctx.moveTo(l.source.x, l.source.y); ctx.lineTo(l.target.x, l.target.y);
    }
    ctx.stroke();
    if (hl) {
      ctx.strokeStyle = colors.text2; ctx.lineWidth = 1 / k; ctx.beginPath();
      for (const l of links) if (l.source === hl || l.target === hl) { ctx.moveTo(l.source.x, l.source.y); ctx.lineTo(l.target.x, l.target.y); }
      ctx.stroke();
    }

    const showNames = k > 2.2;
    ctx.textAlign = 'center';
    for (const n of nodes) {
      if (n.kind !== 'lead') continue;
      const r = n._r;
      if (!inView(n, r)) continue;
      const dim = !n._on || (hl && !hlSet.has(n.id));
      ctx.globalAlpha = dim ? (n._on ? 0.22 : 0.07) : 1;
      let drawn = false;
      if (n.pic && r * k >= 5 && n._on) {
        const e = requestImg(n.pic);
        if (e.state === 'ok') { ctx.drawImage(e.c, n.x - r, n.y - r, r * 2, r * 2); drawn = true; }
      }
      if (!drawn) {
        ctx.fillStyle = (n.degree || 1) > 1 ? colors.text3 : colors.surface3;
        ctx.fillRect(n.x - r, n.y - r, r * 2, r * 2);
        if (r * k > 12) {
          ctx.fillStyle = colors.text; ctx.textBaseline = 'middle'; ctx.font = `600 ${r * 0.7}px Inter, system-ui, sans-serif`;
          ctx.fillText(initials(n.name, n.handle), n.x, n.y + r * 0.04);
        }
      }
      if (n._hit || n === hl) { ctx.lineWidth = 1.5 / k; ctx.strokeStyle = colors.text; ctx.strokeRect(n.x - r - 2 / k, n.y - r - 2 / k, r * 2 + 4 / k, r * 2 + 4 / k); }
      if ((showNames && !dim) || n._hit || n === hl) {
        ctx.fillStyle = colors.text2; ctx.textBaseline = 'top'; ctx.font = `500 ${11 / k}px Inter, system-ui, sans-serif`;
        ctx.fillText('@' + (n.handle || n.label), n.x, n.y + r + 3 / k);
      }
    }
    ctx.globalAlpha = 1;

    for (const n of nodes) {
      if (n.kind !== 'seed') continue;
      const r = n._r;
      if (!inView(n, r + 120 / k)) continue;
      const empty = !n.degree;
      ctx.globalAlpha = hl && !hlSet.has(n.id) ? 0.3 : empty ? 0.45 : 1;
      let drawn = false;
      if (n.pic) { const e = requestImg(n.pic); if (e.state === 'ok') { ctx.drawImage(e.c, n.x - r, n.y - r, 2 * r, 2 * r); drawn = true; } }
      if (!drawn) {
        ctx.fillStyle = colors.text; ctx.fillRect(n.x - r, n.y - r, 2 * r, 2 * r);
        ctx.fillStyle = colors.bg; ctx.textBaseline = 'middle'; ctx.font = `600 ${r * 0.6}px Inter, system-ui, sans-serif`;
        ctx.fillText(initials(null, n.label), n.x, n.y + r * 0.04);
      }
      ctx.lineWidth = 2 / k; ctx.strokeStyle = colors.bg; ctx.strokeRect(n.x - r - 1 / k, n.y - r - 1 / k, 2 * r + 2 / k, 2 * r + 2 / k);
      if (n === focus) { ctx.lineWidth = 1.5 / k; ctx.strokeStyle = colors.text; ctx.strokeRect(n.x - r - 4 / k, n.y - r - 4 / k, 2 * r + 8 / k, 2 * r + 8 / k); }
      if (empty && n !== hover && n !== focus && k < 1.6) continue;
      const fs = 12 / k, txt = '@' + n.label;
      ctx.font = `600 ${fs}px Inter, system-ui, sans-serif`;
      const bw = ctx.measureText(txt).width + 12 / k, bh = fs + 8 / k, ly = n.y + r + 6 / k;
      ctx.fillStyle = colors.surface; ctx.fillRect(n.x - bw / 2, ly, bw, bh);
      ctx.lineWidth = 1 / k; ctx.strokeStyle = colors.border; ctx.strokeRect(n.x - bw / 2, ly, bw, bh);
      ctx.fillStyle = colors.text; ctx.textBaseline = 'top'; ctx.fillText(txt, n.x, ly + 4 / k);
    }
    ctx.globalAlpha = 1;
  }
  function loop() { if (!visible) return; if (dirty) { dirty = false; draw(); } requestAnimationFrame(loop); }

  function makeSim() {
    sim = d3.forceSimulation()
      .force('link', d3.forceLink().id((d) => d.id).distance((l) => 70 + l.source._r * 2).strength((l) => 1 / Math.min(4, l.target.degree || 1) * 0.6))
      .force('charge', d3.forceManyBody().strength((d) => (d.kind === 'seed' ? -1100 : -12)).distanceMax(800).theta(0.95))
      .force('collide', d3.forceCollide().radius((d) => d._r * 1.3 + (d.kind === 'seed' ? 14 : 1.5)).iterations(1))
      .force('x', d3.forceX(0).strength(0.012)).force('y', d3.forceY(0).strength(0.012))
      .alphaDecay(0.03).on('tick', () => (dirty = true));
    sim.stop();
  }
  function merge(data) {
    if (!sim) makeSim();
    const fresh = byId.size === 0;
    const adj = new Map();
    data.links.forEach((l) => { (adj.get(l.target) || adj.set(l.target, []).get(l.target)).push(l.source); });
    const next = new Map();
    const seeds = data.nodes.filter((n) => n.kind === 'seed');
    seeds.forEach((n, i) => {
      let o = byId.get(n.id);
      if (o) Object.assign(o, n);
      else { const a = (i / seeds.length) * Math.PI * 2, R = 240 + seeds.length * 26; o = { ...n, x: Math.cos(a) * R, y: Math.sin(a) * R }; }
      o._r = radius(o); next.set(o.id, o);
    });
    let added = 0;
    data.nodes.forEach((n) => {
      if (n.kind === 'seed') return;
      let o = byId.get(n.id);
      if (o) Object.assign(o, n);
      else {
        const nb = (adj.get(n.id) || []).map((id) => next.get(id)).filter(Boolean);
        let x = 0, y = 0;
        nb.forEach((s) => { x += s.x; y += s.y; });
        if (nb.length) { x /= nb.length; y /= nb.length; }
        const j = nb.length > 1 ? 24 : 80;
        o = { ...n, x: x + (Math.random() - 0.5) * j, y: y + (Math.random() - 0.5) * j };
        added++;
      }
      o._r = radius(o); next.set(o.id, o);
    });
    byId = next; nodes = [...next.values()];
    links = data.links.filter((l) => next.has(l.source) && next.has(l.target)).map((l) => ({ source: l.source, target: l.target }));
    sim.nodes(nodes); sim.force('link').links(links);
    if (hover && !byId.has(hover.id)) hover = null;
    if (focus && !byId.has(focus.id)) focus = null;
    if (!fresh) lastAdded = added;
    refilter();
    if (fresh) { sim.alpha(1); for (let i = 0; i < 180; i++) sim.tick(); fit(false); sim.alpha(0.06).restart(); }
    else if (added) sim.alpha(Math.max(sim.alpha(), 0.08)).restart();
  }
  async function load(force) {
    if (!visible && !force) return;
    let data;
    try { data = await api.get(`/api/map?scope=${scope}&limit=${scope === 'all' ? 2000 : 900}`); } catch (e) { return; }
    lastLoad = Date.now();
    if (!force && data.rev === rev) { paintFoot(); return; }
    rev = data.rev; merge(data);
  }
  function fit(animate) {
    const ns = nodes.filter((n) => n._on !== false);
    if (!ns.length || !W) return;
    let a = Infinity, b = Infinity, c = -Infinity, d = -Infinity;
    ns.forEach((n) => { a = Math.min(a, n.x - n._r); b = Math.min(b, n.y - n._r); c = Math.max(c, n.x + n._r); d = Math.max(d, n.y + n._r + 24); });
    const k = Math.min(3, 0.92 * Math.min(W / (c - a), H / (d - b)));
    const to = { k, x: W / 2 - ((a + c) / 2) * k, y: H / 2 - ((b + d) / 2) * k };
    animate ? animateTo(to) : (T = to); dirty = true;
  }
  function animateTo(to) {
    const from = { ...T }, t0 = performance.now();
    const step = (t) => {
      const p = Math.min(1, (t - t0) / 420), e = 1 - Math.pow(1 - p, 3);
      T = { x: from.x + (to.x - from.x) * e, y: from.y + (to.y - from.y) * e, k: from.k + (to.k - from.k) * e };
      dirty = true; if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  function hit(px, py) {
    const [x, y] = toWorld(px, py);
    let best = null;
    for (const n of nodes) {
      if (n.kind === 'lead' && !n._on) continue;
      const pad = 3 / T.k;
      if (Math.abs(n.x - x) <= n._r + pad && Math.abs(n.y - y) <= n._r + pad && (!best || n.kind === 'seed' || best.kind !== 'seed')) best = n;
    }
    return best;
  }
  function showCard(n, px, py) {
    if (!n) { hc.hidden = true; return; }
    if (n.kind === 'seed') {
      hc.innerHTML = `<div class="hc-top">${pfp({ handle: n.label, pic: n.pic })}<div><div class="nm">@${esc(n.label)}</div><div class="muted num">${int(n.degree)} people linked</div></div></div>`;
    } else {
      const tags = (n.tags || []).filter((t) => /^via @|^in \d+ lists$|^knows you|^you follow|^follows you/.test(t) || S.manual.has(t));
      hc.innerHTML = `<div class="hc-top">${pfp({ name: n.name, handle: n.handle, pic: n.pic })}<div style="min-width:0"><div class="nm">${esc(n.name || n.handle)}</div><div class="muted">@${esc(n.handle)}</div></div></div>
        <div class="tags">${tags.slice(0, 10).map((t) => tagChip({ tag: t })).join('')}</div>`;
    }
    placeCard(px, py); hc.hidden = false;
  }
  function placeCard(px, py) {
    const w = 280, h = hc.offsetHeight || 100;
    let x = px + 16, y = py + 16;
    if (x + w > W - 8) x = px - w - 16;
    if (y + h > H - 8) y = Math.max(8, H - h - 8);
    hc.style.left = x + 'px'; hc.style.top = y + 'px';
  }

  let drag = null;
  const P = (e) => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  canvas.addEventListener('pointerdown', (e) => {
    canvas.setPointerCapture(e.pointerId);
    const [x, y] = P(e), n = hit(x, y);
    drag = { n, sx: x, sy: y, lx: x, ly: y, moved: false };
    if (!n) canvas.classList.add('dragging');
  });
  canvas.addEventListener('pointermove', (e) => {
    const [x, y] = P(e);
    if (drag) {
      const dx = x - drag.lx, dy = y - drag.ly; drag.lx = x; drag.ly = y;
      if (Math.abs(x - drag.sx) + Math.abs(y - drag.sy) > 3) drag.moved = true;
      if (!drag.moved) return;
      hc.hidden = true;
      if (drag.n) { const [wx, wy] = toWorld(x, y); drag.n.fx = wx; drag.n.fy = wy; sim.alphaTarget(0.2).restart(); }
      else { T.x += dx; T.y += dy; }
      dirty = true; return;
    }
    const n = hit(x, y);
    canvas.classList.toggle('pointer', !!n);
    if (n !== hover) { hover = n; dirty = true; showCard(n, x, y); } else if (n) placeCard(x, y);
  });
  canvas.addEventListener('pointerup', () => {
    canvas.classList.remove('dragging');
    if (!drag) return;
    const d = drag; drag = null;
    if (d.n && d.moved) { d.n.fx = null; d.n.fy = null; sim.alphaTarget(0); }
    if (!d.moved) {
      if (d.n && d.n.kind === 'lead') openDrawer(+d.n.id.slice(2));
      else if (d.n && d.n.kind === 'seed') focus = focus === d.n ? null : d.n;
      else focus = null;
      dirty = true;
    }
  });
  canvas.addEventListener('pointerleave', () => { if (!drag) { hover = null; hc.hidden = true; dirty = true; } });
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const [px, py] = P(e), f = Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0018));
    const k = Math.max(0.06, Math.min(8, T.k * f)), [wx, wy] = toWorld(px, py);
    T = { k, x: px - wx * k, y: py - wy * k }; hc.hidden = true; hover = null; dirty = true;
  }, { passive: false });

  function paintTools() {
    document.querySelectorAll('#map-scope button').forEach((b) => b.classList.toggle('on', b.dataset.scope === scope));
    $('#m-lists').value = String(filters.lists);
    [['#m-via', filters.via], ['#m-tag', filters.tag], ['#m-lists', filters.lists]].forEach(([s, v]) => $(s).classList.toggle('set', !!v));
  }
  const saveF = () => store.set('fl-map-filters', filters);
  $('#map-scope').onclick = (e) => {
    const b = e.target.closest('button'); if (!b || b.dataset.scope === scope) return;
    scope = b.dataset.scope; store.set('fl-map-scope', scope); paintTools(); byId = new Map(); rev = null; load(true);
  };
  $('#m-via').onchange = (e) => { filters.via = e.target.value; saveF(); paintTools(); refilter(); };
  $('#m-tag').onchange = (e) => { filters.tag = e.target.value; saveF(); paintTools(); refilter(); };
  $('#m-lists').onchange = (e) => { filters.lists = +e.target.value; saveF(); paintTools(); refilter(); };
  $('#map-fit').onclick = () => fit(true);
  $('#map-q').addEventListener('input', refilter);
  $('#map-q').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const n = nodes.find((n) => n._hit); if (!n) return;
    focus = n; const k = Math.max(T.k, 2.6);
    animateTo({ k, x: W / 2 - n.x * k, y: H / 2 - n.y * k });
  });
  paintTools();

  let pollT = null, footT = null;
  return {
    filters,
    show() {
      visible = true; resize(); readColors();
      if (!byId.size) load(true); else { load(); dirty = true; }
      requestAnimationFrame(loop);
      clearInterval(pollT); pollT = setInterval(() => { if (!document.hidden) load(); }, 8000);
      clearInterval(footT); footT = setInterval(paintFoot, 1000);
    },
    hide() { visible = false; clearInterval(pollT); clearInterval(footT); hc.hidden = true; if (sim) sim.stop(); },
    themeChanged() { readColors(); dirty = true; },
  };
})();

/* ---------- Boot ---------- */
syncFilterUI();
route();
syncSeedBtn();
loadCounts();
loadTags().then(() => resetLeads());
loadScraper();
setInterval(() => { if (!document.hidden) loadScraper(); }, 4000);
setInterval(() => {
  if (!S.sc || !/^Next request/i.test((S.sc.ext || {}).text || '')) return;
  paintStatus(S.sc);
  if ($('#kpi-state')) $('#kpi-state').textContent = scraperState(S.sc).label;
}, 1000);
setInterval(() => {
  if (document.hidden) return;
  loadCounts();
  // Refresh the table only when the user is at the top and not reading a drawer, so rows never jump.
  if (S.view === 'leads' && $('#list-scroll').scrollTop < ROW && $('#drawer').hidden && !L.loading) resetLeads(true);
}, 20000);
setInterval(() => { if (!document.hidden) loadTags(); }, 60000);
