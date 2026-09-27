import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const app = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
function section(from, to) {
  const start = app.indexOf(from), end = app.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `production section ${from}`);
  return app.slice(start, end);
}
const mention = { tag: 'mentions you', grp: 'source', source: 'auto', kind: 'auto', count: 1, total: 1 };
function mount(tags = [mention]) {
  const query = { value: '#mention' }, suggestions = {}, detail = { dataset: {}, addEventListener() {}, insertAdjacentHTML() {}, querySelectorAll: () => [], querySelector: () => null };
  const renderedTags = [];
  const nodes = { '#q': query, '#suggest': suggestions, '#detail': detail };
  const person = { id: 42, handle: 'mention_only', bio: 'Made with @owner', tags, lists: 0, edges: [] };
  const context = vm.createContext({
    document: { activeElement: query }, $: selector => nodes[selector],
    S: { person, tagList: tags }, M: {}, ORDER: { auto: 2, manual: 0 }, GORDER: { source: 0 },
    STATUSES: [], SDESC: {}, FIT_LABEL: {}, TIER_FIT: {},
    isViaTag: tag => tag.startsWith('via @'), lists: row => row.lists || 0,
    esc: value => String(value ?? ''), fmt: value => String(value ?? ''),
    plural: (n, word) => `${n} ${word}s`, avatar: () => '', igLink: () => '', safeUrl: () => null,
    swatch: () => '', tagTok: tag => `#"${tag}"`, words: value => [value],
    // Lead workflow helpers that renderDetail calls; not under test here.
    detailAccess: { capture() {}, restore() {} }, noteQueue: { peek() {} }, noteStatus: () => '', noteConflictHTML: () => '', noteInsightsHTML: () => '', humanRelationshipHTML: () => '',
    rememberDetailView() {}, detailView: () => ({ sections: {}, tag: '' }),
    rememberWorkflowForm() {}, wireWorkflow() {}, activityHTML: () => '', workflowHTML: () => '', workflowSummaryHTML: () => '',
    fitBadge: () => '', detailProfileState: () => ({ source: 'Source not recorded' }),
    connectionEvidenceHTML: () => '', websiteEvidence: () => '', scoutHTML: () => '', evidenceOf: () => [],
    ucf: value => value,
    tagChip: tag => { renderedTags.push(tag.tag); return `<span>${tag.tag}</span>`; }
  });
  vm.runInContext(section('const tagName =', '// ---------- state ----------'), context);
  vm.runInContext(section('const isFitTag =', 'const tagName ='), context);
  vm.runInContext(section('const TOP_TAGS =', 'function tagChip('), context);
  vm.runInContext(section('function tagLabel(', 'function whyHTML('), context);
  vm.runInContext(section('let sugg =', 'function moveSuggest'), context);
  vm.runInContext(section('function renderDetail()', "$('#detail').addEventListener('click', async"), context);
  return { context, person, suggestions, detail, renderedTags };
}

test('mention-only profiles show a factual mention with no inferred follow or familiarity', () => {
  const ui = mount();
  assert.equal(vm.runInContext('youLink(S.person)', ui.context), 'Mentions you');
  vm.runInContext('renderDetail()', ui.context);
  assert.match(ui.detail.innerHTML, /Mentions you/);
  assert.doesNotMatch(ui.detail.innerHTML, /Follows you|You follow|knows you|friend/i);
  // The same helper also supports string tags supplied by overview nodes.
  assert.equal(vm.runInContext("youLink({tags:['mentions you']})", ui.context), 'Mentions you');
});

test('a mention remains visible alongside a separately observed follow', () => {
  const ui = mount([mention, { tag: 'follows you' }, { tag: 'Instagram link' }]);
  assert.equal(vm.runInContext('youLink(S.person)', ui.context), 'Follows you · Mentions you');
});

test('detail shows the automatic mention tag and preserves manual source tags', () => {
  const ui = mount([mention, { tag: 'Already know them', grp: 'source', source: 'manual' }]);
  vm.runInContext('renderDetail()', ui.context);
  // Each tag renders once; the manual one carries its remove button.
  assert.deepEqual([...ui.renderedTags].sort(), ['Already know them', 'mentions you'].sort());
  assert.match(ui.detail.innerHTML, /data-rmtag="Already know them"/);
  assert.match(ui.detail.innerHTML, /class="d-sec d-connection-summary">Mentions you/);
  assert.equal(ui.person.edges.length, 0);
});

test('search suggests the factual mention tag while unrelated generated source tags stay filtered', () => {
  const ui = mount([mention, { ...mention, tag: 'internal mention metadata' }]);
  vm.runInContext('suggest()', ui.context);
  assert.equal(ui.suggestions.hidden, false);
  assert.match(ui.suggestions.innerHTML, /#"mentions you"/);
  assert.doesNotMatch(ui.suggestions.innerHTML, /internal mention metadata/);
});
