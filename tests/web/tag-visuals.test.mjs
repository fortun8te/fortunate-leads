import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('../../web/app.css', import.meta.url), 'utf8');
const visual = css.slice(css.indexOf('/* Tag hierarchy:'));

function colors(selector) {
  const start = visual.indexOf(selector);
  assert.ok(start >= 0, `missing ${selector}`);
  const body = visual.slice(visual.indexOf('{', start) + 1, visual.indexOf('}', start));
  return Object.fromEntries([...body.matchAll(/(--t-[\w-]+):\s*(#[\da-f]{6})/gi)].map(([, key, value]) => [key, value]));
}
function luminance(hex) {
  const channels = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const linear = channels.map((v) => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
}
function contrast(a, b) {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (light + 0.05) / (dark + 0.05);
}

test('emphasis and caution pills keep readable text in both themes', () => {
  for (const [name, palette, page] of [
    ['dark', colors(':root {'), '#0d0d0d'],
    ['light', colors(':root[data-theme="light"] {'), '#ffffff'],
  ]) {
    for (const [text, surface] of [
      ['--t-strong-ink', '--t-strong'], ['--t-possible-ink', '--t-possible'],
      ['--t-flag-ink', '--t-flag'], ['--t-review-ink', '--t-flag-surface'],
    ]) assert.ok(contrast(palette[text], palette[surface]) >= 4.5, `${name} ${text} contrast`);
    assert.ok(contrast(palette['--t-evidence-ink'], page) >= 4.5, `${name} evidence contrast`);
  }
});

test('filled judgments, outlined clues, and neutral context share the pill shape', () => {
  assert.match(visual, /\.tag, \.tchip \{ border-radius: 999px; \}/);
  for (const tier of ['hero', 'flag']) assert.match(visual, new RegExp(`\\.tag\\.t-${tier}, \\.tchip\\.t-${tier} \\{ background: var\\(--t-`));
  for (const tier of ['plus', 'review']) assert.match(visual, new RegExp(`\\.tag\\.t-${tier}, \\.tchip\\.t-${tier} \\{ background: transparent;`));
  assert.match(visual, /\.fi\.t-hero\.inc, \.fi\.t-maybe\.inc/);
  assert.match(visual, /\.fi\.t-flag\.inc, \.fi\.t-review\.inc/);
});
