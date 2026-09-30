// One icon set for the whole workspace: a 16px grid, 1.5px stroke, round caps and joins.
// Stroke and fill come from the global `svg` rule in app.css, so no icon sets its own weight.
// Text characters (arrows, crosses, ticks) are never used as icons.
(() => {
  const PATHS = {
    check: 'M3.5 8.5 6.5 11.5 12.5 4.5',
    close: 'M4 4l8 8M12 4l-8 8',
    plus: 'M8 3.5v9M3.5 8h9',
    minus: 'M3.5 8h9',
    chevronUp: 'M4 9.5 8 5.5l4 4',
    chevronDown: 'M4 6.5l4 4 4-4',
    chevronRight: 'M6.5 4l4 4-4 4',
    arrowUp: 'M8 13V3M4 7l4-4 4 4',
    arrowDown: 'M8 3v10M4 9l4 4 4-4',
    arrowLeft: 'M13 8H3M7 4 3 8l4 4',
    arrowRight: 'M3 8h10M9 4l4 4-4 4',
    external: 'M6.5 3.5h-2a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h7a1 1 0 0 0 1-1v-2M9 3h4v4M13 3 7.5 8.5',
    search: 'M7 11.5a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9zM10.5 10.5l3 3',
    filter: 'M2.5 4h11M4.5 8h7M6.5 12h3',
    note: 'M3 2.5h7l3 3v8H3zM10 2.5v3h3M5.5 8.5h5M5.5 11h3.5',
    clock: 'M8 13.5a5.5 5.5 0 1 0 0-11 5.5 5.5 0 0 0 0 11zM8 5v3l2 1.5',
    undo: 'M5.5 4 3 6.5 5.5 9M3 6.5h6a3.5 3.5 0 0 1 0 7H6',
    globe: 'M8 13.5a5.5 5.5 0 1 0 0-11 5.5 5.5 0 0 0 0 11zM2.5 8h11M8 2.5c1.7 1.6 2.5 3.4 2.5 5.5S9.7 11.9 8 13.5C6.3 11.9 5.5 10.1 5.5 8S6.3 4.1 8 2.5z',
    user: 'M8 7.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zM3 13.5c.6-2.3 2.4-3.5 5-3.5s4.4 1.2 5 3.5',
    pencil: 'M3 13l.5-2.8 7.2-7.2a1.4 1.4 0 0 1 2 0l.3.3a1.4 1.4 0 0 1 0 2L5.8 12.5z',
    trash: 'M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5',
    pause: 'M5.5 3.5v9M10.5 3.5v9',
    play: 'M5 3.5v9l7-4.5z',
    info: 'M8 13.5a5.5 5.5 0 1 0 0-11 5.5 5.5 0 0 0 0 11zM8 7.5V11M8 5.3v.2',
    alert: 'M8 13.5a5.5 5.5 0 1 0 0-11 5.5 5.5 0 0 0 0 11zM8 5v3.5M8 10.6v.2',
    tag: 'M2.5 2.5h5l6 6-5 5-6-6zM5.5 5.5v.01',
    dots: 'M3.5 8h.01M8 8h.01M12.5 8h.01',
  };
  const icon = (name, size = 16, cls = '') => PATHS[name]
    ? `<svg class="ic ${cls}" viewBox="0 0 16 16" width="${size}" height="${size}" aria-hidden="true" focusable="false"><path d="${PATHS[name]}"/></svg>`
    : '';
  // Fit as a ring: the arc is the score out of 100, the number sits inside it.
  const ring = (value, size = 28) => {
    const has = value != null && Number.isFinite(+value);
    const v = has ? Math.max(0, Math.min(100, Math.round(+value))) : 0;
    const r = (size - 3) / 2, c = 2 * Math.PI * r, mid = size / 2;
    const arc = has ? `<circle class="ring-arc" cx="${mid}" cy="${mid}" r="${r}" stroke-dasharray="${(c * v / 100).toFixed(2)} ${c.toFixed(2)}" transform="rotate(-90 ${mid} ${mid})"/>` : '';
    return `<svg class="ring" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" aria-hidden="true" focusable="false"><circle class="ring-track" cx="${mid}" cy="${mid}" r="${r}"/>${arc}</svg>`;
  };
  window.Icons = { PATHS, icon, ring };
})();
