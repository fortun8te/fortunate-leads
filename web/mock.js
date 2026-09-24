// Dev-only mock backend. Loaded only when the page URL has ?mock=1.
(function () {
  let seed = 7;
  const rnd = () => ((seed = (seed * 16807) % 2147483647) - 1) / 2147483646;
  const pick = (a) => a[Math.floor(rnd() * a.length)];
  const chance = (p) => rnd() < p;

  const SEEDS = ['fortun8te', 'dtcdaily', 'brandfounders.club', 'ecomcollective', 'skincarebusiness', 'cpgguild', 'shopify.founders', 'packagingstudy'];
  const ME = 'fortun8te';
  const FIRST = ['Maya', 'Jonas', 'Sofia', 'Liam', 'Ava', 'Noah', 'Emma', 'Lucas', 'Chloe', 'Daan', 'Isla', 'Mateo', 'Nora', 'Eli', 'Zoe', 'Theo', 'Lena', 'Ravi', 'Priya', 'Jade', 'Marcus', 'Tessa', 'Owen', 'Ines', 'Kai', 'Freya', 'Sam', 'Anouk', 'Leo', 'Hana', 'Dylan', 'Mila', 'Ruben', 'Carla', 'Felix', 'Amara'];
  const LAST = ['Carter', 'de Vries', 'Nguyen', 'Brooks', 'Janssen', 'Patel', 'Morales', 'Kim', 'Bakker', 'Hughes', 'Rossi', 'Walsh', 'Visser', 'Chen', 'Ellis', 'Park', 'Reyes', 'Smit', 'Hayes', 'Ortiz'];
  const NICHES = {
    Skincare: ['clean skincare for sensitive skin', 'barrier-first serums', 'SPF you actually reapply', 'skincare for men who hate skincare'],
    Supplements: ['magnesium that tastes good', 'daily greens without the grass taste', 'creatine gummies', 'sleep stack, no melatonin'],
    Apparel: ['heavyweight tees made in Portugal', 'running kit for bad weather', 'linen basics', 'golf apparel for people under 40'],
    Coffee: ['single-origin cold brew cans', 'specialty coffee subscription', 'mushroom coffee, no jitters'],
    Pet: ['dog treats with one ingredient', 'cat furniture that looks like furniture', 'raw dog food delivered'],
    Home: ['candles poured in Brooklyn', 'ceramic cookware', 'bedding for hot sleepers'],
    Haircare: ['scalp care for thinning hair', 'curl cream that holds', 'salt spray for men'],
    Fitness: ['adjustable kettlebells', 'resistance bands that last', 'protein bars with real food'],
  };
  const BRANDWORDS = ['luma', 'north', 'oat', 'kind', 'hale', 'fern', 'dune', 'mora', 'vela', 'solo', 'ember', 'tide', 'noon', 'pax', 'ruby', 'loft'];
  const ROLES = ['Founder', 'Co-founder', 'CMO', 'Creative director', 'Head of growth', 'Marketer'];

  const people = [];
  const edges = [];
  const tags = new Map();
  const verdict = new Map();
  const marks = new Map();
  const notes = new Map();
  const N = 1200;

  for (let i = 1; i <= N; i++) {
    const f = pick(FIRST), l = pick(LAST);
    const niche = pick(Object.keys(NICHES));
    const brand = pick(BRANDWORDS) + (chance(0.5) ? '.' + niche.toLowerCase().slice(0, 4) : pick(['co', 'labs', 'goods', 'studio']));
    const role = pick(ROLES);
    const handle = (f + (chance(0.5) ? '.' : '') + l.split(' ').pop()).toLowerCase().replace(/[^a-z.]/g, '') + (chance(0.4) ? Math.floor(rnd() * 99) : '');
    const hasBio = i <= 320 || chance(0.08);
    const followers = Math.floor(Math.pow(10, 2.5 + rnd() * 3));
    const p = {
      id: i, handle: handle + (people.some((x) => x.handle === handle) ? i : ''), name: f + ' ' + l,
      pic: chance(0.82) ? `https://randomuser.me/api/portraits/${chance(0.5) ? 'women' : 'men'}/${Math.floor(rnd() * 99)}.jpg` : null,
      bio: hasBio ? `${role} @${brand} · ${pick(NICHES[niche])}${chance(0.5) ? ' · ships to the US' : ''}${chance(0.3) ? ' · prev. ' + pick(['Glossier', 'Gymshark', 'Allbirds', 'Olaplex', 'AG1']) : ''}` : null,
      website: hasBio && chance(0.7) ? `https://${brand.replace('.', '')}.com` : null,
      followers, following: Math.floor(200 + rnd() * 1800), posts: Math.floor(10 + rnd() * 900),
      _niche: niche, _role: role,
    };
    people.push(p);
    const nSeeds = chance(0.2) ? (chance(0.35) ? 3 : 2) : 1;
    const ss = new Set();
    while (ss.size < nSeeds) ss.add(pick(SEEDS));
    ss.forEach((s) => edges.push({ seed: s, person_id: i, direction: chance(0.75) ? 'followers' : 'following' }));

    const t = [];
    const seeds = [...ss];
    seeds.slice(0, 2).forEach((s) => t.push({ tag: 'via @' + s, grp: 'source', source: 'auto' }));
    if (seeds.length >= 2) t.push({ tag: `in ${seeds.length} lists`, grp: 'source', source: 'auto' });
    if (ss.has(ME)) t.push({ tag: 'knows you', grp: 'source', source: 'auto' });
    if (hasBio) {
      t.push({ tag: role === 'Co-founder' ? 'Founder' : role, grp: 'role', source: 'auto' });
      t.push({ tag: niche, grp: 'niche', source: 'auto' });
      if (p.website) t.push({ tag: 'Shop link', grp: 'signal', source: 'auto' });
      if (chance(0.25)) t.push({ tag: 'Running ads', grp: 'signal', source: 'auto' });
      if (chance(0.15)) t.push({ tag: 'Launching', grp: 'signal', source: 'auto' });
      if (p.bio.includes('US')) t.push({ tag: 'US shipping', grp: 'signal', source: 'auto' });
    }
    t.push({ tag: followers > 100000 ? '100k+' : followers > 10000 ? '10k–100k' : followers > 1000 ? '1k–10k' : 'Under 1k', grp: 'size', source: 'auto' });
    if (hasBio && chance(0.04)) t.push({ tag: 'Warm intro', grp: 'signal', source: 'manual' });
    tags.set(i, t);

    let score, tier, reason;
    if (!hasBio) {
      score = Math.floor(10 + rnd() * 40 + seeds.length * 8); tier = 'unread';
      reason = seeds.length > 1 ? `Linked to ${seeds.length} seeds, bio not read yet` : 'Bio not read yet';
    } else {
      const base = (role.includes('ounder') ? 35 : role === 'Marketer' ? 10 : 22) + (p.website ? 12 : 0) + seeds.length * 7 + rnd() * 35;
      score = Math.min(98, Math.floor(base)); tier = score >= 70 ? 'hot' : score >= 45 ? 'warm' : 'cold';
      reason = tier === 'hot'
        ? pick([`${role} of a ${niche.toLowerCase()} brand with a live shop, US customers`, `Runs a DTC ${niche.toLowerCase()} brand, sells physical product online`, `${role} at @${brand}, product brand at ad-spend size`])
        : tier === 'warm' ? pick([`${niche} brand, unclear if they run paid social`, `Works at a product brand but not the decision maker`, `Small ${niche.toLowerCase()} shop, may be under budget`])
        : pick(['Agency or service business, not a product brand', 'Personal account, no brand in bio', 'Creator, not a brand owner']);
    }
    verdict.set(i, { score, tier, role: hasBio ? role : null, reason, model: hasBio ? 'rules' : null });
  }
  for (let i = 1; i <= 14; i++) marks.set(i * 3, pick(['good', 'maybe', 'contacted']));

  const via = (id) => edges.filter((e) => e.person_id === id).map((e) => e.seed);
  const row = (p) => {
    const v = verdict.get(p.id);
    return { id: p.id, handle: p.handle, name: p.name, pic: p.pic, bio: p.bio, website: p.website, followers: p.followers, following: p.following, posts: p.posts,
      tier: v.tier, score: v.score, role: v.role, reason: v.reason, tags: tags.get(p.id), via: via(p.id), status: marks.get(p.id) || null };
  };

  let mapRev = 1;
  let extraMap = 0;
  setInterval(() => { mapRev++; extraMap += 6; }, 25000);

  // Scraper that makes progress: one list at a time, a page every ~9 s.
  const scraper = {
    paused: false, budget: { list: 2000, profile: 150 }, today: { list: 212, profile: 0 }, peopleToday: 2431,
    lists: SEEDS.flatMap((s, i) => ['followers', 'following'].map((d, j) => {
      const total = Math.floor(400 + rnd() * 6000);
      const st = i < 3 ? 'done' : i === 3 && j === 0 ? 'running' : s === 'packagingstudy' && d === 'following' ? 'private' : 'queued';
      return { seed: s, direction: d, state: st, received: st === 'done' ? total : st === 'running' ? Math.floor(total * 0.37) : 0, total: st === 'queued' && rnd() < 0.4 ? null : total,
        updated_at: new Date(Date.now() - rnd() * 3600000).toISOString(), error: null };
    })),
    nextAt: Date.now() + 8000,
  };
  function tickScraper() {
    const now = Date.now();
    if (scraper.paused || now < scraper.nextAt) return;
    let l = scraper.lists.find((x) => x.state === 'running') || scraper.lists.find((x) => x.state === 'queued');
    if (!l) return;
    l.state = 'running';
    const size = l.direction === 'following' ? 50 : 25;
    const got = Math.min(size, (l.total ?? 99999) - l.received);
    l.received += got; l.updated_at = new Date().toISOString();
    scraper.today.list++; scraper.peopleToday += Math.round(got * 0.6);
    if (l.total != null && l.received >= l.total) l.state = 'done';
    scraper.nextAt = now + 7000 + rnd() * 5000;
  }
  setInterval(tickScraper, 1000);
  function scraperView() {
    const l = scraper.lists.find((x) => x.state === 'running');
    const secs = Math.max(0, Math.round((scraper.nextAt - Date.now()) / 1000));
    const page = l ? Math.floor(l.received / (l.direction === 'following' ? 50 : 25)) + 1 : 0;
    return {
      ext: { online: true, version: '3.1.0', state: scraper.paused ? 'paused' : 'running', cooldown_until: null, today: scraper.today, budget: scraper.budget,
        last_seen: new Date().toISOString(), last_error: null,
        activity: l ? `@${l.seed} ${l.direction} · page ${page}` : null,
        text: scraper.paused ? 'Paused in workspace' : secs > 1 ? `Next request in ${secs}s` : 'Scraping' },
      paused: scraper.paused, people_today: scraper.peopleToday, lists: scraper.lists,
      queue: { list: scraper.lists.filter((x) => x.state === 'queued' || x.state === 'running').length, profile: 0 },
    };
  }

  function route(method, url, body) {
    const u = new URL(url, location.origin);
    const q = u.searchParams;
    const path = u.pathname;
    if (path === '/api/leads') {
      let list = people.map(row);
      const tiers = (q.get('tier') || '').split(',').filter(Boolean);
      if (tiers.length) list = list.filter((r) => tiers.includes(r.tier));
      const st = q.get('status');
      if (st) list = list.filter((r) => r.status === st); else list = list.filter((r) => r.status !== 'no');
      const tg = (q.get('tags') || '').split(',').filter(Boolean);
      if (tg.length) list = list.filter((r) => tg.every((t) => r.tags.some((x) => x.tag === t)));
      const s = (q.get('q') || '').toLowerCase();
      if (s) list = list.filter((r) => (r.handle + ' ' + r.name + ' ' + (r.bio || '')).toLowerCase().includes(s));
      const sort = q.get('sort') || 'score';
      list.sort(sort === 'followers' ? (a, b) => b.followers - a.followers : sort === 'recent' ? (a, b) => b.id - a.id : (a, b) => b.score - a.score);
      const off = +q.get('offset') || 0, lim = +q.get('limit') || 50;
      return { total: list.length, rows: list.slice(off, off + lim) };
    }
    if (path === '/api/tags') {
      const c = new Map();
      tags.forEach((t) => t.forEach((x) => { const k = x.tag; const e = c.get(k) || { tag: k, grp: x.grp, count: 0 }; e.count++; c.set(k, e); }));
      return [...c.values()].sort((a, b) => b.count - a.count);
    }
    if (path === '/api/counts') {
      const c = { hot: 0, warm: 0, cold: 0, unread: 0, good: 0, maybe: 0, contacted: 0, total: 0, with_bio: 0 };
      people.forEach((p) => { const v = verdict.get(p.id); const m = marks.get(p.id); if (m === 'no') return; c[v.tier]++; c.total++; if (p.bio) c.with_bio++; if (m && c[m] !== undefined) c[m]++; });
      return c;
    }
    let m;
    if ((m = path.match(/^\/api\/person\/(\d+)(\/(\w+))?$/))) {
      const id = +m[1]; const p = people[id - 1];
      if (!m[3]) return { ...row(p), edges: edges.filter((e) => e.person_id === id).map((e) => ({ seed: e.seed, direction: e.direction })), verdict: verdict.get(id), note: notes.get(id) || '' };
      if (m[3] === 'mark') { if (body.status) marks.set(id, body.status); else marks.delete(id); if (body.note !== undefined) notes.set(id, body.note); return { ok: true }; }
      if (m[3] === 'tags') {
        const t = tags.get(id).filter((x) => !(body.remove || []).includes(x.tag));
        (body.add || []).forEach((a) => { if (!t.some((x) => x.tag === a)) t.push({ tag: a, grp: 'signal', source: 'manual' }); });
        tags.set(id, t); return { ok: true };
      }
      if (m[3] === 'read') return { ok: true };
    }
    if (path === '/api/map') {
      const scope = q.get('scope') || 'leads';
      let ids = people.map((p) => p.id);
      if (scope === 'leads') ids = ids.filter((id) => verdict.get(id).score >= 50 || via(id).length >= 2).slice(0, 400 + extraMap);
      else ids = ids.slice(0, 1100 + extraMap);
      const set = new Set(ids);
      const nodes = SEEDS.map((s) => ({ id: 's:' + s, kind: 'seed', label: s, tier: null, score: null, pic: null, degree: edges.filter((e) => e.seed === s && set.has(e.person_id)).length }));
      ids.forEach((id) => { const p = people[id - 1]; const v = verdict.get(id); nodes.push({ id: 'p:' + id, kind: 'lead', label: p.name, handle: p.handle, tier: v.tier, score: v.score, pic: p.pic, degree: via(id).length, tags: tags.get(id).map((x) => x.tag), reason: v.reason }); });
      const links = edges.filter((e) => set.has(e.person_id)).map((e) => ({ source: 's:' + e.seed, target: 'p:' + e.person_id, direction: e.direction }));
      return { nodes, links, rev: mapRev * 10 + (scope === 'leads' ? 1 : 2) };
    }
    if (path === '/api/scraper') return scraperView();
    if (path === '/api/scraper/pause') { scraper.paused = !!body.paused; return { ok: true }; }
    if (path === '/api/scraper/budget') { scraper.budget = { list: +body.list, profile: +body.profile }; return { ok: true }; }
    if (path === '/api/scraper/seeds') {
      let queued = 0;
      body.handles.forEach((h) => body.directions.forEach((d) => { if (!scraper.lists.some((l) => l.seed === h && l.direction === d)) { queued++; scraper.lists.push({ seed: h, direction: d, state: 'queued', received: 0, total: null, updated_at: new Date().toISOString(), error: null }); } }));
      return { ok: true, queued };
    }
    return null;
  }

  const realFetch = window.fetch.bind(window);
  window.fetch = function (url, opts = {}) {
    const s = String(url);
    if (!s.startsWith('/api/')) return realFetch(url, opts);
    const body = opts.body ? JSON.parse(opts.body) : null;
    const res = route(opts.method || 'GET', s, body);
    return new Promise((r) => setTimeout(() => r(new Response(JSON.stringify(res ?? { ok: false, error: 'not found' }), { status: res ? 200 : 404, headers: { 'Content-Type': 'application/json' } })), 60 + Math.random() * 90));
  };
})();
