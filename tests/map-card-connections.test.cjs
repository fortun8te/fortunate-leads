const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm'), fs = require('node:fs');
function element(tag) {
  return { tag, children: [], attrs: {}, setAttribute(key, value) { this.attrs[key] = value; },
    append(...kids) { this.children.push(...kids); }, replaceChildren(...kids) { this.children = kids; },
    addEventListener(name, callback) { this[name] = callback; } };
}
const context = { MapCore: require('../web/map-core.js'), MapModel: require('../web/map-model.js'), document: { createElement: element } };
vm.runInNewContext(fs.readFileSync(require.resolve('../web/map-view.js'), 'utf8'), context);
const { MapView, directedConnections } = context.MapViewModule;
const edge = (from, to, kind = 'follows') => ({ a: { id: from, x: 0, y: 0 }, b: { id: to, x: 10, y: 0 }, kind });
test('directions are relative to the selected person, deduplicated, and supported by recorded follows', () => {
  const lines = directedConnections([edge(1, 2), edge(1, 2), edge(3, 1), edge(1, 4), edge(4, 1), edge(1, 5, 'overlap'), edge(6, 7), edge(1, 1)], '1');
  assert.deepEqual(Array.from(lines, l => [l.other.id, l.outgoing, l.incoming]), [[2, true, false], [3, false, true], [4, true, true]]);
  assert.equal(directedConnections([edge(1, 2, 'mutual')], 1)[0].incoming, true);
});
test('outgoing paths are dashed, incoming solid, and mutual shows both without duplicate strokes', () => {
  const strokes = [];
  const ctx = { setLineDash(value) { this.dash = value; }, beginPath() {}, moveTo() {}, lineTo() {}, stroke() { strokes.push([...this.dash]); } };
  const view = { edgeAt: 0, model: { reduced: true, scene: { get() {} } } };
  MapView.prototype.paintEdges.call(view, ctx, { fg3: '#888' }, { lines: [edge(1, 2), edge(3, 1), edge(1, 4), edge(4, 1), edge(1, 4), edge(1, 5, 'overlap')] }, x => x, y => y, 300, { id: 1 });
  assert.deepEqual(strokes, [[4, 4], [], [], [4, 4]]);
  assert.equal(ctx.dash.length, 0); assert.equal(ctx.globalAlpha, 1); assert.equal(ctx.lineWidth, .75);
});
test('map tags use the shared Leads presentation while keeping Client once and inline removal', () => {
  const chips = element('div'), edited = [];
  const view = { host: { tagPresentation(label) { return { label, importance: label === 'Client' ? 'strong' : 'standard', tone: '', icon: label === 'Client' ? 'verified' : '' }; } }, editTag(...args) { edited.push(args); } };
  MapView.prototype.paintTags.call(view, { id: 1 }, { relationships: ['client'], manual_tags: ['Client', 'Founder'] }, { querySelector: () => chips });
  assert.equal(chips.children.length, 2);
  const client = chips.children[0];
  assert.equal(client.className, 'tag mv-card-tag'); assert.equal(client.attrs['data-importance'], 'strong');
  assert.equal(client.children[0].attrs['data-icon'], 'verified'); assert.equal(client.children[1].textContent, 'Client');
  client.click(); assert.deepEqual(edited, [[{ id: 1 }, 'Client', true]]);
});
