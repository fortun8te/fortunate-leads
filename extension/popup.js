const $ = (id) => document.getElementById(id);
function render(v) {
  if (!v) return;
  $('status').textContent = v.text;
  $('dot').className = v.badge === '!' ? 'attention' : v.state;
  $('job').textContent = v.job || '';
  $('rows').textContent = 'today: ' + (v.today?.people || 0).toLocaleString() + ' people · ' + (v.today?.bios || 0).toLocaleString() + ' bios';
  $('list').textContent = (v.today?.list || 0) + ' / ' + (v.budget?.list ?? '');
  $('profile').textContent = (v.today?.profile || 0) + ' / ' + (v.budget?.profile ?? '');
  $('error').textContent = v.lastError && v.lastError !== v.text ? v.lastError : '';
  const paused = v.state === 'paused' && !/workspace/i.test(v.text);
  $('toggle').textContent = paused ? 'Resume' : 'Pause';
  $('toggle').dataset.cmd = paused ? 'resume' : 'pause';
}
chrome.storage.local.get('view').then((o) => render(o.view));
chrome.storage.onChanged.addListener((c) => { if (c.view) render(c.view.newValue); });
$('toggle').onclick = () => chrome.runtime.sendMessage({ cmd: $('toggle').dataset.cmd || 'pause' });
$('open').onclick = () => chrome.tabs.create({ url: 'http://127.0.0.1:8777/' });
