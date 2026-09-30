import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';

const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');

function setup(width = 1280, side = true) {
  const classes = new Set();
  const button = {setAttribute(name, value) { this[name] = value; }};
  const work = {classList: {toggle(name, on) { if (on) classes.add(name); else classes.delete(name); }}};
  const S = {view: 'leads', open: 42, side};
  const context = vm.createContext({
    S, window: {innerWidth: width},
    $: selector => selector === '#view-work' ? work : button,
    renderRows() {}, M: {resize() {}},
  });
  vm.runInContext(source.slice(source.indexOf('const narrow ='), source.indexOf('// ---------- tags & counts')), context);
  return {S, button, classes, run: code => vm.runInContext(code, context)};
}

test('laptop details temporarily collapse filters and restore them on close', () => {
  const ctx = setup();
  ctx.run('fitDetailLayout()');
  assert.equal(ctx.S.side, false);
  assert.equal(ctx.button['aria-expanded'], 'false');
  assert.ok(ctx.classes.has('work-noside'));
  ctx.run('fitDetailLayout(); restoreDetailLayout()');
  assert.equal(ctx.S.side, true);
  assert.equal(ctx.button['aria-expanded'], 'true');
});

test('opening details preserves filters that were already closed', () => {
  const ctx = setup(1280, false);
  ctx.run('fitDetailLayout(); restoreDetailLayout()');
  assert.equal(ctx.S.side, false);
});

test('explicit filter choices while details are open take precedence', () => {
  const ctx = setup();
  ctx.run('fitDetailLayout(); toggleSide(); toggleSide(); restoreDetailLayout()');
  assert.equal(ctx.S.side, false);
});

test('wide displays and the map retain their filter rail', () => {
  const wide = setup(1920);
  wide.run('fitDetailLayout()');
  assert.equal(wide.S.side, true);
  const map = setup();
  map.S.view = 'map';
  map.run('fitDetailLayout()');
  assert.equal(map.S.side, true);
});
