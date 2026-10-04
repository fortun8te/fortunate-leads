// A small Chrome DevTools Protocol client over Node's built-in WebSocket. No dependencies.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export function findChromium() {
  if (process.env.FL_CHROMIUM) return process.env.FL_CHROMIUM;
  const cache = path.join(os.homedir(), 'Library', 'Caches', 'ms-playwright');
  try {
    const shells = fs.readdirSync(cache).filter((d) => d.startsWith('chromium_headless_shell-')).sort().reverse();
    for (const dir of shells) {
      const root = path.join(cache, dir);
      for (const sub of fs.readdirSync(root)) {
        const bin = path.join(root, sub, 'chrome-headless-shell');
        if (fs.existsSync(bin)) return bin;
      }
    }
  } catch { /* no cache */ }
  return null;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class Browser {
  // Launch an isolated headless browser with a throwaway profile, or attach to `cdpUrl`.
  static async open({ port = 9147, cdpUrl = null, chromium = findChromium() } = {}) {
    const b = new Browser();
    if (cdpUrl) b.base = cdpUrl.replace(/\/$/, '');
    else {
      if (!chromium) throw new Error('No headless Chromium found. Set FL_CHROMIUM to a chrome-headless-shell binary, or pass --cdp-url.');
      b.profile = fs.mkdtempSync(path.join(os.tmpdir(), 'fl-visual-'));
      b.proc = spawn(chromium, [`--remote-debugging-port=${port}`, '--remote-debugging-address=127.0.0.1', `--user-data-dir=${b.profile}`,
        '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--disable-gpu', '--hide-scrollbars', '--force-color-profile=srgb',
        '--font-render-hinting=none', '--disable-lcd-text', 'about:blank'], { stdio: 'ignore' });
      b.base = `http://127.0.0.1:${port}`;
    }
    for (let i = 0; i < 100; i++) {
      try { const r = await fetch(b.base + '/json/list'); if (r.ok) { b.targets = await r.json(); break; } } catch { /* starting */ }
      await sleep(100);
    }
    if (!b.targets) { b.close(); throw new Error('Could not reach the browser debugging port at ' + b.base); }
    const page = b.targets.find((t) => t.type === 'page');
    if (!page) { b.close(); throw new Error('The browser has no page to drive.'); }
    await b.connect(page.webSocketDebuggerUrl);
    return b;
  }

  connect(url) {
    this.ws = new WebSocket(url);
    this.id = 0; this.pending = new Map();
    this.ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.id && this.pending.has(m.id)) { const { ok, no } = this.pending.get(m.id); this.pending.delete(m.id); m.error ? no(new Error(m.error.message)) : ok(m.result); }
    };
    return new Promise((ok, no) => { this.ws.onopen = ok; this.ws.onerror = () => no(new Error('DevTools connection failed')); });
  }

  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((ok, no) => this.pending.set(id, { ok, no }));
  }

  async eval(expression) {
    const r = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error('Page error: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    return r.result.value;
  }

  async viewport(width, height) {
    await this.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
  }

  async goto(url) {
    await this.send('Page.navigate', { url });
    for (let i = 0; i < 200; i++) { try { if (await this.eval('document.readyState') === 'complete') return; } catch { /* navigating */ } await sleep(50); }
    throw new Error('Page did not finish loading: ' + url);
  }

  // PNG as a data URL (optionally one region of the page).
  async shot(clip) {
    const r = await this.send('Page.captureScreenshot', { format: 'png', ...(clip ? { clip: { ...clip, scale: 1 } } : {}) });
    return 'data:image/png;base64,' + r.data;
  }

  close() {
    try { this.ws?.close(); } catch { /* closed */ }
    try { this.proc?.kill(); } catch { /* gone */ }
    if (this.profile) try { fs.rmSync(this.profile, { recursive: true, force: true }); } catch { /* best effort */ }
  }
}
