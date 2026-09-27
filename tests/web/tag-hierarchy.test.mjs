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
  const ui = vm.runInContext('({ tagTier, tagChip, tagItem, rowHTML, rowTagSelection, detailTagSelection })', context);
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

test('dense lead rows lead with user labels and count distinct remaining evidence', () => {
  const { ui } = mount();
  const row = { id: 7, handle: 'store', tags: [
    tag('Fit: strong'), tag('Brand', 'role'), tag('Founder'), tag('Shop Link'),
    tag('Jewelry', 'niche'), tag('Warm intro', 'custom', 'manual'), tag('via @seed', 'source'),
  ] };
  const html = ui.rowHTML(row, 0, 64);
  const desktopTags = html.match(/<div class="tags c-tags">([\s\S]*?)<\/div>/)?.[1];
  assert.ok(desktopTags, 'desktop tag cell is present');
  assert.equal([...desktopTags.matchAll(/class="tag /g)].length, 3);
  assert.match(desktopTags, /data-tag="Warm intro"[\s\S]*data-tag="Founder"[\s\S]*data-tag="Brand"/);
  assert.match(desktopTags, /title="Shop Link · Jewelry"\>\+2/);
  assert.doesNotMatch(html, /data-tag="Fit: strong"|data-tag="via @seed"/);
});

test('client, relationship and caution outrank fit and niche; synonymous decisions use one slot', () => {
  const { ui } = mount();
  const dense = ui.rowTagSelection({ score: 91, tags: [
    tag('AI: Skincare', 'niche'), tag('Scout: Strong'), tag('Too big'),
    tag('Client', 'custom', 'manual'), tag('follows you', 'source'),
    tag('Founder', 'role'), tag('AI: Decision maker', 'ai'),
  ] });
  assert.deepEqual(Array.from(dense.shown, (t) => t.tag), ['Client', 'follows you', 'Too big']);
  assert.equal(dense.hidden.length, 3);
  assert.deepEqual(Array.from(dense.suppressed, (t) => t.tag), ['AI: Decision maker']);

  const decision = ui.rowTagSelection({ tags: [
    tag('AI: Skincare', 'niche'), tag('Shop Link'), tag('Founder', 'role', 'manual'),
    tag('AI: Decision maker', 'ai'), tag('Scout: Strong'), tag('Too big'),
  ] });
  assert.deepEqual(Array.from(decision.shown, (t) => t.tag), ['Founder', 'Too big', 'Scout: Strong']);
  assert.deepEqual(Array.from(decision.hidden, (t) => t.tag), ['Shop Link', 'AI: Skincare']);
  assert.deepEqual(Array.from(decision.suppressed, (t) => t.tag), ['AI: Decision maker']);
});

test('near-synonymous fit and identity chips cannot fill a lead row', () => {
  const { ui } = mount();
  const row = { id: 8, handle: 'founder', score: 90, tags: [
    tag('AI: Top fit', 'ai'), tag('AI: Decision maker', 'ai'), tag('Founder', 'role'),
    tag('AI: Skincare', 'niche'), tag('AI: Runs ads', 'ai'),
    tag('client', 'custom', 'manual'), tag('follows you', 'source'),
  ] };
  const html = ui.rowHTML(row, 0, 64);
  const desktopTags = html.match(/<div class="tags c-tags">([\s\S]*?)<\/div>/)?.[1];
  assert.match(desktopTags, /data-tag="client"[\s\S]*data-tag="follows you"[\s\S]*data-tag="Founder"/);
  assert.doesNotMatch(desktopTags, /data-tag="AI: Decision maker"|data-tag="AI: Top fit"|data-tag="AI: Skincare"/);
  assert.match(desktopTags, /\+2/);
  assert.match(ui.tagChip(tag('AI: Skincare', 'niche'), false), /data-tag="AI: Skincare"[^>]*title="AI: Skincare · auto"[^>]*><span>Skincare<\/span>/);

  const sparse = ui.rowTagSelection({ tags: [tag('Founder', 'role'), tag('AI: Decision maker', 'ai'), tag('AI: Top fit', 'ai')] });
  assert.equal(sparse.shown.length, 2);
  assert.equal(sparse.hidden.length, 0);
  assert.equal(sparse.suppressed.length, 1);
});

test('generic profile access and a repeated fit verdict stay out of informative rows', () => {
  const { ui } = mount();
  const row = { id: 9, handle: 'shop', business_fit: 86, score: 90, tags: [
    tag('Instagram link', 'source'), tag('you follow', 'source'),
    tag('AI: Top fit', 'ai'), tag('AI: Decision maker', 'ai'),
    tag('AI: Supplements', 'niche'), tag('Shop Link', 'signal'),
  ] };
  const selected = ui.rowTagSelection(row);
  assert.deepEqual(Array.from(selected.shown, (t) => t.tag), ['AI: Decision maker', 'Shop Link', 'AI: Supplements']);
  assert.equal(selected.hidden.length, 0);
  assert.deepEqual(Array.from(selected.suppressed, (t) => t.tag).sort(), ['AI: Top fit', 'Instagram link', 'you follow'].sort());
  const accessOnly = ui.rowTagSelection({ tags: [tag('Instagram link', 'source')] });
  assert.equal(accessOnly.shown[0].tag, 'Instagram link');
  const repeatedNiche = ui.rowTagSelection({ tags: [
    tag('Supplements', 'niche'), tag('AI: Supplements', 'ai'),
    tag('Founder', 'role'), tag('you follow', 'source'), tag('Verified'),
  ] });
  assert.deepEqual(Array.from(repeatedNiche.shown, (t) => t.tag), ['Founder', 'Supplements']);
  assert.equal(repeatedNiche.hidden.length, 0);
  assert.deepEqual(Array.from(repeatedNiche.suppressed, (t) => t.tag).sort(), ['AI: Supplements', 'Verified', 'you follow'].sort());
});

test('niche chips and filters use one neutral tier without product hues', () => {
  const { ui } = mount();
  const names = ['Beauty', 'AI: Skincare', 'Coffee', 'Apparel', 'AI: Jewelry', 'Supplements', 'Fitness', 'Home', 'AI: Pets'];
  for (const name of names) {
    const niche = tag(name, name.startsWith('AI: ') ? 'ai' : 'niche');
    assert.equal(ui.tagTier(niche), 'niche');
    assert.match(ui.tagChip(niche), /class="tag [^"]* t-niche"/);
    assert.match(ui.tagItem({ ...niche, sources: ['auto'], count: 1 }), /class="fi t-niche"/);
  }
  assert.equal(ui.tagTier(tag('Beauty', 'niche', 'manual')), 'own');
});

test('mobile overflow omits synonymous decision tags', () => {
  const { ui } = mount();
  const html = ui.rowHTML({ id: 10, handle: 'founder', tags: [tag('Founder', 'role'), tag('AI: Decision maker', 'ai')] }, 0, 64);
  const mobile = html.match(/<div class="row-mobile-tags">([\s\S]*?)<\/div>/)?.[1];
  assert.match(mobile, /data-tag="Founder"/);
  assert.doesNotMatch(mobile, /AI: Decision maker|\+1/);
});

test('different commerce facts remain counted while literal duplicates do not', () => {
  const { ui } = mount();
  const chosen = ui.rowTagSelection({ tags: [
    tag('Founder', 'role'), tag('Brand', 'role'), tag('Shop Link'),
    tag('Shopify'), tag('AI: Runs ads', 'ai'), tag('AI: Brand', 'ai'),
  ] });
  assert.deepEqual(Array.from(chosen.shown, (t) => t.tag), ['Founder', 'Brand', 'Shop Link']);
  assert.deepEqual(Array.from(chosen.hidden, (t) => t.tag), ['Shopify', 'AI: Runs ads']);
  assert.deepEqual(Array.from(chosen.suppressed, (t) => t.tag), ['AI: Brand']);
});

test('detail keeps user tags prominent and makes repeated labels accessible', () => {
  const { ui } = mount();
  const chosen = ui.detailTagSelection({ business_fit: 82, tags: [
    tag('AI: Top fit', 'ai'), tag('AI: Skincare', 'ai'), tag('Skincare', 'niche', 'rule'),
    tag('Warm intro', 'custom', 'manual'), tag('Verified'), tag('Instagram link', 'source'),
    tag('Fit: strong', 'signal'), tag('Founder', 'role'), tag('AI: Decision maker', 'ai'),
  ] });
  assert.deepEqual(Array.from(chosen.primary, (t) => t.tag), ['Warm intro', 'Founder', 'Skincare']);
  assert.deepEqual(Array.from(chosen.related, (t) => t.tag).sort(),
    ['AI: Top fit', 'AI: Skincare', 'Verified', 'Instagram link', 'Fit: strong', 'AI: Decision maker'].sort());
});

test('Tags overview shows three decision groups and folds context tags', () => {
  const { context, groupNode } = mount();
  const rows = [tag('Brand', 'role'), tag('Shop Link'), tag('Too big'), tag('Creator', 'role'), tag('Jewelry', 'niche')];
  context.rows = rows;
  vm.runInContext(`const groups = {${section('  renderGroups(q) {', '  syncRen() {')}}; groups.list = rows; groups.renderGroups('');`, context);
  const html = groupNode.innerHTML;
  assert.doesNotMatch(html, /Best prospects/);
  assert.match(html, /Business signals[\s\S]*Needs a look[\s\S]*Other automatic tags[\s\S]*Products/);
  assert.match(html, /tchip t-niche/);
  assert.match(html, /tchip t-flag[^>]*>\<span\>Too big/);
  assert.match(html, /tchip t-review[^>]*>\<span\>Creator/);
});

test('Tags search shows a bounded page and reveals additional matches on request', () => {
  const { context, groupNode } = mount();
  context.rows = Array.from({ length: 75 }, (_, i) => tag(`Signal ${i}`, 'signal'));
  vm.runInContext(`const groups = {${section('  renderGroups(q) {', '  syncRen() {')}}; groups.list = rows; groups.renderGroups('signal');`, context);
  assert.equal([...groupNode.innerHTML.matchAll(/data-go=/g)].length, 20);
  assert.match(groupNode.innerHTML, /\+55 more/);
  vm.runInContext("groups.more = { signal: 70 }; groups.renderGroups('signal');", context);
  assert.equal([...groupNode.innerHTML.matchAll(/data-go=/g)].length, 70);
  assert.match(groupNode.innerHTML, /\+5 more/);
});
