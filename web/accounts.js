/* Accounts page: Instagram account cards and the profiles to collect. See docs/ACCOUNTS_UX.md.
 *
 * Plain JS, no build step. The top half is pure (parsing, wording, HTML strings) so tests run it in Node.
 * The bottom half is the page: it polls two small endpoints that answer 304 when nothing changed, updates
 * only the rows and cards whose markup changed, and applies every write optimistically with a rollback.
 */
(function (root) {
  'use strict';

  // ---------- small helpers ----------
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const int = (n) => (n == null || n === '' || !Number.isFinite(+n) ? '–' : Number(n).toLocaleString('en-US'));
  const plural = (n, a, b) => int(n) + ' ' + (n === 1 ? a : b || a + 's');
  const DIRS = ['followers', 'following'];
  const DIR_LABEL = { followers: 'Followers', following: 'Following' };

  function relative(iso, now = Date.now()) {
    if (!iso) return 'never';
    const s = Math.max(0, (now - Date.parse(iso)) / 1000);
    if (s < 10) return 'moments ago';
    if (s < 60) return Math.round(s) + 's ago';
    if (s < 3600) return Math.round(s / 60) + 'm ago';
    if (s < 86400) return Math.round(s / 3600) + 'h ago';
    return Math.round(s / 86400) + 'd ago';
  }
  function countdown(until, now = Date.now()) {
    const s = Math.max(0, Math.ceil((Date.parse(until) - now) / 1000));
    if (s < 60) return s + ' s';
    const m = Math.ceil(s / 60);
    if (m < 90) return m + ' min';
    return Math.floor(m / 60) + ' h ' + String(m % 60).padStart(2, '0') + ' min';
  }
  const clock = (iso) => { const d = new Date(iso); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); };

  // ---------- reading pasted text (mirrors server/storage/handles.py and seeds.read_token) ----------
  const HOSTS = new Set(['instagram.com', 'www.instagram.com', 'm.instagram.com', 'instagr.am', 'www.instagr.am']);
  const RESERVED = new Set(['p', 'reel', 'reels', 'tv', 'stories', 'explore', 'accounts', 'direct', 'about', 'developer', '_u']);
  const REASONS = {
    post_link: 'This is a post or reel link, not a profile.',
    not_instagram: 'This is not an Instagram link.',
    too_long: 'Instagram names are 30 characters or fewer.',
    not_valid: 'This is not an Instagram name.',
    duplicate: 'Already in this paste.',
  };
  function readToken(raw) {
    let t = String(raw).trim().replace(/^@/, '');
    if (!t) return { handle: null, reason: 'not_valid' };
    if (/[/?#:]/.test(t) || /instagram\.com|instagr\.am/i.test(t)) {
      const url = /^https?:\/\//i.test(t) ? t : 'https://' + t;
      let parsed;
      try { parsed = new URL(url); } catch { return { handle: null, reason: 'not_instagram' }; }
      if (!HOSTS.has(parsed.hostname.toLowerCase()) || parsed.username || parsed.password || parsed.port) {
        return { handle: null, reason: 'not_instagram' };
      }
      const parts = parsed.pathname.split('/');
      if (parts.length < 2 || parts.length > 3 || (parts.length === 3 && parts[2])) return { handle: null, reason: RESERVED.has((parts[1] || '').toLowerCase()) ? 'post_link' : 'not_valid' };
      try { t = decodeURIComponent(parts[1]); } catch { return { handle: null, reason: 'not_valid' }; }
    }
    if (RESERVED.has(t.toLowerCase()) && /^(p|reel|reels|tv|stories|explore|direct|accounts)$/i.test(t)) return { handle: null, reason: 'post_link' };
    if (/^[A-Za-z0-9._]{1,30}$/.test(t) && !RESERVED.has(t.toLowerCase())) return { handle: t.toLowerCase(), reason: null };
    return { handle: null, reason: /^[A-Za-z0-9._]+$/.test(t) && t.length > 30 ? 'too_long' : 'not_valid' };
  }
  function splitTokens(text) {
    return String(text || '').split(/[\s,;]+/).map((t) => t.replace(/^["'()[\]{}<>`]+|["'()[\]{}<>`]+$/g, '')).filter(Boolean);
  }
  /** Instant, offline reading of a paste. The server's check replaces these verdicts when it answers. */
  function readPaste(text) {
    const seen = new Set();
    return splitTokens(text).map((raw) => {
      const { handle, reason } = readToken(raw);
      if (!handle) return { input: raw, handle: null, state: 'invalid', reason, message: REASONS[reason], add: [], warn: [] };
      if (seen.has(handle)) return { input: raw, handle, state: 'duplicate', reason: 'duplicate', message: REASONS.duplicate, add: [], warn: [] };
      seen.add(handle);
      return { input: raw, handle, state: 'new', reason: null, message: null, add: [], warn: [] };
    });
  }
  const summarize = (items) => {
    const c = { ready: 0, already: 0, duplicate: 0, invalid: 0, private: 0, restore: 0, renamed: 0 };
    for (const i of items) {
      if (['new', 'extend', 'restore'].includes(i.state)) c.ready++;
      else if (i.state === 'exists') c.already++;
      else if (i.state === 'duplicate') c.duplicate++;
      else if (i.state === 'invalid') c.invalid++;
      if ((i.warn || []).includes('private')) c.private++;
      if (i.renamed_from) c.renamed++;
      if (i.state === 'restore') c.restore++;
    }
    return c;
  };
  function summaryText(c) {
    const parts = [];
    if (c.ready) parts.push(c.ready + ' ready');
    if (c.already) parts.push(c.already + ' already added');
    if (c.duplicate) parts.push(c.duplicate + ' repeated');
    if (c.invalid) parts.push(c.invalid === 1 ? "1 can't be read" : c.invalid + " can't be read");
    return parts.join(' · ');
  }

  // ---------- profiles: wording and markup ----------
  const TONE = { collecting: 'live', pausing: 'live', waiting: 'wait', paused: 'quiet', done: 'done', capped: 'quiet', partial: 'quiet',
    private: 'attention', error: 'attention', unverified: 'attention', removed: 'quiet', none: 'quiet', off: 'quiet' };
  function listPart(l) {
    if (!l.wanted && l.status !== 'off') return '';
    if (l.status === 'off') return '';
    const name = DIR_LABEL[l.direction];
    const n = l.expected != null ? `${int(l.saved)} of ${l.estimate ? '~' : ''}${int(l.expected)}` : l.saved ? int(l.saved) : '';
    switch (l.status) {
      case 'collecting': case 'pausing': case 'partial': case 'capped': case 'unverified': return `${name} ${n || 'started'}`;
      case 'done': return `${name} done${l.saved ? ', ' + int(l.saved) : ''}`;
      case 'waiting': return `${name} waiting`;
      case 'paused': return `${name} paused`;
      case 'private': return `${name} private`;
      case 'error': return `${name} could not be read`;
      default: return `${name} not queued yet`;
    }
  }
  const subLine = (s) => DIRS.map((d) => listPart(s.lists[d])).filter(Boolean).join(' · ');
  const avatarHTML = (pic, name) => `<span class="acx-av" data-i="${esc((name || '?').replace(/^@/, '')[0] || '?').toUpperCase()}">${pic ? `<img src="${esc(pic)}" alt="" loading="lazy" width="36" height="36">` : ''}</span>`;
  const IC = () => (root.Icons ? root.Icons.icon : () => '');
  const chip = (tone, label, dot = true) => `<span class="acx-chip" data-tone="${tone}">${dot ? '<i class="acx-dot" aria-hidden="true"></i>' : ''}${esc(label)}</span>`;

  function rowView(s, ui) {
    const open = ui.open.has(s.handle), selected = ui.sel.has(s.handle), busy = !!s.pending;
    const paused = s.status === 'paused' || s.lists.followers.status === 'paused' && !DIRS.some((d) => ['collecting', 'waiting'].includes(s.lists[d].status));
    const holdable = DIRS.some((d) => ['waiting', 'collecting', 'pausing', 'paused', 'none'].includes(s.lists[d].status));
    const bar = s.pct != null && ['collecting', 'waiting', 'paused', 'partial', 'pausing'].includes(s.status) ? s.pct : null;
    const note = s.renamed_from && s.renamed_from.length ? `Renamed from @${s.renamed_from[s.renamed_from.length - 1].from}` : '';
    const sub = s.pending ? 'Adding…' : (subLine(s) || '');
    const removed = s.status === 'removed';
    const action = removed ? `<button type="button" class="acx-link" data-act="restore" data-fid="restore">Restore</button>`
      : holdable ? `<button type="button" class="acx-link" data-act="${paused ? 'resume' : 'pause'}" data-fid="toggle" aria-label="${paused ? 'Resume' : 'Pause'} @${esc(s.handle)}">${paused ? 'Resume' : 'Pause'}</button>` : '';
    const html = `
      <label class="acx-check"><input type="checkbox" data-sel aria-label="Select @${esc(s.handle)}" ${selected ? 'checked' : ''} ${busy ? 'disabled' : ''}></label>
      ${avatarHTML(s.pic, s.handle)}
      <div class="acx-main">
        <div class="acx-line"><b class="acx-handle">@${esc(s.handle)}</b>${s.name ? `<span class="acx-name">${esc(s.name)}</span>` : ''}${note ? `<span class="acx-note" title="${esc(note)}">${esc(note)}</span>` : ''}${s.private ? '<span class="acx-note">Private</span>' : ''}</div>
        <div class="acx-sub">${esc(sub)}</div>
        <div class="acx-bar${bar == null ? ' none' : ''}" aria-hidden="true"><i style="width:${bar == null ? 0 : bar}%"></i></div>
      </div>
      <div class="acx-end">
        ${s.attention && s.status !== 'removed' ? `<span class="acx-attn" title="Needs a look">${IC()('alert', 14)}</span>` : ''}
        ${chip(TONE[s.status] || 'quiet', s.label)}
        <span class="acx-actions">${busy ? '' : action}<button type="button" class="acx-link acx-more" data-act="toggle" aria-expanded="${open}" aria-label="${open ? 'Hide' : 'Show'} details for @${esc(s.handle)}" data-fid="more">${IC()(open ? 'chevronUp' : 'chevronDown', 14)}</button></span>
      </div>
      ${open ? detailHTML(s) : ''}`;
    return { attrs: { class: 'acx-row' + (selected ? ' is-selected' : '') + (busy ? ' is-pending' : '') + (open ? ' is-open' : ''), 'data-handle': s.handle, 'data-status': s.status, 'aria-selected': String(selected), role: 'listitem', tabindex: ui.focus === s.handle ? '0' : '-1' }, html };
  }
  function detailHTML(s) {
    const lists = DIRS.map((d) => {
      const l = s.lists[d];
      const wanted = l.wanted || l.status === 'paused' || (l.status !== 'off');
      const on = s.lists[d].wanted;
      const text = on ? `${l.label}${l.expected != null ? `, ${int(l.saved)} of ${l.estimate ? '~' : ''}${int(l.expected)}` : l.saved ? `, ${int(l.saved)} saved` : ''}` : 'Off';
      return `<div class="acx-dl"><label class="acx-tick"><input type="checkbox" data-dir="${d}" ${on ? 'checked' : ''} aria-label="Collect ${DIR_LABEL[d].toLowerCase()} of @${esc(s.handle)}"><span>${DIR_LABEL[d]}</span></label><span class="acx-dtext">${esc(text)}</span>${l.note ? `<span class="acx-dnote">${esc(l.note)}</span>` : ''}</div>`;
    }).join('');
    const renamed = (s.renamed_from || []).map((r) => `<span>Used to be @${esc(r.from)}</span>`).join('');
    return `<div class="acx-detail">
      ${lists}
      <div class="acx-dmeta">${renamed}${s.added_at ? `<span>Added ${relative(s.added_at)}</span>` : ''}${s.source === 'suggested' ? '<span>Found by suggestions</span>' : ''}${s.identity === 'pending' ? '<span>Instagram ID is saved on the first read</span>' : ''}</div>
      <div class="acx-dact"><button type="button" class="btn" data-act="next" data-fid="next">Do next</button><a class="btn ghost" href="https://www.instagram.com/${encodeURIComponent(s.handle)}/" target="_blank" rel="noopener">Open on Instagram</a><span class="grow"></span><button type="button" class="btn ghost" data-act="remove" data-fid="remove">Remove</button></div>
    </div>`;
  }

  // ---------- cards: wording and markup ----------
  const CARD_DOT = { ok: 'live', wait: 'wait', attention: 'attention', quiet: 'quiet' };
  function headlineText(card, now) {
    if (card.headline.includes('{t}')) return card.until ? card.headline.replace('{t}', countdown(card.until, now)) : card.headline.replace(' {t}', '');
    return card.headline;
  }
  function allowanceHTML(card, now) {
    const a = card.allowance;
    if (!a) return '';
    const rows = [['list', 'list pages'], ['profile', 'bios']].filter(([k]) => a[k]).map(([k, unit]) => {
      const { used, limit } = a[k];
      const pct = limit ? Math.min(100, Math.round(used / limit * 100)) : 0;
      return `<div class="acx-allow"><span class="num">${limit ? `${int(used)} of ${int(limit)} ${unit} today` : `${int(used)} ${unit} today, no daily limit`}</span>${limit ? `<div class="acx-bar" aria-hidden="true"><i style="width:${pct}%"></i></div>` : ''}</div>`;
    });
    return rows.join('');
  }
  function eventsHTML(events) {
    if (!events || !events.length) return '<p class="acx-quiet">Nothing yet.</p>';
    return `<ul class="acx-events">${events.map((e) => `<li><span class="num" title="${esc(e.at)}">${esc(clock(e.at))}</span><span>${esc(e.text)}</span></li>`).join('')}</ul>`;
  }
  function detailsCardHTML(card, ui) {
    const d = card.details, b = d.budgets;
    const lim = (k, label, max) => `<label class="acx-field"><span>${label}</span><input class="input num" inputmode="numeric" data-limit="${k}" data-fid="limit-${k}" value="${b[k].custom && b[k].limit ? b[k].limit : ''}" placeholder="${b[k].default ?? ''}" aria-describedby="acx-lim-${esc(card.lane_id)}"></label>`;
    const err = ui.errors[card.lane_id + ':limit'];
    const reads = [['both', 'Lists and bios'], ['lists', 'Lists only'], ['bios', 'Bios only']];
    return `<div class="acx-more-body" id="acx-more-${esc(card.lane_id)}">
      ${card.kind === 'main' ? `<p class="acx-quiet">This is your personal account. It is only shown here so you can open Instagram.</p>` : `
      <div class="acx-group"><span class="acx-gl">Reads</span><div class="seg" role="group" aria-label="What this account reads">${reads.map(([v, l]) => `<button type="button" data-reads="${v}" aria-pressed="${d.reads === v}" class="${d.reads === v ? 'on' : ''}">${l}</button>`).join('')}</div></div>
      <div class="acx-group"><span class="acx-gl">Daily limits</span><div class="acx-limits">${lim('list', 'List pages', 3000)}${lim('profile', 'Bios', 5000)}</div>
        <p class="acx-quiet" id="acx-lim-${esc(card.lane_id)}" ${err ? 'role="alert" data-error' : ''}>${esc(err || `Leave empty to use the default (${int(b.list.default)} list pages, ${int(b.profile.default)} bios). Whole numbers only.`)}</p></div>
      <div class="acx-group"><span class="acx-gl">Name</span><label class="acx-field"><input class="input" data-label value="${esc(card.label || '')}" placeholder="${esc(card.handle ? '@' + card.handle : 'Scout 2')}" maxlength="40" aria-label="Name for this account" data-fid="label"></label></div>
      <div class="acx-group acx-pace" data-slot="pace" ${d.pace ? '' : 'hidden'}>${d.pace ? esc(d.pace.label || '') : ''}</div>`}
      <div class="acx-group"><span class="acx-gl">Recent activity</span>${eventsHTML(d.events)}</div>
      <div class="acx-foot"><span class="acx-quiet">${card.version ? 'Extension ' + esc(card.version) + '. ' : ''}Seen <span data-seen="${esc(card.last_seen || '')}">${esc(relative(card.last_seen))}</span>.</span><span class="grow"></span>
        ${card.disconnected ? `<button type="button" class="btn ghost" data-card="forget" data-fid="forget">${ui.confirm === card.lane_id + ':forget' ? 'Forget account?' : 'Forget'}</button>`
          : card.kind === 'main' ? '' : `<button type="button" class="btn ghost" data-card="disconnect" data-fid="disconnect">Disconnect</button>`}</div>
    </div>`;
  }
  function cardView(card, ui, now) {
    const open = ui.openCards.has(card.lane_id);
    const act = card.action;
    const busy = ui.busy.has(card.lane_id);
    const btn = !act ? '' : act.id === 'inbox'
      ? `<a class="btn" href="https://www.instagram.com/direct/inbox/" target="_blank" rel="noopener" data-card="inbox" data-fid="primary">${esc(act.label)}</a>`
      : `<button type="button" class="btn${['resume', 'acknowledge', 'connect', 'reconnect'].includes(act.id) ? ' solid' : ''}" data-card="${act.id}" data-fid="primary" ${act.enabled === false || busy ? 'disabled' : ''}>${esc(busy ? 'Working…' : act.label)}</button>`;
    const err = ui.errors[card.lane_id];
    const html = `
      <header class="acx-ch">${avatarHTML(card.avatar.pic, card.avatar.initial)}
        <div class="acx-who"><b>${esc(card.handle ? '@' + card.handle : card.label || card.name)}</b>${card.label && card.handle ? `<span class="acx-name">${esc(card.label)}</span>` : ''}</div>
        <span class="acx-role">${card.kind === 'main' ? 'Main' : 'Collector'}</span></header>
      <p class="acx-health" data-tone="${CARD_DOT[card.tone] || 'quiet'}"><i class="acx-dot" aria-hidden="true"></i><b data-headline="${esc(card.headline)}" ${card.until ? `data-until="${esc(card.until)}"` : ''}>${esc(headlineText(card, now))}</b></p>
      <p class="acx-why">${esc(card.detail)}${act && act.hint && act.enabled === false ? ' ' + esc(act.hint) : ''}${card.state === 'offline' && card.last_seen ? ` Last seen <span data-seen="${esc(card.last_seen)}">${esc(relative(card.last_seen, now))}</span>.` : ''}</p>
      ${allowanceHTML(card, now)}
      ${err ? `<p class="acx-error" role="alert">${esc(err)} <button type="button" class="acx-link" data-card="retry" data-fid="retry">Try again</button></p>` : ''}
      <div class="acx-cfoot">${btn}<span class="grow"></span><button type="button" class="acx-link" data-card="details" aria-expanded="${open}" aria-controls="acx-more-${esc(card.lane_id)}" data-fid="details">Details${IC()(open ? 'chevronUp' : 'chevronDown', 14)}</button></div>
      ${open ? detailsCardHTML(card, ui) : ''}`;
    return { attrs: { class: 'acx-card', 'data-lane': card.lane_id, 'data-state': card.state, 'data-tone': card.tone, 'data-kind': card.kind }, html };
  }

  // ---------- the guided add-account sequence ----------
  function wizardState(w, cards, now = Date.now()) {
    const fresh = cards.filter((c) => !w.base.has(c.lane_id));
    const c = fresh.find((x) => x.avatar && x.state !== 'disconnected') || fresh[0] || null;
    if (!c) return { step: 2, tone: 'wait', text: 'Waiting for the extension to check in from the new Chrome profile…', done: false, card: null,
      slow: now - w.started > 180000 };
    const dup = cards.find((x) => x.lane_id !== c.lane_id && c.ig_id && x.ig_id === c.ig_id && !x.disconnected);
    if (c.kind === 'main') return { step: 3, tone: 'attention', text: 'That is your personal account. It is never used for collecting. Sign in with a different account.', done: false, card: c };
    if (dup) return { step: 3, tone: 'attention', text: `@${dup.handle || 'this account'} is already connected in another Chrome profile. Sign in with a different account.`, done: false, card: c };
    if (!c.ig_id || c.state === 'needs_login') return { step: 3, tone: 'wait', text: 'The extension is connected. Sign in to Instagram in that profile and leave one Instagram tab open.', done: false, card: c };
    if (c.state === 'security_check') return { step: 3, tone: 'attention', text: 'Instagram wants a security check. Complete it in that Chrome profile.', done: false, card: c };
    if (c.state === 'review') return { step: 3, tone: 'attention', text: 'Instagram asked for a review of this account. It cannot collect yet.', done: false, card: c };
    if (c.state === 'offline') return { step: 3, tone: 'wait', text: `Lost contact with @${c.handle || 'the new profile'}. Keep its Chrome profile open.`, done: false, card: c };
    return { step: 4, tone: 'ok', text: `Verified. @${c.handle || 'The account'} will join when collection runs.`, done: true, card: c };
  }
  function wizardHTML(w, cards, setup, now) {
    const st = wizardState(w, cards, now);
    const copy = (text, label) => `<div class="acx-copy"><code title="${esc(text)}">${esc(label || text)}</code><button type="button" class="btn" data-copy="${esc(text)}">Copy</button></div>`;
    // Steps 1 and 2 cannot be seen from here, so they stay open until the extension checks in.
    const state = (n) => (st.card ? (st.step > n ? 'done' : st.step === n ? 'cur' : '') : n <= 2 ? 'cur' : '');
    const step = (n, title, body) => `<li class="${state(n)}" ${state(n) === 'cur' ? 'aria-current="step"' : ''}><span class="n" aria-hidden="true">${state(n) === 'done' ? IC()('check', 12) : n}</span><div><b>${title}</b>${state(n) === 'cur' ? body : ''}</div></li>`;
    return `<div class="acx-wiz-head"><div><h3>Add an Instagram account</h3><p class="acx-live" data-tone="${st.tone}" role="status"><i class="acx-dot" aria-hidden="true"></i>${esc(st.text)}${st.slow ? ' Still nothing? Check that the extension is switched on at chrome://extensions in that profile.' : ''}</p></div>
      <button type="button" class="btn${st.done ? ' solid' : ''}" data-wiz="close">${st.done ? 'Done' : 'Close'}</button></div>
      <ol class="acx-steps">
        ${step(1, 'Make a Chrome profile', '<p>Open the Chrome profile menu, choose Add, then Continue without an account. Use one profile for each Instagram account.</p>')}
        ${step(2, 'Add the extension', `<p>In that profile, open chrome://extensions, turn on Developer mode, choose Load unpacked and pick this folder.</p>${copy('chrome://extensions')}${copy(setup && setup.extension_path || 'extension/', setup && setup.extension_path)}`)}
        ${step(3, 'Sign in to Instagram', `<p>Sign in with the account this profile should use. Leave one Instagram tab open.</p>${copy('https://www.instagram.com/')}`)}
      </ol>`;
  }

  /** One quiet sentence about the queue, from the server's measured range. Nothing when there is nothing true to say. */
  function queueText(q) {
    if (!q) return '';
    if (q.status === 'estimated' && q.eta) {
      const span = (m) => (m < 60 ? Math.ceil(m) + ' min' : m < 2880 ? Math.ceil(m / 60) + ' h' : Math.ceil(m / 1440) + ' days');
      return `${q.eta.scope === 'known_lists' ? 'Known lists' : 'Current queue'}: about ${span(q.eta.low_minutes)} to ${span(q.eta.high_minutes)}`;
    }
    return q.status === 'warming_up' || q.status === 'unknown_totals' ? '' : q.message || '';
  }

  /** Open Instagram's inbox through the verified extension when this is the live workspace; otherwise the plain link does it.
   *  Returns 'link' when the caller should let the link open normally. Never claims success without the extension's proof. */
  async function openInstagramInbox(lane, env) {
    if (env.port !== '8777' || !(env.chrome && env.chrome.runtime && env.chrome.runtime.sendMessage)) return 'link';
    try {
      const setup = await env.getSetup(), ig = setup.instagram;
      if (!setup.extension_id || !ig || !ig.connected || ig.lane_id !== lane || !ig.ig_id) { env.toast('Open the connected Chrome profile and sign in to Instagram.'); return 'refused'; }
      await new Promise((resolve, reject) => {
        const t = env.setTimeout(() => reject(new Error('Unavailable')), 5000);
        env.chrome.runtime.sendMessage(setup.extension_id, { type: 'OPEN_INSTAGRAM', destination: 'inbox', expected_lane_id: lane, expected_account_id: ig.ig_id }, (res) => {
          env.clearTimeout(t);
          if (env.chrome.runtime.lastError || !res || !res.ok || !res.opened || !res.account_verified) reject(new Error('Unconfirmed')); else resolve(res);
        });
      });
      env.toast('Instagram opened in your connected profile.');
      return 'opened';
    } catch { env.toast("Couldn't open the connected profile. Open Instagram in that Chrome profile."); return 'failed'; }
  }

  // ---------- pure state helpers ----------
  /** The seed list after an optimistic action, so the row answers before the server does. */
  function applyLocal(items, action, handles, directions) {
    const set = new Set(handles);
    const label = { paused: 'Paused', waiting: 'Waiting' };
    return items.flatMap((s) => {
      if (!set.has(s.handle)) return [s];
      if (action === 'remove') return [];
      if (action === 'restore') return [{ ...s, status: 'waiting', label: 'Waiting' }];
      if (action === 'pause') return [{ ...s, status: 'paused', label: label.paused, lists: mapLists(s.lists, (l) => (['waiting', 'collecting', 'none'].includes(l.status) ? { ...l, status: 'paused', label: 'Paused' } : l)) }];
      if (action === 'resume') return [{ ...s, status: 'waiting', label: label.waiting, lists: mapLists(s.lists, (l) => (l.status === 'paused' ? { ...l, status: 'waiting', label: 'Waiting' } : l)) }];
      if (action === 'directions') return [{ ...s, lists: mapLists(s.lists, (l) => ({ ...l, wanted: directions.includes(l.direction), status: directions.includes(l.direction) ? (l.status === 'off' ? 'waiting' : l.status) : 'off', label: directions.includes(l.direction) ? (l.status === 'off' ? 'Waiting' : l.label) : 'Off' })) }];
      return [s];
    });
  }
  const mapLists = (lists, fn) => ({ followers: fn(lists.followers), following: fn(lists.following) });
  function provisional(item, directions) {
    const lists = {};
    for (const d of DIRS) lists[d] = { direction: d, wanted: directions.includes(d), status: directions.includes(d) ? 'waiting' : 'off', label: directions.includes(d) ? 'Waiting' : 'Off', saved: 0, expected: null, estimate: false, note: '' };
    return { handle: item.handle, name: item.person && item.person.name || null, pic: item.person && item.person.pic || null, followers: null, following: null, private: false,
      identity: 'pending', added_at: new Date().toISOString(), yours: false, source: 'you', status: 'waiting', label: 'Waiting', attention: false, lists, saved: 0, expected: null, pct: null, renamed_from: [], pending: true };
  }

  const Pure = { esc, int, plural, relative, countdown, clock, readToken, splitTokens, readPaste, summarize, summaryText, rowView, cardView, detailHTML, wizardState, wizardHTML,
    applyLocal, provisional, queueText, openInstagramInbox, headlineText, subLine, listPart, allowanceHTML };
  if (typeof module !== 'undefined' && module.exports) module.exports = Pure;
  root.AccountsPure = Pure;
  if (typeof document === 'undefined' || !document.getElementById || !document.getElementById('acx-cards')) return;

  // =====================================================================================================
  // The page
  // =====================================================================================================
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const reduced = () => root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const view = $('#view-accounts');
  const X = {
    active: false, timer: 0, tick: 0, started: Date.now(),
    seeds: { items: [], counts: null, total: 0, scope: 'both', etag: null, loaded: false, error: false },
    live: { data: null, etag: null, loaded: false, error: false },
    filter: 'all', q: '', sort: 'newest', limit: 200,
    ui: { open: new Set(), sel: new Set(), openCards: new Set(), busy: new Set(), errors: {}, confirm: null, focus: null },
    paste: { text: '', dirs: new Set(JSON.parse(safeGet('acx-dirs') || '["followers","following"]')), items: [], checking: 0, error: '', adding: false },
    wiz: null, lastSel: null, inflight: 0,
  };
  function safeGet(k) { try { return localStorage.getItem('fl-' + k); } catch { return null; } }
  function safeSet(k, v) { try { localStorage.setItem('fl-' + k, v); } catch { /* storage unavailable */ } }
  const toast = (msg, undo, opts) => (typeof root.toast === 'function' ? root.toast(msg, undo, opts) : undefined);

  // ----- network -----
  async function request(method, url, body, etag) {
    let r;
    try {
      r = await fetch(url, { method, cache: 'no-store', headers: Object.assign({}, body !== undefined ? { 'Content-Type': 'application/json' } : {}, etag ? { 'If-None-Match': etag } : {}), body: body !== undefined ? JSON.stringify(body) : undefined });
    } catch (e) { if (typeof root.setOnline === 'function') root.setOnline(false); throw e; }
    if (typeof root.setOnline === 'function') root.setOnline(true);
    if (r.status === 304) return { notModified: true };
    let data = null;
    try { data = await r.json(); } catch { /* empty */ }
    if (!r.ok) { const e = new Error(data && typeof data.error === 'string' ? data.error : 'The request did not go through.'); e.status = r.status; throw e; }
    return { data, etag: r.headers.get('ETag') };
  }
  async function write(url, body) {
    for (let attempt = 0; ; attempt++) {
      try { return (await request('POST', url, body)).data; } catch (e) {
        if (attempt < 2 && (e.status === 503 || e.status === undefined)) { await new Promise((r) => setTimeout(r, 400 * (attempt + 1))); continue; }
        throw e;
      }
    }
  }
  const sentence = (e, fallback) => {
    const m = e && e.message ? String(e.message) : '';
    return m && m.length < 140 && !/^(The server|Internal|Failed to fetch|Load failed)/.test(m) ? m : fallback;
  };

  // ----- keyed rendering -----
  function editing(el) { const a = document.activeElement; return !!(a && el.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) && a.type !== 'checkbox'); }
  function patch(el, { attrs, html }) {
    const sig = JSON.stringify(attrs) + html;
    if (el.__sig === sig) return;
    if (editing(el)) { el.__stale = true; return; }
    const a = document.activeElement;
    const fid = a && el.contains(a) ? a.getAttribute('data-fid') : null;
    for (const [k, v] of Object.entries(attrs)) if (el.getAttribute(k) !== v) el.setAttribute(k, v);
    if (el.__html !== html) { el.innerHTML = html; el.__html = html; }
    el.__sig = sig; el.__stale = false;
    if (fid) { const next = el.querySelector(`[data-fid="${fid}"]`); if (next && !next.disabled) next.focus({ preventScroll: true }); }
  }
  function reconcile(parent, items, keyOf, viewOf, tag) {
    const have = new Map($$(':scope > [data-key]', parent).map((e) => [e.dataset.key, e]));
    items.forEach((item, i) => {
      const k = keyOf(item);
      let el = have.get(k);
      if (el) have.delete(k); else { el = document.createElement(tag); el.dataset.key = k; }
      patch(el, viewOf(item));
      if (parent.children[i] !== el) parent.insertBefore(el, parent.children[i] || null);
    });
    have.forEach((el) => el.remove());
  }
  const setText = (el, text) => { if (el && el.textContent !== text) el.textContent = text; };

  // ----- loading -----
  async function loadLive(force) {
    try {
      const out = await request('GET', '/api/accounts/live' + (X.wiz ? '?setup=1' : ''), undefined, force ? null : X.live.etag);
      if (!out.notModified) { X.live.data = out.data; X.live.etag = out.etag; if (out.data.setup && X.wiz) X.wiz.setup = out.data.setup; }
      X.live.loaded = true; X.live.error = false;
    } catch (e) { X.live.error = true; }
    renderAccounts();
  }
  function seedsURL() { return `/api/seeds?status=${X.filter}&sort=${X.sort}&limit=${X.limit}` + (X.q ? '&q=' + encodeURIComponent(X.q) : ''); }
  async function loadSeeds(force) {
    const url = seedsURL();
    try {
      const out = await request('GET', url, undefined, force || X.seeds.url !== url ? null : X.seeds.etag);
      if (!out.notModified && url === seedsURL()) {
        Object.assign(X.seeds, { items: out.data.items, counts: out.data.counts, total: out.data.total, scope: out.data.scope, queue: out.data.queue, etag: out.etag, url });
        X.ui.sel.forEach((h) => { if (!X.seeds.items.some((s) => s.handle === h)) X.ui.sel.delete(h); });
      }
      X.seeds.loaded = true; X.seeds.error = false;
    } catch (e) { X.seeds.error = true; }
    renderSeeds();
  }
  const refresh = () => Promise.all([loadLive(true), loadSeeds(true)]);
  function schedule() {
    clearTimeout(X.timer);
    if (!X.active) return;
    X.timer = setTimeout(async () => {
      if (!document.hidden && !X.inflight) await Promise.all([loadLive(), loadSeeds()]);
      schedule();
    }, X.wiz ? 2000 : 4000);
  }

  // ----- accounts section -----
  function renderAccounts() {
    const data = X.live.data;
    const cards = $('#acx-cards'), sum = $('#acx-ig-sum');
    if (!data) {
      if (X.live.error) { cards.innerHTML = `<div class="acx-fail" role="alert">Couldn't load your accounts. <button type="button" class="acx-link" data-retry="live">Try again</button></div>`; cards.__sig = null; setText(sum, ''); return; }
      if (!cards.querySelector('.acx-skel')) cards.innerHTML = '<div class="acx-card acx-skel" aria-hidden="true"><i></i><i></i><i></i></div><div class="acx-card acx-skel" aria-hidden="true"><i></i><i></i><i></i></div>';
      return;
    }
    if (cards.querySelector('.acx-skel') || cards.querySelector('.acx-fail')) cards.innerHTML = '';
    const now = Date.now();
    const shown = data.cards.filter((c) => !c.disconnected), gone = data.cards.filter((c) => c.disconnected);
    reconcile(cards, shown, (c) => c.lane_id, (c) => cardView(c, X.ui, now), 'article');
    const s = data.summary;
    const parts = [];
    if (s.collecting) parts.push(`${s.collecting} collecting`);
    if (s.attention) parts.push(`${s.attention} ${s.attention === 1 ? 'needs' : 'need'} you`);
    if (!parts.length && s.collectors) parts.push(s.online ? `${plural(s.online, 'account')} connected` : 'None connected');
    setText(sum, s.total ? parts.join(' · ') : '');
    // Empty state: no accounts and no sequence running opens the sequence itself.
    $('#acx-empty').hidden = !!shown.length || !!X.wiz;
    const ws = data.workspace, note = $('#acx-notice');
    let msg = '';
    if (ws.shared_wait && ws.shared_wait.until) msg = `Instagram asked every account to pause. Collection resumes by itself around ${clock(ws.shared_wait.until)}.`;
    else if (ws.startup && ws.startup.state === 'failed') msg = ws.startup.message || "Couldn't open the Chrome profile. Try again.";
    else if (ws.warning) msg = 'Collection is stopped until each warned account has been reviewed.';
    setText(note, msg); note.hidden = !msg;
    const main = data.main;
    $('#acx-main-note').hidden = !main;
    if (main) setText($('#acx-main-note'), `Your main account${main.handle ? ', @' + main.handle + ',' : ''} is never used for collecting. It appears here only so you can open Instagram from it.`);
    const g = $('#acx-gone');
    g.hidden = !gone.length;
    if (gone.length) {
      setText($('#acx-gone-n'), String(gone.length));
      reconcile($('#acx-gone-list'), gone, (c) => c.lane_id, (c) => cardView(c, X.ui, now), 'article');
    }
    if (X.wiz) renderWizard();
  }
  function renderWizard() {
    const box = $('#acx-wiz');
    const data = X.live.data;
    box.hidden = !X.wiz;
    $('#acx-add-account').hidden = !!X.wiz;
    if (!X.wiz || !data) return;
    const html = wizardHTML(X.wiz, data.cards, X.wiz.setup, Date.now());
    if (box.__html !== html) { box.innerHTML = html; box.__html = html; }
  }
  function openWizard() {
    X.wiz = { base: new Set((X.live.data ? X.live.data.cards : []).map((c) => c.lane_id)), started: Date.now(), setup: null };
    renderAccounts();
    loadLive(true); schedule();
    $('#acx-wiz').scrollIntoView({ block: 'nearest', behavior: reduced() ? 'auto' : 'smooth' });
  }
  function closeWizard() { X.wiz = null; renderAccounts(); schedule(); }

  async function cardAction(lane, id) {
    const data = X.live.data, card = data && data.cards.find((c) => c.lane_id === lane);
    if (!card) return;
    const ui = X.ui;
    const snapshot = JSON.parse(JSON.stringify(data));
    delete ui.errors[lane];
    const optimistic = (patchCard) => { Object.assign(card, patchCard); renderAccounts(); };
    ui.busy.add(lane); X.inflight++;
    try {
      if (id === 'pause') {
        optimistic({ state: 'paused', tone: 'quiet', headline: 'Paused', detail: 'Resume when you want it to collect again.', until: null, paused: true, action: { id: 'resume', label: 'Resume' } });
        await write(`/api/accounts/${encodeURIComponent(lane)}`, { paused: true });
      } else if (id === 'resume') {
        optimistic({ state: 'ready', tone: 'quiet', headline: 'Resuming', detail: 'Checking with the extension…', until: null, paused: false, action: { id: 'pause', label: 'Pause' } });
        await write(`/api/accounts/${encodeURIComponent(lane)}`, { paused: false });
      } else if (id === 'connect') {
        optimistic({ state: 'connecting', tone: 'wait', headline: 'Connecting', detail: 'Opening its Chrome profile. This can take up to 90 seconds.', action: null });
        await write(`/api/accounts/${encodeURIComponent(lane)}/connect`, {});
      } else if (id === 'reconnect') {
        optimistic({ disconnected: false, state: 'ready', tone: 'quiet', headline: 'Reconnecting', detail: 'Checking with the extension…', action: null });
        await write(`/api/accounts/${encodeURIComponent(lane)}/reconnect`, {});
      } else if (id === 'disconnect') {
        optimistic({ disconnected: true, state: 'disconnected', tone: 'quiet', headline: 'Disconnected', detail: 'Not used for collecting. Its Chrome profile can stay open.', action: { id: 'reconnect', label: 'Reconnect' } });
        await write(`/api/accounts/${encodeURIComponent(lane)}/disconnect`, {});
        toast(`Disconnected ${card.handle ? '@' + card.handle : 'the account'}.`, () => cardAction(lane, 'reconnect'));
      } else if (id === 'forget') {
        if (ui.confirm !== lane + ':forget') { ui.confirm = lane + ':forget'; setTimeout(() => { if (ui.confirm === lane + ':forget') { ui.confirm = null; renderAccounts(); } }, 4000); renderAccounts(); return; }
        ui.confirm = null;
        await write(`/api/accounts/${encodeURIComponent(lane)}/forget`, {});
        toast('Account forgotten.');
      } else if (id === 'acknowledge') {
        await write('/api/control', { action: 'acknowledge_scraping_warning', account: lane, reviewed: true });
        toast('Review saved. Collection stays stopped until you start it.');
        root.dispatchEvent(new Event('fl:control-changed'));
      }
    } catch (e) {
      X.live.data = snapshot;
      ui.errors[lane] = sentence(e, "Couldn't do that. Try again.");
      ui.retry = ui.retry || {}; ui.retry[lane] = id;
    } finally { ui.busy.delete(lane); X.inflight--; }
    await loadLive(true);
  }
  async function cardEdit(lane, body, done) {
    const snapshot = JSON.parse(JSON.stringify(X.live.data));
    X.inflight++;
    try { await write(`/api/accounts/${encodeURIComponent(lane)}`, body); if (done) done(); }
    catch (e) { X.live.data = snapshot; X.ui.errors[lane + ':limit'] = sentence(e, "Couldn't save. Try again."); }
    finally { X.inflight--; }
    await loadLive(true);
  }
  function saveLimits(card, inputs) {
    const body = {};
    for (const input of inputs) {
      const key = input.dataset.limit, max = key === 'list' ? 3000 : 5000, v = input.value.trim();
      if (v === '') { body[key] = null; continue; }
      if (!/^\d+$/.test(v) || +v < 1 || +v > max) { X.ui.errors[card.lane_id + ':limit'] = `Use a whole number from 1 to ${int(max)}.`; renderAccounts(); return; }
      body[key] = +v;
    }
    delete X.ui.errors[card.lane_id + ':limit'];
    cardEdit(card.lane_id, { budget: body }, () => toast('Daily limits saved.'));
  }

  // ----- profiles section -----
  function scopeFollowingOnly() { return X.seeds.scope === 'following'; }
  function renderPaste() {
    const p = X.paste, items = p.items, c = summarize(items);
    const box = $('#acx-preview');
    box.hidden = !items.length;
    $$('#acx-dirs button').forEach((b) => {
      const off = scopeFollowingOnly() && b.dataset.v === 'followers';
      b.setAttribute('aria-pressed', String(p.dirs.has(b.dataset.v) && !off)); b.classList.toggle('on', p.dirs.has(b.dataset.v) && !off);
      b.disabled = off || p.adding; b.title = off ? 'Following only is on. Change it in the collection bar.' : '';
    });
    const add = $('#acx-add-btn');
    add.disabled = !c.ready || p.adding || !activeDirs().length;
    setText(add, c.ready ? `Add ${plural(c.ready, 'profile')}` : 'Add');
    if (!items.length) { box.innerHTML = ''; box.__html = ''; return; }
    const chips = items.slice(0, 60).map((i) => {
      const tone = ['new', 'extend', 'restore'].includes(i.state) ? 'ready' : i.state === 'exists' ? 'have' : 'skip';
      const label = i.state === 'invalid' ? i.input : '@' + i.handle;
      const why = i.renamed_from ? `now @${i.handle}` : i.state === 'extend' ? 'adds ' + i.add.map((d) => DIR_LABEL[d].toLowerCase()).join(' and ') : i.state === 'restore' ? 'comes back' : i.message || (i.warn && i.warn.includes('private') ? 'looks private' : i.warn && i.warn.includes('yours') ? 'your account' : '');
      return `<li class="acx-pchip" data-tone="${tone}" title="${esc(i.message || '')}"><b>${esc(i.renamed_from ? '@' + i.renamed_from : label)}</b>${why ? `<span>${esc(why)}</span>` : ''}${i.warn && i.warn.includes('private') && !i.message && i.state !== 'invalid' ? '<span>looks private</span>' : ''}</li>`;
    }).join('');
    const html = `<p class="acx-psum" role="status">${esc(summaryText(c) || 'Nothing to add yet.')}${p.checking ? '' : ''}</p><ul class="acx-pchips">${chips}${items.length > 60 ? `<li class="acx-pchip" data-tone="skip"><span>+${items.length - 60} more</span></li>` : ''}</ul>${p.error ? `<p class="acx-error" role="alert">${esc(p.error)}</p>` : ''}`;
    if (box.__html !== html) { box.innerHTML = html; box.__html = html; }
  }
  const activeDirs = () => DIRS.filter((d) => X.paste.dirs.has(d) && !(scopeFollowingOnly() && d === 'followers'));
  let checkTimer = 0;
  function onPasteInput() {
    const text = $('#acx-paste').value;
    X.paste.text = text; X.paste.error = '';
    X.paste.items = readPaste(text);
    renderPaste();
    clearTimeout(checkTimer);
    if (!X.paste.items.some((i) => i.handle)) return;
    const ticket = ++X.paste.checking;
    checkTimer = setTimeout(async () => {
      try {
        const out = await write('/api/seeds/check', { text, directions: activeDirs().length ? activeDirs() : DIRS });
        if (ticket === X.paste.checking && X.paste.text === text) { X.paste.items = out.items; renderPaste(); }
      } catch { /* the instant reading stays */ }
    }, 220);
  }
  function newRequestId() {
    const a = new Uint8Array(12); (root.crypto || {}).getRandomValues ? root.crypto.getRandomValues(a) : a.forEach((_, i) => { a[i] = Math.floor(Math.random() * 256); });
    return 'r' + [...a].map((b) => b.toString(16).padStart(2, '0')).join('');
  }
  async function addProfiles() {
    const p = X.paste, ready = p.items.filter((i) => ['new', 'extend', 'restore'].includes(i.state));
    const dirs = activeDirs();
    if (!ready.length || !dirs.length || p.adding) return;
    const text = $('#acx-paste').value, requestId = newRequestId();
    const before = X.seeds.items;
    // Instant: rows appear now, the box clears, the server confirms in the background.
    const fresh = ready.filter((i) => !before.some((s) => s.handle === i.handle)).map((i) => provisional(i, dirs));
    X.seeds.items = [...fresh, ...before.filter((s) => !fresh.some((f) => f.handle === s.handle))];
    $('#acx-paste').value = ''; p.text = ''; p.items = []; p.error = ''; p.adding = false; X.inflight++;
    renderPaste(); renderSeeds();
    try {
      const out = await write('/api/seeds/batch', { text, directions: dirs, request_id: requestId });
      const added = out.added;
      const failed = out.items.filter((i) => i.result === 'failed');
      const stopped = X.live.data && X.live.data.workspace.stopped;
      if (failed.length) toast(`Added ${plural(added, 'profile')}. ${plural(failed.length, 'profile')} couldn't be added: ${failed.map((f) => '@' + f.handle).slice(0, 3).join(', ')}.`);
      else if (!added) toast('Those profiles were already added.');
      else toast(`Added ${plural(added, 'profile')}.${stopped ? ' Start collection to begin.' : ''}`, () => undoAdd(requestId, text, dirs));
    } catch (e) {
      X.seeds.items = before;
      $('#acx-paste').value = text; p.text = text; p.items = readPaste(text);
      p.error = sentence(e, "Couldn't add these profiles. Your list is still here.") + ' Your list is still here.';
      renderPaste();
      $('#acx-paste').focus();
    } finally { X.inflight--; }
    await loadSeeds(true);
  }
  async function undoAdd(requestId, text, dirs) {
    X.inflight++;
    try { await write('/api/seeds/undo', { request_id: requestId }); toast('Removed what you just added.'); }
    catch (e) { toast(sentence(e, "Couldn't undo that.")); }
    finally { X.inflight--; }
    await loadSeeds(true);
  }

  async function seedAct(action, handles, extra = {}, label) {
    if (!handles.length) return;
    const before = X.seeds.items, counts = X.seeds.counts;
    X.seeds.items = applyLocal(before, action, handles, extra.directions);
    if (action === 'remove') handles.forEach((h) => X.ui.sel.delete(h));
    renderSeeds(); X.inflight++;
    let failed = false;
    try {
      const out = await write('/api/seeds/act', Object.assign({ action, handles }, extra));
      const missing = out.items.filter((i) => i.result === 'missing').length;
      const word = { pause: 'Paused', resume: 'Resumed', remove: 'Removed', restore: 'Restored', next: 'Moved to the front:', directions: 'Updated' }[action];
      const who = handles.length === 1 ? '@' + handles[0] : plural(handles.length, 'profile');
      const undo = { pause: () => seedAct('resume', handles), resume: () => seedAct('pause', handles), remove: () => seedAct('restore', handles) }[action];
      if (action !== 'directions') toast(`${word} ${who}.${missing ? ` ${plural(missing, 'profile')} no longer exist${missing === 1 ? 's' : ''}.` : ''}`, undo);
    } catch (e) {
      failed = true; X.seeds.items = before; X.seeds.counts = counts;
      toast(sentence(e, `Couldn't ${action === 'next' ? 'move' : action} ${handles.length === 1 ? '@' + handles[0] : 'those profiles'}. Try again.`), null, { error: true });
    } finally { X.inflight--; }
    await loadSeeds(true);
    return !failed;
  }

  function renderSeeds() {
    const S = X.seeds, list = $('#acx-list');
    const counts = S.counts;
    // filters
    const fl = $('#acx-filters');
    const labels = [['all', 'All'], ['collecting', 'Collecting'], ['waiting', 'Waiting'], ['paused', 'Paused'], ['attention', 'Needs a look'], ['done', 'Done']];
    const fhtml = labels.map(([k, l]) => `<button type="button" data-filter="${k}" aria-pressed="${X.filter === k}" class="${X.filter === k ? 'on' : ''}">${l}${counts ? ` <span class="num">${counts[k]}</span>` : ''}</button>`).join('')
      + (counts && counts.removed ? `<button type="button" data-filter="removed" aria-pressed="${X.filter === 'removed'}" class="${X.filter === 'removed' ? 'on' : ''} acx-removed">Removed <span class="num">${counts.removed}</span></button>` : '');
    if (fl.__html !== fhtml) { const f = document.activeElement && fl.contains(document.activeElement) ? document.activeElement.dataset.filter : null; fl.innerHTML = fhtml; fl.__html = fhtml; if (f) { const b = fl.querySelector(`[data-filter="${f}"]`); if (b) b.focus({ preventScroll: true }); } }
    setText($('#acx-count'), counts ? [plural(counts.all, 'profile'), queueText(S.queue)].filter(Boolean).join(' · ') : '');
    // list
    const empty = $('#acx-empty-list');
    if (!S.loaded) {
      if (!list.querySelector('.acx-skel-row')) { list.innerHTML = Array.from({ length: 6 }, () => '<li class="acx-skel-row" aria-hidden="true"><i></i><i></i></li>').join(''); list.__skel = true; }
      empty.hidden = true; return;
    }
    if (list.__skel) { list.innerHTML = ''; list.__skel = false; }
    if (S.error && !S.items.length) { empty.hidden = false; empty.innerHTML = `Couldn't load your profiles. <button type="button" class="acx-link" data-retry="seeds">Try again</button>`; return; }
    reconcile(list, S.items, (s) => s.handle, (s) => rowView(s, X.ui), 'li');
    const none = !S.items.length;
    empty.hidden = !none;
    if (none) {
      const filtered = X.q || X.filter !== 'all';
      empty.innerHTML = filtered ? `<b>No profiles match.</b> <button type="button" class="acx-link" data-retry="clear">Clear search and filters</button>` : `<b>No profiles yet.</b><span>Paste handles or links above to start.</span>`;
    }
    const more = $('#acx-more-btn');
    more.hidden = S.total <= S.items.length;
    setText(more, `Show more · ${int(S.total - S.items.length)} left`);
    renderBulk();
  }
  function renderBulk() {
    const bar = $('#acx-bulk'), n = X.ui.sel.size;
    bar.hidden = !n;
    setText($('#acx-bulk-n'), `${n} selected`);
    const removedView = X.filter === 'removed';
    $$('[data-bulk]', bar).forEach((b) => { b.hidden = removedView ? b.dataset.bulk !== 'restore' && b.dataset.bulk !== 'clear' : b.dataset.bulk === 'restore'; });
    $('#acx-bulk-dirs').hidden = removedView;
  }

  // ----- events -----
  function onListClick(e) {
    const row = e.target.closest('.acx-row');
    if (!row) return;
    const handle = row.dataset.handle, s = X.seeds.items.find((x) => x.handle === handle);
    if (!s) return;
    const act = e.target.closest('[data-act]');
    const ui = X.ui;
    if (e.target.closest('[data-sel]')) {
      const input = e.target.closest('[data-sel]');
      if (e.shiftKey && ui.lastSel) {
        const hs = X.seeds.items.map((x) => x.handle), a = hs.indexOf(ui.lastSel), b = hs.indexOf(handle);
        hs.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((h) => ui.sel.add(h));
      } else if (input.checked) ui.sel.add(handle); else ui.sel.delete(handle);
      ui.lastSel = handle; ui.focus = handle; renderSeeds(); return;
    }
    if (e.target.closest('.acx-check')) return;
    if (e.target.closest('[data-dir]')) {
      const dirs = $$('[data-dir]', row).filter((i) => i.checked).map((i) => i.dataset.dir);
      if (!dirs.length) { e.preventDefault(); toast('Keep at least one list. Remove the profile to stop it.'); return; }
      seedAct('directions', [handle], { directions: dirs }); return;
    }
    if (e.target.closest('a')) return;
    if (!act) { if (!e.target.closest('button,input,label')) { toggleOpen(handle); } return; }
    const a = act.dataset.act;
    if (a === 'toggle') toggleOpen(handle);
    else if (a === 'remove') seedAct('remove', [handle]);
    else seedAct(a, [handle]);
  }
  function toggleOpen(handle) { X.ui.open.has(handle) ? X.ui.open.delete(handle) : X.ui.open.add(handle); X.ui.focus = handle; renderSeeds(); }
  function onListKey(e) {
    const row = e.target.closest('.acx-row');
    if (!row || e.target.closest('input,select,textarea,a') && e.key !== 'Escape') return;
    const rows = $$('.acx-row', $('#acx-list')), i = rows.indexOf(row);
    const move = (to) => { if (rows[to]) { X.ui.focus = rows[to].dataset.handle; rows.forEach((r) => r.setAttribute('tabindex', r === rows[to] ? '0' : '-1')); rows[to].focus(); } };
    const handle = row.dataset.handle;
    switch (e.key) {
      case 'ArrowDown': e.preventDefault(); e.stopPropagation(); move(i + 1); break;
      case 'ArrowUp': e.preventDefault(); e.stopPropagation(); move(i - 1); break;
      case ' ': if (e.target === row) { e.preventDefault(); e.stopPropagation(); X.ui.sel.has(handle) ? X.ui.sel.delete(handle) : X.ui.sel.add(handle); X.ui.lastSel = handle; renderSeeds(); } break;
      case 'Enter': if (e.target === row) { e.preventDefault(); e.stopPropagation(); toggleOpen(handle); } break;
      case 'Delete': case 'Backspace': if (e.target === row) { e.preventDefault(); e.stopPropagation(); seedAct('remove', X.ui.sel.has(handle) ? [...X.ui.sel] : [handle]); } break;
      case 'Escape': if (X.ui.sel.size) { e.stopPropagation(); X.ui.sel.clear(); renderSeeds(); } break;
      default:
    }
  }
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  function bind() {
    $('#acx-paste').addEventListener('input', onPasteInput);
    $('#acx-paste').addEventListener('paste', () => setTimeout(onPasteInput, 0));
    $('#acx-paste').addEventListener('keydown', (e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); addProfiles(); } e.stopPropagation(); });
    $('#acx-add-form').addEventListener('submit', (e) => { e.preventDefault(); addProfiles(); });
    $('#acx-dirs').addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b || b.disabled) return;
      const d = b.dataset.v, set = X.paste.dirs;
      if (set.has(d) && set.size === 1) return;
      set.has(d) ? set.delete(d) : set.add(d);
      safeSet('acx-dirs', JSON.stringify([...set]));
      renderPaste(); onPasteInput();
    });
    $('#acx-q').addEventListener('input', debounce((e) => { X.q = e.target.value.trim(); X.limit = 200; loadSeeds(true); }, 160));
    $('#acx-q').addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.target.value = ''; X.q = ''; loadSeeds(true); } e.stopPropagation(); });
    $('#acx-sort').addEventListener('change', (e) => { X.sort = e.target.value; loadSeeds(true); });
    $('#acx-filters').addEventListener('click', (e) => { const b = e.target.closest('[data-filter]'); if (b) { X.filter = b.dataset.filter; X.limit = 200; X.ui.sel.clear(); loadSeeds(true); } });
    $('#acx-list').addEventListener('click', onListClick);
    $('#acx-list').addEventListener('keydown', onListKey);
    $('#acx-list').addEventListener('error', (e) => { if (e.target.tagName === 'IMG') e.target.remove(); }, true);
    view.addEventListener('error', (e) => { if (e.target.tagName === 'IMG') e.target.remove(); }, true);
    $('#acx-more-btn').addEventListener('click', () => { X.limit += 200; loadSeeds(true); });
    $('#acx-empty-list').addEventListener('click', (e) => {
      if (e.target.closest('[data-retry="clear"]')) { X.q = ''; X.filter = 'all'; $('#acx-q').value = ''; loadSeeds(true); }
      if (e.target.closest('[data-retry="seeds"]')) loadSeeds(true);
    });
    $('#acx-bulk').addEventListener('click', (e) => {
      const b = e.target.closest('[data-bulk]'); if (!b) return;
      const hs = [...X.ui.sel];
      if (b.dataset.bulk === 'clear') { X.ui.sel.clear(); renderSeeds(); return; }
      seedAct(b.dataset.bulk, hs);
    });
    $('#acx-bulk-dirs').addEventListener('change', (e) => {
      const v = e.target.value; e.target.value = '';
      if (!v) return;
      seedAct('directions', [...X.ui.sel], { directions: v === 'both' ? DIRS : [v] });
    });
    $('#acx-cards').addEventListener('click', onCardClick);
    $('#acx-gone-list').addEventListener('click', onCardClick);
    view.addEventListener('click', (e) => { if (e.target.closest('[data-retry="live"]')) loadLive(true); });
    for (const box of [$('#acx-cards'), $('#acx-gone-list')]) {
      box.addEventListener('change', onCardChange);
      box.addEventListener('keydown', (e) => {
        if (e.target.matches('[data-limit],[data-label]')) {
          e.stopPropagation();
          if (e.key === 'Enter') e.target.blur();
          if (e.key === 'Escape') { e.target.value = e.target.defaultValue; e.target.blur(); }
        }
      });
      box.addEventListener('focusout', () => setTimeout(() => $$('[data-key]', box).forEach((el) => { if (el.__stale && !editing(el)) renderAccounts(); }), 0));
    }
    $('#acx-add-account').addEventListener('click', openWizard);
    $('#acx-empty-add').addEventListener('click', openWizard);
    $('#acx-wiz').addEventListener('click', (e) => {
      if (e.target.closest('[data-wiz="close"]')) return closeWizard();
      const c = e.target.closest('[data-copy]');
      if (c) copy(c.dataset.copy, c);
    });
    document.addEventListener('visibilitychange', () => { if (X.active && !document.hidden) refresh(); });
    root.addEventListener('fl:control-changed', () => { if (X.active) refresh(); });
    root.addEventListener('keydown', (e) => { if (X.active && e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test((e.target.tagName || ''))) { e.preventDefault(); e.stopPropagation(); $('#acx-q').focus(); } }, true);
  }
  async function copy(text, btn) {
    let ok = false;
    try { await navigator.clipboard.writeText(text); ok = true; } catch {
      const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
      try { ok = document.execCommand('copy'); } catch { /* no clipboard */ } ta.remove();
    }
    btn.textContent = ok ? 'Copied' : "Couldn't copy"; setTimeout(() => { btn.textContent = 'Copy'; }, 1400);
  }
  function onCardClick(e) {
    const el = e.target.closest('[data-card]'); if (!el) return;
    const lane = el.closest('[data-lane]').dataset.lane, id = el.dataset.card;
    if (id === 'details') { X.ui.openCards.has(lane) ? X.ui.openCards.delete(lane) : X.ui.openCards.add(lane); renderAccounts(); return; }
    if (id === 'inbox') return openInbox(lane, e);
    if (id === 'retry') { const again = X.ui.retry && X.ui.retry[lane]; delete X.ui.errors[lane]; if (again) cardAction(lane, again); return; }
    const m = e.target.closest('[data-reads]'); if (m) return;
    cardAction(lane, id);
  }
  function onCardChange(e) {
    const el = e.target, card = el.closest('[data-lane]'); if (!card) return;
    const lane = card.dataset.lane, data = X.live.data && X.live.data.cards.find((c) => c.lane_id === lane);
    if (!data) return;
    if (el.matches('[data-limit]')) saveLimits(data, $$('[data-limit]', card));
    else if (el.matches('[data-label]')) { const v = el.value.trim(); if (v !== (data.label || '')) cardEdit(lane, { label: v || null }, () => toast(v ? `Renamed to ${v}.` : 'Name cleared.')); }
  }
  document.addEventListener('click', (e) => {
    const r = e.target.closest('[data-reads]'); if (!r || !view.contains(r)) return;
    const lane = r.closest('[data-lane]').dataset.lane;
    cardEdit(lane, { role: r.dataset.reads });
  });
  async function openInbox(lane, e) {
    const env = { port: location.port, chrome: root.chrome, toast, setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (t) => clearTimeout(t),
      getSetup: async () => (await request('GET', '/api/setup')).data };
    if (env.port !== '8777' || !(root.chrome && root.chrome.runtime && root.chrome.runtime.sendMessage)) return;
    e.preventDefault();
    await openInstagramInbox(lane, env);
  }

  // ----- clock: countdowns and ages change text only, never layout -----
  function tickClock() {
    if (!X.active || document.hidden) return;
    const now = Date.now();
    $$('[data-until]', view).forEach((b) => {
      const card = X.live.data && X.live.data.cards.find((c) => c.lane_id === b.closest('[data-lane]').dataset.lane);
      if (!card || !card.until) return;
      if (Date.parse(card.until) <= now && card.state === 'waiting') { setText(b, 'Back any moment'); return; }
      setText(b, headlineText(card, now));
    });
    $$('[data-seen]', view).forEach((s) => setText(s, relative(s.dataset.seen, now)));
  }

  // ----- public -----
  function show() {
    if (!X.active) { X.active = true; if (!X.bound) { bind(); X.bound = true; } }
    renderPaste(); renderSeeds(); renderAccounts();
    refresh(); schedule();
    clearInterval(X.tick); X.tick = setInterval(tickClock, 1000);
  }
  function hide() { X.active = false; clearTimeout(X.timer); clearInterval(X.tick); if (X.wiz) closeWizard(); }
  root.AccountsPage = { show, hide, refresh, state: X };
  if (view.classList.contains('on')) show();
})(typeof window !== 'undefined' ? window : globalThis);
