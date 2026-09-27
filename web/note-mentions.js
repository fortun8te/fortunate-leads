/* Profile selection attaches identity only. Claims remain in the owner's note. */
(function (root) {
  const contains = (note, token) => new RegExp('(^|[^\\w@.])' + token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?![\\w]|\\.[\\w.])').test(note);
  const controls = new WeakMap();
  const surviving = (note, refs = []) => refs.filter(r => contains(note, r.token)).slice(0, 12);
  function attach(input, { search, selected = [], change }) {
    if (!input) return;
    let refs = surviving(input.value, selected), items = [], active = 0, request = 0, timer, match, wanted = false;
    const picker = document.createElement('div');
    picker.id = 'note-mention-options'; picker.className = 'note-mention-picker';
    picker.setAttribute('role', 'listbox'); picker.setAttribute('aria-label', 'Profiles'); picker.hidden = true;
    input.insertAdjacentElement('afterend', picker);
    input.setAttribute('role', 'combobox'); input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-haspopup', 'listbox'); input.setAttribute('aria-controls', picker.id); input.setAttribute('aria-expanded', 'false');
    function close() { wanted = false; clearTimeout(timer); request++; items = []; picker.hidden = true; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); }
    function draw() {
      picker.replaceChildren(); picker.hidden = !items.length;
      input.setAttribute('aria-expanded', String(!!items.length));
      items.forEach((person, index) => {
        const option = document.createElement('div');
        option.id = `note-mention-option-${index}`; option.className = 'note-mention-option';
        option.setAttribute('role', 'option'); option.setAttribute('aria-selected', String(index === active));
        const avatar = document.createElement('span'); avatar.className = 'note-mention-avatar';
        if (person.pic) {
          const img = document.createElement('img'); img.src = person.pic; img.alt = ''; img.loading = 'lazy';
          avatar.append(img);
        } else avatar.textContent = (person.name || person.handle || '?').trim().slice(0, 1).toUpperCase();
        const body = document.createElement('span'); body.className = 'note-mention-body';
        const identity = document.createElement('span'); identity.className = 'note-mention-identity';
        const name = document.createElement('strong'); name.textContent = person.name || person.handle;
        const handle = document.createElement('span'); handle.textContent = '@' + person.handle;
        identity.append(name, handle); body.append(identity);
        const badges = Array.isArray(person.badges) ? person.badges.slice(0, 2) : [];
        if (badges.length) {
          const saved = document.createElement('span'); saved.className = 'note-mention-badges';
          for (const label of badges) {
            const badge = document.createElement('span'); badge.className = 'note-mention-badge'; badge.textContent = label;
            saved.append(badge);
          }
          body.append(saved);
        }
        option.setAttribute('aria-label', `${person.name || person.handle}, @${person.handle}${badges.length ? ', saved: ' + badges.join(', ') : ''}`);
        option.append(avatar, body);
        option.addEventListener('pointerdown', e => { e.preventDefault(); choose(index); });
        picker.append(option);
      });
      if (items.length) input.setAttribute('aria-activedescendant', `note-mention-option-${active}`);
    }
    function choose(index) {
      const person = items[index]; if (!person || !match) return;
      const token = '@' + person.handle;
      const before = input.value.slice(0, match.start), after = input.value.slice(match.end);
      refs = surviving(input.value, refs).filter(r => r.token !== token);
      if (refs.length >= 12) { close(); return; }
      input.value = before + token + ' ' + after;
      refs.push({ person_id: person.id, token });
      change(refs); close(); input.focus();
      input.setSelectionRange(before.length + token.length + 1, before.length + token.length + 1);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }
    function update() {
      refs = surviving(input.value, refs); change(refs); clearTimeout(timer);
      const before = input.value.slice(0, input.selectionStart);
      const found = /(?:^|[^\w@.])@([A-Za-z0-9_.]{0,30})$/.exec(before);
      if (!found || input.selectionStart !== input.selectionEnd || refs.length >= 12) { close(); return; }
      match = { start: before.length - found[1].length - 1, end: input.selectionStart + (/^[A-Za-z0-9_.]*/.exec(input.value.slice(input.selectionStart))[0].length) };
      wanted = true;
      const version = ++request;
      timer = setTimeout(async () => {
        try {
          const result = await search(found[1]);
          if (version !== request || !input.isConnected || document.activeElement !== input) return;
          items = result.people || []; active = 0; draw();
        } catch { if (version === request) close(); }
      }, 160);
    }
    input.addEventListener('input', update);
    input.addEventListener('click', update);
    input.addEventListener('keyup', e => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) update(); });
    input.addEventListener('blur', () => { clearTimeout(timer); close(); });
    input.addEventListener('keydown', e => {
      if ((!items.length && !wanted) || e.isComposing) return;
      if (!items.length && !['Escape', 'Tab'].includes(e.key)) return;
      if (['ArrowDown', 'ArrowUp', 'Enter', 'Escape', 'Tab'].includes(e.key)) {
        e.stopPropagation();
        if (e.key === 'Tab') { close(); return; }
        e.preventDefault();
        if (e.key === 'Escape') close();
        else if (e.key === 'Enter') choose(active);
        else { active = (active + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length; draw(); }
      }
    });
    controls.set(input, {
      capture() {
        if (!wanted || document.activeElement !== input) return null;
        clearTimeout(timer); request++;
        return { owner: input.dataset?.id, value: input.value, start: input.selectionStart,
          end: input.selectionEnd, items: items.slice(), active, match: { ...match } };
      },
      restore(saved) {
        if (!saved || saved.owner !== input.dataset?.id || saved.value !== input.value ||
            document.activeElement !== input || input.selectionStart !== saved.start || input.selectionEnd !== saved.end) return;
        wanted = true; match = saved.match; active = saved.active;
        if (saved.items.length) { items = saved.items; draw(); }
        else update();
      },
    });
  }
  const capture = input => controls.get(input)?.capture() || null;
  const restore = (input, saved) => controls.get(input)?.restore(saved);
  root.LeadNoteMentions = { attach, surviving, contains, capture, restore };
})(typeof window === 'undefined' ? globalThis : window);
