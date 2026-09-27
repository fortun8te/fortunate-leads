import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const html = readFileSync(new URL('../../web/index.html', import.meta.url), 'utf8');
const css = readFileSync(new URL('../../web/app.css', import.meta.url), 'utf8');
const tags = [...html.matchAll(/<(input|select|textarea)\b([^>]*)>/g)];
const attr = (text, key) => text.match(new RegExp(`\\b${key}="([^"]*)"`))?.[1];

// These are source checks. They do not replace keyboard or screen-reader QA.
test('every static form field has a persistent accessible name', () => {
  for (const match of tags) {
    const id = attr(match[2], 'id');
    const before = html.slice(0, match.index);
    const wrappingLabel = before.lastIndexOf('<label') > before.lastIndexOf('</label>');
    const explicitLabel = id && html.includes(`for="${id}"`);
    assert.ok(attr(match[2], 'aria-label') || attr(match[2], 'aria-labelledby') || wrappingLabel || explicitLabel,
      `${id || match[1]} relies on a placeholder instead of a label`);
  }
});

test('document IDs are unique and dialog has a real title and close control', () => {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
  assert.equal(new Set(ids).size, ids.length);
  const dialog = html.match(/<div\b[^>]*\bid="help"[^>]*>/)[0];
  assert.equal(attr(dialog, 'role'), 'dialog');
  assert.equal(attr(dialog, 'aria-modal'), 'true');
  assert.ok(ids.includes(attr(dialog, 'aria-labelledby')));
  assert.match(html, /<button[^>]*id="help-close"[^>]*aria-label="Close keyboard shortcuts"/);
});

const tokens = block => Object.fromEntries([...block.matchAll(/--([\w-]+):\s*(#[\da-f]{6})\b/gi)].map(m => [m[1], m[2]]));
const dark = tokens(css.match(/:root\s*\{([\s\S]*?)\}/)[1]);
const light = {...dark, ...tokens(css.match(/:root\[data-theme="light"\]\s*\{([\s\S]*?)\}/)[1])};
function luminance(hex) {
  const rgb = hex.slice(1).match(/../g).map(x => parseInt(x, 16) / 255)
    .map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
  return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
}
const contrast = (a, b) => {
  const pair = [luminance(a), luminance(b)].sort((x,y) => y-x);
  return (pair[0] + .05) / (pair[1] + .05);
};
for (const [theme, t] of Object.entries({dark, light})) {
  test(`${theme} text colours meet normal-text contrast across neutral panels`, () => {
    for (const fg of ['fg', 'fg2', 'fg3', 'fg4', 'ok', 'warn', 'bad']) {
      for (const bg of ['bg', 'bg1', 'bg2', 'bg3']) {
        assert.ok(contrast(t[fg], t[bg]) >= 4.5, `${fg} on ${bg} is below 4.5:1`);
      }
    }
    assert.ok(contrast(t.fg, t.inv) >= 4.5, 'solid action text contrast');
  });
  test(`${theme} field outlines and focus remain visible`, () => {
    for (const bg of ['bg', 'bg1', 'bg2']) {
      assert.ok(contrast(t['control-line'], t[bg]) >= 3, `field outline on ${bg}`);
      assert.ok(contrast(t.focus, t[bg]) >= 3, `focus outline on ${bg}`);
    }
  });
}

test('map explains its display limit and provides a textual alternative', () => {
  assert.doesNotMatch(html, /id="map-q"/);
  assert.match(html, /id="map-density"[^>]*aria-label="People shown on map"/);
  assert.match(html, /<option value="3000">3,000 people/);
  assert.match(html, /<canvas[^>]*aria-label="[^"]*Open Leads[^\"]*same filters/);
  assert.match(html, /class="lg-ln dotted"[^>]*><\/i>Earlier: absent/);
});
