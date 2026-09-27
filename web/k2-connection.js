// K2 transport setup. Saving or probing this connection never enables the engine.
(() => {
  const form = document.querySelector('#k2-connection-form');
  if (!form) return;
  const status = document.querySelector('#k2-connection-status');
  const host = document.querySelector('#k2-host');
  const port = document.querySelector('#k2-port');
  const key = document.querySelector('#k2-key');
  const fields = document.querySelector('#k2-lan-fields');
  const save = document.querySelector('#k2-save');
  const test = document.querySelector('#k2-test');
  let saved = null, location = 'this_mac', busy = false;
  const buttons = [...form.querySelectorAll('[data-k2-location]')];
  const candidate = () => ({location, host:location === 'other_pc' ? host.value.trim() : '', port:location === 'other_pc' ? Number(port.value) : 11436});
  const same = (a, b) => a && b && a.location === b.location && a.host === b.host && Number(a.port) === Number(b.port);
  const valid = value => value.location === 'this_mac' || !!value.host && Number.isInteger(value.port) && value.port >= 1 && value.port <= 65535;
  function render() {
    fields.hidden = location !== 'other_pc';
    for (const button of buttons) button.setAttribute('aria-pressed', String(button.dataset.k2Location === location));
    const current = candidate();
    save.disabled = busy || !saved || !valid(current) || (!!same(current, saved) && !key.value.trim());
    test.disabled = busy || !saved || !same(current, saved) || !!key.value.trim();
    for (const button of buttons) button.disabled = busy;
    host.disabled = port.disabled = key.disabled = busy;
  }
  async function call(path, body) {
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), 9000);
    try {
      const response = await fetch(path, {cache:'no-store', ...(body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {}), signal:controller.signal});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Connection unavailable');
      return result;
    } finally { clearTimeout(deadline); }
  }
  function showSaved(result) {
    if (!['this_mac','other_pc'].includes(result?.location)) throw new Error('Invalid connection');
    saved = {location:result.location, host:result.host || '', port:Number(result.port), hasKey:!!result.has_api_key};
    location = saved.location; host.value = saved.host; port.value = saved.port; key.value = '';
    render();
  }
  async function load() {
    busy = true; render();
    try { showSaved(await call('/api/k2/connection')); status.textContent = `Connection saved${saved.hasKey ? ' · PC key saved' : ''}. Testing does not turn K2 on.`; }
    catch { saved = null; status.textContent = 'Could not load K2 connection. Reload settings to try again.'; }
    finally { busy = false; render(); }
  }
  form.addEventListener('click', event => {
    const button = event.target.closest('[data-k2-location]');
    if (!button || busy) return;
    location = button.dataset.k2Location;
    if (location === 'this_mac') key.value = '';
    status.textContent = same(candidate(), saved) ? 'Connection saved.' : 'Save this connection before testing.';
    render();
  });
  form.addEventListener('input', () => { status.textContent = same(candidate(), saved) ? 'Connection saved.' : 'Save this connection before testing.'; render(); });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const value = candidate();
    if (busy || !valid(value)) { status.textContent = 'Enter a LAN address and a port from 1 to 65535.'; return; }
    busy = true; status.textContent = 'Saving connection…'; render();
    try { const result = await call('/api/k2/connection', {...value, ...(value.location === 'other_pc' && key.value.trim() ? {api_key:key.value.trim()} : {})}); if (!same(result, value)) throw new Error('Unconfirmed save'); showSaved(result); status.textContent = `Connection saved${saved.hasKey ? ' · PC key saved' : ''}. K2 remains as selected in the engine panel.`; }
    catch (error) { status.textContent = error.message || 'Could not confirm this connection.'; }
    finally { busy = false; render(); }
  });
  test.addEventListener('click', async () => {
    if (busy || !same(candidate(), saved)) return;
    busy = true; status.textContent = 'Testing the saved connection…'; render();
    try { const result = await call('/api/k2/connection/test', {}); status.textContent = result.message || (result.ok ? 'K2 connection is ready.' : 'K2 connection could not be verified.'); }
    catch (error) { status.textContent = error.message || 'Could not test the K2 connection.'; }
    finally { busy = false; render(); }
  });
  render(); load();
})();
