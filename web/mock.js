// Dev-only mock backend. Loaded only when the page URL has ?mock=1.
// Implements the full UI API (see docs/CONTRACT.md) in memory, including shared filters,
// tag facets, bulk edits, tag rules, saved views and the seed map.
(function () {
  let seed = 11;
  const rnd = () => ((seed = (seed * 16807) % 2147483647) - 1) / 2147483646;
  const pick = (a) => a[Math.floor(rnd() * a.length)];
  const chance = (p) => rnd() < p;
  const now = () => new Date().toISOString().replace('Z', '000+00:00');

  const ME = 'fortun8te';
  // Seeds with a rough niche bias and neighbours (people overlap mostly with neighbours).
  const SEEDS = {
    fortun8te: { bias: null, nb: ['dutchdtc', 'adcreativeclub', 'brandfounders.club'] },
    dtcdaily: { bias: null, nb: ['ecomcollective', 'shopify.founders', 'brandfounders.club', 'foundersfeed'] },
    'brandfounders.club': { bias: null, nb: ['dtcdaily', 'foundersfeed', 'cpgguild'] },
    ecomcollective: { bias: null, nb: ['dtcdaily', 'shopify.founders', 'adcreativeclub'] },
    skincarebusiness: { bias: 'Skincare', nb: ['beautyfounders', 'cpgguild'] },
    beautyfounders: { bias: 'Beauty', nb: ['skincarebusiness', 'brandfounders.club'] },
    cpgguild: { bias: 'Food & Drink', nb: ['packagingstudy', 'supplementbrands', 'brandfounders.club'] },
    'shopify.founders': { bias: null, nb: ['ecomcollective', 'dtcdaily'] },
    packagingstudy: { bias: 'Home', nb: ['cpgguild', 'adcreativeclub'] },
    supplementbrands: { bias: 'Supplements', nb: ['cpgguild', 'fitfounders'] },
    fitfounders: { bias: 'Fitness', nb: ['supplementbrands', 'apparelbrands'] },
    apparelbrands: { bias: 'Apparel', nb: ['fitfounders', 'dtcdaily'] },
    dutchdtc: { bias: null, nb: ['fortun8te', 'ecomcollective'] },
    foundersfeed: { bias: null, nb: ['brandfounders.club', 'dtcdaily'] },
    adcreativeclub: { bias: null, nb: ['ecomcollective', 'fortun8te', 'packagingstudy'] },
  };
  const SEED_LIST = Object.keys(SEEDS);
  const WEIGHT = SEED_LIST.map((s) => (s === ME ? 0.6 : s === 'dtcdaily' || s === 'ecomcollective' ? 1.8 : s === 'dutchdtc' ? 0.7 : 1));

  const FIRST = ['Maya', 'Jonas', 'Sofia', 'Liam', 'Ava', 'Noah', 'Emma', 'Lucas', 'Chloe', 'Daan', 'Isla', 'Mateo', 'Nora', 'Eli', 'Zoe', 'Theo', 'Lena', 'Ravi', 'Priya', 'Jade', 'Marcus', 'Tessa', 'Owen', 'Ines', 'Kai', 'Freya', 'Sam', 'Anouk', 'Leo', 'Hana', 'Dylan', 'Mila', 'Ruben', 'Carla', 'Felix', 'Amara', 'Sven', 'Julia', 'Bram', 'Olivia'];
  const LAST = ['Carter', 'de Vries', 'Nguyen', 'Brooks', 'Janssen', 'Patel', 'Morales', 'Kim', 'Bakker', 'Hughes', 'Rossi', 'Walsh', 'Visser', 'Chen', 'Ellis', 'Park', 'Reyes', 'Smit', 'Hayes', 'Ortiz', 'Meijer', 'Laurent'];
  const NICHES = {
    Skincare: ['clean skincare for sensitive skin', 'barrier-first serums', 'SPF you actually reapply'],
    Beauty: ['lip oils in 12 shades', 'vegan nail polish', 'brow gel that lasts'],
    Supplements: ['magnesium that tastes good', 'daily greens without the grass taste', 'creatine gummies'],
    Apparel: ['heavyweight tees made in Portugal', 'running kit for bad weather', 'linen basics'],
    Jewelry: ['recycled gold hoops', 'everyday silver', 'custom name necklaces'],
    Home: ['candles poured in Brooklyn', 'ceramic cookware', 'bedding for hot sleepers'],
    Pets: ['dog treats with one ingredient', 'cat furniture that looks like furniture'],
    'Food & Drink': ['single-origin cold brew cans', 'hot sauce, small batch', 'protein bars with real food'],
    Fitness: ['adjustable kettlebells', 'resistance bands that last', 'home rowing'],
  };
  const NICHE_KEYS = Object.keys(NICHES);
  const BRANDWORDS = ['luma', 'north', 'oat', 'kind', 'hale', 'fern', 'dune', 'mora', 'vela', 'solo', 'ember', 'tide', 'noon', 'pax', 'ruby', 'loft', 'saga', 'wren', 'kiln', 'arlo'];
  const ROLE_P = [['Brand', 0.34], ['Store', 0.1], ['Agency', 0.1], ['Freelancer', 0.07], ['Creative', 0.08], ['Creator', 0.1], ['Supplier', 0.05], ['SaaS', 0.05], ['Coach', 0.03], ['Personal', 0.08]];
  const pickRole = () => { let x = rnd(); for (const [r, p] of ROLE_P) { if ((x -= p) < 0) return r; } return 'Personal'; };
  const pickSeed = () => { let t = WEIGHT.reduce((a, b) => a + b, 0) * rnd(); for (let i = 0; i < SEED_LIST.length; i++) { if ((t -= WEIGHT[i]) < 0) return SEED_LIST[i]; } return SEED_LIST[0]; };
  const size = (f) => f < 1000 ? '<1k' : f < 10000 ? '1k-10k' : f < 100000 ? '10k-100k' : f < 1000000 ? '100k-1M' : '1M+';

  const people = [];
  const currentEdges = (id) => edges.get(id).filter(e => e.state === 'observed');
  const lists = (id) => new Set(currentEdges(id).map((e) => e.seed)).size;
  const edges = new Map(); // id -> [{seed, direction}]
  const tags = new Map(); // id -> [{tag, grp, source}]
  const marks = new Map();
  const notes = new Map();
  const followups = new Map();
  const activity = new Map();
  const reads = new Map(); // id -> latest profile-read state; no real collector runs in demo mode
  let activityId = 1;
  const localDay = (d = new Date()) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  function event(id, kind, body, before = null, after = null, happened = now()) {
    const entry = { id: activityId++, kind, body, before_value: before, after_value: after, happened_at: happened, created_at: now() };
    activity.set(id, [entry, ...(activity.get(id) || [])]);
    return entry;
  }
  function history(id, cursor, limit = 50) {
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) invalid('limit must be 1-100');
    let before = null;
    if (cursor) {
      try { before = JSON.parse(cursor); } catch (_) { invalid('invalid activity cursor'); }
      if (!Array.isArray(before) || before.length !== 2 || typeof before[0] !== 'string' || !Number.isInteger(before[1])) invalid('invalid activity cursor');
    }
    const ordered = [...(activity.get(id) || [])]
      .filter((e) => !before || e.happened_at < before[0] || (e.happened_at === before[0] && e.id < before[1]))
      .sort((a, b) => b.happened_at.localeCompare(a.happened_at) || b.id - a.id);
    const rows = ordered.slice(0, limit), last = rows[rows.length - 1];
    return { rows, next_cursor: ordered.length > limit ? JSON.stringify([last.happened_at, last.id]) : null };
  }
  function invalid(message) { const error = new Error(message); error.mockStatus = 400; throw error; }
  const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
  const STATUSES = ['interested', 'contacted', 'talking', 'client', 'no'];
  function validStatus(value) {
    if (value === 'good') value = 'interested';
    if (value !== null && !STATUSES.includes(value)) invalid('bad status');
    return value;
  }
  function calendarDate(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) invalid('date must be YYYY-MM-DD');
    const day = new Date(value + 'T00:00:00Z');
    if (value.startsWith('0000-') || !Number.isFinite(day.getTime()) || day.toISOString().slice(0, 10) !== value) invalid('invalid calendar date');
    return value;
  }
  function occurrenceTime(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) invalid('happened_at must be an ISO timestamp with timezone');
    calendarDate(value.slice(0, 10));
    if (+value.slice(11, 13) > 23 || +value.slice(14, 16) > 59 || (value[16] === ':' && +value.slice(17, 19) > 59)) invalid('invalid happened_at');
    const stamp = new Date(value);
    if (!Number.isFinite(stamp.getTime())) invalid('invalid happened_at');
    const fraction = ((value.match(/\.(\d+)/) || [])[1] || '').padEnd(6, '0').slice(0, 6);
    return stamp.toISOString().slice(0, 19) + '.' + fraction + '+00:00';
  }
  function markInput(body) {
    const value = { ...body };
    if ('status' in value) value.status = validStatus(value.status);
    if ('note' in value && value.note !== null && (typeof value.note !== 'string' || value.note.length > 5000)) invalid('note must be text');
    return value;
  }
  function followUpInput(body) {
    if (!isObject(body)) invalid('follow_up must be an object');
    const action = body.action;
    if (action === 'complete' || action === 'clear') {
      if ('due_on' in body || 'note' in body) invalid('use complete or clear without date/note');
      return { action };
    }
    if (action != null && !['schedule', 'complete_and_schedule'].includes(action)) invalid('invalid follow_up action');
    const due_on = calendarDate(body.due_on), note = 'note' in body ? body.note : '';
    if (typeof note !== 'string' || note.length > 500) invalid('note must be text up to 500 characters');
    return { action: action || 'schedule', due_on, note: note.trim() };
  }
  function setFollowUp(id, input) {
    let before = followups.get(id) || null;
    if (input.action === 'clear') {
      if (before) { followups.delete(id); event(id, 'follow_up_cleared', '', before); }
      return;
    }
    if (input.action === 'complete' || input.action === 'complete_and_schedule') {
      if (before && !before.completed_at) {
        const next = { ...before, completed_at: now(), updated_at: now() };
        followups.set(id, next); event(id, 'follow_up_completed', '', before, next); before = next;
      }
      if (input.action === 'complete') return;
    }
    if (!before || before.completed_at || before.due_on !== input.due_on || before.note !== input.note) {
      const next = { due_on: input.due_on, note: input.note, completed_at: null, updated_at: now() };
      followups.set(id, next); event(id, 'follow_up_scheduled', '', before, next);
    }
  }
  function saveInteraction(id, body) {
    if (!['dm', 'reply', 'call', 'meeting', 'note'].includes(body.kind)) invalid('invalid interaction kind');
    if (typeof body.body !== 'string' || !body.body.trim() || body.body.length > 5000) invalid('body must be 1-5000 characters');
    const stamp = occurrenceTime('happened_at' in body ? body.happened_at : now());
    const status = 'status' in body ? validStatus(body.status) : undefined;
    const follow = 'follow_up' in body ? followUpInput(body.follow_up) : null;
    // Match the server transaction, including rollback if building the response fails.
    const snapshot = [marks, notes, followups, activity].map((map) => [map, map.has(id), map.get(id)]), nextId = activityId;
    try {
      event(id, body.kind, body.body.trim(), null, null, stamp);
      if ('status' in body) setMark(id, { status });
      if (follow) setFollowUp(id, follow);
      return { ok: true, ...history(id), status: marks.get(id) || null, follow_up: followups.get(id) || null };
    } catch (error) {
      snapshot.forEach(([map, existed, value]) => { if (existed) map.set(id, value); else map.delete(id); });
      activityId = nextId;
      throw error;
    }
  }
  function setMark(id, body) {
    for (const [key, map] of [['status', marks], ['note', notes]]) {
      if (!(key in body)) continue;
      const before = map.get(id) || null, after = body[key] || null;
      if (before !== after) event(id, key, '', before, after);
      if (after) map.set(id, after); else map.delete(id);
    }
  }
  const markRevs = new Map();
  const readQueue = new Set();
  const handles = new Set();
  const N = 3200;

  for (let i = 1; i <= N; i++) {
    const primary = pickSeed();
    const bias = SEEDS[primary].bias;
    const role = pickRole();
    const niche = bias && chance(0.75) ? bias : pick(NICHE_KEYS);
    const bw = pick(BRANDWORDS);
    const brand = bw + pick(['', '.co', 'labs', 'goods', 'studio', '.' + niche.toLowerCase().replace(/[^a-z]/g, '').slice(0, 4)]);
    const f = pick(FIRST), l = pick(LAST);
    const isBrandAcct = role === 'Brand' || role === 'Store' ? chance(0.55) : false;
    let handle = isBrandAcct ? brand.replace(/\.$/, '') : (f + (chance(0.5) ? '.' : '') + l.split(' ').pop()).toLowerCase().replace(/[^a-z.]/g, '');
    while (handles.has(handle)) handle += Math.floor(rnd() * 90 + 10);
    handles.add(handle);
    const hasBio = chance(0.7);
    const followers = Math.floor(Math.pow(10, 2.3 + rnd() * 3.6));
    const us = chance(0.4), nl = !us && chance(0.18), amazon = chance(0.12);
    const title = { Brand: pick(['Founder', 'Co-founder', 'CEO']), Store: 'Owner', Agency: 'Growth agency for DTC', Freelancer: 'Freelance designer', Creative: 'Photographer', Creator: 'Creator', Supplier: 'Private label manufacturing', SaaS: 'Building software for Shopify brands', Coach: 'Ecom coach', Personal: 'Dad, runner' }[role];
    const bio = !hasBio ? null : [
      isBrandAcct ? pick(NICHES[niche]) : `${title}${role === 'Brand' || role === 'Store' ? ' @' + brand : ''}`,
      role === 'Brand' || role === 'Store' ? (isBrandAcct ? null : pick(NICHES[niche])) : null,
      us ? 'ships to the US' : nl ? 'Amsterdam / Rotterdam' : null,
      amazon ? 'now on Amazon' : null,
      chance(0.2) ? 'hello@' + brand.replace(/\./g, '') + '.com' : null,
      chance(0.12) ? 'prev. ' + pick(['Glossier', 'Gymshark', 'Allbirds', 'Olaplex', 'AG1']) : null,
    ].filter(Boolean).join(' · ');
    const website = hasBio && chance(role === 'Brand' || role === 'Store' ? 0.85 : 0.35) ? `https://${brand.replace(/\./g, '')}.${nl ? 'nl' : 'com'}` : null;
    people.push({
      id: i, handle, name: isBrandAcct ? bw[0].toUpperCase() + bw.slice(1) + ' ' + niche.split(' ')[0] : f + ' ' + l,
      pic: null, bio, website, category: hasBio && isBrandAcct ? pick(['Shopping & retail', 'Health/beauty', 'Product/service']) : null,
      followers, following: Math.floor(150 + rnd() * 2200), posts: Math.floor(5 + rnd() * 1200),
      is_verified: followers > 200000 && chance(0.4) ? 1 : 0, is_business: isBrandAcct ? 1 : 0,
      first_seen: new Date(Date.now() - rnd() * 20 * 86400000).toISOString(),
      bio_at: hasBio ? new Date(Date.now() - rnd() * 45 * 86400000).toISOString() : null,
      bio_src: hasBio ? 'extension' : null,
      _role: role, _niche: niche, _us: us, _nl: nl,
    });
    // Seeds: primary plus neighbours (overlap) plus occasional random.
    const ss = new Set([primary]);
    for (const nb of SEEDS[primary].nb) if (chance(0.16)) ss.add(nb);
    if (chance(0.05)) ss.add(pickSeed());
    if (ss.size >= 2 && chance(0.2)) ss.add(pickSeed());
    edges.set(i, [...ss].map((s) => ({ seed: s, state: i % 11 === 0 ? 'unverified' : i % 7 === 0 ? 'absent' : 'observed', first_seen: people[i - 1].first_seen, observed_at: i % 11 === 0 ? null : now(), checked_at: i % 11 === 0 ? null : now(), direction: s === ME ? (chance(0.6) ? 'followers' : 'following') : chance(0.75) ? 'followers' : 'following' })));
  }

  for (const p of people.filter(p => p.id % 5 === 0 && lists(p.id))) {
    const seed = SEED_LIST.find(s => !edges.get(p.id).some(e => e.seed === s));
    edges.get(p.id).push({seed, direction:'following', state: p.id % 10 ? 'absent' : 'unverified', first_seen:p.first_seen, observed_at:null, checked_at:p.id % 10 ? now() : null});
  }

  function autoTags(p) {
    const t = [];
    const add = (tag, grp) => { if (!t.some((x) => x.tag === tag)) t.push({ tag, grp, source: 'auto' }); };
    if (p.bio) {
      add(p._role, 'role');
      if (!['Personal', 'Creator'].includes(p._role) || p.website) add(p._niche, 'niche');
      if (/founder|ceo|owner/i.test(p.bio)) add('Founder', 'signal');
      if (/hiring/i.test(p.bio)) add('Hiring', 'signal');
      if (p.website && (p._role === 'Brand' || p._role === 'Store')) add('Shop Link', 'signal');
      if (p.website && p.id % 3 === 0 && (p._role === 'Brand' || p._role === 'Store')) add('Shopify', 'signal');
      if (/@\w+\.com/.test(p.bio)) add('Email', 'signal');
      if (p._us) add('US', 'signal');
      if (p._nl) add('NL', 'signal');
    }
    if (p.is_verified) add('Verified', 'signal');
    if (p.is_business) add('Business', 'signal');
    add(size(p.followers), 'size');
    const es = currentEdges(p.id);
    const others = [...new Set(es.map((e) => e.seed))].filter((s) => s !== ME).sort();
    others.forEach((s) => add('via @' + s, 'source'));
    const mine = es.filter((e) => e.seed === ME);
    const total = others.length + (mine.length ? 1 : 0);
    if (total >= 2) add(`in ${total} lists`, 'source');
    if (mine.length) add('Instagram link', 'source');
    if (mine.some((e) => e.direction === 'followers')) add('follows you', 'source');
    if (mine.some((e) => e.direction === 'following')) add('you follow', 'source');
    return t;
  }
  people.forEach((p) => tags.set(p.id, autoTags(p)));
  // Some manual tags and marks.
  const MANUAL = ['Warm intro', 'Pitch Q4', 'Met at event', 'Follow up', 'Dream client'];
  people.forEach((p) => { if (p.bio && chance(0.035)) tags.get(p.id).push({ tag: pick(MANUAL), grp: 'custom', source: 'manual' }); });
  people.forEach((p) => { if (chance(0.025)) marks.set(p.id, pick(['interested', 'interested', 'contacted', 'talking', 'client', 'no'])); });
  people.slice(0, 12).forEach((p, i) => {
    const day = new Date(); day.setDate(day.getDate() + i - 5);
    const f = { due_on: localDay(day), note: 'Check whether they need new product visuals', completed_at: i === 11 ? now() : null, updated_at: now() };
    followups.set(p.id, f);
    event(p.id, 'follow_up_scheduled', '', null, f);
    event(p.id, 'dm', 'Introduced Fortunate and asked about their next launch.');
  });
  // Extra examples for the daily view and read-state UI. Nothing is sent or fetched.
  marks.set(13, 'no');
  const rejectedDue = new Date(); rejectedDue.setDate(rejectedDue.getDate() - 2);
  setFollowUp(13, { action: 'schedule', due_on: localDay(rejectedDue), note: 'Review the reminder kept on this not-a-fit lead' });
  ['queued', 'reading', 'failed', 'done'].forEach((state, i) => reads.set(14 + i, state));

  marks.set(1, 'client'); notes.set(1, 'Met at the sample design meetup. Follow up on packaging.');
  marks.forEach((_, id) => markRevs.set(id, now()));

  // Tag rules.
  let ruleId = 3;
  const rules = [
    { id: 1, tag: 'Amazon', field: 'bio', match: 'amazon' },
    { id: 2, tag: 'Ex big brand', field: 'bio', match: 'prev.' },
  ];
  // Same semantics as server/rules.py: comma-separated keywords (whole word in bio/name/category, substring in
  // handle/website, * = any word characters) or a /regex/.
  const TEXT = ['bio', 'name', 'category'];
  function compile(match) {
    const m = String(match || '').trim();
    if (!m) throw new Error('match required');
    if (m.length >= 3 && m.startsWith('/') && m.endsWith('/')) { const rx = new RegExp(m.slice(1, -1), 'i'); return [rx, rx]; }
    const words = m.split(',').map((w) => w.trim()).filter(Boolean).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\\\*/g, '\\w*').replace(/\s+/g, '\\s+'));
    return [new RegExp('(^|\\W)(' + words.join('|') + ')(?=\\W|$)', 'i'), new RegExp(words.join('|'), 'i')];
  }
  const ruleHits = (field, match) => {
    const [word, sub] = compile(match);
    const fields = field === 'any' ? ['bio', 'name', 'handle', 'category', 'website'] : [field];
    return people.filter((p) => fields.some((f) => p[f] && (TEXT.includes(f) ? word : sub).test(p[f])));
  };
  function applyRules() {
    tags.forEach((t, id) => tags.set(id, t.filter((x) => x.source !== 'rule')));
    rules.forEach((r) => {
      r.hits = 0;
      ruleHits(r.field, r.match).forEach((p) => {
        r.hits++;
        const t = tags.get(p.id);
        if (!t.some((x) => x.tag === r.tag)) t.push({ tag: r.tag, grp: 'custom', source: 'rule' });
      });
    });
  }
  applyRules();

  // Verdicts, shaped like the server's: rule score/tier for everyone with a bio, an LLM verdict (evidence quotes,
  // "Fit: strong/good" tag) for part of them.
  const ROLE_BASE = { Brand: ['buyer', 62], Store: ['buyer', 58], Agency: ['connector', 48], Freelancer: ['connector', 44], Creative: ['collaborator', 38],
    Supplier: ['supplier', 30], SaaS: ['unrelated', 20], Coach: ['unrelated', 16], Creator: ['unrelated', 22], Personal: ['unrelated', 8] };
  const REASON = {
    buyer: (p) => pick([`Founder of a ${p._niche.toLowerCase()} brand with a live shop${p._us ? ', ships to the US' : ''}`, `Runs a DTC ${p._niche.toLowerCase()} brand selling physical product`, `${p._niche} brand at a size where ad creative pays off`]),
    connector: () => pick(['Agency serving DTC brands, could refer work', 'Freelancer in the ecom space, possible partner']),
    collaborator: () => 'Creative, not a buyer; possible collaborator',
    supplier: () => 'Supplier to brands, not a buyer',
    unrelated: (p) => p._role === 'Personal' ? 'Personal account, no brand in bio' : p._role === 'Creator' ? 'Creator, not a brand owner' : 'Sells software or coaching, not product',
  };
  const verdicts = new Map();
  people.forEach((p) => {
    const t = tags.get(p.id);
    const has = (x) => t.some((y) => y.tag === x);
    const [role, base] = ROLE_BASE[p._role];
    const n = lists(p.id);
    const score = Math.max(4, Math.min(98, Math.round(base + 6 * has('Founder') + 7 * has('Shop Link') + 4 * (n - 1) + 3 * has('Instagram link') + rnd() * 16 - 6)));
    if (!p.bio) { verdicts.set(p.id, { score: Math.min(60, 20 + 8 * n), tier: 'unread', role: null, reason: n > 1 ? `In ${n} lists, bio not read yet` : 'Bio not read yet', model: null, evidence: [] }); return; }
    const llm = chance(0.6);
    const tier = score >= 70 ? 'hot' : score >= 45 ? 'warm' : 'cold';
    const evidence = llm ? p.bio.split(' · ').slice(0, 2).concat(p.website ? [p.website.replace('https://', '')] : []) : [];
    verdicts.set(p.id, { score, tier, role, reason: REASON[role](p), model: llm ? 'z-ai/glm-5.2:free' : 'rules', evidence });
    const fit = llm && role === 'buyer' && score >= 75 ? 'Fit: strong' : llm && ['buyer', 'connector'].includes(role) && score >= 55 ? 'Fit: good' : null;
    if (fit) t.push({ tag: fit, grp: 'signal', source: 'auto' });
  });

  // Saved views.
  let viewId = 4;
  const views = [
    { id: 1, name: 'Brands in 2+ lists', query: 'any=Brand,Store&min_lists=2' },
    { id: 2, name: 'Founders, no agencies', query: 'tags=Founder&not=Agency,Freelancer' },
    { id: 3, name: 'Contacted', query: 'status=contacted' },
  ];

  const row = (p) => ({
    id: p.id, handle: p.handle, name: p.name, pic: p.pic, bio: p.bio, website: p.website, category: p.category,
    followers: p.followers, following: p.following, posts: p.posts, ...verdictFields(p.id),
    tags: tags.get(p.id), via: [...new Set(currentEdges(p.id).map((e) => e.seed))], lists: lists(p.id), status: marks.get(p.id) || null,
    history_via: [...new Set(edges.get(p.id).map((e) => e.seed))], history_lists: new Set(edges.get(p.id).map((e) => e.seed)).size,
    note: notes.get(p.id) || null, mark_rev: markRevs.get(p.id) || '', follow_up: followups.get(p.id) || null, bio_at: p.bio_at, bio_src: p.bio_src,
    profile_read_pending: ['queued', 'reading'].includes(reads.get(p.id)),
    profile_read: reads.has(p.id) ? { state: reads.get(p.id) } : null,
  });
  const verdictFields = (id) => { const v = verdicts.get(id); const es = currentEdges(id), mine = new Set(es.filter(e => e.seed === ME).map(e => e.direction));
    const relationship = mine.size === 2 ? 'mutual' : mine.has('followers') ? 'follows' : mine.has('following') ? 'followed' : null;
    const n = lists(id), connection_strength = Math.min(100, 30 + [0, 0, 25, 38, 46][Math.min(n, 4)] + Math.max(0, n - 4) * 3 + Math.min(10, es.filter(e => e.direction === 'following').length * 5) + ({mutual:16, follows:10, followed:8}[relationship] || 0));
    const business_fit = v.tier === 'unread' ? null : v.score;
    return { tier: v.tier, score: business_fit == null ? v.score : Math.round(.6 * connection_strength + .4 * business_fit), business_fit, connection_strength, relationship, role: v.role, reason: v.reason }; };
  const csv = (q, k) => (q.get(k) || '').split(',').map((s) => s.trim()).filter(Boolean);

  // Shared filter: tags (ALL), any (ANY), not (NONE), status, q, min_lists, has_bio, seed, followers_min/max.
  function filtered(q) {
    const all = csv(q, 'tags'), any = csv(q, 'any'), not = csv(q, 'not');
    const sts = csv(q, 'status').map((s) => s === 'good' ? 'interested' : s), text = (q.get('q') || '').toLowerCase().trim();
    const ml = +q.get('min_lists') || 0, hb = q.get('has_bio'), sd = (q.get('seed') || '').replace(/^@/, '').toLowerCase();
    const fmin = q.get('followers_min'), fmax = q.get('followers_max'), tiers = csv(q, 'tier');
    if (!sts.includes('all') && sts.some((s) => s !== 'none' && !STATUSES.includes(s))) invalid('bad status');
    const follow = q.get('follow_up');
    if (follow && !['due', 'overdue', 'scheduled', 'completed', 'none'].includes(follow)) invalid('invalid follow_up filter');
    const today = follow ? calendarDate(q.get('today') || localDay()) : localDay();
    return people.filter((p) => {
      if (tiers.length && !tiers.includes(verdicts.get(p.id).tier)) return false;
      const m = marks.get(p.id) || null;
      const f = followups.get(p.id);
      if (follow === 'due' && (!f || f.completed_at || f.due_on > today)) return false;
      if (follow === 'overdue' && (!f || f.completed_at || f.due_on >= today)) return false;
      if (follow === 'scheduled' && (!f || f.completed_at)) return false;
      if (follow === 'completed' && !f?.completed_at) return false;
      if (follow === 'none' && f) return false;
      if (!sts.length ? m === 'no' : !sts.includes('all') && !sts.some((s) => (s === 'none' ? m === null : m === s))) return false;
      if (ml && lists(p.id) < ml) return false;
      if (hb === '1' && !p.bio) return false;
      if (hb === '0' && p.bio) return false;
      if (fmin !== null && fmin !== '' && p.followers < +fmin) return false;
      if (fmax !== null && fmax !== '' && p.followers > +fmax) return false;
      if (sd && !edges.get(p.id).some((e) => e.seed === sd)) return false;
      if (all.length || any.length || not.length) {
        const ts = new Set(tags.get(p.id).map((x) => x.tag));
        if (!all.every((t) => ts.has(t))) return false;
        if (any.length && !any.some((t) => ts.has(t))) return false;
        if (not.some((t) => ts.has(t))) return false;
      }
      if (text && !(p.handle + ' ' + p.name + ' ' + (p.bio || '') + ' ' + (notes.get(p.id) || '')).toLowerCase().includes(text)) return false;
      return true;
    });
  }
  const TIER_RANK = { hot: 0, warm: 1, cold: 2, unread: 3 };
  const FIT = { hot: 'strong', warm: 'good', cold: 'weak', unread: 'unread' };
  function sorted(list, sort) {
    if (!['follow_up', 'connected', 'followers', 'recent', 'score', 'fit'].includes(sort)) invalid('invalid sort');
    const cmp = {
      follow_up: (a, b) => {
        const day = (p) => { const f = followups.get(p.id); return f && !f.completed_at ? f.due_on : '9999-12-31'; };
        return day(a).localeCompare(day(b));
      },
      connected: (a, b) => lists(b.id) - lists(a.id) || b.followers - a.followers,
      followers: (a, b) => b.followers - a.followers,
      recent: (a, b) => b.first_seen.localeCompare(a.first_seen),
      score: (a, b) => verdicts.get(b.id).score - verdicts.get(a.id).score || b.followers - a.followers,
      fit: (a, b) => TIER_RANK[verdicts.get(a.id).tier] - TIER_RANK[verdicts.get(b.id).tier] || lists(b.id) - lists(a.id)
        || verdicts.get(b.id).score - verdicts.get(a.id).score,
    }[sort] || ((a, b) => lists(b.id) - lists(a.id));
    return list.sort((a, b) => cmp(a, b) || a.id - b.id);
  }

  // Seed overlap (shared people), computed over all people.
  function seedLinks() {
    const m = new Map();
    edges.forEach((es) => {
      const ss = [...new Set(es.filter(e => e.state === 'observed').map((e) => e.seed))].sort();
      for (let a = 0; a < ss.length; a++) for (let b = a + 1; b < ss.length; b++) { const k = ss[a] + '|' + ss[b]; m.set(k, (m.get(k) || 0) + 1); }
    });
    return [...m].map(([k, shared]) => { const [a, b] = k.split('|'); return { source: 's:' + a, target: 's:' + b, shared }; });
  }

  let mapRev = 1;
  const bump = () => ++mapRev;

  // Scraper that makes progress: one list at a time, a page every ~9 s.
  const scraper = {
    paused: false, qualify: false, qualify_auto: true, budget: { list: 3000, profile: 300 }, today: { list: 212, profile: 0 }, peopleToday: 2431,
    lists: SEED_LIST.flatMap((s, i) => ['followers', 'following'].map((d, j) => {
      const total = Math.floor(400 + rnd() * 6000);
      const st = i < 6 ? 'done' : i === 6 && j === 0 ? 'running' : s === 'packagingstudy' && d === 'following' ? 'private' : 'queued';
      return { seed: s, direction: d, state: st, received: st === 'done' ? total : st === 'running' ? Math.floor(total * 0.37) : 0, total: st === 'queued' && rnd() < 0.4 ? null : total,
        updated_at: new Date(Date.now() - rnd() * 3600000).toISOString(), error: null };
    })),
    nextAt: Date.now() + 8000,
  };
  function tickScraper() {
    const t = Date.now();
    if (scraper.paused || stagePaused.lists || t < scraper.nextAt) return;
    const l = scraper.lists.find((x) => x.state === 'running') || scraper.lists.find((x) => x.state === 'queued');
    if (!l) return;
    l.state = 'running';
    const got = Math.min(l.direction === 'following' ? 50 : 25, (l.total ?? 99999) - l.received);
    l.received += got; l.updated_at = now();
    scraper.today.list++; scraper.peopleToday += Math.round(got * 0.6);
    if (l.total != null && l.received >= l.total) l.state = 'done';
    scraper.nextAt = t + 7000 + rnd() * 5000;
  }
  setInterval(tickScraper, 1000);
  // Accounts: four Chrome profiles; one is logged out and its list moved on, one is cooling down.
  const ago_ = (ms) => new Date(Date.now() - ms).toISOString();
  const H = 3600000;
  const acct = (o) => ({ label: null, role: 'both', is_main: false, paused: false, budget_custom: false, budget: { list: 3000, profile: 300 },
    version: '3.9.0', state: 'running', hold: null, status: 'running', online: true, healthy: true, cooldown_until: null, last_error: null,
    rate: null, last_limit: null, today: { list: 0, profile: 0 }, hour: { pages: 0, people: 0 }, activity: null, text: null, job: null, lists: [],
    last_seen: ago_(3000), first_seen: ago_(9 * 24 * H), ...o, name: '@' + o.handle });
  const accounts = [
    acct({ lane_id: 'ln_main7fk2q9xd', ig_id: '48213377', handle: 'fortun8te', is_main: true, today: { list: 0, profile: 61 }, hour: { pages: 0, people: 0 },
      job: { kind: 'profile', handle: 'saltwater.skin' }, text: 'Next request in 31s' }),
    acct({ lane_id: 'ln_scout2mw8hr', ig_id: '51120984', handle: 'fortunate.scout', role: 'lists', today: { list: 1480, profile: 0 }, hour: { pages: 212, people: 7340 },
      job: { kind: 'list', seed: 'glowbrand.co', direction: 'followers' }, last_limit: ago_(5.2 * H), rate: { pages_hour: 212, people_hour: 7340 } }),
    acct({ lane_id: 'ln_scout4q8vbe', ig_id: '51120990', handle: 'fl.north', status: 'cooldown', state: 'cooldown', cooldown_until: new Date(Date.now() + 23 * 60000).toISOString(),
      today: { list: 640, profile: 12 }, hour: { pages: 131, people: 3120 }, last_limit: ago_(7 * 60000), last_error: 'rate_limit (please_wait) on @dtc.daily followers' }),
    acct({ lane_id: 'ln_scout3pz4tn', ig_id: '51120985', handle: 'fl.research', role: 'lists', status: 'needs_login', state: 'paused', hold: 'login', healthy: false,
      today: { list: 402, profile: 0 }, hour: { pages: 64, people: 1920 }, last_error: 'login (require_login) on @kettleandco followers', last_seen: ago_(12000) }),
  ];
  const statusOf = (a) => (a.hold ? (a.hold === 'login' ? 'needs_login' : 'challenge') : a.paused ? 'paused' : a.cooldown_until ? 'cooldown' : 'running');
  function alertsView() {
    return accounts.filter((a) => a.hold === 'login').map((a) => ({ level: 'error', lane_id: a.lane_id,
      text: `${a.name} logged out — open its Chrome profile and log in; its list moved to @fortunate.scout` }));
  }
  function rateView() {
    const on = accounts.filter((a) => a.online);
    return { pages_hour: on.reduce((s, a) => s + a.hour.pages, 0), people_hour: on.reduce((s, a) => s + a.hour.people, 0), last_hit_at: ago_(7 * 60000),
      online: on.length, accounts: accounts.length, pages_last_hour: accounts.reduce((s, a) => s + a.hour.pages, 0), people_last_hour: accounts.reduce((s, a) => s + a.hour.people, 0) };
  }
  const settings = { main_list_share: 0 };
  const stagePaused = { lists: false, bios: false };
  const sites = new Map();
  function controlView() {
    const stages = [['lists', 'Collect lists', 'people'], ['bios', 'Read bios', 'bios'], ['ai', 'AI scoring', 'scores']].map(([id, label, unit]) => {
      const paused = id === 'ai' ? !scraper.qualify : scraper.paused || stagePaused[id];
      const wait = !paused && id === 'bios' ? { why: 'Pacing between bio reads', seconds: 24 } : null;
      return { id, label, unit, paused, help: 'Pausing keeps your progress. Resume continues where you left off.',
        state: paused ? 'paused' : wait ? 'waiting' : 'running', wait,
        now: paused ? 'Paused in workspace' : id === 'lists' ? '@cpgguild followers' : id === 'bios' ? 'Next bio read shortly' : 'Checking people with a bio',
        hour: paused ? 0 : id === 'lists' ? 342 : id === 'bios' ? 36 : 120,
        today: id === 'lists' ? scraper.peopleToday : id === 'bios' ? 73 : 450, queue: id === 'lists' ? 17 : 120 };
    });
    return { stages, accounts, all_paused: stages.every((s) => s.paused), at: now() };
  }
  const llm = { models: ['z-ai/glm-5.2:free', 'google/gemma-4-31b-it:free', 'nvidia/nemotron-3-super-120b-a12b:free'], daily_limit: 1000, workers: 4, llm_min: 40, bio_min: 25,
    keys: [
      { id: '3f9a1c02be', key: 'sk-…8c1d', source: 'file', disabled: false, cooldowns: {}, requests_today: { 'z-ai/glm-5.2:free': 412, 'google/gemma-4-31b-it:free': 38 }, last_error: null },
      { id: '7d20e4a911', key: 'sk-…04fa', source: 'file', disabled: false, cooldowns: { 'z-ai/glm-5.2:free': new Date(Date.now() + 42 * 60000).toISOString() }, requests_today: { 'z-ai/glm-5.2:free': 1000 }, last_error: 'z-ai/glm-5.2:free: HTTP 429' },
      { id: 'c51b7e2f08', key: 'sk-…e7b2', source: 'env', disabled: true, cooldowns: {}, requests_today: {}, last_error: 'z-ai/glm-5.2:free: HTTP 401' },
    ] };
  const llmView = () => ({ providers: [{ id: 'proxy', name: 'proxy', url: 'http://127.0.0.1:18741/api/v1/chat/completions', key: null, source: null, disabled: false, cooldowns: {}, requests_today: {}, last_error: null },
    ...llm.keys.map((k) => ({ ...k, name: 'openrouter', url: 'https://openrouter.ai/api/v1/chat/completions' }))], models: llm.models, daily_limit: llm.daily_limit,
    workers: llm.workers, llm_min: llm.llm_min, bio_min: llm.bio_min, laya: { url: 'http://127.0.0.1:18742', up: true }, config: 'data/openrouter.json', verdicts: { llm: 1180, rules: 1100 } });

  function scraperView() {
    const l = scraper.lists.find((x) => x.state === 'running');
    const secs = Math.max(0, Math.round((scraper.nextAt - Date.now()) / 1000));
    const page = l ? Math.floor(l.received / (l.direction === 'following' ? 50 : 25)) + 1 : 0;
    const w = (k) => ({ pages: Math.round(342 / k), people: Math.round(11280 / k), new_people: Math.round(6400 / k), profiles: 0 });
    return {
      ext: { online: true, version: '3.9.0', state: scraper.paused ? 'paused' : 'running', cooldown_until: null, today: scraper.today, budget: scraper.budget,
        last_seen: now(), last_error: null,
        rate: { pages_hour: scraper.paused ? 0 : 342, people_hour: scraper.paused ? 0 : 11280, last_hit_at: new Date(Date.now() - 5.2 * 3600000).toISOString() },
        activity: l ? `@${l.seed} ${l.direction} · page ${page}` : null,
        text: scraper.paused ? 'Paused in workspace' : secs > 1 ? `Next request in ${secs}s` : 'Scraping' },
      paused: scraper.paused, qualify: scraper.qualify, qualify_auto: scraper.qualify_auto, soak: { '1h': w(1), '6h': w(1 / 5.6) },
      people_today: scraper.peopleToday, lists: scraper.lists, accounts: accounts.map((a) => ({ ...a })),
      rate: rateView(), alerts: alertsView(),
      progress: {
        lists: { left: scraper.lists.filter((l) => l.state === 'queued' || l.state === 'running').reduce((n, l) => n + Math.max(0, (l.total || 1000) - l.received), 0), per_hour: 11280, eta_h: 3.4 },
        bios: { left: people.filter((p) => !p.bio).length, per_day: scraper.budget.profile, eta_h: 8.2 },
        qualify: { on: scraper.qualify, left: 1100, per_hour: scraper.qualify ? 120 : 0, eta_h: scraper.qualify ? 9.2 : null, workers: 4, keys: 2 },
      },
      queue: { list: scraper.lists.filter((x) => x.state === 'queued' || x.state === 'running').length, profile: readQueue.size },
    };
  }

  function validateTags(add, remove) {
    for (const list of [add, remove]) if (list != null && !Array.isArray(list)) throw {status:400, message:'tags must be arrays'};
    for (const t of add || []) if (typeof t !== 'string' || !t.trim() || t.trim().length > 64 || t.includes(',')) throw {status:400, message:'tag must be 1-64 characters without commas'};
  }
  function editTags(id, add, remove) {
    validateTags(add, remove);
    const t = tags.get(id).filter((x) => !(remove || []).includes(x.tag) || x.source !== 'manual');
    (add || []).forEach((a) => {
      a = a.replace(/\s+/g, ' ').trim();
      if (!a) return;
      const i = t.findIndex((x) => x.tag === a);
      if (i >= 0 && t[i].source !== 'manual') t.splice(i, 1);
      if (!t.some((x) => x.tag === a)) t.push({ tag: a, grp: 'custom', source: 'manual' });
    });
    tags.set(id, t);
    applyRules(); bump();
  }

  function route(method, url, body) {
    const u = new URL(url, location.origin);
    const q = u.searchParams;
    const path = u.pathname;
    let m;
    const fail = (message, status = 400, detail = {}) => { throw {status, message, detail}; };
    const integer = (key, fallback, lo, hi) => {
      const raw = q.get(key); if (raw == null || !raw.trim()) return fallback;
      if (!/^[+-]?\d+$/.test(raw.trim())) fail(key + ' must be a whole number');
      return Math.min(hi, Math.max(lo, Number(raw) || fallback));
    };
    if (path === '/api/leads/export' && method === 'POST') {
      if (('ids' in body) === ('query' in body)) invalid('provide either ids or query');
      if ('ids' in body) {
        if (!Array.isArray(body.ids) || !body.ids.length || body.ids.length > 5000 || body.ids.some((id) => !Number.isInteger(id) || id < 1)) invalid('ids must be 1-5000 positive ids');
        if (body.ids.some((id) => !people[id - 1])) invalid('some selected people no longer exist; refresh your selection');
      } else if (typeof body.query !== 'string' || body.query.length > 16000) invalid('query must be a URL query string');
      const p = new URLSearchParams(body.query || '');
      const chosen = Array.isArray(body.ids) ? people.filter((x) => body.ids.includes(x.id)) : sorted(filtered(p), p.get('sort') || 'score');
      const columns = ['id', 'handle', 'name', 'instagram_url', 'bio', 'website', 'followers', 'following', 'posts', 'tier', 'score', 'role', 'status', 'note', 'tags', 'sources', 'bio_at', 'bio_src', 'follow_up_due', 'follow_up_note', 'follow_up_completed_at'];
      const cell = (v) => {
        let text = String(v ?? '');
        if (typeof v !== 'number' && (/^[\s\u0000-\u001f]*[=+\-@]/.test(text) || /^[\t\r\n]/.test(text))) text = "'" + text;
        return '"' + text.replace(/"/g, '""') + '"';
      };
      const lines = [columns, ...chosen.map((p) => {
        const r = row(p), f = r.follow_up || {};
        Object.assign(r, { instagram_url: `https://www.instagram.com/${r.handle}/`, tags: r.tags.map((t) => t.tag).join('; '), sources: r.via.join('; '),
          follow_up_due: f.due_on, follow_up_note: f.note, follow_up_completed_at: f.completed_at });
        return columns.map((k) => r[k]);
      })];
      return new Response('\ufeff' + lines.map((values) => values.map(cell).join(',')).join('\r\n') + '\r\n', { headers: { 'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': 'attachment; filename="fortunate-leads-demo.csv"', 'Cache-Control': 'no-store' } });
    }
    if (path === '/api/control') {
      if (method === 'POST') {
        if (body?.action === 'start_all' && body?.stage == null) {
          scraper.paused = false;
          scraper.qualify = true;
          scraper.qualify_auto = true;
          stagePaused.lists = false;
          stagePaused.bios = false;
          for (const a of accounts) {
            a.paused = false;
            a.status = statusOf(a);
          }
          return controlView();
        }
        if (!['pause', 'resume'].includes(body?.action)) return { ok: false, error: 'Choose a stage and action' };
        const pause = body.action === 'pause';
        if (body.stage == null && typeof body.account === 'string') {   // one account (lane) only
          const a = accounts.find((x) => x.lane_id === body.account);
          if (!a) fail('no such account', 404);
          a.paused = pause; if (pause) a.job = null;
          a.status = statusOf(a);
          return controlView();
        }
        if (!['all', 'lists', 'bios', 'ai'].includes(body?.stage)) return { ok: false, error: 'Choose a stage and action' };
        if (scraper.paused) { stagePaused.lists = true; stagePaused.bios = true; scraper.paused = false; }
        // Resume all restarts collection; AI keeps its own explicit switch.
        const ids = body.stage === 'all' ? (pause ? ['lists', 'bios', 'ai'] : ['lists', 'bios']) : [body.stage];
        for (const id of ids) {
          if (id === 'ai') { scraper.qualify = !pause; if (pause) scraper.qualify_auto = false; }
          else stagePaused[id] = pause;
        }
      }
      return controlView();
    }
    if (path === '/api/qual') {
      const view = q.get('view') || 'ai';
      const ai = (p) => { const model = verdicts.get(p.id).model; return model && model !== 'rules'; };
      let list = filtered(q).filter((p) => view === 'all' || (view === 'ai' ? ai(p) : !ai(p)));
      list = q.get('sort') === 'recent' ? list.slice().sort((a, b) => b.id - a.id) : sorted(list, 'score');
      const off = Math.max(0, +q.get('offset') || 0), lim = Math.min(100, Math.max(1, +q.get('limit') || 30));
      return { total: list.length, rows: list.slice(off, off + lim).map((p) => ({ ...row(p), verdict: { ...verdicts.get(p.id), at: ago_(2 * H) }, bio_at: p.bio ? ago_(3 * H) : null, site: sites.get(p.id) || null })),
        summary: { verdicts: people.filter((p) => p.bio).length, ai: people.filter(ai).length, rules: people.filter((p) => p.bio && !ai(p)).length, with_bio: people.filter((p) => p.bio).length, sites: sites.size } };
    }
    if ((m = path.match(/^\/api\/qual\/(\d+)\/deeper$/)) && method === 'POST') {
      const p = people.find((p) => p.id === +m[1]);
      if (!p) return { ok: false, error: 'Person not found' };
      const site = p.website ? { url: p.website, final_url: p.website, title: p.name, summary: 'Sample website result for preview.', signals: { shop: 'Shopify' }, model: 'rules', at: now() } : null;
      if (site) sites.set(p.id, site);
      return { bio_queued: !p.bio, site, note: site ? null : 'No website in the bio yet.' };
    }
    if (path === '/api/leads') {
      const list = sorted(filtered(q), q.get('sort') || 'score');
      const off = integer('offset', 0, 0, Number.MAX_SAFE_INTEGER), lim = integer('limit', 50, 1, 500);
      return { total: list.length, rows: list.slice(off, off + lim).map(row), rev: mapRev };
    }
    if (path === '/api/tags') {
      const inSet = new Set(filtered(q).map((p) => p.id));
      const c = new Map();
      tags.forEach((t, id) => t.forEach((x) => {
        const k = x.tag + '|' + x.source;
        const e = c.get(k) || { tag: x.tag, grp: x.grp, source: x.source, count: 0, total: 0 };
        e.total++; if (inSet.has(id)) e.count++;
        c.set(k, e);
      }));
      return [...c.values()].sort((a, b) => b.total - a.total || a.tag.localeCompare(b.tag));
    }
    // Rename / delete touch manual tags only (like the server). Rename onto an existing tag merges, result is manual.
    if (path === '/api/tags/rename') {
      const from = String(body.from || ''), to = String(body.to || '').trim();
      if (!from || !to) return { ok: false, error: 'from and to required' };
      let renamed = 0;
      tags.forEach((t, id) => {
        if (!t.some((x) => x.tag === from && x.source === 'manual')) return;
        renamed++;
        const grp = (t.find((x) => x.tag === to) || {}).grp || 'custom';
        tags.set(id, [...t.filter((x) => !(x.tag === from && x.source === 'manual') && x.tag !== to), { tag: to, grp, source: 'manual' }]);
      });
      bump(); return { renamed };
    }
    if (path === '/api/tags/delete') {
      let deleted = 0;
      tags.forEach((t, id) => { const n = t.filter((x) => !(x.tag === body.tag && x.source === 'manual')); if (n.length !== t.length) { deleted++; tags.set(id, n); } });
      bump(); return { deleted };
    }
    if (path === '/api/people/bulk') {
      if (!Array.isArray(body.ids) || body.ids.length > 500) fail('ids must contain at most 500 people');
      if ('status' in body) body = { ...body, status: validStatus(body.status) };
      validateTags(body.add, body.remove);
      const ids = [...new Set(body.ids.map(Number).filter((id) => tags.has(id)))];
      ids.forEach((id) => {
        if ((body.add && body.add.length) || (body.remove && body.remove.length)) editTags(id, body.add, body.remove);
        if ('status' in body) { setMark(id, { status: body.status }); markRevs.set(id, marks.has(id) || notes.has(id) ? new Date(Date.now() + bump()).toISOString() : ''); }
      });
      return { ok: true, changed: ids.length };
    }
    if (path === '/api/tag-rules' && method === 'GET') return rules.map((r) => ({ ...r }));
    if (path === '/api/tag-rules/preview') { try { return { hits: ruleHits(q.get('field') || 'any', q.get('match')).length }; } catch (e) { return { ok: false, error: 'bad match: ' + e.message }; } }
    if (path === '/api/tag-rules' && method === 'POST') {
      const tag = String(body.tag || '').trim(), match = String(body.match || '').trim();
      if (!tag || !match || !['bio', 'name', 'handle', 'category', 'website', 'any'].includes(body.field)) return { ok: false, error: 'bad rule' };
      try { compile(match); } catch (e) { return { ok: false, error: 'bad regex: ' + e.message }; }
      const r = { id: ruleId++, tag, field: body.field, match };
      rules.push(r); applyRules();
      return { ...r };
    }
    if ((m = path.match(/^\/api\/tag-rules\/(\d+)\/delete$/))) {
      const i = rules.findIndex((r) => r.id === +m[1]);
      if (i >= 0) rules.splice(i, 1);
      applyRules();
      return { ok: true };
    }
    if (path === '/api/views' && method === 'GET') return views.map((v) => ({ ...v }));
    if (path === '/api/views' && method === 'POST') {
      const v = { id: viewId++, name: String(body.name || 'View').trim(), query: String(body.query || '') };
      views.push(v); return { ok: true, id: v.id };
    }
    if ((m = path.match(/^\/api\/views\/(\d+)\/delete$/))) {
      const i = views.findIndex((v) => v.id === +m[1]);
      if (i >= 0) views.splice(i, 1);
      return { ok: true };
    }
    if (path === '/api/counts') {   // each dimension counted without its own filter, like the server
      const c = { hot: 0, warm: 0, cold: 0, unread: 0, interested: 0, talking: 0, no: 0, contacted: 0, client: 0, none: 0, open: 0, total: people.length, with_bio: 0 };
      const without = (k) => { const x = new URLSearchParams(q); x.delete(k); return x; };
      filtered(without('tier')).forEach((p) => { c[verdicts.get(p.id).tier]++; });
      const st = without('status'); if (!st.get('status')) st.set('status', 'all');
      filtered(st).forEach((p) => { const s = marks.get(p.id) || 'none'; c[s]++; if (s !== 'no') c.open++; });
      people.forEach((p) => { if (p.bio) c.with_bio++; });
      return c;
    }
    if ((m = path.match(/^\/api\/person\/(\d+)(\/([\w-]+))?$/))) {
      const id = +m[1]; const p = people[id - 1];
      if (!p) return null;
      if (!m[3] && method === 'GET') return { ...row(p), edges: currentEdges(id).map(({seed, direction, observed_at}) => ({seed, direction, observed_at})), edge_history: edges.get(id).map(e => ({...e})), verdict: { ...verdicts.get(id), content_fit: verdictFields(id).business_fit }, note: notes.get(id) || null, activity: history(id) };
      if (m[3] === 'activity') {
        if (method === 'GET') return history(id, q.get('cursor'), q.has('limit') ? +q.get('limit') : 50);
        if (method === 'POST') return saveInteraction(id, body);
      }
      if (method !== 'POST') return null;
      if (m[3] === 'mark') {
        const current = () => ({status: marks.get(id) || null, note: notes.get(id) || null, mark_rev: markRevs.get(id) || ''});
        for (const key of ['if_match', 'mark_rev']) if (key in body && typeof body[key] !== 'string') fail(key + ' must be text');
        if ('if_match' in body && 'mark_rev' in body && body.if_match !== body.mark_rev) fail('conflicting revisions');
        const expected = body.if_match ?? body.mark_rev;
        if (expected !== undefined && expected !== current().mark_rev) fail('This record changed. Review the latest note before retrying.', 409, {...current(), current: current()});
        setMark(id, markInput(body));
        if ('status' in body || 'note' in body) { bump(); markRevs.set(id, marks.has(id) || notes.has(id) ? new Date(Date.now() + mapRev).toISOString() : ''); }
        return {ok: true, ...current()};
      }
      if (m[3] === 'follow-up') {
        setFollowUp(id, followUpInput(body));
        return { ok: true, follow_up: followups.get(id) || null };
      }
      if (m[3] === 'tags') { editTags(id, body.add, body.remove); return { ok: true }; }
      if (m[3] === 'read') {
        if (!['queued', 'reading'].includes(reads.get(id))) reads.set(id, 'queued');
        readQueue.add(id);
        return { ok: true };
      }
    }
    if (path === '/api/map') {
      const scope = q.get('scope') || 'leads';
      const limit = integer('limit', 400, 10, 3000);
      let list = filtered(q).filter(p => lists(p.id) > 0 && !SEED_LIST.includes(p.handle) && (!q.get('seed') || currentEdges(p.id).some(e => e.seed === q.get('seed'))));
      const total = list.length;
      list = sorted(list, scope === 'all' ? 'score' : 'connected').slice(0, limit);
      const set = new Set(list.map((p) => p.id));
      const deg = new Map();
      edges.forEach((es) => new Set(es.filter(e => e.state === 'observed').map((e) => e.seed)).forEach((s) => deg.set(s, (deg.get(s) || 0) + 1)));
      const nodes = SEED_LIST.map((s) => ({ id: 's:' + s, kind: 'seed', label: s, handle: s, pic: null, degree: deg.get(s) || 0, is_me: s === ME }));
      const links = [];
      list.forEach((p) => {
        const ss = [...new Set(currentEdges(p.id).map((e) => e.seed))];
        nodes.push({ id: 'p:' + p.id, kind: 'lead', label: p.handle, handle: p.handle, name: p.name, pic: p.pic, degree: ss.length, lists: ss.length,
          status: marks.get(p.id) || null, followers: p.followers, tags: tags.get(p.id).map((x) => x.tag).slice(0, 4), seeds: ss, ...verdictFields(p.id),
          note: notes.get(p.id) || null, fit: FIT[verdicts.get(p.id).tier] });
        edges.get(p.id).forEach((e) => { if (set.has(p.id)) links.push({ source: 's:' + e.seed, target: 'p:' + p.id, direction: e.direction, state: e.state, observed_at: e.observed_at, checked_at: e.checked_at }); });
      });
      return { nodes, links, seed_links: seedLinks(), total, limit, rev: mapRev };
    }
    if (path === '/api/scraper') return scraperView();
    if (path === '/api/accounts') { const v = scraperView(); return { accounts: v.accounts, alerts: v.alerts, rate: v.rate, main_list_share: settings.main_list_share }; }
    if (path === '/api/settings/accounts') { if (typeof body.main_list_share !== 'number' || !Number.isFinite(body.main_list_share) || body.main_list_share < 0 || body.main_list_share > 1) fail('main_list_share must be a number 0-1'); settings.main_list_share = Math.round(body.main_list_share * 100) / 100; return { ok: true, main_list_share: settings.main_list_share }; }
    if (path === '/api/llm') return llmView();
    if (path === '/api/llm/health') return { proxy: { url: 'http://127.0.0.1:18741', up: false }, laya: { url: 'http://127.0.0.1:18742', up: true } };
    if (path === '/api/llm/keys') {
      const k = String(body.key || '').trim();
      if (k.length < 16) return { ok: false, error: 'that does not look like an API key' };
      const id = Math.abs([...k].reduce((h, ch) => (h * 31 + ch.charCodeAt(0)) | 0, 7)).toString(16).padStart(10, '0').slice(0, 10);
      llm.keys.push({ id, key: 'sk-…' + k.slice(-4), source: 'file', disabled: false, cooldowns: {}, requests_today: {}, last_error: null });
      return { ok: true, id };
    }
    if ((m = path.match(/^\/api\/llm\/keys\/(\w+)\/(test|remove)$/))) {
      const i = llm.keys.findIndex((x) => x.id === m[1]);
      if (i < 0) return null;
      if (m[2] === 'remove') { llm.keys.splice(i, 1); return { ok: true }; }
      const passed = !llm.keys[i].disabled;
      return { ok: true, passed, model: llm.models[0], ms: 840, error: passed ? null : 'HTTP 401 (key refused)' };
    }
    if (path === '/api/llm/models') {
      if (body.models) llm.models = body.models;
      if (body.daily_limit != null) llm.daily_limit = body.daily_limit;
      return { ok: true, models: llm.models, daily_limit: llm.daily_limit };
    }
    if (path === '/api/setup') {
      // Setup reads do not create accounts.
      return { repo: '/Users/michael/fortunate-leads', extension_path: '/Users/michael/fortunate-leads/extension', extension_id: 'fgdbghllamedgihmdcolaggnbhnakjnf',
        extension_version: '3.9.0', server: 'http://127.0.0.1:8777', lanes: accounts.length };
    }
    if ((m = path.match(/^\/api\/accounts\/([\w-]+)\/remove$/))) {
      const i = accounts.findIndex((a) => a.lane_id === m[1]);
      if (i >= 0) accounts.splice(i, 1);
      return { ok: true, removed: i >= 0 ? 1 : 0 };
    }
    if ((m = path.match(/^\/api\/accounts\/([\w-]+)$/)) && method === 'POST') {
      const a = accounts.find((x) => x.lane_id === m[1]);
      if (!a) return null;
      if ('role' in body && !['lists','bios','both'].includes(body.role)) fail('role must be lists, bios or both');
      for (const k of ['paused','is_main']) if (k in body && typeof body[k] !== 'boolean') fail(k + ' must be true or false');
      if ('label' in body && body.label !== null && (typeof body.label !== 'string' || body.label.trim().length > 40)) fail('label must be up to 40 characters');
      if ('budget' in body && body.budget !== null) {
        if (typeof body.budget !== 'object' || Array.isArray(body.budget)) fail('budget must be {list, profile} or null');
        for (const k of ['list','profile']) if (k in body.budget && body.budget[k] !== null && (typeof body.budget[k] !== 'number' || !Number.isFinite(body.budget[k]))) fail('budget.' + k + ' must be a number');
      }
      if (body.role) a.role = body.role;
      if ('paused' in body) { a.paused = body.paused; if (a.paused) a.job = null; }
      if ('is_main' in body) a.is_main = !!body.is_main;
      if ('label' in body) { a.label = body.label; a.name = a.handle ? '@' + a.handle : a.label || 'lane ' + a.lane_id.slice(0, 8); }
      if ('budget' in body) {
        a.budget_custom = !!body.budget;
        a.budget = { list: body.budget?.list ?? 3000, profile: body.budget?.profile ?? 300 };
      }
      a.status = statusOf(a);
      return { ok: true, account: { ...a } };
    }
    if (path === '/api/scraper/pause') { if (typeof body.paused !== 'boolean') fail('paused must be true or false'); scraper.paused = body.paused; stagePaused.lists = stagePaused.bios = body.paused; return { ok: true }; }
    if (path === '/api/settings/qualify') {
      if ('on' in body) { if (typeof body.on !== 'boolean') fail('on must be true or false'); scraper.qualify = body.on; if (!body.on) scraper.qualify_auto = false; }
      if ('auto' in body) scraper.qualify_auto = !!body.auto;
      for (const k of ['workers', 'llm_min', 'bio_min']) if (k in body) llm[k] = body[k];
      return { ok: true, qualify: scraper.qualify };
    }
    if (path === '/api/scraper/budget') { for (const [k, cap] of [['list',10000],['profile',2000]]) if (k in body && (!Number.isInteger(body[k]) || body[k] < 0 || body[k] > cap)) fail(k + ' budget must be a whole number within its limit'); scraper.budget = {...scraper.budget, ...body}; return { ok: true }; }
    if (path === '/api/scraper/snowball') {
      const want = body.min_status === 'client' ? ['client'] : ['interested', 'talking', 'client'];
      const seeds = people.filter((p) => want.includes(marks.get(p.id)) && !scraper.lists.some((l) => l.seed === p.handle && l.direction === 'following'))
        .slice(0, body.limit || 50).map((p) => p.handle);
      seeds.forEach((h) => scraper.lists.push({ seed: h, direction: 'following', state: 'queued', received: 0, total: null, updated_at: now(), error: null }));
      return { ok: true, queued: seeds.length, seeds };
    }
    if (path === '/api/scraper/seeds') {
      let queued = 0;
      body.handles.forEach((h) => body.directions.forEach((d) => { if (!scraper.lists.some((l) => l.seed === h && l.direction === d)) { queued++; scraper.lists.push({ seed: h, direction: d, state: 'queued', received: 0, total: null, updated_at: now(), error: null }); } }));
      return { ok: true, queued };
    }
    return null;
  }

  const realFetch = window.fetch.bind(window);
  window.fetch = function (url, opts = {}) {
    const s = String(url);
    if (!s.startsWith('/api/')) return realFetch(url, opts);
    let res, failure = 0;
    try {
      let body = {};
      if (opts.body) {
        try { body = JSON.parse(opts.body); } catch (_) { invalid('body must be valid JSON'); }
      }
      if (!isObject(body)) invalid('body must be a JSON object');
      res = route((opts.method || 'GET').toUpperCase(), s, body);
    } catch (e) {
      failure = e.mockStatus || e.status || 500;
      if (failure === 500) console.warn('mock', s, e);
      res = { ok: false, error: e.message || 'mock error', ...e.detail };
    }
    if (res instanceof Response) return Promise.resolve(res);
    const bad = res && res.ok === false;
    return new Promise((r) => setTimeout(() => r(new Response(JSON.stringify(res ?? { ok: false, error: 'not found' }), { status: failure || (!res ? 404 : bad ? 400 : 200), headers: { 'Content-Type': 'application/json' } })), 40 + Math.random() * 80));
  };
})();
