const test = require('node:test');
const assert = require('node:assert/strict');
const { Camera, Scene, displayPlan } = require('../web/map-core.js');
function fixture(w = 1000, h = 700) {
  const cam = new Camera(); cam.resize(w, h);
  const scene = new Scene();
  const guides = [{id: 0, label: 'Audience of @one', x: .35, y: .4}, {id: 1, label: 'Audience of @two', x: .7, y: .65}];
  const nodes = Array.from({ length: 700 }, (_, i) => ({id: i, cluster: i % 2, rank: i / 700, fit: 'good', x: .35 + (i % 12 - 6) * .006, y: .4 + (Math.floor(i / 12) % 12 - 6) * .006}));
  const clusters = Array.from({length: 40}, (_, i) => ({id: i, x: .35 + i * .0005, y: .4, count: 10, label: 'Audience of @one'}));
  scene.apply({ nodes, clusters }, 0, {instant: true});
  return {cam, scene, guides};
}
test('overview merges repeated audience labels into one readable summary', () => {
  const {cam, scene, guides} = fixture(); const plan = displayPlan(scene, cam, guides);
  assert.equal(plan.groups.length, 1); assert.equal(plan.groups[0].it.d.label, 'Audience of @one');
  assert.ok(plan.groups[0].it.d.count >= 400); assert.ok(plan.nodes.length < 70);
});
test('overview never paints overlapping count markers or individual dots', () => {
  const {cam, scene, guides} = fixture(); const plan = displayPlan(scene, cam, guides);
  const marks = [...plan.groups, ...plan.nodes];
  for (let i = 0; i < marks.length; i++) for (let j = i + 1; j < marks.length; j++) {
    assert.ok(Math.hypot(marks[i].x - marks[j].x, marks[i].y - marks[j].y) >= marks[i].r + marks[j].r + 7);
  }
});
test('selected person remains visible even inside a summary footprint', () => {
  const {cam, scene, guides} = fixture(); const selected = scene.nodes[0].d;
  const plan = displayPlan(scene, cam, guides, selected);
  assert.ok(plan.nodes.some(n => n.it.d.id === selected.id));
});
test('zoom progressively reveals individual people while preserving coordinates', () => {
  const {cam, scene, guides} = fixture(); const before = displayPlan(scene, cam, guides);
  const positions = scene.nodes.map(n => [n.x, n.y]); cam.set(.35, .4, 5);
  const after = displayPlan(scene, cam, guides); assert.ok(after.nodes.length > before.nodes.length);
  assert.deepEqual(scene.nodes.map(n => [n.x, n.y]), positions);
});
test('phone plan fits available screen and leaves the open details sheet clear', () => {
  const {cam, scene, guides} = fixture(390, 650); cam.inset.bottom = 250;
  const plan = displayPlan(scene, cam, guides);
  assert.ok(plan.nodes.every(n => n.x >= 8 && n.x <= 382 && n.y <= 392));
  assert.ok(plan.groups.every(n => n.x >= 20 && n.x <= 370 && n.y <= 380));
});
