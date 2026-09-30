/* Synthetic map world: 10 million deterministic people behind the map view contract.
 *
 * Only for development and tests (?mock=1 or ?mapmock=1). Nothing here is real data.
 * Runs in a Worker (see map-mock.js) so answering a request never blocks the page,
 * and in Node for the tests. It answers the same three calls as the server:
 *   view({mode,x0,y0,x1,y1,budget,scope,min_fit,status,q})  nodes + cluster bubbles for a viewport
 *   edges({ids,mode})                                       lines for the selected person
 *   search({q,mode})                                        people by name or handle
 *
 * Model. 200,000 "hubs" (the highest ranked people, materialised) plus a crowd of
 * 9,800,000 people generated per cell of a 512 x 512 canonical grid. Canonical x is
 * the community (a seed account's audience), canonical y is distance from you. Each
 * mode is a different warp of that square, so a person keeps their identity when the
 * layout changes. Individuals are always the highest ranked people in view.
 */
(function (root) {
  'use strict';
  const HUBS = 200000, GRID = 512, CELLS = GRID * GRID, CROWD = 9800000, CROWD_MAX = 127, CLUSTERS = 48;
  const FINE = 256, ENUM_LIMIT = 20000;
  const TAU = Math.PI * 2;
  const mix = (x) => { x = Math.imul(x ^ (x >>> 16), 0x7feb352d); x = Math.imul(x ^ (x >>> 15), 0x846ca68b); return (x ^ (x >>> 16)) >>> 0; };
  const h3 = (a, b, c) => mix((mix((a + 0x9e3779b9) | 0) ^ Math.imul(b | 0, 0x85ebca6b)) + Math.imul(c | 0, 0xc2b2ae35));
  const u01 = (h) => h / 4294967296;
  const MODES = ['closeness', 'fit', 'seeds', 'status'];
  const FITS = ['strong', 'good', 'weak', 'unread'];
  const STATUSES = [null, 'interested', 'contacted', 'talking', 'client', 'no'];
  const LANE_X = [0, 0.50, 0.61, 0.72, 0.84, 0.95];
  const SEEDS = ['dtcdaily', 'ecomcollective', 'brandfounders.club', 'shopify.founders', 'skincarebusiness', 'beautyfounders',
    'cpgguild', 'packagingstudy', 'supplementbrands', 'fitfounders', 'apparelbrands', 'dutchdtc', 'foundersfeed', 'adcreativeclub',
    'petbrandclub', 'homegoodsdaily', 'coffeefounders', 'candlemakers.co', 'jewelrylabel', 'teaandtonic', 'kitchenware.co',
    'sleepbrands', 'outdoorlabel', 'baby.brands', 'wellnessfounders', 'snackfounders', 'hairbrands.club', 'denimheads',
    'sneakerlabel', 'plantshop.founders', 'greenbeauty', 'giftbrands', 'fragranceclub', 'matcha.makers', 'footwearfounders',
    'yogabrands', 'bikebrands', 'luggagelabel', 'watchhouse', 'eyewearstudio', 'drinkfounders', 'haircare.lab', 'mensgrooming',
    'lingeriebrands', 'kidswear.club', 'toybrands.co', 'stationeryshop', 'furniturelabel'];
  const FIRST = ['Maya', 'Jonas', 'Sofia', 'Liam', 'Ava', 'Noah', 'Emma', 'Lucas', 'Chloe', 'Daan', 'Isla', 'Mateo', 'Nora', 'Eli', 'Zoe',
    'Theo', 'Lena', 'Ravi', 'Priya', 'Jade', 'Marcus', 'Tessa', 'Iris', 'Bram', 'Femke', 'Amir', 'Yara', 'Owen', 'Hana', 'Kian',
    'Mila', 'Otis', 'Sanne', 'Tariq', 'Elin', 'Felix', 'Nina', 'Joris', 'Cleo', 'Ari'];
  const LAST = ['Carter', 'de Vries', 'Nguyen', 'Brooks', 'Janssen', 'Patel', 'Morales', 'Kim', 'Bakker', 'Hughes', 'Rossi', 'Walsh',
    'Visser', 'Chen', 'Ellis', 'Park', 'Reyes', 'Smit', 'Hayes', 'Ortiz', 'Meijer', 'Lund', 'Okafor', 'Haas', 'Dijkstra', 'Silva',
    'Novak', 'Ahmed', 'Berg', 'Moreau', 'Kaya', 'Dekker', 'Fisher', 'Larsen', 'Ito', 'Mendes', 'Rivera', 'Vos', 'Quinn', 'Sato'];
  const BRAND = ['luma', 'north', 'oat', 'kind', 'hale', 'fern', 'dune', 'mora', 'vela', 'solo', 'ember', 'tide', 'noon', 'pax', 'ruby',
    'loft', 'saga', 'wren', 'kiln', 'arlo', 'moss', 'pine', 'salt', 'dawn', 'reed', 'clay', 'birch', 'opal', 'sage', 'thistle'];
  const TAIL = ['skin', 'goods', 'studio', 'co', 'supply', 'labs', 'home', 'foods', 'wear', 'club'];

  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  function identity(id) {
    const h = mix(Math.imul(id, 2654435761) ^ 0x51ed270b), s = h & 3, a = (h >>> 2) % FIRST.length, b = (h >>> 9) % LAST.length;
    const n = (h >>> 16) % 97, br = BRAND[(h >>> 4) % BRAND.length], tl = TAIL[(h >>> 12) % TAIL.length];
    const first = FIRST[a], last = LAST[b], l = last.toLowerCase().replace(/[^a-z]/g, '');
    if (s === 0) return { handle: `${first.toLowerCase()}.${l}`, name: `${first} ${last}` };
    if (s === 1) return { handle: `${first.toLowerCase()}${l}${n}`, name: `${first} ${last}` };
    if (s === 2) return { handle: `${br}${tl}`, name: `${cap(br)} ${cap(tl)}` };
    return { handle: `${br}_${first.toLowerCase()}`, name: `${first}, ${cap(br)}` };
  }

  function createWorld() {
    const C = CLUSTERS;
    // Communities: sector widths on the canonical x axis, a quality score and an island spot each.
    const wRaw = SEEDS.map((_, c) => 0.35 + 3 * Math.pow(u01(h3(c, 11, 3)), 2.5));
    const wSum = wRaw.reduce((a, b) => a + b, 0), wMax = Math.max(...wRaw);
    const CUM = new Float64Array(C + 1);
    for (let c = 0; c < C; c++) CUM[c + 1] = CUM[c] + wRaw[c] / wSum;
    CUM[C] = 1;
    const qual = Float32Array.from({ length: C }, (_, c) => 0.25 + 0.75 * u01(h3(c, 13, 5)));
    const clusterOf = (u) => { let lo = 0, hi = C - 1; while (lo < hi) { const m = (lo + hi + 1) >> 1; if (CUM[m] <= u) lo = m; else hi = m - 1; } return lo; };
    const island = [], order = [...Array(C).keys()].sort((a, b) => wRaw[b] - wRaw[a]);
    order.forEach((c, i) => {
      const r = 0.066 * Math.sqrt(i + 0.5), a = i * 2.399963229728653;
      island[c] = { x: 0.5 + r * Math.cos(a), y: 0.5 + r * Math.sin(a), s: 0.010 + 0.040 * Math.sqrt(wRaw[c] / wMax) };
    });

    // Cell counts of the crowd. Denser far from you, exactly CROWD in total.
    const cellN = new Uint8Array(CELLS);
    {
      const raw = new Float64Array(CELLS); let sum = 0;
      for (let cy = 0; cy < GRID; cy++) {
        const v = (cy + 0.5) / GRID, d = 0.25 + 1.2 * v * v;
        for (let cx = 0; cx < GRID; cx++) { const i = cy * GRID + cx; raw[i] = d * (0.55 + 0.9 * u01(h3(i, 1, 7))); sum += raw[i]; }
      }
      let total = 0; const k = CROWD / sum;
      for (let i = 0; i < CELLS; i++) { cellN[i] = Math.min(CROWD_MAX, Math.round(raw[i] * k)); total += cellN[i]; }
      for (let i = 0, step = 1; total !== CROWD && step < 8000000; i = (i + 7919) % CELLS, step++) {
        if (total < CROWD && cellN[i] < CROWD_MAX) { cellN[i]++; total++; } else if (total > CROWD && cellN[i] > 1) { cellN[i]--; total--; }
      }
    }
    // Fit potential per cell, normalised to 0..1. Crowd people take their cell's fit.
    const fpCell = new Float32Array(CELLS);
    {
      let lo = 9, hi = -9;
      for (let i = 0; i < CELLS; i++) {
        const cx = i % GRID, cy = (i / GRID) | 0, v = (cy + 0.5) / GRID, c = clusterOf((cx + 0.5) / GRID);
        const f = 0.55 * qual[c] + 0.45 * Math.pow(1 - v, 0.8) + 0.16 * (u01(h3(i, 2, 9)) - 0.5);
        fpCell[i] = f; if (f < lo) lo = f; if (f > hi) hi = f;
      }
      for (let i = 0; i < CELLS; i++) fpCell[i] = (fpCell[i] - lo) / (hi - lo);
    }
    const fitClass = (fp) => fp >= 0.86 ? 0 : fp >= 0.66 ? 1 : fp >= 0.40 ? 2 : 3;
    const cellClass = new Uint8Array(CELLS);
    for (let i = 0; i < CELLS; i++) cellClass[i] = fitClass(fpCell[i]);

    // Hubs: the top of the ranking. Index order is rank order.
    const hu = new Float32Array(HUBS), hv = new Float32Array(HUBS), hfp = new Float32Array(HUBS);
    const hst = new Uint8Array(HUBS), hfollow = new Uint8Array(HUBS), hlead = new Uint8Array(HUBS), hcl = new Uint8Array(HUBS);
    const clusterHubs = Array.from({ length: C }, () => []);
    let leadCount = 0;
    for (let h = 0; h < HUBS; h++) {
      hu[h] = u01(h3(h, 21, 1)); hv[h] = Math.pow(u01(h3(h, 22, 1)), 0.75);
      const r = u01(h3(h, 23, 1)), x = u01(h3(h, 24, 1));
      hfp[h] = r < 0.12 ? 0.9 + 0.1 * x : r < 0.5 ? 0.72 + 0.16 * x : r < 0.85 ? 0.42 + 0.28 * x : 0.05 + 0.32 * x;
      hcl[h] = clusterOf(hu[h]);
      if (h < 60000 && u01(h3(h, 25, 1)) < 0.07) {
        const s = u01(h3(h, 26, 1)); hst[h] = s < 0.40 ? 1 : s < 0.65 ? 2 : s < 0.80 ? 3 : s < 0.88 ? 4 : 5;
      }
      hlead[h] = hst[h] || h < 6000 ? 1 : 0; leadCount += hlead[h];
      hfollow[h] = u01(h3(h, 27, 1)) < 0.025 + 0.22 * Math.pow(1 - hv[h], 3) ? 1 : 0;
      if (clusterHubs[hcl[h]].length < 4) clusterHubs[hcl[h]].push(h);
    }
    const hubRank = (h) => 1 - 0.02 * (h + 0.5) / HUBS;

    // Layouts. All return positions in the unit square through P.
    const P = [0, 0];
    function place(mode, u, v, fp, st, ja, jb) {
      const c = clusterOf(u), lo = CUM[c], span = CUM[c + 1] - lo, t = (u - lo) / span;
      if (mode === 'closeness') {
        const th = TAU * (lo + (0.05 + 0.9 * t) * span), rho = 0.035 + 0.44 * Math.pow(v, 0.8);
        P[0] = 0.5 + rho * Math.cos(th); P[1] = 0.5 + rho * Math.sin(th); return P;
      }
      if (mode === 'seeds') {
        const i = island[c], rr = i.s * Math.pow(v, 0.8), a = TAU * t;
        P[0] = i.x + rr * Math.cos(a); P[1] = i.y + rr * Math.sin(a); return P;
      }
      if (mode === 'fit') { P[0] = 0.07 + 0.86 * Math.pow(fp, 1.4); P[1] = 0.05 + 0.9 * u; return P; }
      // status
      if (!st) {
        const th = TAU * (lo + (0.05 + 0.9 * t) * span), rho = 0.035 + 0.44 * Math.pow(v, 0.8);
        P[0] = 0.03 + 0.40 * ((0.5 + rho * Math.cos(th)) - 0.02) / 0.96; P[1] = 0.08 + 0.84 * ((0.5 + rho * Math.sin(th)) - 0.02) / 0.96; return P;
      }
      P[0] = LANE_X[st] + (ja - 0.5) * 0.07; P[1] = 0.1 + 0.8 * (1 - fp) + (jb - 0.5) * 0.06; return P;
    }

    // What the layout means, drawn behind the people: rings, axis, islands or lanes.
    function guidesFor(mode) {
      if (mode === 'closeness') return [[0.75, 'Very close'], [0.5, 'Close'], [0.25, 'A few steps away'], [0, 'Far']].map(([c, label]) => ({ type: 'ring', x: 0.5, y: 0.5, r: +(0.035 + 0.44 * Math.pow(1 - c, 0.8)).toFixed(4), label }));
      if (mode === 'fit') return [{ type: 'axis', x0: 0.07, x1: 0.93, y: 1, from: 'Weaker fit', to: 'Stronger fit' }];
      if (mode === 'seeds') return island.map((i, c) => ({ type: 'island', x: +i.x.toFixed(4), y: +i.y.toFixed(4), r: +(i.s * 1.04).toFixed(4), label: '@' + SEEDS[c], weight: +(wRaw[c] / wMax).toFixed(3) }));
      return [[0.23, 'No status'], [0.50, 'Interested'], [0.61, 'Contacted'], [0.72, 'Talking'], [0.84, 'Client'], [0.95, 'Not a fit']].map(([x, label]) => ({ type: 'lane', x, y: 0.04, label }));
    }

    const modeCache = new Map();
    function modeIndex(mode) {
      let m = modeCache.get(mode); if (m) return m;
      const hx = new Float32Array(HUBS), hy = new Float32Array(HUBS);
      for (let h = 0; h < HUBS; h++) {
        const p = place(mode, hu[h], hv[h], hfp[h], hst[h], u01(h3(h, 31, 2)), u01(h3(h, 32, 2)));
        hx[h] = p[0]; hy[h] = p[1];
      }
      const cellX = new Float32Array(CELLS), cellY = new Float32Array(CELLS), bucket = new Int32Array(CELLS);
      const dens = [0, 1, 2, 3].map(() => new Float64Array(FINE * FINE));
      const vote = new Int32Array(FINE * FINE * 2), start = new Int32Array(FINE * FINE + 1);
      for (let i = 0; i < CELLS; i++) {
        const cx = i % GRID, cy = (i / GRID) | 0, u = (cx + 0.5) / GRID, v = (cy + 0.5) / GRID;
        const p = place(mode, u, v, fpCell[i], 0, 0.5, 0.5);
        cellX[i] = p[0]; cellY[i] = p[1];
        const gx = Math.max(0, Math.min(FINE - 1, Math.floor(p[0] * FINE))), gy = Math.max(0, Math.min(FINE - 1, Math.floor(p[1] * FINE))), b = gy * FINE + gx;
        bucket[i] = b; start[b + 1]++; dens[cellClass[i]][b] += cellN[i];
        // Boyer-Moore vote for the community that owns this fine cell.
        const c = clusterOf(u), vi = b * 2;
        if (vote[vi + 1] === 0) { vote[vi] = c; vote[vi + 1] = cellN[i]; } else if (vote[vi] === c) vote[vi + 1] += cellN[i]; else vote[vi + 1] -= cellN[i];
        if (vote[vi + 1] < 0) { vote[vi] = c; vote[vi + 1] = -vote[vi + 1]; }
      }
      for (let b = 0; b < FINE * FINE; b++) start[b + 1] += start[b];
      const fill = start.slice(0, FINE * FINE), idx = new Int32Array(CELLS);
      for (let i = 0; i < CELLS; i++) idx[fill[bucket[i]]++] = i;
      // Summed-area tables per fit class, so "how many in this rectangle" is four lookups.
      const sat = dens.map((d) => {
        const s = new Float64Array((FINE + 1) * (FINE + 1));
        for (let y = 0; y < FINE; y++) { let row = 0; for (let x = 0; x < FINE; x++) { row += d[y * FINE + x]; s[(y + 1) * (FINE + 1) + x + 1] = s[y * (FINE + 1) + x + 1] + row; } }
        return s;
      });
      m = { hx, hy, cellX, cellY, start, idx, dens, sat, vote };
      modeCache.set(mode, m); return m;
    }
    const satSum = (s, x0, y0, x1, y1) => {
      const W = FINE + 1, a = Math.max(0, Math.min(FINE, x0)), b = Math.max(0, Math.min(FINE, y0)), c = Math.max(0, Math.min(FINE, x1)), d = Math.max(0, Math.min(FINE, y1));
      if (c <= a || d <= b) return 0;
      return s[d * W + c] - s[b * W + c] - s[d * W + a] + s[b * W + a];
    };

    let hubText = null;
    const hubNames = () => hubText || (hubText = Array.from({ length: HUBS }, (_, h) => { const p = identity(h + 1); return (p.handle + ' ' + p.name).toLowerCase(); }));

    const node = (id, x, y, rank, fit, status, cluster, closeness, lead, follows) => {
      const p = identity(id);
      const n = { id, handle: p.handle, name: p.name, x: +x.toFixed(5), y: +y.toFixed(5), rank: +rank.toFixed(5), fit, status, cluster, closeness: +closeness.toFixed(3), lead: !!lead };
      if (follows) n.follows_me = true;
      return n;
    };
    const hubNode = (h, mode, m) => {
      const x = m ? m.hx[h] : 0, y = m ? m.hy[h] : 0;
      return node(h + 1, x, y, hubRank(h), FITS[fitClass(hfp[h])], STATUSES[hst[h]], hcl[h], 1 - hv[h], hlead[h], hfollow[h]);
    };
    function crowdPerson(cell, j, mode) {
      const cx = cell % GRID, cy = (cell / GRID) | 0, u = (cx + u01(h3(cell, j, 1))) / GRID, v = (cy + u01(h3(cell, j, 2))) / GRID, fp = fpCell[cell];
      const p = place(mode, u, v, fp, 0, 0.5, 0.5);
      const closeness = 1 - v, rank = 0.98 * (0.35 * u01(h3(cell, j, 3)) + 0.4 * fp + 0.25 * closeness);
      return { id: HUBS + cell * 128 + j + 1, x: p[0], y: p[1], rank, fit: FITS[cellClass[cell]], cluster: clusterOf(u), closeness, fp, cell };
    }
    const crowdNode = (c) => node(c.id, c.x, c.y, c.rank, c.fit, null, c.cluster, c.closeness, false, false);
    function nodeById(id, mode) {
      const m = modeIndex(mode);
      if (id >= 1 && id <= HUBS) return hubNode(id - 1, mode, m);
      const k = id - HUBS - 1, cell = Math.floor(k / 128), j = k % 128;
      if (k < 0 || cell >= CELLS || j >= cellN[cell]) return null;
      return crowdNode(crowdPerson(cell, j, mode));
    }

    const minClass = (f) => f === 'strong' ? 0 : f === 'good' ? 1 : f === 'weak' ? 2 : 3;
    const statusCode = (s) => s ? Math.max(0, STATUSES.indexOf(s)) : 0;

    function view(a) {
      const mode = MODES.includes(a.mode) ? a.mode : 'closeness', m = modeIndex(mode);
      const B = Math.max(20, Math.min(1500, Math.round(+a.budget || 600)));
      let x0 = Number.isFinite(+a.x0) ? +a.x0 : 0, y0 = Number.isFinite(+a.y0) ? +a.y0 : 0, x1 = Number.isFinite(+a.x1) ? +a.x1 : 1, y1 = Number.isFinite(+a.y1) ? +a.y1 : 1;
      if (x1 < x0) [x0, x1] = [x1, x0]; if (y1 < y0) [y0, y1] = [y1, y0];
      x1 = Math.max(x1, x0 + 1e-6); y1 = Math.max(y1, y0 + 1e-6);
      const leadsOnly = a.scope !== 'all', maxClass = a.min_fit ? minClass(a.min_fit) : 3;
      const stFilter = a.status && a.status !== 'all' ? statusCode(a.status) : -1;
      const q = (a.q || '').trim().toLowerCase().replace(/^@/, '');
      const crowdOn = !leadsOnly && stFilter <= 0 && !q;
      const classOk = [0, 1, 2, 3].map((c) => c <= maxClass);

      // Total matching people in the whole world.
      let total = 0;
      const names = q ? hubNames() : null;
      const hubPass = (h) => (!leadsOnly || hlead[h]) && fitClass(hfp[h]) <= maxClass && (stFilter < 0 || (stFilter === 0 ? hst[h] === 0 : hst[h] === stFilter)) && (!q || names[h].includes(q));
      for (let h = 0; h < HUBS; h++) if (hubPass(h)) total++;
      if (crowdOn) for (let c = 0; c < 4; c++) if (classOk[c]) total += satSum(m.sat[c], 0, 0, FINE, FINE);

      // Bin grid: powers of two so bubble ids stay stable between nearby requests.
      const size = Math.pow(2, Math.floor(Math.log2(Math.max(x1 - x0, y1 - y0) / 12)));
      const bx0 = Math.floor(x0 / size), by0 = Math.floor(y0 / size), nbx = Math.ceil(x1 / size) - bx0 + 1, nby = Math.ceil(y1 / size) - by0 + 1;
      const nb = nbx * nby, binN = new Float64Array(nb), binX = new Float64Array(nb), binY = new Float64Array(nb), binC = new Int32Array(nb * C);
      const addBin = (x, y, c, w) => {
        const gx = Math.floor(x / size) - bx0, gy = Math.floor(y / size) - by0;
        if (gx < 0 || gy < 0 || gx >= nbx || gy >= nby) return;
        const b = gy * nbx + gx; binN[b] += w; binX[b] += x * w; binY[b] += y * w; binC[b * C + c] += w;
      };

      // Hubs in view, in rank order.
      const hits = [], hx = m.hx, hy = m.hy;
      for (let h = 0; h < HUBS; h++) {
        const x = hx[h];
        if (x < x0 || x > x1) continue;
        const y = hy[h];
        if (y < y0 || y > y1 || !hubPass(h)) continue;
        hits.push(h);
      }
      let inView = hits.length;
      // Crowd in view: enumerate individuals when the estimate is small, else use the density grid.
      let crowd = null, crowdEst = 0;
      const gx0 = Math.floor((x0 - 0.012) * FINE), gy0 = Math.floor((y0 - 0.012) * FINE), gx1 = Math.ceil((x1 + 0.012) * FINE), gy1 = Math.ceil((y1 + 0.012) * FINE);
      if (crowdOn) {
        for (let c = 0; c < 4; c++) if (classOk[c]) crowdEst += satSum(m.sat[c], gx0, gy0, gx1, gy1);
        if (crowdEst <= ENUM_LIMIT) {
          crowd = [];
          for (let gy = Math.max(0, gy0); gy < Math.min(FINE, gy1); gy++) for (let gx = Math.max(0, gx0); gx < Math.min(FINE, gx1); gx++) {
            const b = gy * FINE + gx;
            for (let k = m.start[b]; k < m.start[b + 1]; k++) {
              const cell = m.idx[k];
              if (!classOk[cellClass[cell]]) continue;
              for (let j = 0; j < cellN[cell]; j++) {
                const p = crowdPerson(cell, j, mode);
                if (p.x >= x0 && p.x <= x1 && p.y >= y0 && p.y <= y1) crowd.push(p);
              }
            }
          }
          crowd.sort((p, q2) => q2.rank - p.rank);
          inView += crowd.length;
        }
      }
      // Choose individuals: highest rank first, hubs before crowd.
      const nodes = [];
      let ih = 0, ic = 0;
      while (nodes.length < B && (ih < hits.length || (crowd && ic < crowd.length))) {
        if (ih < hits.length) { nodes.push(hubNode(hits[ih++], mode, m)); continue; }
        nodes.push(crowdNode(crowd[ic++]));
      }
      for (; ih < hits.length; ih++) addBin(hx[hits[ih]], hy[hits[ih]], hcl[hits[ih]], 1);
      if (crowd) for (; ic < crowd.length; ic++) addBin(crowd[ic].x, crowd[ic].y, crowd[ic].cluster, 1);
      else if (crowdOn) {
        for (let gy = Math.max(0, gy0); gy < Math.min(FINE, gy1); gy++) for (let gx = Math.max(0, gx0); gx < Math.min(FINE, gx1); gx++) {
          const b = gy * FINE + gx; let n = 0;
          for (let c = 0; c < 4; c++) if (classOk[c]) n += m.dens[c][b];
          if (!n) continue;
          const x = (gx + 0.5) / FINE, y = (gy + 0.5) / FINE;
          if (x < x0 || x > x1 || y < y0 || y > y1) continue;
          addBin(x, y, m.vote[b * 2], n); inView += n;
        }
      }
      const clusters = [];
      for (let b = 0; b < nb; b++) {
        if (binN[b] < 1) continue;
        let best = 0; for (let c = 1; c < C; c++) if (binC[b * C + c] > binC[b * C + best]) best = c;
        const gx = b % nbx + bx0, gy = Math.floor(b / nbx) + by0;
        clusters.push({ id: `${size.toExponential(0)}:${gx}:${gy}`, x: +(binX[b] / binN[b]).toFixed(5), y: +(binY[b] / binN[b]).toFixed(5), count: Math.round(binN[b]), label: '@' + SEEDS[best], top_ids: [] });
      }
      clusters.sort((p, q2) => q2.count - p.count);
      const shown = nodes.length;
      return {
        rev: 'world-1', mode, world: { x0: 0, y0: 0, x1: 1, y1: 1, me: mode === 'closeness' ? { x: 0.5, y: 0.5 } : null, guides: guidesFor(mode) },
        viewport: { x0, y0, x1, y1 }, nodes, clusters: clusters.slice(0, 260), total, shown, hidden: Math.max(0, Math.round(inView) - shown),
        seeds: SEEDS.map((s, i) => ({ id: i, handle: s }))
      };
    }

    function edges(a) {
      const mode = MODES.includes(a.mode) ? a.mode : 'closeness', m = modeIndex(mode);
      const ids = String(a.ids || '').split(',').map(Number).filter((n) => Number.isInteger(n) && n > 0).slice(0, 5);
      const out = [], seen = new Map();
      const add = (n) => { if (n && !seen.has(n.id)) seen.set(n.id, n); };
      for (const id of ids) {
        const me = nodeById(id, mode); if (!me) continue;
        add(me);
        const c = me.cluster, list = clusterHubs[c] || [];
        const own = list.filter((h) => h + 1 !== id).slice(0, 2 + (id % 3));
        for (const h of own) { add(hubNode(h, mode, m)); out.push({ source: id, target: h + 1, kind: 'follows', seed: SEEDS[c] }); }
        for (let k = 0; k < 2; k++) {
          const other = Math.floor(u01(h3(id, 41 + k, 3)) * 3000); if (other + 1 === id) continue;
          add(hubNode(other, mode, m)); out.push({ source: id, target: other + 1, kind: 'mutual' });
        }
        if (mode === 'closeness' && me.follows_me) out.push({ source: id, target: 0, kind: 'follows_you' });
      }
      return { rev: 'world-1', mode, nodes: [...seen.values()], edges: out };
    }

    function search(a) {
      const mode = MODES.includes(a.mode) ? a.mode : 'closeness', m = modeIndex(mode);
      const q = (a.q || '').trim().toLowerCase().replace(/^@/, '');
      if (!q) return { results: [] };
      const names = hubNames(), found = [];
      for (let h = 0; h < HUBS && found.length < 60; h++) {
        const t = names[h], handle = t.split(' ')[0];
        const at = handle === q ? 0 : handle.startsWith(q) ? 1 : t.includes(q) ? 2 : -1;
        if (at >= 0) found.push([at, h]);
      }
      found.sort((p, r) => p[0] - r[0] || p[1] - r[1]);
      return { results: found.slice(0, 8).map(([, h]) => hubNode(h, mode, m)) };
    }
    return { view, edges, search, nodeById, meta: { hubs: HUBS, crowd: CROWD, total: HUBS + CROWD, clusters: C, leads: leadCount, seeds: SEEDS, modes: MODES } };
  }

  const api = { createWorld, MODES, FITS, STATUSES, SEEDS, HUBS, CROWD };
  root.MapWorld = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  // Worker entry: answer { id, kind, args } with { id, result | error }.
  if (typeof importScripts === 'function' && typeof root.postMessage === 'function') {
    let world;
    root.onmessage = (e) => {
      const { id, kind, args } = e.data || {};
      try { world = world || createWorld(); root.postMessage({ id, result: world[kind](args || {}) }); }
      catch (err) { root.postMessage({ id, error: String(err && err.message || err) }); }
    };
  }
})(typeof window === 'undefined' ? (typeof self === 'undefined' ? globalThis : self) : window);
