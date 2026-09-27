import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const app = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
function section(from, to) {
  const start = app.indexOf(from), end = app.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `production section ${from}`);
  return app.slice(start, end);
}
const tag = (name, grp = 'signal', source = 'auto') => ({ tag: name, grp, source, kind: source, total: 4 });
function mount() {
  const groupNode = { innerHTML: '' };
  const context = vm.createContext({
    $: () => groupNode,
    S: { pick: new Set(), cur: -1, open: null },
    KIND: { auto: '', manual: 'man', rule: 'rule' },
    tagName: t => typeof t === 'string' ? t : t.tag,
    isViaTag: name => name.startsWith('via @'),
    isFitTag: name => name.startsWith('Fit: '),
    modeOf: name => name === 'Brand' ? 'inc' : null,
    esc: value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;'),
    fmt: String, int: Number,
    lists: () => 0, avatar: () => '', igLink: () => '', noteIcon: () => '',
    fitBadge: () => '', rowFitHTML: () => '', connHTML: () => '', whyHTML: () => '', statHTML: () => '',
    LeadWorkflow: { localToday: () => '2026-09-27' },
  });
  vm.runInContext(section('const TOP_TAGS =', 'function whyHTML('), context);
  vm.runInContext(section('function rowHTML(', 'function renderRows()'), context);
  const ui = vm.runInContext('({ tagTier, tagChip, rowHTML })', context);
  return { context, groupNode, ui };
}

test('roles and review candidates do not inherit the verdict or hard-caution treatment', () => {
  const { ui } = mount();
  assert.match(ui.tagChip(tag('Fit: strong'), false), /t-hero/);
  assert.match(ui.tagChip(tag('Founder'), false), /t-decision/);
  assert.match(ui.tagChip(tag('Brand', 'role'), false), /t-role.*aria-pressed="true"/);
  assert.match(ui.tagChip(tag('Shop Link'), false), /t-plus/);
  assert.match(ui.tagChip(tag('US'), false), /t-market/);
  assert.match(ui.tagChip(tag('Agency', 'role'), false), /t-partner/);
  assert.match(ui.tagChip(tag('Creator', 'role'), false), /t-review/);
  assert.match(ui.tagChip(tag('Too big'), false), /t-flag/);
  assert.match(ui.tagChip(tag('Creator', 'custom', 'manual'), true), /t-own.*data-rmtag="Creator"/);
});

test('dense lead rows show three labels and preserve the rest in the count tooltip', () => {
  const { ui } = mount();
  const row = { id: 7, handle: 'store', tags: [
    tag('Fit: strong'), tag('Brand', 'role'), tag('Founder'), tag('Shop Link'),
    tag('Jewelry', 'niche'), tag('Warm intro', 'custom', 'manual'), tag('via @seed', 'source'),
  ] };
  const html = ui.rowHTML(row, 0, 64);
  const desktopTags = html.match(/<div class="tags c-tags">([\s\S]*?)<\/div>/)?.[1];
  assert.ok(desktopTags, 'desktop tag cell is present');
  assert.equal([...desktopTags.matchAll(/class="tag /g)].length, 3);
  assert.match(desktopTags, /title="Warm intro · Jewelry"\>\+2/);
  assert.doesNotMatch(html, /data-tag="Fit: strong"|data-tag="via @seed"/);
});

test('Tags overview shows three decision groups and folds context tags', () => {
  const { context, groupNode } = mount();
  const rows = [tag('Brand', 'role'), tag('Shop Link'), tag('Too big'), tag('Creator', 'role'), tag('Jewelry', 'niche')];
  context.rows = rows;
  vm.runInContext(`const groups = {${section('  renderGroups(q) {', '  syncRen() {')}}; groups.list = rows; groups.renderGroups('');`, context);
  const html = groupNode.innerHTML;
  assert.doesNotMatch(html, /Best prospects/);
  assert.match(html, /Business signals[\s\S]*Needs a look[\s\S]*Other automatic tags[\s\S]*Products/);
  assert.match(html, /tchip t-flag[^>]*>\<span\>Too big/);
  assert.match(html, /tchip t-review[^>]*>\<span\>Creator/);
});
