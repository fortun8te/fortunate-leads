#!/usr/bin/env node
// Screenshot regression for the Leads page, the other pages and every map mode.
//
//   node tests/visual/run.mjs --db /path/to/cloned/leads.sqlite --update     # record baselines on a known-good branch
//   node tests/visual/run.mjs --db /path/to/cloned/leads.sqlite              # compare the current branch to them
//   node tests/visual/run.mjs --base-url http://127.0.0.1:9140               # use a server you already started
//
// Options: --widths 1280,1440  --only leads,map  --tolerance 0.003  --channel 12  --idle-frames 6
//          --idle-tolerance 0.0001  --baselines DIR  --out DIR  --port 9148 (server)  --cdp-port 9147  --cdp-url http://127.0.0.1:PORT
//
// Use a clone of the real-data snapshot (cp -c), never the live database. Baselines hold real profile
// photos and names, so they stay out of git (tests/visual/.gitignore). The browser is a headless Chromium
// with a throwaway profile; the page is only ever a local test server.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Browser } from './cdp.mjs';
import { PAGE_COMPARE, PAGE_IDLE } from './compare.mjs';
import { SCENARIOS, PREP, WAIT_IMAGES, MAP_MODES, SELECT_MODE, MAP_READY, CANVAS_RECT, NO_OVERFLOW, slug } from './scenarios.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '../..');
const argv = process.argv.slice(2);
const flag = (n) => argv.includes('--' + n);
const opt = (n, d) => { const i = argv.indexOf('--' + n); return i >= 0 ? argv[i + 1] : d; };
const WIDTHS = opt('widths', '1280,1440').split(',').map(Number);
const HEIGHT = (w) => Math.round(w * 0.625);
const TOL = Number(opt('tolerance', 0.003));
const CHANNEL = Number(opt('channel', 12));
const IDLE = Number(opt('idle-frames', 6));
const IDLE_TOL = Number(opt('idle-tolerance', 0.0001));   // share of pixels allowed to change while idle
const ONLY = opt('only', '') ? opt('only', '').split(',') : null;
const BASELINES = path.resolve(opt('baselines', process.env.FL_VISUAL_BASELINES || path.join(HERE, 'baselines')));
const OUT = path.resolve(opt('out', path.join(HERE, 'out')));
const UPDATE = flag('update');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fail = (m) => { console.error('visual: ' + m); process.exit(2); };

function guardUrl(u) {
  const url = new URL(u);
  if (!['127.0.0.1', 'localhost'].includes(url.hostname)) fail('only a local server can be tested');
  if (url.port === '8777') fail('port 8777 is the live app; test a clone on another port');
  return u.replace(/\/$/, '');
}

async function startServer() {
  const db = opt('db', process.env.FL_VISUAL_DB);
  if (!db) fail('give --db <clone of the snapshot> or --base-url <running server>');
  if (path.resolve(db) === path.resolve(ROOT, 'data', 'leads.sqlite')) fail('refusing to serve the live database');
  const port = Number(opt('port', 9148));
  const proc = spawn('python3', [path.join(ROOT, 'server', 'server.py'), '--db', path.resolve(db), '--port', String(port), '--no-workers'], { stdio: 'ignore' });
  const base = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 300; i++) { try { if ((await fetch(base + '/api/counts')).ok) return { base, proc }; } catch { /* starting */ } await sleep(200); }
  proc.kill(); fail('the test server did not start');
}

// Screenshot until two in a row match: photos and layout have settled.
async function settle(b, clip) {
  let prev = await b.shot(clip);
  for (let i = 0; i < 14; i++) {
    await sleep(500);
    const next = await b.shot(clip);
    const r = await b.eval(`(${PAGE_IDLE})(${JSON.stringify([prev, next])}, ${CHANNEL})`);
    if (r.worst[0] <= 0.0002) return { png: next, settled: true };
    prev = next;
  }
  return { png: prev, settled: false };
}

async function check(b, name, width, png, clip, kind, results) {
  const file = `${name}@${width}.png`;
  const res = { name, width, kind, file };
  fs.mkdirSync(OUT, { recursive: true });
  const data = (u) => Buffer.from(u.split(',')[1], 'base64');
  fs.writeFileSync(path.join(OUT, file), data(png));
  // Idle frames: nothing should change while nothing is happening.
  const frames = [png];
  for (let i = 0; i < IDLE; i++) { await sleep(120); frames.push(await b.shot(clip)); }
  const idle = await b.eval(`(${PAGE_IDLE})(${JSON.stringify(frames)}, ${CHANNEL})`);
  res.idleWorst = Math.max(0, ...idle.worst);
  res.idleStable = res.idleWorst <= IDLE_TOL;
  const base = path.join(BASELINES, file);
  if (UPDATE) { fs.mkdirSync(BASELINES, { recursive: true }); fs.writeFileSync(base, data(png)); res.status = 'recorded'; }
  else if (!fs.existsSync(base)) { res.status = 'no baseline'; }
  else {
    const old = 'data:image/png;base64,' + fs.readFileSync(base).toString('base64');
    const d = await b.eval(`(${PAGE_COMPARE})(${JSON.stringify(old)}, ${JSON.stringify(png)}, ${CHANNEL})`);
    if (d.sizeMismatch) { res.status = 'size changed'; res.detail = d.sizeMismatch.join(' -> '); }
    else {
      res.ratio = d.ratio; res.box = d.box;
      res.status = d.ratio <= TOL ? 'same' : 'changed';
      if (res.status === 'changed') fs.writeFileSync(path.join(OUT, `${name}@${width}.diff.png`), data(d.diff));
    }
  }
  results.push(res);
}

async function main() {
  let server = null, base = opt('base-url', null);
  if (base) base = guardUrl(base); else { server = await startServer(); base = server.base; }
  const b = await Browser.open({ port: Number(opt('cdp-port', 9147)), cdpUrl: opt('cdp-url', null) });
  const results = [], notes = [];
  try {
    for (const width of WIDTHS) {
      const height = HEIGHT(width);
      await b.viewport(width, height);
      for (const sc of SCENARIOS) {
        if (ONLY && !ONLY.includes(sc.name) && !ONLY.includes('pages')) continue;
        try {
          await b.goto('about:blank'); await b.goto(`${base}/${sc.hash}`);
          await sc.run(b); await b.eval(PREP); await b.eval(WAIT_IMAGES);
          const o = await b.eval(NO_OVERFLOW);
          if (o.scrollWidth > o.innerWidth) notes.push(`${sc.name}@${width}: page is ${o.scrollWidth}px wide in a ${o.innerWidth}px window`);
          const s = await settle(b);
          await check(b, sc.name, width, s.png, null, 'page', results);
          if (!s.settled) results.at(-1).unsettled = true;
        } catch (e) { results.push({ name: sc.name, width, kind: 'page', status: 'error', detail: e.message }); }
      }
      if (!ONLY || ONLY.includes('map')) {
        try {
          await b.goto('about:blank'); await b.goto(`${base}/#/map`);
          await b.eval(`new Promise((r) => setTimeout(r, 800))`);
          const modes = await b.eval(MAP_MODES);
          if (!modes.length) results.push({ name: 'map', width, kind: 'map', status: 'error', detail: 'no map modes found on the page' });
          for (const m of modes) {
            try {
              await b.eval(SELECT_MODE(m));
              if (!(await b.eval(MAP_READY))) throw new Error('map did not finish loading');
              await b.eval(PREP); await b.eval(WAIT_IMAGES);
              const clip = await b.eval(CANVAS_RECT);
              const s = await settle(b, { ...clip });
              await check(b, `map-${slug(m.id + '-' + m.label)}`, width, s.png, { ...clip }, 'map', results);
              if (!s.settled) results.at(-1).unsettled = true;
            } catch (e) { results.push({ name: `map-${slug(m.id + '-' + m.label)}`, width, kind: 'map', status: 'error', detail: e.message }); }
          }
        } catch (e) { results.push({ name: 'map', width, kind: 'map', status: 'error', detail: e.message }); }
      }
    }
  } finally { b.close(); server?.proc.kill(); }

  const bad = (r) => ['changed', 'size changed', 'no baseline', 'error'].includes(r.status) || r.idleStable === false || r.unsettled;
  console.log('\nscenario'.padEnd(46) + 'width  result        diff     idle');
  for (const r of results) console.log(`${r.name}`.padEnd(45) + ` ${String(r.width).padEnd(6)} ${String(r.status).padEnd(13)} ${r.ratio == null ? '-' : (r.ratio * 100).toFixed(2) + '%'}`.padEnd(30) + ` ${r.idleStable === undefined ? '-' : r.idleStable ? 'stable' : 'FLICKER ' + (r.idleWorst * 100).toFixed(3) + '%'}${r.unsettled ? ' (never settled)' : ''}${r.detail ? '  ' + r.detail : ''}${r.box ? `  at ${r.box.x},${r.box.y} ${r.box.w}x${r.box.h}` : ''}`);
  for (const n of notes) console.log('overflow: ' + n);
  fs.mkdirSync(OUT, { recursive: true });
  fs.writeFileSync(path.join(OUT, 'report.json'), JSON.stringify({ at: new Date().toISOString(), tolerance: TOL, channel: CHANNEL, idleFrames: IDLE, results, notes }, null, 1));
  const failed = results.filter(bad).length + notes.length;
  console.log(UPDATE ? `\nrecorded ${results.length} baselines in ${BASELINES}` : `\n${results.length - results.filter(bad).length} of ${results.length} pass${notes.length ? `, ${notes.length} overflow` : ''}. Images and report.json are in ${OUT}`);
  process.exit(UPDATE ? 0 : failed ? 1 : 0);
}
main().catch((e) => { console.error(e); process.exit(2); });
