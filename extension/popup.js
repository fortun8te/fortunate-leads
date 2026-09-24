// Reads the 'view' written by background.js status(): {state, text, badge, today{list,profile,people,bios}, budget, job, lastError, at}.
const $ = (id) => document.getElementById(id);
const n = (v) => Math.round(v || 0).toLocaleString('en-US');
const WINDOW = 15 * 60e3;

// Map the worker's status text onto the short state line.
function stateOf(v) {
  const t = String(v.text || '');
  let m;
  if (v.badge === '!' && /^Needs attention/i.test(t)) return { key: 'off', label: 'Needs attention', err: t.replace(/^Needs attention:?\s*/i, '') };
  if ((m = t.match(/^Next request in (\d+)\s*s/i))) {
    const left = Math.max(0, Math.round(((v.at || Date.now()) + +m[1] * 1000 - Date.now()) / 1000));
    return { key: 'wait', label: left ? 'Waiting ' + left + 's' : 'Scraping' };
  }
  if (/^Cooldown/i.test(t)) return { key: 'cool', label: t };
  if (/^Paused/i.test(t)) return { key: 'stop', label: 'Paused' };
  if (/Server offline/i.test(t)) return { key: 'off', label: 'Offline' };
  if (/Open Instagram/i.test(t)) return { key: 'off', label: 'Open Instagram' };
  if (/budget/i.test(t)) return { key: 'stop', label: 'Daily budget reached' };
  if (/^Idle/i.test(t)) return { key: 'stop', label: 'Idle' };
  if (v.state === 'running' || /^(Running|Scraping)/i.test(t)) return { key: 'run', label: 'Scraping' };
  return { key: 'stop', label: t || 'Idle' };
}

// Speed from counters sampled while the popup is open (kept across openings, last 15 min).
let samples = [];
async function sample(v) {
  if (!v || !v.today) return;
  const now = Date.now();
  samples = samples.filter((s) => now - s.t < WINDOW);
  const last = samples[samples.length - 1];
  if (!last || now - last.t > 5e3 || last.people !== v.today.people) samples.push({ t: now, people: v.today.people || 0, pages: v.today.list || 0 });
  await chrome.storage.local.set({ popupSpeed: samples.slice(-200) });
}
function speed() {
  if (samples.length < 2) return '–';
  let people = 0, pages = 0;
  for (let i = 1; i < samples.length; i++) {
    people += Math.max(0, samples[i].people - samples[i - 1].people);
    pages += Math.max(0, samples[i].pages - samples[i - 1].pages);
  }
  const min = (samples[samples.length - 1].t - samples[0].t) / 60e3;
  if (min < 1) return '–';
  const ppm = people / min;
  return (ppm >= 10 ? n(ppm) : ppm.toFixed(1)) + '/min · ' + n((pages / min) * 60) + ' pages/h';
}

let current = null;
function render(v) {
  if (!v) return;
  current = v;
  const s = stateOf(v);
  $('sq').className = 'sq ' + s.key;
  $('state').textContent = s.label;
  $('job').textContent = v.job || '';
  $('today').textContent = n(v.today && v.today.people) + ' people · ' + n(v.today && v.today.list) + ' pages';
  $('speed').textContent = speed();
  const err = s.err || (v.lastError && v.lastError !== v.text ? v.lastError : '');
  $('error').textContent = err;
  const paused = v.state === 'paused' && !/workspace/i.test(v.text || '');
  $('toggle').textContent = paused ? 'Resume' : 'Pause';
  $('toggle').dataset.cmd = paused ? 'resume' : 'pause';
}
$('ver').textContent = 'v' + chrome.runtime.getManifest().version;
chrome.storage.local.get(['view', 'popupSpeed']).then(async (o) => {
  samples = Array.isArray(o.popupSpeed) ? o.popupSpeed : [];
  await sample(o.view);
  render(o.view);
});
chrome.storage.onChanged.addListener(async (c) => { if (c.view) { await sample(c.view.newValue); render(c.view.newValue); } });
setInterval(() => { if (current && /^Next request/i.test(current.text || '')) render(current); }, 1000);
$('toggle').onclick = () => chrome.runtime.sendMessage({ cmd: $('toggle').dataset.cmd || 'pause' });
$('open').onclick = () => chrome.tabs.create({ url: 'http://127.0.0.1:8777/' });
