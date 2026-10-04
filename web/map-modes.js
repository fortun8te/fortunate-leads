/* Map view modes: what a mode means, how its page is ordered and sized, and the segmented control.
 * A mode is a pair of questions about the same network: how close is this person to me, and how big
 * are they. Each mode names the slice of people it draws from (a "lens" on the server), the ordering
 * that places them from the centre outwards, and the size scale. Nothing here touches the canvas. */
(function (root) {
  'use strict';
  const Core = root.MapCore || (typeof require === 'function' ? require('./map-core.js') : null);
  const { clamp, int, plural, compact } = Core;

  // Descending, with missing values after every real value.
  const known = (v) => v != null && Number.isFinite(+v);
  const high = (a, b) => (known(b) ? +b : -Infinity) - (known(a) ? +a : -Infinity) || 0;
  const tie = (a, b) => (b.closeness || 0) - (a.closeness || 0) || (b.rank || 0) - (a.rank || 0) || String(a.id).localeCompare(String(b.id));
  const safe = (d) => (Number.isNaN(d) ? 0 : d);

  const MODES = [
    { id: 'network', closer: 'Closer: recorded follows', label: 'Closest to me', size: 'followers', lens: '',
      line: 'Mutual follows first, then people who follow you, then people you follow.',
      help: 'Distance orders this page by recorded follows with you: mutual follows first, then accounts that follow you, then accounts you follow, then known contacts and shared sources. Recorded follows do not establish a personal relationship. Larger means more saved followers.',
      distance: 'Recorded follows with me', coverage: 'followers',
      order: (a, b) => Core.networkEvidenceTier(a) - Core.networkEvidenceTier(b) || safe(high(a.closeness, b.closeness)) || safe(high(a.rank, b.rank)) || String(a.id).localeCompare(String(b.id)) },
    { id: 'reach', closer: 'Closer: more followers', label: 'Biggest reach', size: 'reach', lens: 'reach',
      line: 'Largest follower counts nearest the centre. Only people with a saved count appear.',
      help: 'Every saved person with a follower count, largest first. Bubble size uses the same scale, so the spread is wide. Counts come from profile reads, so most saved people have none yet.',
      distance: 'Follower count', coverage: null,
      order: (a, b) => safe(high(a.followers, b.followers)) || tie(a, b) },
    { id: 'warm', closer: 'Closer: more links to your connections', label: 'Warm paths', size: 'followers', lens: 'warm',
      line: 'People your connections link to. More links to your connections sit nearer.',
      help: 'Counts how many of the people you follow, or who follow you, have this person in a collected list. It shows where an introduction could come from. It is not a relationship.',
      distance: 'Links to my connections', coverage: 'followers',
      order: (a, b) => safe(high(a.links, b.links)) || safe(high(a.source_count, b.source_count)) || safe(high(a.followers, b.followers)) || tie(a, b) },
    { id: 'shared', closer: 'Closer: more shared audiences', label: 'Shared audiences', size: 'connections', lens: 'shared',
      line: 'People in the most collected audiences sit nearest. Sharing an audience is not a friendship.',
      help: 'Counts the distinct collected source audiences that contain this person. Only people seen in two or more appear. Shared audiences are not mutual friends.',
      distance: 'Collected source audiences', coverage: null,
      order: (a, b) => safe(high(a.source_count, b.source_count)) || tie(a, b) },
    { id: 'leads', closer: 'Closer: recorded follows', label: 'Promising leads', size: 'score', lens: 'leads',
      line: 'High and medium priority leads under 100,000 followers, nearest to you first.',
      help: 'High and medium priority leads with fewer than 100,000 followers or an unknown count, ordered by recorded follows with you. Larger means a higher priority.',
      distance: 'Recorded follows with me', coverage: null,
      order: (a, b) => Core.networkEvidenceTier(a) - Core.networkEvidenceTier(b) || safe(high(a.closeness, b.closeness)) || safe(high(a.priority, b.priority)) || String(a.id).localeCompare(String(b.id)) }
  ];
  const DEFAULT = MODES[0];
  // Earlier builds saved these view names.
  const RETIRED = { fit: 'leads' };
  const byId = (id) => MODES.find((mode) => mode.id === id) || null;
  const normalize = (id) => (byId(id) ? id : RETIRED[id] || DEFAULT.id);
  const get = (id) => byId(normalize(id));

  /* ---------- sizes that only some modes use ---------- */
  const SIZES = [
    { id: 'reach', label: 'Followers, wide range', help: 'Size follows the saved follower count on a wide scale. People without a count are smallest.' },
    { id: 'score', label: 'Priority', help: 'Larger bubbles have a higher lead priority. Unscored people are smallest.' }
  ];
  const sizeFns = {
    reach: (n) => (known(n.followers) ? 9 + 21 * clamp((Math.log10(1 + Math.max(0, +n.followers)) - 2) / 6.5, 0, 1) : 8),
    score: (n) => (known(n.priority) ? 9 + 19 * clamp((+n.priority - 40) / 50, 0, 1) : 7)
  };
  function register() {
    for (const size of SIZES) {
      if (!Core.SIZE_OPTIONS.some((o) => o.id === size.id)) Core.SIZE_OPTIONS.push({ id: size.id, label: size.label });
      Core.SIZE_HELP[size.id] = size.help;
    }
    Object.assign(Core.SIZE_FN, sizeFns);
  }
  if (Core.SIZE_FN) register();

  /* ---------- per-person text ---------- */
  function metricText(person, id) {
    const hasFollowers = known(person.followers) && +person.followers >= 0, hasSources = known(person.source_count) && +person.source_count >= 0;
    const followers = hasFollowers ? `${int(person.followers)} followers` : null;
    const sources = hasSources ? plural(+person.source_count, 'source audience', 'source audiences') : null;
    switch (normalize(id)) {
      case 'warm': return person.links > 0 ? `${plural(person.links, 'link')}${hasFollowers ? ` · ${compact(person.followers)} followers` : ''}` : followers || sources || 'Followers unknown';
      case 'shared': return sources || followers || 'Followers unknown';
      case 'leads': return known(person.priority) ? `Priority ${Math.round(person.priority)}` : followers || sources || 'Followers unknown';
      default: return followers || sources || 'Followers unknown';
    }
  }

  // "139 of 500 have a saved follower count" for the size or distance a mode relies on, else nothing.
  function coverage(id, nodes) {
    const kind = get(id).coverage;
    if (!kind) return null;
    const people = nodes.filter((n) => !n.guest);
    if (!people.length) return null;
    const have = people.filter((n) => known(n.followers)).length;
    if (have === people.length) return null;
    return `${int(have)} of ${int(people.length)} have a saved follower count.`;
  }

  /* ---------- the control ---------- */
  // Fill the segmented control (and its description line) already in the page, or create them.
  function build(doc, onPick, existing = {}) {
    const el = (tag, attrs = {}, ...kids) => {
      const node = doc.createElement(tag);
      for (const [k, v] of Object.entries(attrs)) { if (k === 'text') node.textContent = v; else node.setAttribute(k, v); }
      node.append(...kids);
      return node;
    };
    const group = existing.group || el('div', { class: 'mv-modes seg', role: 'radiogroup', 'aria-label': 'Map view', id: 'map-modes' });
    group.replaceChildren?.();
    const buttons = MODES.map((mode) => {
      const b = el('button', { type: 'button', role: 'radio', 'data-mode': mode.id, 'aria-checked': 'false', tabindex: '-1', title: mode.help, text: mode.label });
      group.append(b);
      return b;
    });
    const line = existing.line || el('p', { class: 'mv-modeline', id: 'map-modeline', 'aria-live': 'polite' });
    line.replaceChildren?.();
    const description = el('span', { class: 'mv-modeline-text' });
    const note = el('span', { class: 'mv-modeline-note' });
    line.append(description, note);
    const move = (from, step) => {
      const next = buttons[(from + step + buttons.length) % buttons.length];
      next.focus?.();
      onPick(next.getAttribute('data-mode'));
    };
    group.addEventListener('click', (e) => {
      const b = e.target?.closest?.('[data-mode]');
      if (b) onPick(b.getAttribute('data-mode'));
    });
    group.addEventListener('keydown', (e) => {
      const at = buttons.findIndex((b) => b.getAttribute('aria-checked') === 'true');
      const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
      if (step) { e.preventDefault(); move(at < 0 ? 0 : at, step); }
      else if (e.key === 'Home') { e.preventDefault(); buttons[0].focus?.(); onPick(buttons[0].getAttribute('data-mode')); }
      else if (e.key === 'End') { e.preventDefault(); buttons[buttons.length - 1].focus?.(); onPick(buttons[buttons.length - 1].getAttribute('data-mode')); }
    });
    function update(id, nodes) {
      const active = get(id);
      buttons.forEach((b) => {
        const on = b.getAttribute('data-mode') === active.id;
        b.setAttribute('aria-checked', String(on)); b.setAttribute('tabindex', on ? '0' : '-1');
        b.className = on ? 'on' : '';
      });
      description.textContent = active.line;
      note.textContent = coverage(active.id, nodes || []) || '';
      note.hidden = !note.textContent;
    }
    return { group, line, update, buttons };
  }

  const api = { MODES, DEFAULT, get, normalize, byId, metricText, coverage, build, sizeFns, SIZES, register,
    comparator: (id) => get(id).order, lens: (id) => get(id).lens };
  root.MapModes = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window === 'undefined' ? globalThis : window);
