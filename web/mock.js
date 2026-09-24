// Dev-only mock backend. Loaded only when the page URL has ?mock=1.
// Implements the full UI API (see docs/CONTRACT.md) in memory, including shared filters,
// tag facets, bulk edits, tag rules, saved views and the seed map.
(function () {
  let seed = 11;
  const rnd = () => ((seed = (seed * 16807) % 2147483647) - 1) / 2147483646;
  const pick = (a) => a[Math.floor(rnd() * a.length)];
  const chance = (p) => rnd() < p;
  const now = () => new Date().toISOString();

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
  const lists = (id) => new Set(edges.get(id).map((e) => e.seed)).size;
  const edges = new Map(); // id -> [{seed, direction}]
  const tags = new Map(); // id -> [{tag, grp, source}]
  const marks = new Map();
  const notes = new Map();
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
      _role: role, _niche: niche, _us: us, _nl: nl,
    });
    // Seeds: primary plus neighbours (overlap) plus occasional random.
    const ss = new Set([primary]);
    for (const nb of SEEDS[primary].nb) if (chance(0.16)) ss.add(nb);
    if (chance(0.05)) ss.add(pickSeed());
    if (ss.size >= 2 && chance(0.2)) ss.add(pickSeed());
    edges.set(i, [...ss].map((s) => ({ seed: s, direction: s === ME ? (chance(0.6) ? 'followers' : 'following') : chance(0.75) ? 'followers' : 'following' })));
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
    const es = edges.get(p.id);
    const others = [...new Set(es.map((e) => e.seed))].filter((s) => s !== ME).sort();
    others.forEach((s) => add('via @' + s, 'source'));
    const mine = es.filter((e) => e.seed === ME);
    const total = others.length + (mine.length ? 1 : 0);
    if (total >= 2) add(`in ${total} lists`, 'source');
    if (mine.length) add('knows you', 'source');
    if (mine.some((e) => e.direction === 'followers')) add('follows you', 'source');
    if (mine.some((e) => e.direction === 'following')) add('you follow', 'source');
    return t;
  }
  people.forEach((p) => tags.set(p.id, autoTags(p)));
  // Some manual tags and marks.
  const MANUAL = ['Warm intro', 'Pitch Q4', 'Met at event', 'Follow up', 'Dream client'];
  people.forEach((p) => { if (p.bio && chance(0.035)) tags.get(p.id).push({ tag: pick(MANUAL), grp: 'custom', source: 'manual' }); });
  people.forEach((p) => { if (chance(0.025)) marks.set(p.id, pick(['good', 'good', 'maybe', 'contacted', 'contacted', 'client', 'no', 'known'])); });

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
    const score = Math.max(4, Math.min(98, Math.round(base + 6 * has('Founder') + 7 * has('Shop Link') + 4 * (n - 1) + 3 * has('knows you') + rnd() * 16 - 6)));
    if (!p.bio) { verdicts.set(p.id, { score: Math.min(60, 20 + 8 * n), tier: 'unread', role: null, reason: n > 1 ? `In ${n} lists, bio not read yet` : 'Bio not read yet', model: null, evidence: '[]' }); return; }
    const llm = chance(0.6);
    const tier = score >= 70 ? 'hot' : score >= 45 ? 'warm' : 'cold';
    const evidence = llm ? p.bio.split(' · ').slice(0, 2).concat(p.website ? [p.website.replace('https://', '')] : []) : [];
    verdicts.set(p.id, { score, tier, role, reason: REASON[role](p), model: llm ? 'meta-llama/llama-3.3-70b-instruct:free' : 'rules', evidence: JSON.stringify(evidence) });
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
    tags: tags.get(p.id), via: [...new Set(edges.get(p.id).map((e) => e.seed))], lists: lists(p.id), status: marks.get(p.id) || null,
  });
  const verdictFields = (id) => { const v = verdicts.get(id); return { tier: v.tier, score: v.score, role: v.role, reason: v.reason }; };
  const csv = (q, k) => (q.get(k) || '').split(',').map((s) => s.trim()).filter(Boolean);

  // Shared filter: tags (ALL), any (ANY), not (NONE), status, q, min_lists, has_bio, seed, followers_min/max.
  function filtered(q) {
    const all = csv(q, 'tags'), any = csv(q, 'any'), not = csv(q, 'not');
    const sts = csv(q, 'status'), text = (q.get('q') || '').toLowerCase().trim();
    const ml = +q.get('min_lists') || 0, hb = q.get('has_bio'), sd = (q.get('seed') || '').replace(/^@/, '').toLowerCase();
    const fmin = q.get('followers_min'), fmax = q.get('followers_max'), tiers = csv(q, 'tier');
    return people.filter((p) => {
      if (tiers.length && !tiers.includes(verdicts.get(p.id).tier)) return false;
      const m = marks.get(p.id) || null;
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
      if (text && !(p.handle + ' ' + p.name + ' ' + (p.bio || '')).toLowerCase().includes(text)) return false;
      return true;
    });
  }
  function sorted(list, sort) {
    const cmp = {
      connected: (a, b) => lists(b.id) - lists(a.id) || b.followers - a.followers,
      followers: (a, b) => b.followers - a.followers,
      recent: (a, b) => b.first_seen.localeCompare(a.first_seen),
      score: (a, b) => verdicts.get(b.id).score - verdicts.get(a.id).score || b.followers - a.followers,
    }[sort] || ((a, b) => lists(b.id) - lists(a.id));
    return list.sort((a, b) => cmp(a, b) || a.id - b.id);
  }

  // Seed overlap (shared people), computed over all people.
  function seedLinks() {
    const m = new Map();
    edges.forEach((es) => {
      const ss = [...new Set(es.map((e) => e.seed))].sort();
      for (let a = 0; a < ss.length; a++) for (let b = a + 1; b < ss.length; b++) { const k = ss[a] + '|' + ss[b]; m.set(k, (m.get(k) || 0) + 1); }
    });
    return [...m].map(([k, shared]) => { const [a, b] = k.split('|'); return { source: 's:' + a, target: 's:' + b, shared }; });
  }

  let mapRev = 1;
  let extraMap = 0;
  setInterval(() => { mapRev++; extraMap += 5; }, 25000);

  // Scraper that makes progress: one list at a time, a page every ~9 s.
  const scraper = {
    paused: false, qualify: false, budget: { list: 2000, profile: 150 }, today: { list: 212, profile: 0 }, peopleToday: 2431,
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
    if (scraper.paused || t < scraper.nextAt) return;
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
  function scraperView() {
    const l = scraper.lists.find((x) => x.state === 'running');
    const secs = Math.max(0, Math.round((scraper.nextAt - Date.now()) / 1000));
    const page = l ? Math.floor(l.received / (l.direction === 'following' ? 50 : 25)) + 1 : 0;
    const w = (k) => ({ pages: Math.round(342 / k), people: Math.round(11280 / k), new_people: Math.round(6400 / k), profiles: 0 });
    return {
      ext: { online: true, version: '3.1.0', state: scraper.paused ? 'paused' : 'running', cooldown_until: null, today: scraper.today, budget: scraper.budget,
        last_seen: now(), last_error: null,
        rate: { pages_hour: scraper.paused ? 0 : 342, people_hour: scraper.paused ? 0 : 11280, last_hit_at: new Date(Date.now() - 5.2 * 3600000).toISOString() },
        activity: l ? `@${l.seed} ${l.direction} · page ${page}` : null,
        text: scraper.paused ? 'Paused in workspace' : secs > 1 ? `Next request in ${secs}s` : 'Scraping' },
      paused: scraper.paused, qualify: scraper.qualify, qualify_auto: true, soak: { '1h': w(1), '6h': w(1 / 5.6) },
      people_today: scraper.peopleToday, lists: scraper.lists,
      queue: { list: scraper.lists.filter((x) => x.state === 'queued' || x.state === 'running').length, profile: 0 },
    };
  }

  function editTags(id, add, remove) {
    const t = tags.get(id).filter((x) => !(remove || []).includes(x.tag) || x.source === 'auto');
    (add || []).forEach((a) => {
      a = String(a).trim();
      if (!a) return;
      const i = t.findIndex((x) => x.tag === a);
      if (i >= 0 && t[i].source !== 'manual') t.splice(i, 1);
      if (!t.some((x) => x.tag === a)) t.push({ tag: a, grp: 'custom', source: 'manual' });
    });
    tags.set(id, t);
  }

  function route(method, url, body) {
    const u = new URL(url, location.origin);
    const q = u.searchParams;
    const path = u.pathname;
    let m;
    if (path === '/api/leads') {
      const list = sorted(filtered(q), q.get('sort') || 'score');
      const off = +q.get('offset') || 0, lim = Math.min(500, +q.get('limit') || 50);
      return { total: list.length, rows: list.slice(off, off + lim).map(row) };
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
      return { renamed };
    }
    if (path === '/api/tags/delete') {
      let deleted = 0;
      tags.forEach((t, id) => { const n = t.filter((x) => !(x.tag === body.tag && x.source === 'manual')); if (n.length !== t.length) { deleted++; tags.set(id, n); } });
      return { deleted };
    }
    if (path === '/api/people/bulk') {
      const ids = (body.ids || []).map(Number).filter((id) => tags.has(id));
      ids.forEach((id) => {
        if ((body.add && body.add.length) || (body.remove && body.remove.length)) editTags(id, body.add, body.remove);
        if ('status' in body) { if (body.status) marks.set(id, body.status); else marks.delete(id); }
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
    if (path === '/api/counts') {
      const c = { hot: 0, warm: 0, cold: 0, unread: 0, good: 0, maybe: 0, no: 0, contacted: 0, client: 0, known: 0, total: 0, with_bio: 0 };
      people.forEach((p) => { const s = marks.get(p.id); c[verdicts.get(p.id).tier]++; if (s) c[s]++; if (s === 'no') return; c.total++; if (p.bio) c.with_bio++; });
      return c;
    }
    if ((m = path.match(/^\/api\/person\/(\d+)(\/(\w+))?$/))) {
      const id = +m[1]; const p = people[id - 1];
      if (!p) return null;
      if (!m[3]) return { ...row(p), edges: edges.get(id).map((e) => ({ ...e })), verdict: { ...verdicts.get(id) }, note: notes.get(id) || '' };
      if (m[3] === 'mark') { if (body.status) marks.set(id, body.status); else marks.delete(id); if (body.note !== undefined) notes.set(id, body.note); return { ok: true }; }
      if (m[3] === 'tags') { editTags(id, body.add, body.remove); return { ok: true }; }
      if (m[3] === 'read') return { ok: true };
    }
    if (path === '/api/map') {
      const scope = q.get('scope') || 'leads';
      const limit = Math.min(5000, +q.get('limit') || 400);
      let list = filtered(q);
      if (scope === 'leads') list = list.filter((p) => lists(p.id) >= 2 || (tags.get(p.id).some((x) => x.tag === 'Brand' || x.tag === 'Founder') && p.followers > 3000));
      list = sorted(list, 'connected').slice(0, limit + (scope === 'leads' ? extraMap : 0));
      const set = new Set(list.map((p) => p.id));
      const deg = new Map();
      edges.forEach((es) => new Set(es.map((e) => e.seed)).forEach((s) => deg.set(s, (deg.get(s) || 0) + 1)));
      const nodes = SEED_LIST.map((s) => ({ id: 's:' + s, kind: 'seed', label: s, handle: s, pic: null, degree: deg.get(s) || 0, is_me: s === ME }));
      const links = [];
      list.forEach((p) => {
        const ss = [...new Set(edges.get(p.id).map((e) => e.seed))];
        nodes.push({ id: 'p:' + p.id, kind: 'lead', label: p.handle, handle: p.handle, name: p.name, pic: p.pic, degree: ss.length, lists: ss.length,
          status: marks.get(p.id) || null, followers: p.followers, tags: tags.get(p.id).map((x) => x.tag).slice(0, 4), seeds: ss, ...verdictFields(p.id) });
        edges.get(p.id).forEach((e) => { if (set.has(p.id)) links.push({ source: 's:' + e.seed, target: 'p:' + p.id, direction: e.direction }); });
      });
      return { nodes, links, seed_links: seedLinks(), rev: mapRev * 1000 + list.length };
    }
    if (path === '/api/scraper') return scraperView();
    if (path === '/api/scraper/pause') { scraper.paused = !!body.paused; return { ok: true }; }
    if (path === '/api/settings/qualify') { scraper.qualify = !!body.on; return { ok: true, qualify: scraper.qualify }; }
    if (path === '/api/scraper/budget') { scraper.budget = { list: +body.list, profile: +body.profile }; return { ok: true }; }
    if (path === '/api/scraper/snowball') {
      const want = body.min_status === 'client' ? ['client'] : ['good', 'client'];
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
    const body = opts.body ? JSON.parse(opts.body) : null;
    let res;
    try { res = route(opts.method || 'GET', s, body); } catch (e) { console.warn('mock', s, e); res = null; }
    const bad = res && res.ok === false;
    return new Promise((r) => setTimeout(() => r(new Response(JSON.stringify(res ?? { ok: false, error: 'not found' }), { status: !res ? 404 : bad ? 400 : 200, headers: { 'Content-Type': 'application/json' } })), 40 + Math.random() * 80));
  };
})();
