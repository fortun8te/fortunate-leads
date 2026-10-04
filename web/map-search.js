/* Map search: the box, the result list and what choosing a result does.
 * Results come from /api/map/search (ranked on the server, whole database). Choosing one asks the model to
 * go to that person: select them, fly the camera there, and show them as a guest when they are not on the page.
 * No canvas code lives here. */
(function (root) {
  'use strict';
  const Core = root.MapCore || (typeof require === 'function' ? require('./map-core.js') : null);
  const { int, compact } = Core;
  const DEBOUNCE = 90, SLOW = 220, RECENT_KEY = 'fortunate.map.recent', RECENT_MAX = 5;
  const RELATION = { mutual: 'Mutual', follows_you: 'Follows you', you_follow: 'You follow', you: 'You' };

  /* ---------- text helpers (mirror the server's folding) ---------- */
  const SPECIAL = { 'ø': 'o', 'ß': 'ss', 'æ': 'ae', 'œ': 'oe', 'ł': 'l', 'đ': 'd', 'ı': 'i', 'ð': 'd', 'þ': 'th' };
  const fold = (text) => String(text || '').normalize('NFKD').toLowerCase().replace(/[øßæœłđıðþ]/g, (c) => SPECIAL[c]).replace(/[̀-ͯ]/g, '');
  const words = (text) => fold(text).match(/[\p{L}\p{N}]+/gu) || [];

  // Split text into [{text, hit}] so the typed words show in bold, whatever accents or case the profile uses.
  function highlight(text, query) {
    text = String(text || '');
    const tokens = words(query).filter(Boolean);
    if (!text || !tokens.length) return [{ text, hit: false }];
    let folded = '';
    const owner = [];
    let i = 0;
    for (const ch of text) {
      const f = fold(ch);
      for (let k = 0; k < f.length; k++) owner.push(i);
      folded += f;
      i += ch.length;
    }
    const marks = new Array(text.length).fill(false);
    for (const token of tokens) {
      let at = -1;
      const boundary = new RegExp('(^|[^\\p{L}\\p{N}])' + token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'u').exec(folded);
      if (boundary) at = boundary.index + boundary[1].length; else at = folded.indexOf(token);
      if (at < 0) continue;
      const from = owner[at], to = (owner[at + token.length - 1] ?? text.length - 1);
      const end = to + (String.fromCodePoint(text.codePointAt(to)).length);
      for (let p = from; p < Math.min(end, text.length); p++) marks[p] = true;
    }
    const out = [];
    for (let p = 0; p < text.length; p++) {
      const last = out[out.length - 1];
      if (last && last.hit === marks[p]) last.text += text[p]; else out.push({ text: text[p], hit: marks[p] });
    }
    return out;
  }
  const relationOf = (n) => RELATION[n.relation] || (n.followed && n.follows_me ? 'Mutual' : n.follows_me ? 'Follows you' : n.followed ? 'You follow' : '');
  const initials = (n) => String(n.name || n.handle || '?').replace(/[^\p{L}\p{N} ]/gu, ' ').trim().split(/\s+/).slice(0, 2).map((s) => s[0]).join('').toUpperCase() || '?';
  const followersText = (n) => (n.followers != null && Number.isFinite(+n.followers) ? `${compact(n.followers)} followers` : '');

  /* ---------- recent searches ---------- */
  function readRecent(storage) {
    try { const list = JSON.parse(storage?.getItem(RECENT_KEY) || '[]'); return Array.isArray(list) ? list.filter((x) => x && x.id != null && x.handle).slice(0, RECENT_MAX) : []; } catch (_) { return []; }
  }
  function rememberRecent(storage, n) {
    try {
      const keep = readRecent(storage).filter((x) => String(x.id) !== String(n.id));
      keep.unshift({ id: n.id, handle: n.handle, name: n.name || '', pic: n.pic || null });
      storage?.setItem(RECENT_KEY, JSON.stringify(keep.slice(0, RECENT_MAX)));
    } catch (_) { /* private mode */ }
  }

  class MapSearch {
    constructor(o) {
      this.input = o.input; this.list = o.list; this.model = o.model; this.doc = o.doc || root.document;
      this.announce = o.announce || (() => {}); this.afterChoose = o.afterChoose || (() => {});
      this.metric = o.metric || followersText;
      this.storage = o.storage === undefined ? root.localStorage : o.storage;
      this.timers = o.timers || { set: (f, ms) => setTimeout(f, ms), clear: (t) => clearTimeout(t) };
      this.results = []; this.active = -1; this.state = 'closed'; this.query = ''; this.timer = null; this.slowTimer = null; this.warmed = false; this.retry = null;
      this.rows = [];
    }
    h(tag, attrs, ...kids) {
      const el = this.doc.createElement(tag);
      for (const [k, v] of Object.entries(attrs || {})) {
        if (v == null || v === false) continue;
        if (k === 'text') el.textContent = v; else if (k === 'class') el.className = v; else el.setAttribute(k, v === true ? '' : v);
      }
      for (const kid of kids.flat()) if (kid != null) el.append(kid);
      return el;
    }

    /* ----- wiring ----- */
    bind() {
      const input = this.input;
      input.addEventListener('input', () => this.typed());
      input.addEventListener('focus', () => { this.warm(); if (!input.value.trim()) this.showRecent(); });
      input.addEventListener('keydown', (e) => this.key(e));
      input.addEventListener('blur', () => this.timers.set(() => this.close(), 140));
      this.list.addEventListener('mousedown', (e) => e.preventDefault());
      this.list.addEventListener('click', (e) => {
        const row = e.target.closest?.('[data-i]');
        if (row) this.pick(+row.getAttribute('data-i'));
      });
      this.list.addEventListener('mousemove', (e) => {
        const row = e.target.closest?.('[data-i]');
        if (row && +row.getAttribute('data-i') !== this.active) { this.active = +row.getAttribute('data-i'); this.mark(); }
      });
    }
    // Ask the server to get its search index ready before the first real query.
    warm() {
      if (this.warmed) return;
      this.warmed = true;
      try { Promise.resolve(this.model.fetchJson('/api/map/search?q=&warm=1')).catch(() => {}); } catch (_) { /* offline */ }
    }

    /* ----- typing ----- */
    typed() {
      this.timers.clear(this.timer);
      const q = this.input.value.trim();
      if (!q) { this.model.searchReq.cancel(); this.query = ''; this.showRecent(); return; }
      this.timer = this.timers.set(() => this.run(q), DEBOUNCE);
    }
    async run(q, now) {
      this.timers.clear(this.timer); this.timers.clear(this.retry);
      this.query = q;
      // Keep the previous rows while the next answer is on its way; only say "Searching" when there is nothing to show.
      this.timers.clear(this.slowTimer);
      if (!this.results.length) this.slowTimer = this.timers.set(() => { if (this.query === q && this.state !== 'ok') this.show([], 'loading'); }, SLOW);
      let list;
      try {
        list = await this.model.searchPeople(q);
      } catch (e) {
        if ((e && e.name === 'AbortError') || q !== this.input.value.trim()) return;
        this.timers.clear(this.slowTimer);
        this.show([], e && e.status === 400 ? 'invalid' : 'error');
        return;
      }
      this.timers.clear(this.slowTimer);
      if (list === null || q !== this.input.value.trim()) return;
      this.results = list; this.active = list.length ? 0 : -1;
      this.show(list, list.length ? 'ok' : 'none');
      if (now && list.length === 1) this.pick(0);
    }

    /* ----- keyboard ----- */
    key(e) {
      const open = this.state !== 'closed';
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (!open) { const q = this.input.value.trim(); if (q) this.run(q); else this.showRecent(); e.preventDefault(); return; }
        if (!this.results.length) return;
        e.preventDefault();
        this.active = (this.active + (e.key === 'ArrowDown' ? 1 : -1) + this.results.length) % this.results.length;
        this.mark();
      } else if (e.key === 'Home' && open && this.results.length && this.input.selectionStart === 0 && e.ctrlKey) {
        e.preventDefault(); this.active = 0; this.mark();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        const q = this.input.value.trim();
        if (this.results.length && this.active >= 0 && this.state !== 'error') this.pick(this.active);
        else if (q) this.run(q, true);
      } else if (e.key === 'Escape') {
        if (this.input.value || open) { e.stopPropagation(); this.input.value = ''; this.close(); }
      } else if (e.key === 'Tab') this.close();
    }

    /* ----- choosing ----- */
    async pick(i) {
      const n = this.results[i];
      if (!n) return;
      this.close(); this.input.value = '';
      if (n.recent) {
        // A remembered person has no position yet; ask for it by handle.
        let found = null;
        try { found = (await this.model.searchPeople('@' + n.handle) || []).find((x) => String(x.id) === String(n.id)); } catch (_) { /* fall through */ }
        if (!found) { this.input.value = '@' + n.handle; this.input.dispatchEvent?.(new root.Event('input')); this.typed(); return; }
        return this.pickPerson(found);
      }
      this.pickPerson(n);
    }
    pickPerson(n) {
      rememberRecent(this.storage, n);
      this.model.goTo(n);
      this.afterChoose(n);
      this.announce(`Showing ${n.name || '@' + n.handle}.`);
    }

    /* ----- drawing the list ----- */
    close() {
      this.model.searchReq.cancel();
      this.timers.clear(this.timer); this.timers.clear(this.slowTimer); this.timers.clear(this.retry);
      this.state = 'closed'; this.results = []; this.active = -1; this.rows = [];
      this.input.removeAttribute('aria-activedescendant'); this.input.setAttribute('aria-expanded', 'false');
      this.list.hidden = true; this.list.replaceChildren();
    }
    showRecent() {
      const recent = readRecent(this.storage);
      if (!recent.length) { this.close(); return; }
      this.results = recent.map((x) => ({ ...x, recent: true })); this.active = -1;
      this.show(this.results, 'recent');
    }
    show(list, state) {
      this.state = state;
      const box = this.list;
      box.hidden = false;
      this.input.setAttribute('aria-expanded', 'true');
      const note = (text, extra) => this.h('p', { class: 'mv-r-note', role: 'status', text }, extra);
      if (state === 'loading') box.replaceChildren(note('Searching…'));
      else if (state === 'none') box.replaceChildren(note(`No one matches “${this.query}”. Try a handle, a name or a word from the bio.`));
      else if (state === 'invalid') box.replaceChildren(note('Search for a name, handle or Instagram profile link.'));
      else if (state === 'error') box.replaceChildren(note('Search isn’t available. Try again.'));
      else {
        this.rows = list.map((n, i) => this.row(n, i));
        const head = state === 'recent' ? [this.h('p', { class: 'mv-r-head', text: 'Recent' })] : [];
        box.replaceChildren(...head, ...this.rows);
        this.mark();
      }
    }
    row(n, i) {
      const query = this.query;
      const marked = (text) => highlight(text, query).map((s) => (s.hit ? this.h('mark', { text: s.text }) : s.text));
      const photo = /^\/img\/\d+$/.test(n.pic || '') ? this.h('img', { src: n.pic, alt: '', width: 28, height: 28, loading: 'lazy', decoding: 'async' }) : null;
      const avatar = this.h('span', { class: 'mv-r-av', 'aria-hidden': 'true' }, photo || initials(n));
      photo?.addEventListener?.('error', () => avatar.replaceChildren(initials(n)), { once: true });
      const relation = relationOf(n);
      const second = [n.name ? this.h('span', { class: 'mv-r-name' }, marked(n.name)) : null, this.metric(n) ? this.h('span', { class: 'mv-r-fol', text: this.metric(n) }) : null].filter(Boolean);
      return this.h('button', { type: 'button', role: 'option', id: 'mv-result-' + i, tabindex: '-1', 'data-i': i, 'aria-selected': 'false', class: 'mv-r' },
        avatar,
        this.h('span', { class: 'mv-r-main' },
          this.h('b', { class: 'mv-r-handle' }, '@', marked(n.handle)),
          second.length ? this.h('span', { class: 'mv-r-sub' }, ...second.flatMap((x, k) => (k ? [this.h('i', { class: 'mv-r-dot', 'aria-hidden': 'true', text: '·' }), x] : [x]))) : null,
          n.match?.field === 'bio' && n.snippet ? this.h('span', { class: 'mv-r-snip' }, marked(n.snippet)) : null),
        relation ? this.h('em', { class: 'mv-r-rel', text: relation }) : null);
    }
    mark() {
      this.rows.forEach((row, i) => {
        const on = i === this.active;
        row.className = 'mv-r' + (on ? ' on' : '');
        row.setAttribute('aria-selected', String(on));
        if (on) { this.input.setAttribute('aria-activedescendant', 'mv-result-' + i); row.scrollIntoView?.({ block: 'nearest' }); }
      });
      if (this.active < 0) this.input.removeAttribute('aria-activedescendant');
    }
  }

  // A soft ring that settles around a person the search just found. Drawn by the view for about a second.
  function drawLocator(ctx, palette, x, y, r, age, guest) {
    const t = Math.min(1, age / 1400);
    if (t >= 1) return false;
    const ease = 1 - Math.pow(1 - t, 3), grow = r + 6 + (1 - ease) * 22;
    ctx.save();
    ctx.globalAlpha = (1 - t) * .9;
    ctx.strokeStyle = palette.fg; ctx.lineWidth = 1.5;
    if (guest) ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.arc(x, y, grow, 0, Math.PI * 2); ctx.stroke();
    ctx.restore();
    return true;
  }
  // Selected guests keep a quiet dashed ring so they are not mistaken for part of the page.
  function drawGuestRing(ctx, palette, x, y, r) {
    ctx.save();
    ctx.globalAlpha = .75; ctx.strokeStyle = palette.fg; ctx.lineWidth = 1.25; ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.arc(x, y, r + 5, 0, Math.PI * 2); ctx.stroke();
    ctx.restore();
  }

  const api = { MapSearch, highlight, fold, words, relationOf, followersText, readRecent, rememberRecent, drawLocator, drawGuestRing, RELATION };
  root.MapSearchModule = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window === 'undefined' ? globalThis : window);
