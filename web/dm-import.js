/* Local Instagram download import. Selected files stay in this page until it is closed. */
(() => {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const state = {files: null, preview: null, busy: false};
  let root;

  async function request(path, body) {
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Import failed (${response.status})`);
    return data;
  }
  function notice(message, error = false) {
    const el = root.querySelector('[data-dm-notice]');
    el.textContent = message;
    el.style.color = error ? 'var(--danger, #ad3e3e)' : '';
  }
  async function encodeFile(file) {
    if (file.size > 32 * 1024 * 1024) throw new Error('Choose files smaller than 32 MB');
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error(`Could not read ${file.name}`));
      reader.onload = () => resolve({name:file.webkitRelativePath || file.name,
        data_base64:String(reader.result).split(',', 2)[1]});
      reader.readAsDataURL(file);
    });
  }
  function renderPreview() {
    const area = root.querySelector('[data-dm-preview]');
    const p = state.preview;
    if (!p) { area.innerHTML = ''; return; }
    const options = p.sender_candidates.map(s => `<option value="${esc(s)}"></option>`).join('');
    const rows = p.threads.map(t => `<tr data-dm-thread="${esc(t.fingerprint)}"><td>${esc(t.participant)}</td>
      <td>${t.outbound} sent · ${t.inbound} received</td><td>${esc(t.last_at?.slice(0,10) || '')}</td>
      <td>${t.outbound ? `<input class="input" data-dm-handle aria-label="Instagram username for ${esc(t.participant)}" placeholder="@username" autocomplete="off" spellcheck="false" maxlength="31">` : 'Received only'}</td></tr>`).join('');
    area.innerHTML = `<div style="margin-top:16px"><label for="dm-owner">Your name in the download</label>
      <div class="row-flex"><input class="input" id="dm-owner" list="dm-senders" value="${esc(p.owner_name)}" placeholder="Choose exact sender name"><datalist id="dm-senders">${options}</datalist><button type="button" class="btn" data-dm-analyze>Show conversations</button></div>
      <p class="muted">Choose the name that appears on messages you sent. A participant's display name does not prove their Instagram username; enter each username yourself.</p></div>
      ${p.owner_name ? `<p class="muted">${p.outbound_threads} conversations with a sent message · ${p.inbound_only_threads} received only · ${p.skipped_group_threads} group chats skipped</p>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>In download</th><th>Messages</th><th>Latest</th><th>Instagram username you confirm</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="row-flex" style="margin-top:12px"><button type="button" class="btn solid" data-dm-confirm>Import mapped people</button><span class="muted">Only conversations with a sent message can be marked Contacted. Existing choices stay in place.</span></div>` : '<p class="muted">Select your sender name, then show the conversations.</p>'}`;
  }
  async function analyze() {
    if (!state.files || state.busy) return;
    state.busy = true; notice('Reading conversations locally…');
    const owner = root.querySelector('#dm-owner')?.value?.trim() || '';
    try {
      state.preview = await request('/api/dm-import/preview', {files:state.files, owner_name:owner});
      renderPreview(); notice(owner ? 'Review the people and enter usernames you know.' : 'Choose your name from the download.');
    } catch (error) { notice(error.message, true); }
    finally { state.busy = false; }
  }
  async function confirm() {
    if (!state.files || !state.preview || state.busy) return;
    const mappings = {};
    for (const row of root.querySelectorAll('[data-dm-thread]')) {
      const value = row.querySelector('[data-dm-handle]')?.value.trim();
      if (value) mappings[row.dataset.dmThread] = value;
    }
    if (!Object.keys(mappings).length) { notice('Enter at least one username you can confirm.', true); return; }
    state.busy = true; notice('Saving contact history…');
    try {
      const data = await request('/api/dm-import/confirm', {files:state.files,
        owner_name:state.preview.owner_name, mappings});
      notice(`${data.imported_threads} conversations saved · ${data.new_people} new people · ${data.marked_contacted} marked Contacted. Your relationship and familiarity choices are unchanged.`);
      root.querySelector('[data-dm-preview]').innerHTML = `<p>Review how well you know them in Leads:</p><div class="row-flex">${(data.contacts || []).map(person => `<a class="btn" href="#/leads?q=${encodeURIComponent(person.handle)}">@${esc(person.handle)}</a>`).join('')}</div>`;
      state.files = null; state.preview = null;
      root.querySelector('[data-dm-file]').value = '';
      root.querySelector('[data-dm-folder]').value = '';
      window.dispatchEvent(new CustomEvent('dm-import-complete', {detail:data}));
    } catch (error) { notice(error.message, true); }
    finally { state.busy = false; }
  }
  async function choose(event) {
    const picked = [...event.target.files].filter(file => !file.webkitRelativePath || /(^|\/)messages\/(inbox|message_requests)\/[^/]+\/message_\d+\.json$/i.test(file.webkitRelativePath));
    if (!picked.length) { notice('No Instagram message JSON files found in that folder.', true); return; }
    if (picked.length > 3000 || picked.reduce((n, f) => n + f.size, 0) > 32 * 1024 * 1024) {
      notice('Choose up to 3,000 files totaling at most 32 MB.', true); return;
    }
    state.busy = true; notice('Preparing selected files…');
    try {
      state.files = await Promise.all(picked.map(encodeFile));
      state.preview = null;
      root.querySelector('[data-dm-preview]').innerHTML = '';
      await request('/api/dm-import/preview', {files:state.files}).then(p => {state.preview=p; renderPreview();});
      notice('Choose your name as it appears in the download.');
    } catch (error) { state.files = null; notice(error.message, true); }
    finally { state.busy = false; }
  }
  function mount() {
    if (root) return;
    const view = document.querySelector('#view-accounts .scr');
    if (!view) return;
    root = document.createElement('details');
    root.className = 'panel'; root.id = 'dm-import';
    root.innerHTML = `<summary class="p-head">Message history</summary>
      <div class="p-body"><p class="muted">Import contact history from an Instagram download. Review usernames before saving; message text stays out of Fortunate.</p>
      <input class="input" type="file" data-dm-file aria-label="Instagram message ZIP or JSON files" accept=".zip,.json,application/zip,application/json" multiple>
      <p class="muted">ZIP or JSON, up to 32 MB. For larger downloads, choose the extracted messages folder.</p>
      <input class="input" type="file" data-dm-folder aria-label="Extracted Instagram download folder" webkitdirectory multiple>
      <p class="muted" data-dm-notice role="status" aria-live="polite"></p><div data-dm-preview></div></div>`;
    view.append(root);
    root.querySelector('[data-dm-file]').addEventListener('change', choose);
    root.querySelector('[data-dm-folder]').addEventListener('change', choose);
    root.addEventListener('click', e => {
      if (e.target.closest('[data-dm-analyze]')) analyze();
      if (e.target.closest('[data-dm-confirm]')) confirm();
    });
  }
  window.DMImport = {mount};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();
