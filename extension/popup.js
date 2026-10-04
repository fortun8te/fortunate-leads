// Reads the 'view' written by background.js status() plus the raw failure sample ('debug').
const $ = (id) => document.getElementById(id);
const n = (v) => Math.round(v || 0).toLocaleString('en-US');
const hm = (t) => new Date(t).toTimeString().slice(0, 5);
const LABEL = { run: 'Scraping', wait: 'Waiting', cool: 'Cooldown', stop: 'Idle', off: 'Needs attention' };
const ago = (t) => { const m = Math.max(0, Math.round((Date.now() - t) / 60e3)); return (m >= 60 ? Math.floor(m / 60) + 'h ' : '') + (m % 60) + 'm ago'; };
const secs = (t) => Math.max(0, Math.ceil((t - Date.now()) / 1e3));

const CODES = { rate_limit: 'Rate limited', soft_block: 'Temporarily blocked', login: 'Signed out', challenge: 'Security check', not_found: 'Not found', network: 'Connection problem', unsupported: 'Not supported', other: 'Unexpected reply' };
const SAYS = { rate_limit: 'Instagram is limiting requests. Collection resumes on its own.', soft_block: 'Instagram is limiting this account for now. Collection resumes on its own.',
  network: "Couldn't reach Instagram. Collection retries on its own.", unsupported: 'Instagram declined this request. Trying another route.', not_found: 'Instagram could not find that profile.', other: 'Instagram sent an unexpected reply. Collection retries on its own.' };
// Errors look like "14:02 rate_limit (reason) on list x | raw sample": show a sentence, keep the raw sample in the details.
const friendly = (e) => { const m = /^(?:\d\d:\d\d )?(\w+)(?: \([^)]*\))? on /.exec(e); return m && SAYS[m[1]] ? SAYS[m[1]] : e.replace(/ \| .*$/, ''); };
let current = null, debug = null;
function bucketText(b, what) {
  if (!b) return '–';
  if (b.until) return 'paused until ' + hm(b.until) + ' · ' + b.hits + ' limit' + (b.hits === 1 ? '' : 's') + ' in 24h';
  if (b.left === 0) return 'daily budget reached';
  const wait = b.readyAt ? secs(b.readyAt) : 0;
  const left = b.left == null ? 'no daily cap' : n(b.left) + ' ' + what + ' left'; // null = unlimited (Infinity does not survive storage)
  return (wait ? 'next in ' + wait + 's' : 'ready') + ' · ' + left + (b.infoOff ? ' · reading page loads' : '');
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
  if (v.state === 'network_wait') { label = 'Connection trouble'; detail = text; }
  if (/^Needs attention/.test(text)) { label = 'Needs attention'; detail = text.replace(/^Needs attention:\s*/, ''); }
  $('state').textContent = label || LABEL[key];
  $('detail').textContent = detail;
  $('job').textContent = v.job || '';
  const t = v.today || {};
  $('today').textContent = n(t.people) + ' people · ' + n(t.list) + ' pages · ' + n(t.bios) + ' bios';
  const r = v.rate;
  $('hour').textContent = r ? n(r.people_hour) + ' people · ' + n(r.pages_hour) + ' pages · ' + n(r.bios_hour) + ' bios · ' + n(r.requests_hour) + ' requests' : '–';
  $('lists').textContent = v.stages?.list === false ? 'Off' : bucketText(v.buckets && v.buckets.list, 'pages');
  $('bios').textContent = v.stages?.profile === false ? 'Off' : bucketText(v.buckets && v.buckets.profile, 'reads');
  $('limits').textContent = r && r.last_hit_at ? n(r.hits_24h) + ' in 24h, last ' + ago(Date.parse(r.last_hit_at)) : 'none reached';
  $('boxdt').hidden = $('box').hidden = !v.box;
  $('box').textContent = n(v.box) + (v.box === 1 ? ' result' : ' results') + ' waiting to reach the server';
  $('note').textContent = v.note || '';
  const err = v.lastError && v.lastError !== text ? v.lastError : '';
  $('errwrap').hidden = !err;
  $('error').textContent = friendly(err);
  const d = debug && err ? debug : null;
  $('raw').hidden = !d;
  if (d) {
    $('rawsum').textContent = 'Instagram response · ' + (CODES[d.code] || 'Unexpected reply') + ' · ' + hm(Date.parse(d.at));
    $('sample').textContent = d.sample || 'Empty response';
  }
  const paused = v.state === 'paused' && !/workspace/i.test(text);
  $('toggle').textContent = paused ? 'Resume' : 'Pause';
  $('toggle').dataset.cmd = paused ? 'resume' : 'pause';
  const pendingRequest = !!v.request?.phase;
  $('requestreview').hidden = !pendingRequest;
  $('toggle').disabled = paused && (pendingRequest || /request.*(?:unknown|did not confirm)|manual review|scraping warning|security check|log in/i.test(text));
  if (typeof renderSpeed === 'function') renderSpeed();
}
const showVer = (a) => { $('ver').textContent = 'v' + chrome.runtime.getManifest().version + (a && a.ig_id ? ' · ' + (a.handle ? '@' + a.handle : 'id ' + a.ig_id) : a ? ' · signed out' : ''); };
let speedAccount = null, speedLane = null, speedState = null, speedSetting = null, speedSaving = false, speedSummary = null;
function renderSpeed(updateInput = false) {
  const eligible = FL.speedEligible(speedAccount, speedLane) && speedState?.accountIgId === speedAccount?.ig_id;
  $('speedwrap').hidden = !eligible;
  $('speedapply').disabled = !eligible || speedSaving;
  if (!eligible) return;
  if (updateInput || !$('speed').value) {
    const ms = FL.pollWaitFor(speedAccount, speedLane, speedSetting);
    $('speed').value = String(ms / 1000);
    $('speedpreset').value = [8000,11650,15000,30000].includes(ms) ? String(ms) : 'custom';
  }
  const pages = current?.rate?.pages_hour;
  const cadence = speedSummary?.cadence;
  $('speedactual').textContent = cadence?.active_same_job_samples > 0 && Number.isFinite(cadence.active_mean_seconds)
    ? 'Measured: ' + cadence.active_mean_seconds.toFixed(1) + ' seconds per active page (' + n(cadence.active_same_job_samples) + ' samples in the last 2 hours). Breaks add time.'
    : pages == null ? 'Actual pace appears after pages are saved.' :
    n(pages) + ' pages saved in the last hour. Actual pace also depends on Instagram and breaks.';
}
chrome.storage.local.get(['account', 'laneId', 'st', 'collectionSpeed']).then((o) => {
  speedAccount = o.account; speedLane = o.laneId; speedState = o.st; speedSetting = o.collectionSpeed; renderSpeed(true);
  loadPipelineSummary(o.laneId, o.account?.ig_id).then((summary) => {
    if (speedLane === o.laneId && speedAccount?.ig_id === o.account?.ig_id) { speedSummary = summary; renderSpeed(); }
  });
});
chrome.storage.onChanged.addListener((c) => {
  if (c.account) { speedAccount = c.account.newValue; speedSummary = null; }
  if (c.laneId) { speedLane = c.laneId.newValue; speedSummary = null; }
  if (c.st) speedState = c.st.newValue;
  if (c.collectionSpeed) speedSetting = c.collectionSpeed.newValue;
  if (c.account || c.laneId || c.st || c.collectionSpeed || c.view) {
    renderSpeed(!!(c.account || c.laneId || c.collectionSpeed));
  }
});
$('speedpreset').onchange = () => {
  if ($('speedpreset').value !== 'custom') $('speed').value = String(Number($('speedpreset').value) / 1000);
  else { $('speeddetails').open = true; $('speed').focus?.(); }
};
$('speed').oninput = () => { $('speedpreset').value = 'custom'; };
$('speedapply').onclick = async () => {
  if (speedSaving || !FL.speedEligible(speedAccount, speedLane) || speedState?.accountIgId !== speedAccount?.ig_id) return;
  const ms = Math.round(Number($('speed').value) * 1000);
  try {
    FL.collectionSpeedSetting(speedAccount, speedLane, ms);
    speedSaving = true; $('speedapply').disabled = true;
    const reply = await chrome.runtime.sendMessage({ cmd: 'set_collection_speed', poll_wait_ms: ms });
    if (!reply?.ok) throw new Error(reply?.error || 'Could not save. Try again.');
    $('speedstatus').textContent = 'Saved. Applies at the next check; no reload needed.';
  } catch (e) { $('speedstatus').textContent = e.message || 'Could not save. Try again.'; }
  finally { speedSaving = false; renderSpeed(); }
};
chrome.storage.local.get('account').then((o) => showVer(o.account));
chrome.storage.onChanged.addListener((c) => { if (c.account) showVer(c.account.newValue); });
chrome.storage.local.get(['view', 'debug']).then((o) => { debug = o.debug || null; render(o.view); });
chrome.storage.onChanged.addListener((c) => {
  if (c.debug) debug = c.debug.newValue || null;
  if (c.view) render(c.view.newValue); else if (c.debug) render(current);
});
setInterval(() => { if (current) render(current); }, 1000);
$('toggle').onclick = () => { if (!$('toggle').disabled) chrome.runtime.sendMessage({ cmd: $('toggle').dataset.cmd || 'pause' }); };
let reviewSaving = false;
$('finishreview').onclick = async () => {
  if (reviewSaving || !current?.request?.phase) return;
  reviewSaving = true; $('finishreview').disabled = true;
  try {
    // The worker requires the matching operator review; this only settles the journal, never resumes.
    const reply = await chrome.runtime.sendMessage({cmd: 'review_shared_request'});
    if (!reply?.ok) throw new Error(reply?.error || 'Complete the review in the workspace first.');
    $('reviewstatus').textContent = 'Review recorded. Collection remains paused.';
  } catch (e) { $('reviewstatus').textContent = e.message || 'Could not finish the review. Try again.'; }
  finally { reviewSaving = false; $('finishreview').disabled = false; }
};
$('open').onclick = () => chrome.tabs.create({ url: 'http://127.0.0.1:8777/' });

// Small, identity-scoped reports contain no response bodies, cookies, cursors or permit tokens.
async function loadPipelineSummary(laneId, igId) {
  if (!laneId || !igId || typeof fetch !== 'function') return null;
  const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 3000);
  try {
    const query = new URLSearchParams({lane: laneId, ig_id: igId, since: new Date(Date.now() - 2 * 3600e3).toISOString()});
    const response = await fetch('http://127.0.0.1:8777/api/pipeline/summary?' + query, {signal: abort.signal});
    if (!response.ok) return null;
    const summary = await response.json();
    return summary?.v === 1 ? summary : null;
  } catch { return null; } finally { clearTimeout(timer); }
}
function compactLocalReport(o) {
  const request = o.sharedRequest;
  const metrics = (value, keys) => Object.fromEntries(keys.filter((key) => Number.isFinite(value?.[key])).map((key) => [key, value[key]]));
  return {
    v: 1, copied_at: new Date().toISOString(), version: chrome.runtime.getManifest().version,
    account: {lane: o.laneId || null, ig_id: o.account?.ig_id || null, handle: o.account?.handle || null},
    state: {state: o.view?.state || 'unknown', local_paused: !!o.localPaused,
      hold: o.st?.hold?.code || null, current_job: o.cur?.job ? {id: o.cur.job.id, kind: o.cur.job.kind, direction: o.cur.job.direction || null} : null},
    counts: {today: metrics(o.view?.today, ['people','list','bios']), last_hour: metrics(o.view?.rate, ['people_hour','pages_hour','bios_hour','requests_hour','hits_24h']), outbox: Array.isArray(o.box) ? o.box.length : 0},
    checks: {requested_ms: FL.pollWaitFor(o.account, o.laneId, o.collectionSpeed), applies_to: 'idle_list_checks'},
    request: request ? {phase: request.phase || 'unknown', age_seconds: request.at ? Math.max(0, Math.floor((Date.now()-request.at)/1000)) : null,
      kind: request.body?.kind || null, job_id: request.body?.job_id || null} : null,
  };
}
$('copy').onclick = async () => {
  const o = await chrome.storage.local.get(['laneId', 'account', 'st', 'view', 'cur', 'localPaused', 'box', 'collectionSpeed', 'sharedRequest']);
  const out = compactLocalReport(o);
  const pipeline = await loadPipelineSummary(o.laneId, o.account?.ig_id);
  if (pipeline) out.pipeline = pipeline;
  else out.pipeline = {available: false, reason: 'Local server unavailable or summary not supported'};
  const text = JSON.stringify(out, null, 2);
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch {
    const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
    try { ok = document.execCommand('copy'); } catch {} ta.remove();
  }
  $('copy').textContent = ok ? 'Copied' : "Couldn't copy";
  setTimeout(() => { $('copy').textContent = 'Copy diagnostics'; }, 1500);
};
