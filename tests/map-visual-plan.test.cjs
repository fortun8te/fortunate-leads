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

test('network overview always keeps owner visible and bounds exploration', () => {
  const {cam, scene} = fixture();
  const guides = [{id: 0, label: 'Direct connections', x: .35, y: .4}];
  scene.nodes[0].d.rank = 0;
  const plan = displayPlan(scene, cam, guides, null, null, 0);
  assert.ok(plan.nodes.some(n => n.it.d.id === 0));
  assert.ok(plan.nodes.length <= 20);
  cam.set(.5, .5, 1000);
  assert.equal(cam.k, 6);
});
test('portrait overview samples distinct communities and preserves real positions', () => {
  const cam = new Camera(); cam.resize(1400,1000);
  const scene = new Scene();
  const nodes = Array.from({length:500}, (_,i)=>({id:i,cluster:i<350?0:1+Math.floor((i-350)/25),x:.06+(i%20)*.045,y:.06+Math.floor(i/20)*.035,fit:'unread',rank:1-i/500}));
  scene.apply({nodes,clusters:[]},0,{instant:true});
  const plan = displayPlan(scene,cam,[],null,null,0);
  assert.ok(plan.nodes.length > 70);
  assert.ok(new Set(plan.nodes.map(n=>n.it.d.cluster)).size >= 6);
  assert.ok(plan.nodes.length <= 140);
  assert.equal(plan.nodes.find(n=>n.it.d.id===0).r,30);
  for(const mark of plan.nodes) assert.deepEqual([mark.it.x,mark.it.y],[nodes[mark.it.d.id].x,nodes[mark.it.d.id].y]);
});
test('overview count chips include loaded people hidden by portrait collisions', () => {
  const cam=new Camera();cam.resize(1400,1000);const scene=new Scene();
  const guides=Array.from({length:6},(_,i)=>({id:i,label:'Audience '+i,x:.18+(i%3)*.3,y:i<3?.28:.72}));
  const nodes=Array.from({length:600},(_,i)=>({id:i,cluster:i%6,rank:i/600,fit:'weak',x:guides[i%6].x+(i%7)*.003,y:guides[i%6].y+(i%11)*.003}));
  const clusters=guides.map(g=>({id:g.id,label:g.label,x:g.x,y:g.y,count:20}));
  scene.apply({nodes,clusters},0,{instant:true});
  const plan=displayPlan(scene,cam,guides);
  assert.equal(plan.nodes.length+plan.groups.reduce((s,g)=>s+g.it.d.count,0),720);
});

test('mode encodings preserve circle geometry and use truthful sizes', () => {
  const {radiusFor,LEGENDS}=require('../web/map-core.js');
  const strong={fit:'strong',source_count:1},weak={fit:'weak',source_count:8};
  assert.ok(radiusFor(strong,1,'fit')>radiusFor(weak,1,'fit'));
  assert.ok(radiusFor(weak,1,'seeds')>radiusFor(strong,1,'seeds'));
  assert.equal(radiusFor(strong,1,'status'),radiusFor(weak,1,'status'));
  for(const mode of ['closeness','fit','seeds','status']) {
    const {cam,scene,guides}=fixture();const before=scene.nodes.map(n=>[n.x,n.y]);
    const plan=displayPlan(scene,cam,guides,null,null,0,mode);
    assert.deepEqual(scene.nodes.map(n=>[n.x,n.y]),before);
    assert.equal(plan.nodes.find(n=>n.it.d.id===0).r,30);
    assert.equal(LEGENDS[mode][0].s,'Distance: evidence');
    assert.equal(LEGENDS[mode].length,3);
  }
});
test('Fit and Sources preserve mode priority instead of forcing pipeline people first', () => {
  const cam=new Camera();cam.resize(1000,700);const scene=new Scene();
  scene.apply({nodes:[{id:1,x:.5,y:.5,status:'client',fit:'weak',rank:.1},{id:2,x:.5,y:.5,fit:'strong',rank:.9}],clusters:[]},0,{instant:true});
  assert.equal(displayPlan(scene,cam,[],null,null,null,'fit').nodes[0].it.d.id,2);
  assert.equal(displayPlan(scene,cam,[],null,null,null,'seeds').nodes[0].it.d.id,2);
  assert.equal(displayPlan(scene,cam,[],null,null,null,'closeness').nodes[0].it.d.id,1);
});
test('modest owner portrait keeps its fixed identity caption clear', () => {
  const cam=new Camera();cam.resize(1400,1000);const scene=new Scene();
  const nodes=[{id:0,x:.5,y:.5,fit:'unread'},...Array.from({length:100},(_,i)=>({id:i+1,x:.44+(i%10)*.015,y:.5+Math.floor(i/10)*.015,fit:'strong',rank:i/100}))];
  scene.apply({nodes,clusters:[]},0,{instant:true});const plan=displayPlan(scene,cam,[],null,null,0);
  const owner=plan.nodes.find(n=>n.it.d.id===0);assert.equal(owner.r,30);
  for(const mark of plan.nodes.filter(n=>n.it.d.id!==0)) {
    const x=Math.max(owner.x-45,Math.min(mark.x,owner.x+45));
    const y=Math.max(owner.y+37,Math.min(mark.y,owner.y+56));
    assert.ok(Math.hypot(mark.x-x,mark.y-y)>mark.r);
  }
});
