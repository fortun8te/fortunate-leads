// Reads the 'view' written by background.js status() plus the raw failure sample ('debug').
const $ = (id) => document.getElementById(id);
const n = (v) => Math.round(v || 0).toLocaleString('en-US');
const hm = (t) => new Date(t).toTimeString().slice(0, 5);
const LABEL = { run: 'Scraping', wait: 'Waiting', cool: 'Cooldown', stop: 'Idle', off: 'Needs attention' };
const ago = (t) => { const m = Math.max(0, Math.round((Date.now() - t) / 60e3)); return (m >= 60 ? Math.floor(m / 60) + 'h ' : '') + (m % 60) + 'm ago'; };
const secs = (t) => Math.max(0, Math.ceil((t - Date.now()) / 1e3));

let current = null, debug = null;
function bucketText(b, what) {
  if (!b) return '–';
  if (b.until) return 'cooling until ' + hm(b.until) + ' · ' + b.hits + ' hit' + (b.hits === 1 ? '' : 's') + '/24h';
  if (b.left === 0) return 'daily budget used';
  const wait = b.readyAt ? secs(b.readyAt) : 0;
  const left = b.left == null ? 'no daily cap' : n(b.left) + ' ' + what + ' left'; // null = unlimited (Infinity does not survive storage)
  return (wait ? 'next in ' + wait + 's' : 'ready') + ' · ' + left + (b.infoOff ? ' · via page loads' : '');
}
function render(v) {
  if (!v) return;
  current = v;
  const key = v.key || 'stop', text = String(v.text || '');
  $('sq').className = 'sq ' + key;
  // text = "[Lists cooling until 14:20 · ]Next request in 8s": last part is the state, the rest is detail.
  const parts = text.split(' · ');
  let label = parts.pop(), detail = parts.join(' · ');
  if (key === 'wait' && v.nextAt) label = secs(v.nextAt) ? 'Waiting ' + secs(v.nextAt) + 's' : 'Scraping';
  if (/^Needs attention/.test(text)) { label = 'Needs attention'; detail = text.replace(/^Needs attention:\s*/, ''); }
  $('state').textContent = label || LABEL[key];
  $('detail').textContent = detail;
  $('job').textContent = v.job || '';
  const t = v.today || {};
  $('today').textContent = n(t.people) + ' people · ' + n(t.list) + ' pages · ' + n(t.bios) + ' bios';
  const r = v.rate;
  $('hour').textContent = r ? n(r.people_hour) + ' people · ' + n(r.pages_hour) + ' pages · ' + n(r.bios_hour) + ' bios · ' + n(r.requests_hour) + ' req' : '–';
  $('lists').textContent = bucketText(v.buckets && v.buckets.list, 'pages');
  $('bios').textContent = bucketText(v.buckets && v.buckets.profile, 'reads');
  $('limits').textContent = r && r.last_hit_at ? n(r.hits_24h) + ' in 24h · last ' + ago(Date.parse(r.last_hit_at)) : 'none hit';
  $('boxdt').hidden = $('box').hidden = !v.box;
  $('box').textContent = n(v.box) + ' waiting for server';
  $('note').textContent = v.note || '';
  const err = v.lastError && v.lastError !== text ? v.lastError : '';
  $('errwrap').hidden = !err;
  $('error').textContent = err.replace(/ \| HTTP .*$/, '');
  const d = debug && err ? debug : null;
  $('raw').hidden = !d;
  if (d) {
    $('rawsum').textContent = 'Raw sample · ' + d.code + (d.reason ? ' (' + d.reason + ')' : '') + ' · ' + hm(Date.parse(d.at));
    $('sample').textContent = d.sample || '(empty)';
  }
  const paused = v.state === 'paused' && !/workspace/i.test(text);
  $('toggle').textContent = paused ? 'Resume' : 'Pause';
  $('toggle').dataset.cmd = paused ? 'resume' : 'pause';
}
$('ver').textContent = 'v' + chrome.runtime.getManifest().version;
chrome.storage.local.get(['view', 'debug']).then((o) => { debug = o.debug || null; render(o.view); });
chrome.storage.onChanged.addListener((c) => {
  if (c.debug) debug = c.debug.newValue || null;
  if (c.view) render(c.view.newValue); else if (c.debug) render(current);
});
setInterval(() => { if (current) render(current); }, 1000);
$('toggle').onclick = () => chrome.runtime.sendMessage({ cmd: $('toggle').dataset.cmd || 'pause' });
$('open').onclick = () => chrome.tabs.create({ url: 'http://127.0.0.1:8777/' });

// Everything needed to diagnose a stall from a paste: state, last raw Instagram answer, recent events, tabs.
$('copy').onclick = async () => {
  const keys = ['st', 'view', 'debug', 'debugLog', 'trail', 'lane', 'cur', 'prog', 'boot', 'workTab', 'localPaused', 'budget', 'lookupTab'];
  const o = await chrome.storage.local.get([...keys, 'box']);
  const box = o.box || [];
  let tabs = [];
  try {
    tabs = (await chrome.tabs.query({ url: 'https://www.instagram.com/*' })).map((t) => ({ id: t.id, url: t.url, status: t.status,
      active: t.active, pinned: t.pinned, discarded: t.discarded, frozen: t.frozen, autoDiscardable: t.autoDiscardable }));
  } catch {}
  const out = { copied_at: new Date().toISOString(), version: chrome.runtime.getManifest().version, ua: navigator.userAgent, tabs,
    outbox: { length: box.length, first: box[0] ? { path: box[0].path, job_id: box[0].body && box[0].body.job_id } : null } };
  for (const k of keys) out[k] = o[k] === undefined ? null : o[k];
  const text = JSON.stringify(out, null, 2);
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch {
    const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
    try { ok = document.execCommand('copy'); } catch {} ta.remove();
  }
  $('copy').textContent = ok ? 'Copied' : 'Copy failed';
  setTimeout(() => { $('copy').textContent = 'Copy debug'; }, 1500);
};
