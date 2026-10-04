// Text colour tokens keep WCAG AA contrast (4.5:1) on every surface they sit on, in both themes.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const css = await readFile(new URL('../../web/app.css', import.meta.url), 'utf8');
const block = (selector) => {
  const start = css.indexOf(selector + ' {');
  return css.slice(start, css.indexOf('\n}', start));
};
const tokens = (text) => Object.fromEntries([...text.matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})\b/gi)].map((m) => [m[1], m[2]]));
const dark = tokens(block(':root'));
const light = { ...dark, ...tokens(block(':root[data-theme="light"]')) };
const channel = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
const luminance = (hex) => [1, 3, 5].map((i) => channel(parseInt(hex.slice(i, i + 2), 16))).reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
const ratio = (a, b) => { const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };

for (const [name, t] of [['dark', dark], ['light', light]]) {
  test(`${name} theme: body and muted text pass AA on every surface`, () => {
    for (const fg of ['fg', 'fg2', 'fg3', 'fg4'])
      for (const bg of ['bg', 'bg1', 'bg2', 'bg3'])
        assert.ok(ratio(t[fg], t[bg]) >= 4.5, `--${fg} on --${bg} is ${ratio(t[fg], t[bg]).toFixed(2)}:1`);
  });
  test(`${name} theme: tag text passes AA on its own chip`, () => {
    for (const g of ['role', 'niche', 'signal', 'size', 'source', 'custom'])
      assert.ok(ratio(t[g + '-fg'], t[g + '-bg']) >= 4.5, `${g} chip is ${ratio(t[g + '-fg'], t[g + '-bg']).toFixed(2)}:1`);
  });
  test(`${name} theme: focus ring and control borders are visible (3:1)`, () => {
    for (const c of ['focus', 'control-line']) assert.ok(ratio(t[c], t.bg) >= 3, `--${c} on --bg is ${ratio(t[c], t.bg).toFixed(2)}:1`);
    assert.ok(ratio(t.focus, t.bg3) >= 3, '--focus on --bg3');
  });
}
