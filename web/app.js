'use strict';
/* Fortunate Leads UI. Plain JS, no build step. */

// ---------- utilities ----------
const { icon, ring } = window.Icons;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// Compact counts: 850, 2.7k, 12.4k, 124k, 1.2M. Full counts (with separators) use int().
const fmt = (v) => {
  if (v == null || v === '' || !Number.isFinite(+v)) return '–';
  const n = Math.round(+v), one = (x) => x.toFixed(1).replace(/\.0$/, '');
  return n >= 999500 ? one(n / 1e6) + 'M' : n >= 1e5 ? Math.round(n / 1e3) + 'k' : n >= 1e3 ? one(n / 1e3) + 'k' : String(n);
};
const int = (n) => n == null || n === '' || !Number.isFinite(+n) ? '–' : Number(n).toLocaleString('en-US');
// Only http(s) URLs become links; anything else (javascript:, data:) is never rendered as an href.
const safeUrl = (u) => typeof u === 'string' && /^https?:\/\//i.test(u.trim()) ? u.trim() : null;
const safePic = (u) => typeof u === 'string' && (/^\/img\/\d+$/.test(u) || /^https?:\/\//i.test(u)) ? u : null;
const initials = (s) => (s || '?').replace(/[^\p{L}\p{N} ]/gu, ' ').trim().split(/\s+/).slice(0, 2).map((w) => w[0]).join('') || '?';
const ago = (t) => {
  if (!t) return '–';
  const s = Math.max(0, (Date.now() - Date.parse(t)) / 1000);
  return s < 10 ? 'moments' : s < 60 ? Math.round(s) + 's' : s < 3600 ? Math.round(s / 60) + 'm' : s < 86400 ? Math.round(s / 3600) + 'h' : Math.round(s / 86400) + 'd';
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
  $('#offline-t').textContent = Date.now() - offlineSince < 60000 ? '' : 'Offline for ' + ago(new Date(offlineSince).toISOString()) + '.';
}
const api = {
  async req(url, opts) {
    let r;
    try { r = await fetch(url, { cache: 'no-store', ...opts }); } catch (e) { setOnline(false); throw e; }
    setOnline(true);
    if (!r.ok) {
      let msg = r.status >= 500 ? 'The server hit a problem. Try again in a moment.' : r.status === 404 ? 'That item no longer exists. Refresh and try again.' : 'The request did not go through. Try again.';
      let detail = null;
      try { detail = await r.json(); if (detail && typeof detail.error === 'string') msg = detail.error; } catch (e) { /* not json */ }
      const err = new Error(msg); err.status = r.status; err.detail = detail; throw err;
    }
    return r.json();
  },
  get(url) { return this.req(url); },
  post(url, body) { return this.req(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }); },
};

// One quiet message at the bottom of the screen. Errors carry an icon; Undo stays until it times out.
// Hovering or focusing a toast keeps it open so it can be read and acted on.
let toastT, toastOut;
const toastHold = { in: false };
function toast(msg, undo, opts = {}) {
  const el = $('#toast');
  const error = opts.error ?? /^(couldn't|could not|can't|cannot|not saved)/i.test(String(msg));
  clearTimeout(toastT); clearTimeout(toastOut);
  el.removeAttribute('data-leaving');
  toastHold.in = el.matches(':hover') || el.contains(document.activeElement);
  el.className = 'toast' + (error ? ' is-error' : '');
  el.innerHTML = `${error ? icon('alert', 16, 'toast-ic') : ''}<span class="toast-msg">${esc(msg)}</span>${undo ? '<button type="button" class="toast-act" id="undo">Undo</button>' : ''}<button type="button" class="toast-x" aria-label="Dismiss message">${icon('close', 14)}</button>`;
  el.hidden = false;
  el.querySelector('.toast-x').onclick = () => hideToast();
  if (undo) $('#undo').onclick = () => { hideToast(true); undo(); };
  const wait = undo ? 7000 : error ? 5500 : 3000;
  const arm = (ms) => { clearTimeout(toastT); toastT = setTimeout(() => (toastHold.in ? arm(1200) : hideToast()), ms); };
  arm(wait);
}
function hideToast(now) {
  const el = $('#toast');
  clearTimeout(toastT); clearTimeout(toastOut);
  if (now || el.hidden) { el.hidden = true; el.removeAttribute('data-leaving'); return; }
  el.dataset.leaving = '';
  toastOut = setTimeout(() => { el.hidden = true; el.removeAttribute('data-leaving'); }, 180);
}
$('#toast').addEventListener('mouseenter', () => { toastHold.in = true; });
$('#toast').addEventListener('mouseleave', () => { toastHold.in = false; });
$('#toast').addEventListener('focusin', () => { toastHold.in = true; });
$('#toast').addEventListener('focusout', () => { toastHold.in = false; });


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
const STATUSES = ['interested', 'contacted', 'talking', 'spoke_before', 'no'];
const SLABEL = { interested: 'Interested', contacted: 'Contacted', talking: 'Talking', spoke_before: 'Spoke before', client: 'Client', no: 'Not a fit' };
const SDESC = { interested: 'Worth contacting', contacted: 'You sent the first message', talking: 'They replied, a conversation is going',
  spoke_before: 'You talked before; no current conversation implied', client: 'A current or past client', no: 'Not for you: hidden from the list' };
const LEGACY_STATUS = { good: 'interested' };
const slabel = (s) => SLABEL[s] || ucf(s);
const CYCLE = [null, 'interested', 'contacted', 'talking', 'spoke_before'];
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
function fitDescription(p) {
  const v = p.verdict || {};
  if (v.model === 'rules' && (p.role || v.role) === 'unclear') return 'DTC fit unclear';
  return ({strong:'Strong DTC fit',good:'Possible DTC fit',weak:'Low DTC fit',unread:'Not reviewed'})[fitOf(p)];
}
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
  return facts.join('. ');
};
const RELATIONSHIP_LABELS = { follows: 'Follows you', followed: 'You follow', mutual: 'Follow each other' };
function observedRelationship(r) {
  const value = Object.hasOwn(r, 'owner_relationship') ? r.owner_relationship : r.relationship;
  return Object.hasOwn(RELATIONSHIP_LABELS, value) ? value : null;
}
function relationshipHTML(r) {
  const value = observedRelationship(r);
  if (!value) return '';
  const owner = r.relationship_owner || 'fortun8te';
  const evidence = (r.relationship_evidence || []).filter(e => e.seed?.toLowerCase() === owner.toLowerCase());
  const detail = evidence.map(e => `@${owner} ${e.direction === 'followers' ? 'followers' : 'following'} list${e.observed_at ? ' · observed ' + (/^\d{4}-\d{2}-\d{2}$/.test(e.observed_at) ? e.observed_at : new Date(e.observed_at).toLocaleString()) : ' · observation time unavailable'}`).join('; ');
  return `<span class="owner-follow" data-relationship="${value}" title="${esc(detail || 'Observed in collected lists for @' + owner + '. Observation time unavailable.')}" aria-label="${esc(RELATIONSHIP_LABELS[value] + '. ' + (detail || 'Observed in collected lists for @' + owner))}">${esc(RELATIONSHIP_LABELS[value])}</span>`;
}
const seedList = (seeds, n) => seeds.slice(0, n).map((s) => '@' + esc(s)).join(', ') + (seeds.length > n ? ` +${seeds.length - n}` : '');
// ---------- state ----------
const emptyFilter = () => ({ tags: [], any: [], not: [], status: '', relationship: '', tier: '', fit: '', q: '', min: 0, bio: '', seed: '', follow_up: '', fmin: null, fmax: null });
const S = {
  view: 'leads',
  f: emptyFilter(), sort: store.get('sort', 'fit'),
  tagList: [], tagBy: new Map(), tagLower: new Map(), counts: null, sc: null,
  rows: [], total: null, done: false, loading: false, error: false, gen: 0, nextOffset: 0, rev: null, stale: false,
  cur: -1, open: null, person: null,
  tagMore: {}, tagFind: '',
  side: false, fmore: store.get('fmore', false),
};

// ---------- filter <-> query string ----------
function toQuery(f = S.f, sort = S.sort, withSort = true) {
  const p = new URLSearchParams();
  if (f.tags.length) p.set('tags', f.tags.join(','));
  if (f.any.length) p.set('any', f.any.join(','));
  if (f.not.length) p.set('not', f.not.join(','));
  if (f.status) p.set('status', f.status);
  if (f.relationship) p.set('relationship', f.relationship);
  if (f.follow_up) p.set('follow_up', f.follow_up);
  if (f.tier) p.set('tier', f.tier);
  if (f.fit) p.set('fit', f.fit);
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
    f: { tags: list('tags'), any: list('any'), not: list('not'), status: p.get('status') || '', tier: FIT_TIER[TIER_FIT[p.get('tier')]] || '', fit: FITS.includes(p.get('fit')) ? p.get('fit') : '', q: p.get('q') || '', min: +p.get('min_lists') || 0,
      relationship: ['follows', 'followed', 'mutual'].includes(p.get('relationship')) ? p.get('relationship') : '',
      follow_up: ['due', 'overdue', 'scheduled', 'completed', 'none'].includes(p.get('follow_up')) ? p.get('follow_up') : '',
      bio: ['0', '1'].includes(p.get('has_bio')) ? p.get('has_bio') : '', seed: (p.get('seed') || '').replace(/^@/, ''), fmin: numOr('followers_min'), fmax: numOr('followers_max') },
    sort: p.get('sort') || 'fit',
  };
}
const filterCount = (f = S.f) => f.tags.length + f.any.length + f.not.length + !!f.status + !!f.relationship + !!f.follow_up + !!f.tier + !!f.fit + !!f.min + !!f.bio + !!f.seed + (f.fmin != null || f.fmax != null) + !!f.q;
const modeOf = (t) => S.f.tags.includes(t) ? 'inc' : S.f.any.includes(t) ? 'any' : S.f.not.includes(t) ? 'exc' : null;
function setMode(t, mode) {
  S.f.tags = S.f.tags.filter((x) => x !== t); S.f.any = S.f.any.filter((x) => x !== t); S.f.not = S.f.not.filter((x) => x !== t);
  if (mode === 'inc') S.f.tags.push(t); else if (mode === 'any') S.f.any.push(t); else if (mode === 'exc') S.f.not.push(t);
}
// A plain click toggles a required tag. Modifiers choose the other match modes.
function clickTag(t, e) {
  const m = modeOf(t);
  const next = e && e.altKey ? (m === 'exc' ? null : 'exc') : e && e.shiftKey ? (m === 'any' ? null : 'any') : m === 'inc' ? null : 'inc';
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
  if (S.f.relationship) out.push({ k: 'relationship', text: RELATIONSHIP_LABELS[S.f.relationship] });
  if (S.f.follow_up) out.push({ k: 'follow_up', text: 'followup:' + S.f.follow_up });
  if (S.f.status) out.push({ k: 'status', text: 'status:' + S.f.status });
  if (S.f.tier) out.push({ k: 'tier', text: 'priority:' + TIER_PRIORITY[S.f.tier] });
  if (S.f.fit) out.push({ k: 'fit', text: 'fit:' + S.f.fit });
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
  if ((m = w.match(/^relationship:(follows|followed|mutual)$/i))) return () => { S.f.relationship = m[1].toLowerCase(); };
  if ((m = w.match(/^followup:(due|overdue|scheduled|completed|none)$/i))) return () => { S.f.follow_up = m[1].toLowerCase(); };
  if ((m = w.match(/^status:(\w+)$/i))) {
    let s = m[1].toLowerCase(); if (s === 'unmarked') s = 'none'; s = LEGACY_STATUS[s] || s;
    if (s && s !== 'open' && s !== 'none' && s !== 'all' && !STATUSES.includes(s)) return null;
    return () => { S.f.status = s === 'open' ? '' : s; };
  }
  if ((m = w.match(/^priority:(\w+)$/i))) { const t = PRIORITY_TIER[m[1].toLowerCase()]; return t ? () => { S.f.tier = t; } : null; }
  if ((m = w.match(/^fit:(\w+)$/i))) { const fit = m[1].toLowerCase(); return FITS.includes(fit) ? () => { S.f.fit = fit; } : null; }
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
  return { view: v === 'start' ? 'accounts' : ['leads', 'map', 'qual', 'tags', 'scraper', 'accounts', 'settings'].includes(v) ? v : 'leads', qs: i < 0 ? '' : h.slice(i + 1) };
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
  if (work) $('#work-h').textContent = v === 'map' ? 'Connections' : 'Leads';
  syncTabs();
  if (v === 'map') M.show(); else if (prev === 'map') M.hide();
  if (v === 'leads' && prev !== 'leads') renderRows();
  // The detail panel belongs to Leads and Connections only; never carry it onto another page.
  if (prev !== v && S.open && (!work || narrow())) closeDetail();
  if (v === 'scraper') renderScraper();
  if (v === 'accounts') { renderAccounts(); window.AccountSetup?.show(); }
  if (v !== 'accounts' && A.wiz) closeWizard();
  if (v === 'settings') loadSettings();
  if (v === 'tags') T.show();
  if (v === 'qual') Q.show();
  if (prev !== v && SCRAPER_FULL_VIEWS.has(v) && !document.hidden) loadScraper();
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
}
function clearFilters() {
  S.f = emptyFilter(); $('#q').value = '';
  filtersChanged();
}

// ---------- theme / density / sidebar ----------
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  store.set('theme', t);
  syncLook();
  M.draw();
}
function applyDensity(d) {
  const sc = $('#scroll'), position = sc.scrollTop / rowH();
  document.documentElement.dataset.density = d;
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
const narrow = () => window.innerWidth <= 900;
let detailHidFilters = false;
function setFilterRail(visible) {
  S.side = visible;
  $('#view-work').classList.toggle('work-noside', !visible);
  syncFilterToggle();
}
function fitDetailLayout() {
  if (S.view === 'leads' && S.open && S.side && !narrow() && window.innerWidth <= 1480) {
    detailHidFilters = true;
    setFilterRail(false);
  }
}
function restoreDetailLayout() {
  if (detailHidFilters) setFilterRail(true);
  detailHidFilters = false;
}
function toggleSide() {
  if (narrow()) { setDrawer(!$('#filters').classList.contains('show')); return; }
  detailHidFilters = false;
  setFilterRail(!S.side);
  renderRows(); M.resize();
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
  $('#tag-dl').innerHTML = [...new Set(['Exceptional fit', ...S.tagList.filter((t) => t.grp !== 'source').sort((a, b) => b.total - a.total).map((t) => t.tag)])].map((tag) => `<option value="${esc(tag)}">`).join('');
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
// ---------- sidebar ----------
const swatch = (kind, extra = '', grp = '') => `<i class="sw ${KIND[kind] ?? ''} ${extra}${grp ? ' g-' + esc(grp) : ''}"></i>`;
function tagItem(t, label) {
  const m = modeOf(t.tag);
  const n = t.count;
  const title = `${t.tag} · ${t.kind}${t.sources.length > 1 ? ' + ' + t.sources.filter((s) => s !== t.kind).join(', ') : ''}`;
  return `<button class="fi t-${tagTier(t)}${m ? ' ' + m : ''}${!m && !n ? ' zero' : ''}" data-tag="${esc(t.tag)}" title="${esc(title)}" aria-pressed="${!!m}"><span class="tag" ${tagStyleAttrs(t)}>${tagContent(t, label, n)}</span></button>`;
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
  const focused = document.activeElement;
  const focus = focused?.id === 'f-find'
    ? { id: focused.id, start: focused.selectionStart, end: focused.selectionEnd, direction: focused.selectionDirection } : null;
  const c = S.counts || {};
  const qs = toQuery().toString();
  const active = filterCount();
  $('#fbtn-n').textContent = active ? ' ' + active : '';
  let h = `<div class="fsec"><h4>Filters<span class="grow"></span>${active ? '<button id="f-reset" title="Clear filters (c)">Clear</button>' : ''}</h4>
    <button class="fi${qs === '' ? ' on' : ''}" data-view="" aria-pressed="${qs === ''}"><span>Open leads</span></button>
    <button class="fi${LeadDaily.isDueView(S.f, S.sort) ? ' on' : ''}" data-view="${esc(LeadDaily.dueQuery())}" aria-pressed="${LeadDaily.isDueView(S.f, S.sort)}"><span>Due follow-ups</span></button></div>
    <div class="fsec"><h4>Relationship</h4>
    <button class="fi${!S.f.status ? ' on' : ''}" data-status=""><span>Open</span><b>${fmt(c.open)}</b></button>
    ${STATUSES.map((s) => `<button class="fi${S.f.status === s ? ' on' : ''}${c[s] ? '' : ' zero'}" data-status="${s}" title="${esc(SDESC[s])}"><span>${slabel(s)}</span><b>${c[s] ? fmt(c[s]) : ''}</b></button>`).join('')}
    <button class="fi${S.f.status === 'none' ? ' on' : ''}" data-status="none" title="No status yet"><span>Unmarked</span><b>${fmt(c.none)}</b></button>
    <button class="fi${S.f.status === 'all' ? ' on' : ''}" data-status="all" title="Everyone, including Not a fit"><span>All</span><b>${c.open != null ? fmt(c.open + (c.no || 0)) : ''}</b></button></div>
    <div class="fsec"><h4>Follow-up</h4>${['due', 'overdue', 'scheduled', 'completed', 'none'].map((v) => `<button class="fi${S.f.follow_up === v ? ' on' : ''}" data-follow-up="${v}"><span>${v === 'due' ? 'Due today or earlier' : ucf(v)}</span></button>`).join('')}</div>
    <div class="fsec"><h4>Priority tier</h4>
    ${FITS.map((f) => `<button class="fi${S.f.tier === FIT_TIER[f] ? ' on' : ''}" data-tier="${FIT_TIER[f]}"><i class="fdot f-${f}"></i><span>${PRIORITY_LABEL[f]}</span><b>${c[FIT_TIER[f]] != null ? fmt(c[FIT_TIER[f]]) : ''}</b></button>`).join('')}</div>
    <div class="fsec"><h4>Business fit</h4>
    ${FITS.map((f) => `<button class="fi${S.f.fit === f ? ' on' : ''}" data-fit="${f}"><i class="fdot f-${f}"></i><span>${FIT_LABEL[f]}</span></button>`).join('')}</div>
    <div class="fsec fsegs f-x"><h4>Shape</h4>
      <div class="fseg"><span>Lists</span><div class="seg">${LIST_OPTS.map(([n, l]) => `<button data-min="${n}" class="${S.f.min === n ? 'on' : ''}">${l}</button>`).join('')}</div></div>
      <div class="fseg"><span>Bio</span><div class="seg">${BIO_OPTS.map(([v, l]) => `<button data-bio="${v}" class="${S.f.bio === v ? 'on' : ''}">${l}</button>`).join('')}</div></div>
      <div class="fseg"><span>Followers</span><div class="seg">${FOL_OPTS.map(([k, l, a, b]) => `<button data-fol="${k}" class="${S.f.fmin === a && S.f.fmax === b ? 'on' : ''}">${l}</button>`).join('')}</div></div>
    </div>
    <div class="fsec f-x"><input class="input ffind" id="f-find" type="search" placeholder="Find tag" value="${esc(S.tagFind)}" autocomplete="off" spellcheck="false"><p class="filter-mode-hint">Click: require · Shift: any · Alt: exclude</p></div>`;
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
  const open = S.fmore || !!S.tagFind;
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
  if (t.closest('#f-more')) { S.fmore = !(S.fmore || S.tagFind); S.tagFind = ''; store.set('fmore', S.fmore); return renderFilters(); }
  const b = t.closest('button');
  if (!b) return;
  const d = b.dataset;
  if (d.tag != null) { e.preventDefault(); return clickTag(d.tag, e); }
  if (d.view != null) { applyQuery(d.view); if (narrow()) setDrawer(false); return; }
  // Segments toggle: clicking the active option turns it off.
  if (d.status != null) S.f.status = d.status;
  else if (d.followUp != null) S.f.follow_up = S.f.follow_up === d.followUp ? '' : d.followUp;
  else if (d.tier != null) S.f.tier = S.f.tier === d.tier ? '' : d.tier;
  else if (d.fit != null) S.f.fit = S.f.fit === d.fit ? '' : d.fit;
  else if (d.min != null) S.f.min = S.f.min === +d.min ? 0 : +d.min;
  else if (d.bio != null) S.f.bio = S.f.bio === d.bio ? '' : d.bio;
  else if (d.fol != null) { const o = FOL_OPTS.find((x) => x[0] === d.fol); const same = S.f.fmin === o[2] && S.f.fmax === o[3]; S.f.fmin = same ? null : o[2]; S.f.fmax = same ? null : o[3]; }
  else return;
  filtersChanged();
});
$('#filters').addEventListener('input', (e) => {
  if (e.target.id === 'f-find') { S.tagFind = e.target.value; renderFilters(); }
});
$('#filters').addEventListener('keydown', (e) => {
  if (e.target.id === 'f-find' && e.key === 'Enter') {
    const first = $('#filters [data-tag]');
    if (first) clickTag(first.dataset.tag, e);
  }
});
function applyQuery(qs) {
  const p = fromQuery(qs);
  S.f = p.f; S.sort = p.sort; $('#sort').value = S.sort; $('#q').value = S.f.q;
  filtersChanged();
}
function setDrawer(open) { $('#filters').classList.toggle('show', open); $('#scrim').hidden = !open; syncFilterToggle(); }
$('#filters-btn').onclick = (e) => { e.stopPropagation(); toggleSide(); };
$('#scrim').onclick = () => setDrawer(false);

// Both views share the same observed-follow filter and URL state.
for (const container of ['.qbar', '.map-toolbar']) {
  const host = $(container);
  if (!host) continue;
  const select = document.createElement('select');
  select.className = 'select relationship-filter'; select.dataset.relationshipFilter = '';
  select.setAttribute('aria-label', 'Observed follows with your account');
  select.innerHTML = '<option value="">All connections</option>' + Object.entries(RELATIONSHIP_LABELS).map(([value,label]) => `<option value="${value}">${label}</option>`).join('');
  select.addEventListener('change', () => {
    S.f.relationship = select.value;
    if (select.value && !S.f.status) S.f.status = 'all';
    filtersChanged();
  });
  host.append(select);
}

// ---------- query bar ----------
function renderTokens() {
  const clear = $('#clear-filters');
  if (clear) { clear.hidden = !filterCount(); clear.onclick = clearFilters; }
  $$('[data-relationship-filter]').forEach(el => { el.value = S.f.relationship || ''; });
  $('#tokens').innerHTML = tokens().map((t, i) => `<span class="tok ${t.k === 'tag' ? t.mode : 'prm'}"><button class="tok-label" type="button" data-t="${i}" aria-label="${esc(t.k === 'tag' ? `${t.text}: ${t.mode === 'exc' ? 'excluded' : t.mode === 'any' ? 'match any' : 'required'}. Change filter mode` : `Edit ${t.text} filter`)}">${esc(t.text)}</button><button class="x" type="button" data-rm="${i}" title="Remove" aria-label="Remove ${esc(t.text)} filter">&times;</button></span>`).join('');
  if (document.activeElement !== $('#q') && $('#q').value.trim() !== S.f.q) $('#q').value = S.f.q;
}
function removeToken(t) {
  if (t.k === 'tag') setMode(t.tag, null);
  else if (t.k === 'relationship') S.f.relationship = '';
  else if (t.k === 'follow_up') S.f.follow_up = '';
  else if (t.k === 'status') S.f.status = '';
  else if (t.k === 'tier') S.f.tier = '';
  else if (t.k === 'fit') S.f.fit = '';
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
    if (t.k === 'tag') clickTag(t.tag, e); else if (t.k === 'relationship') $('[data-relationship-filter]')?.focus(); else editToken(t);
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
      .map((f) => ({ text: (legacy ? 'fit:' : 'priority:') + f, count: legacy ? undefined : S.counts?.[PRIORITY_TIER[f]] }));
  }
  else if (/^lists:\d*$/i.test(w)) items = ['2+', '3+', '4+', '5+'].map((s) => ({ text: 'lists:' + s }));
  else if (/^bio:\w*$/i.test(w)) items = ['yes', 'no'].map((s) => ({ text: 'bio:' + s }));
  else if (/^followers:\S*$/i.test(w)) items = ['<1k', '1k+', '10k+', '100k+', '1k-10k', '10k-100k'].map((s) => ({ text: 'followers:' + s }));
  else if (w.length >= 2 && /^[a-z]+:?$/i.test(w)) items = ['status:', 'priority:', 'fit:', 'lists:', 'bio:', 'seed:', 'followers:', 'via:@'].filter((k) => k.startsWith(w.toLowerCase())).map((k) => ({ text: k, key: true }));
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
      const currentId = S.rows[S.cur]?.id;
      S.rows = [...new Map(d.rows.map((r) => [r.id, r])).values()];
      S.cur = currentId == null ? -1 : S.rows.findIndex((r) => r.id === currentId);
    } else {
      const seen = new Set(S.rows.map((r) => r.id));
      S.rows.push(...d.rows.filter((r) => { if (seen.has(r.id)) return false; seen.add(r.id); return true; }));
    }
    S.error = false;
    // Start on the best lead so the keyboard works at once (desktop only; touch has no cursor).
    if (S.cur < 0 && S.rows.length && S.view === 'leads' && !narrow()) S.cur = 0;
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
  // An explicitly marked client is an established relationship, not a fit verdict.
  if ((t.source === 'manual' || t.kind === 'manual') && /^(client|customer)$/i.test(name)) return 'client';
  // Other personal labels stay neutral; adding one is not an automatic fit verdict.
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
const TIER_ORDER = { hero: 0, decision: 1, flag: 2, maybe: 3, role: 4, plus: 5, market: 6, partner: 7, client: 8, own: 8, niche: 9, review: 10, ctx: 11, '': 11, min: 12 };
// One presentation contract for tags, filter choices, rules and the tag directory.
// The source explains the evidence; it does not change a tag's visual meaning.
const EXCEPTIONAL_TAGS = new Set(['exceptional fit', 'exceptional opportunity', 'design partner']);
const TAG_LABELS = { 'Fit: strong': 'Strong fit', 'Fit: good': 'Good fit', 'Fit: weak': 'Weak fit',
  'Scout: Strong': 'Strong fit', 'Scout: Possible': 'Good fit', 'Scout: No': 'Not a fit',
  'Shop Link': 'Shop link', 'Link Hub': 'Link hub', '<1k': 'Under 1k',
  '1k-10k': '1k–10k', '10k-100k': '10k–100k', '100k-1M': '100k–1M' };
const REVIEW_TAGS = new Set(['check fit', 'missing contact', 'missing context', 'not reachable']);
const PRODUCT_TAGS = new Set(['skincare', 'beauty', 'supplements', 'apparel', 'jewelry', 'home', 'pets', 'coffee', 'food & drink', 'fitness', 'wellness', 'baby', 'accessories', 'outdoor', 'tech gadgets']);
const BUSINESS_TAGS = new Set(['founder', 'decision maker', 'runs ads', 'shopify', 'shop link', 'brand', 'store', 'dtc', 'us market']);
const QUIET_TAGS = new Set(['verified', 'business', 'email', 'link hub', 'us', 'us market', 'uk', 'nl', 'ecom', 'scaling', 'hiring']);
const tagKey = t => tagLabel(tagName(t)).toLowerCase().replace(/^scout: /, '');
const isExceptionalTag = t => (t.source === 'manual' || t.kind === 'manual') && EXCEPTIONAL_TAGS.has(tagKey(t));
const isProspectTag = t => isExceptionalTag(t) || ['top fit', 'strong fit', 'good fit'].includes(tagKey(t));
function tagImportance(t) {
  const name = tagName(t), key = tagKey(t);
  if (isExceptionalTag(t)) return 'exceptional';
  if (key === 'top fit' || key === 'open to collaborate') return 'priority';
  if (['strong fit', 'client', 'customer', 'returning client', 'warm introduction', 'warm intro'].includes(key)) return 'strong';
  if (key === 'good fit') return 'accent';
  if (isViaTag(name)) return 'background';
  if (['source', 'size'].includes(t.grp) || QUIET_TAGS.has(key) || FLAG_TAGS.has(name) || SOFT_TAGS.has(name)) return 'quiet';
  if (t.grp === 'scout' && !BUSINESS_TAGS.has(key) && !REVIEW_TAGS.has(key)) return 'background';
  return 'standard';
}
function tagTone(t) {
  const key = tagKey(t);
  if (['founder', 'decision maker', 'runs ads'].includes(key)) return 'positive';
  if (REVIEW_TAGS.has(key)) return 'review';
  return '';
}
function tagStyleAttrs(t) { return `data-importance="${tagImportance(t)}" data-tone="${tagTone(t)}"`; }
function tagIcon(t) {
  const level = tagImportance(t);
  const icon = level === 'exceptional' ? 'sparkles' : ['priority', 'strong'].includes(level) ? 'verified' : '';
  return icon ? `<span class="tag-icon" data-icon="${icon}" aria-hidden="true"></span>` : '';
}
function tagContent(t, label, count) {
  return `${tagIcon(t)}<span>${esc(label || tagLabel(t.tag))}</span>${count == null ? '' : `<b class="num">${fmt(count)}</b>`}`;
}
function tagChip(t, rm) {
  const k = KIND[t.source] ?? '';
  const m = modeOf(t.tag);
  const label = tagLabel(t.tag);
  const tier = tagTier(t);
  return `<button class="tag ${k} g-${esc(t.grp || 'custom')}${t.grp === 'source' ? ' src' : ''}${tier ? ' t-' + tier : ''}${m ? ' is-filtered' : ''}" ${tagStyleAttrs(t)} data-filter-mode="${m || ''}" data-tag="${esc(t.tag)}" aria-pressed="${!!m}" title="${esc(t.tag)} · ${esc(t.source)}${m ? ' · filter ' + m : ''}">${tagContent(t, label)}${rm ? `<i class="x" data-rmtag="${esc(t.tag)}" title="Remove">&times;</i>` : ''}</button>`;
}
// The source stays in the tooltip and filter value; the visible chip says what
// the label means. Repeating "AI:" on every chip hides the actual distinction.
function tagLabel(name) {
  return isViaTag(name) ? name.slice(4) : TAG_LABELS[name] || name.replace(/^AI: /, '');
}
const ORDER = { manual: 0, rule: 1, auto: 2 };
const GORDER = { ai: -1, role: 0, niche: 1, signal: 2, custom: 3, size: 5, source: 6 };
const ROW_RELATIONSHIP = new Set(['knows you', 'follows you', 'mentions you']);
const ROW_WEAK_CONNECTION = new Set(['you follow', 'Instagram link']);
const ROW_GENERIC = new Set(['Verified', 'Business']);
const ROW_COMMERCE = new Set(['AI: Runs ads', 'AI: Ad tracking detected', 'AI: Has online shop', 'Shopify', 'Shop Link', 'DTC']);
// Show different kinds of evidence in a short row. A founder/decision-maker
// pair, for example, takes one slot; the full set remains on the lead detail.
function rowTagFacet(t) {
  if (t.source === 'manual' && /^(client|customer)$/i.test(t.tag)) return 'client';
  if (isProspectTag(t)) return 'verdict';
  if (ROW_RELATIONSHIP.has(t.tag)) return 'relationship';
  if (ROW_WEAK_CONNECTION.has(t.tag)) return 'access';
  if (FLAG_TAGS.has(t.tag)) return 'caution';
  if (HERO_TAGS.has(t.tag) || MAYBE_TAGS.has(t.tag)) return 'verdict';
  if (DECISION_TAGS.has(t.tag)) return 'decision';
  if (ROLE_TAGS.has(t.tag) || t.grp === 'role' && !PARTNER_TAGS.has(t.tag)) return 'role';
  if (ROW_COMMERCE.has(t.tag)) return 'commerce';
  if (PARTNER_TAGS.has(t.tag)) return 'partner';
  if (t.source === 'manual') return 'manual:' + t.tag.toLowerCase();
  if (MARKET_TAGS.has(t.tag)) return 'market';
  if (/^AI: (Pre-launch|Early stage|Growing|Established)$/.test(t.tag)) return 'stage';
  if (t.grp === 'niche' || tagTier(t) === 'niche') return 'niche';
  return 'other:' + (t.grp || 'custom');
}
const ROW_FACET_ORDER = { client: 0, manual: 1, relationship: 2, caution: 3, verdict: 4, decision: 5, role: 6, commerce: 7, partner: 8, market: 9, stage: 10, niche: 11, other: 12, access: 13 };
const rowFacetRank = (t) => isExceptionalTag(t) ? -3 : isProspectTag(t) ? -2 : t.source === 'manual' && rowTagFacet(t) !== 'client' ? 1 : ROW_FACET_ORDER[rowTagFacet(t).split(':')[0]] ?? 10;
// Keep the prospect judgement visible alongside the most useful profile facts.
function rowTags(r) {
  return (r.tags || []).filter(t => !(Object.hasOwn(r, 'owner_relationship') && t.source !== 'manual' && ['follows you','you follow'].includes(t.tag))).filter((t) => (t.source === 'manual' || t.grp !== 'source' && t.grp !== 'size' || ROW_RELATIONSHIP.has(t.tag) || ROW_WEAK_CONNECTION.has(t.tag)))
    .sort((a, b) => rowFacetRank(a) - rowFacetRank(b) || ORDER[a.source] - ORDER[b.source] || TIER_ORDER[tagTier(a)] - TIER_ORDER[tagTier(b)] || Number(a.tag.startsWith('AI: ')) - Number(b.tag.startsWith('AI: ')) || a.tag.localeCompare(b.tag));
}
const chipIdentity = (t) => tagLabel(t.tag).trim().toLocaleLowerCase();
function rowTagSelection(r, limit = 3) {
  const tags = rowTags(r), shown = [], hidden = [], suppressed = [], facets = new Set(), labels = new Set();
  const informative = tags.some((t) => t.tag !== 'AI: Top fit' && !ROW_WEAK_CONNECTION.has(t.tag) && !ROW_GENERIC.has(t.tag));
  for (const t of tags) {
    const facet = rowTagFacet(t);
    const redundant = (informative && (ROW_WEAK_CONNECTION.has(t.tag) || ROW_GENERIC.has(t.tag))) ||
      (facet === 'verdict' && facets.has('verdict'));
    const equivalent = labels.has(chipIdentity(t)) || facet === 'decision' && facets.has('decision');
    if (redundant || equivalent) { suppressed.push(t); continue; }
    if (facets.has(facet)) { hidden.push(t); labels.add(chipIdentity(t)); continue; }
    facets.add(facet); labels.add(chipIdentity(t));
    if (shown.length < limit) shown.push(t);
    else hidden.push(t);
  }
  return { shown, hidden, suppressed };
}
// Keep the full tag vocabulary available in details, but place repeated
// verdicts and duplicate labels behind a small disclosure. Manual tags stay
// visible and removable even when an automatic tag has the same label.
function detailTagSelection(p) {
  const tags = (p.tags || []).filter(t => !(Object.hasOwn(p, 'owner_relationship') && t.source !== 'manual' && ['follows you','you follow'].includes(t.tag))).filter((t) => !(t.tag === 'knows you' && t.source !== 'manual') &&
    (t.grp !== 'source' || t.source === 'manual' || t.tag === 'Instagram link' || t.tag === 'mentions you'))
    .sort((a, b) => Number(b.source === 'manual') - Number(a.source === 'manual') || rowFacetRank(a) - rowFacetRank(b) ||
      ORDER[a.source] - ORDER[b.source] || a.tag.localeCompare(b.tag));
  const primary = [], related = [], labels = new Set();
  const hasFounder = tags.some((t) => t.tag === 'Founder');
  for (const t of tags) {
    const label = chipIdentity(t);
    const redundant = t.source !== 'manual' && (ROW_GENERIC.has(t.tag) || t.tag === 'Instagram link' || t.tag === 'AI: Decision maker' && hasFounder ||
      isProspectTag(t) && primary.some(isProspectTag));
    if (redundant || labels.has(label)) related.push(t);
    else { primary.push(t); labels.add(label); }
  }
  return { primary, related };
}
// One plain sentence on why this person fits: the review's own words, else the first line of their bio.
function whyHTML(r) {
  if (r.reason) return esc(r.reason);
  const sig = rowTags(r).filter((t) => t.grp === 'signal').slice(0, 3).map((t) => tagLabel(t.tag));
  const bio = String(r.bio || '').split(/\n/)[0].trim();
  return sig.length ? esc(sig.join(', ')) : bio ? esc(bio) : '<span class="none">Not reviewed yet</span>';
}
const statHTML = (s) => STATUSES.includes(s) ? `<span class="stat ${s}"><i></i>${slabel(s)}</span>` : '';
// Fit is a ring, not a pill: the arc is the score out of 100, the number sits inside.
const rowFitHTML = (r, size = 28) => {
  const has = r.business_fit != null && Number.isFinite(+r.business_fit);
  return `<span class="row-fit f-${fitOf(r)}${has && +r.business_fit >= 100 ? ' wide' : ''}" role="img" aria-label="${has ? `Fit ${esc(r.business_fit)} out of 100` : 'Fit not scored yet'}">${ring(has ? r.business_fit : null, size)}<b class="num">${has ? esc(Math.round(+r.business_fit)) : ''}</b></span>`;
};
// One-click "open on Instagram": a plain link, so the row / map click underneath never fires.
const igLink = (h) => `<a class="ig" data-ig href="https://www.instagram.com/${encodeURIComponent(h)}/" target="_blank" rel="noopener" title="Open on Instagram (o)" aria-label="Open @${esc(h)} on Instagram">${icon('external', 13)}</a>`;
const noteIcon = (note) => note ? `<span class="note-ic" aria-label="Has a note">${icon('note', 13)}</span>` : '';
// Follow-ups read as plain dates. Only an open one shows in the row.
function followUpChip(f) {
  if (!f || f.completed_at) return '';
  const today = LeadWorkflow.localToday(), late = f.due_on < today;
  const text = late ? 'Overdue' : f.due_on === today ? 'Due today' : 'Follow up ' + shortDate(f.due_on);
  return `<span class="followup-chip${late || f.due_on === today ? ' due' : ''}"${f.note ? ` title="${esc(f.note)}"` : ''}>${esc(text)}</span>`;
}
// Keep the saved classification and owner tags visible, with remaining evidence available on hover.
const ROW_CHIPS = 2;
function rowChips(r, limit = ROW_CHIPS) {
  const tags = rowTagSelection(r, limit);
  return tags.shown.map((t) => tagChip(t)).join('') + (tags.hidden.length ? `<span class="more" title="${esc(tags.hidden.map((t) => t.tag).join(' · '))}">+${tags.hidden.length}</span>` : '');
}
function leadConnectionLines(r) {
  const edges = r.connection_edges || r.edges;
  const owner = String(r.relationship_owner || 'fortun8te').toLowerCase();
  const bySeed = new Map();
  for (const edge of edges || []) {
    if (!edge?.seed || !['followers', 'following'].includes(edge.direction)) continue;
    const key = edge.seed.toLowerCase();
    if (!bySeed.has(key)) bySeed.set(key, { seed: edge.seed, dirs: new Set() });
    bySeed.get(key).dirs.add(edge.direction);
  }
  const lines = [...bySeed.values()].sort((a, b) => Number(b.seed.toLowerCase() === owner) - Number(a.seed.toLowerCase() === owner) || a.seed.localeCompare(b.seed)).map(({ seed, dirs }) => {
    const isYou = seed.toLowerCase() === owner;
    if (dirs.size === 2) return isYou ? 'You follow each other' : 'They and @' + seed + ' follow each other';
    if (dirs.has('followers')) return isYou ? 'Follows you' : 'Follows @' + seed;
    return isYou ? 'You follow them' : '@' + seed + ' follows them';
  });
  if (!lines.length && !Array.isArray(edges)) {
    lines.push(...[...new Set(r.via || [])].map(seed => 'Seen in @' + seed + '’s list'));
  }
  return lines;
}
function rowConnectionHTML(r) {
  const lines = leadConnectionLines(r);
  if (!lines.length) return '';
  const text = lines[0] + (lines.length > 1 ? ' +' + (lines.length - 1) : '');
  return `<span class="row-connection" title="${esc('Saved follows: ' + lines.join('; '))}">${esc(text)}</span><span class="row-connection-separator" aria-hidden="true"> · </span>`;
}
function rowHTML(r, i, h) {
  const cls = ['row', i === S.cur ? 'cur' : '', S.open === r.id ? 'open' : '', r.status === 'no' ? 'st-no' : ''].join(' ');
  const named = r.name && r.name !== r.handle;
  return `<div class="${cls}" data-i="${i}" data-person-id="${r.id}" style="top:${i * h}px">
    <div class="c-sel">${avatar(r.pic, r.name || r.handle)}</div>
    <div class="who"><div class="l1"><button class="lead-open" aria-label="Open @${esc(r.handle)}"><b>${esc(named ? r.name : '@' + r.handle)}</b>${named ? `<span class="handle">@${esc(r.handle)}</span>` : ''}</button>${igLink(r.handle)}${noteIcon(r.note)}${followUpChip(r.follow_up)}</div><div class="why">${rowConnectionHTML(r)}<span>${whyHTML(r)}</span></div>
      <div class="row-meta"><span class="c-st-m">${statHTML(r.status)}</span><span class="num muted">${fmt(r.followers)} followers</span></div></div>
    <div class="tags c-tags">${rowChips(r, S.rowTagLimit)}</div>
    <span class="num r fol c-fol">${fmt(r.followers)}</span>
    <div class="c-fit">${rowFitHTML(r)}</div>
    <span class="c-st">${statHTML(r.status)}</span>
  </div>`;
}
function renderRows() {
  S.rowTagLimit = $('#pane-leads').clientWidth <= 860 ? 1 : ROW_CHIPS;
  const active = document.activeElement;
  const activeRow = active?.closest('#rows .row');
  const focusId = activeRow?.dataset.personId;
  const focusKey = active?.matches('.lead-open') ? '.lead-open' : active?.matches('[data-ig]') ? '[data-ig]' : active?.hasAttribute('data-tag') ? `[data-tag="${CSS.escape(active.dataset.tag)}"]` : null;
  const box = $('#rows'), sc = $('#scroll'), h = rowH();
  if ($('#work-count')) $('#work-count').textContent = S.total == null ? '' : int(S.total);
  if (!S.rows.length) {
    box.style.height = '100%';
    if (S.loading || (S.total == null && !S.error)) {
      // Same grid as a real row, so nothing moves when the data arrives.
      box.innerHTML = `<span class="sr-only" role="status">Loading leads</span>` + Array.from({ length: Math.max(8, Math.ceil(sc.clientHeight / h) + 1) }, (_, i) => `<div class="row skel" aria-hidden="true" style="top:${i * h}px"><div class="c-sel"><span class="av"></span></div><div class="who"><i style="width:${34 + (i * 7) % 18}%"></i><i style="width:${52 + (i * 11) % 30}%"></i></div><div class="tags c-tags"><i class="chip"></i><i class="chip"></i></div><span class="c-fol"><i></i></span><div class="c-fit"><i class="rg"></i></div><span class="c-st"><i></i></span></div>`).join('');
    } else if (S.error) {
      box.innerHTML = `<div class="empty"><b>${offlineSince ? 'Server offline' : 'Could not load leads'}</b><p>Check your connection, then try again.</p><button class="btn" id="retry">Retry</button></div>`;
    } else {
      const filtered = filterCount();
      const st = S.f.status && STATUSES.includes(S.f.status) ? S.f.status : '';
      const only = filtered === 1 && S.f.status;
      const head = st ? `No leads marked ${esc(slabel(st))} yet` : S.f.status === 'none' && only ? 'Every lead has a status' : filtered ? 'No matches' : 'No leads yet';
      const body = st && only ? 'Open all leads, pick one and press <kbd>s</kbd> to set a status.' : S.f.status === 'none' && only ? 'You have gone through everyone. New leads will appear here.' : filtered ? 'Try a different search or clear your filters.' : 'Add an Instagram account to start finding people.';
      box.innerHTML = `<div class="empty"><b>${head}</b><p>${body}</p>${filtered ? `<button class="btn" id="clear-all">${only ? 'Show all leads' : 'Clear filters'} <kbd>c</kbd></button>` : '<a class="btn" href="#/accounts">Add an account</a>'}</div>`;
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
  select(i);
  openDetail(S.rows[i].id, { keyboard: e.detail === 0 });
});
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
let mutationQueue = Promise.resolve();
function serializeMutation(fn) {
  const run = mutationQueue.then(fn, fn);
  mutationQueue = run.catch(() => {});
  return run;
}
// ---------- marking ----------
function mark(id, status, o = {}) { return serializeMutation(() => markNow(id, status, o)); }
async function markNow(id, status, { quiet = false } = {}) {
  const r = S.rows.find((x) => x.id === id) || (S.person?.id === id ? S.person : null);
  const prev = r ? r.status : null;
  invalidatePersonRead(id);
  patchRow(id, { status });
  // Optimistic: say so at once; a failed save replaces this with an error.
  if (!quiet && prev !== status) toast(status ? `@${r?.handle || 'lead'} marked ${slabel(status)}` : `@${r?.handle || 'lead'} status cleared`, () => mark(id, prev ?? null, { quiet: true }));
  try {
    await api.post(`/api/person/${id}/mark`, { status });
    loadCounts(); loadFacetsSoon();
    // Owner status changes the server-derived tags, reason and research too.
    // Await the readback inside this mutation so rapid status changes stay ordered.
    if (S.open === id) await refreshPerson(id);
    else refreshActivity(id);
  }
  catch (e) { patchRow(id, { status: prev }); toast("Couldn't save. Try again."); }
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
  S.open = id;
  fitDetailLayout();
  const base = S.rows.find((r) => r.id === id);
  const node = M.byId.get('p:' + id);
  S.person = base ? { ...base, loading: true } : node ? { id, handle: node.handle, name: node.name, followers: node.followers, lists: node.lists, status: node.status, tags: [], loading: true } : { id, loading: true, handle: '', tags: [] };
  $('#detail').hidden = false;
  renderDetail(); renderRows(); M.draw();
  detailAccess.sync();
  await refreshPerson(id);
}
const personReads = new Map();
const notePolls = new Map();
function invalidatePersonRead(id) { const version = (personReads.get(id) || 0) + 1; personReads.set(id, version); return version; }
async function refreshPerson(id) {
  const version = invalidatePersonRead(id);
  try {
    const p = await api.get('/api/person/' + id);
    if (S.open !== id || personReads.get(id) !== version) return;
    noteQueue.reconcile(id, p.note || '', p.mark_rev ?? '');
    S.person = p;
    const poll = notePolls.get(id) || { note: p.note, markRev: p.mark_rev, attempts: 0, timer: null };
    clearTimeout(poll.timer);
    if (poll.note !== p.note || poll.markRev !== p.mark_rev) { poll.note = p.note; poll.markRev = p.mark_rev; poll.attempts = 0; }
    if ((p.ranking_pending || p.note_interpretation?.state === 'pending') && poll.attempts < 20) {
      poll.attempts++;
      poll.timer = setTimeout(() => { if (S.open === id && !document.hidden) refreshPerson(id); }, 3000);
    }
    notePolls.set(id, poll);
    const r = S.rows.find((x) => x.id === id);
    if (r) Object.assign(r, { tags: p.tags, status: p.status, tier: p.tier, score: p.score, business_fit: p.business_fit,
      relationships: p.relationships, familiarity: p.familiarity, connection_strength: p.connection_strength, reason: p.reason, note: p.note, mark_rev: p.mark_rev,
      owner_relationship: p.owner_relationship, relationship_owner: p.relationship_owner, relationship_evidence: p.relationship_evidence, owner_status: p.owner_status, owner_conflict: p.owner_conflict, reachable: p.reachable, manual_tags: p.manual_tags });
  } catch (e) {
    if (S.open !== id || !S.person || personReads.get(id) !== version) return;
    S.person.loading = false; S.person.failed = true;
  }
  renderDetail(); renderRows();
}
function closeDetail() {
  if (S.open) noteQueue.flush(S.open).catch(() => {});
  S.open = null; S.person = null;
  $('#detail').hidden = true;
  restoreDetailLayout();
  M.focus = null;
  renderRows(); M.resize();
  detailAccess.close();
}
function edgeDay(value) { const date = String(value || '').slice(0, 10); return /^\d{4}-\d{2}-\d{2}$/.test(date) ? date : ''; }
// Dates read as "Sep 16", with the year only when it is not this year. The ISO day stays in the datetime attribute.
function shortDate(day) {
  const d = new Date(String(day).slice(0, 10) + 'T00:00:00');
  if (Number.isNaN(+d)) return String(day);
  return d.toLocaleDateString('en-US', d.getFullYear() === new Date().getFullYear() ? { month: 'short', day: 'numeric' } : { month: 'short', day: 'numeric', year: 'numeric' });
}
function connectionEvidenceHTML(current, historical) {
  const direction = (e) => e.direction === 'followers' ? `They followed @${e.seed}` : e.direction === 'following' ? `@${e.seed} followed them` : 'Seen in a list';
  const seen = (value, label) => { const day = edgeDay(value); return day ? `<time datetime="${esc(day)}">${label} ${esc(shortDate(day))}</time>` : `${label} date unavailable`; };
  const active = current.length ? current.map((e) => `<button data-seed="${esc(e.seed)}" title="Filter by @${esc(e.seed)}"><b>@${esc(e.seed)}</b><span>${esc(direction(e))}. ${seen(e.observed_at, 'Seen')}</span></button>`).join('') : '<span class="muted">No recent list evidence</span>';
  const earlier = historical.length ? `<h4 class="d-history-heading">Earlier observations</h4><div class="edges d-history-edges">${historical.map((e) => {
    const timing = e.state === 'absent' ? `${seen(e.checked_at, 'Not found when checked')}${edgeDay(e.first_seen) ? `. ${seen(e.first_seen, 'First seen')}` : ''}` : `${seen(e.first_seen || e.observed_at, 'Previously seen')}, not reverified`;
    return `<div class="d-history-row"><b>@${esc(e.seed)}</b><span>${esc(direction(e))}. ${timing}</span></div>`;
  }).join('')}</div>` : '';
  return `<div class="edges">${active}</div>${earlier}`;
}
const modelLabel = (m) => (!m ? '' : m === 'rules' ? 'Rule-based' : String(m).split('/').pop().replace(/:free$/, ''));
// The Hermes leadscout's final read: verdict, two sentences and the pages it used.
function scoutHTML(sc) {
  if (!sc) return '';
  const verified = sc.verified !== false;
  const historical = !!sc.overridden_by_owner;
  const label = historical ? 'Earlier research' : sc.stale ? 'Older read · unverified' : !verified ? 'Unverified candidate' : ({ strong: 'Strong lead', possible: 'Possible lead', no: 'Not a lead' }[sc.verdict] || sc.verdict);
  const cls = historical ? '' : sc.stale || !verified || sc.verdict === 'no' || !sc.reachable ? 't-flag' : sc.verdict === 'strong' ? 't-hero' : 't-plus';
  return `<div class="d-sec"><h4>Website research<span class="grow"></span><span class="tag ${cls}"><span>${esc(label)}${historical || sc.stale || sc.reachable ? '' : ' · not reachable'}</span></span></h4>
    ${historical ? '<p class="muted">Your relationship update takes priority over this earlier check.</p>' : sc.stale ? '<p class="muted">This check is older than the current profile. Its verdict needs a new review.</p>' : !verified ? '<p class="muted">The cited evidence could not be checked. The earlier score remains in place; Research can be checked again later.</p>' : ''}
    <p class="d-reason">${esc(sc.summary || '')}</p>
    ${sc.sources?.length ? `<div class="d-links">${sc.sources.slice(0, 5).map((u) => { const h = (() => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch { return u; } })(); return safeUrl(u) ? `<a class="btn" href="${esc(safeUrl(u))}" target="_blank" rel="noopener noreferrer">${esc(h)}</a>` : ''; }).join('')}</div>` : ''}</div>`;
}
function websiteEvidence(site) {
  if (!site) return '';
  const url = safeUrl(site.final_url) || safeUrl(site.url);
  const tags = !site.stale && Array.isArray(site.tags) ? site.tags.filter((t) => typeof t === 'string') : [];
  return `<section class="ql-site"><div class="ql-sh"><b>${site.summary_source === 'page_excerpt' ? 'From website' : 'Website evidence'}</b>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(site.title || url)}</a>` : ''}${site.at ? `<em>Checked ${esc(site.at.slice(0, 10))}</em>` : ''}</div>
    ${site.stale ? '<p class="muted">Older website evidence. Check the site again before using these claims.</p>' : `${site.summary ? `<p>${esc(site.summary)}</p>` : ''}${tags.length ? `<div class="ql-facts">${tags.map((t) => `<span>${esc(t)}</span>`).join('')}</div>` : ''}`}
    ${site.error ? `<details class="ql-site-error"><summary>Website unavailable</summary><p class="muted">${esc(ucf(site.error))}</p></details>` : ''}</section>`;
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
  const p = S.person;
  if (!p) return;
  const v = p.verdict || {};
  const edges = p.edges || (p.via || []).map((s) => ({ seed: s }));
  const oldEdges = (p.edge_history || []).filter((e) => e.state !== 'observed');
  const n = p.lists != null ? lists(p) : new Set(edges.map((e) => e.seed)).size;
  const tags = detailTagSelection(p);
  const manualTags = [...tags.primary, ...tags.related].filter((t) => t.source === 'manual' && !humanRelationships(p).some(key => HUMAN_RELATIONSHIPS[key]?.toLowerCase() === t.tag.toLowerCase()));
  // Fit has its own ring in the summary, so verdict tags stay out of the tag list.
  const overviewTags = tags.primary.filter((t) => t.source !== 'manual' && rowTagFacet(t) !== 'verdict');
  const evidenceTags = tags.related.filter((t) => t.source !== 'manual');
  const url = safeUrl(p.website);
  const site = p.website ? String(p.website).replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, '') : '';
  const panel = $('#detail');
  const detailFocus = detailAccess.capture();
  const mentionFocus = LeadNoteMentions.capture($('#note'));
  const view = detailView(p.id);
  const tagVal = view.tag;
  const noteVal = noteQueue.peek(p.id)?.draft ?? p.note ?? '';
  rememberWorkflowForm();
  const reason = p.reason || v.reason;
  const ev = evidenceOf(v);
  const you = observedRelationship(p) ? '' : youLink({...p, relationship: Object.hasOwn(p, 'owner_relationship') ? 'unknown' : p.relationship});
  const role = p.role || v.role;
  const profile = detailProfileState(p);
  const bio = p.bio ? esc(p.bio) : p.loading ? 'Loading profile…' : p.failed ? 'Profile could not be loaded.' : p.bio_at ? 'No bio on this profile.' : 'Profile has not been read yet.';
  panel.dataset.owner = String(p.id);
  const fitLabel = p.loading ? '' : fitDescription(p);
  const why = reason || (p.bio ? String(p.bio).split(/\n/)[0].trim() : '');
  const connectionLines = leadConnectionLines({...p, edges});
  const connection = you || connectionLines.slice(0, 2).join(' · ') + (connectionLines.length > 2 ? ' +' + (connectionLines.length - 2) : '');
  const readDate = p.bio_at ? new Date(p.bio_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : '';
  panel.innerHTML = `
    <header class="d-head">${avatar(p.pic, p.name || p.handle, 'lg')}
      <div class="who"><b id="d-person-title" tabindex="-1">${esc(p.name || p.handle || '')}</b><span>@${esc(p.handle)}${role ? `<i class="d-role">${esc(ucf(role))}</i>` : ''}</span></div>
      <div class="d-tools">${S.view === 'leads' && S.rows.length > 1 ? `<button type="button" id="d-prev" class="d-icon" aria-label="Previous lead" title="Previous (k)">${icon('arrowUp')}</button><button type="button" id="d-next" class="d-icon" aria-label="Next lead" title="Next (j)">${icon('arrowDown')}</button>` : ''}<button type="button" class="d-icon d-close" id="d-close" aria-label="Close lead details" title="Close (esc)">${icon('close')}</button></div></header>
    ${p.failed ? '<div class="d-sec"><p class="bad" role="status">Could not load this lead.</p><button class="btn" id="d-retry">Retry</button></div>' : ''}
    <section class="d-sec d-summary" aria-label="Summary">
      ${why ? `<p class="d-why">${esc(why)}</p>` : p.loading ? '<p class="d-why"><i class="sk sk-line"></i></p>' : ''}
      <div class="d-facts">${p.loading ? '' : `<span class="d-fit">${rowFitHTML(p, 32)}<b>${esc(fitLabel)}</b></span>`}<span class="d-follower-count"><b class="num">${fmt(p.followers)}</b> followers</span>${relationshipHTML(p)}</div>
      ${connection ? `<p class="d-connection-summary">${esc(connection)}</p>` : ''}
      <div class="d-primary">
        ${p.handle ? `<a class="btn solid d-instagram" href="https://www.instagram.com/${encodeURIComponent(p.handle)}/" target="_blank" rel="noopener">Open Instagram${icon('external', 14)}</a>` : ''}
        ${url ? `<a class="btn d-website" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Website${icon('external', 14)}</a>` : ''}
      </div>
    </section>
    <section class="d-sec d-status-section" aria-labelledby="d-h-status"><h3 class="d-h" id="d-h-status">Lead stage</h3>${statusChoicesHTML(p)}${p.owner_conflict ? `<p class="d-owner-conflict" role="status">${esc(p.owner_conflict)}</p>` : ''}</section>
    <section class="d-sec d-note-section"><h3 class="d-h"><label for="note">Note</label><span class="grow"></span><span class="d-note" id="note-st" role="status" aria-live="polite">${esc(noteStatus(p.id))}</span></h3><textarea class="input" id="note" data-id="${p.id}" aria-describedby="note-st" ${p.loading || p.failed ? 'disabled' : ''} placeholder="Add a note · @ to link">${esc(noteVal)}</textarea><div id="note-conflict" role="status">${noteConflictHTML(p.id)}</div><div id="note-insights">${noteInsightsHTML(p)}</div></section>
    ${workflowSummaryHTML(p)}
    <section class="d-sec d-labels-section" aria-labelledby="d-h-tags"><h3 class="d-h" id="d-h-tags">Tags</h3>
      ${overviewTags.length || manualTags.length ? `<div class="d-tags">${overviewTags.map((t) => tagChip(t)).join('')}${manualTags.map((t) => `<span class="d-tag-item">${tagChip(t)}<button type="button" class="d-tag-remove" data-rmtag="${esc(t.tag)}" aria-label="Remove ${esc(t.tag)} tag" title="Remove ${esc(t.tag)}">${icon('close', 10)}</button></span>`).join('')}</div>` : ''}
      <form class="tag-add d-label-editor" id="tag-form"><input class="input" id="tag-in" data-owner="${p.id}" aria-label="Add a tag" list="tag-dl" placeholder="Add a tag" autocomplete="off" value="${esc(tagVal)}"><button class="btn" type="submit">Add tag</button></form>
    </section>
    <section class="d-sec d-know-section" aria-labelledby="d-h-know"><h3 class="d-h" id="d-h-know">How you know them</h3>${knowThemHTML(p)}</section>
    <details class="d-sec d-disclosure" data-detail-section="profile" data-owner="${p.id}" ${view.sections.profile ? 'open' : ''}><summary id="d-profile-summary"><span>More details</span>${icon('chevronDown', 14, 'd-chev')}</summary>
      <h4 class="d-h2">Profile</h4>
      ${p.category ? `<p class="d-category">${esc(p.category)}</p>` : ''}
      <div class="d-bio${p.bio ? '' : ' muted'}">${bio}</div>
      <dl class="d-stats"><div><dt>Followers</dt><dd class="num">${int(p.followers)}</dd></div><div><dt>Following</dt><dd class="num">${int(p.following)}</dd></div><div><dt>Posts</dt><dd class="num">${int(p.posts)}</dd></div>${p.score == null ? '' : `<div><dt>Priority</dt><dd class="num">${esc(p.score)}</dd></div>`}</dl>
      ${site && !url ? `<p class="muted">${esc(site)}</p>` : ''}
      <p class="muted profile-freshness">${readDate ? 'Profile read ' + esc(readDate) + '.' : 'Profile not read yet.'} Source: ${esc(profile.source.toLowerCase())}.</p>
      ${profile.message ? `<p class="${profile.failed ? 'bad' : 'muted'} profile-freshness" role="status">${esc(profile.message)}</p>` : ''}
      ${!p.loading && !p.failed ? `<button class="btn" id="d-read" ${profile.pending ? 'disabled' : ''}>${esc(profile.button)}</button>` : ''}
      ${ev.length || evidenceTags.length || p.site || p.scout ? `<h4 class="d-h2">Evidence</h4>` : ''}
      ${evidenceTags.length ? `<div class="d-tags d-evidence-tags">${evidenceTags.map((t) => tagChip(t)).join('')}</div>` : ''}
      ${ev.length ? `<ul class="evidence">${ev.map((q) => `<li>${esc(q)}</li>`).join('')}</ul>` : ''}
      ${websiteEvidence(p.site)}
      ${scoutHTML(p.scout)}
      <h4 class="d-h2">Connections${n ? `<span class="d-h2-n">${plural(n, 'list')}</span>` : ''}</h4>
      ${connectionEvidenceHTML(edges, oldEdges)}
      <h4 class="d-h2">History</h4><div id="activity-timeline">${activityHTML(p.activity, p.id)}</div>
      ${p.review_history?.length ? `<h4 class="d-h2">Review history</h4><ol class="activity-list">${p.review_history.map(r => `<li><div><strong>${esc(({complete:'Checked',needs_research:'Needs more evidence',unverified:'Could not verify',archived:'Earlier result'})[r.status] || 'Earlier result')}${r.score != null ? `, ${esc(r.score)}` : ''}</strong><time datetime="${esc(r.created_at)}">${esc(new Date(r.created_at).toLocaleString())}</time></div>${r.reason ? `<p>${esc(r.reason)}</p>` : ''}</li>`).join('')}</ol>` : ''}
    </details>`;
  wireWorkflow(p);
  const mentionDrafts = store.get('note-mention-drafts', {});
  LeadNoteMentions.attach($('#note'), {
    selected: mentionDrafts[p.id] || p.note_mentions || [],
    search: query => api.get('/api/note-mentions?q=' + encodeURIComponent(query)),
    change: refs => { const drafts = store.get('note-mention-drafts', {}); drafts[p.id] = refs; store.set('note-mention-drafts', drafts); },
  });
  detailAccess.restore(detailFocus);
  LeadNoteMentions.restore($('#note'), mentionFocus);
}
$('#detail').addEventListener('click', async (e) => {
  if (e.target.closest('#d-close')) return closeDetail();
  const p = S.person;
  if (!p) return;
  const mentionedProfile = e.target.closest('[data-note-profile]');
  if (mentionedProfile) return openDetail(Number(mentionedProfile.dataset.noteProfile));
  const resolution = e.target.closest('[data-note-resolution]');
  if (resolution) {
    try { await noteQueue.resolve(p.id, resolution.dataset.noteResolution === 'draft'); if (S.person?.id === p.id) { if (resolution.dataset.noteResolution === 'saved') { const refs = store.get('note-mention-drafts', {}); delete refs[p.id]; store.set('note-mention-drafts', refs); S.person.note = noteQueue.peek(p.id)?.draft || ''; S.person.mark_rev = noteQueue.peek(p.id)?.revision; } renderDetail(); } }
    catch { /* The queue keeps the draft and displays the latest conflict. */ }
    return;
  }
  const qt = e.target.closest('[data-addtag]');
  if (qt) return editTags(p.id, [qt.dataset.addtag], []);
  const rmt = e.target.closest('[data-rmtag]');
  if (rmt) { e.stopPropagation(); return editTags(p.id, [], [rmt.dataset.rmtag]); }
  const tag = e.target.closest('[data-tag]');
  if (tag) return clickTag(tag.dataset.tag, e);
  if (e.target.closest('[data-note-retry]')) return retryNoteRead(p.id);
  const suggestion = e.target.closest('[data-note-fact]');
  if (suggestion) return applyNoteFact(p.id, Number(suggestion.dataset.noteFact));
  const rel = e.target.closest('[data-human-relationship]');
  if (rel) return updateHumanRelationship(p.id, 'relationships', rel.dataset.humanRelationship);
  const familiarity = e.target.closest('[data-familiarity]');
  if (familiarity) return updateHumanRelationship(p.id, 'familiarity', familiarity.dataset.familiarity);
  const nav = e.target.closest('#d-prev, #d-next');
  if (nav) { const i = S.rows.findIndex((x) => x.id === p.id) + (nav.id === 'd-next' ? 1 : -1); if (S.rows[i]) { select(i, true); openDetail(S.rows[i].id); } return; }
  const m = e.target.closest('[data-s]');
  if (m) return mark(p.id, p.status === m.dataset.s ? null : m.dataset.s);
  const sd = e.target.closest('[data-seed]');
  if (sd) { const t = canonTag('via @' + sd.dataset.seed); if (t) clickTag(t, e); else { S.f.seed = sd.dataset.seed; filtersChanged(); } return; }
  if (e.target.id === 'd-read') {
    e.target.disabled = true;
    try { await api.post(`/api/person/${p.id}/read`); if (S.person?.id === p.id) { S.person.profile_read_pending = true; renderDetail(); } toast('Profile refresh queued'); } catch (err) { e.target.disabled = false; toast('Could not queue: ' + err.message); }
  }
});
$('#detail').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (e.target.id !== 'tag-form' || !S.person) return;
  const input = $('#tag-in'), draft = input.value, value = draft.trim(), id = S.person.id;
  if (!value) return;
  const saved = await editTags(id, [value], []);
  if (saved && detailView(id).tag === draft) detailView(id).tag = '';
  if (saved && S.person?.id === id && $('#tag-in')?.value === draft) $('#tag-in').value = '';
});
$('#detail').addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || e.defaultPrevented || e.isComposing || !LeadAccessibility.isEditableTarget(e.target)) return;
  e.stopPropagation();
  (document.activeElement || e.target).blur();
  if (detailAccess.isModal()) detailAccess.focusInitial();
});
const noteQueue = LeadWorkflow.createNoteQueue({
  initial: store.get('note-drafts', {}),
  requireRevision: true,
  persist: (drafts) => store.set('note-drafts', drafts),
  save: async (id, note, revision) => {
    invalidatePersonRead(id);
    let result;
    const mentionDraft = store.get('note-mention-drafts', {})[id];
    const noteMentions = mentionDraft === undefined ? undefined : LeadNoteMentions.surviving(note, mentionDraft);
    try { result = await api.post(`/api/person/${id}/mark`, { note, if_match: revision, ...(noteMentions === undefined ? {} : { note_mentions: noteMentions }) }); }
    catch (error) {
      if (error.status === 409) {
        try { const latest = await api.get(`/api/person/${id}`); if (S.person?.id === id) S.person.note_mentions = latest.note_mentions || []; noteQueue.conflict(id, latest.note || '', latest.mark_rev ?? ''); }
        catch { /* Keep the draft blocked until a successful detail refresh. */ }
      }
      throw error;
    }
    if (noteQueue.peek(id)?.draft === note) { const drafts = store.get('note-mention-drafts', {}); delete drafts[id]; store.set('note-mention-drafts', drafts); }
    invalidatePersonRead(id);
    const r = S.rows.find((x) => x.id === id), n = M.byId.get('p:' + id);
    if (r) { r.note = note || null; r.mark_rev = result.mark_rev; }
    if (n) n.note = note || null;
    if (S.person?.id === id) {
      S.person.note = note; S.person.mark_rev = result.mark_rev; S.person.note_mentions = result.note_mentions || [];
      S.person.note_interpretation = result.note_interpretation || null;
      refreshActivity(id);
      setTimeout(() => { if (S.person?.id === id) refreshPerson(id); }, 0);
    }
    renderRows();
    return result;
  },
  change: (id) => { if (S.person?.id === id) renderNoteState(id); },
});
function noteStatus(id) { const e = noteQueue.peek(id); if (!e) return 'Saves as you type'; return e.conflict || e.error?.status === 409 ? 'Draft kept. Review the saved note.' : e.error ? 'Not saved. Draft kept; edit to retry.' : e.pending || e.draft !== e.saved ? 'Saving…' : 'Saved'; }
function noteConflictHTML(id) {
  const e = noteQueue.peek(id);
  if (!e?.conflict) return '';
  return `<p>This note changed elsewhere. Your draft is above.</p><p><b>Saved note</b><br>${esc(e.remoteNote || 'Empty note')}</p><div class="workflow-actions"><button class="btn" type="button" data-note-resolution="saved" ${e.pending ? 'disabled' : ''}>Use saved note</button><button class="btn" type="button" data-note-resolution="draft" ${e.pending ? 'disabled' : ''}>Save my draft instead</button></div>`;
}
const HUMAN_RELATIONSHIPS = { worked_with: 'Worked with', client: 'Client', colleague: 'Colleague', friend: 'Friend', acquaintance: 'Acquaintance' };
const FAMILIARITY_LABELS = { briefly: 'Briefly', know_them: 'Know them', close: 'Close' };
function humanRelationships(p) {
  return Array.isArray(p.relationships) ? p.relationships : p.status === 'client' ? ['client', 'worked_with'] : [];
}
// Where the conversation stands. One choice at a time; the second press clears it.
function statusChoicesHTML(p) {
  const disabled = p.loading || p.failed ? 'disabled' : '';
  return `<div class="marks status-choices" role="group" aria-label="Status">${STATUSES.filter(s => s !== 'no').map(s => `<button type="button" id="d-status-${s}" data-s="${s}" aria-pressed="${p.status === s}" class="${p.status === s ? 'on' : ''}" ${disabled}><span class="stat ${s}"><i></i></span><b>${slabel(s)}</b></button>`).join('')}</div>
    <button type="button" id="d-status-no" data-s="no" aria-pressed="${p.status === 'no'}" class="relationship-not-fit ${p.status === 'no' ? 'on' : ''}" ${disabled}>${p.status === 'no' ? icon('check', 14) : ''}<span>Not a fit</span></button>`;
}
// History with them, and how well. Both are optional and independent of the status above.
function knowThemHTML(p) {
  const selected = humanRelationships(p), disabled = p.loading || p.failed ? 'disabled' : '';
  return `<div class="marks relationship-choices" role="group" aria-label="How you know them">${Object.entries(HUMAN_RELATIONSHIPS).map(([key,label]) => `<button type="button" id="d-relationship-${key}" data-human-relationship="${key}" aria-label="${label} relationship" aria-pressed="${selected.includes(key)}" class="${selected.includes(key) ? 'on' : ''}" ${disabled}><b>${label}</b></button>`).join('')}</div>
    <div class="relationship-familiarity"><span>How well? <em>Optional</em></span><div class="marks" role="group" aria-label="How well you know them (optional)">${Object.entries(FAMILIARITY_LABELS).map(([key,label]) => `<button type="button" id="d-familiarity-${key}" data-familiarity="${key}" aria-pressed="${p.familiarity === key}" class="${p.familiarity === key ? 'on' : ''}" ${disabled}><b>${label}</b></button>`).join('')}</div></div>
  `;
}
function humanRelationshipHTML(p) { return statusChoicesHTML(p) + knowThemHTML(p); }
function updateHumanRelationship(id, field, value) {
  return saveHumanContext(id, p => {
    if (field === 'relationships') {
      const current = humanRelationships(p);
      return { relationships: current.includes(value) ? current.filter(v => v !== value && !(value === 'worked_with' && v === 'client')) : [...current, value] };
    }
    return { familiarity: p.familiarity === value ? null : value };
  });
}
function saveHumanContext(id, update) {
  return serializeMutation(async () => {
    try {
      await noteQueue.flush(id);
      const p = S.person?.id === id ? S.person : await api.get(`/api/person/${id}`);
      if (p.loading || p.failed) return;
      const patch = update(p);
      if (!patch) return;
      invalidatePersonRead(id);
      await api.post(`/api/person/${id}/mark`, { ...patch, if_match: p.mark_rev ?? '' });
      await refreshPerson(id);
      loadCounts(); loadFacetsSoon();
    } catch (error) {
      if (error.status === 409) await refreshPerson(id);
      toast(error.status === 409 ? 'This profile changed. Review it and try again.' : "Couldn't save the relationship. Try again.");
    }
  });
}
function noteFactPatch(p, fact) {
  const kinds = { current_client: 'client', past_client: 'client', worked_with: 'worked_with', colleague: 'colleague', friend: 'friend', acquaintance: 'acquaintance' };
  const stages = { in_conversation: 'talking', contacted: 'contacted', spoke_before: 'spoke_before', not_a_fit: 'no' };
  const relation = kinds[fact.kind];
  if (relation && !humanRelationships(p).includes(relation)) return { relationships: [...new Set([...humanRelationships(p), relation, ...(relation === 'client' ? ['worked_with'] : [])])] };
  if (Object.hasOwn(FAMILIARITY_LABELS, fact.kind) && p.familiarity !== fact.kind) return { familiarity: fact.kind };
  const status = stages[fact.kind];
  if (status && (!p.status || p.status === 'client')) return { status };
  return null;
}
function applyNoteFact(id, index) {
  const original = S.person?.id === id ? S.person.note_interpretation?.facts?.[index] : null;
  if (!original) return;
  return saveHumanContext(id, p => {
    const fact = p.note_interpretation?.facts?.find(f => f.kind === original.kind && f.quote === original.quote);
    if (!fact || !p.note?.includes(fact.quote) || p.note_interpretation.state !== 'ready') return null;
    return noteFactPatch(p, fact);
  });
}
const noteRetryBusy = new Set();
async function retryNoteRead(id) {
  if (noteRetryBusy.has(id) || S.person?.id !== id || !S.person.note_interpretation?.can_retry) return;
  noteRetryBusy.add(id); renderNoteState(id);
  try {
    await noteQueue.flush(id);
    await api.post(`/api/person/${id}/note-retry`, {});
    const poll = notePolls.get(id);
    if (poll) poll.attempts = 0;
    await refreshPerson(id);
  } catch { toast("Couldn't retry. Your note is saved."); }
  finally { noteRetryBusy.delete(id); if (S.person?.id === id) renderNoteState(id); }
}
function noteInsightsHTML(p) {
  const state = p.note_interpretation;
  const draft = noteQueue.peek(p.id);
  if (!p.note || !state || draft && (draft.pending || draft.draft !== p.note)) return '';
  if (state.state === 'disabled') return '<span class="muted">Note saved. Local processing is off.</span>';
  if (state.state === 'pending') return '<span class="muted">Reading your note locally…</span>';
  if (state.state === 'unavailable' || state.state === 'failed') return `<span class="muted">${esc(state.message || 'Note saved. Local understanding is unavailable.')}</span>${state.can_retry ? ` <button class="btn ghost" type="button" data-note-retry ${noteRetryBusy.has(p.id) ? 'disabled' : ''}>${noteRetryBusy.has(p.id) ? 'Retrying…' : 'Retry reading'}</button>` : ''}`;
  if (state.state !== 'ready') return '';
  const facts = (state.facts || []).map((fact, index) => {
    if (!fact.label || typeof fact.quote !== 'string' || !p.note.includes(fact.quote)) return '';
    const patch = noteFactPatch(p, fact);
    const known = ['current_client','past_client','worked_with','colleague','friend','acquaintance'].includes(fact.kind);
    if (known && !patch) return '';
    const label = patch?.relationships ? HUMAN_RELATIONSHIPS[fact.kind === 'current_client' || fact.kind === 'past_client' ? 'client' : fact.kind] : patch?.familiarity ? FAMILIARITY_LABELS[patch.familiarity] : patch?.status ? slabel(patch.status) : '';
    const action = patch ? ` <button class="btn ghost" type="button" data-note-fact="${index}">Set ${esc(label)}</button>` : '';
    const profiles = fact.kind === 'mentioned_connection' ? (p.note_mentions || [])
      .filter(ref => Number.isSafeInteger(ref.person_id) && LeadNoteMentions.contains(fact.quote, ref.token))
      .map(ref => `<button class="btn ghost" type="button" data-note-profile="${ref.person_id}" aria-label="Open @${esc(ref.handle)}">@${esc(ref.handle)}${ref.name ? ' · ' + esc(ref.name) : ''}</button>`).join(' ') : '';
    return `<p><span class="muted">${esc(fact.label)}:</span> “${esc(fact.quote)}”${action}</p>${profiles ? `<p class="note-mentioned-profiles">${profiles}</p>` : ''}`;
  }).join('');
  return facts + (facts && state.ranking_effect ? `<p class="muted note-ranking-effect">${esc(state.ranking_effect)}</p>` : '');
}
function renderNoteState(id) {
  if ($('#note-st')) $('#note-st').textContent = noteStatus(id);
  if ($('#note-conflict')) $('#note-conflict').innerHTML = noteConflictHTML(id);
  if ($('#note-insights') && S.person?.id === id) $('#note-insights').innerHTML = noteInsightsHTML(S.person);
}
$('#detail').addEventListener('input', (e) => {
  if (e.target.id === 'note' && S.person) noteQueue.edit(S.person.id, e.target.value, S.person.note || '', S.person.mark_rev ?? '');
});
window.addEventListener('beforeunload', (e) => { if (noteQueue.dirty()) { noteQueue.flushAll().catch(() => {}); e.preventDefault(); e.returnValue = ''; } });
document.addEventListener('visibilitychange', () => { if (document.hidden) noteQueue.flushAll().catch(() => {}); });
async function editTags(id, add, remove) {
  invalidatePersonRead(id);
  try { await api.post(`/api/person/${id}/tags`, { add, remove }); } catch (e) { toast(e.message || "Couldn't save the tag. Try again."); return; }
  await refreshPerson(id);
  loadFacetsSoon();
  if (add.length) toast(/^client$/i.test(add[0]) ? 'Relationship set to Client' : `Tagged ${add[0]}`);
  return true;
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
const activityLabels = { dm_import: 'Instagram message history', dm: 'DM sent', reply: 'Reply received', call: 'Call', meeting: 'Meeting', note: 'Note', status: 'Status changed', follow_up_scheduled: 'Follow-up scheduled', follow_up_completed: 'Follow-up completed', follow_up_cleared: 'Follow-up cleared', identity_merged: 'Profiles merged', follow_up_merged: 'Follow-ups merged' };
function activityHTML(activity, id = S.person?.id) {
  if (!activity) return '<p class="muted">Activity is unavailable.</p><button class="btn" id="activity-retry" type="button">Retry activity</button>';
  const rows = activity.rows || [], all = !!workflowDrafts.get(id)?.historyOpen;
  const visible = all ? rows : rows.slice(0, 3);
  return `<ol class="activity-list">${visible.map((a) => `<li><div><strong>${esc(activityLabels[a.kind] || a.kind)}</strong><time datetime="${esc(a.happened_at)}">${esc(new Date(a.happened_at).toLocaleString())}</time></div>${a.body ? `<p>${esc(a.body)}</p>` : ''}${a.kind === 'dm_import' ? `<p class="muted">${esc(a.after_value?.outbound ?? 0)} sent · ${esc(a.after_value?.inbound ?? 0)} received · From Instagram download</p>` : a.before_value != null || a.after_value != null ? `<p class="muted">${esc(activityValue(a.before_value))} → ${esc(activityValue(a.after_value))}</p>` : ''}</li>`).join('')}</ol>${!rows.length ? '<p class="muted">No activity recorded yet.</p>' : ''}<div class="workflow-actions">${rows.length > 3 || activity.next_cursor ? `<button class="btn" type="button" id="activity-show-all" aria-expanded="${all}">${all ? 'Show less activity' : 'Show all activity'}</button>` : ''}${all && activity.next_cursor ? `<button class="btn" type="button" id="activity-more" ${activityMoreBusy.has(id) ? 'disabled' : ''}>Load older activity</button>` : ''}</div>`;
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
  const status = !f ? 'No follow-up scheduled' : f.completed_at ? 'Follow-up completed' : overdue ? 'Overdue, was due ' + shortDate(f.due_on) : f.due_on === today ? 'Due today' : 'Due ' + shortDate(f.due_on);
  const disabled = workflowBusy(p.id) ? 'disabled' : '';
  return `<section class="d-sec workflow" id="workflow-summary" aria-labelledby="d-h-follow"><h3 class="d-h" id="d-h-follow">Follow-up</h3>
    ${open ? `<div class="workflow-summary-line"><p class="${overdue ? 'bad' : 'muted'}">${esc(status)}</p><button type="button" class="btn" id="followup-complete" data-follow-action="complete" ${disabled}>Complete</button></div>` : ''}
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
  invalidatePersonRead(id);
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
    invalidatePersonRead(id);
    const response = await api.post(`/api/person/${id}/follow-up`, body);
    if (S.person?.id === id) rememberWorkflowForm();
    const untouchedReminder = submitted.due === (baseline && !baseline.completed_at ? baseline.due_on : '') && submitted.action === (baseline && !baseline.completed_at ? baseline.note || '' : '');
    if (!body.action || untouchedReminder) acknowledgeWorkflowDraft(id, submitted, ['due', 'action'], 'followOpen');
    workflowMessage(id, 'followup', body.action === 'complete' ? 'Follow-up completed' : body.action === 'clear' ? 'Follow-up cleared' : 'Follow-up saved');
    applyWorkflowResponse(id, response);
    if (S.open === id) await refreshPerson(id);
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
    invalidatePersonRead(id);
    const response = await api.post(`/api/person/${id}/activity`, body);
    if (S.person?.id === id) rememberWorkflowForm();
    const cleared = acknowledgeWorkflowDraft(id, submitted, activityFields, 'activityOpen');
    if (cleared) workflowDrafts.set(id, { ...workflowDrafts.get(id), optionsOpen: false });
    if (body.follow_up && reminder.due === (baseline && !baseline.completed_at ? baseline.due_on : '') && reminder.action === (baseline && !baseline.completed_at ? baseline.note || '' : '')) acknowledgeWorkflowDraft(id, reminder, ['due', 'action'], 'followOpen');
    workflowMessage(id, 'activity', 'Interaction saved');
    applyWorkflowResponse(id, response);
    if (S.open === id) await refreshPerson(id);
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
// ---------- keyboard ----------
const detailAccess = LeadAccessibility.createDetailFocus({
  panel: $('#detail'), isMobile: narrow,
  fallbackFocus: () => S.view === 'leads' ? $('#rows .row.cur') || $('#q') : S.view === 'map' ? $('#map-canvas') : $('.tabs a.on'),
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
    if (S.open) { closeDetail(); return; }
    if (S.view === 'map' && M.focus) { M.focus = null; M.draw(); return; }
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
  if (S.view === 'map' && $('#pane-map').classList.contains('comparing')) {
    if (k === '/') { e.preventDefault(); $('#connection-target').focus(); }
    return;
  }
  if (k === '/') { e.preventDefault(); (S.view === 'map' ? $('#map-q') : $('#q')).focus(); return; }
  if (k === '#') { e.preventDefault(); const q = $('#q'); q.value = (q.value.trim() ? q.value.trim() + ' ' : '') + '#'; q.focus(); suggest(); return; }
  if (k === '[') { toggleSide(); return; }
  if (k === 'c') { clearFilters(); return; }
  if (S.view === 'map') {
    // Comparison has its own selection; do not act on the retained overview person.
    if ($('#pane-map').classList.contains('comparing')) return;
    if (k === 'f') { M.fit(); return; }
    if (k === '+' || k === '=') { M.zoomBy(1.4); return; }
    if (k === '-') { M.zoomBy(1 / 1.4); return; }
    const p = M.focus || (S.open ? S.person : null);
    if (p && /^[0-5]$/.test(k)) mark(p.id, k === '0' ? null : STATUSES[+k - 1]);
    if (p && k === 'm') mark(p.id, CYCLE[(CYCLE.indexOf(p.status ?? null) + 1) % CYCLE.length]);
    if (p && k === 'o') window.open(`https://www.instagram.com/${encodeURIComponent(p.handle)}/`, '_blank', 'noopener');
    if (p && k === 't' && S.open === p.id) { e.preventDefault(); LeadAccessibility.revealAndFocus($('#tag-in')); }
    return;
  }
  // Tab can focus a different lead without using the j/k navigation cursor.
  const focusedRow = e.target.closest('#rows .row');
  if (focusedRow) S.cur = +focusedRow.dataset.i;
  if (k === 'j' || k === 'ArrowDown' || k === 'k' || k === 'ArrowUp' || k === 'J' || k === 'K') {
    e.preventDefault();
    const down = k === 'j' || k === 'J' || k === 'ArrowDown';
    select(S.cur < 0 ? 0 : S.cur + (down ? 1 : -1), true);
    if (S.open && S.rows[S.cur]) openDetail(S.rows[S.cur].id);
    return;
  }
  const r = focusedRow ? S.rows[S.cur] : current();
  if (k === 'Enter' && S.rows[S.cur]) { e.preventDefault(); openDetail(S.rows[S.cur].id, { keyboard: true }); return; }
  if (!r) return;
  if (k === 'm') { mark(r.id, CYCLE[(CYCLE.indexOf(r.status ?? null) + 1) % CYCLE.length]); return; }
  if (k === 's') { e.preventDefault(); openStatusMenu(r.id); return; }
  if (k === 'n') { e.preventDefault(); const focusNote = () => { if (S.open === r.id) $('#note')?.focus(); }; if (S.open !== r.id) openDetail(r.id).then(focusNote); else focusNote(); return; }
  if (/^[0-5]$/.test(k)) { setStatusKey(r.id, k === '0' ? null : STATUSES[+k - 1]); return; }
  if (k === 'o') { window.open(`https://www.instagram.com/${encodeURIComponent(r.handle)}/`, '_blank', 'noopener'); return; }
  if (k === 't') {
    e.preventDefault();
    const focusTag = () => { if (S.open === r.id) LeadAccessibility.revealAndFocus($('#tag-in')); };
    if (S.open !== r.id) openDetail(r.id).then(focusTag); else focusTag();
  }
});
$('#help').onclick = (e) => { if (e.target === e.currentTarget || e.target.closest('#help-close')) setHelp(false); };
// Marking someone "Not a fit" is triage: move on to the next lead, and Undo restores this one.
function setStatusKey(id, status) {
  mark(id, status);
  if (status === 'no' && S.view === 'leads') {
    const i = S.rows.findIndex((x) => x.id === id);
    if (i >= 0 && i < S.rows.length - 1) { select(i + 1, true); if (S.open) openDetail(S.rows[i + 1].id); }
  }
}
const statusMenu = { id: null, i: 0, from: null };
function openStatusMenu(id) {
  const r = S.rows.find((x) => x.id === id) || S.person;
  if (!r) return;
  closeStatusMenu(false);
  statusMenu.id = id; statusMenu.from = document.activeElement;
  const opts = [...STATUSES.map((s, n) => [s, String(n + 1)]), [null, '0']];
  statusMenu.opts = opts.map(([s]) => s);
  statusMenu.i = Math.max(0, statusMenu.opts.indexOf(r.status ?? null));
  const el = document.createElement('div');
  el.id = 'status-menu'; el.className = 'status-menu'; el.setAttribute('role', 'menu'); el.setAttribute('aria-label', 'Set status for @' + r.handle);
  el.innerHTML = `<div class="sm-h">@${esc(r.handle)}</div>` + opts.map(([s, k], n) => `<button type="button" role="menuitem" data-sm="${n}" class="${n === statusMenu.i ? 'on' : ''}"><span>${s ? slabel(s) : 'No status'}</span><kbd>${k}</kbd></button>`).join('');
  document.body.appendChild(el);
  const anchor = $(`#rows [data-person-id="${id}"]`) || $('#detail');
  const box = anchor.getBoundingClientRect(), w = 220;
  el.style.left = Math.max(8, Math.min(innerWidth - w - 8, box.left + 56)) + 'px';
  el.style.top = Math.max(8, Math.min(innerHeight - el.offsetHeight - 8, box.bottom - 8)) + 'px';
  el.addEventListener('click', (e) => { const b = e.target.closest('[data-sm]'); if (b) chooseStatusMenu(+b.dataset.sm); });
  document.addEventListener('mousedown', statusMenuOutside, true);
}
function statusMenuOutside(e) { if (!e.target.closest('#status-menu')) closeStatusMenu(true); }
function closeStatusMenu(restore = true) {
  const el = $('#status-menu'); if (el) el.remove();
  document.removeEventListener('mousedown', statusMenuOutside, true);
  statusMenu.id = null;
  if (restore && statusMenu.from?.isConnected) statusMenu.from.focus({ preventScroll: true });
}
function chooseStatusMenu(n) {
  const id = statusMenu.id, status = statusMenu.opts[n];
  closeStatusMenu();
  if (id != null) setStatusKey(id, status);
}
function moveStatusMenu(d) {
  statusMenu.i = (statusMenu.i + d + statusMenu.opts.length) % statusMenu.opts.length;
  $$('#status-menu [data-sm]').forEach((b, n) => b.classList.toggle('on', n === statusMenu.i));
}
// Captured first so the menu owns the keyboard while it is open.
document.addEventListener('keydown', (e) => {
  if (statusMenu.id == null || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key;
  if (k === 'Escape') closeStatusMenu();
  else if (k === 'ArrowDown' || k === 'j') moveStatusMenu(1);
  else if (k === 'ArrowUp' || k === 'k') moveStatusMenu(-1);
  else if (k === 's') closeStatusMenu();
  else if (k === 'Enter' || k === ' ') chooseStatusMenu(statusMenu.i);
  else if (/^[0-5]$/.test(k)) chooseStatusMenu(k === '0' ? statusMenu.opts.length - 1 : +k - 1);
  else if (k === 'Tab') closeStatusMenu(false);
  else return;
  if (k !== 'Tab') { e.preventDefault(); e.stopImmediatePropagation(); }
}, true);

// ---------- tags manager ----------
const T = {
  list: [], rules: [], q: '', editing: null, confirm: null,
  async show() { await Promise.all([this.load(), this.loadRules()]); },
  async load() {
    try { this.list = mergeTags(await api.get('/api/tags')); this.failed = false; } catch (e) { this.failed = !this.list.length; }
    this.render();
  },
  async loadRules() {
    try { this.rules = await api.get('/api/tag-rules'); this.rulesErr = null; } catch (e) { this.rulesErr = e.status === 404 ? 'missing' : 'failed'; if (this.rulesErr === 'missing') this.rules = null; }
    this.renderRules();
  },
  // Custom labels can be renamed or deleted. Relationship choices belong to each person.
  editable: (t) => t.grp !== 'relationship' && t.sources.includes('manual'),
  render() {
    const q = this.q.toLowerCase();
    this.renderGroups(q);
    const rows = this.list.filter((t) => this.editable(t) && (!q || t.tag.toLowerCase().includes(q)))
      .sort((a, b) => b.total - a.total || a.tag.localeCompare(b.tag));
    $('#tg-n').textContent = int(rows.length);
    const ed = this.editing;
    $('#tg-body').innerHTML = rows.length ? rows.map((t) => {
      const can = this.editable(t);
      const label = `<button class="tag ${KIND[t.kind]}" ${tagStyleAttrs(t)} data-go="${esc(t.tag)}" title="Show leads with this tag">${tagContent(t)}</button>`;
      const name = ed === t.tag ? `<form class="ren" data-ren="${esc(t.tag)}"><input class="input" id="ren-in" value="${esc(t.tag)}" autocomplete="off" spellcheck="false"><button class="btn solid" id="ren-go">Rename</button><button type="button" class="btn" data-cancel>Cancel</button></form>` : label;
      return `<tr class="${t.total ? '' : 'dim'}">
        <td>${name}</td>
        <td class="r num">${int(t.total)}</td>
        <td class="r">${can && ed !== t.tag ? `<span class="acts"><button data-edit="${esc(t.tag)}">Rename</button><button data-del="${esc(t.tag)}" class="${this.confirm === t.tag ? 'warn' : ''}">${this.confirm === t.tag ? 'Confirm' : 'Delete'}</button></span>` : ''}</td></tr>`;
    }).join('') : `<tr><td colspan="3" class="muted">${this.failed ? 'Could not load tags' : q ? 'No match' : 'None yet. Open a person and add a label.'}</td></tr>`;
    if (ed) { const i = $('#ren-in'); if (i && document.activeElement !== i) { i.focus(); i.select(); } this.syncRen(); }
  },
  renderGroups(q) {
    const matches = t => !q || (t.tag + ' ' + tagLabel(t.tag)).toLowerCase().includes(q);
    const rows = this.list.filter(matches);
    const category = t => {
      const name = tagName(t), key = tagKey(t);
      if (isProspectTag(t)) return 'fit';
      if (BUSINESS_TAGS.has(key)) return 'business';
      if (tagTone(t) === 'review' || FLAG_TAGS.has(name) || SOFT_TAGS.has(name)) return 'review';
      if (isViaTag(name)) return 'via';
      if (t.grp === 'niche' || PRODUCT_TAGS.has(key)) return 'products';
      if (t.grp === 'size') return 'size';
      if (t.grp === 'source') return 'collection';
      if (t.grp === 'signal' || QUIET_TAGS.has(key)) return 'clues';
      return 'other';
    };
    // These are real filters, including zero results. No score or label is invented.
    const defaults = [['Exceptional fit', 'manual'], ['AI: Top fit', 'auto'], ['Fit: strong', 'auto'], ['Fit: good', 'auto']];
    const fit = rows.filter(t => category(t) === 'fit');
    for (const [tag, kind] of defaults) {
      if (!fit.some(t => t.tag === tag) && matches({tag})) fit.push({tag, kind, source: kind, sources: [kind], grp: 'signal', total: 0, count: 0});
    }
    const levelOrder = {exceptional: 0, priority: 1, strong: 2, accent: 3, standard: 4, quiet: 5, background: 6};
    const chip = t => `<button class="tchip t-${tagTier(t) || 'ctx'}" ${tagStyleAttrs(t)} data-go="${esc(t.tag)}" title="${esc(t.tag)} · ${int(t.total)} people">${tagContent(t, null, t.total)}</button>`;
    const sec = (key, title, list, cls = '', note = '') => {
      if (!list.length) return '';
      list = [...list].sort((a, b) => levelOrder[tagImportance(a)] - levelOrder[tagImportance(b)] || Number(!!tagTone(b)) - Number(!!tagTone(a)) || b.total - a.total || a.tag.localeCompare(b.tag));
      const limit = this.more?.[key] || (q ? 50 : key === 'via' ? 8 : 16);
      return `<section class="tg-sec ${cls}"><div class="tg-ch"><h3>${esc(title)}</h3><span class="num muted">${list.length}</span></div>
        <div class="tg-chips">${list.slice(0, limit).map(chip).join('')}${list.length > limit ? `<button class="tchip more" data-tmore="${key}">+${list.length - limit} more</button>` : ''}</div>
        ${note ? `<p class="tg-section-note">${esc(note)}</p>` : ''}</section>`;
    };
    const inGroup = key => rows.filter(t => category(t) === key);
    const context = [['products','Products'], ['clues','Other clues'], ['size','Audience size'], ['via','Found via'], ['collection','Scraping'], ['other','Other']]
      .map(([key,title]) => sec(key,title,inGroup(key))).join('');
    $('#tg-groups').innerHTML = sec('fit', 'Best prospects', fit, 'tg-top', 'Exceptional fit is added by you. Other fit tags come from saved checks.')
      + `<div class="tg-main">${sec('business', 'Business signals', inGroup('business'))}${sec('review', 'Needs review', inGroup('review'))}</div>`
      + (context ? `<div class="tg-context"><div class="tg-context-heading"><h3>Other tags</h3></div><div class="tg-rest">${context}</div></div>` : '')
      + (!rows.length && !fit.length ? '<p class="muted tg-empty">No matching tags.</p>' : '');
  },
  syncRen() {
    const i = $('#ren-in'); if (!i) return;
    const v = i.value.trim();
    const exists = v && v !== this.editing && this.list.some((t) => t.tag.toLowerCase() === v.toLowerCase());
    $('#ren-go').textContent = exists ? 'Merge' : 'Rename';
  },
  async rename(from, to) {
    to = to.trim();
    if (!to || to === from) { this.editing = null; this.render(); return; }
    const canon = this.list.find((t) => t.tag.toLowerCase() === to.toLowerCase());
    if (canon) to = canon.tag;
    try { await api.post('/api/tags/rename', { from, to }); } catch (e) { toast("Couldn't rename. Try again."); return; }
    this.fixFilter(from, to);
    this.editing = null;
    toast(canon ? `Merged ${from} into ${to}` : `Renamed to ${to}`);
    this.after();
  },
  async remove(tags) {
    for (const tag of tags) {
      try { await api.post('/api/tags/delete', { tag }); } catch (e) { toast("Couldn't delete " + tag + '. Try again.'); return; }
      this.fixFilter(tag, null);
    }
    this.confirm = null;
    toast(tags.length === 1 ? `Deleted ${tags[0]}` : `Deleted ${tags.length} tags`);
    this.after();
  },
  fixFilter(from, to) {
    for (const k of ['tags', 'any', 'not']) S.f[k] = S.f[k].map((t) => t === from ? to : t).filter(Boolean);
  },
  after() { this.load(); this.loadRules(); loadFacets(); resetLeads(true); },
  renderRules() {
    const rs = this.rules;
    $('#rl-n').textContent = rs ? int(rs.length) : '';
    $('#rl-body').innerHTML = this.rulesErr === 'failed' && !rs?.length ? `<tr><td colspan="5" class="muted">Could not load rules</td></tr>` : rs == null ? `<tr><td colspan="5" class="muted">Rules not available on this server</td></tr>`
      : rs.length ? rs.map((r) => `<tr><td><button class="tag rule" ${tagStyleAttrs({tag:r.tag, source:"rule"})} data-go="${esc(r.tag)}">${tagContent({tag:r.tag, source:"rule"})}</button></td><td class="mono hide-sm">${esc(ucf(r.field))}</td>
        <td class="mono">"${esc(r.match)}"</td><td class="r num">${int(r.hits)}</td><td class="r"><button class="rl-del" data-rdel="${esc(r.id)}">Delete</button></td></tr>`).join('')
        : `<tr><td colspan="5" class="muted">No rules</td></tr>`;
  },
};
$('#tg-q').addEventListener('input', (e) => { T.q = e.target.value; T.more = {}; T.render(); });
function goTag(tag) { S.f = emptyFilter(); S.f.tags = [tag]; $('#q').value = ''; S.view = 'leads'; filtersChanged(); setView('leads'); }
$('#view-tags').addEventListener('click', (e) => {
  const go = e.target.closest('[data-go]');
  if (go) return goTag(go.dataset.go);
  const tm = e.target.closest('[data-tmore]');
  if (tm) {
    const key = tm.dataset.tmore;
    T.more = Object.assign(T.more || {}, { [key]: (T.more?.[key] || (T.q ? 20 : 12)) + 50 });
    return T.render();
  }
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
  } catch (err) { toast(err.status === 400 ? ucf(err.message) : "Couldn't add the rule. Try again."); return; }
  T.after();
});
async function deleteRule(id) {
  const r = (T.rules || []).find((x) => String(x.id) === String(id));
  try { await api.post(`/api/tag-rules/${encodeURIComponent(id)}/delete`); } catch (e) { toast("Couldn't delete the rule. Try again."); return; }
  toast(r ? `Deleted rule ${r.tag}` : 'Rule deleted', r ? () => api.post('/api/tag-rules', { tag: r.tag, field: r.field, match: r.match }).then(() => T.after()) : null);
  T.after();
}



// ---------- scraper ----------
const SCRAPER_FULL_VIEWS = new Set(['scraper', 'accounts', 'settings', 'qual']);
// Sidebar counts come from the lightweight scraper snapshot.
function renderStatus() {
  const sc = S.sc;
  const q = sc ? (sc.queue?.list || 0) + (sc.queue?.profile || 0) : 0;
  $('#n-queue').textContent = q ? fmt(q) : '';
  const accs = sc?.accounts || [];
  const alerts = (sc?.alerts || []).filter((x) => x.level === 'error').length;
  $('#n-acc').textContent = alerts ? String(alerts) : '';
  $('#n-acc').hidden = !alerts;
}
async function loadScraper() {
  if (S.scLoading) return;
  S.scLoading = true;
  if (S.view === 'scraper' && (!S.sc || S.scError)) renderScraper();
  try { S.sc = await api.get('/api/scraper'); S.scError = false; S.scStale = false; }
  catch (e) { S.scError = true; S.scStale = true; }
  finally { S.scLoading = false; }
  syncSeed();
  if (SCRAPER_FULL_VIEWS.has(S.view)) await loadProcessingStatus();
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
  if (SCRAPER_FULL_VIEWS.has(S.view)) await loadProcessingStatus();
  if (S.view === 'accounts') renderAccounts();
  if (S.view === 'settings') renderCheckingMode();
  if (S.view === 'qual') Q.renderProg();
}
let listFilter = 'all';
const LIST_STATE = { partial: 'Partial', running: 'Collecting', queued: 'Not started', paused: 'Paused', error: 'Failed', private: 'Private account', done: 'Done' };
function eta(h) {
  if (h == null) return null;
  if (h <= 0) return 'done';
  const m = Math.round(h * 60);
  if (m < 60) return `about ${Math.max(1, m)} min`;
  if (h < 48) return `about ${Math.round(h)} h`;
  return `about ${Math.round(h / 24)} days`;
}
function renderScraper() {
  mountCollectionTargets();
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
  const listStage = (sc.stages || sc.control?.stages)?.find(s => s.id === 'lists');
  const listPaused = !!sc.paused || !!listStage?.paused;
  const listHeld = listStage && ['cooldown', 'blocked', 'waiting', 'held'].includes(listStage.state);
  const listActive = !listPaused && listStage?.active === true;
  const run = ls.find((l) => l.state === 'running');
  // One plain sentence: what is happening right now.
  const cool = x.cooldown_until && Date.parse(x.cooldown_until) > Date.now();
  const capped = ['list', 'profile'].some((k) => x.budget?.[k] && (x.today?.[k] || 0) >= x.budget[k]);
  const reading = listActive && run && `Reading @${run.seed}'s ${run.direction === 'followers' ? 'followers' : 'following list'}`;
  let now, sub = '';
  if (S.scStale && S.sc) { now = 'Connection lost'; sub = 'Showing the last update. Progress may have changed.'; }
  else if (listPaused) { now = 'Paused'; sub = 'Use Start scraping at the top to continue from saved progress.'; }
  else if (listHeld) { now = 'Collection waiting'; sub = listStage.now || collectionReason(listStage.reason_code || listStage.wait?.why || listStage.reason, 'Progress is saved. Waiting to continue.'); }
  else if (!x.online) { now = 'Accounts disconnected'; sub = 'Use Connect accounts to reopen your saved Chrome profiles.'; }
  else if (cool && reading && x.state === 'running') { now = reading; sub = `Bio reads are on a short break so Instagram doesn't flag your account. Back ${backIn(x.cooldown_until)}.`; }
  else if (cool && capped) { now = 'Daily limit reached'; sub = `Resumes ${backIn(x.cooldown_until)}. Progress is saved.`; }
  else if (cool && Date.parse(x.cooldown_until) - Date.now() > 3600e3) { now = 'Resting'; sub = `Instagram asked us to slow down. Back ${backIn(x.cooldown_until)}.`; }
  else if (cool) { now = 'Short break'; sub = `So Instagram doesn't flag your account. Back ${backIn(x.cooldown_until)}.`; }
  else if (reading) { now = reading; sub = `${int(run.received)}${run.total ? ' of ' + int(run.total) : ''} list entries saved so far.`; }
  else { now = listStage?.now || 'Waiting for confirmed activity'; sub = ''; }
  const accs = sc.accounts || [];
  const conn = accs.length ? accs.map((a) => `<span class="cpill" title="${esc(ST_LABEL[a.status] || a.status)}"><i class="dot ${a.online ? 'live' : 'off'}"></i>${esc(a.name || a.handle || a.lane_id)}<span class="muted">${a.online ? `${int(a.hour?.people || 0)} list entries this hour` : 'offline'}</span></span>`).join('')
    : `<span class="cpill"><i class="dot ${x.online ? 'live' : 'off'}"></i>Extension ${x.online ? 'connected' : 'not connected'}</span>`;
  if (!S.scError) $('#now').innerHTML = `<div class="now-line"><i class="dot ${x.online && listActive ? 'live' : 'off'}"></i><div><b>${esc(now)}</b><span class="muted">${esc(sub)}</span></div></div><div class="conns">${conn}</div>`;
  const h1 = sc.soak?.['1h'] || {};
  const tile = (label, v, small) => `<div class="tile"><span>${label}</span><b class="num">${v}</b><small>${small}</small></div>`;
  $('#scr-counts').innerHTML = tile('New profiles this hour', (sc.control?.progress?.unique_new_profiles?.hour ?? h1.new_people) == null ? '–' : int(sc.control?.progress?.unique_new_profiles?.hour ?? h1.new_people), sc.rate?.pages_hour ? `${int(Math.round(sc.rate.pages_hour))} list pages an hour` : 'new people')
    + tile('New profiles today', (sc.control?.progress?.unique_new_profiles?.today ?? sc.people_today) == null ? '–' : int(sc.control?.progress?.unique_new_profiles?.today ?? sc.people_today), 'newly added profiles')
    + tile('Profiles saved', S.counts?.total != null ? int(S.counts.total) : '–', 'people in Leads');
  const L = pr.lists || {}, B = pr.bios || {}, Q = pr.qualify || {};
  const minuteRate = (value, noun) => value == null ? `measuring ${noun}` : `${int(value)} ${noun} in the last minute`;
  const recv = sc.coverage?.lists?.saved_entries ?? ls.reduce((a, l) => a + (l.saved_entries ?? l.received ?? 0), 0);
  const incompleteRows = ls.filter((l) => l.state === 'partial' || l.state === 'error');
  const incompleteLists = L.incomplete_lists ?? incompleteRows.length;
  const incompleteLeft = L.incomplete_left;
  const cappedLists = L.capped_lists ?? 0;
  const tot = recv + (L.left || 0);
  const stage = (title, line, pct, when) => `<div class="stg"><div class="st-top"><b>${title}</b><span class="muted">${when || ''}</span></div>
    ${pct == null ? '' : `<div class="bar-p ${pct >= 100 ? 'done' : 'run'}"><i style="width:${Math.min(100, pct || 0)}%"></i></div>`}<div class="muted">${line}</div></div>`;
  const offline = x.online ? '' : 'waiting for the extension';
  const listsDone = ls.length > 0 && ls.every(l => l.completion === 'complete');
  const listWhen = S.scStale ? 'Last known progress' : listsDone ? 'Done' : listPaused ? 'Paused' : listHeld ? collectionReason(listStage.reason_code || listStage.wait?.why, 'Waiting')
    : L.left === 0 && incompleteLists ? 'Incomplete lists need review'
    : !h1.pages ? 'No pages saved this hour' : L.per_minute === 0 ? 'No rows returned this minute'
    : sc.collection?.eta ? `${sc.collection.eta.scope === 'known_lists' ? 'Known lists' : 'Current queue'}: ${eta(sc.collection.eta.low_minutes / 60)}–${eta(sc.collection.eta.high_minutes / 60).replace('about ', '')}` : sc.collection?.message || (run ? 'Reading now' : offline || 'Waiting');
  const bioLine = `${int(B.left)} bios to read · ${minuteRate(B.per_minute, 'bios')}`
    + (B.per_day ? ` · workspace cap ${int(B.per_day)} a day` : ' · no workspace cap');
  const bioWhen = B.left === 0 ? 'Nothing waiting' : offline || (B.per_minute === 0
    ? 'No bios read this minute' : eta(B.eta_h) ? eta(B.eta_h) + ' left' : 'measuring speed…');
  const listLine = `${int(recv)} list entries saved this attempt · ${L.estimate ? 'about ' : ''}${int(L.left)} left in active lists`
    + (incompleteLists ? ` · ${int(incompleteLists)} incomplete ${incompleteLists === 1 ? 'list' : 'lists'}` : '')
    + (incompleteLeft ? ` (about ${int(incompleteLeft)} entries missing)` : '')
    + (cappedLists ? ` · ${int(cappedLists)} capped ${cappedLists === 1 ? 'list' : 'lists'}` : '')
    + ` · ${minuteRate(L.per_minute, 'rows returned')}`;
  $('#stages').innerHTML = [
    stage('1. Collect lists', listLine,
      listsDone ? 100 : incompleteLists ? null : tot ? Math.min(99, (recv / tot) * 100) : 0, listWhen),
    stage('2. Read bios', bioLine, B.left === 0 ? 100 : 0, bioWhen),
    stage('Local AI', esc(localProcessingSummary()), null, backgroundAIState()),
    stage('External AI', sc.qualify ? `${int(Q.left || 0)} people waiting · ${minuteRate(Q.per_minute, 'reviews')}` : 'No profiles are being sent to external AI.',
      null, sc.qualify ? Q.left ? 'On' : 'Up to date' : 'Off'),
  ].join('');
  $('#ext-ver').textContent = x.version ? 'Extension v' + x.version : '';
  const tl = x.today?.list, bl = x.budget?.list, tp = x.today?.profile, bp = x.budget?.profile;
  $('#ext-kv').innerHTML = [
    ['Today', `${int(tl)} list pages${bl ? ` of ${int(bl)}` : ' (no cap)'}, ${int(tp)} bios${bp ? ` of ${int(bp)}` : ' (no cap)'}`],
    ['Status', x.last_error ? collectionReason(x.last_error, 'Check the connected account') : 'No errors'],
  ].map(([k, v]) => `<span>${k}</span><b>${esc(v)}</b>`).join('');
  const groups = { all: ls, active: ls.filter((l) => ['waiting', 'collecting'].includes(l.completion) || !l.completion && ['running', 'queued'].includes(l.state)), done: ls.filter((l) => l.completion === 'complete'), issues: ls.filter((l) => ['partial', 'unverified', 'blocked'].includes(l.completion) || !l.completion && ['error', 'private', 'paused', 'partial'].includes(l.state)) };
  const focusedListFilter = $('#lists-f').contains(document.activeElement) ? document.activeElement.dataset.v : null;
  $('#lists-f').innerHTML = Object.entries(groups).map(([k, v]) => `<button data-v="${k}" aria-pressed="${listFilter === k}" class="${listFilter === k ? 'on' : ''}">${({all:'All',active:'In queue',done:'Complete',issues:'Incomplete'})[k]} <span class="num">${v.length}</span></button>`).join('');
  if (focusedListFilter) $(`#lists-f [data-v="${focusedListFilter}"]`)?.focus({ preventScroll: true });
  const order = { running: 0, queued: 1, partial: 2, paused: 2, error: 3, private: 4, done: 5 };
  const all = [...groups[listFilter]].sort((a, b) => (order[a.state] ?? 9) - (order[b.state] ?? 9) || (b.updated_at || '').localeCompare(a.updated_at || ''));
  const rows = all.slice(0, listsShown);
  const empty = ls.length ? { active: 'No active lists.', done: 'No completed lists.', issues: 'No issues.' }[listFilter] : 'No lists yet. Add an account above.';
  $('#lists-body').innerHTML = rows.length ? rows.map((l) => {
    const saved = l.saved_entries ?? l.received;
    const expected = l.expected ?? l.total;
    const complete = l.completion === 'complete';
    const pct = expected ? Math.min(complete ? 100 : 99, (saved / expected) * 100) : null;
    const label = complete ? 'Complete' : l.completion === 'unverified' ? 'Needs review' : l.state === 'running' && !listActive ? listPaused ? 'Paused' : 'Waiting' : l.state === 'queued' && saved > 0 ? 'Partial · queued' : l.state === 'queued' && (l.error || l.completion_reason || l.pages > 0) ? 'Waiting to retry' : l.state === 'queued' ? 'Not started' : LIST_STATE[l.state] || ucf(l.state);
    const reason = l.completion_reason || l.error ? collectionReason(l.reason_code || l.error || l.completion_reason, 'Saved progress needs another attempt') : '';
    return `<tr><td><b>@${esc(l.seed)}</b><small class="list-direction-mobile">${l.direction === 'followers' ? 'Followers' : 'Following'}</small>${reason ? `<small class="list-reason">${esc(reason)}</small>` : ''}</td><td class="hide-sm muted">${l.direction === 'followers' ? 'Their followers' : 'Who they follow'}</td>
      <td class="prog"><div class="bar-p ${pct == null ? 'unknown' : complete ? 'done' : l.state === 'running' && listActive ? 'run' : ''}"><i style="width:${pct ?? 0}%"></i></div></td>
      <td class="r num">${int(saved)}${expected != null ? ' / ' + (l.expected_source === 'estimate' ? '~' : '') + int(expected) : ' / ?'}</td>
      <td><span class="state ${esc(complete ? 'done' : l.state === 'done' ? 'partial' : l.state)}">${l.state === 'running' && listActive ? '<i class="dot run"></i>' : ''}${esc(label)}</span></td></tr>`;
  }).join('') + (all.length > rows.length ? `<tr><td colspan="5"><button class="btn ghost" id="lists-all">Show 10 more · ${int(all.length - rows.length)} remaining</button></td></tr>` : '')
    : `<tr><td colspan="5" class="muted">${empty}</td></tr>`;
}
let listsShown = 10;
$('#now').addEventListener('click', (e) => { if (e.target.closest('#scr-retry')) loadScraper(); });
$('#lists-body').addEventListener('click', (e) => { if (e.target.closest('#lists-all')) { listsShown += 10; renderScraper(); } });
function backIn(t) {
  const m = Math.ceil(Math.max(0, Date.parse(t) - Date.now()) / 60000);
  return m <= 1 ? 'in about a minute' : m < 90 ? `in ${m} min` : `in ${Math.floor(m / 60)} h ${m % 60} min`;
}
$('#scr-add').onclick = () => { $('#seed-panel').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'center' }); $('#seed-in').focus({ preventScroll: true }); };
$('#lists-f').addEventListener('click', (e) => { const b = e.target.closest('[data-v]'); if (b) { listFilter = b.dataset.v; listsShown = 10; renderScraper(); } });
$('#budget').addEventListener('submit', async (e) => {
  e.preventDefault();
  const val = (el) => { const v = el.value.trim(); return /^\d+$/.test(v) ? +v : null; };
  const list = val($('#b-list')), profile = val($('#b-profile'));
  if (list == null || profile == null) { toast('Enter whole numbers, 0 or more'); return; }
  try { await api.post('/api/scraper/budget', { list, profile }); toast('Budget saved'); $('#b-save').blur(); loadScraper(); } catch (err) { toast("Couldn't save. Try again."); }
});
$('#snowball').onclick = async () => {
  try {
    const r = await api.post('/api/scraper/snowball', { min_status: 'interested' });
    toast(r.queued ? `Queued the following lists of ${plural(r.queued, 'lead')}` : 'Nothing new. Every Interested, Talking or Client lead is already queued, done or private.');
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
let seedAdding = false;
let seedAction = '';
const seedFollowingOnly = () => S.sc?.control?.collection_scope === 'following';
const seedDirs = () => $$('#seed-dir button.on').map((b) => b.dataset.v).filter(direction => !seedFollowingOnly() || direction === 'following');
const seedCollectionRunning = () => !!S.sc && !S.sc.paused && S.sc.stages?.find(stage => stage.id === 'lists')?.paused === false;
function syncSeed() {
  const n = parseHandles($('#seed-in').value).length;
  const directions = seedDirs();
  $('#seed-n').textContent = n ? `${plural(n, 'profile')} · ${plural(n * directions.length, 'list')}` : '';
  const unavailable = seedAdding || !n || !directions.length || !!S.scStale;
  $('#seed-start').disabled = unavailable;
  $('#seed-in').disabled = seedAdding;
  $('#seed-start').textContent = seedAdding ? seedAction === 'start' ? 'Starting…' : 'Adding…' : seedCollectionRunning() ? 'Add profiles' : 'Start collecting';
  $$('#seed-dir button').forEach(b => {
    const excluded = seedFollowingOnly() && b.dataset.v === 'followers';
    b.disabled = seedAdding || excluded;
    b.setAttribute('aria-pressed', String(!excluded && b.classList.contains('on')));
    b.title = excluded ? 'Following only is selected above' : '';
  });
}
$('#seed-in').addEventListener('input', syncSeed);
$('#seed-dir').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b && !b.disabled) { b.classList.toggle('on'); syncSeed(); } });
async function submitSeed(start = true) {
  const handles = parseHandles($('#seed-in').value), directions = seedDirs();
  if (seedAdding || S.scStale || !handles.length || !directions.length) return;
  seedAdding = true; seedAction = start ? 'start' : 'queue'; syncSeed();
  try {
    const r = await api.post(start ? '/api/start' : '/api/scraper/seeds', { handles, directions });
    if (start && r.started !== true && r.starting !== true) {
      $('#seed-feedback').textContent = "Profiles are queued. Couldn't confirm collection started. Check status and try again.";
      loadScraper(); return;
    }
    const message = start ? r.starting === true ? 'Profiles queued. Connecting your saved accounts…' : 'Collection is on. Check the status above for activity.'
      : r.queued ? `${ucf(plural(r.queued, 'list'))} added to the queue.` : 'Those lists are already queued.';
    $('#seed-feedback').textContent = message;
    toast(message);
    window.dispatchEvent(new Event('fl:control-changed'));
    $('#seed-in').value = ''; syncSeed(); loadScraper();
  } catch (e) { $('#seed-feedback').textContent = start ? "Couldn't start collection. Your input is kept. Try again." : "Couldn't add these lists. Your input is kept. Try again."; }
  finally { seedAdding = false; seedAction = ''; syncSeed(); }
}
$('#seed-start').onclick = () => submitSeed(!seedCollectionRunning());
window.addEventListener('fl:control-changed', () => loadScraper());

// ---------- accounts (one Chrome profile + extension + Instagram account each) ----------
const ST_LABEL = { connection_error: 'Connection trouble', running: 'Running', online: 'Online', cooldown: 'Cooldown', needs_login: 'Needs login', challenge: 'Security check', offline: 'Offline', paused: 'Paused' };
const ST_DOT = { connection_error: 'hollow', running: 'live run', online: 'live', cooldown: 'hollow', needs_login: 'need', challenge: 'need', offline: 'off', paused: '' };
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
function collectionReason(value, fallback = 'Scraping paused') {
  const reason = String(value || '').toLowerCase();
  if (/login|logged.out|authentication|session.expired/.test(reason)) return 'Sign in to Instagram again';
  if (/challenge|checkpoint|security/.test(reason)) return 'Complete the Instagram security check';
  if (/private|access.denied|forbidden/.test(reason)) return 'This account cannot access the list';
  if (/429|rate.limit|slow.down|feedback|required.wait|cooldown|instagram.wait|instagram.requested.a.pause|instagram.warning|instagram.collection.is.resting/.test(reason)) return 'Waiting for Instagram to allow requests';
  if (/html|redirect|unexpected.response|home.page|502|503|504/.test(reason)) return 'Instagram did not return the list. Progress is saved.';
  if (/unconfirmed|permit|in.flight|shared|workspace/.test(reason)) return 'Waiting for the current request to finish';
  if (/budget|daily|cap.reached/.test(reason)) return 'Daily allowance reached';
  if (/partial|incomplete|limited|short.list|missing/.test(reason)) return 'Part of this list is saved';
  if (/offline|extension/.test(reason)) return 'Open the connected Chrome profile';
  return fallback;
}
function accountAccess(a) {
  if (a.hold === 'login' || a.status === 'needs_login') return { label: 'Login needed', detail: 'Open this Chrome profile and sign in.', kind: 'bad' };
  if (a.hold || a.status === 'challenge') return { label: 'Security check', detail: 'Complete the check in this Chrome profile.', kind: 'bad' };
  if (a.collection_protected || a.is_main) return {label:'Personal use only', detail:'Automated collection is blocked for this account.', kind:'quiet'};
  if (!a.online || a.status === 'offline') return { label: 'Offline', detail: `Last seen ${ago(a.last_seen)} ago`, kind: 'quiet' };
  if (a.status === 'connection_error') return {label:'Connection trouble', detail:'Check Instagram in this Chrome profile. Retries keep your saved progress.', kind:'wait'};
  const instagramWait = a.cooldown_until && Date.parse(a.cooldown_until) > Date.now();
  if (a.list_endpoint_until && Date.parse(a.list_endpoint_until) > Date.now()) return {
    label: 'Follower lists paused',
    detail: instagramWait
      ? 'Instagram also requested a wait. Following lists and bios resume when that wait ends.'
      : 'Following lists and bios can continue. Followers will retry later.',
    kind: 'wait' };
  if (instagramWait || a.status === 'cooldown') return { label: 'Instagram limit active', detail: a.cooldown_until ? `Resumes ${backIn(a.cooldown_until)}.` : 'Waiting for Instagram to allow requests again.', kind: 'wait' };
  if (a.paused || a.status === 'paused') return { label: 'Paused', detail: 'Ready when resumed.', kind: 'quiet' };
  if (a.collection_reason === 'main_reserved') return {label:'Protected', detail:a.collection_now, kind:'quiet'};
  if (a.collection_state === 'waiting') return {label:'Waiting', detail:a.collection_now, kind:'wait'};
  return { label: 'Connected', detail: `Seen ${ago(a.last_seen)} ago`, kind: 'ok' };
}
function accountInstagramHTML(a) {
  if (!a.is_main) return '';
  return location.port === '8777' && window.chrome?.runtime?.sendMessage
    ? '<button class="btn" data-instagram="inbox">Instagram Inbox</button>'
    : '<a class="btn" href="https://www.instagram.com/direct/inbox/" target="_blank" rel="noopener" title="Opens Instagram; check the signed-in account">Instagram Inbox</a>';
}
async function openInstagramAccount(lane, destination) {
  try {
    const setup = await api.get('/api/setup');
    const instagram = setup.instagram;
    if (!setup.extension_id || !instagram?.connected || instagram.lane_id !== lane || !instagram.ig_id) {
      toast('Open the connected Chrome profile and sign in to Instagram.'); return;
    }
    const reply = await new Promise((resolve, reject) => {
      const deadline = setTimeout(() => reject(Error('Unavailable')), 5000);
      window.chrome.runtime.sendMessage(setup.extension_id, {type:'OPEN_INSTAGRAM',destination,expected_lane_id:lane,expected_account_id:instagram.ig_id}, result => {
        clearTimeout(deadline);
        if (window.chrome.runtime.lastError || !result?.ok || !result.opened || !result.account_verified) reject(Error('Unconfirmed'));
        else resolve(result);
      });
    });
    if (reply.opened) toast('Instagram opened in your connected profile.');
  } catch { toast("Couldn't open the connected profile. Open Instagram in that Chrome profile."); }
}
function accountRow(a) {
  const b = a.budget || {}, t = a.today || {}, h = a.hour || {};
  const conf = A.confirm === a.lane_id, access = accountAccess(a);
  const protectedAccount = a.collection_protected || a.is_main;
  const budget = `${b.list ? `${int(b.list)} list requests/day` : 'No cap'} · ${b.profile ? `${int(b.profile)} profile requests/day` : 'No cap'}`;
  const work = protectedAccount ? access.detail : a.collection_now || (a.paused ? 'Work paused' : a.collection_wait ? collectionReason(a.collection_wait, 'Scraping paused') : a.status === 'running'
    ? a.job ? jobText(a) : 'Waiting for a profile'
    : access.kind === 'ok' ? 'Ready' : access.detail);
  const name = A.renaming === a.lane_id
    ? `<form class="acc-ren" data-ren><input class="input" id="acc-label" value="${esc(A.renameValue ?? a.label ?? '')}" placeholder="Label, e.g. Scout 2" maxlength="40" autocomplete="off"><button class="btn solid">Save</button><button type="button" class="btn" data-ren-x>Cancel</button></form>`
    : `<div class="acc-identity"><b class="acc-name">${esc(a.handle ? '@' + a.handle : a.label || a.name)}</b>${a.handle && a.label ? `<span class="muted acc-label">${esc(a.label)}</span>` : ''}</div>`;
  return `<section class="acc${access.kind === 'bad' ? ' warn' : ''}" data-lane="${esc(a.lane_id)}">
    <div class="acc-top"><i class="dot ${a.collection_wait ? 'hollow' : ST_DOT[a.collection_state === 'running' ? 'running' : a.collection_state ? a.online ? 'online' : 'offline' : a.status] || ''}" aria-hidden="true"></i>${name}${a.is_main ? '<span class="pill" title="Excluded from automated collection">Main</span>' : ''}
      <span class="grow"></span>${accountInstagramHTML(a)}${protectedAccount ? '' : `<button class="btn${a.paused ? ' solid' : ''}" data-pause>${a.paused ? 'Resume' : 'Pause'}</button>`}</div>
    <div class="acc-brief"><span class="acc-access ${access.kind}">${esc(a.collection_wait && access.kind === 'ok' ? collectionReason(a.collection_wait, 'Scraping paused') : access.label)}</span>${a.collection_wait && access.kind === 'ok' ? '' : `<span class="muted">${esc(work)}</span>`}</div>
    <div class="acc-glance"><span class="acc-key">Today</span><b class="num">${int(t.list)} list requests · ${int(t.profile)} profile requests</b>${b.list ? `<div class="bar-p run" role="img" aria-label="${int(t.list)} of ${int(b.list)} workspace list requests used"><i style="width:${Math.min(100, (t.list || 0) / b.list * 100)}%"></i></div>` : ''}${access.kind === 'bad' ? '<span class="acc-need">Needs you</span>' : ''}</div>
    ${protectedAccount ? '' : `<div class="acc-mode"><span class="acc-key">Collect</span><div class="seg" aria-label="What this account collects">${ROLES.map(([v, l]) => `<button data-role="${v}" aria-pressed="${a.role === v}" class="${a.role === v ? 'on' : ''}">${l}</button>`).join('')}</div></div>`}
    <details class="adv acc-more"><summary>Account settings</summary>
    <div class="acc-usage"><span class="acc-key">Today · workspace caps</span><b class="num">${int(t.list)} list requests · ${int(t.profile)} profile requests</b><small>${esc(budget)}</small></div>
    <p class="muted acc-telemetry">${int(h.people)} list entries this hour${a.last_limit ? ` · Instagram last slowed this profile ${ago(a.last_limit)} ago` : ''}</p>
    <div class="acc-ctl">

      <button class="toggle${a.is_main ? ' on' : ''}" data-main aria-pressed="${!!a.is_main}" title="Main accounts are excluded from automated collection"><i></i><span>Main account</span></button>
      <span class="grow"></span>
      ${protectedAccount ? '' : `<form class="acc-bud" data-bud>
        <label><input class="input" type="number" min="0" max="3000" data-b="list" value="${a.budget_custom ? esc(b.list) : ''}" placeholder="${esc(b.list)}" inputmode="numeric" title="0 = no workspace daily cap"><span class="muted">list pages/day</span></label>
        <label><input class="input" type="number" min="0" max="5000" data-b="profile" value="${a.budget_custom ? esc(b.profile) : ''}" placeholder="${esc(b.profile)}" inputmode="numeric" title="0 = no workspace daily cap"><span class="muted">bios/day</span></label>
        <button class="btn">Save</button>
      </form>`}
    </div>
    <div class="acc-foot"><button class="btn ghost acc-edit" data-rename>Rename</button><span class="muted num">${a.version ? 'Extension v' + esc(a.version) + ' · ' : ''}Seen ${ago(a.last_seen)} ago</span>
      <span class="grow"></span>
      <button class="btn ${conf ? 'danger' : 'ghost'}" data-remove>${conf ? 'Remove account?' : 'Remove'}</button></div>
    </details>
  </section>`;
}

// Suggestions use saved evidence; discovery adds one target only when the queue is empty.
const collectionSuggestions = {items:[], loadedAt:0, loading:false, busy:new Set(), added:new Set(), error:'', enabled:null, history:[]};
function collectionSuggestionsHTML(items, busy = new Set()) {
  return items.slice(0,3).map(item => {
    const directions = (item.directions || []).filter(d => ['followers','following'].includes(d));
    if (!/^[a-z0-9._]{1,30}$/i.test(item.handle || '') || !directions.length) return '';
    const what = directions.length === 2 ? 'Both lists' : directions[0] === 'followers' ? 'Followers' : 'Following';
    const reason = item.reason || 'Matches your saved leads';
    const labels = {'Marked as your client':'Client','You are already talking':'Talking','Marked as interested':'Interested','Personal connection recorded by you':'Recorded connection','Good fit recorded by you':'Saved fit','Strong saved business fit':'Strong fit','Good saved business fit':'Good fit'};
    const why = labels[reason.split(' · ')[0]] || 'Saved match';
    const followers = Number.isFinite(item.followers) ? ` · ${item.followers.toLocaleString('en-US',{notation:'compact',maximumFractionDigits:1})} followers` : '';
    const sources = Number.isFinite(item.observed_sources) && item.observed_sources > 0 ? ` · ${item.observed_sources} source accounts` : '';
    return `<div class="collection-suggestion"><div><b>@${esc(item.handle)}</b><details class="suggestion-evidence"><summary>${esc(what)} · ${esc(why)}${esc(followers + sources)}</summary><p>${esc(reason)}</p></details></div><button class="btn" data-suggested-handle="${esc(item.handle)}" aria-label="Add @${esc(item.handle)} to scraping"${busy.has(item.handle) ? ' disabled' : ''}>${busy.has(item.handle) ? 'Adding…' : 'Add'}</button><button class="btn ghost" data-discovery-hide="${esc(item.handle)}" aria-label="Skip @${esc(item.handle)}"${busy.has(item.handle) ? ' disabled' : ''}>Skip</button></div>`;
  }).join('');
}
function renderCollectionSuggestions() {
  const box = $('#collection-suggestions');
  if (!box) return;
  const state = collectionSuggestions;
  const open = box.querySelector('.suggested-accounts')?.open || false;
  const focused = box.contains(document.activeElement) ? document.activeElement : null;
  const focusHandle = focused?.dataset.suggestedHandle || focused?.dataset.discoveryHide;
  const focusAction = focused?.matches('.suggested-accounts > summary') ? 'disclosure' : focused?.hasAttribute('data-discovery-toggle') ? 'toggle' : focused?.dataset.suggestedHandle ? 'add' : focused?.dataset.discoveryHide ? 'skip' : null;
  box.innerHTML = `<details class="suggested-accounts" ${open ? 'open' : ''}><summary>Suggested accounts</summary><div class="collection-suggestion-heading"><button class="btn" data-discovery-toggle aria-pressed="${!!state.enabled}" ${state.enabled === null || state.saving ? 'disabled' : ''}>${state.saving ? 'Saving…' : `Auto-discover ${state.enabled === null ? '…' : state.enabled ? 'on' : 'off'}`}</button></div>${state.items.length ? `<div class="collection-suggestions-grid">${collectionSuggestionsHTML(state.items,state.busy)}</div>` : `<p class="muted">${state.loading ? 'Checking saved profiles…' : 'No suggestions yet.'}</p>`}</details>${state.error ? `<p class="muted" role="status">${esc(state.error)}</p>` : ''}`;
  if (focusAction) {
    const selector = focusAction === 'disclosure' ? '.suggested-accounts > summary' : focusAction === 'toggle' ? '[data-discovery-toggle]' : focusAction === 'add' ? `[data-suggested-handle="${focusHandle}"]` : `[data-discovery-hide="${focusHandle}"]`;
    const action = box.querySelector(selector);
    if (action && !action.disabled) action.focus({preventScroll:true});
    else box.querySelector('.suggested-accounts > summary')?.focus({preventScroll:true});
  }
}
async function loadCollectionSuggestions(force = false) {
  const state = collectionSuggestions;
  if (state.loading || (!force && Date.now() - state.loadedAt < 60000)) return;
  state.loading = true;
  try {
    const result = await api.get('/api/scraper/suggestions?limit=3');
    state.enabled = !!result.auto_discover;
    state.history = result.history || [];
    state.items = Array.isArray(result.suggestions) ? result.suggestions.filter(item => !state.added.has(item.handle)).slice(0,3) : [];
    state.error = '';
  } catch { state.error = 'Suggestions are unavailable. You can still add a profile above.'; }
  finally { state.loading = false; state.loadedAt = Date.now(); renderCollectionSuggestions(); }
}
async function updateDiscovery(change) {
  const state = collectionSuggestions;
  if (state.saving) return;
  state.saving = true; renderCollectionSuggestions();
  try { await api.post('/api/scraper/suggestions', change); await loadCollectionSuggestions(true); }
  catch { state.error = 'Could not save discovery settings. Try again.'; }
  finally { state.saving = false; renderCollectionSuggestions(); }
}
async function addSuggestedTarget(handle) {
  const state = collectionSuggestions, item = state.items.find(row => row.handle === handle);
  if (!item || state.busy.has(handle)) return;
  const directions = (item.directions || []).filter(d => ['followers','following'].includes(d));
  if (!directions.length) return;
  state.busy.add(handle); state.error = ''; renderCollectionSuggestions();
  try {
    const result = await api.post('/api/scraper/seeds', {handles:[handle], directions});
    state.added.add(handle);
    state.items = state.items.filter(row => row.handle !== handle);
    toast(result.queued === 0 ? 'Already queued' : `@${handle} added to the collection queue`);
    loadScraper();
    await loadCollectionSuggestions(true);
  } catch { state.error = `Couldn't add @${handle}. Try again.`; }
  finally { state.busy.delete(handle); renderCollectionSuggestions(); }
}

// Move the existing source form and list table between views. Event handlers and
// input state stay on the same nodes, and both views use the same queue API.
let collectionTargetHomes;
function mountCollectionTargets() {
  const form = $('#seed-panel'), table = $('#lists-body')?.closest('.panel');
  if (!form || !table) return;
  if (!collectionTargetHomes) {
    $('#seed-in').placeholder = '@handle or instagram.com/handle, one per line';
    $('#seed-in').setAttribute('aria-label', 'Target profile handles or Instagram links');
    collectionTargetHomes = [form, table].map(node => {
      const home = document.createComment('collection targets');
      node.before(home);
      return {node, home};
    });
  }
  let workspace = $('#acc-targets');
  if (!workspace) {
    workspace = document.createElement('section');
    workspace.id = 'acc-targets';
    workspace.className = 'collection-targets';
    workspace.setAttribute('aria-label', 'Scrape profiles');
    workspace.innerHTML = '<header class="collection-target-heading"><h2>Collect from profiles</h2></header>';
    $('#acc-coverage').after(workspace);
    const suggestions = document.createElement('section');
    suggestions.id = 'collection-suggestions';
    suggestions.setAttribute('aria-label', 'Suggested target profiles');
    workspace.append(suggestions);
    suggestions.addEventListener('click', e => {
      const toggle = e.target.closest('[data-discovery-toggle]'), hide = e.target.closest('[data-discovery-hide]');
      if (toggle && !toggle.disabled) { updateDiscovery({enabled: !collectionSuggestions.enabled}); return; }
      if (hide) { updateDiscovery({hide: hide.dataset.discoveryHide}); return; }
      const button = e.target.closest('button[data-suggested-handle]');
      if (button && !button.disabled) addSuggestedTarget(button.dataset.suggestedHandle);
    });
    renderCollectionSuggestions();
  }
  if (S.view === 'accounts') {
    for (const {node} of collectionTargetHomes) if (node.parentNode !== workspace) workspace.append(node);
    const suggestions = $('#collection-suggestions');
    if (suggestions.previousSibling !== table) table.after(suggestions);
    loadCollectionSuggestions();
  } else if (S.view === 'scraper') {
    for (const {node, home} of collectionTargetHomes) if (node.previousSibling !== home) home.after(node);
  }
}

function collectionCoverageHTML(sc) {
  if (!sc) return '';
  const lists = sc.coverage?.lists, bios = sc.progress?.bios, queue = sc.collection;
  const stages = sc.stages || sc.control?.stages || [];
  const held = stages.find(stage => stage.wait?.scope === 'workspace') || stages.find(stage => stage.state === 'waiting' && stage.wait);
  const collectionStages = stages.filter(stage => ['lists','bios'].includes(stage.id));
  const activity = sc.control?.collection || {
    stopping: collectionStages.some(stage => stage.state === 'stopping' || stage.paused && stage.active),
    stop_acknowledged: collectionStages.length === 2 && collectionStages.every(stage => stage.stop_acknowledged === true)
      ? true : collectionStages.some(stage => stage.stop_acknowledged === false) ? false : undefined,
  };
  const pausedByUser = sc.paused || collectionStages.length === 2 && collectionStages.every(stage => stage.paused);
  const state = activity?.stopping ? 'Stopping. Waiting for the current request to finish'
    : pausedByUser ? activity?.stop_acknowledged === true ? 'Stopped' : activity?.stop_acknowledged === false ? 'Checking the last request before stopping' : 'Paused by you'
    : held ? held.now || collectionReason(held.reason_code || held.wait?.why, 'Collection is waiting')
    : collectionStages.some(stage => stage.state === 'running') ? 'Scraping' : 'Waiting';
  const until = held?.wait?.until;
  const resumes = !pausedByUser && until && Number.isFinite(Date.parse(until)) && Date.parse(until) > Date.now()
    ? ` · Retries automatically at ${new Date(until).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}. Progress is saved.` : '';
  const estimate = queue?.eta && !pausedByUser && !held ? (() => {
    const minutes = value => value < 60 ? `${Math.ceil(value)} min` : value < 2880 ? `${Math.ceil(value / 60)} h` : `${Math.ceil(value / 1440)} days`;
    return `${queue.eta.scope === 'known_lists' ? 'Known lists' : 'Current queue'}: about ${minutes(queue.eta.low_minutes)}–${minutes(queue.eta.high_minutes)}`;
  })() : '';
  if (queue) {
    const metrics = sc.control?.progress;
    const saved = metrics?.unique_new_profiles?.today == null ? '' : `<span title="Counts since midnight UTC"><b>${int(metrics.unique_new_profiles.today)} new profiles today</b> · ${int(metrics.bios_read?.today || 0)} bios read · ${int(metrics.saved_list_entries?.today || 0)} list entries saved</span>`;
    const parts = [`${int(queue.finished)} finished`, `${int(queue.pending)} queued`];
    if (queue.limited) parts.push(`${int(queue.limited)} limited`);
    if (queue.partial > (queue.limited || 0)) parts.push(`${int(queue.partial - (queue.limited || 0))} partial`);
    if (queue.needs_review) parts.push(`${int(queue.needs_review)} need review`);
    const timing = pausedByUser || held || activity.stopping ? state + resumes : estimate || queue.message;
    const unknown = queue.unknown_lists ? ` · ${int(queue.unknown_lists)} list sizes unknown` : '';
    return `<div class="collection-summary">${saved}<span><b>${parts.map(esc).join(' · ')}</b></span><span>${esc(timing)}${esc(unknown)}</span>${queue.open_ended ? '<span class="muted">Auto-discovery on</span>' : ''}<span>${bios?.left != null ? `${int(bios.left)} bios queued` : 'Counting bios'}</span></div>`;
  }
  const saved = lists?.saved_entries == null ? 'Counting saved entries' : `${int(lists.saved_entries)} list entries saved`;
  const bioQueue = bios?.left == null ? 'Counting unread bios' : `${int(bios.left)} bios waiting${bios.failed ? ` · ${int(bios.failed)} need retry` : ''}`;
  const speed = bios?.per_minute == null ? '' : ` · ${int(bios.per_minute)} bios read this minute`;
  return `<div class="collection-summary"><span><b>${esc(saved)}</b>${lists?.complete_lists != null ? ` · ${int(lists.complete_lists)} complete lists` : ''}</span><span>${esc(bioQueue)}${speed}</span><span class="muted">${esc(state)}${esc(resumes)}${esc(estimate)}</span><span>${esc(localProcessingSummary())}</span>${backgroundAIControlsHTML()}</div>`;
}

function renderAccounts() {
  renderScraper();
  syncSeed();
  const sc = S.sc;
  const accs = sc?.accounts || [];
  const coverage = $('#acc-coverage');
  if (coverage) coverage.innerHTML = collectionCoverageHTML(sc);
  $('#acc-start').disabled = A.starting || !sc || !!S.scStale;
  $('#acc-start').textContent = A.starting ? 'Starting…' : 'Start local models';
  $('#acc-alerts').innerHTML = ''; // Account rows show short, actionable status without raw error payloads.
  const connected = accs.filter((a) => a.online).length;
  const attention = accs.filter((a) => accountAccess(a).kind === 'bad').length;
  $('#acc-summary').textContent = accs.length
    ? `${connected} connected${attention ? ` · ${attention} need attention` : ` · ${accs.length} accounts`}`
    : 'Connect an Instagram account to begin';
  $('#acc-n').textContent = accs.length ? int(accs.length) : '';
  if (!accs.length && !A.wiz && !A.dismissed && sc) openWizard();
  const list = $('#acc-list');
  if (list.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return; // don't clobber typing
  const openLanes = new Set([...list.querySelectorAll('[data-lane]')]
    .filter((row) => row.querySelector('.acc-more')?.open).map((row) => row.dataset.lane));
  const focused = list.contains(document.activeElement) ? document.activeElement : null;
  const focusRow = focused?.closest('[data-lane]');
  const focusIndex = focused?.matches('button, summary') && focusRow
    ? [...focusRow.querySelectorAll('button, summary')].indexOf(focused) : -1;
  const focusLane = focusIndex >= 0 ? focusRow.dataset.lane : null;
  const collectionStages = (sc?.stages || sc?.control?.stages || []).filter(s => s.id === 'lists' || s.id === 'bios');
  const sharedWait = collectionStages.find(s => s.wait?.scope === 'workspace');
  const collectionWait = (sharedWait ? sharedWait.reason_code || sharedWait.wait?.why || 'workspace' : null)
    || (collectionStages.length === 2 && collectionStages.every(s => s.paused) ? 'Scraping paused' : null);
  list.innerHTML = accs.length ? accs.map(a => { const status = sc.control?.accounts?.find(row => row.lane_id === a.lane_id); return accountRow({...a, collection_wait: collectionWait, collection_state: status?.state, collection_reason: status?.reason_code, collection_now: status?.now}); }).join('') : `<div class="acc-empty muted">${A.wiz ? 'Follow the steps above; the account shows up here once its extension checks in.' : 'Connect an account to start collecting people.'}</div>`;
  for (const row of list.querySelectorAll('[data-lane]')) {
    if (openLanes.has(row.dataset.lane)) row.querySelector('.acc-more').open = true;
    if (row.dataset.lane === focusLane) row.querySelectorAll('button, summary')[focusIndex]?.focus({ preventScroll: true });
  }
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
  if (t.dataset.instagram) return openInstagramAccount(lane, t.dataset.instagram);
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
    try { await api.post(`/api/accounts/${encodeURIComponent(lane)}/remove`, {}); toast(`${a.name} removed`); } catch (err) { toast("Couldn't remove. Try again."); }
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
    if (Number.isNaN(list) || Number.isNaN(profile)) { toast('Enter whole numbers, 0 or more'); return; }
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
  const status = $('#acc-start-status');
  status.dataset.state = 'working';
  status.textContent = 'Starting…';
  renderAccounts();
  try {
    const result = await api.post('/api/engine/start', {});
    if (result?.ok === false) throw new Error(result.error || 'Could not start');
    window.dispatchEvent(new Event('fl:control-changed'));
    status.dataset.state = 'success';
    status.textContent = 'Local models ready.';
    toast('Local models started');
    await loadScraper();
  } catch (e) {
    status.dataset.state = 'error';
    status.textContent = 'Could not start local models. Check setup in Settings.';
    toast("Couldn't start local models. Try again.");
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
$('#acc-manage')?.addEventListener('click', () => {
  const accounts = $('#acc-list').closest('.panel');
  accounts.scrollIntoView({block:'start', behavior:matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
  accounts.querySelector('button')?.focus({preventScroll:true});
});
$('#acc-add').onclick = () => openWizard();
$('#wiz').addEventListener('click', (e) => {
  const c = e.target.closest('[data-copy]');
  if (c) return copyText(c.dataset.copy, c);
  if (e.target.closest('#wiz-x')) closeWizard();
});

// ---------- settings (qualification, OpenRouter keys and models, local services) ----------
const SET = { llm: null, health: null, tests: {}, models: null, confirm: null, dirty: false, modeBusy: false, processing: null, localProcessing: null, processingStale: true };
async function loadSettings() {
  loadBiofetch();
  await loadProcessingStatus();
  const [llm] = await Promise.allSettled([api.get('/api/llm')]);
  if (llm.status === 'fulfilled') { SET.llm = llm.value; if (!SET.dirty) SET.models = [...SET.llm.models]; }
  renderSettings();
  if (!SET.health) checkHealth();
  loadScout();
}
const PROCESSING_LABELS = { R: 'Rules only', RLAI: 'Local AI', RLEAI: 'External AI' };
async function loadProcessingStatus() {
  if (SET.processingLoading || SET.modeBusy || SET.localBusy) return;
  SET.processingLoading = true;
  const localVersion = SET.localStatusVersion || 0;
  try {
    const [mode, local] = await Promise.allSettled([api.get('/api/processing-mode'), api.get('/api/local-processing')]);
    if (mode.status === 'fulfilled' && Object.hasOwn(PROCESSING_LABELS, mode.value.mode)) { if (!SET.processing || (mode.value.generation ?? 0) >= (SET.processing.generation ?? 0)) SET.processing = mode.value; SET.processingStale = false; }
    else SET.processingStale = true;
    if (localVersion === (SET.localStatusVersion || 0)) SET.localProcessing = local.status === 'fulfilled' ? local.value : null;
  } finally { SET.processingLoading = false; }
}
function settingsMode(sc) {
  if (S.scStale || S.scError || SET.processingStale) return null;
  const processing = SET.processing && (SET.processing.generation ?? 0) >= (sc?.processing?.generation ?? 0) ? SET.processing : sc?.processing;
  return ({R:'rules',RLAI:'local',RLEAI:'external'})[processing?.mode] || null;
}
function backgroundAIState(local = SET.localProcessing) {
  if (!local) return 'Status unavailable';
  if (!local.enabled) return 'Off';
  if (local.paused) return local.stop_acknowledged === true ? 'Paused' : 'Stopping';
  const resources = local.runtime?.resources;
  if (resources?.busy) return 'Running';
  if (local.state === 'waiting_for_mac' || resources?.allowed === false && (resources.recovering || resources.thermal_limited || resources.error)) return 'Waiting for Mac';
  if (!local.ready) return local.state === 'starting' ? 'Starting' : 'Unavailable';
  if (local.state === 'waiting' || resources?.retry_in > 0) return 'Waiting';
  return local.state === 'working' ? 'Running' : 'Ready';
}
function backgroundAIControlsHTML() {
  const local = SET.localProcessing;
  if (!local?.enabled) return '';
  const stopping = local.paused && local.stop_acknowledged !== true;
  return `<span class="background-ai-inline"><span>Background AI · ${esc(SET.localBusy ? 'Saving…' : backgroundAIState(local))}</span><button class="btn" type="button" data-local-ai-toggle ${SET.localBusy || SET.modeBusy || stopping ? 'disabled' : ''}>${stopping ? 'Stopping…' : local.paused ? 'Resume' : 'Pause'}</button></span>`;
}
function localProcessingSummary() {
  const local = SET.localProcessing;
  if (!local) return 'Local AI status unavailable';
  if (!local.enabled) return 'Local AI off';
  const counts = `${int(local.reviewed || 0)} bios reviewed · ${int(local.queue || 0)} waiting${local.seeding ? ' · finding more saved bios' : ''}`;
  const notes = (local.notes_pending ? ` · ${int(local.notes_pending)} notes waiting` : '') + (local.notes_failed ? ` · ${int(local.notes_failed)} notes need review` : '');
  const unverified = local.unverified ? ` · ${int(local.unverified)} profiles need review` : '';
  const research = local.needs_research ? ` · ${int(local.needs_research)} need more research` : '';
  const state = backgroundAIState(local);
  if (!['Ready','Running'].includes(state)) return `Background AI ${state.toLowerCase()} · ${counts}${notes}${unverified}`;
  return `K2 · ${counts}${notes}${unverified}${research}`;
}
function renderCheckingMode() {
  const mode = settingsMode(S.sc), code = ({rules:'R',local:'RLAI',external:'RLEAI'})[mode];
  const status = $('#set-mode-status');
  if (status) { status.textContent = SET.modeBusy ? 'Saving…' : code ? `${PROCESSING_LABELS[code]} selected` : 'Current mode unavailable. Refresh to try again.'; status.hidden = !!code && !SET.modeBusy; }
  $('#set-mode')?.querySelectorAll('[data-mode]').forEach(button => {
    button.classList.toggle('on', button.dataset.mode === code);
    button.setAttribute('aria-pressed', String(button.dataset.mode === code));
    button.disabled = SET.modeBusy || !code;
  });
  $('#set-q-auto').disabled = SET.modeBusy || mode !== 'external';
  const summary = $('#set-mode-models');
  if (summary) {
    const model = SET.localProcessing?.model || 'K2 3.7B';
    summary.textContent = mode === 'external' ? `${model} · Laya · External review: ${SET.scout?.model || 'choose a model below'}` : mode === 'local' ? `${model} · Laya` : '';
    summary.hidden = !mode || mode === 'rules';
  }
  const progress = $('#set-local-progress');
  if (progress) { progress.textContent = localProcessingSummary(); progress.hidden = mode === 'rules'; }
  const activity = $('#set-local-activity'), metrics = SET.localProcessing?.progress;
  if (activity) {
    activity.hidden = mode === 'rules';
    const eta = metrics?.eta_seconds, minutes = eta ? Math.ceil(eta / 60) : 0;
    const time = minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`;
    const active = metrics?.active?.handle;
    const history = SET.localProcessing?.history || [];
    activity.innerHTML = `<div class="local-work-now"><b>${active ? `Checking @${esc(active)}` : backgroundAIState(SET.localProcessing)}</b><span class="muted">${metrics?.per_minute ? `${metrics.per_minute} checks/min` : 'Measuring pace'}${eta ? ` · about ${time} for this queue` : ''}</span></div>${history.length ? `<details><summary>Recent checks</summary><div class="local-history">${history.map(item => `<button type="button" class="local-history-item" data-reviewed-person="${item.person_id}"><span>@${esc(item.handle || `profile ${item.person_id}`)}</span><span>${esc(({complete:'Checked',needs_research:'Needs more evidence',unverified:'Could not verify',archived:'Earlier result'})[item.status] || 'Updated')}${item.score !== null && item.score !== undefined ? ` · ${item.score}` : ''}</span><time>${esc(new Date(item.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}))}</time></button>`).join('')}</div></details>` : ''}`;
  }
  const background = $('#background-ai'), local = SET.localProcessing;
  if (background) background.hidden = mode === 'rules';
  const state = $('#local-ai-state'), toggle = $('#local-ai-toggle'), help = $('#local-ai-help');
  if (state) state.textContent = SET.localBusy ? 'Saving…' : backgroundAIState(local);
  if (toggle) { const stopping = local?.paused && local.stop_acknowledged !== true; toggle.textContent = stopping ? 'Stopping…' : local?.paused ? 'Resume' : 'Pause'; toggle.disabled = SET.localBusy || SET.modeBusy || !local || !local.enabled || stopping; }
  if (help) { help.textContent = local?.paused && local.stop_acknowledged !== true ? 'Finishing the current check.' : ''; help.hidden = !help.textContent; }
}
async function toggleBackgroundAI() {
  const current = SET.localProcessing;
  if (!current || !current.enabled || SET.localBusy || SET.modeBusy || current.paused && current.stop_acknowledged !== true) return;
  SET.localBusy = true; SET.localStatusVersion = (SET.localStatusVersion || 0) + 1; renderCheckingMode();
  try {
    const result = await api.post('/api/local-processing', {paused: !current.paused});
    if (typeof result.paused !== 'boolean' || result.paused === !!current.paused) throw new Error('Pause state not confirmed');
    SET.localProcessing = result;
    toast(result.paused ? result.stop_acknowledged === true ? 'Background AI paused. Your queue is saved.' : 'Background AI is stopping. Your queue is saved.' : 'Background AI resumed when your Mac is ready.');
    window.dispatchEvent(new Event('fl:control-changed'));
  } catch { SET.localProcessing = null; toast('Could not confirm background AI. Refresh to check.'); }
  finally {
    SET.localBusy = false; renderCheckingMode();
    if (S.view === 'accounts') renderAccounts();
    if (S.view === 'qual') Q.renderProg();
  }
}
$('#local-ai-toggle')?.addEventListener('click', toggleBackgroundAI);
$('#set-local-activity')?.addEventListener('click', event => {
  const button = event.target.closest('[data-reviewed-person]');
  if (button) openDetail(+button.dataset.reviewedPerson);
});
for (const selector of ['#view-accounts', '#ql-prog']) $(selector)?.addEventListener('click', event => {
  if (event.target.closest('[data-local-ai-toggle]')) toggleBackgroundAI();
});
$('#set-mode')?.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-mode]'), mode = button?.dataset.mode;
  if (!Object.hasOwn(PROCESSING_LABELS, mode || '') || button.disabled || SET.modeBusy || mode === SET.processing?.mode) return;
  SET.modeBusy = true; renderCheckingMode();
  try {
    const result = await api.post('/api/processing-mode', {mode});
    if (result.mode !== mode) throw new Error('Mode could not be confirmed');
    SET.processing = result; SET.processingStale = false;
    if (S.sc) S.sc.processing = result;
    window.dispatchEvent(new Event('fl:control-changed'));
    toast('Checking mode saved');
  } catch (error) { SET.processingStale = true; if (S.sc) delete S.sc.processing; toast('Could not confirm the change. Refresh to check the current mode.'); }
  finally {
    SET.modeBusy = false; await loadProcessingStatus();
    await loadScout(); renderSettings();
  }
});
// Leadscout: which model the Hermes agent runs on, how many at once, and what it used this week.
async function loadScout() {
  try { SET.scout = await api.get('/api/scout'); } catch (e) { SET.scout = null; }
  renderScout();
}
function renderScout() {
  const sc = SET.scout, el = $('#set-scout');
  const external = !S.scStale && !S.scError && settingsMode(S.sc) === 'external';
  if (!el) return;
  if (!sc) { el.innerHTML = '<div class="set-row muted">Could not load leadscout status.</div>'; return; }
  const tok = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n);
  const use = sc.usage.length ? sc.usage.map((u) => `<div class="kv-row"><span>${esc(u.model)}<small class="muted"> · ${esc(u.provider || '')}</small></span>
      <span class="num">${int(u.runs)} runs · ${tok(u.tokens_in + u.tokens_out)} tokens</span></div>`).join('') : '<div class="muted">No runs this week.</div>';
  el.innerHTML = `
    <div class="set-row"><div><b>Enable deeper research</b><span class="muted">${sc.available ? `${int(sc.done_today)} checked today · ${int(sc.done)} in total · ${int(sc.waiting)} waiting` : 'Web research is unavailable on this Mac'}</span></div>
      <button class="toggle${external && sc.on ? ' on' : ''}" id="scout-on" role="switch" aria-checked="${external && sc.on}" aria-label="Extra web checks"${external ? '' : ' disabled'}><i></i></button></div>
    <div class="set-row"><div><b>Research model</b><span class="muted">Used for external review and optional deep dives.</span></div>
      <div class="seg" id="scout-model">${sc.models.map((m) => `<button data-m="${esc(m.id)}" class="${sc.model === m.id ? 'on' : ''}" aria-pressed="${sc.model === m.id}" title="${esc(m.label)}">${esc(m.label.split(' (')[0])}</button>`).join('')}</div></div>
    <div class="set-row"><div><b>Research at once</b><span class="muted">Maximum leads being researched together.</span></div>
      <div class="seg" id="scout-workers">${[2, 3, 4, 6, 8].map((n) => `<button data-w="${n}" class="${sc.workers === n ? 'on' : ''}" aria-pressed="${sc.workers === n}">${n}</button>`).join('')}</div></div>
    <div class="sub-h"><b>Used in the last 7 days</b><span class="muted">Recorded research usage. Provider allowance is shown in your provider account.</span></div>
    <div class="kv-list">${use}</div>`;
}
$('#set-scout')?.addEventListener('click', async (e) => {
  const body = e.target.closest('#scout-on') ? { on: !SET.scout?.on }
    : e.target.closest('[data-m]') ? { model: e.target.closest('[data-m]').dataset.m }
    : e.target.closest('[data-w]') ? { workers: +e.target.closest('[data-w]').dataset.w } : null;
  if (!body || (body.on && settingsMode(S.sc) !== 'external')) return;
  try { SET.scout = await api.post('/api/settings/scout', body); renderScout(); renderCheckingMode(); toast('Saved'); } catch (err) { toast("Couldn't save. Try again."); }
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
    + row('Local Laya', h?.laya.url || l?.laya?.url, h ? h.laya.up : null, 'Use Start local services on Accounts to start the local helper.');
}
function keyStatus(p) {
  if (p.disabled) return { text: 'Refused', cls: 'bad' };
  const until = Object.values(p.cooldowns || {}).sort().pop();
  if (until) return { text: 'Cooling until ' + new Date(until).toTimeString().slice(0, 5), cls: 'warn' };
  return { text: 'Ready', cls: '' };
}
function renderSettings() {
  renderCheckingMode();
  const l = SET.llm, sc = S.sc;
  $('#set-q-auto').classList.toggle('on', !!sc?.qualify_auto);
  $('#set-q-auto').setAttribute('aria-checked', String(!!sc?.qualify_auto));
  const f = $('#set-q');
  if (l && !f.contains(document.activeElement)) { $('#set-workers').value = l.workers; $('#set-llm-min').value = l.llm_min; $('#set-bio-min').value = l.bio_min; }
  const bL = $('#b-list'), bP = $('#b-profile'), bud = sc?.ext?.budget || {};
  if (document.activeElement !== bL && document.activeElement !== bP) { bL.value = bud.list ?? ''; bP.value = bud.profile ?? ''; }
  syncLook();
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
      <td class="r"><span class="acts"><button data-ktest="${esc(p.id)}"${t === 'run' || settingsMode(S.sc) !== 'external' || S.scStale ? ' disabled' : ''}>Test</button>${p.source === 'env' ? '' : `<button data-kdel="${esc(p.id)}" class="${conf ? 'warn' : ''}">${conf ? 'Confirm' : 'Remove'}</button>`}</span></td></tr>`;
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
  $('#set-free-at').textContent = auto.at ? `${free.length} models · checked ${ago(auto.at)} ago` : auto.error ? "Couldn't reach OpenRouter" : 'Not checked yet';
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
  catch { toast("Couldn't reach OpenRouter. Check your connection."); }
  finally { e.target.disabled = false; }
};
$('#set-q-auto').onclick = async () => {
  if (settingsMode(S.sc) !== 'external' || S.scStale || S.scError) return;
  const auto = !S.sc?.qualify_auto;
  try { await api.post('/api/settings/qualify', { auto }); toast(auto ? 'Starts by itself after the lists' : 'Starts only by hand'); await loadScraper(); renderSettings(); } catch (e) { toast("Couldn't save. Try again."); }
};
$('#set-q').addEventListener('submit', async (e) => {
  e.preventDefault();
  const n = (id) => { const v = $(id).value.trim(); return /^\d+$/.test(v) ? +v : NaN; };
  const body = { workers: n('#set-workers'), llm_min: n('#set-llm-min'), bio_min: n('#set-bio-min') };
  if (Object.values(body).some(Number.isNaN)) { toast('Enter whole numbers only'); return; }
  try { await api.post('/api/settings/qualify', body); toast('Saved'); document.activeElement?.blur(); loadSettings(); } catch (err) { toast(err.status === 400 ? ucf(err.message) : 'Could not save'); }
});
$('#set-key-f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const key = $('#set-key').value.trim();
  if (!key) return;
  try { await api.post('/api/llm/keys', { key }); $('#set-key').value = ''; toast('Key added'); loadSettings(); }
  catch (err) { toast(err.status === 400 ? ucf(err.message) : "Couldn't add the key. Check it and try again."); }
});
$('#set-limit-f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const v = $('#set-limit').value.trim();
  if (!/^\d+$/.test(v)) { toast('Enter a whole number. 0 means no limit.'); return; }
  try { await api.post('/api/llm/models', { daily_limit: +v }); toast('Daily limit saved'); document.activeElement?.blur(); loadSettings(); }
  catch (err) { toast(err.status === 400 ? ucf(err.message) : 'Could not save'); }
});
$('#set-model-f').addEventListener('submit', (e) => {
  e.preventDefault();
  const m = $('#set-model').value.trim();
  if (!/^[\w.-]+\/[\w.:-]+$/.test(m) || !/:free$|^stealth\//.test(m)) { toast('Choose a free model from the list, or enter vendor/model:free'); return; }
  if (!SET.models) { toast('Settings are still loading. Try again in a moment.'); return; }
  if (!SET.models.includes(m)) { SET.models.push(m); SET.dirty = true; }
  $('#set-model').value = ''; renderModels();
});
$('#set-models-save').onclick = async () => {
  if (!SET.models?.length || !SET.dirty) return;
  const models = [...SET.models];
  $('#set-models-save').disabled = true;
  try {
    const saved = await api.post('/api/llm/models', { models });
    if (!Array.isArray(saved.models) || saved.models.join('\n') !== models.join('\n')) throw new Error('Order changed');
    SET.models = [...saved.models]; SET.dirty = false;
    if (SET.llm) SET.llm.models = [...saved.models];
    toast('Model order saved. Checking mode unchanged.');
  } catch (err) { toast(err.status === 400 ? ucf(err.message) : 'Could not confirm this model order. Your changes are still here.'); }
  finally { renderModels(); }
};
$('#view-settings').addEventListener('click', async (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.id === 'set-recheck') return checkHealth();
  const ms = SET.models;
  const swap = (i, j) => { [ms[i], ms[j]] = [ms[j], ms[i]]; SET.dirty = true; renderModels(); };
  if (b.dataset.mup) return swap(+b.dataset.mup, +b.dataset.mup - 1);
  if (b.dataset.mdown) return swap(+b.dataset.mdown, +b.dataset.mdown + 1);
  if (b.dataset.mdel) { if (ms.length > 1) { ms.splice(+b.dataset.mdel, 1); SET.dirty = true; renderModels(); } else toast('Keep at least one model in the list'); return; }
  if (b.dataset.ktest) {
    if (settingsMode(S.sc) !== 'external' || S.scStale || S.scError) { toast('Switch to External AI to test a key'); return; }
    const id = b.dataset.ktest;
    SET.tests[id] = 'run'; renderSettings();
    try { SET.tests[id] = await api.post(`/api/llm/keys/${id}/test`, {}); } catch (err) { SET.tests[id] = { passed: false, error: 'Could not reach the server' }; }
    return loadSettings();
  }
  if (b.dataset.kdel) {
    const id = b.dataset.kdel;
    if (SET.confirm !== id) { SET.confirm = id; renderSettings(); setTimeout(() => { if (SET.confirm === id) { SET.confirm = null; renderSettings(); } }, 3000); return; }
    SET.confirm = null;
    try { await api.post(`/api/llm/keys/${id}/remove`, {}); toast('Key removed'); } catch (err) { toast(err.status === 400 ? ucf(err.message) : "Couldn't remove. Try again."); }
    return loadSettings();
  }
});

// ---------- qualification ----------
const ROLE_LABEL = { buyer: 'Brand owner', connector: 'Agency or freelancer', collaborator: 'Creative', peer: 'Similar service', supplier: 'Supplier', unrelated: 'Not a business', unclear: 'Unclear' };
function qualificationConnections(r) {
  const byHandle = new Map();
  for (const edge of r.connection_edges || []) {
    if (!edge?.handle || !['followers', 'following'].includes(edge.direction)) continue;
    if (r.owner_relationship && edge.handle.toLowerCase() === r.relationship_owner?.toLowerCase()) continue;
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
  if (!lines.length) return r.owner_relationship ? '' : '<p class="muted">No observed follows yet</p>';
  const row = (line) => `<li>${esc(line)}</li>`;
  const visible = `<ul class="ql-connections">${lines.slice(0, 2).map(row).join('')}</ul>`;
  return visible + (lines.length > 2 ? `<details class="ql-more-connections"><summary>${plural(lines.length - 2, 'more connection')}</summary><ul class="ql-connections">${lines.slice(2).map(row).join('')}</ul></details>` : '');
}
function profileCheckSummary(result) {
  const bio = result.bio?.state;
  const parts = [bio === 'pending' ? 'Profile read queued.' : bio === 'held' ? 'Waiting for collection to resume.' : bio === 'unavailable' ? 'Profile unavailable.' : bio === 'fresh' ? 'Profile up to date.' : 'Profile checked.'];
  if (result.site?.error) parts.push('Website unavailable.');
  else if (result.site && (result.site.title || result.site.summary || Object.values(result.site.signals || {}).some(Boolean))) parts.push(result.reused ? 'Website up to date.' : 'Website checked.');
  return parts.join(' ');
}
const Q = {
  view: 'ai', q: '', sort: 'score', rows: [], total: 0, sum: null, busy: new Set(), gen: 0, loadError: null, failedMore: false,
  async show() { await this.load(); },
  async load(more) {
    const g = ++this.gen;
    const p = new URLSearchParams({ view: this.view, sort: this.sort, limit: 30, offset: more ? this.rows.length : 0 });
    if (this.q) p.set('q', this.q);
    let d;
    try { d = await api.get('/api/qual?' + p); } catch (e) {
      if (g !== this.gen) return;
      this.loadError = e.status === 404 ? 'Review is unavailable in this server version.' : 'Could not update review results.';
      this.failedMore = !!more; this.render(); return;
    }
    if (g !== this.gen) return;
    this.loadError = null;
    this.rows = more ? [...this.rows, ...d.rows] : d.rows; this.total = d.total; this.sum = d.summary;
    // With no AI verdicts yet, show keyword verdicts instead of an empty page.
    if (!more && this.view === 'ai' && !d.total && !this.q && !this.fellBack) { this.fellBack = true; this.view = 'all'; this.syncSeg(); return this.load(); }
    this.renderProg(); this.render();
  },
  syncSeg() { $$('#ql-view button').forEach((b) => b.classList.toggle('on', b.dataset.v === this.view)); },
  renderProg() {
    const s = this.sum || {}, Qp = S.sc?.progress?.qualify || {};
    const mode = settingsMode(S.sc);
    const known = !!mode;
    const on = mode === 'external';
    const status = !known ? 'Checking status unavailable' : on ? (Qp.left ? `${int(Qp.left)} waiting for external review` : 'External review enabled') : mode === 'local' ? localProcessingSummary() : 'Rules only';
    $('#ql-prog').innerHTML = `<p class="ql-progress-summary"><span>${s.rules == null ? 'Rules count unavailable' : `Rules checked ${int(s.rules)} profiles`}</span><span class="muted">${esc(status)}</span>${mode === 'external' ? `<span class="muted">${esc(localProcessingSummary())}</span>` : ''}${backgroundAIControlsHTML()}</p>`;
    $('#ql-toggle').textContent = 'Checking mode';
    $('#n-qual').textContent = on && Qp.left ? fmt(Qp.left) : '';
  },
  card(r) {
    const v = r.verdict || {}, ai = v.model && v.model !== 'rules';
    const ev = evidenceOf(v);
    const bio = (r.bio || '').trim();
    const facts = [r.category ? esc(r.category) : '', r.followers != null ? `${fmt(r.followers)} followers` : ''].filter(Boolean).join(' · ');
    const siteHTML = websiteEvidence(r.site);
    const busy = this.busy.has(r.id);
    return `<article class="ql-card" data-id="${r.id}">
      <div class="ql-top">${avatar(r.pic, r.name || r.handle, 'lg')}
        <div class="who"><b>${esc(r.name || r.handle)}</b><span>@${esc(r.handle)}${r.status ? ' · ' + esc(ucf(r.status)) : ''}</span></div>
        <div class="ql-score">${fitBadge(r, 'lg')}</div></div>
      <div class="ql-why"><p>${esc(r.reason || 'No assessment yet.')}</p><p class="ql-by">${ai ? 'AI' : 'Rules'}${v.at ? ` · ${ago(v.at)} ago` : ''}</p></div>
      <div class="ql-profile">${facts ? `<p class="ql-factline">${facts}</p>` : ''}<p>${bio ? esc(bio.length > 140 ? bio.slice(0, 140) + '…' : bio) : r.is_private ? 'Private profile' : 'Bio not read yet'}</p>
        ${r.website && safeUrl(r.website) ? `<a href="${esc(safeUrl(r.website))}" target="_blank" rel="noopener">${esc(r.website.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, ''))}</a>` : ''}</div>
      <div class="ql-network">${relationshipHTML(r)}${qualificationConnections(r)}</div>
      ${ev.length || siteHTML ? `<details class="ql-evidence"><summary>Evidence</summary>${ev.length ? `<ul class="evidence">${ev.map((q) => `<li>${esc(q)}</li>`).join('')}</ul>` : ''}${siteHTML}</details>` : ''}
      <div class="ql-acts"><button class="btn solid" data-open="${r.id}">Profile</button>
        <a class="btn ghost ql-instagram" href="https://www.instagram.com/${encodeURIComponent(r.handle)}/" target="_blank" rel="noopener">Instagram ↗</a>
        <span class="grow"></span><button class="btn ghost" data-deep="${r.id}" ${busy ? 'disabled' : ''}>${busy ? 'Checking…' : 'Check profile'}</button></div>
      ${r.deeper_result ? `<p class="ql-result ${r.deeper_result.state === 'error' ? 'bad' : 'muted'}" role="status">${esc(r.deeper_result.note)}</p>` : ''}
    </article>`;
  },
  render() {
    if (this.loadError) {
      $('#ql-n').textContent = 'Results unavailable';
      $('#ql-list').innerHTML = `<div class="muted ql-empty" role="alert">${esc(this.loadError)} <button type="button" class="btn" data-ql-retry>Retry</button></div>`;
      $('#ql-more').hidden = true; return;
    }
    $('#ql-n').textContent = `${int(this.total)} ${this.total === 1 ? 'person' : 'people'}`;
    const focused = $('#ql-list').contains(document.activeElement) ? document.activeElement : null;
    const focusAttr = focused?.hasAttribute('data-deep') ? 'data-deep' : focused?.hasAttribute('data-open') ? 'data-open' : null;
    const focusId = focusAttr ? focused.getAttribute(focusAttr) : null;
    $('#ql-list').innerHTML = this.rows.length ? this.rows.map((r) => this.card(r)).join('')
      : `<div class="muted ql-empty">${this.q ? 'No people match this search.' : this.view === 'ai' ? 'No AI reviews yet. Choose a checking mode in Settings.' : 'Nobody matches.'}</div>`;
    if (focusAttr) $(`#ql-list [${focusAttr}="${focusId}"]`)?.focus({ preventScroll: true });
    $('#ql-more').hidden = this.rows.length >= this.total;
  },
  async deeper(id) {
    const restoreFocus = document.activeElement?.dataset.deep === String(id);
    this.busy.add(id); this.render();
    try {
      const d = await api.post(`/api/qual/${id}/deeper`);
      const r = this.rows.find((x) => x.id === id);
      const note = profileCheckSummary(d);
      if (r) { if (d.site) r.site = d.site; r.deeper_result = {state:d.state, note}; }
      toast(note);
    } catch (e) {
      const r = this.rows.find((x) => x.id === id);
      const message = "Couldn't check this profile. Try again.";
      if (r) r.deeper_result = {state:'error',note:message};
      toast(message);
    }
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
$('#ql-toggle').onclick = () => {
  location.hash = '#/settings';
  setView('settings');
  const picker = $('#set-mode');
  picker?.scrollIntoView({ block: 'center' });
  picker?.querySelector('button[aria-pressed="true"]')?.focus({ preventScroll: true });
};
$('#ql-list').addEventListener('click', (e) => {
  if (e.target.closest('[data-ql-retry]')) return Q.load(Q.failedMore);
  const d = e.target.closest('[data-deep]'); if (d) return Q.deeper(+d.dataset.deep);
  const o = e.target.closest('[data-open]');
  if (o) { setView('leads'); setURL(true); openDetail(+o.dataset.open); }
});

// ---------- map ----------
// The map lives in map-core.js, map-model.js, map-view.js and map-host.js.
// M is the small surface the rest of this file uses; it waits for the view to attach.
const M = {
  impl: null, wantShown: false,
  get byId() { return new Map((this.impl?.model.scene.nodes || []).map((it) => ['p:' + it.d.id, it.d])); },
  attach(impl) { this.impl = impl; if (this.wantShown) impl.show(); },
  show() { this.wantShown = true; this.impl?.show(); },
  hide() { this.wantShown = false; this.impl?.hide(); },
  resize() { this.impl?.resize(); },
  draw() { this.impl?.invalidate(); },
  patch(ids, op) { if (op && 'status' in op) for (const id of ids) this.impl?.model.patchStatus(id, op.status); },
  fit() { this.impl?.model.fit(); },
  zoomBy(f) { this.impl?.zoomCentre(f); },
  get focus() { return this.impl?.model.selected || null; },
  set focus(v) { if (!v) this.impl?.model.deselect(); },
  load() { this.impl?.model.retry(); },
};
window.addEventListener('connections-viewchange', () => {
  if (S.view === 'map') M.show();
});
// Server came back: refresh whatever the offline spell left stale or empty.
function reconnected() {
  resetLeads(true); loadFacets(); loadCounts();
  if (S.view === 'map') M.load();
  if (S.view === 'tags') T.show();
  if (S.open) refreshPerson(S.open);
}

window.addEventListener('dm-import-complete', reconnected);

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
  loadFacets(); loadCounts(); loadScraper();
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
setInterval(() => { if (S.view === 'leads' && !document.hidden && S.rows.length && $('#scroll').scrollTop < 5 && !S.open) resetLeads(true); }, 45000);
setInterval(() => { if (offlineSince) setOnline(false); if (S.view === 'scraper') renderScraper(); }, 1000);
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
  try { await api.post('/api/settings/biofetch', body); $('#bf-tok').value = ''; toast(on ? 'Meta bios on' : 'Meta bios off'); loadBiofetch(); } catch (e) { toast("Couldn't save. Try again."); }
};
