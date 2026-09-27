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
      let detail = null;
      try { detail = await r.json(); if (detail && typeof detail.error === 'string') msg = detail.error; } catch (e) { /* not json */ }
      const err = new Error(msg); err.status = r.status; err.detail = detail; throw err;
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
// Business fit is the profile judgement. The older tier and score are blended priority.
const FITS = ['strong', 'good', 'weak', 'unread'];
const FIT_LABEL = { strong: 'Strong', good: 'Good', weak: 'Weak', unread: 'Unread' };
const PRIORITY_LABEL = { strong: 'High', good: 'Medium', weak: 'Low', unread: 'Unread' };
const TIER_FIT = { hot: 'strong', warm: 'good', cold: 'weak', unread: 'unread' };
const FIT_TIER = { strong: 'hot', good: 'warm', weak: 'cold', unread: 'unread' };
const PRIORITY_TIER = { high: 'hot', medium: 'warm', low: 'cold', unread: 'unread' };
const TIER_PRIORITY = { hot: 'high', warm: 'medium', cold: 'low', unread: 'unread' };
const isFitTag = (t) => /^Fit: /.test(t);
const tagName = (t) => (typeof t === 'string' ? t : t.tag);
const fitOf = (r) => r.business_fit != null ? (r.business_fit >= 70 ? 'strong' : r.business_fit >= 45 ? 'good' : 'weak') : (r.fit || 'unread');
function fitBadge(r, cls = '') {
  const f = fitOf(r);
  const score = r.business_fit != null ? `<b>${esc(r.business_fit)}</b>` : '';
  return `<span class="fit f-${f} ${cls}" title="Business fit from profile evidence${r.business_fit != null ? ': ' + esc(r.business_fit) : ' unavailable'}"><i></i>Fit ${f === 'unread' ? '–' : FIT_LABEL[f]}${score}</span>`;
}
// Current list evidence comes from the API; source tags can lag until requalification.
const viaSeeds = (r) => Array.isArray(r.via) ? r.via : (r.tags || []).map(tagName).filter(isViaTag).map((t) => t.slice(5));
const YOU = { 'mentions you': 'Mentions you', 'knows you': 'Known personally', 'Instagram link': 'Instagram link' };
const youLink = (r) => { const tags = r.tags || []; const names = tags.map(tagName);
  const facts = [];
  if (r.relationship == null) {   // older payloads: fall back to the observed source tags
    const k = ['follows you', 'you follow', 'Instagram link'].find((t) => names.includes(t));
    if (k) facts.push({ 'follows you': 'Follows you', 'you follow': 'You follow', 'Instagram link': 'Instagram link' }[k]);
  }
  if (r.relationship === 'mutual') facts.push('Mutual follow observed');
  else if (r.relationship === 'follows') facts.push('Observed following you');
  else if (r.relationship === 'followed') facts.push('Observed you following');
  if (r.relationship != null && !facts.length && names.includes('Instagram link')) facts.push(YOU['Instagram link']);
  if (names.includes('mentions you')) facts.push(YOU['mentions you']);
  if (tags.some((t) => t.tag === 'knows you' && t.source === 'manual')) facts.push(YOU['knows you']);
  return facts.join(' · ');
};
const seedList = (seeds, n) => seeds.slice(0, n).map((s) => '@' + esc(s)).join(', ') + (seeds.length > n ? ` +${seeds.length - n}` : '');
// ---------- state ----------
const emptyFilter = () => ({ tags: [], any: [], not: [], status: '', tier: '', q: '', min: 0, bio: '', seed: '', follow_up: '', fmin: null, fmax: null });
const S = {
  view: 'leads',
  f: emptyFilter(), sort: store.get('sort', 'fit'),
  tagList: [], tagBy: new Map(), tagLower: new Map(), counts: null, sc: null, views: [],
  rows: [], total: null, done: false, loading: false, error: false, gen: 0, nextOffset: 0, rev: null, stale: false,
  cur: -1, open: null, person: null, seedCard: null,
  pick: new Set(), anchor: -1, picking: false,
  tagMore: {}, tagFind: '', saving: false,
  side: false, fmore: store.get('fmore', false),
};

// ---------- filter <-> query string ----------
function toQuery(f = S.f, sort = S.sort, withSort = true) {
  const p = new URLSearchParams();
  if (f.tags.length) p.set('tags', f.tags.join(','));
  if (f.any.length) p.set('any', f.any.join(','));
  if (f.not.length) p.set('not', f.not.join(','));
  if (f.status) p.set('status', f.status);
  if (f.follow_up) p.set('follow_up', f.follow_up);
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
      follow_up: ['due', 'overdue', 'scheduled', 'completed', 'none'].includes(p.get('follow_up')) ? p.get('follow_up') : '',
      bio: ['0', '1'].includes(p.get('has_bio')) ? p.get('has_bio') : '', seed: (p.get('seed') || '').replace(/^@/, ''), fmin: numOr('followers_min'), fmax: numOr('followers_max') },
    sort: p.get('sort') || 'fit',
  };
}
const filterCount = (f = S.f) => f.tags.length + f.any.length + f.not.length + !!f.status + !!f.follow_up + !!f.tier + !!f.min + !!f.bio + !!f.seed + (f.fmin != null || f.fmax != null) + !!f.q;
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
const parseNum = (s) => {
  const m = String(s).trim().toLowerCase().match(/^(\d+(?:\.\d+)?)([km]?)$/);
  if (!m) return null;
  const n = Math.round(+m[1] * (m[2] === 'k' ? 1e3 : m[2] === 'm' ? 1e6 : 1));
  return Number.isSafeInteger(n) ? n : null;
};

// Token text for a tag. Tags with spaces are quoted.
function tagTok(t, mode) {
  const pre = mode === 'any' ? '~' : mode === 'exc' ? '-' : '';
  if (isViaTag(t) && /^via @[\w.]+$/.test(t)) return pre + 'via:@' + t.slice(5);
  return pre + '#' + (/^[^\s"]+$/.test(t) ? t : '"' + t + '"');
}
function tokens() {
  const out = [];
  for (const mode of MODES) for (const t of S.f[mode === 'inc' ? 'tags' : mode === 'any' ? 'any' : 'not']) out.push({ k: 'tag', tag: t, mode, text: tagTok(t, mode) });
  if (S.f.follow_up) out.push({ k: 'follow_up', text: 'followup:' + S.f.follow_up });
  if (S.f.status) out.push({ k: 'status', text: 'status:' + S.f.status });
  if (S.f.tier) out.push({ k: 'tier', text: 'priority:' + TIER_PRIORITY[S.f.tier] });
  if (S.f.min) out.push({ k: 'min', text: `lists:${S.f.min}+` });
  if (S.f.bio) out.push({ k: 'bio', text: 'bio:' + (S.f.bio === '1' ? 'yes' : 'no') });
  if (S.f.seed) out.push({ k: 'seed', text: 'seed:@' + S.f.seed });
  if (S.f.fmin != null || S.f.fmax != null) out.push({ k: 'fol', text: 'followers:' + folLabel(S.f.fmin, S.f.fmax) });
  return out;
}
const TOKEN_RX = /^[~+|-]?(#|via:)|^(status|fit|priority|lists|bio|seed|followers|followup):/i;
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
  if ((m = w.match(/^followup:(due|overdue|scheduled|completed|none)$/i))) return () => { S.f.follow_up = m[1].toLowerCase(); };
  if ((m = w.match(/^status:(\w+)$/i))) {
    let s = m[1].toLowerCase(); if (s === 'unmarked') s = 'none'; s = LEGACY_STATUS[s] || s;
    if (s && s !== 'open' && s !== 'none' && s !== 'all' && !STATUSES.includes(s)) return null;
    return () => { S.f.status = s === 'open' ? '' : s; };
  }
  if ((m = w.match(/^(priority|fit):(\w+)$/i))) { const t = (m[1].toLowerCase() === 'priority' ? PRIORITY_TIER : FIT_TIER)[m[2].toLowerCase()]; return t ? () => { S.f.tier = t; } : null; }
  if ((m = w.match(/^lists:(\d+)\+?$/i))) {
    const n = Number(m[1]);
    return Number.isSafeInteger(n) ? () => { S.f.min = n > 1 ? n : 0; } : null;
  }
  if ((m = w.match(/^bio:(yes|no|1|0)$/i))) return () => { S.f.bio = /^(yes|1)$/i.test(m[1]) ? '1' : '0'; };
  if ((m = w.match(/^seed:@?([\w.]+)$/i))) return () => { S.f.seed = m[1].toLowerCase(); };
  if ((m = w.match(/^followers:(.+)$/i))) {
    const v = m[1].trim();
    let a = null, b = null, x;
    if ((x = v.match(/^([\d.]+[km]?)\+$/i)) || (x = v.match(/^>=?([\d.]+[km]?)$/i))) a = parseNum(x[1]);
    else if ((x = v.match(/^<([\d.]+[km]?)$/i))) { b = parseNum(x[1]); if (b != null) b -= 1; }
    else if ((x = v.match(/^([\d.]+[km]?)-([\d.]+[km]?)$/i))) {
      a = parseNum(x[1]); b = parseNum(x[2]);
      if (a == null || b == null || a > b) return null;
    }
    else return null;
    if ((a == null && b == null) || (b != null && b < 0)) return null;
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
  $$('.tabs a').forEach((a) => { a.href = hashFor(a.dataset.view); a.classList.toggle('on', a.dataset.view === S.view); if (a.dataset.view === S.view) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
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
$('.skip-link')?.addEventListener('click', (e) => {
  e.preventDefault();
  $('#main-content')?.focus({ preventScroll: true });
});

const WORK_SUB = { leads: 'Everyone the scraper found, business fit first.', map: 'Who is connected to whom. Click a dot to open that person.' };
function setView(v) {
  if (S.open && S.view !== v) noteQueue.flush(S.open).catch(() => {});
  const prev = S.view;
  S.view = v;
  const work = v === 'leads' || v === 'map';
  if ($('#work-count')) $('#work-count').hidden = v !== 'leads';
  $('#view-work').classList.toggle('on', work);
  $('#view-tags').classList.toggle('on', v === 'tags');
  $('#view-qual').classList.toggle('on', v === 'qual');
  $('#view-scraper').classList.toggle('on', v === 'scraper');
  $('#view-accounts').classList.toggle('on', v === 'accounts');
  $('#view-settings').classList.toggle('on', v === 'settings');
  $('#pane-leads').classList.toggle('on', v === 'leads');
  $('#pane-map').classList.toggle('on', v === 'map');
  if (work) { $('#work-h').textContent = v === 'map' ? 'Map' : 'Leads'; $('#work-p').textContent = WORK_SUB[v]; }
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
  if (prev !== v && SCRAPER_FULL_VIEWS.has(v) && !document.hidden) loadScraper();
  if (!work) hideSuggest();
}

// Apply a filter change: URL, sidebar, tokens, list, facets, map.
function filterScopeKey(f = S.f) {
  const canonical = { ...f };
  for (const key of ['tags', 'any', 'not']) canonical[key] = [...new Set(f[key])].sort();
  return toQuery(canonical, S.sort, false).toString();
}
let appliedFilterKey = filterScopeKey(fromQuery(parseHash().qs).f);
function filtersChanged(o = {}) {
  S.f.tags = [...new Set(S.f.tags)]; S.f.any = [...new Set(S.f.any)]; S.f.not = [...new Set(S.f.not)];
  const nextFilterKey = filterScopeKey();
  if (nextFilterKey !== appliedFilterKey) {
    const hadSelection = S.pick.size || S.picking;
    S.pick.clear(); S.anchor = -1;
    if (hadSelection) toast('Selection cleared because filters changed');
  }
  appliedFilterKey = nextFilterKey;
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
  syncLook();
  M.draw();
}
$('#theme-btn').onclick = () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
function applyDensity(d) {
  const sc = $('#scroll'), position = sc.scrollTop / rowH();
  document.documentElement.dataset.density = d;
  $('#density-btn').textContent = d === 'compact' ? 'Comfortable' : 'Compact';
  store.set('density', d);
  syncLook();
  // Resize the scrollable canvas before restoring the same lead and row fraction.
  $('#rows').style.height = S.rows.length * rowH() + 'px';
  sc.scrollTop = position * rowH();
  renderRows();
}
function syncLook() {
  const r = document.documentElement.dataset;
  $$('#set-theme button').forEach((b) => { const on = b.dataset.v === r.theme; b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on)); });
  $$('#set-density button').forEach((b) => { const on = b.dataset.v === (r.density || 'comfortable'); b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on)); });
}
$('#set-theme').onclick = (e) => { const b = e.target.closest('[data-v]'); if (b) applyTheme(b.dataset.v); };
$('#set-density').onclick = (e) => { const b = e.target.closest('[data-v]'); if (b) applyDensity(b.dataset.v); };
$('#density-btn').onclick = () => applyDensity(document.documentElement.dataset.density === 'compact' ? 'comfortable' : 'compact');
const narrow = () => window.innerWidth <= 900;
function toggleSide() {
  if (narrow()) { setDrawer(!$('#filters').classList.contains('show')); return; }
  S.side = !S.side;
  $('#view-work').classList.toggle('work-noside', !S.side);
  syncFilterToggle(); renderRows(); M.resize();
}
function syncFilterToggle() {
  $('#filters-btn').setAttribute('aria-expanded', String(narrow() ? $('#filters').classList.contains('show') : S.side));
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
  try { d = await api.get('/api/tags?' + LeadWorkflow.runtimeQuery(toQuery(S.f, S.sort, false))); } catch (e) { return; }
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
  try { d = await api.get('/api/counts?' + LeadWorkflow.runtimeQuery(toQuery(S.f, S.sort, false))); } catch (e) { return; }
  if (g !== countGen) return;
  S.counts = d;
  $('#n-leads').textContent = fmt(d.total);
  renderFilters();
}
const viewState = { draft: '', busy: false, error: '', generation: 0, deleting: new Set() };
// Keep older browser-only saves intact until their owner explicitly restores them.
const browserViews = store.get('views', []);
const legacyViews = Array.isArray(browserViews) ? browserViews.filter((v) => v && typeof v.name === 'string' && typeof v.query === 'string') : [];
function savedViewQuery(query) {
  const p = fromQuery(query);
  for (const key of ['tags', 'any', 'not']) p.f[key] = [...new Set(p.f[key])].sort();
  return toQuery(p.f, p.sort).toString();
}
function acceptSavedView(view) {
  S.views = [...S.views.filter((v) => String(v.id) !== String(view.id) && v.name !== view.name), view]
    .sort((a, b) => a.name.localeCompare(b.name));
}
async function loadViews() {
  const generation = ++viewState.generation;
  try {
    const views = await api.get('/api/views');
    if (generation !== viewState.generation) return;
    if (!Array.isArray(views)) throw new Error('Invalid views response');
    S.views = views; viewState.error = '';
  } catch (e) {
    if (generation !== viewState.generation) return;
    viewState.error = 'Could not load saved views.';
  }
  renderFilters();
}

// ---------- sidebar ----------
const swatch = (kind, extra = '', grp = '') => `<i class="sw ${KIND[kind] ?? ''} ${extra}${grp ? ' g-' + esc(grp) : ''}"></i>`;
function tagItem(t, label) {
  const m = modeOf(t.tag);
  const n = t.count;
  const title = `${t.tag} · ${t.kind}${t.sources.length > 1 ? ' + ' + t.sources.filter((s) => s !== t.kind).join(', ') : ''}`;
  return `<button class="fi t-${tagTier(t)}${m ? ' ' + m : ''}${!m && !n ? ' zero' : ''}" data-tag="${esc(t.tag)}" title="${esc(title)}" aria-pressed="${!!m}">${swatch(t.kind, t.grp === 'source' ? 'src' : '', t.grp)}<span>${esc(label || t.tag)}</span><b>${fmt(n)}</b></button>`;
}
function tagSection(key, title, list, labelFn) {
  const q = S.tagFind.toLowerCase();
  if (q) list = list.filter((t) => t.tag.toLowerCase().includes(q));
  if (!list.length) return '';
  list = [...list].sort((a, b) => (!!modeOf(b.tag) - !!modeOf(a.tag)) || (b.count > 0) - (a.count > 0) || b.count - a.count || b.total - a.total || a.tag.localeCompare(b.tag));
  const lim = S.tagMore[key] || q ? 500 : 8;
  const shown = list.slice(0, Math.max(lim, list.filter((t) => modeOf(t.tag)).length));
  return `<div class="fsec f-x"><h4>${esc(title)}<span class="grow"></span><span class="num">${list.length}</span></h4>
    ${shown.map((t) => tagItem(t, labelFn && labelFn(t))).join('')}
    ${list.length > shown.length ? `<button class="fmore" data-more="${key}">+${list.length - shown.length} more</button>` : S.tagMore[key] && list.length > 8 ? `<button class="fmore" data-less="${key}">Less</button>` : ''}</div>`;
}
function renderFilters() {
  const nameInput = $('#v-name');
  if (S.saving && nameInput) viewState.draft = nameInput.value;
  const focused = document.activeElement;
  const focus = ['f-find', 'v-name'].includes(focused?.id)
    ? { id: focused.id, start: focused.selectionStart, end: focused.selectionEnd, direction: focused.selectionDirection } : null;
  const c = S.counts || {};
  const qs = savedViewQuery(toQuery().toString());
  const recoverable = legacyViews.map((v, index) => ({ ...v, index })).filter((v) => !S.views.some((saved) => saved.name === v.name && savedViewQuery(saved.query) === savedViewQuery(v.query)));
  const active = filterCount();
  $('#fbtn-n').textContent = active ? ' ' + active : '';
  let h = `<div class="fsec"><h4>Views<span class="grow"></span>${active ? '<button id="f-reset" title="Clear filters (c)">Clear</button>' : ''}<button id="v-new" title="Save view (v)">Save</button></h4>
    ${S.saving ? `<form class="fsave" id="v-form"><input class="input" id="v-name" aria-label="View name" placeholder="View name" autocomplete="off" maxlength="80" value="${esc(viewState.draft)}"><button class="btn solid" ${viewState.busy ? 'disabled' : ''}>${viewState.busy ? 'Saving…' : 'Save'}</button></form>` : ''}
    <button class="fi${qs === '' ? ' on' : ''}" data-view="" aria-pressed="${qs === ''}"><span>Open leads</span></button>
    <button class="fi${LeadDaily.isDueView(S.f, S.sort) ? ' on' : ''}" data-view="${esc(LeadDaily.dueQuery())}" aria-pressed="${LeadDaily.isDueView(S.f, S.sort)}"><span>Due follow-ups</span></button>
    ${viewState.error ? `<span class="fnone" role="status">${esc(viewState.error)}</span><button class="fi" id="v-retry"><span>Retry</span></button>` : ''}
    ${S.views.map((v) => `<button class="fi${savedViewQuery(v.query) === qs ? ' on' : ''}" data-view="${esc(v.query)}" aria-pressed="${savedViewQuery(v.query) === qs}" title="${esc(v.query)}"><span>${esc(v.name)}</span><i class="del" data-vdel="${esc(v.id)}" title="Delete view">&times;</i></button>`).join('')}
    ${recoverable.length ? `<h4>Browser copies</h4><span class="fnone">Open a copy, then Save to restore it.</span>${recoverable.map((v) => `<button class="fi" data-vrecover="${v.index}"><span>${esc(v.name)}</span></button>`).join('')}` : ''}</div>
    <div class="fsec"><h4>Status</h4>
    <button class="fi${!S.f.status ? ' on' : ''}" data-status=""><span>Open</span><b>${fmt(c.open)}</b></button>
    ${STATUSES.map((s) => `<button class="fi${S.f.status === s ? ' on' : ''}${c[s] ? '' : ' zero'}" data-status="${s}" title="${esc(SDESC[s])}"><span>${slabel(s)}</span><b>${c[s] ? fmt(c[s]) : ''}</b></button>`).join('')}
    <button class="fi${S.f.status === 'none' ? ' on' : ''}" data-status="none" title="No status yet"><span>Unmarked</span><b>${fmt(c.none)}</b></button>
    <button class="fi${S.f.status === 'all' ? ' on' : ''}" data-status="all" title="Everyone, including Not a fit"><span>All</span><b>${c.open != null ? fmt(c.open + (c.no || 0)) : ''}</b></button></div>
    <div class="fsec"><h4>Follow-up</h4>${['due', 'overdue', 'scheduled', 'completed', 'none'].map((v) => `<button class="fi${S.f.follow_up === v ? ' on' : ''}" data-follow-up="${v}"><span>${v === 'due' ? 'Due today or earlier' : ucf(v)}</span></button>`).join('')}</div>
    <div class="fsec"><h4>Priority tier</h4>
    ${FITS.map((f) => `<button class="fi${S.f.tier === FIT_TIER[f] ? ' on' : ''}" data-tier="${FIT_TIER[f]}"><i class="fdot f-${f}"></i><span>${PRIORITY_LABEL[f]}</span><b>${c[FIT_TIER[f]] != null ? fmt(c[FIT_TIER[f]]) : ''}</b></button>`).join('')}</div>
    <div class="fsec fsegs f-x"><h4>Shape</h4>
      <div class="fseg"><span>Lists</span><div class="seg">${LIST_OPTS.map(([n, l]) => `<button data-min="${n}" class="${S.f.min === n ? 'on' : ''}">${l}</button>`).join('')}</div></div>
      <div class="fseg"><span>Bio</span><div class="seg">${BIO_OPTS.map(([v, l]) => `<button data-bio="${v}" class="${S.f.bio === v ? 'on' : ''}">${l}</button>`).join('')}</div></div>
      <div class="fseg"><span>Followers</span><div class="seg">${FOL_OPTS.map(([k, l, a, b]) => `<button data-fol="${k}" class="${S.f.fmin === a && S.f.fmax === b ? 'on' : ''}">${l}</button>`).join('')}</div></div>
    </div>
    <div class="fsec f-x"><input class="input ffind" id="f-find" type="search" placeholder="Find tag" value="${esc(S.tagFind)}" autocomplete="off" spellcheck="false"></div>`;
  const tags = S.tagList;
  const own = tags.filter((t) => t.kind === 'manual');
  const rule = tags.filter((t) => t.kind === 'rule');
  const auto = tags.filter((t) => t.kind === 'auto');
  h += tagSection('own', 'Your tags', own);
  if (!own.length && !S.tagFind) h += `<div class="fsec f-x"><h4>Your tags</h4><span class="fnone">None yet</span></div>`;
  h += tagSection('rule', 'Rule tags', rule);
  h += tagSection('via', 'Seeds', auto.filter((t) => isViaTag(t.tag)), (t) => t.tag.slice(4));
  for (const [g, title] of GROUPS) h += tagSection(g, title, auto.filter((t) => t.grp === g));
  h += tagSection('src', 'You', auto.filter((t) => t.grp === 'source' && !isViaTag(t.tag) && !isListTag(t.tag)));
  h += tagSection('other', 'Other', auto.filter((t) => !['source', ...GROUPS.map((g) => g[0])].includes(t.grp)));
  const hid = S.f.tags.length + S.f.any.length + S.f.not.length + !!S.f.min + !!S.f.bio + !!S.f.seed + (S.f.fmin != null || S.f.fmax != null);
  const open = S.fmore || !!S.tagFind || S.saving;
  h += `<div class="f-tog"><button class="fi" id="f-more"><span>${open ? 'Fewer filters' : 'More filters'}</span>${hid && !open ? `<b>${hid} on</b>` : ''}</button>
    ${active ? '<button class="fi" id="f-clear" title="Clear filters (c)"><span>Clear all</span></button>' : ''}</div>`;
  const el = $('#filters');
  el.classList.toggle('xo', open);
  const st = el.scrollTop;
  el.innerHTML = h;
  if (focus) {
    const input = $('#' + focus.id);
    if (input) { input.focus({ preventScroll: true }); input.setSelectionRange(focus.start, focus.end, focus.direction); }
  }
  el.scrollTop = st;
}
$('#filters').addEventListener('click', async (e) => {
  const t = e.target;
  const more = t.closest('[data-more]'), less = t.closest('[data-less]');
  if (more) { S.tagMore[more.dataset.more] = true; return renderFilters(); }
  if (less) { S.tagMore[less.dataset.less] = false; return renderFilters(); }
  if (t.id === 'f-reset' || t.closest('#f-clear')) return clearFilters();
  if (t.closest('#f-more')) { S.fmore = !(S.fmore || S.tagFind || S.saving); S.tagFind = ''; S.saving = false; store.set('fmore', S.fmore); return renderFilters(); }
  if (t.id === 'v-new') { S.saving = !S.saving; renderFilters(); if (S.saving) $('#v-name')?.focus(); return; }
  if (t.closest('#v-retry')) return loadViews();
  const recover = t.closest('[data-vrecover]');
  if (recover) {
    if (viewState.busy) return;
    const view = legacyViews[+recover.dataset.vrecover];
    if (!view) return;
    applyQuery(view.query);
    viewState.draft = view.name;
    if ($('#v-name')) $('#v-name').value = view.name;
    startSaveView(); return;
  }
  const vdel = t.closest('[data-vdel]');
  if (vdel) { e.stopPropagation(); return deleteView(vdel.dataset.vdel); }
  const b = t.closest('button');
  if (!b) return;
  const d = b.dataset;
  if (d.tag != null) { e.preventDefault(); return clickTag(d.tag, e); }
  if (d.view != null) { applyQuery(d.view); if (narrow()) setDrawer(false); return; }
  // Segments toggle: clicking the active option turns it off.
  if (d.status != null) S.f.status = d.status;
  else if (d.followUp != null) S.f.follow_up = S.f.follow_up === d.followUp ? '' : d.followUp;
  else if (d.tier != null) S.f.tier = S.f.tier === d.tier ? '' : d.tier;
  else if (d.min != null) S.f.min = S.f.min === +d.min ? 0 : +d.min;
  else if (d.bio != null) S.f.bio = S.f.bio === d.bio ? '' : d.bio;
  else if (d.fol != null) { const o = FOL_OPTS.find((x) => x[0] === d.fol); const same = S.f.fmin === o[2] && S.f.fmax === o[3]; S.f.fmin = same ? null : o[2]; S.f.fmax = same ? null : o[3]; }
  else return;
  filtersChanged();
});
$('#filters').addEventListener('input', (e) => {
  if (e.target.id === 'v-name') viewState.draft = e.target.value;
  if (e.target.id === 'f-find') { S.tagFind = e.target.value; renderFilters(); }
});
$('#filters').addEventListener('keydown', (e) => {
  if (e.target.id === 'f-find' && e.key === 'Enter') {
    const first = $('#filters [data-tag]');
    if (first) clickTag(first.dataset.tag, e);
  }
  if (e.target.id === 'v-name' && e.key === 'Escape') { S.saving = false; renderFilters(); $('#v-new')?.focus(); }
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
  if (!name || viewState.busy) return;
  const query = toQuery().toString();
  const draft = $('#v-name')?.value ?? name;
  viewState.draft = draft; viewState.busy = true; ++viewState.generation;
  renderFilters();
  try {
    const saved = await api.post('/api/views', { name, query });
    ++viewState.generation;
    acceptSavedView(saved);
    const latest = $('#v-name')?.value ?? viewState.draft;
    if (latest === draft) { S.saving = false; viewState.draft = ''; }
    toast('View saved');
  } catch (e) {
    toast('Could not save view. Try again.');
  } finally {
    viewState.busy = false; renderFilters();
  }
}
async function deleteView(id) {
  const v = S.views.find((x) => String(x.id) === String(id));
  const key = String(id);
  if (!v || viewState.deleting.has(key)) return;
  viewState.deleting.add(key); ++viewState.generation;
  try {
    await api.post(`/api/views/${encodeURIComponent(id)}/delete`);
    ++viewState.generation;
    S.views = S.views.filter((x) => String(x.id) !== key);
    renderFilters();
  } catch (e) {
    toast('Could not delete view. Try again.'); return;
  } finally {
    viewState.deleting.delete(key);
  }
  toast(`Deleted ${v.name}`, () => restoreDeletedView(v));
}
async function restoreDeletedView(view) {
  ++viewState.generation;
  try {
    const restored = await api.post('/api/views', { name: view.name, query: view.query });
    ++viewState.generation; acceptSavedView(restored); renderFilters(); toast('View restored');
  } catch (e) {
    toast('Could not restore view. Try again.', () => restoreDeletedView(view));
  }
}
function startSaveView() {
  if (narrow()) setDrawer(true);
  else if (!S.side) toggleSide();
  S.saving = true; renderFilters(); $('#v-name')?.focus();
}
$('#save-view').onclick = startSaveView;
function setDrawer(open) { $('#filters').classList.toggle('show', open); $('#scrim').hidden = !open; syncFilterToggle(); }
$('#filters-btn').onclick = (e) => { e.stopPropagation(); toggleSide(); };
$('#scrim').onclick = () => setDrawer(false);

// ---------- query bar ----------
function renderTokens() {
  const clear = $('#clear-filters');
  if (clear) { clear.hidden = !filterCount(); clear.onclick = clearFilters; }
  $('#tokens').innerHTML = tokens().map((t, i) => `<span class="tok ${t.k === 'tag' ? t.mode : 'prm'}" data-t="${i}" title="${t.k === 'tag' ? 'Click: all, any, none' : 'Click to edit'}"><span>${esc(t.text)}</span><button class="x" data-rm="${i}" title="Remove" aria-label="Remove ${esc(t.text)} filter">&times;</button></span>`).join('');
  if (document.activeElement !== $('#q') && $('#q').value.trim() !== S.f.q) $('#q').value = S.f.q;
}
function removeToken(t) {
  if (t.k === 'tag') setMode(t.tag, null);
  else if (t.k === 'follow_up') S.f.follow_up = '';
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
  const draft = (inp.value.trim() ? inp.value.trim() + ' ' : '') + t.text;
  filtersChanged();
  inp.value = draft;
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
// Only valid tokens are reserved for filters. Misspelled filters remain searchable text.
const qFree = (v) => words(v).filter((w) => !/^[~+|-]?#$/.test(w) && (!TOKEN_RX.test(w) || !parseToken(w, false))).join(' ').trim();
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
    items = S.tagList.filter((t) => t.grp !== 'source' || t.tag === 'Instagram link' || t.tag === 'mentions you' || isViaTag(t.tag))
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
  else if (/^(?:priority|fit):\w*$/i.test(w)) {
    const legacy = w.toLowerCase().startsWith('fit:');
    items = (legacy ? FITS : Object.keys(PRIORITY_TIER)).filter((f) => ((legacy ? 'fit:' : 'priority:') + f).startsWith(w.toLowerCase()))
      .map((f) => ({ text: 'priority:' + (legacy ? TIER_PRIORITY[FIT_TIER[f]] : f), count: S.counts?.[(legacy ? FIT_TIER : PRIORITY_TIER)[f]] }));
  }
  else if (/^lists:\d*$/i.test(w)) items = ['2+', '3+', '4+', '5+'].map((s) => ({ text: 'lists:' + s }));
  else if (/^bio:\w*$/i.test(w)) items = ['yes', 'no'].map((s) => ({ text: 'bio:' + s }));
  else if (/^followers:\S*$/i.test(w)) items = ['<1k', '1k+', '10k+', '100k+', '1k-10k', '10k-100k'].map((s) => ({ text: 'followers:' + s }));
  else if (w.length >= 2 && /^[a-z]+:?$/i.test(w)) items = ['status:', 'priority:', 'lists:', 'bio:', 'seed:', 'followers:', 'via:@'].filter((k) => k.startsWith(w.toLowerCase())).map((k) => ({ text: k, key: true }));
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
  if (!keep) { S.rows = []; S.total = null; S.cur = -1; S.anchor = -1; $('#scroll').scrollTop = 0; }
  S.nextOffset = 0; S.rev = null; S.stale = false;
  S.done = false; S.error = false;
  // Rendering retained rows must not start an append before this replacement.
  S.loading = true;
  renderRows();
  S.loading = false;
  await loadMore(gen, keep ? Math.max(PAGE, S.rows.length) : PAGE, keep);
}
// Best business fit (sort=fit): profile score first, then blended priority.
function fetchLeads(offset, limit) {
  const p = LeadWorkflow.runtimeQuery(toQuery(S.f, S.sort, false));
  p.set('sort', S.sort); p.set('offset', offset); p.set('limit', limit);
  return api.get('/api/leads?' + p);
}
async function loadMore(gen = S.gen, n = PAGE, replace = false) {
  if (S.loading || S.error || (S.done && !replace)) return;
  S.loading = true;
  try {
    const offset = replace ? 0 : S.nextOffset;
    const d = await fetchLeads(offset, Math.min(500, n));
    if (gen !== S.gen) return;
    S.total = d.total;
    if (offset === 0) S.rev = d.rev ?? null;
    else if (S.rev != null && d.rev != null && d.rev !== S.rev) S.stale = true;
    S.nextOffset = Number.isInteger(d.next_offset) ? d.next_offset : offset + d.rows.length;
    if (replace) {
      const currentId = S.rows[S.cur]?.id, anchorId = S.rows[S.anchor]?.id;
      S.rows = [...new Map(d.rows.map((r) => [r.id, r])).values()];
      S.cur = currentId == null ? -1 : S.rows.findIndex((r) => r.id === currentId);
      S.anchor = anchorId == null ? -1 : S.rows.findIndex((r) => r.id === anchorId);
    } else {
      const seen = new Set(S.rows.map((r) => r.id));
      S.rows.push(...d.rows.filter((r) => { if (seen.has(r.id)) return false; seen.add(r.id); return true; }));
    }
    S.error = false;
    S.done = d.has_more === false || S.nextOffset >= d.total || !d.rows.length;
  } catch (e) {
    if (gen === S.gen) S.error = true;
  } finally {
    if (gen === S.gen) { S.loading = false; renderRows(); }
  }
}
const rowH = () => parseFloat(css('--row')) || 64;
// Importance: top = best-fit markers; key = what makes a lead (AI verdict, product category, decision maker, US);
// min = how they were found and audience size. Everything else sits in between.
const TOP_TAGS = new Set(['AI: Top fit', 'Fit: strong']);
const KEY_TAGS = new Set(['Founder', 'US', 'Fit: good']);
// Verdicts carry the strongest emphasis. Roles, evidence and product categories
// describe a lead; they are not verdicts and should not all look like warnings.
const HERO_TAGS = new Set(['Scout: Strong', 'AI: Top fit', 'Fit: strong']);
const MAYBE_TAGS = new Set(['Scout: Possible', 'Fit: good']);
const DECISION_TAGS = new Set(['Founder', 'AI: Decision maker']);
const ROLE_TAGS = new Set(['Brand', 'Store']);
const PLUS_TAGS = new Set(['AI: Runs ads', 'Shopify', 'Shop Link', 'DTC']);
const MARKET_TAGS = new Set(['AI: US market', 'US', 'US market']);
const FLAG_TAGS = new Set(['Too big', 'Other market', 'Scout: No', 'Not reachable', 'Celebrity']);
const PARTNER_TAGS = new Set(['Agency', 'Freelancer', 'Creative', 'Supplier']);
const SOFT_TAGS = new Set(['Creator', 'Coach', 'Personal', 'SaaS', 'Not DTC', 'Not a brand']);
function tagTier(t) {
  const name = tagName(t);
  // Personal labels stay neutral; adding one is not an automatic fit verdict.
  if (t.source === 'manual' || t.kind === 'manual') return 'own';
  if (FLAG_TAGS.has(name)) return 'flag';
  if (HERO_TAGS.has(name)) return 'hero';
  if (MAYBE_TAGS.has(name)) return 'maybe';
  if (PARTNER_TAGS.has(name)) return 'partner';
  if (SOFT_TAGS.has(name)) return 'review';
  if (DECISION_TAGS.has(name)) return 'decision';
  if (ROLE_TAGS.has(name) || t.grp === 'role') return 'role';
  if (MARKET_TAGS.has(name)) return 'market';
  if (PLUS_TAGS.has(name)) return 'plus';
  if (t.grp === 'niche' || /^AI: (?!Top|Decision|Runs|US|Pre|Early|Grow|Estab)/.test(name)) return 'niche';
  if (t.grp === 'source' || t.grp === 'size' || isViaTag(name)) return 'min';
  return 'ctx';
}
const TIER_ORDER = { hero: 0, decision: 1, flag: 2, maybe: 3, role: 4, plus: 5, market: 6, partner: 7, own: 8, niche: 9, review: 10, ctx: 11, '': 11, min: 12 };
function tagChip(t, rm) {
  const k = KIND[t.source] ?? '';
  const m = modeOf(t.tag);
  const label = isViaTag(t.tag) ? t.tag.slice(4) : t.tag;
  const tier = tagTier(t);
  return `<button class="tag ${k} g-${esc(t.grp || 'custom')}${t.grp === 'source' ? ' src' : ''}${tier ? ' t-' + tier : ''}${m ? ' is-filtered' : ''}" data-tag="${esc(t.tag)}" aria-pressed="${!!m}" title="${esc(t.tag)} · ${esc(t.source)}${m ? ' · filter ' + m : ''}"><span>${esc(label)}</span>${rm ? `<i class="x" data-rmtag="${esc(t.tag)}" title="Remove">&times;</i>` : ''}</button>`;
}
const ORDER = { manual: 0, rule: 1, auto: 2 };
const GORDER = { ai: -1, role: 0, niche: 1, signal: 2, custom: 3, size: 5, source: 6 };
// Tags that describe the person, not how they were found or what the fit badge already says.
function rowTags(r) {
  return (r.tags || []).filter((t) => t.grp !== 'source' && t.grp !== 'size' && !isFitTag(t.tag))
    .sort((a, b) => TIER_ORDER[tagTier(a)] - TIER_ORDER[tagTier(b)] || ORDER[a.source] - ORDER[b.source] || (GORDER[a.grp] ?? 4) - (GORDER[b.grp] ?? 4));
}
function whyHTML(r) {
  if (r.reason) return esc(r.reason);
  const sig = rowTags(r).filter((t) => t.grp === 'signal').slice(0, 3).map((t) => t.tag);
  return sig.length ? esc(sig.join(' · ')) : r.bio ? esc(r.bio) : '<span class="none">No bio</span>';
}
const statHTML = (s) => STATUSES.includes(s) ? `<span class="stat ${s}" title="${esc(SDESC[s])}"><i></i>${slabel(s)}</span>` : '';
const rowFitHTML = (r) => `<span class="row-fit f-${fitOf(r)}" aria-label="Business fit ${r.business_fit == null ? 'unavailable' : esc(r.business_fit)}" title="Business fit ${r.business_fit == null ? 'unavailable' : esc(r.business_fit)} · Priority ${r.score == null ? 'unavailable' : esc(r.score)}"><i></i><b>${r.business_fit == null ? '–' : esc(r.business_fit)}</b></span>`;
// One-click "open on Instagram": a plain link, so the row / map click underneath never fires.
const igLink = (h) => `<a class="ig" data-ig href="https://www.instagram.com/${encodeURIComponent(h)}/" target="_blank" rel="noopener" title="Open on Instagram (o)" aria-label="Open @${esc(h)} on Instagram"><svg viewBox="0 0 16 16" width="13" height="13"><path d="M9.5 2.5h4v4M13.5 2.5 7.5 8.5M11.5 9.5v3a1 1 0 0 1-1 1h-7a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1h3"/></svg></a>`;
const noteIcon = (note) => note ? `<span class="note-ic" title="${esc(note)}" aria-label="Has a note"><svg viewBox="0 0 16 16" width="12" height="12"><path d="M3 2.5h7l3 3v8H3z M10 2.5v3h3 M5.5 8.5h5 M5.5 11h3.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg></span>` : '';
function rowHTML(r, i, h) {
  const n = lists(r);
  const picked = S.pick.has(r.id);
  const cls = ['row', i === S.cur ? 'cur' : '', S.open === r.id ? 'open' : '', picked ? 'picked' : '', r.status === 'no' ? 'st-no' : ''].join(' ');
  const tags = rowTags(r);
  return `<div class="${cls}" data-i="${i}" data-person-id="${r.id}" style="top:${i * h}px">
    <div class="c-sel">${avatar(r.pic, r.name || r.handle)}<button class="ck${picked ? ' on' : ''}" data-ck role="checkbox" aria-checked="${picked}" aria-label="Select @${esc(r.handle)}" title="Select (x)"></button></div>
    <div class="who"><div class="l1"><button class="lead-open" aria-label="Open @${esc(r.handle)}"><b>@${esc(r.handle)}</b></button>${igLink(r.handle)}${noteIcon(r.note)}${r.follow_up ? `<span class="followup-chip" title="${esc(r.follow_up.note || 'Follow-up')}">${r.follow_up.completed_at ? 'Done' : r.follow_up.due_on < LeadWorkflow.localToday() ? 'Overdue' : 'Follow-up'} ${esc(r.follow_up.due_on)}</span>` : ''}${r.name && r.name !== r.handle ? `<span>${esc(r.name)}</span>` : ''}</div><div class="why">${whyHTML(r)}</div><div class="row-mobile-tags">${tags.slice(0, 2).map((t) => tagChip(t)).join('')}${tags.length > 2 ? `<span class="more">+${tags.length - 2}</span>` : ''}</div></div>
    <div class="tags c-tags">${tags.slice(0, 3).map((t) => tagChip(t)).join('')}${tags.length > 3 ? `<span class="more" title="${esc(tags.slice(3).map((t) => t.tag).join(' · '))}">+${tags.length - 3}</span>` : ''}</div>
    <span class="num r fol c-fol">${fmt(r.followers)}</span>
    <div class="c-fit">${rowFitHTML(r)}</div>
    <span class="c-st">${statHTML(r.status)}</span>
    <div class="mnum"><span class="num">${fmt(r.followers)} followers</span>${rowFitHTML(r)}</div>
  </div>`;
}
function renderRows() {
  const active = document.activeElement;
  const activeRow = active?.closest('#rows .row');
  const focusId = activeRow?.dataset.personId;
  const focusKey = active?.matches('[data-ck]') ? '[data-ck]' : active?.matches('.lead-open') ? '.lead-open' : active?.matches('[data-ig]') ? '[data-ig]' : active?.hasAttribute('data-tag') ? `[data-tag="${CSS.escape(active.dataset.tag)}"]` : null;
  const box = $('#rows'), sc = $('#scroll'), h = rowH();
  if ($('#work-count')) $('#work-count').textContent = S.total == null ? '' : int(S.total);
  $('#count').textContent = S.total == null ? '' : plural(S.total, 'person', 'people') + (S.pick.size ? ` · ${int(S.pick.size)} selected` : '');
  $('#pane-leads').classList.toggle('selecting', S.pick.size > 0);
  renderBulk();
  $('#export-selected').disabled = exporting || !S.pick.size;
  const loadedPicked = S.rows.length && S.rows.every((r) => S.pick.has(r.id));
  $('#sel-page').setAttribute('role', 'checkbox');
  $('#sel-page').setAttribute('aria-label', 'Select all loaded leads');
  $('#sel-page').setAttribute('aria-checked', loadedPicked ? 'true' : S.pick.size ? 'mixed' : 'false');
  $('#sel-page').className = 'ck' + (loadedPicked ? ' on' : S.pick.size ? ' part' : '');
  if (!S.rows.length) {
    box.style.height = '100%';
    if (S.loading || (S.total == null && !S.error)) {
      box.innerHTML = Array.from({ length: 14 }, (_, i) => `<div class="skel" style="top:${i * h}px"><i></i><i style="width:${120 + (i * 37) % 80}px"></i><i style="width:${200 + (i * 53) % 160}px"></i></div>`).join('');
    } else if (S.error) {
      box.innerHTML = `<div class="empty"><b>${offlineSince ? 'Server offline' : 'Could not load leads'}</b><p>Check your connection, then try again.</p><button class="btn" id="retry">Retry</button></div>`;
    } else {
      const filtered = filterCount();
      box.innerHTML = `<div class="empty"><b>${filtered ? 'No matches' : 'No leads yet'}</b><p>${filtered ? 'Try a different search or clear your filters.' : 'Add an Instagram account to start finding people.'}</p>${filtered ? '<button class="btn" id="clear-all">Clear filters <kbd>c</kbd></button>' : '<a class="btn" href="#/scraper">Add seeds</a>'}</div>`;
    }
    return;
  }
  box.style.height = (S.rows.length * h + (S.error || S.stale ? Math.max(h, 64) : 0)) + 'px';
  const from = Math.max(0, Math.floor(sc.scrollTop / h) - 8);
  const to = Math.min(S.rows.length, Math.ceil((sc.scrollTop + sc.clientHeight) / h) + 8);
  let out = '';
  for (let i = from; i < to; i++) out += rowHTML(S.rows[i], i, h);
  if (S.error) out += `<div class="page-retry" style="top:${S.rows.length * h}px" role="status"><span>Could not load more leads.</span><button class="btn" id="retry-more">Retry</button></div>`;
  else if (S.stale) out += `<div class="page-retry" style="top:${S.rows.length * h}px" role="status"><span>Results changed while you browsed.</span><button class="btn" id="refresh-leads">Refresh</button></div>`;
  box.innerHTML = out;
  if (focusId && focusKey) {
    const target = box.querySelector(`[data-person-id="${CSS.escape(focusId)}"] ${focusKey}`);
    if (target) target.focus({ preventScroll: true });
    else { sc.tabIndex = -1; sc.focus({ preventScroll: true }); }
  }
  if (!S.error && !S.done && to >= S.rows.length - 20) loadMore();
}
$('#scroll').addEventListener('scroll', () => requestAnimationFrame(renderRows), { passive: true });
window.addEventListener('resize', debounce(() => { syncFilterToggle(); renderRows(); M.resize(); }, 60));
$('#rows').addEventListener('click', (e) => {
  if (e.target.id === 'retry') return resetLeads();
  if (e.target.closest('#retry-more')) { S.error = false; return loadMore(); }
  if (e.target.closest('#refresh-leads')) return resetLeads();
  if (e.target.closest('#clear-all')) return clearFilters();
  if (e.target.closest('[data-ig]')) { e.stopPropagation(); return; }
  const row = e.target.closest('.row');
  if (!row) return;
  const i = +row.dataset.i;
  const tag = e.target.closest('[data-tag]');
  if (tag) { e.preventDefault(); return clickTag(tag.dataset.tag, e); }
  if (e.target.closest('[data-ck]') || e.target.closest('.c-sel') || e.metaKey || e.ctrlKey) { e.preventDefault(); return togglePick(i, e.shiftKey); }
  if (e.shiftKey) { e.preventDefault(); window.getSelection()?.removeAllRanges(); return rangePick(i); }
  select(i);
  openDetail(S.rows[i].id, { keyboard: e.detail === 0 });
});
$('#sel-page').onclick = () => {
  const all = S.rows.length && S.rows.every((r) => S.pick.has(r.id));
  if (all || S.pick.size) S.pick.clear(); else S.rows.forEach((r) => S.pick.add(r.id));
  renderRows();
};

function select(i, scroll) {
  if (!S.rows.length) return;
  const moveFocus = scroll && document.activeElement?.closest('#rows .row');
  S.cur = Math.max(0, Math.min(S.rows.length - 1, i));
  if (scroll) {
    const sc = $('#scroll'), h = rowH(), top = S.cur * h;
    if (top < sc.scrollTop) sc.scrollTop = top;
    else if (top + h > sc.scrollTop + sc.clientHeight) sc.scrollTop = top + h - sc.clientHeight;
  }
  renderRows();
  if (moveFocus) $(`#rows [data-person-id="${S.rows[S.cur].id}"] .lead-open`)?.focus({ preventScroll: true });
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
  if (S.stale) { toast('Results changed. Refresh the list before selecting everyone.'); return; }
  S.picking = true; renderBulk();
  const ids = [...new Set(S.rows.map((r) => r.id))].slice(0, 5000);
  const gen = S.gen;
  try {
    const total = S.total ?? 0;
    if (total > 5000) toast('Selecting the first 5,000');
    let offset = S.nextOffset;
    let changed = false;
    while (ids.length < Math.min(total, 5000)) {
      const d = await fetchLeads(offset, 500);
      if (gen !== S.gen || !d.rows.length) break;
      if (S.rev != null && d.rev != null && d.rev !== S.rev) { S.stale = true; changed = true; toast('Results changed. Refresh the list before selecting everyone.'); break; }
      offset += d.rows.length;
      const seen = new Set(ids);
      for (const r of d.rows) if (ids.length < 5000 && !seen.has(r.id)) { ids.push(r.id); seen.add(r.id); }
      if (offset >= d.total) break;
    }
    if (gen === S.gen && !changed) S.pick = new Set(ids);
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
  const key = [n, S.total, S.picking, [...S.pick].join(',')].join('|');
  if (key === bulkKey && !el.hidden) return;
  bulkKey = key;
  const focused = document.activeElement && el.contains(document.activeElement) ? document.activeElement.id : null;
  const addVal = $('#bk-add')?.value || '';
  const counts = new Map();
  S.rows.forEach((r) => { if (S.pick.has(r.id)) (r.tags || []).forEach((t) => { if (t.source === 'manual') counts.set(t.tag, (counts.get(t.tag) || 0) + 1); }); });
  const rm = [...counts].sort((a, b) => b[1] - a[1]);
  el.innerHTML = `<b>${int(n)} selected</b>
    ${S.total > n ? `<button class="link" id="bk-all"${S.picking ? ' disabled' : ''}>${S.picking ? 'Selecting…' : `Select first ${int(Math.min(S.total, 5000))}`}</button>` : ''}
    <form id="bk-form" style="display:contents"><input class="input" id="bk-add" list="tag-dl" placeholder="Add tag" autocomplete="off" value="${esc(addVal)}"><button class="btn">Tag</button></form>
    ${rm.length ? `<select class="select" id="bk-rm" title="Remove tag"><option value="">Remove tag</option>${rm.map(([t, c]) => `<option value="${esc(t)}">${esc(t)} (${c})</option>`).join('')}</select>` : ''}
    <span class="sep"></span>
    <span class="st-b">${STATUSES.map((s, i) => `<button data-bs="${s}" title="${esc(SDESC[s])} (${i + 1})">${slabel(s)}</button>`).join('')}<button data-bs="" title="Clear status (0)">Clear</button></span>
    <span class="grow"></span>
    <button class="btn" id="bk-x" title="Clear selection (esc)">&times;</button>`;
  el.hidden = false;
  if (focused) $('#' + focused)?.focus();
}
function clearBulkSelection() {
  const i = S.rows[S.cur] && S.pick.has(S.rows[S.cur].id) ? S.cur : S.rows.findIndex((r) => S.pick.has(r.id));
  clearPick();
  if (S.view === 'leads' && i >= 0) {
    select(i, true);
    $(`#rows [data-person-id="${S.rows[i].id}"] .lead-open`)?.focus({ preventScroll: true });
  } else if (S.view === 'leads') $('#q').focus({ preventScroll: true });
}
$('#bulk').addEventListener('click', (e) => {
  if (e.target.id === 'bk-all') return pickAllInFilter();
  if (e.target.id === 'bk-x') return clearBulkSelection();
  const s = e.target.closest('[data-bs]');
  if (s) bulk({ status: s.dataset.bs || null });
});
$('#bulk').addEventListener('submit', (e) => {
  e.preventDefault();
  const v = $('#bk-add').value.trim();
  if (v) { $('#bk-add').value = ''; $('#bk-add').blur(); bulk({ add: [v] }); }
});
$('#bulk').addEventListener('change', (e) => { if (e.target.id === 'bk-rm' && e.target.value) bulk({ remove: [e.target.value] }); });
$('#bulk').addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); clearBulkSelection(); } });

async function bulk(op, ids = [...S.pick], quiet) { return serializeMutation(() => bulkNow(op, ids, quiet)); }
let mutationQueue = Promise.resolve();
function serializeMutation(fn) {
  const run = mutationQueue.then(fn, fn);
  mutationQueue = run.catch(() => {});
  return run;
}
async function bulkNow(op, ids, quiet) {
  if (!ids.length) return;
  ids = [...new Set(ids)];
  if (ids.length > 5000) { toast('Select up to 5,000 people at once'); return; }
  const body = { ids };
  if (op.add) body.add = op.add;
  if (op.remove) body.remove = op.remove;
  if ('status' in op) body.status = op.status;
  // Remember previous status per id for undo.
  const prevStatus = new Map();
  if ('status' in op) S.rows.forEach((r) => { if (ids.includes(r.id)) prevStatus.set(r.id, r.status || null); });
  let result;
  try { result = await api.post('/api/people/bulk', body); }
  catch (e) { toast(e.status === 400 ? ucf(e.message) : 'Could not save'); return; }
  const updated = Number.isInteger(result?.updated) ? result.updated : null;
  if (updated === null) { resetLeads(true); loadFacetsSoon(); loadCounts(); if (!quiet) toast('Saved. Refreshing to confirm how many people changed. Undo unavailable for this change.'); return; }
  if (updated !== ids.length) {
    resetLeads(true); loadFacetsSoon(); loadCounts();
    if (!quiet) toast(`${int(updated)} of ${int(ids.length)} people updated. List refreshed.`);
    return;
  }
  const set = new Set(ids);
  S.rows.forEach((r) => {
    if (!set.has(r.id)) return;
    if ('status' in op) r.status = op.status;
    if (op.add) op.add.forEach((t) => { if (!(r.tags || []).some((x) => x.tag === t)) r.tags = [...(r.tags || []), { tag: t, grp: S.tagBy.get(t)?.grp || 'custom', source: 'manual' }]; });
    if (op.remove) r.tags = (r.tags || []).filter((x) => !op.remove.includes(x.tag) || x.source !== 'manual');
  });
  if (S.person && set.has(S.person.id)) refreshPerson(S.person.id);
  bulkKey = ''; renderRows(); loadFacetsSoon(); loadCounts();
  M.patch(ids, op);
  if (quiet) return;
  const what = 'status' in op ? (op.status ? `marked ${slabel(op.status)}` : 'status cleared') : op.add ? `tagged ${op.add[0]}` : `untagged ${op.remove[0]}`;
  const canUndo = 'status' in op && prevStatus.size === ids.length;
  toast(`${ucf(plural(ids.length, 'person', 'people'))} ${what}${canUndo ? '' : '. Undo unavailable for this change.'}`, canUndo ? () => {
      const by = new Map();
      prevStatus.forEach((s, id) => { const k = s || ''; by.set(k, [...(by.get(k) || []), id]); });
      by.forEach((g, s) => bulk({ status: s || null }, g, true));
  } : undefined);
}

// ---------- marking ----------
function mark(id, status) { return serializeMutation(() => markNow(id, status)); }
async function markNow(id, status) {
  const r = S.rows.find((x) => x.id === id) || (S.person?.id === id ? S.person : null);
  const prev = r ? r.status : null;
  patchRow(id, { status });
  try { await api.post(`/api/person/${id}/mark`, { status }); loadCounts(); refreshActivity(id); }
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
async function openDetail(id, { keyboard = false } = {}) {
  if (S.open && S.open !== id) noteQueue.flush(S.open).catch(() => {});
  detailAccess.open();
  S.open = id; S.seedCard = null;
  const base = S.rows.find((r) => r.id === id);
  const node = M.byId.get('p:' + id);
  S.person = base ? { ...base, loading: true } : node ? { id, handle: node.handle, name: node.name, followers: node.followers, lists: node.lists, status: node.status, tags: [], loading: true } : { id, loading: true, handle: '', tags: [] };
  $('#detail').hidden = false;
  renderDetail(); renderRows(); M.draw();
  detailAccess.sync();
  await refreshPerson(id);
}
async function refreshPerson(id) {
  try {
    const p = await api.get('/api/person/' + id);
    if (S.open !== id) return;
    noteQueue.reconcile(id, p.note || '');
    S.person = p;
    const r = S.rows.find((x) => x.id === id);
    if (r) Object.assign(r, { tags: p.tags, status: p.status, tier: p.tier, score: p.score, business_fit: p.business_fit,
      connection_strength: p.connection_strength, reason: p.reason, note: p.note, mark_rev: p.mark_rev });
  } catch (e) {
    if (S.open !== id || !S.person) return;
    S.person.loading = false; S.person.failed = true;
  }
  renderDetail(); renderRows();
}
function closeDetail() {
  if (S.open) noteQueue.flush(S.open).catch(() => {});
  S.open = null; S.person = null; S.seedCard = null;
  $('#detail').hidden = true;
  M.focus = null;
  renderRows(); M.resize();
  detailAccess.close();
}
// One row per seed; both directions read as mutual.
function seedEdges(edges) {
  const m = new Map();
  for (const e of edges) { if (!m.has(e.seed)) m.set(e.seed, new Set()); if (e.direction) m.get(e.seed).add(e.direction); }
  return [...m];
}
const modelLabel = (m) => (!m ? '' : m === 'rules' ? 'Rule-based' : String(m).split('/').pop().replace(/:free$/, ''));
// The Hermes leadscout's final read: verdict, two sentences and the pages it used.
function scoutHTML(sc) {
  if (!sc) return '';
  const verified = sc.verified !== false;
  const label = sc.stale ? 'Older read · unverified' : !verified ? 'Unverified candidate' : ({ strong: 'Strong lead', possible: 'Possible lead', no: 'Not a lead' }[sc.verdict] || sc.verdict);
  const cls = sc.stale || !verified || sc.verdict === 'no' || !sc.reachable ? 't-flag' : sc.verdict === 'strong' ? 't-hero' : 't-plus';
  return `<div class="d-sec"><h4>Leadscout<span class="grow"></span><span class="tag ${cls}"><span>${esc(label)}${sc.stale || sc.reachable ? '' : ' · not reachable'}</span></span></h4>
    ${sc.stale ? '<p class="muted">This check is older than the current profile. Its verdict needs a new review.</p>' : !verified ? '<p class="muted">The cited evidence could not be checked. The earlier score remains in place; Leadscout will retry later.</p>' : ''}
    <p class="d-reason">${esc(sc.summary || '')}</p>
    ${sc.sources?.length ? `<div class="d-links">${sc.sources.slice(0, 5).map((u) => { const h = (() => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch { return u; } })(); return safeUrl(u) ? `<a class="btn" href="${esc(safeUrl(u))}" target="_blank" rel="noopener noreferrer">${esc(h)}</a>` : ''; }).join('')}</div>` : ''}</div>`;
}
function websiteEvidence(site) {
  if (!site) return '';
  const url = safeUrl(site.final_url) || safeUrl(site.url);
  const tags = !site.stale && Array.isArray(site.tags) ? site.tags.filter((t) => typeof t === 'string') : [];
  return `<section class="ql-site"><div class="ql-sh"><b>Website evidence</b>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(site.title || url)}</a>` : ''}${site.at ? `<em>Checked ${esc(site.at.slice(0, 10))}</em>` : ''}</div>
    ${site.stale ? '<p class="muted">Older website evidence. Check the site again before using these claims.</p>' : `${site.summary ? `<p>${esc(site.summary)}</p>` : ''}${tags.length ? `<div class="ql-facts">${tags.map((t) => `<span>${esc(t)}</span>`).join('')}</div>` : ''}`}
    ${site.error ? `<p class="muted">${esc(ucf(site.error))}</p>` : ''}</section>`;
}
const evidenceOf = (v) => (Array.isArray(v?.evidence) ? v.evidence : []).filter((q) => typeof q === 'string' && q.trim());
const detailViewState = new Map();
function detailView(id) {
  if (!detailViewState.has(id)) detailViewState.set(id, { sections: {}, tag: '' });
  return detailViewState.get(id);
}
function rememberDetailView() {
  $('#detail').querySelectorAll('details[data-detail-section][data-owner]').forEach((el) => {
    detailView(+el.dataset.owner).sections[el.dataset.detailSection] = el.open;
  });
  const tag = $('#tag-in');
  if (tag?.dataset.owner) detailView(+tag.dataset.owner).tag = tag.value;
}
function detailProfileState(p) {
  const state = p.profile_read?.state === 'reading' ? 'reading' : p.profile_read_pending ? 'queued' : p.profile_read?.state || '';
  const pending = state === 'queued' || state === 'reading';
  const message = state === 'queued' ? 'Profile refresh queued' : state === 'reading' ? 'Reading profile' : state === 'failed' ? 'Could not refresh profile. Open Profile details to retry.' : '';
  const source = { extension: 'Instagram browser', tab: 'Instagram browser', meta_bd: 'Profile lookup' }[p.bio_src] || (p.bio_src ? 'Other profile source' : 'Source not recorded');
  return { pending, failed: state === 'failed', message, source, button: pending ? state === 'reading' ? 'Reading profile…' : 'Refresh queued' : state === 'failed' ? 'Retry profile read' : p.bio_at ? 'Refresh profile' : 'Read profile' };
}
async function retryDetail(id) {
  if (S.person?.id !== id || S.person.loading) return;
  S.person.loading = true; S.person.failed = false;
  renderDetail();
  await refreshPerson(id);
}
$('#detail').addEventListener('toggle', (e) => {
  const el = e.target;
  if (el.matches('details[data-detail-section][data-owner]')) detailView(+el.dataset.owner).sections[el.dataset.detailSection] = el.open;
}, true);
$('#detail').addEventListener('input', (e) => {
  if (e.target.id === 'tag-in' && e.target.dataset.owner) detailView(+e.target.dataset.owner).tag = e.target.value;
});
$('#detail').addEventListener('click', (e) => {
  if (e.target.closest('#d-retry') && S.person) retryDetail(S.person.id);
});
function renderDetail() {
  rememberDetailView();
  if (S.seedCard) return renderSeedCard();
  const p = S.person;
  if (!p) return;
  const v = p.verdict || {};
  const edges = p.edges || (p.via || []).map((s) => ({ seed: s }));
  const oldEdges = (p.edge_history || []).filter((e) => e.state !== 'observed');
  const n = p.lists != null ? lists(p) : new Set(edges.map((e) => e.seed)).size;
  const tags = (p.tags || []).filter((t) => !(t.tag === 'knows you' && t.source !== 'manual') &&
    (t.grp !== 'source' || t.source === 'manual' || t.tag === 'Instagram link' || t.tag === 'mentions you'))
    .sort((a, b) => ORDER[a.source] - ORDER[b.source] || (GORDER[a.grp] ?? 4) - (GORDER[b.grp] ?? 4));
  const url = safeUrl(p.website);
  const site = p.website ? String(p.website).replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, '') : '';
  const panel = $('#detail');
  const detailFocus = detailAccess.capture();
  const view = detailView(p.id);
  const tagVal = view.tag;
  const noteVal = noteQueue.peek(p.id)?.draft ?? p.note ?? '';
  rememberWorkflowForm();
  const have = new Set((p.tags || []).map((t) => t.tag));
  const quick = (S.tagList || []).filter((t) => t.grp !== 'source' && !have.has(t.tag)).sort((a, b) => b.total - a.total).slice(0, 6);
  const reason = p.reason || v.reason;
  const ev = evidenceOf(v);
  const you = p.relationship === 'mutual' ? 'You follow each other (seen)' : p.relationship === 'follows' ? 'They follow you (seen)' : p.relationship === 'followed' ? 'You follow them (seen)' : youLink(p);
  const role = p.role || v.role;
  const profile = detailProfileState(p);
  const bio = p.bio ? esc(p.bio) : p.loading ? 'Loading profile…' : p.failed ? 'Profile could not be loaded.' : p.bio_at ? 'No bio on this profile.' : 'Profile has not been read yet.';
  panel.dataset.owner = String(p.id);
  const connections = seedEdges(edges);
  const connectionText = (seed, directions) => directions.size > 1 ? 'Follow each other (seen)' : directions.has('followers') ? `They follow @${seed} (seen)` : directions.has('following') ? `@${seed} follows them (seen)` : 'Seen in a list';
  panel.innerHTML = `
    <div class="d-head">${avatar(p.pic, p.name || p.handle, 'lg')}
      <div class="who"><b id="d-person-title" tabindex="-1">${esc(p.name || p.handle || '…')}</b><span>@${esc(p.handle)}${role ? ' · ' + esc(ucf(role)) : ''}</span></div>
      <button class="d-close" id="d-close" aria-label="Close lead details" title="Close (esc)">&times;</button></div>
    ${p.failed ? '<div class="d-sec"><p class="bad" role="status">Could not load this lead.</p><button class="btn" id="d-retry">Retry</button></div>' : ''}
    <div class="d-primary">
      ${p.handle ? `<a class="btn solid d-instagram" href="https://www.instagram.com/${encodeURIComponent(p.handle)}/" target="_blank" rel="noopener">Open Instagram ↗</a>` : ''}
      <span class="d-follower-count"><b>${fmt(p.followers)}</b> followers</span>
      ${url ? `<a class="d-website" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Website ↗</a>` : ''}
    </div>
    <div class="d-sec d-fit">
      <div class="d-fit-h">${p.loading ? '' : fitBadge(p, 'lg')}${p.score == null ? '' : `<span class="muted">Priority ${esc(p.score)}</span>`}</div>
      ${reason ? `<p class="d-reason">${esc(reason)}</p>` : !p.loading ? '<p class="d-reason muted">No qualification yet</p>' : ''}
    </div>
    <section class="d-sec d-tags-section"><h4>Tags</h4>
      <div class="d-tags">${tags.length ? tags.map((t) => `<span class="d-tag-item">${tagChip(t)}${t.source === 'manual' ? `<button type="button" class="d-tag-remove" data-rmtag="${esc(t.tag)}" aria-label="Remove ${esc(t.tag)} tag" title="Remove ${esc(t.tag)}">×</button>` : ''}</span>`).join('') : '<span class="muted">No tags yet</span>'}</div>
      <form class="tag-add" id="tag-form"><input class="input" id="tag-in" data-owner="${p.id}" aria-label="Add a tag" list="tag-dl" placeholder="Add a tag" autocomplete="off" value="${esc(tagVal)}"><button class="btn" type="submit">Add tag</button></form>
      ${quick.length ? `<div class="quick-tags" aria-label="Suggested tags">${quick.slice(0, 4).map((t) => `<button class="qt" data-addtag="${esc(t.tag)}" title="Add ${esc(t.tag)}">+ ${esc(t.tag)}</button>`).join('')}</div>` : ''}
    </section>
    <section class="d-sec d-status-section"><h4>Status</h4><div class="marks">${STATUSES.map((s, i) => `<button id="d-status-${s}" data-s="${s}" aria-pressed="${p.status === s}" aria-label="${esc(slabel(s))}: ${esc(SDESC[s])}" class="${s}${p.status === s ? ' on' : ''}"><i></i><b>${slabel(s)}</b><span>${esc(SDESC[s])}</span><kbd>${i + 1}</kbd></button>`).join('')}</div></section>
    <section class="d-sec d-note-section"><h4><label for="note">Note</label><span class="grow"></span><span class="d-note" id="note-st" role="status" aria-live="polite">${esc(noteStatus(p.id))}</span></h4><textarea class="input" id="note" data-id="${p.id}" aria-describedby="note-st" ${p.loading || p.failed ? 'disabled' : ''} placeholder="How you know them or what to do next…">${esc(noteVal)}</textarea></section>
    ${workflowSummaryHTML(p)}
    <details class="d-sec d-disclosure" data-detail-section="connections" data-owner="${p.id}" ${view.sections.connections ? 'open' : ''}><summary>Connections <span class="num">${n ? plural(n, 'list') : 'None'}</span></summary>
      ${you ? `<p class="d-connection-you">${esc(you)}</p>` : ''}
      <div class="edges">${connections.length ? connections.map(([seed, directions]) => `<button data-seed="${esc(seed)}" title="Filter by @${esc(seed)}"><b>@${esc(seed)}</b><span>${esc(connectionText(seed, directions))}</span></button>`).join('') : '<span class="muted">No recent list evidence</span>'}</div>
      ${oldEdges.length ? `<p class="muted d-history">${oldEdges.length} earlier list observations are unverified or no longer present.</p>` : ''}
    </details>
    <details class="d-sec d-disclosure" data-detail-section="profile" data-owner="${p.id}" ${view.sections.profile ? 'open' : ''}><summary id="d-profile-summary">Profile and evidence</summary>
      <div class="d-bio${p.bio ? '' : ' muted'}">${bio}</div>
      <div class="d-stats"><div><b>${fmt(p.followers)}</b><span>Followers</span></div><div><b>${fmt(p.following)}</b><span>Following</span></div><div><b>${fmt(p.posts)}</b><span>Posts</span></div></div>
      ${site && !url ? `<p class="muted">${esc(site)}</p>` : ''}
      <p class="muted profile-freshness">${p.bio_at ? 'Profile read ' + esc(new Date(p.bio_at).toLocaleDateString()) : 'Profile not read yet'} · ${esc(profile.source)}</p>
      ${profile.message ? `<p class="${profile.failed ? 'bad' : 'muted'} profile-freshness" role="status">${esc(profile.message)}</p>` : ''}
      ${!p.loading && !p.failed ? `<button class="btn" id="d-read" ${profile.pending ? 'disabled' : ''}>${esc(profile.button)}</button>` : ''}
      ${ev.length ? `<ul class="evidence">${ev.map((q) => `<li>${esc(q)}</li>`).join('')}</ul>` : ''}
      ${websiteEvidence(p.site)}
      ${scoutHTML(p.scout)}
    </details>
    <details class="d-sec d-disclosure" data-detail-section="activity" data-owner="${p.id}" ${view.sections.activity ? 'open' : ''}><summary>Activity</summary>${workflowHTML(p)}</details>`;
  const sn = M.seeds?.find((x) => x.pid === p.id);
  if (sn) $('#detail').insertAdjacentHTML('beforeend', `<div class="d-seed">${seedBlock(sn)}</div>`);
  wireWorkflow(p);
  detailAccess.restore(detailFocus);
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
    e.target.disabled = true;
    try { await api.post(`/api/person/${p.id}/read`); if (S.person?.id === p.id) { S.person.profile_read_pending = true; renderDetail(); } toast('Profile refresh queued'); } catch (err) { e.target.disabled = false; toast('Could not queue: ' + err.message); }
  }
});
$('#detail').addEventListener('submit', (e) => {
  e.preventDefault();
  if (e.target.id !== 'tag-form') return;
  const v = $('#tag-in').value.trim();
  if (v && S.person) { $('#tag-in').value = ''; editTags(S.person.id, [v], []); }
});
$('#detail').addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || e.defaultPrevented || e.isComposing || !LeadAccessibility.isEditableTarget(e.target)) return;
  e.stopPropagation();
  (document.activeElement || e.target).blur();
  if (detailAccess.isModal()) detailAccess.focusInitial();
});
const noteQueue = LeadWorkflow.createNoteQueue({
  initial: store.get('note-drafts', {}),
  persist: (drafts) => store.set('note-drafts', drafts),
  save: async (id, note) => {
    await api.post(`/api/person/${id}/mark`, { note });
    const r = S.rows.find((x) => x.id === id), n = M.byId.get('p:' + id);
    if (r) r.note = note || null;
    if (n) n.note = note || null;
    if (S.person?.id === id) { S.person.note = note; refreshActivity(id); }
    renderRows();
  },
  change: (id) => { if (S.person?.id === id && $('#note-st')) $('#note-st').textContent = noteStatus(id); },
});
function noteStatus(id) { const e = noteQueue.peek(id); if (!e) return 'Saves as you type'; return e.error ? 'Not saved — draft kept. Edit to retry.' : e.pending || e.draft !== e.saved ? 'Saving…' : 'Saved'; }
$('#detail').addEventListener('input', (e) => {
  if (e.target.id === 'note' && S.person) noteQueue.edit(S.person.id, e.target.value, S.person.note || '');
});
window.addEventListener('beforeunload', (e) => { if (noteQueue.dirty()) { noteQueue.flushAll().catch(() => {}); e.preventDefault(); e.returnValue = ''; } });
document.addEventListener('visibilitychange', () => { if (document.hidden) noteQueue.flushAll().catch(() => {}); });
async function editTags(id, add, remove) {
  try { await api.post(`/api/person/${id}/tags`, { add, remove }); } catch (e) { toast('Could not save tag'); return; }
  await refreshPerson(id);
  loadFacetsSoon();
  if (add.length) toast(`Tagged ${add[0]}`);
}

// Date-only follow-ups stay in the browser's local calendar; saved views remain relative.
const workflowDrafts = new Map();
function rememberWorkflowForm() {
  const groups = [
    ['#followup-form', { due: '#followup-date', action: '#followup-note' }],
    ['#activity-form', { kind: '#activity-kind', body: '#activity-body', when: '#activity-when', status: '#activity-status', followChoice: '#activity-followup', nextDue: '#activity-followup-date', nextAction: '#activity-followup-note' }],
  ];
  for (const [selector, fields] of groups) {
    const form = $(selector); if (!form) continue;
    const id = +form.dataset.owner, previous = form._workflowValues || {};
    const draft = { ...workflowDrafts.get(id) }, versions = { ...draft._versions };
    let changed = false;
    for (const [key, selector] of Object.entries(fields)) {
      const field = $(selector); if (!field) continue;
      const value = field.value;
      if (!form._workflowPrime && previous[key] !== value) {
        draft[key] = value; versions[key] = (versions[key] || 0) + 1; changed = true;
      }
      previous[key] = value;
    }
    form._workflowValues = previous; form._workflowPrime = false;
    if (changed) { draft._versions = versions; workflowDrafts.set(id, draft); }
  }
  for (const [selector, key] of [['#followup-editor', 'followOpen'], ['#activity-editor', 'activityOpen'], ['#activity-options', 'optionsOpen']]) {
    const el = $(selector); if (!el) continue;
    if (el._workflowOpen !== el.open) workflowDrafts.set(+el.dataset.owner, { ...workflowDrafts.get(+el.dataset.owner), [key]: el.open });
    el._workflowOpen = el.open;
  }
}
function localDateTime() { const d = new Date(); return LeadWorkflow.localToday(d) + 'T' + String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); }
const followUpBusy = new Set(), activityBusy = new Set(), activityMoreBusy = new Set();
const workflowMessages = new Map(), activityFetchVersions = new Map();
const activityFields = ['kind', 'body', 'when', 'status', 'followChoice', 'nextDue', 'nextAction'];
const activityLabels = { dm: 'DM sent', reply: 'Reply received', call: 'Call', meeting: 'Meeting', note: 'Note', status: 'Status changed', follow_up_scheduled: 'Follow-up scheduled', follow_up_completed: 'Follow-up completed', follow_up_cleared: 'Follow-up cleared', identity_merged: 'Profiles merged', follow_up_merged: 'Follow-ups merged' };
function activityHTML(activity, id = S.person?.id) {
  if (!activity) return '<p class="muted">Activity is unavailable.</p><button class="btn" id="activity-retry" type="button">Retry activity</button>';
  const rows = activity.rows || [], all = !!workflowDrafts.get(id)?.historyOpen;
  const visible = all ? rows : rows.slice(0, 3);
  return `<ol class="activity-list">${visible.map((a) => `<li><div><strong>${esc(activityLabels[a.kind] || a.kind)}</strong><time datetime="${esc(a.happened_at)}">${esc(new Date(a.happened_at).toLocaleString())}</time></div>${a.body ? `<p>${esc(a.body)}</p>` : ''}${a.before_value != null || a.after_value != null ? `<p class="muted">${esc(activityValue(a.before_value))} → ${esc(activityValue(a.after_value))}</p>` : ''}</li>`).join('')}</ol>${!rows.length ? '<p class="muted">No activity recorded yet.</p>' : ''}<div class="workflow-actions">${rows.length > 3 || activity.next_cursor ? `<button class="btn" type="button" id="activity-show-all" aria-expanded="${all}">${all ? 'Show less activity' : 'Show all activity'}</button>` : ''}${all && activity.next_cursor ? `<button class="btn" type="button" id="activity-more" ${activityMoreBusy.has(id) ? 'disabled' : ''}>Load older activity</button>` : ''}</div>`;
}
function activityValue(value) {
  if (value == null || value === '') return 'None';
  if (typeof value === 'object') {
    if (value.kept || value.merged) return 'Kept: ' + activityValue(value.kept) + ' · Merged: ' + activityValue(value.merged);
    return [value.due_on, value.status ? slabel(value.status) : '', value.note, value.completed_at ? 'Completed' : ''].filter(Boolean).join(' · ') || JSON.stringify(value);
  }
  return String(value);
}
function workflowBusy(id) { return followUpBusy.has(id) || activityBusy.has(id); }
function workflowShortcuts(target) {
  return `<div class="workflow-shortcuts" role="group" aria-label="Choose follow-up date">${[[0, 'Today'], [1, 'Tomorrow'], [7, 'In a week']].map(([days, label]) => `<button type="button" class="btn" data-workflow-days="${days}" data-workflow-date="${target}">${label}</button>`).join('')}</div>`;
}
function workflowSummaryHTML(p) {
  if (p.loading || p.failed) return '';
  const f = p.follow_up, open = f && !f.completed_at, d = workflowDrafts.get(p.id) || {};
  const today = LeadWorkflow.localToday(), overdue = open && f.due_on < today;
  const status = !f ? 'No follow-up scheduled' : f.completed_at ? 'Follow-up completed' : (overdue ? 'Overdue · ' : f.due_on === today ? 'Due today · ' : 'Scheduled · ') + f.due_on;
  const disabled = workflowBusy(p.id) ? 'disabled' : '';
  return `<section class="d-sec workflow" id="workflow-summary"><h4>Next action<span class="grow"></span><span class="muted">${esc(p.status ? slabel(p.status) : 'No status')}</span></h4>
    <div class="workflow-summary-line"><p class="${overdue ? 'bad' : 'muted'}">${esc(status)}</p>${open ? `<button type="button" class="btn" id="followup-complete" data-follow-action="complete" ${disabled}>Complete</button>` : ''}</div>
    ${open && f.note ? `<p class="workflow-next-action">${esc(f.note)}</p>` : ''}
    <details class="workflow-editor" id="followup-editor" data-owner="${p.id}" data-workflow-disclosure="followOpen" ${d.followOpen ? 'open' : ''}><summary id="followup-editor-summary">${open ? 'Change follow-up' : f ? 'Schedule next follow-up' : 'Add follow-up'}</summary>
      <form id="followup-form" data-owner="${p.id}" class="workflow-form" aria-describedby="followup-state" aria-busy="${workflowBusy(p.id)}"><label for="followup-date">Date</label><input class="input" id="followup-date" type="date" required value="${esc(d.due ?? (open ? f.due_on : ''))}">${workflowShortcuts('followup-date')}
        <label for="followup-note">Next action (optional)</label><input class="input" id="followup-note" maxlength="500" value="${esc(d.action ?? (open ? f.note || '' : ''))}" placeholder="Send portfolio, check reply…"><div class="workflow-actions"><button class="btn solid" id="followup-save" type="submit" ${disabled}>${open ? 'Save follow-up' : 'Schedule follow-up'}</button>${f ? `<button type="button" class="btn" id="followup-clear" data-follow-action="clear" ${disabled}>Clear follow-up</button>` : ''}</div></form></details>
    <p id="followup-state" role="status" aria-live="polite">${esc(workflowMessages.get(p.id)?.followup || '')}</p></section>`;
}
function workflowHTML(p) {
  if (p.loading || p.failed) return '';
  const d = workflowDrafts.get(p.id) || {}, f = p.follow_up, open = f && !f.completed_at;
  const followChoices = [['', 'Keep current follow-up'], ['schedule', open ? 'Reschedule follow-up' : 'Schedule next follow-up']];
  if (open || ['complete', 'complete_and_schedule'].includes(d.followChoice)) followChoices.push(['complete', 'Complete current follow-up'], ['complete_and_schedule', 'Complete and schedule next']);
  if (f || d.followChoice === 'clear') followChoices.push(['clear', 'Clear follow-up']);
  const scheduling = ['schedule', 'complete_and_schedule'].includes(d.followChoice);
  return `<section class="d-sec workflow" id="workflow-activity"><h4>Activity</h4>
    <details class="workflow-editor" id="activity-editor" data-owner="${p.id}" data-workflow-disclosure="activityOpen" ${d.activityOpen ? 'open' : ''}><summary id="activity-editor-summary">Log interaction</summary>
      <form id="activity-form" data-owner="${p.id}" class="workflow-form" aria-describedby="activity-state" aria-busy="${workflowBusy(p.id)}"><label for="activity-kind">Interaction</label><select id="activity-kind" class="select">${['dm', 'reply', 'call', 'meeting', 'note'].map((k) => `<option value="${k}" ${d.kind === k ? 'selected' : ''}>${activityLabels[k]}</option>`).join('')}</select>
        <label for="activity-when">When (your local time)</label><input class="input" id="activity-when" type="datetime-local" required value="${esc(d.when ?? localDateTime())}"><label for="activity-body">Details</label><textarea class="input" id="activity-body" maxlength="5000" required placeholder="What happened?">${esc(d.body || '')}</textarea>
        <details class="workflow-optional" id="activity-options" data-owner="${p.id}" data-workflow-disclosure="optionsOpen" ${(d.optionsOpen ?? !!(d.status || d.followChoice)) ? 'open' : ''}><summary id="activity-options-summary">Change status or follow-up (optional)</summary>
          <div class="workflow-form"><label for="activity-status">Status</label><select id="activity-status" class="select"><option value="">Keep current status</option>${STATUSES.map((s) => `<option value="${s}" ${d.status === s ? 'selected' : ''}>${esc(slabel(s))}</option>`).join('')}</select>
            <label for="activity-followup">Follow-up</label><select id="activity-followup" class="select">${followChoices.map(([value, label]) => `<option value="${value}" ${d.followChoice === value ? 'selected' : ''}>${label}</option>`).join('')}</select>
            <div class="workflow-form" id="activity-followup-fields" ${scheduling ? '' : 'hidden'}><label for="activity-followup-date">Next follow-up date</label><input class="input" id="activity-followup-date" type="date" value="${esc(d.nextDue || '')}" ${scheduling ? 'required' : 'disabled'}>${workflowShortcuts('activity-followup-date')}<label for="activity-followup-note">Next action (optional)</label><input class="input" id="activity-followup-note" maxlength="500" value="${esc(d.nextAction || '')}" ${scheduling ? '' : 'disabled'} placeholder="What will you do next?"></div></div></details>
        <button class="btn solid" id="activity-save" type="submit" ${workflowBusy(p.id) ? 'disabled' : ''}>Save interaction</button></form></details>
    <p id="activity-state" role="status" aria-live="polite">${esc(workflowMessages.get(p.id)?.activity || '')}</p><div id="activity-timeline">${activityHTML(p.activity, p.id)}</div></section>`;
}
function wireWorkflow(p) {
  if (p.loading || p.failed) return;
  for (const selector of ['#followup-form', '#activity-form']) { const form = $(selector); if (form) form._workflowPrime = true; }
  for (const selector of ['#followup-editor', '#activity-editor', '#activity-options']) { const el = $(selector); if (el) el._workflowOpen = el.open; }
  rememberWorkflowForm(); syncWorkflowControls(p.id);
}
function syncWorkflowControls(id) {
  if (S.person?.id !== id) return;
  const busy = workflowBusy(id);
  for (const selector of ['#followup-save', '#followup-complete', '#followup-clear', '#activity-save']) { const el = $(selector); if (el) el.disabled = busy; }
  for (const selector of ['#followup-form', '#activity-form']) $(selector)?.setAttribute('aria-busy', String(busy));
  for (const key of ['followup', 'activity']) { const el = $('#' + key + '-state'); if (el) el.textContent = workflowMessages.get(id)?.[key] || ''; }
  const choice = $('#activity-followup')?.value, scheduling = ['schedule', 'complete_and_schedule'].includes(choice);
  const fields = $('#activity-followup-fields'); if (fields) fields.hidden = !scheduling;
  const date = $('#activity-followup-date'); if (date) { date.disabled = !scheduling; date.required = scheduling; }
  const note = $('#activity-followup-note'); if (note) note.disabled = !scheduling;
}
function workflowMessage(id, key, message) {
  workflowMessages.set(id, { ...workflowMessages.get(id), [key]: message }); syncWorkflowControls(id);
}
function snapshotWorkflowDraft(id, form, fields) {
  const draft = { ...workflowDrafts.get(id) };
  for (const key of fields) if (form?._workflowValues && key in form._workflowValues) draft[key] = form._workflowValues[key];
  workflowDrafts.set(id, draft);
  return { ...draft, _versions: { ...draft._versions } };
}
function acknowledgeWorkflowDraft(id, submitted, fields, closedKey) {
  const current = workflowDrafts.get(id) || {};
  const result = LeadWorkflow.reconcileWorkflowDraft(current, submitted, fields);
  // A changed form is one coherent newer draft, so retain its unchanged context too.
  if (result.unchanged) workflowDrafts.set(id, { ...result.draft, ...(closedKey ? { [closedKey]: false } : {}) });
  return result.unchanged;
}
function applyWorkflowResponse(id, response) {
  const patch = {};
  if (Object.hasOwn(response, 'follow_up')) patch.follow_up = response.follow_up;
  if (Object.hasOwn(response, 'status')) patch.status = response.status;
  const row = S.rows.find((item) => item.id === id); if (row) Object.assign(row, patch);
  if ('status' in patch) M.patch([id], { status: patch.status });
  if (S.person?.id === id) {
    Object.assign(S.person, patch);
    if (Array.isArray(response.rows)) {
      activityFetchVersions.set(id, (activityFetchVersions.get(id) || 0) + 1);
      S.person.activity = { rows: response.rows, next_cursor: response.next_cursor };
    }
    renderDetail();
  }
  renderRows();
}
async function refreshActivity(id) {
  if (S.person?.id !== id) return;
  const version = (activityFetchVersions.get(id) || 0) + 1; activityFetchVersions.set(id, version);
  try {
    const activity = await api.get(`/api/person/${id}/activity?limit=50`);
    if (S.person?.id === id && activityFetchVersions.get(id) === version) {
      S.person.activity = activity;
      const el = $('#activity-timeline'); if (el) el.innerHTML = activityHTML(activity, id);
    }
  } catch (error) { if (S.person?.id === id && !S.person.activity) workflowMessage(id, 'activity', 'Could not load activity: ' + error.message); }
}
async function updateFollowUp(id, body, button) {
  if (workflowBusy(id)) return;
  rememberWorkflowForm();
  const baseline = S.person?.id === id ? S.person.follow_up : null;
  const submitted = snapshotWorkflowDraft(id, $('#followup-form'), ['due', 'action']);
  followUpBusy.add(id); workflowMessage(id, 'followup', 'Saving…');
  try {
    const response = await api.post(`/api/person/${id}/follow-up`, body);
    if (S.person?.id === id) rememberWorkflowForm();
    const untouchedReminder = submitted.due === (baseline && !baseline.completed_at ? baseline.due_on : '') && submitted.action === (baseline && !baseline.completed_at ? baseline.note || '' : '');
    if (!body.action || untouchedReminder) acknowledgeWorkflowDraft(id, submitted, ['due', 'action'], 'followOpen');
    workflowMessage(id, 'followup', body.action === 'complete' ? 'Follow-up completed' : body.action === 'clear' ? 'Follow-up cleared' : 'Follow-up saved');
    applyWorkflowResponse(id, response);
    refreshActivity(id); loadCounts(); loadFacetsSoon(); resetLeads(true);
  } catch (error) {
    workflowMessage(id, 'followup', 'Could not save: ' + error.message + '. Your draft is kept.');
    if (S.person?.id === id) { const editor = $('#followup-editor'); if (editor) editor.open = true; }
  } finally { followUpBusy.delete(id); syncWorkflowControls(id); }
}
async function saveInteraction(id, form) {
  if (workflowBusy(id)) return;
  rememberWorkflowForm();
  const submitted = snapshotWorkflowDraft(id, form, activityFields), date = new Date(submitted.when);
  if (!Number.isFinite(date.getTime())) { workflowMessage(id, 'activity', 'Choose a valid date and time.'); return; }
  if (!submitted.body?.trim()) { workflowMessage(id, 'activity', 'Add a few details about the interaction.'); return; }
  const body = { kind: submitted.kind, body: submitted.body, happened_at: date.toISOString() };
  if (submitted.status) body.status = submitted.status;
  if (submitted.followChoice) {
    if (['schedule', 'complete_and_schedule'].includes(submitted.followChoice)) {
      if (!submitted.nextDue) { workflowMessage(id, 'activity', 'Choose the next follow-up date.'); return; }
      body.follow_up = { due_on: submitted.nextDue, note: submitted.nextAction || '' };
      if (submitted.followChoice === 'complete_and_schedule') body.follow_up.action = 'complete_and_schedule';
    } else body.follow_up = { action: submitted.followChoice };
  }
  const reminder = snapshotWorkflowDraft(id, $('#followup-form'), ['due', 'action']);
  const baseline = S.person?.id === id ? S.person.follow_up : null;
  activityBusy.add(id); workflowMessage(id, 'activity', 'Saving…');
  try {
    const response = await api.post(`/api/person/${id}/activity`, body);
    if (S.person?.id === id) rememberWorkflowForm();
    const cleared = acknowledgeWorkflowDraft(id, submitted, activityFields, 'activityOpen');
    if (cleared) workflowDrafts.set(id, { ...workflowDrafts.get(id), optionsOpen: false });
    if (body.follow_up && reminder.due === (baseline && !baseline.completed_at ? baseline.due_on : '') && reminder.action === (baseline && !baseline.completed_at ? baseline.note || '' : '')) acknowledgeWorkflowDraft(id, reminder, ['due', 'action'], 'followOpen');
    workflowMessage(id, 'activity', 'Interaction saved');
    applyWorkflowResponse(id, response);
    if (!Array.isArray(response.rows)) refreshActivity(id);
    if (body.status || body.follow_up) { loadCounts(); loadFacetsSoon(); resetLeads(true); }
  } catch (error) {
    workflowMessage(id, 'activity', 'Could not save: ' + error.message + '. Your draft is kept.');
    if (S.person?.id === id) { const editor = $('#activity-editor'); if (editor) editor.open = true; }
  } finally { activityBusy.delete(id); syncWorkflowControls(id); }
}
$('#detail').addEventListener('input', (e) => { if (e.target.closest('.workflow-form')) rememberWorkflowForm(); });
$('#detail').addEventListener('change', (e) => {
  if (e.target.closest('.workflow-form')) { rememberWorkflowForm(); if (S.person) syncWorkflowControls(S.person.id); }
});
$('#detail').addEventListener('toggle', (e) => {
  const el = e.target;
  if (!el.matches?.('details[data-workflow-disclosure]') || !el.isConnected) return;
  if (el._workflowOpen === el.open) return;
  const id = +el.dataset.owner, key = el.dataset.workflowDisclosure;
  workflowDrafts.set(id, { ...workflowDrafts.get(id), [key]: el.open }); el._workflowOpen = el.open;
  if (key === 'activityOpen' && el.open && !workflowDrafts.get(id)?.when && !workflowBusy(id)) {
    const field = $('#activity-when');
    if (field) { field.value = localDateTime(); rememberWorkflowForm(); }
  }
}, true);
$('#detail').addEventListener('submit', async (e) => {
  if (!['followup-form', 'activity-form'].includes(e.target.id)) return;
  e.preventDefault();
  const id = +e.target.dataset.owner; if (S.person?.id !== id) return;
  if (e.target.id === 'followup-form') return updateFollowUp(id, { due_on: $('#followup-date').value, note: $('#followup-note').value }, e.submitter);
  return saveInteraction(id, e.target);
});
$('#detail').addEventListener('click', async (e) => {
  const id = S.person?.id; if (!id) return;
  const shortcut = e.target.closest('[data-workflow-days]');
  if (shortcut) {
    const field = $('#' + shortcut.dataset.workflowDate); if (!field || field.disabled) return;
    field.value = LeadWorkflow.addLocalDays(+shortcut.dataset.workflowDays); rememberWorkflowForm(); return;
  }
  const action = e.target.closest('[data-follow-action]');
  if (action) return updateFollowUp(id, { action: action.dataset.followAction }, action);
  if (e.target.id === 'activity-retry') return refreshActivity(id);
  if (e.target.id === 'activity-show-all') {
    const d = workflowDrafts.get(id) || {}; workflowDrafts.set(id, { ...d, historyOpen: !d.historyOpen });
    $('#activity-timeline').innerHTML = activityHTML(S.person.activity, id); $('#activity-show-all')?.focus(); return;
  }
  if (e.target.id !== 'activity-more' || activityMoreBusy.has(id)) return;
  const button = e.target, activity = S.person.activity; if (!activity?.next_cursor) return;
  activityMoreBusy.add(id); button.disabled = true;
  try {
    const page = await api.get(`/api/person/${id}/activity?limit=50&cursor=${encodeURIComponent(activity.next_cursor)}`);
    if (S.person?.id === id && S.person.activity === activity) {
      const seen = new Set(activity.rows.map((row) => row.id));
      activity.rows.push(...page.rows.filter((row) => !seen.has(row.id))); activity.next_cursor = page.next_cursor;
      $('#activity-timeline').innerHTML = activityHTML(activity, id); ($('#activity-more') || $('#activity-show-all'))?.focus();
    }
  } catch (error) { workflowMessage(id, 'activity', 'Could not load older activity: ' + error.message); }
  finally { activityMoreBusy.delete(id); if (S.person?.id === id && $('#activity-more')) $('#activity-more').disabled = false; }
});
let exporting = false;
async function exportLeads(selected) {
  if (exporting) return;
  const ids = [...S.pick], params = LeadWorkflow.runtimeQuery(toQuery());
  params.set('sort', S.sort);
  const query = params.toString();
  if (selected && !ids.length) return;
  exporting = true;
  const state = $('#export-state'); state.textContent = 'Saving notes and preparing CSV…';
  $('#export-filtered').disabled = true; $('#export-selected').disabled = true;
  try {
    await noteQueue.flushAll();
    const response = await fetch('/api/leads/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(selected ? { ids } : { query }) });
    if (!response.ok) { let message = 'HTTP ' + response.status; try { message = (await response.json()).error || message; } catch {} throw new Error(message); }
    const blob = await response.blob(), url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = `fortunate-leads-${selected ? 'selected' : 'filtered'}-${LeadWorkflow.localToday()}.csv`; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
    state.textContent = selected ? `Exported ${ids.length} selected leads` : 'Exported all matching leads';
  } catch (error) { state.textContent = 'Export failed: ' + error.message + '. Your selection is kept; try again.'; }
  finally { exporting = false; $('#export-filtered').disabled = false; $('#export-selected').disabled = !S.pick.size; }
}
$('#export-filtered').onclick = () => exportLeads(false);
$('#export-selected').onclick = () => exportLeads(true);

// ---------- keyboard ----------
const detailAccess = LeadAccessibility.createDetailFocus({
  panel: $('#detail'), isMobile: narrow,
  fallbackFocus: () => S.view === 'leads' ? $('#rows .row.cur') || $('#q') : S.view === 'map' ? $('#canvas') : $('.tabs a.on'),
});
let gPending = 0;
let helpReturnFocus = null;
function setHelp(open) {
  const help = $('#help');
  gPending = 0;
  if (open) {
    helpReturnFocus = document.activeElement;
    help.hidden = false;
    help.tabIndex = -1;
    help.focus({ preventScroll: true });
  } else {
    help.hidden = true;
    if (helpReturnFocus?.isConnected) helpReturnFocus.focus({ preventScroll: true });
    else $('.tabs a.on')?.focus({ preventScroll: true });
    helpReturnFocus = null;
  }
}
// Stop keys before native controls or delegated workspace handlers can act underneath help.
document.addEventListener('keydown', (e) => {
  if ($('#help').hidden) return;
  if (!e.metaKey && !e.ctrlKey) e.preventDefault();
  e.stopImmediatePropagation();
  if (e.key === 'Escape' || e.key === '?') setHelp(false);
}, true);
document.addEventListener('keydown', (e) => {
  if (e.defaultPrevented || e.isComposing || e.keyCode === 229) return;
  if (detailAccess.handleKeydown(e)) return;
  const typing = LeadAccessibility.isEditableTarget(e.target);
  if (e.key === 'Escape') {
    gPending = 0;
    if (!$('#help').hidden) { $('#help').hidden = true; return; }
    if (typing) { (document.activeElement || e.target).blur(); if (detailAccess.isModal()) detailAccess.focusInitial(); return; }
    if (detailAccess.isModal()) { closeDetail(); return; }
    if ($('#filters').classList.contains('show')) { setDrawer(false); return; }
    if (S.open || S.seedCard) { closeDetail(); return; }
    if (S.view === 'map' && M.focus) { M.focus = null; M.draw(); return; }
    if (S.pick.size) { clearPick(); return; }
    if (S.view === 'leads' && S.cur >= 0) { S.cur = -1; renderRows(); }
    return;
  }
  if (LeadAccessibility.isInteractiveTarget(e.target) || $('#detail').contains(e.target) || detailAccess.isModal() || !$('#help').hidden || e.metaKey || e.ctrlKey || e.altKey) { gPending = 0; return; }
  const k = e.key;
  // Let native controls handle activation instead of also opening the current lead.
  if ((k === 'Enter' || k === ' ') && e.target.closest('button, a')) return;
  if (gPending && Date.now() - gPending < 900) {
    gPending = 0;
    const to = { l: 'leads', m: 'map', q: 'qual', t: 'tags', s: 'scraper', a: 'accounts', ',': 'settings' }[k];
    if (to) { location.hash = hashFor(to); e.preventDefault(); }
    return;
  }
  if (k === 'g') { gPending = Date.now(); return; }
  if (k === '?') { e.preventDefault(); setHelp(true); return; }
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
    // Comparison has its own selection; do not act on the retained overview person.
    if ($('#pane-map').classList.contains('comparing')) return;
    if (k === 'f') { M.fit(); return; }
    if (k === '+' || k === '=') { M.zoomBy(1.4); return; }
    if (k === '-') { M.zoomBy(1 / 1.4); return; }
    if (k === 'l') { M.toggleLabels(); return; }
    if (k === 'n') { M.next(); return; }
    const p = S.person;
    if (p && /^[0-5]$/.test(k)) mark(p.id, k === '0' ? null : STATUSES[+k - 1]);
    if (p && k === 'm') mark(p.id, CYCLE[(CYCLE.indexOf(p.status ?? null) + 1) % CYCLE.length]);
    if (p && k === 'o') window.open(`https://www.instagram.com/${encodeURIComponent(p.handle)}/`, '_blank', 'noopener');
    if (p && k === 't') { e.preventDefault(); LeadAccessibility.revealAndFocus($('#tag-in')); }
    return;
  }
  // Tab can focus a different lead without using the j/k navigation cursor.
  const focusedRow = e.target.closest('#rows .row');
  if (focusedRow) S.cur = +focusedRow.dataset.i;
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
  const r = focusedRow ? S.rows[S.cur] : current();
  if (k === 'Enter' && S.rows[S.cur]) { e.preventDefault(); openDetail(S.rows[S.cur].id, { keyboard: true }); return; }
  if (S.pick.size && /^[0-5]$/.test(k)) { bulk({ status: k === '0' ? null : STATUSES[+k - 1] }); return; }
  if (S.pick.size && k === 't') { e.preventDefault(); $('#bk-add')?.focus(); return; }
  if (!r) return;
  if (k === 'm') { mark(r.id, CYCLE[(CYCLE.indexOf(r.status ?? null) + 1) % CYCLE.length]); return; }
  if (/^[0-5]$/.test(k)) { mark(r.id, k === '0' ? null : STATUSES[+k - 1]); return; }
  if (k === 'o') { window.open(`https://www.instagram.com/${encodeURIComponent(r.handle)}/`, '_blank', 'noopener'); return; }
  if (k === 't') {
    e.preventDefault();
    const focusTag = () => { if (S.open === r.id) LeadAccessibility.revealAndFocus($('#tag-in')); };
    if (S.open !== r.id) openDetail(r.id).then(focusTag); else focusTag();
  }
});
$('#help').onclick = () => setHelp(false);

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
    const focusedTag = $('#tg-body').contains(document.activeElement) ? document.activeElement.dataset.ck : null;
    const q = this.q.toLowerCase();
    this.renderGroups(q);
    const rows = this.list.filter((t) => this.editable(t) && (!q || t.tag.toLowerCase().includes(q)))
      .sort((a, b) => b.total - a.total || a.tag.localeCompare(b.tag));
    $('#tg-n').textContent = int(rows.length);
    const ed = this.editing;
    $('#tg-body').innerHTML = rows.length ? rows.map((t) => {
      const can = this.editable(t);
      const on = this.checked.has(t.tag);
      const label = `<button class="tag ${KIND[t.kind]} g-${esc(t.grp || 'custom')}${t.grp === 'source' ? ' src' : ''}${tagTier(t) ? ' t-' + tagTier(t) : ''}" data-go="${esc(t.tag)}" title="Show leads with this tag"><span>${esc(t.tag)}</span></button>`;
      const name = ed === t.tag ? `<form class="ren" data-ren="${esc(t.tag)}"><input class="input" id="ren-in" value="${esc(t.tag)}" autocomplete="off" spellcheck="false"><button class="btn solid" id="ren-go">Rename</button><button type="button" class="btn" data-cancel>Cancel</button></form>` : label;
      return `<tr class="${on ? 'on' : ''}${t.total ? '' : ' dim'}">
        <td class="c-ck">${can ? `<button class="ck${on ? ' on' : ''}" data-ck="${esc(t.tag)}" role="checkbox" aria-checked="${on}" aria-label="Select ${esc(t.tag)}"></button>` : ''}</td>
        <td>${name}</td>
        <td class="r num">${int(t.total)}</td>
        <td class="r">${can && ed !== t.tag ? `<span class="acts"><button data-edit="${esc(t.tag)}">Rename</button><button data-del="${esc(t.tag)}" class="${this.confirm === t.tag ? 'warn' : ''}">${this.confirm === t.tag ? 'Confirm' : 'Delete'}</button></span>` : ''}</td></tr>`;
    }).join('') : `<tr><td colspan="4" class="muted">${this.failed ? 'Could not load tags' : q ? 'No match' : 'None yet. Open a person and type a tag under their profile.'}</td></tr>`;
    const selectable = rows.filter((t) => this.editable(t));
    const visibleChecked = selectable.filter((t) => this.checked.has(t.tag)).length;
    const allOn = selectable.length > 0 && visibleChecked === selectable.length;
    $('#tg-all').className = 'ck' + (allOn ? ' on' : visibleChecked ? ' part' : '');
    $('#tg-all').setAttribute('aria-checked', allOn ? 'true' : visibleChecked ? 'mixed' : 'false');
    if (focusedTag) $$('#tg-body [data-ck]').find((b) => b.dataset.ck === focusedTag)?.focus({ preventScroll: true });
    if (ed) { const i = $('#ren-in'); if (i && document.activeElement !== i) { i.focus(); i.select(); } this.syncRen(); }
    this.renderMerge();
  },
  // Overview: the tags that make a lead first, then every other automatic tag grouped in plain words. Click = filter Leads.
  renderGroups(q) {
    const auto = this.list.filter((t) => t.kind === 'auto' && (!q || t.tag.toLowerCase().includes(q)));
    const fit = auto.filter((t) => ['hero', 'maybe'].includes(tagTier(t)));
    const business = auto.filter((t) => ['decision', 'role', 'plus', 'market', 'partner'].includes(tagTier(t)));
    const caution = auto.filter((t) => ['flag', 'review'].includes(tagTier(t)));
    const highlighted = new Set([...fit, ...business, ...caution]);
    const rest = auto.filter((t) => !highlighted.has(t));
    const groups = [
      ['niche', 'Products'], ['signal', 'Other clues'], ['size', 'Audience size'],
      ['via', 'Found via'], ['source', 'Collection'],
    ];
    const pick = (g) => g === 'via' ? rest.filter((t) => isViaTag(t.tag)) : g === 'source' ? rest.filter((t) => t.grp === 'source' && !isViaTag(t.tag))
      : rest.filter((t) => (t.grp || 'custom') === g);
    const known = new Set(['niche', 'signal', 'size', 'source']);
    const chip = (t) => `<button class="tchip t-${tagTier(t) || 'mid'}" data-go="${esc(t.tag)}" title="Show the ${int(t.total)} people tagged ${esc(t.tag)}"><span>${esc(isViaTag(t.tag) ? t.tag.slice(4) : t.tag)}</span><b class="num">${fmt(t.total)}</b></button>`;
    const sec = (key, title, list, cls = '') => {
      if (!list.length) return '';
      list = [...list].sort((a, b) => TIER_ORDER[tagTier(a)] - TIER_ORDER[tagTier(b)] || b.total - a.total || a.tag.localeCompare(b.tag));
      const lim = this.more?.[key] || q ? 400 : 10;
      return `<section class="tg-sec ${cls}"><div class="tg-ch"><h3>${esc(title)}</h3><span class="num muted">${list.length}</span></div>
        <div class="tg-chips">${list.slice(0, lim).map(chip).join('')}
        ${list.length > lim ? `<button class="tchip more" data-tmore="${key}">+${list.length - lim} more</button>` : ''}</div></section>`;
    };
    $('#tg-groups').innerHTML = (!auto.length ? `<p class="muted tg-empty">${q ? 'No matching automatic tags.' : 'Automatic tags appear as people are qualified.'}</p>` : '')
      + sec('fit', 'Best prospects', fit, 'tg-top')
      + sec('business', 'Business signals', business)
      + sec('caution', 'Needs a look', caution, 'tg-caution')
      + (rest.length ? `<details class="adv tg-more"${q ? ' open' : ''}><summary>Other automatic tags <span class="num muted">${rest.length}</span></summary><div class="tg-rest">${groups.map(([k, t]) => sec(k, t, pick(k))).join('')}
        ${sec('other', 'Other', rest.filter((t) => !known.has(t.grp) && !isViaTag(t.tag)))}</div></details>` : '');
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
const SCRAPER_FULL_VIEWS = new Set(['scraper', 'accounts', 'settings', 'qual']);
function scState() {
  const sc = S.sc;
  if (S.scStale) return { label: 'Connection lost · last known status', short: 'Offline', dot: 'hollow' };
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
  $('#st-act').textContent = sc && !x.online ? 'Open Chrome with Instagram logged in' : x.activity || (run ? `@${run.seed} ${run.direction}` : x.text || '');
  const t = x.today?.list, b = x.budget?.list;
  $('#st-today').textContent = t == null ? '–' : `${int(t)}/${b == null ? '–' : int(b)}`;
  $('#st-meter').style.width = t != null && b ? Math.min(100, (t / b) * 100) + '%' : '0';
  $('#st-pph').textContent = x.rate?.pages_hour != null ? int(Math.round(x.rate.pages_hour)) : '–';
  $('#st-peh').textContent = x.rate?.people_hour != null ? int(Math.round(x.rate.people_hour)) : '–';
  $('#st-hit').textContent = x.rate?.last_hit_at ? ago(x.rate.last_hit_at) + ' ago' : 'None';
  $('#pause-btn').disabled = !sc || !!S.scStale;
  $('#pause-btn').textContent = sc?.paused ? 'Resume' : 'Pause';
  $('#pause-btn').classList.toggle('solid', !!sc?.paused);
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
  if (S.scLoading) return;
  S.scLoading = true;
  if (S.view === 'scraper' && (!S.sc || S.scError)) renderScraper();
  try { S.sc = await api.get('/api/scraper'); S.scError = false; S.scStale = false; }
  catch (e) { S.scError = true; S.scStale = true; }
  finally { S.scLoading = false; }
  renderStatus();
  if (S.view === 'scraper') renderScraper();
  if (S.view === 'accounts') renderAccounts();
  if (S.view === 'settings') renderSettings();
  if (S.view === 'qual') Q.renderProg();
}
async function loadScraperStatus() {
  if (S.scLoading || S.scStatusLoading) return;
  S.scStatusLoading = true;
  try {
    // Preserve the detailed lists and progress from the slower full refresh.
    S.sc = { ...(S.sc || {}), ...await api.get('/api/scraper/status') };
    S.scStale = false;
  } catch (e) { S.scStale = true; }
  finally { S.scStatusLoading = false; }
  renderStatus();
  if (S.view === 'accounts') renderAccounts();
}
$('#pause-btn').onclick = async () => {
  if (!S.sc) return;
  const paused = !S.sc.paused;
  S.sc.paused = paused; renderStatus();
  try { await api.post('/api/scraper/pause', { paused }); } catch (e) { S.sc.paused = !paused; renderStatus(); toast('Could not reach server'); }
  loadScraper();
};
// The AI pill in the control strip is the main switch; the Qualification page keeps a shortcut to the same setting.
async function toggleQualify() {
  if (!S.sc) return;
  const on = !S.sc.qualify;
  S.sc.qualify = on; renderStatus();
  try { await api.post('/api/settings/qualify', { on }); toast(on ? 'Qualify on' : 'Qualify off'); }
  catch (e) { S.sc.qualify = !on; renderStatus(); toast('Could not save'); }
}

let listFilter = 'all';
const LIST_STATE = { partial: 'Partial', running: 'Reading now', queued: 'Waiting', paused: 'Paused', error: 'Failed', private: 'Private account', done: 'Done' };
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
  if (!sc || S.scError) {
    const message = S.scError
      ? sc ? 'Could not refresh. Showing last loaded data.' : 'Could not load collection status.'
      : 'Loading collection status…';
    const html = `<div class="row-flex"><span class="muted" role="status">${message}</span>${S.scError ? `<button class="btn" type="button" id="scr-retry"${S.scLoading ? ' disabled' : ''}>${S.scLoading ? 'Retrying…' : 'Retry'}</button>` : ''}</div>`;
    if ($('#now').innerHTML !== html) $('#now').innerHTML = html;
    if (!sc) {
      $('#stages').innerHTML = '';
      $('#lists-body').innerHTML = `<tr><td colspan="5" class="muted">${S.scError ? 'Lists are unavailable until collection status loads.' : 'Loading lists…'}</td></tr>`;
      return;
    }
  }
  const x = sc.ext || {}, ls = sc.lists || [], pr = sc.progress || {};
  const run = ls.find((l) => l.state === 'running');
  // One plain sentence: what is happening right now.
  const cool = x.cooldown_until && Date.parse(x.cooldown_until) > Date.now();
  const capped = ['list', 'profile'].some((k) => x.budget?.[k] && (x.today?.[k] || 0) >= x.budget[k]);
  const reading = run && `Reading @${run.seed}'s ${run.direction === 'followers' ? 'followers' : 'following list'}`;
  let now, sub = '';
  if (S.scStale && S.sc) { now = 'Connection lost'; sub = 'Showing the last update. Progress may have changed.'; }
  else if (sc.paused) { now = 'Paused'; sub = 'Use the Lists or Bios controls at the top to resume collecting.'; }
  else if (!x.online) { now = 'Chrome extension not connected'; sub = `Open Chrome with Instagram logged in${x.last_seen ? `. Last seen ${ago(x.last_seen)} ago.` : '.'}`; }
  else if (cool && reading && x.state === 'running') { now = reading; sub = `Bio reads are on a short break so Instagram doesn't flag your account. Back ${backIn(x.cooldown_until)}.`; }
  else if (cool && capped) { now = 'Daily limit reached'; sub = `Back ${backIn(x.cooldown_until)}. Add another Instagram account under Accounts to keep going today.`; }
  else if (cool && Date.parse(x.cooldown_until) - Date.now() > 3600e3) { now = 'Resting'; sub = `Instagram asked us to slow down. Back ${backIn(x.cooldown_until)}.`; }
  else if (cool) { now = 'Short break'; sub = `So Instagram doesn't flag your account. Back ${backIn(x.cooldown_until)}.`; }
  else if (reading) { now = reading; sub = `${int(run.received)}${run.total ? ' of ' + int(run.total) : ''} people so far.`; }
  else { now = 'Online, waiting for work'; sub = 'Add accounts to scrape below.'; }
  const accs = sc.accounts || [];
  const conn = accs.length ? accs.map((a) => `<span class="cpill" title="${esc(ST_LABEL[a.status] || a.status)}"><i class="dot ${a.online ? 'live' : 'off'}"></i>${esc(a.name || a.handle || a.lane_id)}<span class="muted">${a.online ? `${int(a.hour?.people || 0)} this hour` : 'offline'}</span></span>`).join('')
    : `<span class="cpill"><i class="dot ${x.online ? 'live' : 'off'}"></i>Extension ${x.online ? 'connected' : 'not connected'}</span>`;
  if (!S.scError) $('#now').innerHTML = `<div class="now-line"><i class="dot ${x.online && !sc.paused ? 'live' : 'off'}"></i><div><b>${esc(now)}</b><span class="muted">${esc(sub)}</span></div></div><div class="conns">${conn}</div>`;
  const h1 = sc.soak?.['1h'] || {};
  const tile = (label, v, small) => `<div class="tile"><span>${label}</span><b class="num">${v}</b><small>${small}</small></div>`;
  $('#scr-counts').innerHTML = tile('Scraped in the last hour', int(h1.new_people ?? h1.people ?? 0), sc.rate?.pages_hour ? `${int(Math.round(sc.rate.pages_hour))} list pages an hour` : 'new people')
    + tile('Scraped today', int(sc.people_today ?? 0), 'new people')
    + tile('Scraped in total', S.counts?.total != null ? int(S.counts.total) : '–', 'people in Leads');
  const L = pr.lists || {}, B = pr.bios || {}, Q = pr.qualify || {};
  const minuteRate = (value, noun) => value == null ? `measuring ${noun}` : `${int(value)} ${noun} in the last minute`;
  const recv = ls.reduce((a, l) => a + (l.received || 0), 0), tot = recv + (L.left || 0);
  const stage = (title, line, pct, when) => `<div class="stg"><div class="st-top"><b>${title}</b><span class="muted">${when || ''}</span></div>
    <div class="bar-p ${pct >= 100 ? 'done' : 'run'}"><i style="width:${Math.min(100, pct || 0)}%"></i></div><div class="muted">${line}</div></div>`;
  const offline = x.online ? '' : 'waiting for the extension';
  const listsDone = ls.length > 0 && ls.every(l => l.state === 'done');
  const listWhen = S.scStale ? 'Last known progress' : listsDone ? 'Done' : sc.paused ? 'Paused'
    : eta(L.eta_h) ? `${eta(L.eta_h)} left` : run ? 'Reading now' : offline || 'Waiting';
  const bioLine = `${int(B.left)} bios to read · ${minuteRate(B.per_minute, 'bios')} · limit ${int(B.per_day)} a day`;
  const bioWhen = B.left === 0 ? 'Nothing waiting' : offline || (eta(B.eta_h) ? eta(B.eta_h) + ' left' : 'measuring speed…');
  // Speed scales with accounts: each extra Instagram account adds roughly one account's measured pace.
  const lanes = Math.max(1, (sc.accounts || []).filter((a) => a.online && !a.paused).length);
  const faster = L.eta_h > 72 && L.per_hour ? `<p class="muted">Each extra Instagram account adds about ${int(Math.round(L.per_hour / lanes))} people an hour. Add one under Accounts.</p>` : '';
  $('#stages').innerHTML = [
    stage('1. Collect lists', `${int(recv)} people collected, ${L.estimate ? 'about ' : ''}${int(L.left)} still to go · ${minuteRate(L.per_minute, 'list entries')}`,
      listsDone ? 100 : tot ? Math.min(99, (recv / tot) * 100) : 0, listWhen) + faster,
    stage('2. Read bios', bioLine, B.left === 0 ? 100 : 0, bioWhen),
    stage('3. AI scoring', Q.on ? `${int(Q.left || 0)} people to score · ${Q.keys || 0} OpenRouter keys, ${Q.workers || 0} at a time · ${int(Q.per_minute || 0)} per minute · ${int(Q.per_hour || 0)} per hour`
      : `Off. Press Resume on AI at the top to let AI score ${int(Q.left || 0)} people with bios.`,
      Q.left ? 0 : 100, !Q.on ? 'Off' : !Q.left ? 'Done' : eta(Q.eta_h) ? eta(Q.eta_h) + ' left' : 'starting'),
  ].join('');
  $('#ext-ver').textContent = x.version ? 'Extension v' + x.version : '';
  const tl = x.today?.list, bl = x.budget?.list, tp = x.today?.profile, bp = x.budget?.profile;
  $('#ext-kv').innerHTML = [
    ['Today', `${int(tl)} of ${int(bl)} list pages, ${int(tp)} of ${int(bp)} bios`],
    ['Last error', x.last_error || 'None'],
  ].map(([k, v]) => `<span>${k}</span><b>${esc(v)}</b>`).join('');
  const groups = { all: ls, active: ls.filter((l) => l.state === 'running' || l.state === 'queued'), done: ls.filter((l) => l.state === 'done'), issues: ls.filter((l) => ['error', 'private', 'paused', 'partial'].includes(l.state)) };
  const focusedListFilter = $('#lists-f').contains(document.activeElement) ? document.activeElement.dataset.v : null;
  $('#lists-f').innerHTML = Object.entries(groups).map(([k, v]) => `<button data-v="${k}" aria-pressed="${listFilter === k}" class="${listFilter === k ? 'on' : ''}">${ucf(k)} <span class="num">${v.length}</span></button>`).join('');
  if (focusedListFilter) $(`#lists-f [data-v="${focusedListFilter}"]`)?.focus({ preventScroll: true });
  const order = { running: 0, queued: 1, partial: 2, paused: 2, error: 3, private: 4, done: 5 };
  const all = [...groups[listFilter]].sort((a, b) => (order[a.state] ?? 9) - (order[b.state] ?? 9) || (b.updated_at || '').localeCompare(a.updated_at || ''));
  const rows = listsAll ? all : all.slice(0, 8);
  const empty = ls.length ? { active: 'No active lists.', done: 'No completed lists.', issues: 'No issues.' }[listFilter] : 'No lists yet. Add an account above.';
  $('#lists-body').innerHTML = rows.length ? rows.map((l) => {
    const pct = l.total ? Math.min(100, (l.received / l.total) * 100) : null;
    return `<tr><td><b>@${esc(l.seed)}</b></td><td class="hide-sm muted">${l.direction === 'followers' ? 'Their followers' : 'Who they follow'}</td>
      <td class="prog"><div class="bar-p ${pct == null ? 'unknown' : l.state === 'done' ? 'done' : l.state === 'running' ? 'run' : ''}"><i style="width:${pct ?? 0}%"></i></div></td>
      <td class="r num">${int(l.received)}${l.total ? ' of ' + int(l.total) : ''}</td>
      <td><span class="state ${esc(l.state)}" title="${esc(l.error || '')}">${l.state === 'running' ? '<i class="dot run"></i>' : ''}${esc(LIST_STATE[l.state] || ucf(l.state))}</span></td></tr>`;
  }).join('') + (all.length > rows.length ? `<tr><td colspan="5"><button class="btn ghost" id="lists-all">Show all ${int(all.length)}</button></td></tr>` : '')
    : `<tr><td colspan="5" class="muted">${empty}</td></tr>`;
}
let listsAll = false;
$('#now').addEventListener('click', (e) => { if (e.target.closest('#scr-retry')) loadScraper(); });
$('#lists-body').addEventListener('click', (e) => { if (e.target.closest('#lists-all')) { listsAll = true; renderScraper(); } });
function backIn(t) {
  const m = Math.ceil(Math.max(0, Date.parse(t) - Date.now()) / 60000);
  return m <= 1 ? 'in about a minute' : m < 90 ? `in ${m} min` : `in ${Math.floor(m / 60)} h ${m % 60} min`;
}
$('#scr-add').onclick = () => { $('#seed-panel').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'center' }); $('#seed-in').focus({ preventScroll: true }); };
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
  const reserved = new Set(['p', 'reel', 'reels', 'tv', 'stories', 'explore', 'accounts', 'direct', 'about', 'developer', '_u']);
  const hosts = new Set(['instagram.com', 'www.instagram.com', 'm.instagram.com', 'instagr.am', 'www.instagr.am']);
  for (let t of s.split(/[\s,;]+/)) {
    t = t.trim().replace(/^@/, '');
    if (!t) continue;
    if (/[\/?#:]/.test(t) || /instagram\.com|instagr\.am/i.test(t)) {
      const match = t.match(/^(?:https?:\/\/)?([^/?#]+)(\/[^?#]*)?(?:\?[^#]*)?(?:#.*)?$/i);
      if (!match || !hosts.has(match[1].toLowerCase())) continue;
      const path = match[2] || '';
      const parts = path.split('/');
      if (parts.length < 2 || parts.length > 3 || (parts.length === 3 && parts[2])) continue;
      try { t = decodeURIComponent(parts[1]); } catch { continue; }
    }
    t = t.toLowerCase();
    if (/^[a-z0-9._]{1,30}$/.test(t) && !reserved.has(t)) out.add(t);
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
const A = { wiz: null, setup: null, confirm: null, renaming: null, renameValue: null, busy: new Set(), dismissed: false, starting: false };

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
function accountAccess(a) {
  if (a.hold === 'login' || a.status === 'needs_login') return { label: 'Login needed', detail: 'Open this Chrome profile and sign in.', kind: 'bad' };
  if (a.hold || a.status === 'challenge') return { label: 'Security check', detail: 'Complete the check in this Chrome profile.', kind: 'bad' };
  if (!a.online || a.status === 'offline') return { label: 'Offline', detail: `Last seen ${ago(a.last_seen)} ago`, kind: 'quiet' };
  if (a.cooldown_until && Date.parse(a.cooldown_until) > Date.now()) return { label: 'Instagram limit active', detail: 'See the top status bar for the wait time.', kind: 'wait' };
  if (a.status === 'cooldown') return { label: 'Instagram limit active', detail: 'See the top status bar for the wait time.', kind: 'wait' };
  if (a.paused || a.status === 'paused') return { label: 'Paused', detail: 'Ready when resumed.', kind: 'quiet' };
  return { label: 'Connected', detail: `Seen ${ago(a.last_seen)} ago`, kind: 'ok' };
}
function accountRow(a) {
  const b = a.budget || {}, t = a.today || {}, h = a.hour || {};
  const conf = A.confirm === a.lane_id, access = accountAccess(a);
  const role = ROLES.find(([v]) => v === a.role)?.[1] || 'Unassigned';
  const budget = `${b.list ? `${int(b.list)} list pages/day` : 'No cap'} · ${b.profile ? `${int(b.profile)} bios/day` : 'No cap'}`;
  const name = A.renaming === a.lane_id
    ? `<form class="acc-ren" data-ren><input class="input" id="acc-label" value="${esc(A.renameValue ?? a.label ?? '')}" placeholder="Label, e.g. Scout 2" maxlength="40" autocomplete="off"><button class="btn solid">Save</button><button type="button" class="btn" data-ren-x>Cancel</button></form>`
    : `<div class="acc-identity"><b class="acc-name">${esc(a.handle ? '@' + a.handle : a.label || a.name)}</b>${a.handle && a.label ? `<span class="muted acc-label">${esc(a.label)}</span>` : ''}</div><button class="btn ghost acc-edit" data-rename title="Rename">Rename</button>`;
  return `<section class="acc${access.kind === 'bad' ? ' warn' : ''}" data-lane="${esc(a.lane_id)}">
    <div class="acc-top"><i class="dot ${ST_DOT[a.status] || ''}"></i>${name}${a.is_main ? '<span class="pill">Main</span>' : ''}
      <span class="grow"></span><button class="btn${a.paused ? ' solid' : ''}" data-pause>${a.paused ? 'Resume' : 'Pause'}</button></div>
    <div class="acc-overview">
      <div class="acc-fact"><span class="acc-key">Instagram access</span><b class="acc-access ${access.kind}">${esc(access.label)}</b><small>${esc(access.detail)}</small></div>
      <div class="acc-fact acc-usage"><span class="acc-key">Today · workspace caps</span><b class="num">${int(t.list)} pages · ${int(t.profile)} bios</b><small>${esc(role)}${a.job ? ` · ${esc(jobText(a))}` : ''} · ${esc(budget)}</small>${b.list ? `<div class="bar-p run" aria-label="${int(t.list)} of ${int(b.list)} workspace list pages used"><i style="width:${Math.min(100, (t.list || 0) / b.list * 100)}%"></i></div>` : ''}</div>
    </div>
    <div class="acc-bottom"><span class="muted">${int(h.people)} people this hour${a.last_limit ? ` · Instagram last slowed this profile ${ago(a.last_limit)} ago` : ''}</span><details class="adv acc-more"><summary>Advanced settings</summary>
    <div class="acc-ctl">
      <div class="seg" title="What this account collects">${ROLES.map(([v, l]) => `<button data-role="${v}" aria-pressed="${a.role === v}" class="${a.role === v ? 'on' : ''}">${l}</button>`).join('')}</div>
      <button class="toggle${a.is_main ? ' on' : ''}" data-main aria-pressed="${!!a.is_main}" title="Your own account: bios only, unless Settings gives it a share of the lists"><i></i><span>Main account</span></button>
      <span class="grow"></span>
      <form class="acc-bud" data-bud>
        <label><input class="input" type="number" min="0" max="3000" data-b="list" value="${a.budget_custom ? esc(b.list) : ''}" placeholder="${esc(b.list)}" inputmode="numeric" title="0 = no workspace daily cap"><span class="muted">list pages/day</span></label>
        <label><input class="input" type="number" min="0" max="5000" data-b="profile" value="${a.budget_custom ? esc(b.profile) : ''}" placeholder="${esc(b.profile)}" inputmode="numeric" title="0 = no workspace daily cap"><span class="muted">bios/day</span></label>
        <button class="btn">Save</button>
      </form>
    </div>
    <div class="acc-foot"><span class="muted num">${a.version ? 'Extension v' + esc(a.version) + ' · ' : ''}Seen ${ago(a.last_seen)} ago${a.last_error && a.status !== 'running' ? ' · Last error: ' + esc(a.last_error.slice(0, 100)) : ''}</span>
      <span class="grow"></span>
      <button class="btn ${conf ? 'danger' : 'ghost'}" data-remove>${conf ? 'Confirm remove' : 'Remove'}</button></div>
    </details></div>
  </section>`;
}

function renderAccounts() {
  const sc = S.sc;
  const accs = sc?.accounts || [], alerts = sc?.alerts || [];
  $('#acc-start').disabled = A.starting || !sc || !!S.scStale || !accs.length;
  $('#acc-start').textContent = A.starting ? 'Starting…' : 'Start all';
  $('#acc-alerts').innerHTML = alerts.map((x) => `<div class="alert ${x.level}"><i></i><span>${esc(x.text)}</span></div>`).join('');
  const r = sc?.rate || {};
  const online = accs.filter((a) => a.online).length;
  const bios = accs.reduce((n, a) => n + (a.today?.profile || 0), 0), pages = accs.reduce((n, a) => n + (a.today?.list || 0), 0);
  const kpi = (label, val, sub) => `<div class="tile"><span>${label}</span><b class="num">${val}</b>${sub ? `<small>${sub}</small>` : ''}</div>`;
  $('#acc-kpis').innerHTML = [
    kpi('Online', `${online}/${accs.length}`, ''),
    kpi('People this hour', int(r.people_last_hour), ''),
    kpi('Read today', `${int(pages)} pages · ${int(bios)} bios`, ''),
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
  let saved = false;
  try {
    const r = await api.post(`/api/accounts/${encodeURIComponent(lane)}`, body);
    if (a && r.account) Object.assign(a, r.account);
    saved = true;
    renderAccounts(); renderStatus();
    if (msg) toast(msg);
  } catch (e) { toast(e.status === 400 ? ucf(e.message) : 'Could not save'); }
  finally { A.busy.delete(lane); }
  loadScraper();
  return saved;
}
$('#acc-list').addEventListener('input', (e) => { if (e.target.id === 'acc-label') A.renameValue = e.target.value; });
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
  if (t.hasAttribute('data-rename')) { A.renameValue = null; A.renaming = lane; A.confirm = null; return renderAccounts(); }
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
$('#acc-list').addEventListener('submit', async (e) => {
  e.preventDefault();
  const row = e.target.closest('[data-lane]'), lane = row?.dataset.lane;
  if (!lane) return;
  if (e.target.hasAttribute('data-ren')) {
    const label = $('#acc-label').value.trim();
    A.renameValue = $('#acc-label').value;
    const saved = await editAccount(lane, { label: label || null }, label ? `Renamed to ${label}` : 'Label cleared');
    if (saved) { A.renaming = null; A.renameValue = null; document.activeElement?.blur(); renderAccounts(); }
    else $('#acc-label')?.focus();
    return;
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
$('#acc-start').addEventListener('click', async () => {
  if (A.starting) return;
  A.starting = true;
  renderAccounts();
  try {
    const result = await api.post('/api/control', { action: 'start_all' });
    if (result?.ok === false) throw new Error(result.error || 'Could not start');
    window.dispatchEvent(new Event('fl:control-changed'));
    toast('Started lists, bios and AI. Cooldowns still apply.');
    await loadScraper();
  } catch (e) {
    toast(e.message || 'Could not start all');
  } finally {
    A.starting = false;
    renderAccounts();
  }
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
const SET = { llm: null, health: null, tests: {}, models: null, confirm: null, share: null, dirty: false };
async function loadSettings() {
  loadBiofetch();
  const [llm, acc] = await Promise.allSettled([api.get('/api/llm'), api.get('/api/accounts')]);
  if (llm.status === 'fulfilled') { SET.llm = llm.value; if (!SET.dirty) SET.models = [...SET.llm.models]; }
  if (acc.status === 'fulfilled') SET.share = acc.value.main_list_share;
  renderSettings();
  if (!SET.health) checkHealth();
  loadScout();
}
// Leadscout: which model the Hermes agent runs on, how many at once, and what it used this week.
async function loadScout() {
  try { SET.scout = await api.get('/api/scout'); } catch (e) { SET.scout = null; }
  renderScout();
}
function renderScout() {
  const sc = SET.scout, el = $('#set-scout');
  if (!el) return;
  if (!sc) { el.innerHTML = '<div class="set-row muted">Could not load leadscout status.</div>'; return; }
  const tok = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n);
  const use = sc.usage.length ? sc.usage.map((u) => `<div class="kv-row"><span>${esc(u.model)}<small class="muted"> · ${esc(u.provider || '')}</small></span>
      <span class="num">${int(u.runs)} runs · ${tok(u.tokens_in + u.tokens_out)} tokens</span></div>`).join('') : '<div class="muted">No runs this week.</div>';
  el.innerHTML = `
    <div class="set-row"><div><b>Check the best leads with Hermes</b><span class="muted">${sc.available ? `${int(sc.done_today)} checked today · ${int(sc.done)} in total · ${int(sc.waiting)} waiting` : 'Hermes (hermesme) not found on this Mac'}</span></div>
      <button class="toggle${sc.on ? ' on' : ''}" id="scout-on" role="switch" aria-checked="${sc.on}" aria-label="Leadscout on"><i></i></button></div>
    <div class="set-row"><div><b>Model</b><span class="muted">If it fails, the agent falls back to Gemma, then Grok, then Nemotron.</span></div>
      <div class="seg" id="scout-model">${sc.models.map((m) => `<button data-m="${esc(m.id)}" class="${sc.model === m.id ? 'on' : ''}" aria-pressed="${sc.model === m.id}" title="${esc(m.label)}">${esc(m.label.split(' (')[0])}</button>`).join('')}</div></div>
    <div class="set-row"><div><b>Agents at once</b><span class="muted">About 10–25 s per lead each.</span></div>
      <div class="seg" id="scout-workers">${[2, 3, 4, 6, 8].map((n) => `<button data-w="${n}" class="${sc.workers === n ? 'on' : ''}" aria-pressed="${sc.workers === n}">${n}</button>`).join('')}</div></div>
    <div class="sub-h"><b>Used in the last 7 days</b><span class="muted">From Hermes' own records. SuperGrok's weekly % is only shown in the Grok app.</span></div>
    <div class="kv-list">${use}</div>`;
}
$('#set-scout')?.addEventListener('click', async (e) => {
  const body = e.target.closest('#scout-on') ? { on: !SET.scout?.on }
    : e.target.closest('[data-m]') ? { model: e.target.closest('[data-m]').dataset.m }
    : e.target.closest('[data-w]') ? { workers: +e.target.closest('[data-w]').dataset.w } : null;
  if (!body) return;
  try { SET.scout = await api.post('/api/settings/scout', body); renderScout(); toast('Saved'); } catch (err) { toast('Could not save'); }
});
async function checkHealth() {
  SET.health = 'checking'; renderServices();
  try { SET.health = await api.get('/api/llm/health'); } catch (e) { SET.health = 'failed'; }
  renderServices();
}
const upText = (up) => (SET.health === 'failed' ? 'Could not check' : up == null ? 'Checking' : up ? 'Running' : 'Not running');
function renderServices() {
  const h = SET.health && typeof SET.health === 'object' ? SET.health : null, l = SET.llm;
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
  $('#set-q-on').setAttribute('aria-checked', String(!!sc?.qualify));
  $('#set-q-auto').setAttribute('aria-checked', String(!!sc?.qualify_auto));
  const f = $('#set-q');
  if (l && !f.contains(document.activeElement)) { $('#set-workers').value = l.workers; $('#set-llm-min').value = l.llm_min; $('#set-bio-min').value = l.bio_min; }
  const bL = $('#b-list'), bP = $('#b-profile'), bud = sc?.ext?.budget || {};
  if (document.activeElement !== bL && document.activeElement !== bP) { bL.value = bud.list ?? ''; bP.value = bud.profile ?? ''; }
  syncLook();
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
  $('#set-models').innerHTML = ms.length ? ms.map((m, i) => `<li><span class="num muted">${i + 1}</span><code>${esc(m)}</code>${/:free$|^stealth\//.test(m) ? '<span class="pill">Free</span>' : ''}<span class="grow"></span>
    <button class="btn ghost" data-mup="${i}" title="Try earlier"${i ? '' : ' disabled'}>Up</button><button class="btn ghost" data-mdown="${i}" title="Try later"${i < ms.length - 1 ? '' : ' disabled'}>Down</button><button class="btn ghost" data-mdel="${i}">Remove</button></li>`).join('')
    : '<li class="muted">No models</li>';
  $('#set-models-save').disabled = !SET.dirty;
  // OpenRouter's current free catalogue (checked daily by the server); one click adds a model to the list above.
  const auto = SET.llm?.auto_models || {};
  const free = [...new Set([...(auto.stealth || []), ...(auto.free || [])])];
  $('#set-free-at').textContent = auto.at ? `${free.length} models · checked ${ago(auto.at)} ago` : auto.error ? 'Could not reach OpenRouter' : 'Not checked yet';
  $('#set-free').innerHTML = free.length ? free.map((m) => `<li><code>${esc(m)}</code>${(auto.new_stealth || []).includes(m) ? '<span class="pill">New</span>' : ''}<span class="grow"></span>
    ${ms.includes(m) ? '<span class="muted">In use</span>' : `<button class="btn ghost" data-madd="${esc(m)}">Add</button>`}</li>`).join('')
    : '<li class="muted">None found</li>';
}
$('#set-free').addEventListener('click', (e) => {
  const m = e.target.closest('[data-madd]')?.dataset.madd;
  if (!m || !SET.models || SET.models.includes(m)) return;
  SET.models.push(m); SET.dirty = true; renderModels(); toast('Added. Press Save to use it.');
});
$('#set-free-refresh').onclick = async (e) => {
  e.target.disabled = true;
  try { const r = await api.post('/api/llm/models/refresh', {}); if (SET.llm) SET.llm.auto_models = r.auto; renderModels(); toast('Model list updated'); }
  catch { toast('Could not reach OpenRouter'); }
  finally { e.target.disabled = false; }
};
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
function qualificationConnections(r) {
  const byHandle = new Map();
  for (const edge of r.connection_edges || []) {
    if (!edge?.handle || !['followers', 'following'].includes(edge.direction)) continue;
    const key = String(edge.handle).toLowerCase();
    if (!byHandle.has(key)) byHandle.set(key, { handle: edge.handle, dirs: new Set(), isMe: false });
    const item = byHandle.get(key);
    item.dirs.add(edge.direction);
    item.isMe ||= !!edge.is_me;
  }
  const observed = [...byHandle.values()].sort((a, b) => Number(b.isMe) - Number(a.isMe) || a.handle.localeCompare(b.handle));
  const lines = observed.map(({ handle, dirs, isMe }) => {
    const both = dirs.has('followers') && dirs.has('following');
    if (isMe) return both ? 'You (@' + handle + ') follow each other' : dirs.has('followers') ? 'They follow you (@' + handle + ')' : 'You (@' + handle + ') follow them';
    return both ? 'They and @' + handle + ' follow each other' : dirs.has('followers') ? 'They follow @' + handle : '@' + handle + ' follows them';
  });
  if (!lines.length && !Array.isArray(r.connection_edges)) {
    // Older sample payloads have list names but not direction.
    lines.push(...[...new Set(r.via || [])].map((handle) => 'Seen in @' + handle + "'s list"));
  }
  if (!lines.length) return '<p class="muted">No observed follows yet</p>';
  const row = (line) => `<li>${esc(line)}</li>`;
  return `<ul class="ql-connections">${lines.slice(0, 3).map(row).join('')}</ul>${lines.length > 3 ? `<details class="ql-extra"><summary>Show ${lines.length - 3} more</summary><ul class="ql-connections">${lines.slice(3).map(row).join('')}</ul></details>` : ''}`;
}
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
    const kpi = (v, l) => `<div class="tile"><span>${l}</span><b class="num">${v}</b></div>`;
    $('#ql-prog').innerHTML = `<div class="tiles">${kpi(int(s.ai ?? 0), 'Checked by AI')}${kpi(int(s.rules ?? 0), 'Keyword check only')}${kpi(int(Qp.left ?? 0), 'Waiting for AI')}${kpi(Qp.per_hour == null ? '–' : int(Qp.per_hour), 'AI checks per hour')}</div>
      <div class="ql-pbar"><div class="bar-p ${on && Qp.left ? 'run' : 'done'}"><i style="width:${pct}%"></i></div>
      <span class="muted">${esc(when)}${on ? ` · ${plural(Qp.workers || 0, 'check')} at a time · ${plural(Qp.keys || 0, 'OpenRouter key')}` : ''}</span></div>`;
    $('#ql-toggle').textContent = on ? 'Pause AI checks' : 'Start AI checks';
    $('#ql-toggle').classList.toggle('solid', !on);
    $('#n-qual').textContent = on && Qp.left ? fmt(Qp.left) : '';
  },
  card(r) {
    const v = r.verdict || {}, ai = v.model && v.model !== 'rules';
    const ev = evidenceOf(v);
    const aiTags = (r.tags || []).filter((t) => t.grp === 'ai');
    const bio = (r.bio || '').trim();
    const facts = [r.category ? esc(r.category) : '', r.followers != null ? `${fmt(r.followers)} followers` : ''].filter(Boolean).join(' · ');
    const siteHTML = websiteEvidence(r.site);
    const busy = this.busy.has(r.id);
    return `<article class="ql-card" data-id="${r.id}">
      <div class="ql-top">${avatar(r.pic, r.name || r.handle, 'lg')}
        <div class="who"><b>${esc(r.name || r.handle)}</b><span>@${esc(r.handle)}${r.status ? ' · ' + esc(ucf(r.status)) : ''}</span></div>
        <div class="ql-score"><b class="num" title="Blended priority">Priority ${r.score ?? '–'}</b>${fitBadge(r)}</div></div>
      <div class="ql-why"><p><b>${esc(ROLE_LABEL[r.role] || ucf(r.role || 'Unknown'))}.</b> ${esc(r.reason || 'No reason given.')}</p>
        ${aiTags.length ? `<div class="ql-tags">${aiTags.map((t) => tagChip(t)).join('')}</div>` : ''}
        ${ev.length ? `<details class="ql-extra"><summary>Why this verdict</summary><ul class="evidence">${ev.map((q) => `<li>"${esc(q)}"</li>`).join('')}</ul></details>` : ''}
        <p class="ql-by">${ai ? 'AI checked' : 'Keyword check'}${v.at ? ` · ${ago(v.at)} ago` : ''}</p></div>
      <div class="ql-profile">${facts ? `<p class="ql-factline">${facts}</p>` : ''}<p>${bio ? esc(bio.length > 140 ? bio.slice(0, 140) + '…' : bio) : r.is_private ? 'Private profile' : 'Bio not read yet'}</p>
        ${r.website && safeUrl(r.website) ? `<a href="${esc(safeUrl(r.website))}" target="_blank" rel="noopener">${esc(r.website.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, ''))}</a>` : ''}</div>
      <div class="ql-network"><h4>Observed follows</h4>${qualificationConnections(r)}</div>
      ${siteHTML}
      <div class="ql-acts"><button class="btn${busy ? '' : ' solid'}" data-deep="${r.id}" ${busy ? 'disabled' : ''}>${busy ? 'Reading…' : 'Dig deeper'}</button>
        <span class="grow"></span><button class="btn ghost" data-open="${r.id}">Open lead</button>
        <a class="btn ghost ql-instagram" href="https://www.instagram.com/${encodeURIComponent(r.handle)}/" target="_blank" rel="noopener">Instagram ↗</a></div>
    </article>`;
  },
  render() {
    $('#ql-n').textContent = `${int(this.total)} ${this.total === 1 ? 'person' : 'people'}`;
    const focused = $('#ql-list').contains(document.activeElement) ? document.activeElement : null;
    const focusAttr = focused?.hasAttribute('data-deep') ? 'data-deep' : focused?.hasAttribute('data-open') ? 'data-open' : null;
    const focusId = focusAttr ? focused.getAttribute(focusAttr) : null;
    $('#ql-list').innerHTML = this.rows.length ? this.rows.map((r) => this.card(r)).join('')
      : `<div class="muted ql-empty">${this.q ? 'No people match this search.' : this.view === 'ai' ? 'Nobody has been checked by AI yet. Press Resume on AI at the top; results appear here.' : 'Nobody matches.'}</div>`;
    if (focusAttr) $(`#ql-list [${focusAttr}="${focusId}"]`)?.focus({ preventScroll: true });
    $('#ql-more').hidden = this.rows.length >= this.total;
  },
  async deeper(id) {
    const restoreFocus = document.activeElement?.dataset.deep === String(id);
    this.busy.add(id); this.render();
    try {
      const d = await api.post(`/api/qual/${id}/deeper`);
      const r = this.rows.find((x) => x.id === id);
      if (r && d.site) r.site = d.site;
      toast(ucf(d.note || 'Done'));
    } catch (e) { toast(e.status === 400 ? ucf(e.message) : 'Could not dig deeper'); }
    this.busy.delete(id); this.render();
    if (restoreFocus && document.activeElement === document.body && S.view === 'qual') {
      $(`#ql-list [data-deep="${id}"]`)?.focus({ preventScroll: true });
    }
  },
};
$('#ql-view').addEventListener('click', (e) => { const b = e.target.closest('[data-v]'); if (!b) return; Q.view = b.dataset.v; Q.fellBack = true; Q.syncSeg(); Q.load(); });
$('#ql-q').addEventListener('input', debounce((e) => { Q.q = e.target.value.trim(); Q.load(); }, 250));
$('#ql-sort').addEventListener('change', (e) => { Q.sort = e.target.value; Q.load(); });
$('#ql-more').onclick = () => Q.load(true);
$('#ql-toggle').onclick = async () => { await toggleQualify(); Q.renderProg(); };
$('#ql-list').addEventListener('click', (e) => {
  const d = e.target.closest('[data-deep]'); if (d) return Q.deeper(+d.dataset.deep);
  const o = e.target.closest('[data-open]');
  if (o) { const r = Q.rows.find((x) => x.id === +o.dataset.open); S.f = emptyFilter(); S.f.q = r ? r.handle : ''; $('#q').value = S.f.q; S.view = 'leads'; filtersChanged(); setView('leads'); openDetail(+o.dataset.open); }
});

// ---------- map ----------
const LEAD_R = [0, 4, 5.6, 7, 8.2, 9.4];
const JUDGE_COLOR = { good: '#ff8a1f', bad: '#e5484d' };
// Profile photos for the map, pre-cropped to circles on small canvases.
const MAP_PIC_LIMIT = 3200;
const PICS = new Map(); let picsLoading = 0; const picQueue = [];
function mapPic(url) {
  let e = PICS.get(url);
  if (e) { PICS.delete(url); PICS.set(url, e); }
  else {
    // Keep the cache and pending work bounded across filters and refreshes.
    if (PICS.size >= MAP_PIC_LIMIT) {
      let victim;
      for (const pair of PICS) if (pair[1].state !== 'loading') { victim = pair; break; }
      if (!victim) return null;
      PICS.delete(victim[0]);
      const queued = picQueue.indexOf(victim[0]);
      if (queued !== -1) picQueue.splice(queued, 1);
    }
    PICS.set(url, (e = { state: 'queued', c: null })); picQueue.push(url); pumpPics();
  }
  return e.state === 'ok' ? e.c : null;
}
function pumpPics() {
  while (picsLoading < 8 && picQueue.length) {
    const url = picQueue.shift(), e = PICS.get(url);
    if (!e || e.state !== 'queued') continue;
    e.state = 'loading'; picsLoading++;
    const im = new Image(); im.decoding = 'async';
    const finish = (canvas) => {
      e.c = canvas; e.state = canvas ? 'ok' : 'err'; picsLoading--;
      if (canvas) M.schedule();
      pumpPics();
    };
    im.onload = () => {
      try {
        const s = 64, c = document.createElement('canvas'); c.width = c.height = s;
        const g = c.getContext('2d'), m = Math.min(im.naturalWidth, im.naturalHeight);
        if (!g || !m) return finish(null);
        g.beginPath(); g.arc(s / 2, s / 2, s / 2, 0, Math.PI * 2); g.clip();
        g.drawImage(im, (im.naturalWidth - m) / 2, (im.naturalHeight - m) / 2, m, m, 0, 0, s, s);
        finish(c);
      } catch (_) { finish(null); }
    };
    im.onerror = () => finish(null);
    im.src = url;
  }
}
const MARKED = new Set(['interested', 'contacted', 'talking', 'client']);
// At overview scale several people can occupy the same few screen pixels. Keep
// the most useful dot in each cell; search, hover and selection remain visible.
function mapDots(leads, k, x, y, w, h, important) {
  const dense = leads.length > 1500 && k < 0.85;
  const dots = [], priority = [], cells = dense ? new Map() : null;
  for (const n of leads) {
    const px = n.x * k + x, py = n.y * k + y, r = Math.max(n.r * k, 2.2);
    if (px + r < -20 || px - r > w + 20 || py + r < -20 || py - r > h + 20) continue;
    if (!dense) { dots.push(n); continue; }
    if (important.has(n.id)) { priority.push(n); continue; }
    const key = `${Math.floor(px / 4)},${Math.floor(py / 4)}`;
    const prior = cells.get(key);
    const rank = (n.L || 0) * 10 + (MARKED.has(n.status) ? 25 : 0) + (n.judge ? 15 : 0);
    if (!prior || rank >= prior.rank) cells.set(key, { n, rank });
  }
  if (cells) for (const { n } of cells.values()) dots.push(n);
  return dots.concat(priority);
}
const M = {
  sim: null, nodes: [], seeds: [], leads: [], links: [], historyLinks: [], seedLinks: [], byId: new Map(), nbr: new Map(), selfRelation: new Map(), rev: null, scope: 'leads',
  k: 1, x: 0, y: 0, w: 0, h: 0, hover: null, focus: null, matches: [], mi: -1, labels: store.get('labels', true),
  loaded: false, stale: true, fitted: false, timer: null, raf: 0, maxShared: 1, shown: false, loading: false, loadSeq: 0,
  limit: 400, rawData: null, audienceKey: null, dataRev: null,
  show() {
    this.shown = true;
    this.resize();
    if (!this.loaded || this.stale) this.load();
    clearInterval(this.timer); this.timer = setInterval(() => { if (!document.hidden) this.load(true); }, 30000);
    if (this.sim && this.sim.alpha() > this.sim.alphaMin()) this.sim.restart();
    $('#map-labels').classList.toggle('on', this.labels); $('#map-labels').setAttribute('aria-pressed', String(this.labels));
    $('#map-density').value = String(this.limit);
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
    const p = LeadWorkflow.runtimeQuery(toQuery(S.f, S.sort, false));
    p.set('scope', this.scope); p.set('limit', this.limit);
    return '/api/map?' + p;
  },
  async load(poll) {
    if (this.loading && poll) return;
    const seq = ++this.loadSeq;
    this.loading = true;
    const url = this.url();
    let d;
    try { d = await api.get(url); } catch (e) {
      if (seq !== this.loadSeq) return;
      this.loading = false;
      if (!this.nodes.length) this.status(offlineSince ? 'Server offline' : 'Could not load map');
      return;
    }
    if (seq !== this.loadSeq) return;
    this.loading = false;
    if (url !== this.url()) return;
    this.loaded = true; this.stale = false;
    const key = d.rev + '|' + url;
    if (key === this.rev && this.nodes.length) return;
    const audience = url.replace(/&limit=\d+$/, '');
    let shown = d;
    // A smaller ranked sample can omit the open person. Keep that one known
    // node and its recorded links only while audience and data revision agree.
    const selectedId = this.focus?.kind === 'seed' ? null : this.focus?.id || (S.open ? 'p:' + S.open : null);
    if (selectedId && this.audienceKey === audience && this.dataRev === d.rev && this.rawData &&
        !d.nodes.some((n) => n.id === selectedId)) {
      const previous = this.rawData.nodes.find((n) => n.id === selectedId);
      if (previous) shown = { ...d, nodes: [...d.nodes, previous],
        links: [...d.links, ...this.rawData.links.filter((l) => l.source === selectedId || l.target === selectedId)],
        keptSelected: true };
    }
    this.rawData = d; this.audienceKey = audience; this.dataRev = d.rev;
    this.rev = key;
    this.build(shown);
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
    this.drawnLeads = null;
    // One line per seed-person pair and evidence state. Historical lines never count as current neighbours.
    const pair = new Map();
    for (const l of d.links || []) {
      if (!this.byId.has(l.source) || !this.byId.has(l.target)) continue;
      const state = ['observed', 'absent', 'unverified'].includes(l.state) ? l.state : 'unverified';
      const key = l.source + '>' + l.target + ':' + state, had = pair.get(key);
      if (had) { if (had.dir !== l.direction) had.dir = 'both'; }
      else pair.set(key, { source: l.source, target: l.target, dir: l.direction || 'followers', state });
    }
    this.links = [...pair.values()].filter((l) => l.state === 'observed');
    const selfId = this.seeds.find((n) => n.is_me)?.id;
    this.selfRelation = new Map(this.links.filter((l) => l.source === selfId).map((l) => [l.target, l.dir]));
    this.historyLinks = [...pair.values()].filter((l) => l.state !== 'observed')
      .map((l) => ({ ...l, source: this.byId.get(l.source), target: this.byId.get(l.target) }));
    this.seedLinks = (d.seed_links || []).map((l) => ({ source: sid(l.source), target: sid(l.target), shared: +l.shared || 0 }))
      .filter((l) => this.byId.has(l.source) && this.byId.has(l.target) && l.shared > 0);
    this.maxShared = Math.max(1, ...this.seedLinks.map((l) => l.shared));
    const neighbours = new Map(this.nodes.map((n) => [n.id, new Set()]));
    const outgoing = new Map(this.seeds.map((n) => [n.id, new Set()]));
    for (const l of this.links) {
      neighbours.get(l.source).add(l.target);
      neighbours.get(l.target).add(l.source);
      outgoing.get(l.source)?.add(l.target);
    }
    this.nbr = new Map([...neighbours].map(([id, ids]) => [id, [...ids]]));
    for (const n of this.seeds) n.vis = outgoing.get(n.id).size;
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
    // A golden-angle spiral spreads dense groups without an all-pairs force.
    // The 10k view keeps this linear-time placement and a bounded click adjustment.
    const placedByGroup = new Map();
    for (const n of this.leads) {
      if (n.x != null) continue;
      const ss = this.nbr.get(n.id).map((id) => this.byId.get(id)).filter(Boolean);
      const cx = ss.reduce((a, s) => a + s.x, 0) / (ss.length || 1), cy = ss.reduce((a, s) => a + s.y, 0) / (ss.length || 1);
      const group = ss.map((s) => s.id).sort().join('|') || 'unlinked';
      const i = placedByGroup.get(group) || 0;
      placedByGroup.set(group, i + 1);
      const a = i * 2.399963229728653, rr = 38 + Math.sqrt(i) * 14;
      n.x = cx + Math.cos(a) * rr; n.y = cy + Math.sin(a) * rr;
    }
    const nl = this.leads.length;
    const total = Math.max(nl, Number(d.total) || 0);
    $('#map-count').textContent = `${int(nl)} of ${int(total)} matching people shown · ${plural(this.seeds.length, 'source account')}${d.keptSelected ? ' · selected person kept on map' : ''}`;
    const absent = this.historyLinks.filter((l) => l.state === 'absent').length;
    const unverified = this.historyLinks.length - absent;
    $('#map-count').title = `Displayed connections: ${int(this.links.length)} observed, ${int(absent)} absent, ${int(unverified)} unverified. Historical links do not count toward current neighbours or source degrees. Other matching people may be outside this sample; choose a larger Show setting to see more.`;
    const me = this.seeds.find((n) => n.is_me);
    const meButton = $('#map-me');
    meButton.hidden = !me;
    if (me) meButton.textContent = `You · @${me.label}`;
    if (this.focus) this.focus = this.byId.get(this.focus.id) || null;
    if (this.hover) this.hover = this.byId.get(this.hover.id) || null;
    this.simulate(old.size ? 0.5 : 1);
    this.search();
    if (!nl) this.draw();
  },
  simulate(alpha) {
    if (this.sim) this.sim.stop();
    const F = window.d3;
    if (!F?.forceSimulation) { this.status('Map library missing'); return; }
    const big = this.nodes.length > 2500, huge = this.nodes.length > 6000;   // 10k people: shorter reach, faster settle
    this.seedLinks.forEach((l) => { l.ss = true; });
    const all = [...this.links, ...this.seedLinks];
    this.sim = F.forceSimulation(this.nodes)
      .force('link', F.forceLink(all).id((n) => n.id)
        .distance((l) => l.ss ? 520 - 360 * Math.sqrt(l.shared / this.maxShared) : l.source.r + 26 + (l.target.L > 1 ? 30 : 10) + Math.sqrt(l.source.vis || 1) * 1.6)
        .strength((l) => l.ss ? 0.04 + 0.5 * (l.shared / this.maxShared) : (huge ? 0.008 : 0.9) / Math.max(1, l.target.L)))
      // The 10k sample already starts in seed-centred clusters. On that scale,
      // all-node charge and collision dominate each tick without adding useful
      // detail at overview zoom; links and the seed force still refine it.
      .force('charge', huge ? null : F.forceManyBody().strength((n) => n.kind === 'seed' ? -30 : -14).distanceMax(big ? 200 : 300).theta(big ? 1.1 : 0.9))
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
      .force('collide', huge ? null : F.forceCollide((n) => n.kind === 'seed' ? n.r * 1.45 + 6 : n.r + 1.8).iterations(1).strength(0.8))
      .force('x', F.forceX(0).strength((n) => n.kind === 'seed' ? 0.02 : 0.004)).force('y', F.forceY(0).strength((n) => n.kind === 'seed' ? 0.02 : 0.004))
      .alpha(alpha).alphaDecay(huge ? 0.08 : big ? 0.055 : 0.035).alphaMin(huge ? 0.02 : big ? 0.012 : 0.001).velocityDecay(0.42)
      .on('tick', () => this.schedule())
      .on('end', () => { if (this.autoFit) this.fit(); });
    // The seed-centroid layout is usable immediately. D3 settles in later
    // frames; a synchronous tick loop could block input for hundreds of ms.
    if (alpha >= 1) this.fit();
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
  relax(n, frames = 14, limit = 80) {
    const near = () => { const R = n.r + 90; return this.nodes.filter((m) => m !== n && Math.abs(m.x - n.x) < R && Math.abs(m.y - n.y) < R); };
    // Drag release only adjusts the closest neighbours. The old all-pairs
    // loop could lock the tab when thousands of dots shared a small region.
    let list = near().sort((a, b) => (a.x - n.x) ** 2 + (a.y - n.y) ** 2 -
      ((b.x - n.x) ** 2 + (b.y - n.y) ** 2)).slice(0, limit);
    const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
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
      if (!reduced) this.draw();
      if (moved && --frames > 0) { if (reduced) step(); else requestAnimationFrame(step); }
    };
    if (list.length) { if (reduced) { step(); this.draw(); } else requestAnimationFrame(step); }
  },
  toggleLabels() { this.labels = !this.labels; store.set('labels', this.labels); $('#map-labels').classList.toggle('on', this.labels); $('#map-labels').setAttribute('aria-pressed', String(this.labels)); this.draw(); },
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
    const overview = this.leads.length > 250 && k < 0.85;
    const on = (n) => hd ? hd.set.has(n.id) : match && match.size ? match.has(n.id) : true;
    // Viewport culling bounds in world coords.
    const vx0 = -this.x / k - 20, vy0 = -this.y / k - 20, vx1 = (this.w - this.x) / k + 20, vy1 = (this.h - this.y) / k + 20;
    const selected = (S.open && this.byId.get('p:' + S.open)) || this.focus;
    const important = new Set(match || []);
    if (selected) important.add(selected.id);
    if (this.hover) important.add(this.hover.id);
    const dots = mapDots(this.leads, k, this.x, this.y, this.w, this.h, important);
    this.drawnLeads = dots;
    const visibleEdge = (l) => Math.max(l.source.x, l.target.x) >= vx0 && Math.min(l.source.x, l.target.x) <= vx1 &&
      Math.max(l.source.y, l.target.y) >= vy0 && Math.min(l.source.y, l.target.y) <= vy1;
    const shownEdge = (l) => visibleEdge(l) && (!overview || !hd || l.source === hd.n || l.target === hd.n);
    const drawnLinks = overview && !hd ? [] : this.links.filter(shownEdge);
    const drawnHistory = overview && !hd ? [] : this.historyLinks.filter(shownEdge);

    // Seed overlap edges, weighted by shared people.
    for (const l of overview && !hd ? this.seedLinks.slice(0, 12) : this.seedLinks) {
      const a = l.source, b = l.target;
      const w = 1 + 5 * (l.shared / this.maxShared);
      const hot = hd && (hd.n === a || hd.n === b);
      c.globalAlpha = hd ? (hot ? 0.8 : 0.04) : overview ? 0.18 : 0.28;
      c.strokeStyle = hot ? fg2 : fg4; c.lineWidth = Math.max(w, 1 / k);
      c.beginPath(); c.moveTo(a.x, a.y); c.lineTo(b.x, b.y); c.stroke();
    }
    // Historical links have distinct styles and do not participate in layout or connection counts.
    const historicalPass = (state, alpha, dash) => {
      c.globalAlpha = alpha;
      c.setLineDash(dash); c.beginPath();
      for (const l of drawnHistory) if (l.state === state &&
          (!overview || (hd && (l.source === hd.n || l.target === hd.n)))) {
        c.moveTo(l.source.x, l.source.y); c.lineTo(l.target.x, l.target.y);
      }
      c.stroke();
      c.setLineDash([]);
    };
    c.strokeStyle = line; c.lineWidth = 1 / k;
    historicalPass('absent', overview && hd ? 0.45 : dim ? 0.04 : 0.18, [1 / k, 6 / k]);
    historicalPass('unverified', overview && hd ? 0.55 : dim ? 0.06 : 0.28, [6 / k, 5 / k]);
    // Solid hairline = they follow the seed (or both ways); dashed = the seed follows them.
    const edgePass = (multi, alpha, selectedOnly = false) => {
      c.globalAlpha = alpha;
      for (const dashed of [false, true]) {
        c.setLineDash(dashed ? [3 / k, 3 / k] : []); c.beginPath();
        for (const l of drawnLinks) if ((l.target.L > 1) === multi && (l.dir === 'following') === dashed &&
            (!selectedOnly || l.source === hd.n || l.target === hd.n)) {
          c.moveTo(l.source.x, l.source.y); c.lineTo(l.target.x, l.target.y);
        }
        c.stroke();
      }
      c.setLineDash([]);
    };
    // Crowded maps fade the single-list lines so clusters and coloured dots stay readable.
    const crowd = this.leads.length > 5000 ? 0.4 : this.leads.length > 1500 ? 0.7 : 1;
    if (!overview) {
      edgePass(false, dim ? 0.03 : 0.1 * crowd);
      c.strokeStyle = fg4; edgePass(true, dim ? 0.06 : 0.34 * crowd);
    }
    if (hd) {
      c.strokeStyle = fg2; c.lineWidth = 1.2 / k;
      edgePass(false, 0.85, true); edgePass(true, 0.85, true);
    }
    // Leads: dots coloured by fit, sized by lists; one batched path per fit, dimmed pass first.
    const minPx = (this.leads.length > 1500 ? 1.7 : 2.2) / k;
    const fitColor = Object.fromEntries(FITS.map((f) => [f, css('--fit-' + f)]));
    fitColor.unread = fg4;   // on the dark canvas the list colour for unread is too faint to find
    const circle = (n, r) => { c.moveTo(n.x + r, n.y); c.arc(n.x, n.y, r, 0, Math.PI * 2); };
    const pass = (filter, fill, alpha) => {
      c.globalAlpha = alpha; c.fillStyle = fill; c.beginPath();
      for (const n of dots) if (filter(n)) circle(n, Math.max(n.r, minPx));
      c.fill();
    };
    // Tags decide the colour: green = good signs, red = red flags; everyone else stays grey by fit. Judged dots draw on top.
    for (const f of [...FITS].reverse()) {
      if (dim) pass((n) => !on(n) && !n.judge && n.fit === f, fitColor[f], 0.18);
      pass((n) => on(n) && !n.judge && n.fit === f, fitColor[f], 1);
    }
    for (const [j, color] of [['bad', JUDGE_COLOR.bad], ['good', JUDGE_COLOR.good]]) {
      if (dim) pass((n) => !on(n) && n.judge === j, color, 0.2);
      pass((n) => on(n) && n.judge === j, color, 1);
    }
    // Photos on top of the dots once they are big enough to read.
    for (const n of dots) {
      if (!n.pic) continue;
      const r = Math.max(n.r, minPx);
      if (r * k < 6) continue;
      const img = mapPic(n.pic);
      if (!img) continue;
      c.globalAlpha = dim && !on(n) ? 0.18 : 1;
      c.drawImage(img, n.x - r, n.y - r, r * 2, r * 2);
    }
    // Marked rings.
    c.globalAlpha = 1; c.strokeStyle = fg; c.lineWidth = 1.4 / k; c.beginPath();
    for (const n of dots) {
      if (!n.status || !MARKED.has(n.status) || !on(n)) continue;
      circle(n, Math.max(n.r, minPx) + 2.2 / k + 1);
    }
    c.stroke();
    // Open / focused node.
    if (selected) { c.strokeStyle = fg; c.lineWidth = 2 / k; c.beginPath(); circle(selected, selected.r + 6 / k); c.stroke(); }
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
      if (n.is_me) {
        c.globalAlpha = 1;
        c.strokeStyle = fg; c.lineWidth = Math.max(1.4, 1.4 / k);
        c.beginPath(); c.arc(n.x, n.y, r + 5 / k, 0, Math.PI * 2); c.stroke();
      }
    }
    c.restore();
    c.globalAlpha = 1;
    this.drawLabels(hd, match, fg, fg2, fg3, bg, sans);
    if (overview && !hd) {
      c.fillStyle = fg3; c.font = '12px ' + sans; c.textAlign = 'left'; c.textBaseline = 'alphabetic';
      c.fillText('Showing strongest shared-list links · Zoom in for recorded follow lines', 16, 28);
    }
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
      const t = n.is_me ? `YOU · @${n.label}` : '@' + n.label;
      const w = c.measureText(t).width + 10, x = sx(n) - w / 2, y = sy(n) + n.r * k + 4;
      if (x > this.w || x + w < 0 || y > this.h || y + 20 < 0) continue;
      const mine = hd && hd.n === n;
      if (!mine && !n.is_me && hit(x, y, x + w, y + 20)) continue;
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
    const imp = (n) => n.L * 10 + (MARKED.has(n.status) ? 25 : 0) + (n.judge === 'good' ? 20 : n.judge === 'bad' ? -10 : 0) + ({ strong: 12, good: 6 }[n.fit] || 0) + Math.log10((n.followers || 1) + 1);
    cand = cand.filter((n) => n && n.kind !== 'seed' && view(n));
    const few = this.leads.length <= 120;
    if (!hd && !(match && match.size) && !few) cand = cand.filter((n) => n.L >= 2 || k > 1.4 || MARKED.has(n.status) || n.fit === 'strong' || n.judge === 'good');
    cand.sort((a, b) => imp(b) - imp(a));
    const overview = this.leads.length > 250 && k < 0.85;
    const max = hd ? (hd.n.kind === 'seed' ? 24 : 12) : (match && match.size) ? 24 : few ? 160 :
      Math.round(Math.min(overview ? 12 : 120, (this.w * this.h / 26000) * Math.max(1, k * k)));
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
  // Hit test against the current drawn dots (or live nodes before first draw).
  // A cached quadtree went stale after reloads (new people could not be clicked) and its fixed 8-unit search radius
  // missed the edge of big photo nodes. Seeds are drawn on top, so they win; then the node the pointer is most inside.
  at(px, py) {
    const k = this.k, x = (px - this.x) / k, y = (py - this.y) / k, minPx = 2.4 / k, pad = 7 / k;
    for (let i = this.seeds.length - 1; i >= 0; i--) {
      const n = this.seeds[i];
      if (Math.hypot(n.x - x, n.y - y) <= n.r + 3 / k) return n;
    }
    let best = null, bestScore = Infinity;
    for (const n of this.drawnLeads || this.leads) {
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
    $('#map-hits').textContent = q ? `${int(this.matches.length)} displayed` : '';
    this.draw();
  },
  next() {
    if (!this.matches.length) return;
    this.mi = (this.mi + 1) % this.matches.length;
    const n = this.matches[this.mi];
    $('#map-hits').textContent = `${this.mi + 1}/${this.matches.length} displayed`;
    this.select(n);
    this.centerOn(n, 1.8);
  },
  select(n) {
    this.focus = n;
    // A seed that is also a person opens that person's panel (status, tags, note) with its lists below.
    if (n.kind === 'seed' && (n.is_me || !n.pid)) openSeed(n); else openDetail(n.kind === 'seed' ? n.pid : +n.id.slice(2));
    this.draw();
    // Opening the panel changes the usable map width. Keep the chosen node
    // comfortably visible and clear only its immediate neighbours.
    requestAnimationFrame(() => {
      if (this.focus !== n || !this.shown) return;
      this.resize();
      this.autoFit = false;
      const marginX = Math.min(110, this.w * 0.22), marginY = Math.min(90, this.h * 0.18);
      const px = n.x * this.k + this.x, py = n.y * this.k + this.y;
      this.x += Math.max(marginX - px, Math.min(0, this.w - marginX - px));
      this.y += Math.max(marginY - py, Math.min(0, this.h - marginY - py));
      this.relax(n, 5, 32);
      this.draw();
    });
  },
};
function hoverCard(n) {
  if (n.kind === 'seed') {
    const ov = (M.overlap.get(n.id) || []).slice(0, 3);
    return `<b>${n.is_me ? 'You · ' : ''}@${esc(n.label)}</b><span>${n.is_me ? 'Your account' : 'Source account'} · ${int(n.degree)} observed people · ${int(n.vis)} shown</span>${ov.map(([id, s]) => `<span>${int(s)} observed in both lists with @${esc(M.byId.get(id)?.label)}</span>`).join('')}`;
  }
  const seeds = (n.seeds || (M.nbr.get(n.id) || []).map((id) => M.byId.get(id)?.label)).filter(Boolean);
  const relation = M.selfRelation.get(n.id);
  const relationText = relation === 'both' ? 'You follow each other' : relation === 'followers' ? 'Follows you' : relation === 'following' ? 'You follow them' : '';
  return `<div class="h-top"><b>${esc(n.name || n.handle || n.label)}</b>${fitBadge(n)}</div><span>@${esc(n.handle || n.label)}${n.followers != null ? ' · ' + fmt(n.followers) + ' followers' : ''}${n.status ? ' · ' + esc(slabel(n.status)) : ''}</span>
    ${relationText ? `<span>${relationText} · recorded follow</span>` : ''}${n.reason ? `<p>${esc(n.reason)}</p>` : ''}${n.note ? `<p class="h-note">${noteIcon(n.note)} ${esc(n.note.length > 120 ? n.note.slice(0, 120) + '…' : n.note)}</p>` : ''}<span>In ${plural(n.L, 'source list')}${seeds.length ? ` · via ${seedList(seeds, 2)}` : ''}</span>`;
}
function openSeed(n) {
  if (S.open) noteQueue.flush(S.open).catch(() => {});
  S.seedCard = n.id; S.open = null; S.person = null;
  $('#detail').hidden = false;
  renderSeedCard(); renderRows();
}
function renderSeedCard() {
  const n = M.byId.get(S.seedCard);
  if (!n) return;
  $('#detail').innerHTML = `
    <div class="d-head"><span class="av lg">${esc(initials(n.label))}</span>
      <div class="who"><b>${n.is_me ? 'You · ' : ''}@${esc(n.label)}${String(n.label).includes('~') ? '' : igLink(n.label)}</b><span>${String(n.label).includes('~') ? 'Archived account identity' : n.is_me ? 'Your Instagram account' : 'Source account'}</span></div>
      <button class="d-close" id="d-close" title="Close (esc)">&times;</button></div>
    ${n.is_me ? '' : '<div class="d-sec"><span class="muted">Profile details appear after this account is read.</span></div>'}
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
      ${via || n.is_me ? `<button class="btn solid" data-sf="${esc(via || 'follows you')}">Filter to ${n.is_me ? 'people who follow you' : '@' + esc(n.label)}</button>` : ''}
      <button class="btn" data-only="${esc(n.label)}">People found via @${esc(n.label)}</button>
      ${String(n.label).includes('~') ? '' : `<a class="btn" href="https://www.instagram.com/${encodeURIComponent(n.label)}/" target="_blank" rel="noopener">Instagram</a>`}</div></div>
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
  c.addEventListener('pointercancel', (e) => {
    pts.delete(e.pointerId); pinch = null; drag = null; c.classList.remove('drag');
  });
  c.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse' && M.hover) { M.hover = null; M.draw(); } $('#hover').hidden = true; });
  c.addEventListener('wheel', (e) => {
    e.preventDefault();
    M.zoomBy(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015)), e.offsetX, e.offsetY);
  }, { passive: false });
})();
$('#map-fit').onclick = () => { M.autoFit = false; M.fit(); };
$('#map-me').onclick = () => { const me = M.seeds.find((n) => n.is_me); if (me) { M.select(me); M.centerOn(me, 1.4); } };
if (window.ResizeObserver) new ResizeObserver(() => { if (S.view === 'map') M.resize(); }).observe($('#stage'));
$('#map-labels').onclick = () => M.toggleLabels();
$('#map-density').onchange = (e) => {
  const limit = Number(e.target.value);
  if (![400, 1000, 3000].includes(limit) || limit === M.limit) return;
  M.limit = limit; M.load();
};
$('#zoom-in').onclick = () => M.zoomBy(1.4);
$('#zoom-out').onclick = () => M.zoomBy(1 / 1.4);
$('#map-q').addEventListener('input', debounce(() => M.search(), 120));
$('#map-q').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); M.next(); } });
$('#map-scope').addEventListener('click', (e) => {
  const b = e.target.closest('[data-v]');
  if (!b || b.dataset.v === M.scope) return;
  $$('#map-scope button').forEach((x) => { x.classList.toggle('on', x === b); x.setAttribute('aria-pressed', String(x === b)); });
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
syncFilterToggle();
(function boot() {
  const { view, qs } = parseHash();
  const p = fromQuery(qs);
  if (qs) { S.f = p.f; S.sort = p.sort; }
  $('#sort').value = S.sort; $('#q').value = S.f.q;
  history.replaceState(null, '', hashFor(view));
  renderFilters(); renderTokens();
  resetLeads();
  loadFacets(); loadCounts(); loadScraper(); loadViews();
  noteQueue.flushAll().catch(() => toast('Some notes are not saved. Your drafts are kept.'));
  setView(view);
})();
setInterval(() => { if (!document.hidden) loadScraperStatus(); }, 5000);
setInterval(() => { if (!document.hidden && SCRAPER_FULL_VIEWS.has(S.view)) loadScraper(); }, 15000);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  if (SCRAPER_FULL_VIEWS.has(S.view)) loadScraper();
  else loadScraperStatus();
});
setInterval(() => { if (!document.hidden) { loadCounts(); loadFacets(); } }, 30000);
setInterval(() => { if (S.view === 'leads' && !document.hidden && S.rows.length && $('#scroll').scrollTop < 5 && !S.open && !S.pick.size) resetLeads(true); }, 45000);
setInterval(() => { if (offlineSince) setOnline(false); if (S.view === 'scraper') renderScraper(); else renderStatus(); }, 1000);
// Due/overdue follow-up lists roll over at local midnight; a queued profile read shows up once it lands.
const refresher = LeadRefresh.createRefreshCoordinator({
  getState: () => ({
    hidden: document.hidden, view: S.view, followUp: S.f.follow_up, loading: S.loading, openId: S.open ?? null,
    personPending: !!(S.person && (S.person.profile_read_pending || S.person.profile_read?.state === 'reading')),
    personLoading: !!S.person?.loading,
  }),
  refreshLeads: () => resetLeads(true),
  loadPerson: (id) => refreshPerson(id),
  onDayChange: () => { if (S.open) renderDetail(); },
});
setInterval(() => refresher.tick(), 30000);
document.addEventListener('visibilitychange', () => refresher.visibilityChanged());

async function loadBiofetch() {
  try { const b = await api.get('/api/settings/biofetch'); $('#bf-on').classList.toggle('on', !!b.on); $('#bf-on').setAttribute('aria-checked', String(!!b.on)); $('#bf-uid').value = b.ig_user_id || '';
    $('#bf-info').textContent = `Business and creator accounts only, via your own Meta app. Today: ${b.hits || 0} bios, ${b.misses || 0} not business${b.last_error ? ' · ' + b.last_error : ''}`; } catch (e) {}
}
$('#bf-on').onclick = async () => {
  const on = !$('#bf-on').classList.contains('on'), body = { on, ig_user_id: $('#bf-uid').value };
  if ($('#bf-tok').value) body.token = $('#bf-tok').value;
  try { await api.post('/api/settings/biofetch', body); $('#bf-tok').value = ''; toast(on ? 'Meta bios on' : 'Meta bios off'); loadBiofetch(); } catch (e) { toast('Could not save'); }
};
