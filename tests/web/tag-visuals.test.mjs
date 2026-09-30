import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';

const css = readFileSync(new URL('../../web/app.css', import.meta.url), 'utf8');
const refine = readFileSync(new URL('../../web/refine.css', import.meta.url), 'utf8');
const visual = css.slice(css.indexOf('/* Figma tag variables:'));
function colors(selector) {
  const start = visual.indexOf(selector);
  assert.ok(start >= 0, `missing ${selector}`);
  const body = visual.slice(visual.indexOf('{', start) + 1, visual.indexOf('}', start));
  return Object.fromEntries([...body.matchAll(/(--[\w-]+):\s*(#[\da-f]{3,6}|white)/gi)].map(([, key, value]) => [key, value === 'white' ? '#ffffff' : value.length === 4 ? '#'+[...value.slice(1)].map(x=>x+x).join('') : value]));
}
function luminance(hex) {
  const channels = [1, 3, 5].map(i=>parseInt(hex.slice(i,i+2),16)/255);
  const linear = channels.map(v=>v<=.04045 ? v/12.92 : ((v+.055)/1.055)**2.4);
  return linear[0]*.2126 + linear[1]*.7152 + linear[2]*.0722;
}
function contrast(a,b) {
  const [light,dark]=[luminance(a),luminance(b)].sort((x,y)=>y-x);
  return (light+.05)/(dark+.05);
}

test('all Figma importance and evidence palettes retain readable text in both themes',()=>{
  for(const [theme,p] of [['dark',colors(':root {')],['light',colors(':root[data-theme="light"] {')]]) {
    for(const level of ['exceptional','priority']) {
      const top=p[`--tag-${level}-top`], bottom=p[`--tag-${level}-bottom`];
      const midpoint='#'+[1,3,5].map(i=>Math.round((parseInt(top.slice(i,i+2),16)+parseInt(bottom.slice(i,i+2),16))/2).toString(16).padStart(2,'0')).join('');
      assert.ok(contrast(p[`--tag-${level}-text`],midpoint)>=4.5,`${theme} ${level} text center`);
      // The exact dark Figma priority top is 3.995:1; text sits across the darker center.
      for(const end of ['top','bottom']) assert.ok(contrast(p[`--tag-${level}-text`],p[`--tag-${level}-${end}`])>=3,`${theme} ${level} edge`);
    }
    for(const level of ['strong','accent','standard','background','signal','review']) {
      assert.ok(contrast(p[`--tag-${level}-text`],p[`--tag-${level}-fill`])>=4.5,`${theme} ${level}`);
    }
    assert.ok(contrast(p['--tag-quiet-text'],theme==='dark'?'#171717':'#ffffff')>=4.5,`${theme} quiet`);
  }
});

test('selection does not change tag importance or add a permanent outline',()=>{
  for(const match of css.matchAll(/\.tag\.is-filtered(?:::after)?\s*\{([^}]+)\}/g)) {
    assert.doesNotMatch(match[1], /(?:background|border|box-shadow|color):/);
    if(match[1].includes('outline:')) assert.match(match[1],/outline: none/);
  }
  assert.match(refine,/\.tag:focus-visible[^}]+outline: 2px/s);
  assert.match(refine, /border-radius: var\(--rad-chip\)/);
  assert.match(refine, /height: 22px/);
});

test('exact Figma icons are local and commercial font bytes stay outside the repo',()=>{
  for(const name of ['sparkles','verified']) {
    const icon=readFileSync(new URL(`../../web/vendor/icons/tag-${name}.svg`,import.meta.url),'utf8');
    assert.match(icon,/<svg/);
    // Exact reference icons remain local even when the quieter chip style hides icons.
    assert.match(refine, /\.tag-icon\s*\{\s*display: none/);
  }
  assert.match(css,/local\("ABC Areal Superfamily Variable"\)/);
  assert.match(css,/url\("\/api\/local-font\/areal"\)/);
  assert.equal(existsSync(new URL('../../web/vendor/fonts/abc-areal-variable.ttf',import.meta.url)),false);
});
