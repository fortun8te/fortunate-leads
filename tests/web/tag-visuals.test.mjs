import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('../../web/app.css', import.meta.url), 'utf8');
const visual = css.slice(css.indexOf('/* Tag hierarchy:'));

function colors(selector, source = visual) {
  const start = source.indexOf(selector);
  assert.ok(start >= 0, `missing ${selector}`);
  const body = source.slice(source.indexOf('{', start) + 1, source.indexOf('}', start));
  return Object.fromEntries([...body.matchAll(/(--[\w-]+):\s*(#[\da-f]{6})/gi)].map(([, key, value]) => [key, value]));
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
  for (const [name, palette] of [
    ['dark', colors(':root {')],
    ['light', colors(':root[data-theme="light"] {')],
  ]) {
    for (const [text, surface] of [
      ['--t-strong-ink', '--t-strong'], ['--t-possible-ink', '--t-possible'],
      ['--t-flag-ink', '--t-flag'], ['--t-review-ink', '--t-review'],
      ['--t-decision-ink', '--t-decision'], ['--t-role-ink', '--t-role'],
      ['--t-partner-ink', '--t-partner'], ['--t-evidence-ink', '--t-plus'],
      ['--t-client-ink', '--t-client'], ['--t-market-ink', '--t-market'],
    ]) assert.ok(contrast(palette[text], palette[surface]) >= 4.5, `${name} ${text} contrast`);
  }
});

test('niche and context stay neutral, while semantic tags retain their hierarchy', () => {
  assert.match(visual, /\.tag, \.tchip \{ border-radius: 999px; \}/);
  for (const tier of ['hero', 'flag', 'decision', 'role', 'partner', 'plus', 'client', 'market', 'review']) assert.match(visual, new RegExp(`\\.tag\\.t-${tier}, \\.tchip\\.t-${tier} \\{ background: var\\(--t-`));
  assert.match(visual, /\.tag\.t-niche, \.tchip\.t-niche \{ background: var\(--bg2\); color: var\(--fg2\);/);
  assert.doesNotMatch(visual, /\.tag\.t-niche\.h-|\.tchip\.t-niche\.h-|--t-niche-/);
  assert.match(visual, /\.tag\.t-ctx, \.tchip\.t-ctx \{ background: transparent;/);
  assert.match(visual, /\.fi\.t-hero\.inc, \.fi\.t-maybe\.inc/);
  assert.match(visual, /\.fi\.t-flag\.inc, \.fi\.t-review\.inc/);
  for (const [name, palette] of [
    ['dark', colors(':root {', css)],
    ['light', colors(':root[data-theme="light"] {', css)],
  ]) assert.ok(contrast(palette['--fg2'], palette['--bg2']) >= 4.5, `${name} neutral niche contrast`);
});
