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
    fmt: String, int: Number, swatch: () => '',
    lists: () => 0, avatar: () => '', igLink: () => '', noteIcon: () => '',
    fitBadge: () => '', rowFitHTML: () => '', connHTML: () => '', whyHTML: () => '', statHTML: () => '',
    LeadWorkflow: { localToday: () => '2026-09-27' },
  });
  vm.runInContext(section('const TOP_TAGS =', 'function whyHTML('), context);
  vm.runInContext(section('function tagItem(', 'function tagSection('), context);
  vm.runInContext(section('function rowHTML(', 'function renderRows()'), context);
  const ui = vm.runInContext('({ tagTier, tagHue, tagChip, tagItem, rowHTML, rowTagSelection })', context);
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
  assert.match(ui.tagChip(tag('Client', 'custom', 'manual'), false), /t-client/);
});

test('dense lead rows show distinct evidence and preserve the rest in the count tooltip', () => {
  const { ui } = mount();
  const row = { id: 7, handle: 'store', tags: [
    tag('Fit: strong'), tag('Brand', 'role'), tag('Founder'), tag('Shop Link'),
    tag('Jewelry', 'niche'), tag('Warm intro', 'custom', 'manual'), tag('via @seed', 'source'),
  ] };
  const html = ui.rowHTML(row, 0, 64);
  const desktopTags = html.match(/<div class="tags c-tags">([\s\S]*?)<\/div>/)?.[1];
  assert.ok(desktopTags, 'desktop tag cell is present');
  assert.equal([...desktopTags.matchAll(/class="tag /g)].length, 3);
  assert.match(desktopTags, /data-tag="Warm intro"[\s\S]*data-tag="Jewelry"[\s\S]*data-tag="Shop Link"/);
  assert.match(desktopTags, /title="Founder · Brand"\>\+2/);
  assert.doesNotMatch(html, /data-tag="Fit: strong"|data-tag="via @seed"/);
});

test('near-synonymous fit and identity chips cannot fill a lead row', () => {
  const { ui } = mount();
  const row = { id: 8, handle: 'founder', tags: [
    tag('AI: Top fit', 'ai'), tag('AI: Decision maker', 'ai'), tag('Founder', 'role'),
    tag('AI: Skincare', 'niche'), tag('AI: Runs ads', 'ai'),
    tag('client', 'custom', 'manual'), tag('follows you', 'source'),
  ] };
  const html = ui.rowHTML(row, 0, 64);
  const desktopTags = html.match(/<div class="tags c-tags">([\s\S]*?)<\/div>/)?.[1];
  assert.match(desktopTags, /data-tag="client"[\s\S]*data-tag="follows you"[\s\S]*data-tag="AI: Skincare"/);
  assert.doesNotMatch(desktopTags, /data-tag="Founder"|data-tag="AI: Decision maker"|data-tag="AI: Top fit"/);
  assert.match(desktopTags, /\+4/);
  assert.match(ui.tagChip(tag('AI: Skincare', 'niche'), false), /data-tag="AI: Skincare"[^>]*title="AI: Skincare · auto"[^>]*><span>Skincare<\/span>/);

  const sparse = ui.rowTagSelection({ tags: [tag('Founder', 'role'), tag('AI: Decision maker', 'ai'), tag('AI: Top fit', 'ai')] });
  assert.equal(sparse.shown.length, 2);
  assert.equal(sparse.hidden.length, 1);
});

test('generic profile access and a repeated fit verdict stay out of informative rows', () => {
  const { ui } = mount();
  const row = { id: 9, handle: 'shop', business_fit: 86, score: 90, tags: [
    tag('Instagram link', 'source'), tag('you follow', 'source'),
    tag('AI: Top fit', 'ai'), tag('AI: Decision maker', 'ai'),
    tag('AI: Supplements', 'niche'), tag('Shop Link', 'signal'),
  ] };
  const selected = ui.rowTagSelection(row);
  assert.deepEqual(Array.from(selected.shown, (t) => t.tag), ['AI: Supplements', 'Shop Link', 'AI: Decision maker']);
  assert.deepEqual(Array.from(selected.hidden, (t) => t.tag).sort(), ['AI: Top fit', 'Instagram link', 'you follow'].sort());
  const accessOnly = ui.rowTagSelection({ tags: [tag('Instagram link', 'source')] });
  assert.equal(accessOnly.shown[0].tag, 'Instagram link');
  const repeatedNiche = ui.rowTagSelection({ tags: [
    tag('Supplements', 'niche'), tag('AI: Supplements', 'ai'),
    tag('Founder', 'role'), tag('you follow', 'source'), tag('Verified'),
  ] });
  assert.deepEqual(Array.from(repeatedNiche.shown, (t) => t.tag), ['Supplements', 'Founder']);
  assert.deepEqual(Array.from(repeatedNiche.hidden, (t) => t.tag).sort(), ['AI: Supplements', 'Verified', 'you follow'].sort());
});

test('niche families keep the same color across chips and filters', () => {
  const { ui } = mount();
  const families = [
    ['Beauty', 'h-beauty'], ['AI: Skincare', 'h-beauty'],
    ['Coffee', 'h-food'], ['Apparel', 'h-style'], ['AI: Jewelry', 'h-style'],
    ['Supplements', 'h-wellness'], ['Fitness', 'h-wellness'],
    ['Home', 'h-lifestyle'], ['AI: Pets', 'h-lifestyle'],
  ];
  for (const [name, hue] of families) {
    const niche = tag(name, name.startsWith('AI: ') ? 'ai' : 'niche');
    assert.equal(ui.tagHue(niche), hue);
    assert.match(ui.tagChip(niche), new RegExp(`class="tag [^\"]*${hue}`));
    assert.match(ui.tagItem({ ...niche, sources: ['auto'], count: 1 }), new RegExp(`class="fi [^\"]*${hue}`));
  }
  assert.equal(ui.tagHue(tag('Outdoor', 'niche')), '');
  assert.equal(ui.tagHue(tag('Beauty', 'niche', 'manual')), '');
  assert.equal(ui.tagHue(tag('Founder', 'role')), '');
});

test('mobile overflow counts hidden same-facet tags with one visible chip', () => {
  const { ui } = mount();
  const html = ui.rowHTML({ id: 10, handle: 'founder', tags: [tag('Founder', 'role'), tag('AI: Decision maker', 'ai')] }, 0, 64);
  const mobile = html.match(/<div class="row-mobile-tags">([\s\S]*?)<\/div>/)?.[1];
  assert.match(mobile, /data-tag="Founder"/);
  assert.match(mobile, /title="AI: Decision maker">\+1/);
});

test('Tags overview shows three decision groups and folds context tags', () => {
  const { context, groupNode } = mount();
  const rows = [tag('Brand', 'role'), tag('Shop Link'), tag('Too big'), tag('Creator', 'role'), tag('Jewelry', 'niche')];
  context.rows = rows;
  vm.runInContext(`const groups = {${section('  renderGroups(q) {', '  syncRen() {')}}; groups.list = rows; groups.renderGroups('');`, context);
  const html = groupNode.innerHTML;
  assert.doesNotMatch(html, /Best prospects/);
  assert.match(html, /Business signals[\s\S]*Needs a look[\s\S]*Other automatic tags[\s\S]*Products/);
  assert.match(html, /tchip t-niche h-style/);
  assert.match(html, /tchip t-flag[^>]*>\<span\>Too big/);
  assert.match(html, /tchip t-review[^>]*>\<span\>Creator/);
});
