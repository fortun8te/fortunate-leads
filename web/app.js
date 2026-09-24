'use strict';
/* Fortunate Leads UI. Plain JS, no build step. */

// ---------- utilities ----------
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (v) => { if (v == null || v === '' || !Number.isFinite(+v)) return '–'; const n = +v; return n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M' : n >= 1e4 ? Math.round(n / 1e3) + 'k' : n >= 1e3 ? (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'k' : String(Math.round(n)); };
const int = (n) => n == null || n === '' || !Number.isFinite(+n) ? '–' : Number(n).toLocaleString('en-US');
// Only http(s) URLs become links; anything else (javascript:, data:) is never rendered as an href.
const safeUrl = (u) => typeof u === 'string' && /^https?:\/\//i.test(u.trim()) ? u.trim() : null;
const safePic = (u) => typeof u === 'string' && (/^\/img\/\d+$/.test(u) || /^https?:\/\//i.test(u)) ? u : null;
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
const ucf = (s) => { s = String(s ?? ''); return s.charAt(0).toUpperCase() + s.slice(1); };
const plural = (n, a, b) => int(n) + ' ' + (n === 1 ? a : b || a + 's');
function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }

let offlineSince = null;
function setOnline(ok) {
  if (ok) { const was = offlineSince; offlineSince = null; $('#offline').hidden = true; if (was) reconnected(); return; }
  offlineSince = offlineSince || Date.now();
  $('#offline').hidden = false;
  $('#offline-t').textContent = ago(new Date(offlineSince).toISOString());
}
const api = {
  async req(url, opts) {
    let r;
    try { r = await fetch(url, { cache: 'no-store', ...opts }); } catch (e) { setOnline(false); throw e; }
    setOnline(true);
    if (!r.ok) {
      let msg = 'HTTP ' + r.status;
      try { const b = await r.json(); if (b && typeof b.error === 'string') msg = b.error; } catch (e) { /* not json */ }
      const err = new Error(msg); err.status = r.status; throw err;
    }
    return r.json();
  },
  get(url) { return this.req(url); },
  post(url, body) { return this.req(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }); },
};

let toastT;
function toast(msg, undo) {
  const el = $('#toast');
  el.innerHTML = `<span>${esc(msg)}</span>${undo ? '<button id="undo">Undo</button>' : ''}`;
  el.hidden = false;
  if (undo) $('#undo').onclick = () => { el.hidden = true; undo(); };
  clearTimeout(toastT); toastT = setTimeout(() => { el.hidden = true; }, undo ? 6000 : 2400);
}

function avatar(pic, name, cls = '') {
  const i = esc(initials(name));
  pic = safePic(pic);
  if (!pic) return `<span class="av ${cls}">${i}</span>`;
  return `<span class="av ${cls}" data-i="${i}"><img src="${esc(pic)}" alt="" loading="lazy"></span>`;
}
// Broken pictures fall back to initials (error events do not bubble, so capture).
document.addEventListener('error', (e) => {
  const img = e.target;
  if (img.tagName === 'IMG' && img.parentElement?.classList.contains('av')) img.parentElement.textContent = img.parentElement.dataset.i || '?';
}, true);

// ---------- constants ----------
// The pipeline, in order. Keys 1-5 set it, 0 clears, m steps forward. 'no' hides the person from the default views.
const STATUSES = ['interested', 'contacted', 'talking', 'client', 'no'];
const SLABEL = { interested: 'Interested', contacted: 'Contacted', talking: 'Talking', client: 'Client', no: 'Not a fit' };
const SDESC = { interested: 'Worth contacting', contacted: 'You sent the first message', talking: 'They replied, a conversation is going',
  client: 'Paying client', no: 'Not for you: hidden from the list' };
const LEGACY_STATUS = { good: 'interested' };
const slabel = (s) => SLABEL[s] || ucf(s);
const CYCLE = [null, 'interested', 'contacted', 'talking', 'client'];
const GROUPS = [['ai', 'AI verdict'], ['role', 'Role'], ['niche', 'Niche'], ['signal', 'Signal'], ['size', 'Size']];
const LIST_OPTS = [[2, '2+'], [3, '3+'], [4, '4+'], [5, '5+']];
const BIO_OPTS = [['1', 'Yes'], ['0', 'No']];
const FOL_OPTS = [['lt1k', '<1k', null, 999], ['1k', '1k+', 1000, null], ['10k', '10k+', 10000, null], ['100k', '100k+', 100000, null]];
const PAGE = 100;
const isViaTag = (t) => t.startsWith('via @');
const isListTag = (t) => /^in \d+ lists$/.test(t);
const KIND = { manual: 'man', rule: 'rule', auto: '' };
const MODES = ['inc', 'any', 'exc'];
// Fit: the verdict tier under the names the UI uses (the server sends it as `fit` on map nodes).
const FITS = ['strong', 'good', 'weak', 'unread'];
const FIT_LABEL = { strong: 'Strong', good: 'Good', weak: 'Weak', unread: 'Unread' };
const TIER_FIT = { hot: 'strong', warm: 'good', cold: 'weak', unread: 'unread' };
const FIT_TIER = { strong: 'hot', good: 'warm', weak: 'cold', unread: 'unread' };
const isFitTag = (t) => /^Fit: /.test(t);
const tagName = (t) => (typeof t === 'string' ? t : t.tag);
const fitOf = (r) => r.fit || TIER_FIT[r.tier] || 'unread';
function fitBadge(r, cls = '') {
  const f = fitOf(r);
  const score = f !== 'unread' && r.score != null ? `<b>${esc(r.score)}</b>` : '';
  return `<span class="fit f-${f} ${cls}" title="Fit${r.score != null ? ' score ' + esc(r.score) : ''}"><i></i>${FIT_LABEL[f]}${score}</span>`;
}
// Seeds a person was found through, from the "via @seed" tags (these leave out Michael's own account).
const viaSeeds = (r) => (r.tags || []).map(tagName).filter(isViaTag).map((t) => t.slice(5));
const YOU = { 'follows you': 'Follows you', 'you follow': 'You follow', 'knows you': 'Knows you' };
const youLink = (r) => { const names = (r.tags || []).map(tagName); const k = ['follows you', 'you follow', 'knows you'].find((t) => names.includes(t)); return k ? YOU[k] : ''; };
const seedList = (seeds, n) => seeds.slice(0, n).map((s) => '@' + esc(s)).join(', ') + (seeds.length > n ? ` +${seeds.length - n}` : '');
function connHTML(r) {
  const n = lists(r), via = viaSeeds(r), you = youLink(r);
  return `<span class="c1">${n ? `In ${plural(n, 'list')}` : 'No lists'}${you ? ` · <em>${you}</em>` : ''}</span>${via.length ? `<span class="c2">via ${seedList(via, 2)}</span>` : ''}`;
}

// ---------- state ----------
const emptyFilter = () => ({ tags: [], any: [], not: [], status: '', tier: '', q: '', min: 0, bio: '', seed: '', fmin: null, fmax: null });
const S = {
  view: 'leads',
  f: emptyFilter(), sort: store.get('sort', 'fit'),
  tagList: [], tagBy: new Map(), tagLower: new Map(), counts: null, sc: null, views: [], viewsLocal: false,
  rows: [], total: null, done: false, loading: false, error: false, gen: 0,
  cur: -1, open: null, person: null, seedCard: null,
  pick: new Set(), anchor: -1, picking: false,
  tagMore: {}, tagFind: '', saving: false,
  side: store.get('side', true),
};

// ---------- filter <-> query string ----------
function toQuery(f = S.f, sort = S.sort, withSort = true) {
  const p = new URLSearchParams();
  if (f.tags.length) p.set('tags', f.tags.join(','));
  if (f.any.length) p.set('any', f.any.join(','));
  if (f.not.length) p.set('not', f.not.join(','));
  if (f.status) p.set('status', f.status);
  if (f.tier) p.set('tier', f.tier);
  if (f.q) p.set('q', f.q);
  if (f.min) p.set('min_lists', f.min);
  if (f.bio) p.set('has_bio', f.bio);
  if (f.seed) p.set('seed', f.seed);
  if (f.fmin != null) p.set('followers_min', f.fmin);
  if (f.fmax != null) p.set('followers_max', f.fmax);
  if (withSort && sort && sort !== 'fit') p.set('sort', sort);
  return p;
}
function fromQuery(qs) {
  const p = new URLSearchParams(qs || '');
  const list = (k) => (p.get(k) || '').split(',').map((s) => s.trim()).filter(Boolean);
  const numOr = (k) => p.get(k) != null && p.get(k) !== '' && !isNaN(+p.get(k)) ? +p.get(k) : null;
  return {
    f: { tags: list('tags'), any: list('any'), not: list('not'), status: p.get('status') || '', tier: FIT_TIER[TIER_FIT[p.get('tier')]] || '', q: p.get('q') || '', min: +p.get('min_lists') || 0,
      bio: ['0', '1'].includes(p.get('has_bio')) ? p.get('has_bio') : '', seed: (p.get('seed') || '').replace(/^@/, ''), fmin: numOr('followers_min'), fmax: numOr('followers_max') },
    sort: p.get('sort') || 'fit',
  };
}
const filterCount = (f = S.f) => f.tags.length + f.any.length + f.not.length + !!f.status + !!f.tier + !!f.min + !!f.bio + !!f.seed + (f.fmin != null || f.fmax != null) + !!f.q;
const modeOf = (t) => S.f.tags.includes(t) ? 'inc' : S.f.any.includes(t) ? 'any' : S.f.not.includes(t) ? 'exc' : null;
function setMode(t, mode) {
  S.f.tags = S.f.tags.filter((x) => x !== t); S.f.any = S.f.any.filter((x) => x !== t); S.f.not = S.f.not.filter((x) => x !== t);
  if (mode === 'inc') S.f.tags.push(t); else if (mode === 'any') S.f.any.push(t); else if (mode === 'exc') S.f.not.push(t);
}
// click: all -> any -> none -> off; shift: any; alt: none.
function clickTag(t, e) {
  const m = modeOf(t);
  const next = e && e.altKey ? (m === 'exc' ? null : 'exc') : e && e.shiftKey ? (m === 'any' ? null : 'any') : m === null ? 'inc' : m === 'inc' ? 'any' : m === 'any' ? 'exc' : null;
  setMode(t, next);
  filtersChanged();
}
const folLabel = (a, b) => a != null && b != null ? `${fmt(a)}-${fmt(b)}` : a != null ? `${fmt(a)}+` : `<${fmt((b || 0) + 1)}`;
const parseNum = (s) => { const m = String(s).trim().toLowerCase().match(/^(\d+(?:\.\d+)?)([km]?)$/); return m ? Math.round(+m[1] * (m[2] === 'k' ? 1e3 : m[2] === 'm' ? 1e6 : 1)) : null; };

// Token text for a tag. Tags with spaces are quoted.
function tagTok(t, mode) {
  const pre = mode === 'any' ? '~' : mode === 'exc' ? '-' : '';
  if (isViaTag(t) && /^via @[\w.]+$/.test(t)) return pre + 'via:@' + t.slice(5);
  return pre + '#' + (/^[^\s"]+$/.test(t) ? t : '"' + t + '"');
}
function tokens() {
  const out = [];
  for (const mode of MODES) for (const t of S.f[mode === 'inc' ? 'tags' : mode === 'any' ? 'any' : 'not']) out.push({ k: 'tag', tag: t, mode, text: tagTok(t, mode) });
  if (S.f.status) out.push({ k: 'status', text: 'status:' + S.f.status });
  if (S.f.tier) out.push({ k: 'tier', text: 'fit:' + TIER_FIT[S.f.tier] });
  if (S.f.min) out.push({ k: 'min', text: `lists:${S.f.min}+` });
  if (S.f.bio) out.push({ k: 'bio', text: 'bio:' + (S.f.bio === '1' ? 'yes' : 'no') });
  if (S.f.seed) out.push({ k: 'seed', text: 'seed:@' + S.f.seed });
  if (S.f.fmin != null || S.f.fmax != null) out.push({ k: 'fol', text: 'followers:' + folLabel(S.f.fmin, S.f.fmax) });
  return out;
}
const TOKEN_RX = /^[~+|-]?(#|via:)|^(status|fit|lists|bio|seed|followers):/i;
function canonTag(name) { return S.tagBy.has(name) ? name : S.tagLower.get(name.toLowerCase()) || null; }
// Parses one typed token. Returns a function that applies it, or null.
function parseToken(w, strict) {
  let m;
  if ((m = w.match(/^([~+|-]?)#"(.+)"$/)) || (m = w.match(/^([~+|-]?)#([^\s"]+)$/))) {
    const t = canonTag(m[2]);
    if (!t && strict) return null;
    const mode = m[1] === '~' || m[1] === '|' ? 'any' : m[1] === '-' ? 'exc' : 'inc';
    return () => setMode(t || m[2], mode);
  }
  if ((m = w.match(/^([~+|-]?)via:@?([\w.]+)$/i))) {
    const mode = m[1] === '~' || m[1] === '|' ? 'any' : m[1] === '-' ? 'exc' : 'inc';
    return () => setMode(canonTag('via @' + m[2]) || 'via @' + m[2].toLowerCase(), mode);
  }
  if ((m = w.match(/^status:(\w*)$/i))) {
    let s = m[1].toLowerCase(); if (s === 'unmarked') s = 'none'; s = LEGACY_STATUS[s] || s;
    if (s && s !== 'open' && s !== 'none' && s !== 'all' && !STATUSES.includes(s)) return null;
    return () => { S.f.status = s === 'open' ? '' : s; };
  }
  if ((m = w.match(/^fit:(\w+)$/i))) { const t = FIT_TIER[m[1].toLowerCase()]; return t ? () => { S.f.tier = t; } : null; }
  if ((m = w.match(/^lists:(\d+)\+?$/i))) return () => { S.f.min = +m[1] > 1 ? +m[1] : 0; };
  if ((m = w.match(/^bio:(yes|no|1|0)$/i))) return () => { S.f.bio = /^(yes|1)$/i.test(m[1]) ? '1' : '0'; };
  if ((m = w.match(/^seed:@?([\w.]+)$/i))) return () => { S.f.seed = m[1].toLowerCase(); };
  if ((m = w.match(/^followers:(.+)$/i))) {
    const v = m[1].trim();
    let a = null, b = null, x;
    if ((x = v.match(/^([\d.]+[km]?)\+$/i)) || (x = v.match(/^>=?([\d.]+[km]?)$/i))) a = parseNum(x[1]);
    else if ((x = v.match(/^<([\d.]+[km]?)$/i))) { b = parseNum(x[1]); if (b != null) b -= 1; }
    else if ((x = v.match(/^([\d.]+[km]?)-([\d.]+[km]?)$/i))) { a = parseNum(x[1]); b = parseNum(x[2]); }
    else return null;
    if (a == null && b == null) return null;
    return () => { S.f.fmin = a; S.f.fmax = b; };
  }
  return null;
}
// Split on spaces, keeping quoted parts together.
const words = (s) => s.match(/[^\s"]*"[^"]*"?|\S+/g) || [];

// ---------- routing / URL ----------
function parseHash() {
  const h = location.hash.replace(/^#/, '') || '/leads';
  const i = h.indexOf('?');
  const v = (i < 0 ? h : h.slice(0, i)).replace(/^\//, '');
  return { view: ['leads', 'map', 'qual', 'tags', 'scraper', 'accounts', 'settings'].includes(v) ? v : 'leads', qs: i < 0 ? '' : h.slice(i + 1) };
}
function hashFor(view) {
  const qs = view === 'leads' || view === 'map' ? toQuery().toString() : '';
  return '#/' + view + (qs ? '?' + qs : '');
}
function setURL(push) {
  const url = hashFor(S.view);
  if (location.hash === url) return;
  if (push) history.pushState(null, '', url); else history.replaceState(null, '', url);
  syncTabs();
}
function syncTabs() {
  $$('.tabs a').forEach((a) => { a.href = hashFor(a.dataset.view); a.classList.toggle('on', a.dataset.view === S.view); });
}
function route() {
  const { view, qs } = parseHash();
  if (view === 'leads' || view === 'map') {
    const p = fromQuery(qs);
    if (toQuery(p.f, p.sort).toString() !== toQuery().toString()) {
      S.f = p.f; S.sort = p.sort; $('#sort').value = S.sort; $('#q').value = S.f.q;
      filtersChanged({ url: false });
    }
  }
  setView(view);
}
window.addEventListener('popstate', route);
window.addEventListener('hashchange', route);

function setView(v) {
  const prev = S.view;
  S.view = v;
  const work = v === 'leads' || v === 'map';
  $('#view-work').classList.toggle('on', work);
  $('#view-tags').classList.toggle('on', v === 'tags');
  $('#view-qual').classList.toggle('on', v === 'qual');
  $('#view-scraper').classList.toggle('on', v === 'scraper');
  $('#view-accounts').classList.toggle('on', v === 'accounts');
  $('#view-settings').classList.toggle('on', v === 'settings');
  $('#pane-leads').classList.toggle('on', v === 'leads');
  $('#pane-map').classList.toggle('on', v === 'map');
  syncTabs();
  if (v === 'map') M.show(); else if (prev === 'map') M.hide();
  if (v === 'leads' && prev !== 'leads') renderRows();
  if (v !== 'map' && S.seedCard) { S.seedCard = null; if (!S.open) $('#detail').hidden = true; }
  if (prev !== v && narrow() && (S.open || S.seedCard)) closeDetail();
  if (v === 'scraper') renderScraper();
  if (v === 'accounts') renderAccounts();
  if (v !== 'accounts' && A.wiz) closeWizard();
  if (v === 'settings') loadSettings();
  if (v === 'tags') T.show();
  if (v === 'qual') Q.show();
  if (!work) hideSuggest();
}

// Apply a filter change: URL, sidebar, tokens, list, facets, map.
function filtersChanged(o = {}) {
  S.f.tags = [...new Set(S.f.tags)]; S.f.any = [...new Set(S.f.any)]; S.f.not = [...new Set(S.f.not)];
  if (o.url !== false) setURL(o.push !== false);
  syncTabs();
  renderFilters(); renderTokens();
  resetLeads();
  loadFacets(); loadCounts();
  M.stale = true; M.relayout = true;
  if (S.view === 'map') M.reloadSoon();
}
function clearFilters() {
  S.f = emptyFilter(); $('#q').value = '';
  filtersChanged();
}

// ---------- theme / density / sidebar ----------
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  $('#theme-btn').textContent = t === 'dark' ? 'Light' : 'Dark';
  store.set('theme', t);
  M.draw();
}
$('#theme-btn').onclick = () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
function applyDensity(d) {
  document.documentElement.dataset.density = d;
  $('#density-btn').textContent = d === 'compact' ? 'Comfortable' : 'Compact';
  store.set('density', d);
  renderRows();
}
$('#density-btn').onclick = () => applyDensity(document.documentElement.dataset.density === 'compact' ? 'comfortable' : 'compact');
const narrow = () => window.innerWidth <= 900;
function toggleSide() {
  if (narrow()) { setDrawer(!$('#filters').classList.contains('show')); return; }
  S.side = !S.side; store.set('side', S.side);
  $('#view-work').classList.toggle('work-noside', !S.side);
  renderRows(); M.resize();
}

// ---------- tags & counts ----------
function mergeTags(list) {
  const m = new Map();
  for (const t of list || []) {
    const e = m.get(t.tag) || { tag: t.tag, grp: t.grp, sources: [], count: 0, total: 0 };
    e.count += t.count || 0; e.total += t.total ?? t.count ?? 0;
    if (!e.sources.includes(t.source)) e.sources.push(t.source);
    if (t.source === 'manual' || !e.grp) e.grp = t.grp;
    m.set(t.tag, e);
  }
  for (const e of m.values()) e.kind = e.sources.includes('manual') ? 'manual' : e.sources.includes('rule') ? 'rule' : 'auto';
  return [...m.values()];
}
let facetGen = 0;
async function loadFacets() {
  const g = ++facetGen;
  let d;
  try { d = await api.get('/api/tags?' + toQuery(S.f, S.sort, false)); } catch (e) { return; }
  if (g !== facetGen) return;
  S.tagList = mergeTags(d);
  S.tagBy = new Map(S.tagList.map((t) => [t.tag, t]));
  S.tagLower = new Map(S.tagList.map((t) => [t.tag.toLowerCase(), t.tag]));
  $('#tag-dl').innerHTML = S.tagList.filter((t) => t.grp !== 'source').sort((a, b) => b.total - a.total).map((t) => `<option value="${esc(t.tag)}">`).join('');
  renderFilters();
  if (!$('#suggest').hidden) suggest();
}
const loadFacetsSoon = debounce(loadFacets, 120);
let countGen = 0;
async function loadCounts() {
  const g = ++countGen;
  let d;
  try { d = await api.get('/api/counts?' + toQuery(S.f, S.sort, false)); } catch (e) { return; }
  if (g !== countGen) return;
  S.counts = d;
  $('#n-leads').textContent = fmt(d.total);
  renderFilters();
}
async function loadViews() {
  try { S.views = await api.get('/api/views'); S.viewsLocal = false; }
  catch (e) { S.views = store.get('views', []); S.viewsLocal = true; }
  renderFilters();
}

// ---------- sidebar ----------
const swatch = (kind, extra = '', grp = '') => `<i class="sw ${KIND[kind] ?? ''} ${extra}${grp ? ' g-' + esc(grp) : ''}"></i>`;
function tagItem(t, label) {
  const m = modeOf(t.tag);
  const n = t.count;
  const title = `${t.tag} · ${t.kind}${t.sources.length > 1 ? ' + ' + t.sources.filter((s) => s !== t.kind).join(', ') : ''}`;
  return `<button class="fi${m ? ' ' + m : ''}${!m && !n ? ' zero' : ''}" data-tag="${esc(t.tag)}" title="${esc(title)}">${swatch(t.kind, t.grp === 'source' ? 'src' : '', t.grp)}<span>${esc(label || t.tag)}</span><b>${fmt(n)}</b></button>`;
}
function tagSection(key, title, list, labelFn) {
  const q = S.tagFind.toLowerCase();
  if (q) list = list.filter((t) => t.tag.toLowerCase().includes(q));
  if (!list.length) return '';
  list = [...list].sort((a, b) => (!!modeOf(b.tag) - !!modeOf(a.tag)) || (b.count > 0) - (a.count > 0) || b.count - a.count || b.total - a.total || a.tag.localeCompare(b.tag));
  const lim = S.tagMore[key] || q ? 500 : 8;
  const shown = list.slice(0, Math.max(lim, list.filter((t) => modeOf(t.tag)).length));
  return `<div class="fsec"><h4>${esc(title)}<span class="grow"></span><span class="num">${list.length}</span></h4>
    ${shown.map((t) => tagItem(t, labelFn && labelFn(t))).join('')}
    ${list.length > shown.length ? `<button class="fmore" data-more="${key}">+${list.length - shown.length} more</button>` : S.tagMore[key] && list.length > 8 ? `<button class="fmore" data-less="${key}">Less</button>` : ''}</div>`;
}
function renderFilters() {
  const c = S.counts || {};
  const qs = toQuery().toString();
  const active = filterCount();
  $('#fbtn-n').textContent = active ? ' ' + active : '';
  let h = `<div class="fsec"><h4>Views<span class="grow"></span>${active ? '<button id="f-reset" title="Clear filters (c)">Clear</button>' : ''}<button id="v-new" title="Save view (v)">Save</button></h4>
    ${S.saving ? `<form class="fsave" id="v-form"><input class="input" id="v-name" placeholder="View name" autocomplete="off"><button class="btn solid">Save</button></form>` : ''}
    <button class="fi${!active ? ' on' : ''}" data-view=""><span>Everyone</span><b>${fmt(c.total)}</b></button>
    ${S.views.map((v) => `<button class="fi${v.query === qs && active ? ' on' : ''}" data-view="${esc(v.query)}" title="${esc(v.query)}"><span>${esc(v.name)}</span><i class="del" data-vdel="${esc(v.id)}" title="Delete view">&times;</i></button>`).join('')}</div>
    <div class="fsec"><h4>Status</h4>
    <button class="fi${!S.f.status ? ' on' : ''}" data-status=""><span>Open</span><b>${fmt(c.open)}</b></button>
    ${STATUSES.map((s) => `<button class="fi${S.f.status === s ? ' on' : ''}${c[s] ? '' : ' zero'}" data-status="${s}" title="${esc(SDESC[s])}"><span>${slabel(s)}</span><b>${c[s] ? fmt(c[s]) : ''}</b></button>`).join('')}
    <button class="fi${S.f.status === 'none' ? ' on' : ''}" data-status="none" title="No status yet"><span>Unmarked</span><b>${fmt(c.none)}</b></button>
    <button class="fi${S.f.status === 'all' ? ' on' : ''}" data-status="all" title="Everyone, including Not a fit"><span>All</span><b>${c.open != null ? fmt(c.open + (c.no || 0)) : ''}</b></button></div>
    <div class="fsec"><h4>Fit</h4>
    ${FITS.map((f) => `<button class="fi${S.f.tier === FIT_TIER[f] ? ' on' : ''}" data-tier="${FIT_TIER[f]}"><i class="fdot f-${f}"></i><span>${FIT_LABEL[f]}</span><b>${c[FIT_TIER[f]] != null ? fmt(c[FIT_TIER[f]]) : ''}</b></button>`).join('')}</div>
    <div class="fsec fsegs"><h4>Shape</h4>
      <div class="fseg"><span>Lists</span><div class="seg">${LIST_OPTS.map(([n, l]) => `<button data-min="${n}" class="${S.f.min === n ? 'on' : ''}">${l}</button>`).join('')}</div></div>
      <div class="fseg"><span>Bio</span><div class="seg">${BIO_OPTS.map(([v, l]) => `<button data-bio="${v}" class="${S.f.bio === v ? 'on' : ''}">${l}</button>`).join('')}</div></div>
      <div class="fseg"><span>Followers</span><div class="seg">${FOL_OPTS.map(([k, l, a, b]) => `<button data-fol="${k}" class="${S.f.fmin === a && S.f.fmax === b ? 'on' : ''}">${l}</button>`).join('')}</div></div>
    </div>
    <div class="fsec"><input class="input ffind" id="f-find" type="search" placeholder="Find tag" value="${esc(S.tagFind)}" autocomplete="off" spellcheck="false"></div>`;
  const tags = S.tagList;
  const own = tags.filter((t) => t.kind === 'manual');
  const rule = tags.filter((t) => t.kind === 'rule');
  const auto = tags.filter((t) => t.kind === 'auto');
  h += tagSection('own', 'Your tags', own);
  if (!own.length && !S.tagFind) h += `<div class="fsec"><h4>Your tags</h4><span class="fnone">None yet</span></div>`;
  h += tagSection('rule', 'Rule tags', rule);
  h += tagSection('via', 'Seeds', auto.filter((t) => isViaTag(t.tag)), (t) => t.tag.slice(4));
  for (const [g, title] of GROUPS) h += tagSection(g, title, auto.filter((t) => t.grp === g));
  h += tagSection('src', 'You', auto.filter((t) => t.grp === 'source' && !isViaTag(t.tag) && !isListTag(t.tag)));
  h += tagSection('other', 'Other', auto.filter((t) => !['source', ...GROUPS.map((g) => g[0])].includes(t.grp)));
  const el = $('#filters');
  const st = el.scrollTop, focus = document.activeElement?.id === 'f-find', pos = focus ? document.activeElement.selectionStart : 0;
  el.innerHTML = h;
  el.scrollTop = st;
  if (focus) { const f = $('#f-find'); f.focus(); f.setSelectionRange(pos, pos); }
  if (S.saving && document.activeElement?.id !== 'v-name') $('#v-name')?.focus();
}
$('#filters').addEventListener('click', async (e) => {
  const t = e.target;
  const more = t.closest('[data-more]'), less = t.closest('[data-less]');
  if (more) { S.tagMore[more.dataset.more] = true; return renderFilters(); }
  if (less) { S.tagMore[less.dataset.less] = false; return renderFilters(); }
  if (t.id === 'f-reset') return clearFilters();
  if (t.id === 'v-new') { S.saving = !S.saving; return renderFilters(); }
  const vdel = t.closest('[data-vdel]');
  if (vdel) { e.stopPropagation(); return deleteView(vdel.dataset.vdel); }
  const b = t.closest('button');
  if (!b) return;
  const d = b.dataset;
  if (d.tag != null) { e.preventDefault(); return clickTag(d.tag, e); }
  if (d.view != null) { applyQuery(d.view); if (narrow()) setDrawer(false); return; }
  // Segments toggle: clicking the active option turns it off.
  if (d.status != null) S.f.status = d.status;
  else if (d.tier != null) S.f.tier = S.f.tier === d.tier ? '' : d.tier;
  else if (d.min != null) S.f.min = S.f.min === +d.min ? 0 : +d.min;
  else if (d.bio != null) S.f.bio = S.f.bio === d.bio ? '' : d.bio;
  else if (d.fol != null) { const o = FOL_OPTS.find((x) => x[0] === d.fol); const same = S.f.fmin === o[2] && S.f.fmax === o[3]; S.f.fmin = same ? null : o[2]; S.f.fmax = same ? null : o[3]; }
  else return;
  filtersChanged();
});
$('#filters').addEventListener('input', (e) => { if (e.target.id === 'f-find') { S.tagFind = e.target.value; renderFilters(); } });
$('#filters').addEventListener('keydown', (e) => {
  if (e.target.id === 'f-find' && e.key === 'Enter') {
    const first = $('#filters [data-tag]');
    if (first) clickTag(first.dataset.tag, e);
  }
  if (e.target.id === 'v-name' && e.key === 'Escape') { S.saving = false; renderFilters(); }
});
$('#filters').addEventListener('submit', (e) => {
  e.preventDefault();
  if (e.target.id === 'v-form') saveView($('#v-name').value.trim());
});
function applyQuery(qs) {
  const p = fromQuery(qs);
  S.f = p.f; S.sort = p.sort; $('#sort').value = S.sort; $('#q').value = S.f.q;
  filtersChanged();
}
async function saveView(name) {
  if (!name) return;
  const query = toQuery().toString();
  S.saving = false;
  if (S.viewsLocal) {
    S.views = [...S.views, { id: Date.now(), name, query }]; store.set('views', S.views);
  } else {
    try { await api.post('/api/views', { name, query }); } catch (e) { toast('Could not save view'); }
    await loadViews();
  }
  renderFilters(); toast('View saved');
}
async function deleteView(id) {
  const v = S.views.find((x) => String(x.id) === String(id));
  if (!v) return;
  if (S.viewsLocal) { S.views = S.views.filter((x) => x !== v); store.set('views', S.views); renderFilters(); }
  else {
    try { await api.post(`/api/views/${encodeURIComponent(id)}/delete`); } catch (e) { toast('Could not delete view'); return; }
    await loadViews();
  }
  toast(`Deleted ${v.name}`, () => { S.saving = false; (S.viewsLocal ? Promise.resolve(S.views.push(v)) : api.post('/api/views', { name: v.name, query: v.query })).then(loadViews); });
}
function startSaveView() {
  if (narrow()) setDrawer(true);
  else if (!S.side) toggleSide();
  S.saving = true; renderFilters();
}
$('#save-view').onclick = startSaveView;
function setDrawer(open) { $('#filters').classList.toggle('show', open); $('#scrim').hidden = !open; }
$('#filters-btn').onclick = (e) => { e.stopPropagation(); setDrawer(!$('#filters').classList.contains('show')); };
$('#scrim').onclick = () => setDrawer(false);

// ---------- query bar ----------
function renderTokens() {
  $('#tokens').innerHTML = tokens().map((t, i) => `<span class="tok ${t.k === 'tag' ? t.mode : 'prm'}" data-t="${i}" title="${t.k === 'tag' ? 'Click: all, any, none' : 'Click to edit'}"><span>${esc(t.text)}</span><button class="x" data-rm="${i}" title="Remove">&times;</button></span>`).join('');
  if (document.activeElement !== $('#q') && $('#q').value.trim() !== S.f.q) $('#q').value = S.f.q;
}
function removeToken(t) {
  if (t.k === 'tag') setMode(t.tag, null);
  else if (t.k === 'status') S.f.status = '';
  else if (t.k === 'tier') S.f.tier = '';
  else if (t.k === 'min') S.f.min = 0;
  else if (t.k === 'bio') S.f.bio = '';
  else if (t.k === 'seed') S.f.seed = '';
  else if (t.k === 'fol') { S.f.fmin = null; S.f.fmax = null; }
}
function editToken(t) {
  removeToken(t);
  const inp = $('#q');
  inp.value = (inp.value.trim() ? inp.value.trim() + ' ' : '') + t.text;
  filtersChanged();
  inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length);
  suggest();
}
$('#qbox').addEventListener('click', (e) => {
  const all = tokens();
  const rm = e.target.closest('[data-rm]');
  if (rm) { e.stopPropagation(); removeToken(all[+rm.dataset.rm]); filtersChanged(); return; }
  const tk = e.target.closest('[data-t]');
  if (tk) {
    const t = all[+tk.dataset.t];
    if (t.k === 'tag') clickTag(t.tag, e); else editToken(t);
    return;
  }
  if (e.target.closest('#suggest')) return;
  $('#q').focus();
});
$('#qbox').addEventListener('dblclick', (e) => {
  const tk = e.target.closest('[data-t]');
  if (tk) { const t = tokens()[+tk.dataset.t]; if (t) editToken(t); }
});
const qFree = (v) => words(v).filter((w) => !TOKEN_RX.test(w)).join(' ').trim();
const qInput = debounce(() => {
  const q = qFree($('#q').value);
  if (q !== S.f.q) { S.f.q = q; filtersChanged({ push: false }); }
}, 200);
// Converts every complete token in the input into a filter. Returns true if any changed.
function commitInput(strict) {
  const inp = $('#q');
  const ws = words(inp.value);
  const keep = [];
  let changed = false;
  for (const w of ws) {
    const fn = TOKEN_RX.test(w) ? parseToken(w, strict) : null;
    if (fn) { fn(); changed = true; } else keep.push(w);
  }
  if (!changed) return false;
  inp.value = keep.join(' ') + (keep.length ? ' ' : '');
  S.f.q = qFree(inp.value);
  filtersChanged();
  return true;
}
let sugg = { items: [], i: 0 };
function curWord() { const v = $('#q').value; const ws = words(v); return /\s$/.test(v) ? '' : ws[ws.length - 1] || ''; }
function suggest() {
  const w = curWord();
  let items = [];
  let m;
  if ((m = w.match(/^([~+|-]?)#"?([^"]*)$/))) {
    const pre = m[1], q = m[2].toLowerCase();
    const mode = pre === '~' || pre === '|' ? 'any' : pre === '-' ? 'exc' : 'inc';
    items = S.tagList.filter((t) => t.grp !== 'source' || t.tag === 'knows you' || isViaTag(t.tag))
      .filter((t) => !q || t.tag.toLowerCase().includes(q))
      .sort((a, b) => (b.tag.toLowerCase().startsWith(q)) - (a.tag.toLowerCase().startsWith(q)) || (b.count > 0) - (a.count > 0) || b.count - a.count || b.total - a.total)
      .slice(0, 14).map((t) => ({ text: tagTok(t.tag, mode), kind: t.kind, src: t.grp === 'source', count: t.count, note: t.kind === 'auto' ? t.grp : t.kind }));
  } else if ((m = w.match(/^([~+|-]?)via:@?([\w.]*)$/i))) {
    const q = m[2].toLowerCase();
    items = S.tagList.filter((t) => isViaTag(t.tag) && t.tag.slice(5).includes(q)).sort((a, b) => b.count - a.count).slice(0, 14)
      .map((t) => ({ text: tagTok(t.tag, m[1] === '-' ? 'exc' : m[1] === '~' || m[1] === '|' ? 'any' : 'inc'), kind: 'auto', src: true, count: t.count, note: 'seed' }));
  } else if ((m = w.match(/^seed:@?([\w.]*)$/i))) {
    const seeds = [...new Set([...S.tagList.filter((t) => isViaTag(t.tag)).map((t) => t.tag.slice(5)), ...(S.sc?.lists || []).map((l) => l.seed)])];
    items = seeds.filter((s) => s.includes(m[1].toLowerCase())).slice(0, 14).map((s) => ({ text: 'seed:@' + s, note: 'seed' }));
  } else if (/^status:\w*$/i.test(w)) items = ['open', 'none', 'all', ...STATUSES].filter((s) => ('status:' + s).startsWith(w.toLowerCase())).map((s) => ({ text: 'status:' + s, count: s === 'open' ? S.counts?.total : S.counts?.[s] }));
  else if (/^fit:\w*$/i.test(w)) items = FITS.filter((f) => ('fit:' + f).startsWith(w.toLowerCase())).map((f) => ({ text: 'fit:' + f, count: S.counts?.[FIT_TIER[f]] }));
  else if (/^lists:\d*$/i.test(w)) items = ['2+', '3+', '4+', '5+'].map((s) => ({ text: 'lists:' + s }));
  else if (/^bio:\w*$/i.test(w)) items = ['yes', 'no'].map((s) => ({ text: 'bio:' + s }));
  else if (/^followers:\S*$/i.test(w)) items = ['<1k', '1k+', '10k+', '100k+', '1k-10k', '10k-100k'].map((s) => ({ text: 'followers:' + s }));
  else if (w.length >= 2 && /^[a-z]+:?$/i.test(w)) items = ['status:', 'fit:', 'lists:', 'bio:', 'seed:', 'followers:', 'via:@'].filter((k) => k.startsWith(w.toLowerCase())).map((k) => ({ text: k, key: true }));
  sugg = { items, i: 0 };
  const box = $('#suggest');
  if (!items.length || document.activeElement !== $('#q')) { box.hidden = true; return; }
  box.innerHTML = items.map((it, i) => `<button class="${i === 0 ? 'on' : ''}${it.count === 0 ? ' zero' : ''}" data-s="${i}" tabindex="-1">${it.kind ? swatch(it.kind, it.src ? 'src' : '') : ''}<span>${esc(it.text)}</span>${it.note ? `<em>${esc(it.note)}</em>` : ''}${it.count != null ? `<b>${fmt(it.count)}</b>` : ''}</button>`).join('');
  box.hidden = false;
}
function hideSuggest() { $('#suggest').hidden = true; sugg.items = []; }
function moveSuggest(d) {
  if (!sugg.items.length) return;
  sugg.i = (sugg.i + d + sugg.items.length) % sugg.items.length;
  $$('#suggest button').forEach((b, i) => b.classList.toggle('on', i === sugg.i));
  $$('#suggest button')[sugg.i]?.scrollIntoView({ block: 'nearest' });
}
function acceptSuggest(i = sugg.i) {
  const it = sugg.items[i];
  if (!it) return false;
  const inp = $('#q');
  const v = inp.value, w = curWord();
  inp.value = v.slice(0, v.length - w.length) + it.text + (it.key ? '' : ' ');
  if (!it.key) commitInput(false);
  inp.focus();
  suggest();
  return true;
}
$('#suggest').addEventListener('mousedown', (e) => { e.preventDefault(); const b = e.target.closest('[data-s]'); if (b) acceptSuggest(+b.dataset.s); });
$('#q').addEventListener('input', () => { suggest(); qInput(); });
$('#q').addEventListener('focus', () => { $('#qbox').classList.add('focus'); suggest(); });
$('#q').addEventListener('blur', (e) => {
  $('#qbox').classList.remove('focus'); setTimeout(hideSuggest, 100);
  // A bare "#" left from the # shortcut is not a search.
  const v = e.target.value.replace(/(^|\s)[~+|-]?#\s*$/, '').trimEnd();
  if (v !== e.target.value.trimEnd()) e.target.value = v;
});
$('#q').addEventListener('keydown', (e) => {
  const inp = e.target;
  const open = !$('#suggest').hidden && sugg.items.length;
  if (e.key === 'ArrowDown' && open) { e.preventDefault(); moveSuggest(1); return; }
  if (e.key === 'ArrowUp' && open) { e.preventDefault(); moveSuggest(-1); return; }
  if ((e.key === 'Tab' && open && !e.shiftKey) || (e.key === 'Enter' && open)) { e.preventDefault(); acceptSuggest(); return; }
  if (e.key === 'Enter') { e.preventDefault(); if (!commitInput(false)) { S.f.q = qFree(inp.value); filtersChanged({ push: false }); } if (S.view === 'leads' && S.rows.length && !e.shiftKey) { inp.blur(); select(0, true); } return; }
  if (e.key === ' ' && inp.selectionStart === inp.value.length) {
    const w = curWord();
    if (w && TOKEN_RX.test(w) && !/^[~+|-]?#"[^"]*$/.test(w) && parseToken(w, true)) { e.preventDefault(); commitInput(true); suggest(); }
    return;
  }
  if (e.key === 'Backspace' && !inp.value && inp.selectionStart === 0) {
    const all = tokens();
    if (all.length) { e.preventDefault(); editToken(all[all.length - 1]); }
    return;
  }
  if (e.key === 'Escape') { if (open) { e.stopPropagation(); hideSuggest(); } }
});
$('#sort').value = S.sort;
$('#sort').onchange = (e) => { S.sort = e.target.value; store.set('sort', S.sort); filtersChanged(); };

// ---------- leads list ----------
const lists = (r) => Math.max(0, Math.round(+(r.lists ?? (Array.isArray(r.via) ? new Set(r.via).size : 0)) || 0));
async function resetLeads(keep) {
  const gen = ++S.gen;
  if (!keep) { S.rows = []; S.total = null; S.cur = -1; $('#scroll').scrollTop = 0; }
  S.done = false; S.error = false; S.loading = false;
  renderRows();
  await loadMore(gen, keep ? Math.max(PAGE, S.rows.length) : PAGE, keep);
}
// Best fit (sort=fit): strong, good, weak, unread, each tier most connected first, then by score.
function fetchLeads(offset, limit) {
  const p = toQuery(S.f, S.sort, false);
  p.set('sort', S.sort); p.set('offset', offset); p.set('limit', limit);
  return api.get('/api/leads?' + p);
}
async function loadMore(gen = S.gen, n = PAGE, replace = false) {
  if (S.loading || (S.done && !replace)) return;
  S.loading = true;
  try {
    const d = await fetchLeads(replace ? 0 : S.rows.length, Math.min(500, n));
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
const rowH = () => parseFloat(css('--row')) || 64;
function tagChip(t, rm) {
  const k = KIND[t.source] ?? '';
  const m = modeOf(t.tag);
  const label = isViaTag(t.tag) ? t.tag.slice(4) : t.tag;
  return `<button class="tag ${k} g-${esc(t.grp || 'custom')}${t.grp === 'source' ? ' src' : ''}" data-tag="${esc(t.tag)}" title="${esc(t.tag)} · ${esc(t.source)}${m ? ' · filter ' + m : ''}"><span>${esc(label)}</span>${rm ? `<i class="x" data-rmtag="${esc(t.tag)}" title="Remove">&times;</i>` : ''}</button>`;
}
const ORDER = { manual: 0, rule: 1, auto: 2 };
const GORDER = { ai: -1, role: 0, niche: 1, signal: 2, custom: 3, size: 5, source: 6 };
// Tags that describe the person, not how they were found or what the fit badge already says.
function rowTags(r) {
  return (r.tags || []).filter((t) => t.grp !== 'source' && t.grp !== 'size' && !isFitTag(t.tag))
    .sort((a, b) => ORDER[a.source] - ORDER[b.source] || (GORDER[a.grp] ?? 4) - (GORDER[b.grp] ?? 4));
}
function whyHTML(r) {
  if (r.reason) return esc(r.reason);
  const sig = rowTags(r).filter((t) => t.grp === 'signal').slice(0, 3).map((t) => t.tag);
  return sig.length ? esc(sig.join(' · ')) : r.bio ? esc(r.bio) : '<span class="none">No bio</span>';
}
const statHTML = (s) => STATUSES.includes(s) ? `<span class="stat ${s}" title="${esc(SDESC[s])}"><i></i>${slabel(s)}</span>` : '';
const noteIcon = (note) => note ? `<span class="note-ic" title="${esc(note)}" aria-label="Has a note"><svg viewBox="0 0 16 16" width="12" height="12"><path d="M3 2.5h7l3 3v8H3z M10 2.5v3h3 M5.5 8.5h5 M5.5 11h3.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg></span>` : '';
function rowHTML(r, i, h) {
  const n = lists(r);
  const picked = S.pick.has(r.id);
  const cls = ['row', i === S.cur ? 'cur' : '', S.open === r.id ? 'open' : '', picked ? 'picked' : '', r.status === 'no' ? 'st-no' : ''].join(' ');
  const tags = rowTags(r);
  return `<div class="${cls}" data-i="${i}" style="top:${i * h}px">
    <div class="c-sel">${avatar(r.pic, r.name || r.handle)}<button class="ck${picked ? ' on' : ''}" data-ck title="Select (x)"></button></div>
    <div class="who"><div class="l1"><b>@${esc(r.handle)}</b>${noteIcon(r.note)}${r.name && r.name !== r.handle ? `<span>${esc(r.name)}</span>` : ''}</div><div class="why">${whyHTML(r)}</div></div>
    <div class="c-fit">${fitBadge(r)}</div>
    <div class="conn c-conn">${connHTML(r)}</div>
    <div class="tags c-tags">${tags.slice(0, 2).map((t) => tagChip(t)).join('')}${tags.length > 2 ? `<span class="more">+${tags.length - 2}</span>` : ''}</div>
    <span class="num r fol c-fol">${fmt(r.followers)}</span>
    <span class="c-st">${statHTML(r.status)}</span>
    <div class="mnum">${fitBadge(r)}<span>${n} ${n === 1 ? 'list' : 'lists'} · ${fmt(r.followers)}</span>${statHTML(r.status)}</div>
  </div>`;
}
function renderRows() {
  const box = $('#rows'), sc = $('#scroll'), h = rowH();
  $('#count').textContent = S.total == null ? '' : plural(S.total, 'person', 'people') + (S.pick.size ? ` · ${int(S.pick.size)} selected` : '');
  $('#pane-leads').classList.toggle('selecting', S.pick.size > 0);
  renderBulk();
  const loadedPicked = S.rows.length && S.rows.every((r) => S.pick.has(r.id));
  $('#sel-page').className = 'ck' + (loadedPicked ? ' on' : S.pick.size ? ' part' : '');
  if (!S.rows.length) {
    box.style.height = '100%';
    if (S.loading || (S.total == null && !S.error)) {
      box.innerHTML = Array.from({ length: 14 }, (_, i) => `<div class="skel" style="top:${i * h}px"><i></i><i style="width:${120 + (i * 37) % 80}px"></i><i style="width:${200 + (i * 53) % 160}px"></i></div>`).join('');
    } else if (S.error) {
      box.innerHTML = `<div class="empty"><b>${offlineSince ? 'Server offline' : 'Could not load leads'}</b><button class="btn" id="retry">Retry</button></div>`;
    } else {
      const filtered = filterCount();
      box.innerHTML = `<div class="empty"><b>${filtered ? 'No matches' : 'No leads yet'}</b>${filtered ? '<button class="btn" id="clear-all">Clear filters <kbd>c</kbd></button>' : '<a class="btn" href="#/scraper">Add seeds</a>'}</div>`;
    }
    return;
  }
  box.style.height = S.rows.length * h + 'px';
  const from = Math.max(0, Math.floor(sc.scrollTop / h) - 8);
  const to = Math.min(S.rows.length, Math.ceil((sc.scrollTop + sc.clientHeight) / h) + 8);
  let out = '';
  for (let i = from; i < to; i++) out += rowHTML(S.rows[i], i, h);
  box.innerHTML = out;
  if (!S.done && to >= S.rows.length - 20) loadMore();
}
$('#scroll').addEventListener('scroll', () => requestAnimationFrame(renderRows), { passive: true });
window.addEventListener('resize', debounce(() => { renderRows(); M.resize(); }, 60));
$('#rows').addEventListener('click', (e) => {
  if (e.target.id === 'retry') return resetLeads();
  if (e.target.closest('#clear-all')) return clearFilters();
  const row = e.target.closest('.row');
  if (!row) return;
  const i = +row.dataset.i;
  const tag = e.target.closest('[data-tag]');
  if (tag) { e.preventDefault(); return clickTag(tag.dataset.tag, e); }
  if (e.target.closest('[data-ck]') || e.target.closest('.c-sel') || e.metaKey || e.ctrlKey) { e.preventDefault(); return togglePick(i, e.shiftKey); }
  if (e.shiftKey) { e.preventDefault(); window.getSelection()?.removeAllRanges(); return rangePick(i); }
  select(i);
  openDetail(S.rows[i].id);
});
$('#sel-page').onclick = () => {
  const all = S.rows.length && S.rows.every((r) => S.pick.has(r.id));
  if (all || S.pick.size) S.pick.clear(); else S.rows.forEach((r) => S.pick.add(r.id));
  renderRows();
};

function select(i, scroll) {
  if (!S.rows.length) return;
  S.cur = Math.max(0, Math.min(S.rows.length - 1, i));
  if (scroll) {
    const sc = $('#scroll'), h = rowH(), top = S.cur * h;
    if (top < sc.scrollTop) sc.scrollTop = top;
    else if (top + h > sc.scrollTop + sc.clientHeight) sc.scrollTop = top + h - sc.clientHeight;
  }
  renderRows();
}
function togglePick(i, range) {
  const r = S.rows[i];
  if (!r) return;
  if (range && S.anchor >= 0) return rangePick(i);
  if (S.pick.has(r.id)) S.pick.delete(r.id); else S.pick.add(r.id);
  S.anchor = i; S.cur = i;
  renderRows();
}
function rangePick(i) {
  const a = S.anchor >= 0 ? S.anchor : S.cur >= 0 ? S.cur : i;
  for (let k = Math.min(a, i); k <= Math.max(a, i); k++) if (S.rows[k]) S.pick.add(S.rows[k].id);
  S.cur = i;
  renderRows();
}
async function pickAllInFilter() {
  if (S.picking) return;
  S.picking = true; renderBulk();
  const ids = S.rows.map((r) => r.id);
  const gen = S.gen;
  try {
    const total = S.total ?? 0;
    if (total > 5000) toast('Selecting the first 5,000');
    while (ids.length < Math.min(total, 5000)) {
      const d = await fetchLeads(ids.length, 500);
      if (gen !== S.gen || !d.rows.length) break;
      ids.push(...d.rows.map((r) => r.id));
    }
    if (gen === S.gen) ids.forEach((id) => S.pick.add(id));
  } catch (e) { toast('Could not select all'); }
  S.picking = false;
  renderRows();
}
function clearPick() { S.pick.clear(); S.anchor = -1; renderRows(); }

// ---------- bulk bar ----------
let bulkKey = '';
function renderBulk() {
  const el = $('#bulk');
  const n = S.pick.size;
  if (!n) { el.hidden = true; bulkKey = ''; return; }
  const key = [n, S.total, S.picking].join('|');
  if (key === bulkKey && !el.hidden) return;
  bulkKey = key;
  const focused = document.activeElement && el.contains(document.activeElement) ? document.activeElement.id : null;
  const addVal = $('#bk-add')?.value || '';
  const counts = new Map();
  S.rows.forEach((r) => { if (S.pick.has(r.id)) (r.tags || []).forEach((t) => { if (t.source !== 'auto') counts.set(t.tag, (counts.get(t.tag) || 0) + 1); }); });
  const rm = [...counts].sort((a, b) => b[1] - a[1]);
  el.innerHTML = `<b>${int(n)} selected</b>
    ${S.total > n ? `<button class="link" id="bk-all">${S.picking ? 'Selecting…' : `Select all ${int(S.total)}`}</button>` : ''}
    <form id="bk-form" style="display:contents"><input class="input" id="bk-add" list="tag-dl" placeholder="Add tag" autocomplete="off" value="${esc(addVal)}"><button class="btn">Tag</button></form>
    ${rm.length ? `<select class="select" id="bk-rm" title="Remove tag"><option value="">Remove tag</option>${rm.map(([t, c]) => `<option value="${esc(t)}">${esc(t)} (${c})</option>`).join('')}</select>` : ''}
    <span class="sep"></span>
    <span class="st-b">${STATUSES.map((s, i) => `<button data-bs="${s}" title="${esc(SDESC[s])} (${i + 1})">${slabel(s)}</button>`).join('')}<button data-bs="" title="Clear status (0)">Clear</button></span>
    <span class="grow"></span>
    <button class="btn" id="bk-x" title="Clear selection (esc)">&times;</button>`;
  el.hidden = false;
  if (focused) $('#' + focused)?.focus();
}
$('#bulk').addEventListener('click', (e) => {
  if (e.target.id === 'bk-all') return pickAllInFilter();
  if (e.target.id === 'bk-x') return clearPick();
  const s = e.target.closest('[data-bs]');
  if (s) bulk({ status: s.dataset.bs || null });
});
$('#bulk').addEventListener('submit', (e) => {
  e.preventDefault();
  const v = $('#bk-add').value.trim();
  if (v) { $('#bk-add').value = ''; $('#bk-add').blur(); bulk({ add: [v] }); }
});
$('#bulk').addEventListener('change', (e) => { if (e.target.id === 'bk-rm' && e.target.value) bulk({ remove: [e.target.value] }); });
$('#bulk').addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.target.blur(); e.stopPropagation(); } });

async function bulk(op, ids = [...S.pick], quiet) {
  if (!ids.length) return;
  const body = { ids };
  if (op.add) body.add = op.add;
  if (op.remove) body.remove = op.remove;
  if ('status' in op) body.status = op.status;
  // Remember previous status per id for undo.
  const prevStatus = new Map();
  if ('status' in op) S.rows.forEach((r) => { if (ids.includes(r.id)) prevStatus.set(r.id, r.status || null); });
  try { await api.post('/api/people/bulk', body); } catch (e) { toast('Could not save'); return; }
  const set = new Set(ids);
  S.rows.forEach((r) => {
    if (!set.has(r.id)) return;
    if ('status' in op) r.status = op.status;
    if (op.add) op.add.forEach((t) => { if (!(r.tags || []).some((x) => x.tag === t)) r.tags = [...(r.tags || []), { tag: t, grp: S.tagBy.get(t)?.grp || 'custom', source: 'manual' }]; });
    if (op.remove) r.tags = (r.tags || []).filter((x) => !op.remove.includes(x.tag) || x.source === 'auto');
  });
  if (S.person && set.has(S.person.id)) refreshPerson(S.person.id);
  bulkKey = ''; renderRows(); loadFacetsSoon(); loadCounts();
  M.patch(ids, op);
  if (quiet) return;
  const what = 'status' in op ? (op.status ? `marked ${slabel(op.status)}` : 'status cleared') : op.add ? `tagged ${op.add[0]}` : `untagged ${op.remove[0]}`;
  toast(`${ucf(plural(ids.length, 'person', 'people'))} ${what}`, () => {
    if (op.add) bulk({ remove: op.add }, ids, true);
    else if (op.remove) bulk({ add: op.remove }, ids, true);
    else {
      const by = new Map();
      prevStatus.forEach((s, id) => { const k = s || ''; by.set(k, [...(by.get(k) || []), id]); });
      by.forEach((g, s) => bulk({ status: s || null }, g, true));
    }
  });
}

// ---------- marking ----------
async function mark(id, status) {
  const r = S.rows.find((x) => x.id === id) || (S.person?.id === id ? S.person : null);
  const prev = r ? r.status : null;
  patchRow(id, { status });
  try { await api.post(`/api/person/${id}/mark`, { status }); loadCounts(); }
  catch (e) { patchRow(id, { status: prev }); toast('Could not save'); }
}
function patchRow(id, patch) {
  const r = S.rows.find((x) => x.id === id);
  if (r) Object.assign(r, patch);
  if (S.person && S.person.id === id) { Object.assign(S.person, patch); renderDetail(); }
  if ('status' in patch) M.patch([id], { status: patch.status });
  renderRows();
}
function current() { return S.open ? S.person || S.rows.find((r) => r.id === S.open) : S.rows[S.cur]; }

// ---------- detail ----------
async function openDetail(id) {
  S.open = id; S.seedCard = null;
  const base = S.rows.find((r) => r.id === id);
  const node = M.byId.get('p:' + id);
  S.person = base ? { ...base, loading: true } : node ? { id, handle: node.handle, name: node.name, followers: node.followers, lists: node.lists, status: node.status, tags: [], loading: true } : { id, loading: true, handle: '', tags: [] };
  $('#detail').hidden = false;
  renderDetail(); renderRows(); M.draw();
  await refreshPerson(id);
}
async function refreshPerson(id) {
  try {
    const p = await api.get('/api/person/' + id);
    if (S.open !== id) return;
    S.person = p;
    const r = S.rows.find((x) => x.id === id);
    if (r) Object.assign(r, { tags: p.tags, status: p.status, tier: p.tier, score: p.score, reason: p.reason });
  } catch (e) {
    if (S.open !== id || !S.person) return;
    S.person.loading = false; S.person.failed = true;
  }
  renderDetail(); renderRows();
}
function closeDetail() {
  S.open = null; S.person = null; S.seedCard = null;
  $('#detail').hidden = true;
  M.focus = null;
  renderRows(); M.resize();
}
// One row per seed; both directions read as mutual.
function seedEdges(edges) {
  const m = new Map();
  for (const e of edges) { if (!m.has(e.seed)) m.set(e.seed, new Set()); if (e.direction) m.get(e.seed).add(e.direction); }
  return [...m];
}
const modelLabel = (m) => (!m ? '' : m === 'rules' ? 'Rule-based' : String(m).split('/').pop().replace(/:free$/, ''));
const evidenceOf = (v) => (Array.isArray(v?.evidence) ? v.evidence : []).filter((q) => typeof q === 'string' && q.trim());
function renderDetail() {
  if (S.seedCard) return renderSeedCard();
  const p = S.person;
  if (!p) return;
  const v = p.verdict || {};
  const edges = p.edges || (p.via || []).map((s) => ({ seed: s }));
  const n = p.lists != null ? lists(p) : new Set(edges.map((e) => e.seed)).size;
  const tags = (p.tags || []).filter((t) => t.grp !== 'source' || t.source === 'manual' || t.tag === 'knows you')
    .sort((a, b) => ORDER[a.source] - ORDER[b.source] || (GORDER[a.grp] ?? 4) - (GORDER[b.grp] ?? 4));
  const url = safeUrl(p.website);
  const site = p.website ? String(p.website).replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, '') : '';
  const focused = document.activeElement?.id;
  const tagVal = $('#tag-in')?.value || '';
  const noteEl = $('#note'), noteVal = noteEl && +noteEl.dataset.id === p.id ? noteEl.value : p.note || '';
  const have = new Set((p.tags || []).map((t) => t.tag));
  const quick = (S.tagList || []).filter((t) => t.grp !== 'source' && !have.has(t.tag)).sort((a, b) => b.total - a.total).slice(0, 6);
  const reason = p.reason || v.reason;
  const ev = evidenceOf(v);
  const you = youLink(p);
  const role = p.role || v.role;
  $('#detail').innerHTML = `
    <div class="d-head">${avatar(p.pic, p.name || p.handle, 'lg')}
      <div class="who"><b>${esc(p.name || p.handle || '…')}</b><span>@${esc(p.handle)}${role ? ' · ' + esc(ucf(role)) : ''}</span>${p.category ? `<span>${esc(p.category)}</span>` : ''}</div>
      <button class="d-close" id="d-close" title="Close (esc)">&times;</button></div>
    <div class="d-sec d-fit">
      <div class="d-fit-h">${p.loading ? '' : fitBadge({ ...p, tier: p.tier || v.tier, score: p.score ?? v.score }, 'lg')}<span class="muted">${esc(modelLabel(v.model))}</span></div>
      <p class="d-reason${reason ? '' : ' muted'}">${reason ? esc(reason) : p.loading ? '' : 'No verdict yet'}</p>
      ${ev.length ? `<ul class="evidence">${ev.map((q) => `<li>${esc(q)}</li>`).join('')}</ul>` : ''}</div>
    <div class="d-sec"><h4>Connections<span class="grow"></span><span class="num">${n ? 'In ' + plural(n, 'list') : ''}</span></h4>
      ${you ? `<div class="you-line">${you}</div>` : ''}
      <div class="edges">${edges.length ? seedEdges(edges).map(([seed, d]) => `<button data-seed="${esc(seed)}" title="Filter by this seed"><b>@${esc(seed)}</b><span>${d.size > 1 ? 'Mutual' : d.has('following') ? 'Followed by them' : d.has('followers') ? 'Follows them' : ''}</span></button>`).join('') : '<span class="muted">–</span>'}</div></div>
    <div class="d-sec"><h4>Profile</h4><div class="d-bio${p.bio ? '' : ' muted'}">${p.bio ? esc(p.bio) : p.loading ? '' : p.failed ? 'Could not load' : 'No bio read yet'}</div>
      <div class="d-stats">
        <div><b>${fmt(p.followers)}</b><span>Followers</span></div><div><b>${fmt(p.following)}</b><span>Following</span></div><div><b>${fmt(p.posts)}</b><span>Posts</span></div></div>
      <div class="d-links">
        <a class="btn solid" href="https://www.instagram.com/${encodeURIComponent(p.handle)}/" target="_blank" rel="noopener">Instagram <kbd>o</kbd></a>
        ${url ? `<a class="btn" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(site)}</a>` : site ? `<span class="btn">${esc(site)}</span>` : ''}
        ${!p.bio && !p.loading ? '<button class="btn" id="d-read">Read bio</button>' : ''}</div></div>
    <div class="d-sec"><h4>Tags</h4><div class="d-tags">${tags.length ? tags.map((t) => tagChip(t, t.source === 'manual')).join('') : '<span class="muted">None</span>'}</div>
      <form class="tag-add" id="tag-form"><input class="input" id="tag-in" list="tag-dl" placeholder="Add tag" autocomplete="off" value="${esc(tagVal)}"><button class="btn">Add <kbd>t</kbd></button></form>
      ${quick.length ? `<div class="quick-tags">${quick.map((t) => `<button class="qt" data-addtag="${esc(t.tag)}" title="Add ${esc(t.tag)}">+ ${esc(t.tag)}</button>`).join('')}</div>` : ''}</div>
    <div class="d-sec"><h4>Status</h4><div class="marks">${STATUSES.map((s, i) => `<button data-s="${s}" class="${s}${p.status === s ? ' on' : ''}"><i></i><b>${slabel(s)}</b><span>${esc(SDESC[s])}</span><kbd>${i + 1}</kbd></button>`).join('')}</div></div>
    <div class="d-sec"><h4>Note<span class="grow"></span><span class="d-note" id="note-st">${p.note ? 'Saved' : 'Saves as you type'}</span></h4><textarea class="input" id="note" data-id="${p.id}" placeholder="Write anything: how you know them, what to pitch, when to follow up">${esc(noteVal)}</textarea></div>`;
  const sn = M.seeds?.find((x) => x.pid === p.id);
  if (sn) $('#detail').insertAdjacentHTML('beforeend', `<div class="d-seed">${seedBlock(sn)}</div>`);
  if (focused === 'tag-in') $('#tag-in').focus();
  if (focused === 'note') { const t = $('#note'); t.focus(); t.setSelectionRange(t.value.length, t.value.length); }
}
$('#detail').addEventListener('click', async (e) => {
  if (e.target.closest('#d-close')) return closeDetail();
  if (S.seedCard || e.target.closest('[data-sf],[data-only],[data-focus]')) return seedCardClick(e);
  const p = S.person;
  if (!p) return;
  const qt = e.target.closest('[data-addtag]');
  if (qt) return editTags(p.id, [qt.dataset.addtag], []);
  const rmt = e.target.closest('[data-rmtag]');
  if (rmt) { e.stopPropagation(); return editTags(p.id, [], [rmt.dataset.rmtag]); }
  const tag = e.target.closest('[data-tag]');
  if (tag) return clickTag(tag.dataset.tag, e);
  const m = e.target.closest('[data-s]');
  if (m) return mark(p.id, p.status === m.dataset.s ? null : m.dataset.s);
  const sd = e.target.closest('[data-seed]');
  if (sd) { const t = canonTag('via @' + sd.dataset.seed); if (t) clickTag(t, e); else { S.f.seed = sd.dataset.seed; filtersChanged(); } return; }
  if (e.target.id === 'd-read') {
    try { await api.post(`/api/person/${p.id}/read`); toast('Profile read queued'); } catch (err) { toast('Could not queue'); }
  }
});
$('#detail').addEventListener('submit', (e) => {
  e.preventDefault();
  const v = $('#tag-in').value.trim();
  if (v && S.person) { $('#tag-in').value = ''; editTags(S.person.id, [v], []); }
});
$('#detail').addEventListener('keydown', (e) => { if (e.key === 'Escape' && /INPUT|TEXTAREA/.test(e.target.tagName)) { e.stopPropagation(); e.target.blur(); } });
const saveNote = debounce(async (id, note) => {
  try {
    await api.post(`/api/person/${id}/mark`, { note });
    if (S.person?.id === id) { S.person.note = note; if ($('#note-st')) { $('#note-st').textContent = 'Saved'; $('#note-st').className = 'd-note ok'; } }
    const r = S.rows.find((x) => x.id === id), n = M.byId.get('p:' + id);
    if (r) { r.note = note || null; renderRows(); }
    if (n) n.note = note || null;
  } catch (e) { if ($('#note-st')) { $('#note-st').textContent = 'Not saved, check the server'; $('#note-st').className = 'd-note bad'; } }
}, 600);
$('#detail').addEventListener('input', (e) => {
  if (e.target.id === 'note' && S.person) { $('#note-st').textContent = 'Saving…'; $('#note-st').className = 'd-note'; saveNote(S.person.id, e.target.value); }
});
async function editTags(id, add, remove) {
  try { await api.post(`/api/person/${id}/tags`, { add, remove }); } catch (e) { toast('Could not save tag'); return; }
  await refreshPerson(id);
  loadFacetsSoon();
  if (add.length) toast(`Tagged ${add[0]}`, () => editTags(id, [], add));
}

// ---------- keyboard ----------
let gPending = 0;
document.addEventListener('keydown', (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if (e.key === 'Escape') {
    if (!$('#help').hidden) { $('#help').hidden = true; return; }
    if (typing) { e.target.blur(); return; }
    if ($('#filters').classList.contains('show')) { setDrawer(false); return; }
    if (S.open || S.seedCard) { closeDetail(); return; }
    if (S.view === 'map' && M.focus) { M.focus = null; M.draw(); return; }
    if (S.pick.size) { clearPick(); return; }
    if (S.view === 'leads' && S.cur >= 0) { S.cur = -1; renderRows(); }
    return;
  }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key;
  if (gPending && Date.now() - gPending < 900) {
    gPending = 0;
    const to = { l: 'leads', m: 'map', q: 'qual', t: 'tags', s: 'scraper', a: 'accounts', ',': 'settings' }[k];
    if (to) { location.hash = hashFor(to); e.preventDefault(); }
    return;
  }
  if (k === 'g') { gPending = Date.now(); return; }
  if (k === '?') { $('#help').hidden = !$('#help').hidden; return; }
  if (k === 'd') { applyDensity(document.documentElement.dataset.density === 'compact' ? 'comfortable' : 'compact'); return; }
  const work = S.view === 'leads' || S.view === 'map';
  if (k === '/' && S.view === 'tags') { e.preventDefault(); $('#tg-q').focus(); return; }
  if (!work) return;
  if (k === '/') { e.preventDefault(); $('#q').focus(); return; }
  if (k === '#') { e.preventDefault(); const q = $('#q'); q.value = (q.value.trim() ? q.value.trim() + ' ' : '') + '#'; q.focus(); suggest(); return; }
  if (k === '[') { toggleSide(); return; }
  if (k === 'c') { clearFilters(); return; }
  if (k === 'v') { e.preventDefault(); startSaveView(); return; }
  if (S.view === 'map') {
    if (k === 'f') { M.fit(); return; }
    if (k === '+' || k === '=') { M.zoomBy(1.4); return; }
    if (k === '-') { M.zoomBy(1 / 1.4); return; }
    if (k === 'l') { M.toggleLabels(); return; }
    if (k === 'n') { M.next(); return; }
    const p = S.person;
    if (p && /^[0-5]$/.test(k)) mark(p.id, k === '0' ? null : STATUSES[+k - 1]);
    if (p && k === 'm') mark(p.id, CYCLE[(CYCLE.indexOf(p.status ?? null) + 1) % CYCLE.length]);
    if (p && k === 'o') window.open(`https://www.instagram.com/${encodeURIComponent(p.handle)}/`, '_blank', 'noopener');
    if (p && k === 't') { e.preventDefault(); $('#tag-in')?.focus(); }
    return;
  }
  if (k === 'j' || k === 'ArrowDown' || k === 'k' || k === 'ArrowUp' || k === 'J' || k === 'K') {
    e.preventDefault();
    const down = k === 'j' || k === 'J' || k === 'ArrowDown';
    const from = S.cur;
    select(S.cur < 0 ? 0 : S.cur + (down ? 1 : -1), true);
    if (e.shiftKey && S.rows[S.cur]) {
      if (from >= 0 && S.rows[from]) S.pick.add(S.rows[from].id);
      S.pick.add(S.rows[S.cur].id); S.anchor = S.anchor < 0 ? from : S.anchor; renderRows();
    } else if (S.open && S.rows[S.cur]) openDetail(S.rows[S.cur].id);
    return;
  }
  if (k === 'x') { if (S.cur < 0) select(0); togglePick(S.cur); return; }
  if (k === 'A') { pickAllInFilter(); return; }
  const r = current();
  if (k === 'Enter' && S.rows[S.cur]) { openDetail(S.rows[S.cur].id); return; }
  if (S.pick.size && /^[0-5]$/.test(k)) { bulk({ status: k === '0' ? null : STATUSES[+k - 1] }); return; }
  if (S.pick.size && k === 't') { e.preventDefault(); $('#bk-add')?.focus(); return; }
  if (!r) return;
  if (k === 'm') { mark(r.id, CYCLE[(CYCLE.indexOf(r.status ?? null) + 1) % CYCLE.length]); return; }
  if (/^[0-5]$/.test(k)) { mark(r.id, k === '0' ? null : STATUSES[+k - 1]); return; }
  if (k === 'o') { window.open(`https://www.instagram.com/${encodeURIComponent(r.handle)}/`, '_blank', 'noopener'); return; }
  if (k === 't') { e.preventDefault(); if (S.open !== r.id) openDetail(r.id).then(() => $('#tag-in')?.focus()); else $('#tag-in')?.focus(); }
});
$('#help-btn').onclick = () => { $('#help').hidden = false; };
$('#help').onclick = () => { $('#help').hidden = true; };

// ---------- tags manager ----------
const T = {
  list: [], rules: [], src: '', q: '', checked: new Set(), editing: null, confirm: null,
  async show() { await Promise.all([this.load(), this.loadRules()]); },
  async load() {
    try { this.list = mergeTags(await api.get('/api/tags')); this.failed = false; } catch (e) { this.failed = !this.list.length; }
    this.render();
  },
  async loadRules() {
    try { this.rules = await api.get('/api/tag-rules'); this.rulesErr = null; } catch (e) { this.rulesErr = e.status === 404 ? 'missing' : 'failed'; if (this.rulesErr === 'missing') this.rules = null; }
    this.renderRules();
  },
  // Rename, merge and delete act on manual tags only. Rule tags follow their rule; auto tags follow the qualifier.
  editable: (t) => t.sources.includes('manual'),
  render() {
    const q = this.q.toLowerCase();
    this.renderGroups(q);
    const rows = this.list.filter((t) => this.editable(t) && (!q || t.tag.toLowerCase().includes(q)))
      .sort((a, b) => b.total - a.total || a.tag.localeCompare(b.tag));
    $('#tg-n').textContent = int(rows.length);
    const ed = this.editing;
    $('#tg-body').innerHTML = rows.length ? rows.map((t) => {
      const can = this.editable(t);
      const on = this.checked.has(t.tag);
      const label = `<button class="tag ${KIND[t.kind]} g-${esc(t.grp || 'custom')}${t.grp === 'source' ? ' src' : ''}" data-go="${esc(t.tag)}" title="Show leads with this tag"><span>${esc(t.tag)}</span></button>`;
      const name = ed === t.tag ? `<form class="ren" data-ren="${esc(t.tag)}"><input class="input" id="ren-in" value="${esc(t.tag)}" autocomplete="off" spellcheck="false"><button class="btn solid" id="ren-go">Rename</button><button type="button" class="btn" data-cancel>Cancel</button></form>` : label;
      return `<tr class="${on ? 'on' : ''}${t.total ? '' : ' dim'}">
        <td class="c-ck">${can ? `<button class="ck${on ? ' on' : ''}" data-ck="${esc(t.tag)}"></button>` : ''}</td>
        <td>${name}</td>
        <td class="r num">${int(t.total)}</td>
        <td class="r">${can && ed !== t.tag ? `<span class="acts"><button data-edit="${esc(t.tag)}">Rename</button><button data-del="${esc(t.tag)}" class="${this.confirm === t.tag ? 'warn' : ''}">${this.confirm === t.tag ? 'Confirm' : 'Delete'}</button></span>` : ''}</td></tr>`;
    }).join('') : `<tr><td colspan="4" class="muted">${this.failed ? 'Could not load tags' : q ? 'No match' : 'None yet. Open a person and type a tag under their profile.'}</td></tr>`;
    const allOn = rows.filter((t) => this.editable(t)).every((t) => this.checked.has(t.tag)) && this.checked.size;
    $('#tg-all').className = 'ck' + (allOn ? ' on' : this.checked.size ? ' part' : '');
    if (ed) { const i = $('#ren-in'); if (i && document.activeElement !== i) { i.focus(); i.select(); } this.syncRen(); }
    this.renderMerge();
  },
  // Overview: every automatic tag, grouped in plain words, with how many people have it. Click = filter Leads.
  renderGroups(q) {
    const auto = this.list.filter((t) => t.kind === 'auto' && (!q || t.tag.toLowerCase().includes(q)));
    const kinds = [
      ['AI', this.list.filter((t) => t.grp === 'ai').length, 'Added only after the AI read the full profile. Most reliable.'],
      ['Automatic', this.list.filter((t) => t.kind === 'auto' && t.grp !== 'ai').length, 'Added by fixed keyword checks on name, bio and link.'],
      ['Keyword rules', this.list.filter((t) => t.kind === 'rule').length, 'Your own word rules, set up at the bottom.'],
      ['Yours', this.list.filter((t) => t.kind === 'manual').length, 'Tags you typed on a person yourself.'],
    ];
    $('#tg-kinds').innerHTML = kinds.map(([k, n, d]) => `<div class="tg-kind"><b>${k}</b><span class="num">${plural(n, 'tag')}</span><p class="muted">${d}</p></div>`).join('');
    const G = [
      ['ai', 'AI verdict', 'What the AI concluded after reading the profile: product type, stage, ads, US market, decision maker.'],
      ['role', 'What they are', 'Brand, store, agency, creator and so on.'],
      ['niche', 'What they sell', 'Product category, from their bio and link.'],
      ['signal', 'Signals in their profile', 'Founder, hiring, shop link, country and similar hints.'],
      ['size', 'Audience size', 'Follower count bands.'],
      ['via', 'Where we found them', 'The account whose follower or following list they came from.'],
      ['source', 'Link to you', 'In several lists, follows you, you follow them.'],
    ];
    const pick = (g) => g === 'via' ? auto.filter((t) => isViaTag(t.tag)) : g === 'source' ? auto.filter((t) => t.grp === 'source' && !isViaTag(t.tag))
      : auto.filter((t) => (t.grp || 'custom') === g);
    const known = new Set(['ai', 'role', 'niche', 'signal', 'size', 'source']);
    const other = auto.filter((t) => !known.has(t.grp));
    const card = (key, title, desc, list) => {
      if (!list.length && key !== 'ai') return '';
      list = [...list].sort((a, b) => b.total - a.total || a.tag.localeCompare(b.tag));
      const lim = this.more?.[key] || q ? 400 : 14;
      return `<div class="panel tg-card${key === 'ai' ? ' ai' : ''}"><div class="tg-ch"><h3>${esc(title)}</h3><span class="num muted">${list.length}</span></div><p class="muted">${esc(desc)}</p>
        <div class="tg-chips">${list.length ? list.slice(0, lim).map((t) => `<button class="tchip g-${esc(t.grp || 'custom')}" data-go="${esc(t.tag)}" title="Show the ${int(t.total)} people tagged ${esc(t.tag)}"><span>${esc(isViaTag(t.tag) ? t.tag.slice(4) : t.tag)}</span><b class="num">${fmt(t.total)}</b></button>`).join('')
          : '<span class="muted">None yet. These appear once AI scoring (Qualify) has checked people.</span>'}
        ${list.length > lim ? `<button class="tchip more" data-tmore="${key}">+${list.length - lim} more</button>` : ''}</div></div>`;
    };
    $('#tg-groups').innerHTML = G.map(([k, t, d]) => card(k, t, d, pick(k))).join('') + card('other', 'Other', 'Automatic tags outside the groups above.', other);
  },
  syncRen() {
    const i = $('#ren-in'); if (!i) return;
    const v = i.value.trim();
    const exists = v && v !== this.editing && this.list.some((t) => t.tag.toLowerCase() === v.toLowerCase());
    $('#ren-go').textContent = exists ? 'Merge' : 'Rename';
  },
  renderMerge() {
    const el = $('#tg-merge');
    const n = this.checked.size;
    if (n < 1) { el.hidden = true; return; }
    const tags = [...this.checked];
    const best = tags.map((t) => this.list.find((x) => x.tag === t)).filter(Boolean).sort((a, b) => b.total - a.total)[0];
    const prev = $('#mg-to')?.value;
    el.innerHTML = `<b>${plural(n, 'tag')}</b>${n > 1 ? `<span>Merge into</span><input class="input" id="mg-to" list="tag-dl" value="${esc(prev || best?.tag || '')}" autocomplete="off"><button class="btn" id="mg-go">Merge</button>` : ''}
      <button class="btn" id="mg-del">${n > 1 ? 'Delete all' : 'Delete'}</button><span class="grow"></span><button class="btn" id="mg-x">Done</button>`;
    el.hidden = false;
  },
  async rename(from, to) {
    to = to.trim();
    if (!to || to === from) { this.editing = null; this.render(); return; }
    const canon = this.list.find((t) => t.tag.toLowerCase() === to.toLowerCase());
    if (canon) to = canon.tag;
    try { await api.post('/api/tags/rename', { from, to }); } catch (e) { toast('Could not rename'); return; }
    this.fixFilter(from, to);
    this.editing = null;
    toast(canon ? `Merged ${from} into ${to}` : `Renamed to ${to}`);
    this.after();
  },
  async remove(tags) {
    for (const tag of tags) {
      try { await api.post('/api/tags/delete', { tag }); } catch (e) { toast('Could not delete ' + tag); return; }
      this.fixFilter(tag, null);
      this.checked.delete(tag);
    }
    this.confirm = null;
    toast(tags.length === 1 ? `Deleted ${tags[0]}` : `Deleted ${tags.length} tags`);
    this.after();
  },
  async merge(tags, to) {
    to = to.trim();
    if (!to) return;
    for (const t of tags) if (t !== to) {
      try { await api.post('/api/tags/rename', { from: t, to }); } catch (e) { toast('Could not merge ' + t); return; }
      this.fixFilter(t, to);
    }
    this.checked.clear();
    toast(`Merged into ${to}`);
    this.after();
  },
  fixFilter(from, to) {
    for (const k of ['tags', 'any', 'not']) S.f[k] = S.f[k].map((t) => t === from ? to : t).filter(Boolean);
  },
  after() { this.load(); this.loadRules(); loadFacets(); M.stale = true; resetLeads(true); },
  renderRules() {
    const rs = this.rules;
    $('#rl-n').textContent = rs ? int(rs.length) : '';
    $('#rl-body').innerHTML = this.rulesErr === 'failed' && !rs?.length ? `<tr><td colspan="5" class="muted">Could not load rules</td></tr>` : rs == null ? `<tr><td colspan="5" class="muted">Rules not available on this server</td></tr>`
      : rs.length ? rs.map((r) => `<tr><td><button class="tag rule" data-go="${esc(r.tag)}"><span>${esc(r.tag)}</span></button></td><td class="mono hide-sm">${esc(ucf(r.field))}</td>
        <td class="mono">"${esc(r.match)}"</td><td class="r num">${int(r.hits)}</td><td class="r"><button class="rl-del" data-rdel="${esc(r.id)}">Delete</button></td></tr>`).join('')
        : `<tr><td colspan="5" class="muted">No rules</td></tr>`;
  },
};
$('#tg-q').addEventListener('input', (e) => { T.q = e.target.value; T.render(); });
$('#tg-all').onclick = () => {
  const q = T.q.toLowerCase();
  const rows = T.list.filter((t) => T.editable(t) && (!q || t.tag.toLowerCase().includes(q)));
  if (T.checked.size) T.checked.clear(); else rows.forEach((t) => T.checked.add(t.tag));
  T.render();
};
function goTag(tag) { S.f = emptyFilter(); S.f.tags = [tag]; $('#q').value = ''; S.view = 'leads'; filtersChanged(); setView('leads'); }
$('#view-tags').addEventListener('click', (e) => {
  const go = e.target.closest('[data-go]');
  if (go) return goTag(go.dataset.go);
  const tm = e.target.closest('[data-tmore]');
  if (tm) { T.more = Object.assign(T.more || {}, { [tm.dataset.tmore]: true }); return T.render(); }
  const ck = e.target.closest('[data-ck]');
  if (ck) { const t = ck.dataset.ck; if (T.checked.has(t)) T.checked.delete(t); else T.checked.add(t); return T.render(); }
  const ed = e.target.closest('[data-edit]');
  if (ed) { T.editing = ed.dataset.edit; T.confirm = null; return T.render(); }
  if (e.target.closest('[data-cancel]')) { T.editing = null; return T.render(); }
  const del = e.target.closest('[data-del]');
  if (del) {
    const t = del.dataset.del;
    if (T.confirm === t) return T.remove([t]);
    T.confirm = t; T.render();
    setTimeout(() => { if (T.confirm === t) { T.confirm = null; T.render(); } }, 3000);
    return;
  }
  if (e.target.id === 'mg-go') return T.merge([...T.checked], $('#mg-to').value);
  if (e.target.id === 'mg-del') {
    const b = e.target;
    if (b.dataset.sure) return T.remove([...T.checked]);
    b.dataset.sure = '1'; b.textContent = 'Confirm delete'; b.classList.add('solid');
    return;
  }
  if (e.target.id === 'mg-x') { T.checked.clear(); return T.render(); }
  const rd = e.target.closest('[data-rdel]');
  if (rd) return deleteRule(rd.dataset.rdel);
});
$('#view-tags').addEventListener('submit', (e) => {
  const f = e.target.closest('[data-ren]');
  if (f) { e.preventDefault(); T.rename(f.dataset.ren, $('#ren-in').value); }
});
$('#view-tags').addEventListener('input', (e) => { if (e.target.id === 'ren-in') T.syncRen(); });
$('#view-tags').addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && e.target.id === 'ren-in') { e.stopPropagation(); T.editing = null; T.render(); }
  if (e.key === 'Enter' && e.target.id === 'mg-to') { e.preventDefault(); T.merge([...T.checked], e.target.value); }
});

// Rules: live preview of hits.
let prevGen = 0;
const previewRule = debounce(async () => {
  const field = $('#rl-field').value, match = $('#rl-match').value.trim(), tag = $('#rl-tag').value.trim();
  $('#rl-add').disabled = !(tag && match);
  const g = ++prevGen;
  $('#rl-prev').classList.remove('bad');
  if (!match) { $('#rl-prev').textContent = ''; return; }
  let txt = '';
  $('#rl-prev').title = '';
  try {
    if (T.noPreview) throw new Error('no preview');
    const d = await api.get(`/api/tag-rules/preview?field=${encodeURIComponent(field)}&match=${encodeURIComponent(match)}`);
    txt = plural(d.hits, 'hit');
  } catch (e) {
    if (e.status === 400) { if (g === prevGen) { $('#rl-prev').textContent = 'Invalid'; $('#rl-prev').title = e.message; $('#rl-prev').classList.add('bad'); $('#rl-add').disabled = true; } return; }
    if (e.status === 404) T.noPreview = true;
    // Fallback: plain search covers handle, name and bio.
    if (['bio', 'name', 'handle', 'any'].includes(field)) {
      try { const d = await api.get('/api/leads?limit=1&q=' + encodeURIComponent(match)); txt = '~' + plural(d.total, 'hit'); } catch (err) { txt = ''; }
    }
  }
  if (g === prevGen) $('#rl-prev').textContent = txt;
}, 220);
['rl-tag', 'rl-match'].forEach((id) => $('#' + id).addEventListener('input', previewRule));
$('#rl-field').addEventListener('change', previewRule);
$('#rule-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const tag = $('#rl-tag').value.trim(), field = $('#rl-field').value, match = $('#rl-match').value.trim();
  if (!tag || !match) return;
  try {
    const r = await api.post('/api/tag-rules', { tag, field, match });
    toast(`Rule added${r.hits != null ? ', ' + plural(r.hits, 'hit') : ''}`);
    $('#rl-match').value = ''; $('#rl-prev').textContent = ''; $('#rl-add').disabled = true;
  } catch (err) { toast(err.status === 400 ? ucf(err.message) : 'Could not add rule'); return; }
  T.after();
});
async function deleteRule(id) {
  const r = (T.rules || []).find((x) => String(x.id) === String(id));
  try { await api.post(`/api/tag-rules/${encodeURIComponent(id)}/delete`); } catch (e) { toast('Could not delete rule'); return; }
  toast(r ? `Deleted rule ${r.tag}` : 'Rule deleted', r ? () => api.post('/api/tag-rules', { tag: r.tag, field: r.field, match: r.match }).then(() => T.after()) : null);
  T.after();
}

// ---------- scraper ----------
function scState() {
  const sc = S.sc;
  if (!sc) return { label: 'Connecting', short: 'Connecting', dot: '' };
  const x = sc.ext || {};
  if (sc.paused) return { label: 'Paused', short: 'Paused', dot: '' };
  if (!x.online) return { label: 'Extension offline', short: 'Offline', dot: 'hollow' };
  if (x.state === 'paused') return { label: 'Paused in extension', short: 'Paused', dot: '' };
  if (x.cooldown_until && Date.parse(x.cooldown_until) > Date.now()) return { label: 'Cooldown ' + left(x.cooldown_until), short: 'Cooldown', dot: 'hollow' };
  if (x.state === 'running') return { label: 'Running', short: 'Running', dot: 'live run' };
  return { label: 'Idle', short: 'Idle', dot: 'live' };
}
function renderStatus() {
  const sc = S.sc, x = sc?.ext || {}, st = scState();
  $('#st-dot').className = 'dot ' + st.dot;
  $('#st-label').textContent = window.innerWidth <= 640 ? st.short : st.label;
  const run = sc?.lists?.find((l) => l.state === 'running');
  $('#st-act').textContent = x.last_error && !x.online ? x.last_error : x.activity || (run ? `@${run.seed} ${run.direction}` : x.text || '');
  const t = x.today?.list, b = x.budget?.list;
  $('#st-today').textContent = t == null ? '–' : `${int(t)}/${b == null ? '–' : int(b)}`;
  $('#st-meter').style.width = t != null && b ? Math.min(100, (t / b) * 100) + '%' : '0';
  $('#st-pph').textContent = x.rate?.pages_hour != null ? int(Math.round(x.rate.pages_hour)) : '–';
  $('#st-peh').textContent = x.rate?.people_hour != null ? int(Math.round(x.rate.people_hour)) : '–';
  $('#st-hit').textContent = x.rate?.last_hit_at ? ago(x.rate.last_hit_at) + ' ago' : 'None';
  $('#pause-btn').textContent = sc?.paused ? 'Resume' : 'Pause';
  $('#pause-btn').classList.toggle('solid', !!sc?.paused);
  $('#qualify-btn').classList.toggle('on', !!sc?.qualify);
  const q = sc ? (sc.queue?.list || 0) + (sc.queue?.profile || 0) : 0;
  $('#n-queue').textContent = q ? fmt(q) : '';
  // one dot per account; the label counts the ones working
  const accs = sc?.accounts || [];
  const lanes = $('#st-lanes');
  lanes.hidden = accs.length < 2;
  if (accs.length > 1) {
    const html = accs.map((a) => `<i class="dot ${ST_DOT[a.status] || ''}" title="${esc(a.name)} · ${esc(ST_LABEL[a.status] || a.status)}"></i>`).join('');
    if (lanes.innerHTML !== html) lanes.innerHTML = html;
    const busy = accs.filter((a) => a.status === 'running').length;
    if (st.label === 'Running') $('#st-label').textContent = `Running · ${busy}/${accs.length}`;
  }
  const alerts = (sc?.alerts || []).filter((x) => x.level === 'error').length;
  $('#n-acc').textContent = alerts ? '!' + alerts : accs.length > 1 ? String(accs.length) : '';
}
async function loadScraper() {
  try { S.sc = await api.get('/api/scraper'); } catch (e) { /* keep last */ }
  renderStatus();
  if (S.view === 'scraper') renderScraper();
  if (S.view === 'accounts') renderAccounts();
  if (S.view === 'settings') renderSettings();
  if (S.view === 'qual') Q.renderProg();
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
const LIST_STATE = { running: 'Reading now', queued: 'Waiting', paused: 'Paused', error: 'Failed', private: 'Private account', done: 'Done' };
function eta(h) {
  if (h == null) return null;
  if (h <= 0) return 'done';
  const m = Math.round(h * 60);
  if (m < 60) return `about ${Math.max(1, m)} min`;
  if (h < 48) return `about ${Math.round(h)} h`;
  return `about ${Math.round(h / 24)} days`;
}
function renderScraper() {
  const sc = S.sc;
  if (!sc) { $('#stages').innerHTML = '<div class="muted">Loading</div>'; return; }
  const x = sc.ext || {}, ls = sc.lists || [], pr = sc.progress || {};
  const run = ls.find((l) => l.state === 'running');
  // One plain sentence: what is happening right now.
  let now;
  if (sc.paused) now = 'Paused. Press Resume at the top to continue.';
  else if (!x.online) now = `The Chrome extension is not connected${x.last_seen ? ' (last seen ' + ago(x.last_seen) + ' ago)' : ''}. Open Chrome with Instagram logged in.`;
  else if (x.cooldown_until && Date.parse(x.cooldown_until) > Date.now()) now = `Taking a break so Instagram does not block you, back in ${left(x.cooldown_until)}.`;
  else if (run) now = `Reading @${run.seed}'s ${run.direction === 'followers' ? 'followers' : 'following list'}: ${int(run.received)}${run.total ? ' of ' + int(run.total) : ''}.`;
  else now = ucf(x.activity || x.text || 'Idle');
  const accs = sc.accounts || [];
  const conn = accs.length ? accs.map((a) => `<span class="cpill" title="${esc(ST_LABEL[a.status] || a.status)}"><i class="dot ${ST_DOT[a.status] || ''}"></i>${esc(a.name || a.handle || a.lane_id)}</span>`).join('')
    : `<span class="cpill"><i class="dot ${x.online ? 'live' : 'off'}"></i>Extension ${x.online ? 'connected' : 'not connected'}</span>`;
  $('#now').innerHTML = `<div class="now-line"><i class="dot ${x.online && !sc.paused ? 'live run' : 'off'}"></i><span>${esc(now)}</span></div><div class="conns">${conn}</div>`;
  const L = pr.lists || {}, B = pr.bios || {}, Q = pr.qualify || {};
  const recv = ls.reduce((a, l) => a + (l.received || 0), 0), tot = recv + (L.left || 0);
  const stage = (title, line, pct, when) => `<div class="stg"><div class="st-top"><b>${title}</b><span class="muted">${when || ''}</span></div>
    <div class="bar-p ${pct >= 100 ? 'done' : 'run'}"><i style="width:${Math.min(100, pct || 0)}%"></i></div><div class="muted">${line}</div></div>`;
  const offline = x.online ? '' : 'waiting for the extension';
  $('#stages').innerHTML = [
    stage('1. Collect lists', `${int(recv)} people collected, ${int(L.left || 0)} still to go${L.per_hour ? ` · ${int(Math.round(L.per_hour))} per hour` : ''}`,
      tot ? (recv / tot) * 100 : 0, !L.left ? 'Done' : offline || (eta(L.eta_h) ? eta(L.eta_h) + ' left' : '')),
    stage('2. Read bios', `${int(B.left || 0)} bios waiting · limit ${int(B.per_day || 0)} a day to stay safe`,
      B.left ? 0 : 100, !B.left ? 'Nothing waiting' : offline || (eta(B.eta_h) ? eta(B.eta_h) + ' left' : '')),
    stage('3. AI scoring', Q.on ? `${int(Q.left || 0)} people to score · ${Q.keys || 0} OpenRouter keys, ${Q.workers || 0} at a time${Q.per_hour ? ` · ${int(Q.per_hour)} per hour` : ''}`
      : `Off. Turn on Qualify at the top to let AI score ${int(Q.left || 0)} people with bios.`,
      Q.left ? 0 : 100, !Q.on ? 'Off' : !Q.left ? 'Done' : eta(Q.eta_h) ? eta(Q.eta_h) + ' left' : 'starting'),
  ].join('');
  $('#ext-ver').textContent = x.version ? 'Extension v' + x.version : '';
  const tl = x.today?.list, bl = x.budget?.list, tp = x.today?.profile, bp = x.budget?.profile;
  $('#ext-kv').innerHTML = [
    ['Today', `${int(tl)} of ${int(bl)} list pages, ${int(tp)} of ${int(bp)} bios`],
    ['Last error', x.last_error || 'None'],
  ].map(([k, v]) => `<span>${k}</span><b>${esc(v)}</b>`).join('');
  const bL = $('#b-list'), bP = $('#b-profile');
  if (document.activeElement !== bL && document.activeElement !== bP) { bL.value = bl ?? ''; bP.value = bp ?? ''; }
  const groups = { all: ls, active: ls.filter((l) => l.state === 'running' || l.state === 'queued'), done: ls.filter((l) => l.state === 'done'), issues: ls.filter((l) => ['error', 'private', 'paused'].includes(l.state)) };
  $('#lists-f').innerHTML = Object.entries(groups).map(([k, v]) => `<button data-v="${k}" class="${listFilter === k ? 'on' : ''}">${ucf(k)} <span class="num">${v.length}</span></button>`).join('');
  const order = { running: 0, queued: 1, paused: 2, error: 3, private: 4, done: 5 };
  const rows = [...groups[listFilter]].sort((a, b) => (order[a.state] ?? 9) - (order[b.state] ?? 9) || (b.updated_at || '').localeCompare(a.updated_at || ''));
  $('#lists-body').innerHTML = rows.length ? rows.map((l) => {
    const pct = l.total ? Math.min(100, (l.received / l.total) * 100) : null;
    return `<tr><td><b>@${esc(l.seed)}</b></td><td class="hide-sm muted">${l.direction === 'followers' ? 'Their followers' : 'Who they follow'}</td>
      <td class="prog"><div class="bar-p ${pct == null ? 'unknown' : l.state === 'done' ? 'done' : l.state === 'running' ? 'run' : ''}"><i style="width:${pct ?? 0}%"></i></div></td>
      <td class="r num">${int(l.received)}${l.total ? ' of ' + int(l.total) : ''}</td>
      <td><span class="state ${esc(l.state)}" title="${esc(l.error || '')}">${l.state === 'running' ? '<i class="dot run"></i>' : ''}${esc(LIST_STATE[l.state] || ucf(l.state))}</span></td></tr>`;
  }).join('') : `<tr><td colspan="5" class="muted">No lists yet. Add an account above.</td></tr>`;
}
$('#lists-f').addEventListener('click', (e) => { const b = e.target.closest('[data-v]'); if (b) { listFilter = b.dataset.v; renderScraper(); } });
$('#budget').addEventListener('submit', async (e) => {
  e.preventDefault();
  const val = (el) => { const v = el.value.trim(); return /^\d+$/.test(v) ? +v : null; };
  const list = val($('#b-list')), profile = val($('#b-profile'));
  if (list == null || profile == null) { toast('Budgets must be whole numbers, 0 or more'); return; }
  try { await api.post('/api/scraper/budget', { list, profile }); toast('Budget saved'); $('#b-save').blur(); loadScraper(); } catch (err) { toast('Could not save'); }
});
$('#snowball').onclick = async () => {
  try {
    const r = await api.post('/api/scraper/snowball', { min_status: 'interested' });
    toast(r.queued ? `Queued who ${plural(r.queued, 'Interested lead')} follow${r.queued === 1 ? 's' : ''}` : 'Nothing new: every Interested, Talking or Client lead is already done or private');
    loadScraper();
  } catch (e) { toast(e.status === 400 ? ucf(e.message) : 'Could not queue'); }
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
  $('#seed-n').textContent = n ? plural(n, 'account') : '';
  $('#seed-add').disabled = !n || !seedDirs().length;
}
$('#seed-in').addEventListener('input', syncSeed);
$('#seed-dir').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) { b.classList.toggle('on'); syncSeed(); } });
$('#seed-add').onclick = async () => {
  const handles = parseHandles($('#seed-in').value);
  if (!handles.length) return;
  try {
    const r = await api.post('/api/scraper/seeds', { handles, directions: seedDirs() });
    toast(r.queued != null ? (r.queued ? `${ucf(plural(r.queued, 'list'))} queued` : 'Already queued') : 'Queued');
    $('#seed-in').value = ''; syncSeed(); loadScraper();
  } catch (e) { toast('Could not queue'); }
};

// ---------- accounts (one Chrome profile + extension + Instagram account each) ----------
const ST_LABEL = { running: 'Running', online: 'Online', cooldown: 'Cooldown', needs_login: 'Needs login', challenge: 'Security check', offline: 'Offline', paused: 'Paused' };
const ST_DOT = { running: 'live run', online: 'live', cooldown: 'hollow', needs_login: 'need', challenge: 'need', offline: 'off', paused: '' };
const ROLES = [['lists', 'Lists'], ['bios', 'Bios'], ['both', 'Both']];
const A = { wiz: null, setup: null, confirm: null, renaming: null, busy: new Set(), dismissed: false };

async function copyText(text, btn) {
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch (e) {
    const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
    try { ok = document.execCommand('copy'); } catch (e2) { /* no clipboard */ } ta.remove();
  }
  if (btn) { btn.textContent = ok ? 'Copied' : 'Copy failed'; setTimeout(() => { btn.textContent = 'Copy'; }, 1400); }
}
const copyRow = (text, label) => `<div class="copy"><code title="${esc(text)}">${esc(label || text)}</code><button class="btn" data-copy="${esc(text)}">Copy</button></div>`;

function jobText(a) {
  if (a.job) return a.job.kind === 'list' ? `@${a.job.seed} ${a.job.direction}` : `Bio of @${a.job.handle}`;
  if (a.status === 'needs_login') return 'Waiting for a login';
  if (a.status === 'challenge') return 'Waiting for the security check';
  if (a.status === 'offline') return 'Last seen ' + ago(a.last_seen) + ' ago';
  return ucf(a.activity || a.text || 'Idle');
}
function stateText(a) {
  const base = ST_LABEL[a.status] || ucf(a.status);
  return a.status === 'cooldown' && a.cooldown_until ? `${base} ${left(a.cooldown_until)}` : base;
}
function accountRow(a) {
  const b = a.budget || {}, t = a.today || {}, h = a.hour || {};
  const conf = A.confirm === a.lane_id, warn = a.status === 'needs_login' || a.status === 'challenge';
  const stat = (label, val, sub) => `<div><span>${label}</span><b class="num">${val}</b>${sub ? `<small>${sub}</small>` : ''}</div>`;
  const name = A.renaming === a.lane_id
    ? `<form class="acc-ren" data-ren><input class="input" id="acc-label" value="${esc(a.label || '')}" placeholder="Label, e.g. Scout 2" maxlength="40" autocomplete="off"><button class="btn solid">Save</button><button type="button" class="btn" data-ren-x>Cancel</button></form>`
    : `<b class="acc-name">${esc(a.handle ? '@' + a.handle : a.label || a.name)}</b>${a.handle && a.label ? `<span class="muted acc-label">${esc(a.label)}</span>` : ''}<button class="btn ghost acc-edit" data-rename title="Rename">Rename</button>`;
  return `<section class="acc${warn ? ' warn' : ''}" data-lane="${esc(a.lane_id)}">
    <div class="acc-top"><i class="dot ${ST_DOT[a.status] || ''}"></i>${name}${a.is_main ? '<span class="pill">Main</span>' : ''}
      <span class="grow"></span><span class="acc-state${warn ? ' bad' : ''}">${esc(stateText(a))}</span></div>
    <div class="acc-now"><span class="muted">Now</span><span title="${esc(jobText(a))}">${esc(jobText(a))}</span></div>
    <div class="acc-stats">
      ${stat('Pages/hour', int(h.pages))}
      ${stat('People/hour', int(h.people))}
      ${stat('Pages today', int(t.list), 'of ' + int(b.list))}
      ${stat('Bios today', int(t.profile), b.profile ? 'of ' + int(b.profile) : 'no daily cap')}
      ${stat('Last limit', a.last_limit ? ago(a.last_limit) + ' ago' : 'None')}
    </div>
    <div class="acc-ctl">
      <div class="seg" title="What this account collects">${ROLES.map(([v, l]) => `<button data-role="${v}" class="${a.role === v ? 'on' : ''}">${l}</button>`).join('')}</div>
      <button class="toggle${a.is_main ? ' on' : ''}" data-main title="Your own account: bios only, unless Settings gives it a share of the lists"><i></i><span>Main account</span></button>
      <span class="grow"></span>
      <form class="acc-bud" data-bud>
        <label><input class="input" type="number" min="0" max="3000" data-b="list" value="${a.budget_custom ? esc(b.list) : ''}" placeholder="${esc(b.list)}" inputmode="numeric"><span class="muted">pages/day</span></label>
        <label><input class="input" type="number" min="0" max="5000" data-b="profile" value="${a.budget_custom ? esc(b.profile) : ''}" placeholder="${esc(b.profile)}" inputmode="numeric"><span class="muted">bios/day</span></label>
        <button class="btn">Save</button>
      </form>
    </div>
    <div class="acc-foot"><span class="muted num">${a.version ? 'v' + esc(a.version) + ' · ' : ''}Seen ${ago(a.last_seen)} ago${a.last_error && a.status !== 'running' ? ' · ' + esc(a.last_error.slice(0, 100)) : ''}</span>
      <span class="grow"></span>
      <button class="btn${a.paused ? ' solid' : ''}" data-pause>${a.paused ? 'Resume' : 'Pause'}</button>
      <button class="btn ${conf ? 'danger' : 'ghost'}" data-remove>${conf ? 'Confirm remove' : 'Remove'}</button></div>
  </section>`;
}

function renderAccounts() {
  const sc = S.sc;
  const accs = sc?.accounts || [], alerts = sc?.alerts || [];
  $('#acc-alerts').innerHTML = alerts.map((x) => `<div class="alert ${x.level}"><i></i><span>${esc(x.text)}</span></div>`).join('');
  const r = sc?.rate || {};
  const online = accs.filter((a) => a.online).length;
  const bios = accs.reduce((n, a) => n + (a.today?.profile || 0), 0), pages = accs.reduce((n, a) => n + (a.today?.list || 0), 0);
  const kpi = (label, val, sub) => `<div class="kpi"><span>${label}</span><b>${val}</b>${sub ? `<small>${sub}</small>` : ''}</div>`;
  $('#acc-kpis').innerHTML = [
    kpi('Online', `${online}/${accs.length}`, accs.length ? plural(accs.filter((a) => a.status === 'running').length, 'working', 'working') : 'No accounts yet'),
    kpi('Pages/hour', int(r.pages_last_hour), 'All accounts'),
    kpi('People/hour', int(r.people_last_hour), r.last_hit_at ? 'Limit ' + ago(r.last_hit_at) + ' ago' : 'No limit hits'),
    kpi('Today', int(pages), plural(bios, 'bio') + ' read'),
  ].join('');
  $('#acc-n').textContent = accs.length ? int(accs.length) : '';
  if (!accs.length && !A.wiz && !A.dismissed && sc) openWizard();
  const list = $('#acc-list');
  if (list.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return; // don't clobber typing
  list.innerHTML = accs.length ? accs.map(accountRow).join('') : `<div class="acc-empty muted">${A.wiz ? 'Follow the steps above; the account shows up here once its extension checks in.' : 'No account has checked in yet.'}</div>`;
  if (A.renaming) { const i = $('#acc-label'); if (i && document.activeElement !== i) { i.focus(); i.select(); } }
  if (A.wiz) renderWizard();
}

async function editAccount(lane, body, msg) {
  if (A.busy.has(lane)) return;
  A.busy.add(lane);
  const a = S.sc?.accounts?.find((x) => x.lane_id === lane);
  try {
    const r = await api.post(`/api/accounts/${encodeURIComponent(lane)}`, body);
    if (a && r.account) Object.assign(a, r.account);
    renderAccounts(); renderStatus();
    if (msg) toast(msg);
  } catch (e) { toast(e.status === 400 ? ucf(e.message) : 'Could not save'); }
  finally { A.busy.delete(lane); }
  loadScraper();
}
$('#acc-list').addEventListener('click', async (e) => {
  const row = e.target.closest('[data-lane]');
  if (!row) return;
  const lane = row.dataset.lane, a = S.sc?.accounts?.find((x) => x.lane_id === lane);
  const t = e.target.closest('button');
  if (!a || !t || t.type === 'submit' && t.closest('form')) return;
  if (!t.hasAttribute('data-remove') && A.confirm) { A.confirm = null; renderAccounts(); }
  if (t.dataset.role) return t.dataset.role !== a.role && editAccount(lane, { role: t.dataset.role }, `${a.name}: ${ROLES.find((x) => x[0] === t.dataset.role)[1].toLowerCase()}`);
  if (t.hasAttribute('data-main')) return editAccount(lane, { is_main: !a.is_main }, a.is_main ? `${a.name} is no longer the main account` : `${a.name} is the main account`);
  if (t.hasAttribute('data-pause')) return editAccount(lane, { paused: !a.paused }, a.paused ? `${a.name} resumed` : `${a.name} paused`);
  if (t.hasAttribute('data-rename')) { A.renaming = lane; A.confirm = null; return renderAccounts(); }
  if (t.hasAttribute('data-ren-x')) { A.renaming = null; return renderAccounts(); }
  if (t.hasAttribute('data-remove')) {
    if (A.confirm !== lane) {
      A.confirm = lane; renderAccounts();
      setTimeout(() => { if (A.confirm === lane) { A.confirm = null; renderAccounts(); } }, 4000);
      return;
    }
    A.confirm = null;
    try { await api.post(`/api/accounts/${encodeURIComponent(lane)}/remove`, {}); toast(`${a.name} removed`); } catch (err) { toast('Could not remove'); }
    loadScraper();
  }
});
$('#acc-list').addEventListener('submit', (e) => {
  e.preventDefault();
  const row = e.target.closest('[data-lane]'), lane = row?.dataset.lane;
  if (!lane) return;
  if (e.target.hasAttribute('data-ren')) {
    const label = $('#acc-label').value.trim();
    A.renaming = null;
    document.activeElement?.blur();
    return editAccount(lane, { label: label || null }, label ? `Renamed to ${label}` : 'Label cleared');
  }
  if (e.target.hasAttribute('data-bud')) {
    const val = (k) => { const v = row.querySelector(`[data-b="${k}"]`).value.trim(); return v === '' ? null : /^\d+$/.test(v) ? +v : NaN; };
    const list = val('list'), profile = val('profile');
    if (Number.isNaN(list) || Number.isNaN(profile)) { toast('Budgets are whole numbers, 0 or more'); return; }
    document.activeElement?.blur();
    return editAccount(lane, { budget: list == null && profile == null ? null : { list, profile } }, list == null && profile == null ? 'Using the default budget' : 'Budget saved');
  }
});
$('#acc-list').addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && e.target.id === 'acc-label') { e.stopPropagation(); A.renaming = null; renderAccounts(); }
});

// Add-account wizard: three steps, ticked as the new profile's extension checks in and reports its account.
async function openWizard() {
  const accs = S.sc?.accounts || [];
  A.wiz = { base: new Set(accs.map((a) => a.lane_id)), acct: null, poll: null };
  $('#wiz').hidden = false; $('#acc-add').hidden = true;
  if (!A.setup) { try { A.setup = await api.get('/api/setup'); } catch (e) { /* shown as unknown */ } }
  renderWizard();
  A.wiz.poll = setInterval(pollWizard, 2000);
}
function closeWizard() {
  if (A.wiz?.poll) clearInterval(A.wiz.poll);
  A.wiz = null; A.dismissed = true;
  $('#wiz').hidden = true; $('#acc-add').hidden = false;
  renderAccounts();
}
async function pollWizard() {
  if (!A.wiz) return;
  let d;
  try { d = await api.get('/api/accounts'); } catch (e) { return; }
  if (!A.wiz) return;
  const fresh = d.accounts.filter((a) => !A.wiz.base.has(a.lane_id));
  A.wiz.acct = fresh.find((a) => a.ig_id) || fresh[0] || null;
  renderWizard();
}
function renderWizard() {
  const w = A.wiz, s = A.setup || {};
  if (!w) return;
  const a = w.acct;
  const done = [!!a, !!a, !!(a && a.ig_id && !a.hold)];
  const step = (i, title, body) => `<li class="${done[i] ? 'done' : done.slice(0, i).every(Boolean) ? 'cur' : ''}">
    <span class="n">${done[i] ? '<i class="tick"></i>' : i + 1}</span><div><b>${title}</b>${body}</div></li>`;
  const who = a ? (a.ig_id ? (a.handle ? 'Connected: @' + a.handle : 'Connected: account ' + a.ig_id) : 'Extension checked in, not logged in yet') : 'Waiting for the extension';
  $('#wiz').innerHTML = `<div class="p-head"><h3>Add account</h3><span class="muted wiz-who">${esc(who)}</span><div class="grow"></div>
      <button class="btn${done[2] ? ' solid' : ''}" id="wiz-x">${done[2] ? 'Done' : 'Close'}</button></div>
    <ol class="steps">
      ${step(0, 'Create a Chrome profile', `<p>Chrome profile menu, Add, Continue without an account. One profile per Instagram account.</p>${copyRow('chrome://profile-picker')}`)}
      ${step(1, 'Load the extension', `<p>In the new profile: open Extensions, turn on Developer mode, Load unpacked, pick this folder.</p>${copyRow('chrome://extensions')}${copyRow(s.extension_path || 'extension/ in the repo')}
        <p class="muted num">Extension id ${esc(s.extension_id || '–')}${s.extension_version ? ' · v' + esc(s.extension_version) : ''}</p>`)}
      ${step(2, 'Log in to Instagram', `<p>Log in with the account this profile should use and keep one Instagram tab open.</p>${copyRow('https://www.instagram.com/')}`)}
    </ol>`;
}
$('#acc-add').onclick = () => openWizard();
$('#wiz').addEventListener('click', (e) => {
  const c = e.target.closest('[data-copy]');
  if (c) return copyText(c.dataset.copy, c);
  if (e.target.closest('#wiz-x')) closeWizard();
});

// ---------- settings (qualification, OpenRouter keys and models, local services) ----------
const SET = { llm: null, health: null, tests: {}, models: null, confirm: null, share: null, busy: false };
async function loadSettings() {
  const [llm, acc] = await Promise.allSettled([api.get('/api/llm'), api.get('/api/accounts')]);
  if (llm.status === 'fulfilled') { SET.llm = llm.value; if (!SET.dirty) SET.models = [...SET.llm.models]; }
  if (acc.status === 'fulfilled') SET.share = acc.value.main_list_share;
  renderSettings();
  if (!SET.health) checkHealth();
}
async function checkHealth() {
  SET.health = 'checking'; renderServices();
  try { SET.health = await api.get('/api/llm/health'); } catch (e) { SET.health = null; }
  renderServices();
}
const upText = (up) => (up == null ? 'Checking' : up ? 'Running' : 'Not running');
function renderServices() {
  const h = SET.health && SET.health !== 'checking' ? SET.health : null, l = SET.llm;
  const row = (name, url, up, hint) => `<div class="svc"><i class="dot ${up ? 'on' : up === false ? 'off' : ''}"></i><div><b>${name}</b><span class="muted num">${esc(url || '')}</span>${up === false ? `<small>${hint}</small>` : ''}</div><span class="grow"></span><span class="${up ? '' : 'muted'}">${upText(up)}</span></div>`;
  $('#set-svc').innerHTML = row('OpenRouter proxy', h?.proxy.url || l?.providers?.[0]?.url, h ? h.proxy.up : null, 'Optional. Without it the keys below go to OpenRouter directly.')
    + row('Laya sidecar', h?.laya.url || l?.laya?.url, h ? h.laya.up : null, 'Optional. Start it with python3 sidecar/laya_server.py (see docs/SETUP.md).');
}
function keyStatus(p) {
  if (p.disabled) return { text: 'Refused', cls: 'bad' };
  const until = Object.values(p.cooldowns || {}).sort().pop();
  if (until) return { text: 'Cooling until ' + new Date(until).toTimeString().slice(0, 5), cls: 'warn' };
  return { text: 'Ready', cls: '' };
}
function renderSettings() {
  const l = SET.llm, sc = S.sc;
  $('#set-q-on').classList.toggle('on', !!sc?.qualify);
  $('#set-q-auto').classList.toggle('on', !!sc?.qualify_auto);
  const f = $('#set-q');
  if (l && !f.contains(document.activeElement)) { $('#set-workers').value = l.workers; $('#set-llm-min').value = l.llm_min; $('#set-bio-min').value = l.bio_min; }
  if (SET.share != null && document.activeElement?.id !== 'set-share') $('#set-share').value = Math.round(SET.share * 100);
  renderServices();
  const keys = (l?.providers || []).filter((p) => p.key);
  $('#set-keys-n').textContent = l ? plural(keys.length, 'key') : '';
  $('#set-file').textContent = l?.config || '';
  $('#set-keys').innerHTML = !l ? `<tr><td colspan="5" class="muted">Loading</td></tr>` : keys.length ? keys.map((p) => {
    const st = keyStatus(p), t = SET.tests[p.id], today = Object.values(p.requests_today || {}).reduce((a, b) => a + b, 0);
    const test = t === 'run' ? 'Testing' : t ? (t.passed ? `Works, ${t.ms} ms` : t.error) : p.last_error || '';
    const conf = SET.confirm === p.id;
    return `<tr><td class="mono">${esc(p.key)}</td><td class="hide-sm muted">${p.source === 'env' ? 'Environment' : 'Settings'}</td>
      <td><span class="kst ${st.cls}">${esc(st.text)}</span></td><td class="r num">${int(today)}</td>
      <td class="hide-sm key-note${t && !t.passed && t !== 'run' ? ' bad' : ''}" title="${esc(test)}">${esc(test)}</td>
      <td class="r"><span class="acts"><button data-ktest="${esc(p.id)}"${t === 'run' ? ' disabled' : ''}>Test</button>${p.source === 'env' ? '' : `<button data-kdel="${esc(p.id)}" class="${conf ? 'warn' : ''}">${conf ? 'Confirm' : 'Remove'}</button>`}</span></td></tr>`;
  }).join('') : `<tr><td colspan="6" class="muted">No keys yet. Free models work with a free OpenRouter key.</td></tr>`;
  if (l && document.activeElement?.id !== 'set-limit') $('#set-limit').value = l.daily_limit;
  renderModels();
}
function renderModels() {
  const ms = SET.models || [];
  $('#set-models').innerHTML = ms.length ? ms.map((m, i) => `<li><span class="num muted">${i + 1}</span><code>${esc(m)}</code>${/:free$/.test(m) ? '<span class="pill">Free</span>' : ''}<span class="grow"></span>
    <button class="btn ghost" data-mup="${i}" title="Try earlier"${i ? '' : ' disabled'}>Up</button><button class="btn ghost" data-mdown="${i}" title="Try later"${i < ms.length - 1 ? '' : ' disabled'}>Down</button><button class="btn ghost" data-mdel="${i}">Remove</button></li>`).join('')
    : '<li class="muted">No models</li>';
  $('#set-models-save').disabled = !SET.dirty;
}
$('#set-q-on').onclick = async () => {
  const on = !S.sc?.qualify;
  try { await api.post('/api/settings/qualify', { on }); toast(on ? 'Qualify on' : 'Qualify off'); await loadScraper(); renderSettings(); } catch (e) { toast('Could not save'); }
};
$('#set-q-auto').onclick = async () => {
  const auto = !S.sc?.qualify_auto;
  try { await api.post('/api/settings/qualify', { auto }); toast(auto ? 'Starts by itself after the lists' : 'Starts only by hand'); await loadScraper(); renderSettings(); } catch (e) { toast('Could not save'); }
};
$('#set-q').addEventListener('submit', async (e) => {
  e.preventDefault();
  const n = (id) => { const v = $(id).value.trim(); return /^\d+$/.test(v) ? +v : NaN; };
  const body = { workers: n('#set-workers'), llm_min: n('#set-llm-min'), bio_min: n('#set-bio-min') };
  if (Object.values(body).some(Number.isNaN)) { toast('Whole numbers only'); return; }
  try { await api.post('/api/settings/qualify', body); toast('Saved'); document.activeElement?.blur(); loadSettings(); } catch (err) { toast(err.status === 400 ? ucf(err.message) : 'Could not save'); }
});
$('#set-share-f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const v = $('#set-share').value.trim();
  if (!/^\d+$/.test(v) || +v > 100) { toast('A share from 0 to 100 %'); return; }
  try { const r = await api.post('/api/settings/accounts', { main_list_share: +v / 100 }); SET.share = r.main_list_share; toast(+v ? `Main account takes up to ${v} % of list pages` : 'Main account reads bios only'); document.activeElement?.blur(); }
  catch (err) { toast(err.status === 400 ? ucf(err.message) : 'Could not save'); }
});
$('#set-key-f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const key = $('#set-key').value.trim();
  if (!key) return;
  try { await api.post('/api/llm/keys', { key }); $('#set-key').value = ''; toast('Key added'); loadSettings(); }
  catch (err) { toast(err.status === 400 ? ucf(err.message) : 'Could not add the key'); }
});
$('#set-limit-f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const v = $('#set-limit').value.trim();
  if (!/^\d+$/.test(v)) { toast('A whole number, 0 for no limit'); return; }
  try { await api.post('/api/llm/models', { daily_limit: +v }); toast('Daily limit saved'); document.activeElement?.blur(); loadSettings(); }
  catch (err) { toast(err.status === 400 ? ucf(err.message) : 'Could not save'); }
});
$('#set-model-f').addEventListener('submit', (e) => {
  e.preventDefault();
  const m = $('#set-model').value.trim();
  if (!/^[\w.-]+\/[\w.:-]+$/.test(m)) { toast('Model ids look like vendor/model:free'); return; }
  if (!SET.models.includes(m)) { SET.models.push(m); SET.dirty = true; }
  $('#set-model').value = ''; renderModels();
});
$('#set-models-save').onclick = async () => {
  // free models first (they cost nothing), keeping the chosen order inside each group
  const models = [...SET.models.filter((m) => /:free$/.test(m)), ...SET.models.filter((m) => !/:free$/.test(m))];
  try { await api.post('/api/llm/models', { models }); SET.dirty = false; toast('Models saved'); loadSettings(); }
  catch (err) { toast(err.status === 400 ? ucf(err.message) : 'Could not save'); }
};
$('#view-settings').addEventListener('click', async (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.id === 'set-recheck') return checkHealth();
  const ms = SET.models;
  const swap = (i, j) => { [ms[i], ms[j]] = [ms[j], ms[i]]; SET.dirty = true; renderModels(); };
  if (b.dataset.mup) return swap(+b.dataset.mup, +b.dataset.mup - 1);
  if (b.dataset.mdown) return swap(+b.dataset.mdown, +b.dataset.mdown + 1);
  if (b.dataset.mdel) { if (ms.length > 1) { ms.splice(+b.dataset.mdel, 1); SET.dirty = true; renderModels(); } else toast('Keep at least one model'); return; }
  if (b.dataset.ktest) {
    const id = b.dataset.ktest;
    SET.tests[id] = 'run'; renderSettings();
    try { SET.tests[id] = await api.post(`/api/llm/keys/${id}/test`, {}); } catch (err) { SET.tests[id] = { passed: false, error: 'Could not reach the server' }; }
    return loadSettings();
  }
  if (b.dataset.kdel) {
    const id = b.dataset.kdel;
    if (SET.confirm !== id) { SET.confirm = id; renderSettings(); setTimeout(() => { if (SET.confirm === id) { SET.confirm = null; renderSettings(); } }, 3000); return; }
    SET.confirm = null;
    try { await api.post(`/api/llm/keys/${id}/remove`, {}); toast('Key removed'); } catch (err) { toast(err.status === 400 ? ucf(err.message) : 'Could not remove'); }
    return loadSettings();
  }
});

// ---------- qualification ----------
const ROLE_LABEL = { buyer: 'Brand owner', connector: 'Agency or freelancer', collaborator: 'Creative', peer: 'Similar service', supplier: 'Supplier', unrelated: 'Not a business', unclear: 'Unclear' };
const Q = {
  view: 'ai', q: '', sort: 'score', rows: [], total: 0, sum: null, busy: new Set(), gen: 0,
  async show() { await this.load(); },
  async load(more) {
    const g = ++this.gen;
    const p = new URLSearchParams({ view: this.view, sort: this.sort, limit: 30, offset: more ? this.rows.length : 0 });
    if (this.q) p.set('q', this.q);
    let d;
    try { d = await api.get('/api/qual?' + p); } catch (e) { if (!this.rows.length) $('#ql-list').innerHTML = `<div class="muted ql-empty">${e.status === 404 ? 'The server needs a restart to show this page.' : 'Could not load'}</div>`; return; }
    if (g !== this.gen) return;
    this.rows = more ? [...this.rows, ...d.rows] : d.rows; this.total = d.total; this.sum = d.summary;
    // With no AI verdicts yet, show keyword verdicts instead of an empty page.
    if (!more && this.view === 'ai' && !d.total && !this.q && !this.fellBack) { this.fellBack = true; this.view = 'all'; this.syncSeg(); return this.load(); }
    this.renderProg(); this.render();
  },
  syncSeg() { $$('#ql-view button').forEach((b) => b.classList.toggle('on', b.dataset.v === this.view)); },
  renderProg() {
    const s = this.sum || {}, Qp = S.sc?.progress?.qualify || {};
    const on = Qp.on ?? S.sc?.qualify;
    const pct = s.verdicts ? Math.round((s.ai / s.verdicts) * 100) : 0;
    const when = !on ? 'AI scoring is off' : !Qp.left ? 'Everyone waiting has been checked' : eta(Qp.eta_h) ? eta(Qp.eta_h) + ' left' : 'starting';
    const kpi = (v, l) => `<div class="ql-k"><b class="num">${v}</b><span>${l}</span></div>`;
    $('#ql-prog').innerHTML = `<div class="ql-kpis">${kpi(fmt(s.ai ?? 0), 'checked by AI')}${kpi(fmt(s.rules ?? 0), 'keyword check only')}${kpi(fmt(Qp.left ?? 0), 'waiting for AI')}${kpi(Qp.per_hour ? fmt(Qp.per_hour) : '–', 'AI checks per hour')}</div>
      <div class="ql-pbar"><div class="bar-p ${on && Qp.left ? 'run' : 'done'}"><i style="width:${pct}%"></i></div>
      <span class="muted"><i class="dot ${on ? 'live' : 'off'}"></i> ${esc(when)}${on ? ` · ${plural(Qp.workers || 0, 'check')} at a time · ${plural(Qp.keys || 0, 'OpenRouter key')}` : ' · turn on Qualify at the top to start'}</span></div>`;
    $('#n-qual').textContent = on && Qp.left ? fmt(Qp.left) : '';
  },
  card(r) {
    const v = r.verdict || {}, ai = v.model && v.model !== 'rules';
    const ev = evidenceOf(v);
    const aiTags = (r.tags || []).filter((t) => t.grp === 'ai');
    const seeds = [...new Set(r.via || [])];
    const bio = (r.bio || '').trim();
    const based = [
      bio ? `<li><b>Bio</b><span>${esc(bio.length > 160 ? bio.slice(0, 160) + '…' : bio)}</span>${r.bio_at ? `<em>read ${ago(r.bio_at)} ago</em>` : ''}</li>` : `<li><b>Bio</b><span class="muted">${r.is_private ? 'Private account, cannot be read' : 'Not read yet'}</span></li>`,
      r.website ? `<li><b>Link in bio</b><span><a href="${esc(safeUrl(r.website) || '#')}" target="_blank" rel="noopener">${esc(r.website.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, ''))}</a></span></li>` : '',
      r.category ? `<li><b>Instagram category</b><span>${esc(r.category)}</span></li>` : '',
      `<li><b>Network</b><span>${seeds.length ? `In ${plural(seeds.length, 'list')}: ${seedList(seeds, 4)}` : 'Not in any list'}${youLink(r) ? ' · ' + esc(youLink(r)) : ''}</span></li>`,
      r.followers != null ? `<li><b>Audience</b><span>${fmt(r.followers)} followers${r.posts != null ? ' · ' + fmt(r.posts) + ' posts' : ''}</span></li>` : '',
    ].join('');
    const site = r.site;
    const sg = site?.signals || {};
    const facts = [sg.shop && `Built on ${sg.shop}`, sg.cart && !sg.shop && 'Has a cart', sg.meta_pixel && 'Meta ads pixel', sg.tiktok_pixel && 'TikTok ads pixel', sg.google_ads && 'Google Ads tag',
      sg.klaviyo && 'Klaviyo email', sg.usd && 'Prices in USD', sg.eur && 'Prices in EUR', sg.stage && sg.stage !== 'unknown' && ucf(sg.stage) + ' stage', sg.us_market === true && 'Sells to the US'].filter(Boolean);
    const siteHTML = site ? `<div class="ql-site"><div class="ql-sh"><b>Website</b><a href="${esc(safeUrl(site.final_url) || '#')}" target="_blank" rel="noopener">${esc((site.final_url || '').replace(/^https?:\/\/(www\.)?/, '').slice(0, 60))}</a><em>${ago(site.at)} ago</em></div>
      ${site.summary ? `<p>${esc(site.summary)}</p>` : ''}${site.error ? `<p class="muted">${esc(ucf(site.error))}</p>` : ''}
      ${facts.length ? `<div class="ql-facts">${facts.map((f) => `<span>${esc(f)}</span>`).join('')}</div>` : ''}</div>` : '';
    const busy = this.busy.has(r.id);
    return `<article class="ql-card" data-id="${r.id}">
      <div class="ql-top">${avatar(r.pic, r.name || r.handle, 'lg')}
        <div class="who"><b>${esc(r.name || r.handle)}</b><span>@${esc(r.handle)}${r.status ? ' · ' + esc(ucf(r.status)) : ''}</span></div>
        <div class="ql-score"><b class="num">${r.score ?? '–'}</b>${fitBadge(r)}</div></div>
      <div class="ql-body">
        <div class="ql-why"><h4>Verdict</h4><p><b>${esc(ROLE_LABEL[r.role] || ucf(r.role || 'Unknown'))}.</b> ${esc(r.reason || 'No reason given.')}</p>
          ${ev.length ? `<ul class="evidence">${ev.map((q) => `<li>"${esc(q)}"</li>`).join('')}</ul>` : ''}
          ${aiTags.length ? `<div class="ql-tags">${aiTags.map((t) => tagChip(t)).join('')}</div>` : ''}
          <p class="ql-by">${ai ? `Checked by AI (${esc(modelLabel(v.model))})` : 'Keyword check only, no AI yet'}${v.at ? ` · ${ago(v.at)} ago` : ''}</p></div>
        <div class="ql-based"><h4>Based on</h4><ul>${based}</ul></div>
      </div>
      ${siteHTML}
      <div class="ql-acts"><button class="btn${busy ? '' : ' solid'}" data-deep="${r.id}" ${busy ? 'disabled' : ''}>${busy ? 'Reading…' : 'Dig deeper'}</button>
        <span class="muted ql-hint">${r.website ? 'Reads the bio again and their website' : 'Reads the bio again (no website in bio)'}</span><span class="grow"></span>
        <button class="btn ghost" data-open="${r.id}">Open</button>
        <a class="btn ghost" href="https://www.instagram.com/${encodeURIComponent(r.handle)}/" target="_blank" rel="noopener">Instagram</a></div>
    </article>`;
  },
  render() {
    $('#ql-n').textContent = `${int(this.total)} ${this.total === 1 ? 'person' : 'people'}`;
    $('#ql-list').innerHTML = this.rows.length ? this.rows.map((r) => this.card(r)).join('')
      : `<div class="muted ql-empty">${this.view === 'ai' ? 'Nobody has been checked by AI yet. Turn on Qualify at the top; results appear here.' : 'Nobody matches.'}</div>`;
    $('#ql-more').hidden = this.rows.length >= this.total;
  },
  async deeper(id) {
    this.busy.add(id); this.render();
    try {
      const d = await api.post(`/api/qual/${id}/deeper`);
      const r = this.rows.find((x) => x.id === id);
      if (r && d.site) r.site = d.site;
      toast(ucf(d.note || 'Done'));
    } catch (e) { toast(e.status === 400 ? ucf(e.message) : 'Could not dig deeper'); }
    this.busy.delete(id); this.render();
  },
};
$('#ql-view').addEventListener('click', (e) => { const b = e.target.closest('[data-v]'); if (!b) return; Q.view = b.dataset.v; Q.fellBack = true; Q.syncSeg(); Q.load(); });
$('#ql-q').addEventListener('input', debounce((e) => { Q.q = e.target.value.trim(); Q.load(); }, 250));
$('#ql-sort').addEventListener('change', (e) => { Q.sort = e.target.value; Q.load(); });
$('#ql-more').onclick = () => Q.load(true);
$('#ql-list').addEventListener('click', (e) => {
  const d = e.target.closest('[data-deep]'); if (d) return Q.deeper(+d.dataset.deep);
  const o = e.target.closest('[data-open]');
  if (o) { const r = Q.rows.find((x) => x.id === +o.dataset.open); S.f = emptyFilter(); S.f.q = r ? r.handle : ''; $('#q').value = S.f.q; S.view = 'leads'; filtersChanged(); setView('leads'); openDetail(+o.dataset.open); }
});

// ---------- map ----------
const LEAD_R = [0, 4, 5.6, 7, 8.2, 9.4];
// Profile photos for the map, pre-cropped to circles on small canvases.
const PICS = new Map(); let picsLoading = 0; const picQueue = [];
function mapPic(url) {
  let e = PICS.get(url);
  if (!e) { PICS.set(url, (e = { state: 'queued', c: null })); picQueue.push(url); pumpPics(); }
  return e.state === 'ok' ? e.c : null;
}
function pumpPics() {
  while (picsLoading < 8 && picQueue.length) {
    const url = picQueue.pop(), e = PICS.get(url);
    picsLoading++;
    const im = new Image(); im.decoding = 'async';
    im.onload = () => {
      const s = 64, c = document.createElement('canvas'); c.width = c.height = s;
      const g = c.getContext('2d'), m = Math.min(im.naturalWidth, im.naturalHeight);
      g.beginPath(); g.arc(s / 2, s / 2, s / 2, 0, Math.PI * 2); g.clip();
      g.drawImage(im, (im.naturalWidth - m) / 2, (im.naturalHeight - m) / 2, m, m, 0, 0, s, s);
      e.c = c; e.state = 'ok'; picsLoading--; M.schedule(); pumpPics();
    };
    im.onerror = () => { e.state = 'err'; picsLoading--; pumpPics(); };
    im.src = url;
  }
}
const MARKED = new Set(['interested', 'contacted', 'talking', 'client']);
const M = {
  sim: null, nodes: [], seeds: [], leads: [], links: [], seedLinks: [], byId: new Map(), nbr: new Map(), rev: null, scope: 'leads',
  k: 1, x: 0, y: 0, w: 0, h: 0, hover: null, focus: null, matches: [], mi: -1, labels: store.get('labels', true),
  loaded: false, stale: true, fitted: false, timer: null, raf: 0, maxShared: 1, shown: false, loading: false,
  show() {
    this.shown = true;
    this.resize();
    if (!this.loaded || this.stale) this.load();
    clearInterval(this.timer); this.timer = setInterval(() => { if (!document.hidden) this.load(true); }, 30000);
    if (this.sim && this.sim.alpha() > this.sim.alphaMin()) this.sim.restart();
    $('#map-labels').classList.toggle('on', this.labels);
  },
  hide() { this.shown = false; clearInterval(this.timer); if (this.sim) this.sim.stop(); $('#hover').hidden = true; },
  resize() {
    const c = $('#canvas'), st = $('#stage');
    if (!st.clientWidth) return;
    const dpr = window.devicePixelRatio || 1;
    const w = st.clientWidth, h = st.clientHeight;
    if (this.w && this.fitted) { this.x += (w - this.w) / 2; this.y += (h - this.h) / 2; }
    this.w = w; this.h = h;
    c.width = w * dpr; c.height = h * dpr;
    this.ctx = c.getContext('2d'); this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.draw();
  },
  reloadSoon: debounce(() => M.load(), 250),
  url() {
    const p = toQuery(S.f, S.sort, false);
    p.set('scope', this.scope); p.set('limit', this.scope === 'all' ? 5000 : 800);
    return '/api/map?' + p;
  },
  async load(poll) {
    if (this.loading && poll) return;
    this.loading = true;
    const url = this.url();
    let d;
    try { d = await api.get(url); } catch (e) { this.loading = false; if (!this.nodes.length) this.status(offlineSince ? 'Server offline' : 'Could not load map'); return; }
    this.loading = false;
    if (url !== this.url()) return;
    this.loaded = true; this.stale = false;
    const key = d.rev + '|' + url;
    if (key === this.rev && this.nodes.length) return;
    this.rev = key;
    this.build(d);
  },
  build(d) {
    // A new filter gets a fresh, fitted layout; a refresh of the same view keeps positions and pins.
    const old = this.relayout ? new Map() : this.byId;
    this.relayout = false;
    if (!old.size) this.autoFit = true;
    // When filtered, seeds with nobody in the result only add noise: leave them out.
    if (filterCount() && d.nodes.some((n) => n.kind !== 'seed')) {
      const used = new Set((d.links || []).map((l) => l.source));
      d = Object.assign({}, d, { nodes: d.nodes.filter((n) => n.kind !== 'seed' || used.has(n.id)) });
    }
    const sid = (s) => String(s).startsWith('s:') || String(s).startsWith('p:') ? String(s) : 's:' + s;
    this.nodes = d.nodes.map((n) => {
      const o = old.get(n.id);
      const seed = n.kind === 'seed';
      const L = seed ? 0 : Math.max(1, Math.round(+(n.lists ?? n.degree ?? 1)) || 1);
      const r = seed ? Math.max(9, Math.min(26, 7 + Math.sqrt(n.degree || 0) * 0.62)) : LEAD_R[Math.min(5, L)];
      return Object.assign(n, { L, r, vis: 0, fit: seed ? null : fitOf(n) }, o ? { x: o.x, y: o.y, vx: 0, vy: 0, fx: o.fx, fy: o.fy } : {});
    });
    this.byId = new Map(this.nodes.map((n) => [n.id, n]));
    this.seeds = this.nodes.filter((n) => n.kind === 'seed');
    this.leads = this.nodes.filter((n) => n.kind !== 'seed');
    // One line per seed-person pair: 'followers' = they follow the seed, 'following' = the seed follows them, 'both'.
    const pair = new Map();
    for (const l of d.links || []) {
      if (!this.byId.has(l.source) || !this.byId.has(l.target)) continue;
      const key = l.source + '>' + l.target, had = pair.get(key);
      if (had) { if (had.dir !== l.direction) had.dir = 'both'; } else pair.set(key, { source: l.source, target: l.target, dir: l.direction || 'followers' });
    }
    this.links = [...pair.values()];
    this.seedLinks = (d.seed_links || []).map((l) => ({ source: sid(l.source), target: sid(l.target), shared: +l.shared || 0 }))
      .filter((l) => this.byId.has(l.source) && this.byId.has(l.target) && l.shared > 0);
    this.maxShared = Math.max(1, ...this.seedLinks.map((l) => l.shared));
    this.nbr = new Map(this.nodes.map((n) => [n.id, []]));
    for (const l of this.links) { this.nbr.get(l.source).push(l.target); this.nbr.get(l.target).push(l.source); this.byId.get(l.source).vis++; }
    this.overlap = new Map(this.seeds.map((s) => [s.id, []]));
    for (const l of this.seedLinks) { this.overlap.get(l.source).push([l.target, l.shared]); this.overlap.get(l.target).push([l.source, l.shared]); }
    this.overlap.forEach((a) => a.sort((x, y) => y[1] - x[1]));
    // Initial layout: seeds on a circle ordered by overlap, leads near the centroid of their seeds.
    const fresh = this.seeds.filter((s) => s.x == null);
    if (fresh.length) {
      const order = [];
      const left = new Set(this.seeds.map((s) => s.id));
      let curr = [...this.seeds].sort((a, b) => b.degree - a.degree)[0]?.id;
      while (curr) { order.push(curr); left.delete(curr); curr = (this.overlap.get(curr) || []).find(([id]) => left.has(id))?.[0] || [...left][0]; }
      const R = 140 + this.seeds.length * 26;
      order.forEach((id, i) => { const s = this.byId.get(id); if (s.x == null) { const a = (i / order.length) * Math.PI * 2; s.x = Math.cos(a) * R; s.y = Math.sin(a) * R; } });
    }
    for (const n of this.leads) {
      if (n.x != null) continue;
      const ss = this.nbr.get(n.id).map((id) => this.byId.get(id)).filter(Boolean);
      const cx = ss.reduce((a, s) => a + s.x, 0) / (ss.length || 1), cy = ss.reduce((a, s) => a + s.y, 0) / (ss.length || 1);
      const a = Math.random() * Math.PI * 2, rr = (ss.length > 1 ? 10 : 30) + Math.random() * 60;
      n.x = cx + Math.cos(a) * rr; n.y = cy + Math.sin(a) * rr;
    }
    const nl = this.leads.length;
    $('#map-count').textContent = `${int(nl)} people · ${plural(this.seeds.length, 'source account')}`;
    if (this.focus) this.focus = this.byId.get(this.focus.id) || null;
    this.simulate(old.size ? 0.5 : 1);
    this.search();
    if (!nl) this.draw();
  },
  simulate(alpha) {
    if (this.sim) this.sim.stop();
    const F = window.d3;
    if (!F?.forceSimulation) { this.status('Map library missing'); return; }
    const big = this.nodes.length > 2500;
    this.seedLinks.forEach((l) => { l.ss = true; });
    const all = [...this.links, ...this.seedLinks];
    this.sim = F.forceSimulation(this.nodes)
      .force('link', F.forceLink(all).id((n) => n.id)
        .distance((l) => l.ss ? 520 - 360 * Math.sqrt(l.shared / this.maxShared) : l.source.r + 26 + (l.target.L > 1 ? 30 : 10) + Math.sqrt(l.source.vis || 1) * 1.6)
        .strength((l) => l.ss ? 0.04 + 0.5 * (l.shared / this.maxShared) : 0.9 / Math.max(1, l.target.L)))
      .force('charge', F.forceManyBody().strength((n) => n.kind === 'seed' ? -30 : -14).distanceMax(big ? 200 : 300).theta(big ? 1.1 : 0.9))
      .force('seeds', (alpha) => {
        // Seeds repel only each other, so leads can sit close to their seeds.
        const ss = this.seeds;
        for (let i = 0; i < ss.length; i++) for (let j = i + 1; j < ss.length; j++) {
          const a = ss[i], b = ss[j];
          const dx = b.x - a.x || 0.1, dy = b.y - a.y || 0.1, d2 = Math.max(dx * dx + dy * dy, 400);
          const f = (3600 + 20 * (a.vis + b.vis)) * alpha / d2;
          a.vx -= dx * f; a.vy -= dy * f; b.vx += dx * f; b.vy += dy * f;
        }
      })
      .force('collide', F.forceCollide((n) => n.kind === 'seed' ? n.r * 1.45 + 6 : n.r + 1.8).iterations(1).strength(0.8))
      .force('x', F.forceX(0).strength((n) => n.kind === 'seed' ? 0.02 : 0.004)).force('y', F.forceY(0).strength((n) => n.kind === 'seed' ? 0.02 : 0.004))
      .alpha(alpha).alphaDecay(big ? 0.055 : 0.035).alphaMin(big ? 0.012 : 0.001).velocityDecay(0.42)
      .on('tick', () => this.schedule())
      .on('end', () => { if (this.autoFit) this.fit(); });
    if (alpha >= 1) {
      const t0 = performance.now();
      for (let i = 0; i < 160 && performance.now() - t0 < 450; i++) this.sim.tick();
      this.fit();
    }
    if (!this.shown) this.sim.stop();
  },
  schedule() {
    if (this.raf) return;
    this.raf = requestAnimationFrame(() => { this.raf = 0; if (this.autoFit) this.fit(); else this.draw(); });
  },
  status(t) {
    const c = this.ctx; if (!c) return;
    c.clearRect(0, 0, this.w, this.h);
    c.fillStyle = css('--fg3'); c.font = '14px ' + css('--sans'); c.textAlign = 'left';
    c.fillText(t, 16, 28);
  },
  bounds(list) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of list) { x0 = Math.min(x0, n.x - n.r); y0 = Math.min(y0, n.y - n.r); x1 = Math.max(x1, n.x + n.r); y1 = Math.max(y1, n.y + n.r + (n.kind === 'seed' ? 22 : 0)); }
    return [x0, y0, x1, y1];
  },
  fit(list) {
    // Seeds whose lists are still empty float far out; leave them out of the frame when there is anything else.
    if (!list) { const busy = this.nodes.filter((n) => n.kind !== 'seed' || n.vis > 0 || n.degree > 0); list = busy.length ? busy : this.nodes; }
    if (!list.length || !this.w) return;
    const [x0, y0, x1, y1] = this.bounds(list);
    // Room for seed labels at the sides and the legend at the bottom.
    const px = this.w < 600 ? 44 : 80, pt = 24, pb = this.w < 600 ? 40 : 64;
    this.k = Math.min(3, Math.max(0.05, Math.min((this.w - px * 2) / (x1 - x0 || 1), (this.h - pt - pb) / (y1 - y0 || 1))));
    this.x = this.w / 2 - ((x0 + x1) / 2) * this.k; this.y = pt + (this.h - pt - pb) / 2 - ((y0 + y1) / 2) * this.k;
    this.fitted = true;
    this.draw();
  },
  zoomBy(f, px = this.w / 2, py = this.h / 2) {
    this.autoFit = false;
    const k = Math.min(10, Math.max(0.04, this.k * f));
    this.x = px - ((px - this.x) * k) / this.k; this.y = py - ((py - this.y) * k) / this.k; this.k = k;
    this.draw();
  },
  centerOn(n, k) {
    this.k = Math.max(this.k, k || 1.6);
    this.x = this.w / 2 - n.x * this.k; this.y = this.h / 2 - n.y * this.k;
    this.draw();
  },
  // Drag a node: it moves alone (a seed takes the people only in its list along), is pinned where dropped,
  // and only its close neighbours step aside. The rest of the map does not move.
  dragTo(n, x, y) {
    const dx = x - n.x, dy = y - n.y;
    n.x = n.fx = x; n.y = n.fy = y; n.vx = n.vy = 0;
    if (n.kind === 'seed') for (const id of this.nbr.get(n.id) || []) { const m = this.byId.get(id); if (m && m.L === 1 && m.fx == null) { m.x += dx; m.y += dy; m.vx = m.vy = 0; } }
    this.draw();
  },
  relax(n) {
    const near = () => { const R = n.r + 90; return this.nodes.filter((m) => m !== n && Math.abs(m.x - n.x) < R && Math.abs(m.y - n.y) < R); };
    let list = near(), frames = 14;
    const step = () => {
      let moved = false;
      for (const a of [n, ...list]) for (const b of list) {
        if (a === b) continue;
        const dx = b.x - a.x || 0.01, dy = b.y - a.y || 0.01, d = Math.hypot(dx, dy), min = a.r + b.r + (a.kind === 'seed' || b.kind === 'seed' ? 8 : 2);
        if (d >= min) continue;
        const push = (min - d) / d * 0.5;
        if (b.fx == null) { b.x += dx * push; b.y += dy * push; moved = true; }
        if (a !== n && a.fx == null) { a.x -= dx * push; a.y -= dy * push; }
      }
      this.draw();
      if (moved && --frames > 0) requestAnimationFrame(step);
    };
    if (list.length) requestAnimationFrame(step);
  },
  toggleLabels() { this.labels = !this.labels; store.set('labels', this.labels); $('#map-labels').classList.toggle('on', this.labels); this.draw(); },
  // Neighbourhood of the hovered or focused node.
  hood() {
    const n = this.hover || this.focus;
    if (!n) return null;
    const set = new Set([n.id]);
    for (const id of this.nbr.get(n.id) || []) set.add(id);
    if (n.kind === 'seed') for (const [id] of this.overlap.get(n.id) || []) set.add(id);
    return { n, set };
  },
  patch(ids, op) {
    let hit = false;
    for (const id of ids) {
      const n = this.byId.get('p:' + id) || this.seeds.find((x) => x.pid === id);
      if (!n) continue;
      hit = true;
      if ('status' in op) n.status = op.status;
      if (op.add && n.tags) op.add.forEach((t) => { if (!n.tags.includes(t)) n.tags.push(t); });
      if (op.remove && n.tags) n.tags = n.tags.filter((t) => !op.remove.includes(t));
    }
    if (hit) this.draw();
  },
  draw() {
    const c = this.ctx;
    if (!c || S.view !== 'map' || !this.w) return;
    const fg = css('--fg'), fg2 = css('--fg2'), fg3 = css('--fg3'), fg4 = css('--fg4'), bg = css('--bg'), line = css('--line2'), sans = css('--sans');
    const k = this.k;
    c.clearRect(0, 0, this.w, this.h);
    if (!this.nodes.length) { this.status(this.loaded ? 'No people match these filters' : 'Loading'); return; }
    c.save(); c.translate(this.x, this.y); c.scale(k, k);
    const hd = this.hood();
    const match = this.matchSet;
    const dim = !!hd || (match && match.size > 0);
    const on = (n) => hd ? hd.set.has(n.id) : match && match.size ? match.has(n.id) : true;
    // Viewport culling bounds in world coords.
    const vx0 = -this.x / k - 20, vy0 = -this.y / k - 20, vx1 = (this.w - this.x) / k + 20, vy1 = (this.h - this.y) / k + 20;
    const inView = (n) => n.x + n.r > vx0 && n.x - n.r < vx1 && n.y + n.r > vy0 && n.y - n.r < vy1;

    // Seed overlap edges, weighted by shared people.
    for (const l of this.seedLinks) {
      const a = l.source, b = l.target;
      const w = 1 + 5 * (l.shared / this.maxShared);
      const hot = hd && (hd.n === a || hd.n === b);
      c.globalAlpha = hd ? (hot ? 0.8 : 0.06) : 0.28;
      c.strokeStyle = hot ? fg2 : fg4; c.lineWidth = Math.max(w, 1 / k);
      c.beginPath(); c.moveTo(a.x, a.y); c.lineTo(b.x, b.y); c.stroke();
    }
    // Lead edges: people in one list sit next to their seed, so their line is barely drawn; bridges (2+ lists) read clearly.
    // Solid hairline = they follow the seed (or both ways); dashed = the seed follows them.
    const edgePass = (multi, alpha) => {
      c.globalAlpha = alpha;
      for (const dashed of [false, true]) {
        c.setLineDash(dashed ? [3 / k, 3 / k] : []); c.beginPath();
        for (const l of this.links) if ((l.target.L > 1) === multi && (l.dir === 'following') === dashed) { c.moveTo(l.source.x, l.source.y); c.lineTo(l.target.x, l.target.y); }
        c.stroke();
      }
      c.setLineDash([]);
    };
    c.strokeStyle = line; c.lineWidth = 1 / k;
    edgePass(false, dim ? 0.03 : 0.1);
    c.strokeStyle = fg4; edgePass(true, dim ? 0.06 : this.leads.length > 1500 ? 0.22 : 0.34);
    if (hd) {
      c.globalAlpha = 0.85; c.strokeStyle = fg2; c.lineWidth = 1.2 / k; c.beginPath();
      for (const id of this.nbr.get(hd.n.id) || []) { const m = this.byId.get(id); c.moveTo(hd.n.x, hd.n.y); c.lineTo(m.x, m.y); }
      c.stroke();
    }
    // Leads: dots coloured by fit, sized by lists; one batched path per fit, dimmed pass first.
    const minPx = 2.4 / k;
    const fitColor = Object.fromEntries(FITS.map((f) => [f, css('--fit-' + f)]));
    fitColor.unread = fg4;   // on the dark canvas the list colour for unread is too faint to find
    const circle = (n, r) => { c.moveTo(n.x + r, n.y); c.arc(n.x, n.y, r, 0, Math.PI * 2); };
    const pass = (filter, fill, alpha) => {
      c.globalAlpha = alpha; c.fillStyle = fill; c.beginPath();
      for (const n of this.leads) if (inView(n) && filter(n)) circle(n, Math.max(n.r, minPx));
      c.fill();
    };
    for (const f of [...FITS].reverse()) {
      if (dim) pass((n) => !on(n) && n.fit === f, fitColor[f], 0.18);
      pass((n) => on(n) && n.fit === f, fitColor[f], 1);
    }
    // Photos on top of the dots once they are big enough to read.
    for (const n of this.leads) {
      if (!n.pic || !inView(n)) continue;
      const r = Math.max(n.r, minPx);
      if (r * k < 6) continue;
      const img = mapPic(n.pic);
      if (!img) continue;
      c.globalAlpha = dim && !on(n) ? 0.18 : 1;
      c.drawImage(img, n.x - r, n.y - r, r * 2, r * 2);
    }
    // Marked rings.
    c.globalAlpha = 1; c.strokeStyle = fg; c.lineWidth = 1.4 / k; c.beginPath();
    for (const n of this.leads) {
      if (!n.status || !MARKED.has(n.status) || !inView(n) || !on(n)) continue;
      circle(n, Math.max(n.r, minPx) + 2.2 / k + 1);
    }
    c.stroke();
    // Open / focused node.
    const sel = (S.open && this.byId.get('p:' + S.open)) || this.focus;
    if (sel) { c.strokeStyle = fg; c.lineWidth = 2 / k; c.beginPath(); circle(sel, sel.r + 6 / k); c.stroke(); }
    // Seeds.
    for (const n of this.seeds) {
      const r = n.r;
      const faded = (hd && !hd.set.has(n.id)) || !n.vis;
      c.globalAlpha = faded ? 0.3 : 1;
      const img = n.pic && mapPic(n.pic);
      c.beginPath(); c.arc(n.x, n.y, r, 0, Math.PI * 2);
      if (img) c.drawImage(img, n.x - r, n.y - r, r * 2, r * 2);
      else { c.fillStyle = n.is_me ? bg : fg; c.fill(); }
      c.strokeStyle = n.is_me ? fg : bg; c.lineWidth = n.is_me ? Math.max(3, 3 / k) : 2 / k;
      c.beginPath(); c.arc(n.x, n.y, r, 0, Math.PI * 2); c.stroke();
    }
    c.restore();
    c.globalAlpha = 1;
    this.drawLabels(hd, match, fg, fg2, fg3, bg, sans);
    if (this.loaded && !this.leads.length) { c.fillStyle = fg3; c.font = '14px ' + sans; c.textAlign = 'left'; c.textBaseline = 'alphabetic'; c.fillText('No people match these filters', 16, 28); }
  },
  // Screen-space labels with greedy collision avoidance.
  drawLabels(hd, match, fg, fg2, fg3, bg, sans) {
    const c = this.ctx, k = this.k;
    const placed = [];
    const hit = (x0, y0, x1, y1) => placed.some((b) => x0 < b[2] && x1 > b[0] && y0 < b[3] && y1 > b[1]);
    const sx = (n) => n.x * k + this.x, sy = (n) => n.y * k + this.y;
    // Seed squares block labels.
    for (const n of this.seeds) { const r = n.r * k; placed.push([sx(n) - r, sy(n) - r, sx(n) + r, sy(n) + r]); }
    c.textBaseline = 'middle';
    // Seed labels, always, below the square.
    c.font = `600 13px ${sans}`;
    const seedsBy = [...this.seeds].sort((a, b) => b.degree - a.degree);
    for (const n of seedsBy) {
      const faded = (hd && !hd.set.has(n.id)) || !n.vis;
      const t = '@' + n.label + (n.is_me ? ' (you)' : '');
      const w = c.measureText(t).width + 10, x = sx(n) - w / 2, y = sy(n) + n.r * k + 4;
      if (x > this.w || x + w < 0 || y > this.h || y + 20 < 0) continue;
      const mine = hd && hd.n === n;
      if (!mine && hit(x, y, x + w, y + 20)) continue;
      placed.push([x, y, x + w, y + 20]);
      c.globalAlpha = faded ? 0.45 : 1;
      c.fillStyle = bg; c.beginPath(); c.roundRect ? c.roundRect(x, y, w, 20, 10) : c.rect(x, y, w, 20); c.fill();
      c.fillStyle = fg; c.fillText(t, x + 5, y + 10.5);
    }
    c.globalAlpha = 1;
    if (!this.labels && !hd && !(match && match.size)) return;
    // Lead label candidates by importance.
    let cand;
    if (hd && hd.n.kind !== 'seed') cand = [hd.n];
    else if (hd) cand = (this.nbr.get(hd.n.id) || []).map((id) => this.byId.get(id));
    else if (match && match.size) cand = this.matches;
    else cand = this.leads;
    const view = (n) => { const x = sx(n), y = sy(n); return x > -50 && x < this.w + 50 && y > -20 && y < this.h + 20; };
    const imp = (n) => n.L * 10 + (MARKED.has(n.status) ? 25 : 0) + ({ strong: 12, good: 6 }[n.fit] || 0) + Math.log10((n.followers || 1) + 1);
    cand = cand.filter((n) => n && n.kind !== 'seed' && view(n));
    const few = this.leads.length <= 120;
    if (!hd && !(match && match.size) && !few) cand = cand.filter((n) => n.L >= 2 || k > 1.4 || MARKED.has(n.status) || n.fit === 'strong');
    cand.sort((a, b) => imp(b) - imp(a));
    const max = hd || (match && match.size) || few ? 160 : Math.round(Math.min(120, (this.w * this.h / 26000) * Math.max(1, k * k)));
    let shown = 0;
    for (const n of cand) {
      if (shown >= max) break;
      const big = MARKED.has(n.status) || (hd && hd.n === n) || (match && match.has(n.id));
      c.font = `${big ? 600 : 500} 12px ${sans}`;
      const t = n.handle || n.label;
      const w = c.measureText(t).width + 8, r = Math.max(n.r * k, 2.4);
      const x = sx(n), y = sy(n);
      let box = [x + r + 3, y - 8, x + r + 3 + w, y + 8];
      if (hit(...box)) { box = [x - r - 3 - w, y - 8, x - r - 3, y + 8]; if (hit(...box)) continue; }
      placed.push(box); shown++;
      c.fillStyle = bg; c.globalAlpha = 0.86; c.beginPath(); c.roundRect ? c.roundRect(box[0], box[1], w, 16, 4) : c.rect(box[0], box[1], w, 16); c.fill();
      c.globalAlpha = 1; c.fillStyle = big ? fg : fg2; c.fillText(t, box[0] + 4, y + 0.5);
    }
  },
  // Hit test against what is drawn (dot radius with the on-screen minimum, photo, ring), always on the live node list:
  // a cached quadtree went stale after reloads (new people could not be clicked) and its fixed 8-unit search radius
  // missed the edge of big photo nodes. Seeds are drawn on top, so they win; then the node the pointer is most inside.
  at(px, py) {
    const k = this.k, x = (px - this.x) / k, y = (py - this.y) / k, minPx = 2.4 / k, pad = 7 / k;
    for (let i = this.seeds.length - 1; i >= 0; i--) {
      const n = this.seeds[i];
      if (Math.hypot(n.x - x, n.y - y) <= n.r + 3 / k) return n;
    }
    let best = null, bestScore = Infinity;
    for (const n of this.leads) {
      if (n.x == null) continue;
      const r = Math.max(n.r, minPx), reach = Math.max(r + 3 / k, pad);  // tiny dots get a 7 px target
      const dx = n.x - x, dy = n.y - y;
      if (dx > reach || dx < -reach || dy > reach || dy < -reach) continue;
      const d = Math.hypot(dx, dy);
      if (d > reach) continue;
      const score = d - r;   // negative = inside the drawn circle
      if (score < bestScore) { bestScore = score; best = n; }
    }
    return best;
  },
  search() {
    const q = $('#map-q').value.trim().toLowerCase().replace(/^@/, '');
    this.matches = q ? this.nodes.filter((n) => ((n.handle || n.label || '') + ' ' + (n.name || '')).toLowerCase().includes(q))
      .sort((a, b) => (b.kind === 'seed') - (a.kind === 'seed') || b.L - a.L) : [];
    this.matchSet = new Set(this.matches.map((n) => n.id));
    this.mi = -1;
    $('#map-hits').textContent = q ? int(this.matches.length) : '';
    this.draw();
  },
  next() {
    if (!this.matches.length) return;
    this.mi = (this.mi + 1) % this.matches.length;
    const n = this.matches[this.mi];
    $('#map-hits').textContent = `${this.mi + 1}/${this.matches.length}`;
    this.select(n);
    this.centerOn(n, 1.8);
  },
  select(n) {
    this.focus = n;
    // A seed that is also a person opens that person's panel (status, tags, note) with its lists below.
    if (n.kind === 'seed' && !n.pid) openSeed(n); else openDetail(n.kind === 'seed' ? n.pid : +n.id.slice(2));
    this.draw();
  },
};
function hoverCard(n) {
  if (n.kind === 'seed') {
    const ov = (M.overlap.get(n.id) || []).slice(0, 3);
    return `<b>@${esc(n.label)}${n.is_me ? ' (you)' : ''}</b><span>Seed · ${int(n.degree)} people · ${int(n.vis)} shown</span>${ov.map(([id, s]) => `<span>${int(s)} shared with @${esc(M.byId.get(id)?.label)}</span>`).join('')}`;
  }
  const seeds = (n.seeds || (M.nbr.get(n.id) || []).map((id) => M.byId.get(id)?.label)).filter(Boolean);
  return `<div class="h-top"><b>${esc(n.name || n.handle || n.label)}</b>${fitBadge(n)}</div><span>@${esc(n.handle || n.label)}${n.followers != null ? ' · ' + fmt(n.followers) + ' followers' : ''}${n.status ? ' · ' + esc(slabel(n.status)) : ''}</span>
    ${n.reason ? `<p>${esc(n.reason)}</p>` : ''}${n.note ? `<p class="h-note">${noteIcon(n.note)} ${esc(n.note.length > 120 ? n.note.slice(0, 120) + '…' : n.note)}</p>` : ''}<span>In ${plural(n.L, 'list')}: ${seedList(seeds, 3)}</span>`;
}
function openSeed(n) {
  S.seedCard = n.id; S.open = null; S.person = null;
  $('#detail').hidden = false;
  renderSeedCard(); renderRows();
}
function renderSeedCard() {
  const n = M.byId.get(S.seedCard);
  if (!n) return;
  $('#detail').innerHTML = `
    <div class="d-head"><span class="av lg">${esc(initials(n.label))}</span>
      <div class="who"><b>@${esc(n.label)}</b><span>${n.is_me ? 'You' : 'Seed'}</span></div>
      <button class="d-close" id="d-close" title="Close (esc)">&times;</button></div>
    <div class="d-sec"><span class="muted">Not read as a person yet, so no status or tags. Its bio is read when a list reaches it.</span></div>
    ${seedBlock(n)}`;
}
// The seed part of a panel: shown alone for a seed without a person row, or under that person's own panel.
function seedBlock(n) {
  const ov = M.overlap.get(n.id) || [];
  const mx = Math.max(1, ...ov.map((o) => o[1]));
  const ls = (S.sc?.lists || []).filter((l) => l.seed === n.label);
  const via = canonTag('via @' + n.label);
  return `<div class="d-sec"><h4>${n.is_me ? 'Your account' : 'Seed'}: its lists on the map</h4><div class="d-stats"><div><b>${fmt(n.degree)}</b><span>People</span></div><div><b>${fmt(n.vis)}</b><span>On map</span></div><div><b>${ov.length}</b><span>Overlaps</span></div><div><b>${ls.filter((l) => l.state === 'done').length}/${ls.length || '–'}</b><span>Lists</span></div></div></div>
    <div class="d-sec"><div class="d-links" style="margin-top:0">
      ${via || n.is_me ? `<button class="btn solid" data-sf="${esc(via || 'knows you')}">Filter to ${n.is_me ? 'people who know you' : '@' + esc(n.label)}</button>` : ''}
      <button class="btn" data-only="${esc(n.label)}">seed:@${esc(n.label)}</button>
      <a class="btn" href="https://www.instagram.com/${encodeURIComponent(n.label)}/" target="_blank" rel="noopener">Instagram</a></div></div>
    ${ls.length ? `<div class="d-sec"><h4>Lists</h4><div class="ov">${ls.map((l) => `<span>${esc(ucf(l.direction))}</span><span class="num muted">${int(l.received)}${l.total ? '/' + int(l.total) : ''}</span><span class="state ${esc(l.state)}">${esc(ucf(l.state))}</span>`).join('')}</div></div>` : ''}
    <div class="d-sec"><h4>Shared people</h4>${ov.length ? `<div class="ov">${ov.map(([id, s]) => `<button data-focus="${esc(id)}">@${esc(M.byId.get(id)?.label)}</button><span class="num">${int(s)}</span><span><span class="bar-p done"><i style="width:${(s / mx) * 100}%"></i></span></span>`).join('')}</div>` : '<span class="muted">None</span>'}</div>`;
}
function seedCardClick(e) {
  const sf = e.target.closest('[data-sf]');
  if (sf) { setMode(sf.dataset.sf, 'inc'); filtersChanged(); return; }
  const only = e.target.closest('[data-only]');
  if (only) { S.f.seed = only.dataset.only; filtersChanged(); return; }
  const fo = e.target.closest('[data-focus]');
  if (fo) { const n = M.byId.get(fo.dataset.focus); if (n) { M.select(n); M.centerOn(n, M.k); } }
}
(function mapInput() {
  const c = $('#canvas');
  let drag = null, moved = false;
  const pts = new Map();
  let pinch = null;
  c.addEventListener('pointerdown', (e) => {
    pts.set(e.pointerId, { x: e.offsetX, y: e.offsetY });
    c.setPointerCapture(e.pointerId);
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), k: M.k };
      if (drag?.n) { drag.n.fx = null; drag.n.fy = null; }
      drag = null; return;
    }
    const n = M.at(e.offsetX, e.offsetY);
    drag = { x: e.clientX, y: e.clientY, ox: M.x, oy: M.y, n, gx: n ? (e.offsetX - M.x) / M.k - n.x : 0, gy: n ? (e.offsetY - M.y) / M.k - n.y : 0 };
    moved = false; c.classList.add('drag');
  });
  c.addEventListener('pointermove', (e) => {
    if (pts.has(e.pointerId)) pts.set(e.pointerId, { x: e.offsetX, y: e.offsetY });
    if (pinch && pts.size === 2) {
      const [a, b] = [...pts.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      M.zoomBy((pinch.k * d / pinch.d) / M.k, (a.x + b.x) / 2, (a.y + b.y) / 2);
      return;
    }
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (!moved && Math.hypot(dx, dy) > (e.pointerType === 'mouse' ? 5 : 10)) {
        moved = true;
        M.autoFit = false; $('#hover').hidden = true;
        // Freeze the layout while a node is carried, so nothing else drifts.
        if (drag.n && M.sim) M.sim.stop();
      }
      if (!moved) return;
      if (drag.n) M.dragTo(drag.n, (e.offsetX - M.x) / M.k - drag.gx, (e.offsetY - M.y) / M.k - drag.gy);
      else { M.x = drag.ox + dx; M.y = drag.oy + dy; M.draw(); }
      return;
    }
    if (e.pointerType === 'touch') return;
    const n = M.at(e.offsetX, e.offsetY);
    if (n !== M.hover) { M.hover = n; M.draw(); }
    const h = $('#hover');
    if (!n) { h.hidden = true; c.style.cursor = ''; return; }
    c.style.cursor = 'pointer';
    h.innerHTML = hoverCard(n);
    h.hidden = false;
    const x = Math.min(e.offsetX + 16, M.w - h.offsetWidth - 8), y = Math.min(e.offsetY + 16, M.h - h.offsetHeight - 8);
    h.style.left = Math.max(8, x) + 'px'; h.style.top = Math.max(8, y) + 'px';
  });
  const end = (e) => {
    pts.delete(e.pointerId);
    if (pinch) { if (pts.size < 2) pinch = null; drag = null; return; }
    if (drag && !moved) {
      if (drag.n) M.select(drag.n);
      else if (M.focus) { M.focus = null; M.draw(); }
    } else if (drag?.n) M.relax(drag.n);
    drag = null; c.classList.remove('drag');
  };
  c.addEventListener('pointerup', end);
  c.addEventListener('pointercancel', end);
  c.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse' && M.hover) { M.hover = null; M.draw(); } $('#hover').hidden = true; });
  c.addEventListener('wheel', (e) => {
    e.preventDefault();
    M.zoomBy(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015)), e.offsetX, e.offsetY);
  }, { passive: false });
})();
$('#map-fit').onclick = () => { M.autoFit = false; M.fit(); };
if (window.ResizeObserver) new ResizeObserver(() => { if (S.view === 'map') M.resize(); }).observe($('#stage'));
$('#map-labels').onclick = () => M.toggleLabels();
$('#zoom-in').onclick = () => M.zoomBy(1.4);
$('#zoom-out').onclick = () => M.zoomBy(1 / 1.4);
$('#map-q').addEventListener('input', debounce(() => M.search(), 120));
$('#map-q').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); M.next(); } });
$('#map-scope').addEventListener('click', (e) => {
  const b = e.target.closest('[data-v]');
  if (!b || b.dataset.v === M.scope) return;
  $$('#map-scope button').forEach((x) => x.classList.toggle('on', x === b));
  M.scope = b.dataset.v; M.autoFit = true; M.load();
});

// Server came back: refresh whatever the offline spell left stale or empty.
function reconnected() {
  resetLeads(true); loadFacets(); loadCounts(); loadViews();
  M.stale = true; if (S.view === 'map') M.load();
  if (S.view === 'tags') T.show();
  if (S.open) refreshPerson(S.open);
}

// ---------- boot ----------
applyTheme(store.get('theme', document.documentElement.dataset.theme || 'dark'));
applyDensity(store.get('density', 'comfortable'));
$('#view-work').classList.toggle('work-noside', !S.side);
(function boot() {
  const { view, qs } = parseHash();
  const p = fromQuery(qs);
  if (qs) { S.f = p.f; S.sort = p.sort; }
  $('#sort').value = S.sort; $('#q').value = S.f.q;
  history.replaceState(null, '', hashFor(view));
  renderFilters(); renderTokens();
  resetLeads();
  loadFacets(); loadCounts(); loadScraper(); loadViews();
  setView(view);
})();
setInterval(loadScraper, 3000);
setInterval(() => { if (!document.hidden) { loadCounts(); loadFacets(); } }, 30000);
setInterval(() => { if (S.view === 'leads' && !document.hidden && S.rows.length && $('#scroll').scrollTop < 5 && !S.open && !S.pick.size) resetLeads(true); }, 45000);
setInterval(() => { if (offlineSince) setOnline(false); if (S.view === 'scraper') renderScraper(); else renderStatus(); }, 1000);
