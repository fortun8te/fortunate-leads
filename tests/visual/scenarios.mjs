// What the regression run captures. Each scenario gets a fresh page load at each width.
// Map modes are discovered from the page, so the list follows whatever the branch under test offers.

export const PREP = `(() => {
  if (!document.getElementById('fl-visual')) {
    const s = document.createElement('style'); s.id = 'fl-visual';
    // Still frames: no animation, no caret, and the live collection strip keeps its space but not its text.
    s.textContent = '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}.strip{visibility:hidden!important}#toast{display:none!important}';
    document.head.appendChild(s);
  }
  // Relative times drift between runs; keep their width and drop the number.
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = walker.nextNode());) if (/\\b(\\d+[smhd]|moments) ago\\b/.test(n.nodeValue)) n.nodeValue = n.nodeValue.replace(/\\b(\\d+[smhd]|moments) ago\\b/g, '0m ago');
})()`;

export const WAIT_IMAGES = `(async () => {
  const end = Date.now() + 8000;
  while (Date.now() < end) {
    const pending = [...document.images].filter((i) => i.offsetParent && !i.complete);
    if (!pending.length) return true;
    await new Promise((r) => setTimeout(r, 150));
  }
  return false;
})()`;

const waitFor = (cond, ms = 20000) => `(async () => { const end = Date.now() + ${ms}; while (Date.now() < end) { try { if (${cond}) return true; } catch (e) {} await new Promise((r) => setTimeout(r, 100)); } return false; })()`;

const page = (name, hash, ready, extra = '') => ({
  name, hash, kind: 'page',
  async run(b) { if (!(await b.eval(waitFor(ready)))) throw new Error('page never became ready'); if (extra) await b.eval(extra); },
});

export const SCENARIOS = [
  page('leads', '#/leads', 'S.rows.length > 0 && document.querySelector("#rows .row:not(.skel)")'),
  page('leads-closest', '#/leads?sort=closeness', 'S.rows.length > 0 && S.sort === "closeness" && document.querySelector("#rows .row:not(.skel)")'),
  page('leads-detail', '#/leads', 'S.rows.length > 0 && document.querySelector("#rows .lead-open")',
    `(async () => { document.querySelector("#rows .lead-open").click(); const end = Date.now() + 15000; while (Date.now() < end && (!S.person || S.person.loading)) await new Promise((r) => setTimeout(r, 100)); })()`),
  page('review', '#/qual', 'document.querySelector("#view-qual.on")'),
  page('tags', '#/tags', 'document.querySelector("#view-tags.on")'),
  page('collection', '#/scraper', 'document.querySelector("#view-scraper.on") && S.sc'),
  page('accounts', '#/accounts', 'document.querySelector("#view-accounts.on") && S.sc'),
  page('settings', '#/settings', 'document.querySelector("#view-settings.on")'),
];

// The map: every view mode the branch offers (a select, or a group of buttons).
export const MAP_MODES = `(() => {
  const sel = document.querySelector('#map-size');
  if (sel && sel.options.length) return [...sel.options].map((o) => ({ id: o.value, label: o.textContent.trim(), via: 'select' }));
  const box = document.querySelector('#map-modes');
  if (box) return [...box.querySelectorAll('button, [role="radio"]')].map((el, i) => ({ id: el.dataset.mode || el.dataset.view || el.dataset.id || String(i), label: el.textContent.trim(), via: 'button', index: i }));
  return [];
})()`;

export const SELECT_MODE = (m) => m.via === 'select'
  ? `(() => { const s = document.querySelector('#map-size'); s.value = ${JSON.stringify(m.id)}; s.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`
  : `(() => { document.querySelector('#map-modes').querySelectorAll('button, [role="radio"]')[${m.index}].click(); return true; })()`;

export const MAP_READY = waitFor(`document.querySelector('#map-canvas') && document.querySelector('#map-canvas').width > 0 && (document.querySelector('#map-state')||{hidden:true}).hidden`, 40000);
export const CANVAS_RECT = `(() => { const r = document.querySelector('#map-canvas').getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) }; })()`;
export const NO_OVERFLOW = `({ scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth, main: (document.querySelector('main') || document.body).scrollWidth })`;
export const slug = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'mode';
