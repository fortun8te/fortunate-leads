const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm');
const Core = require('../web/map-core.js');
const Model = require('../web/map-model.js');
const { MapView, recordedOwnerConnections, recordedBackgroundConnections } = require('../web/map-view.js');
const mark = (id, x, y, r = 6, extra = {}) => ({kind:'n',x,y,r,it:{d:{id,...extra},x,y}});
function viewFor(selected = 1, reduced = true) {
  return Object.assign(Object.create(MapView.prototype), {
    model:{selected:{id:selected},world:{me:{id:0}},reduced},invalidate() {},
  });
}

test('clicking a neighboring portrait body selects it instead of the previous selection halo', () => {
  const selected = mark(1, 100, 100, 10), other = mark(2, 112, 100, 6);
  const view = viewFor(); view.displayMarks = {nodes:[selected,other],groups:[]};
  assert.equal(view.pick(112,100).it.d.id,2,'the old +3px halo used to intercept this click');
  selected.r = 16;
  assert.equal(view.pick(112,100).it.d.id,2,'the nearest real portrait body wins during enlargement');
  let selectedId;
  view.hideTip=()=>{};view.model.select=person=>{selectedId=person.id;};
  view.activate(view.pick(112,100));
  assert.equal(selectedId,2);
});

test('each new selection visibly grows and paints last without changing positions or camera', () => {
  const view=viewFor(), first=mark(1,100,100), second=mark(2,140,100), owner=mark(0,50,50,18);
  const items=[first.it,second.it,owner.it], original=items.map(it=>[it.x,it.y]);
  view.model.cam={cx:.5,cy:.5,k:1}; const camera={...view.model.cam};
  const plan=()=>({nodes:items.map(it=>({...({1:first,2:second,0:owner}[it.d.id]),r:it.d.id===0?18:6})),groups:[]});
  let current=plan();view.emphasizeMarks(current,.016);
  assert.equal(current.nodes[0].r,9,'three pixels are visible on a six-pixel portrait');
  assert.equal(view.portraitOrder(current).at(-1).it.d.id,1);
  view.model.selected={id:2};current=plan();view.emphasizeMarks(current,.016);
  assert.equal(current.nodes[0].r,6,'the previous portrait shrinks');
  assert.equal(current.nodes[1].r,9,'the next portrait grows independently');
  assert.equal(view.portraitOrder(current).at(-1).it.d.id,2);
  view.model.selected={id:0};current=plan();view.emphasizeMarks(current,.016);
  assert.equal(current.nodes[2].r,18,'owner geometry and caption remain fixed');
  assert.deepEqual(items.map(it=>[it.x,it.y]),original);
  assert.deepEqual(view.model.cam,camera);
});

test('selection growth animates independently on each portrait and settles after deselection', () => {
  const view=viewFor(1,false), items=[mark(1,100,100).it,mark(2,140,100).it];
  const plan=()=>({nodes:items.map(it=>({kind:'n',it,x:it.x,y:it.y,r:6})),groups:[]});
  for(let i=0;i<30;i++)view.emphasizeMarks(plan(),.016);
  assert.equal(items[0].displayFocus,1);
  view.model.selected={id:2};view.emphasizeMarks(plan(),.016);
  assert.ok(items[0].displayFocus<1);assert.ok(items[1].displayFocus>0);
  for(let i=0;i<30;i++)view.emphasizeMarks(plan(),.016);
  assert.equal(items[0].displayFocus,0);assert.equal(items[1].displayFocus,1);
  view.model.selected=null;
  for(let i=0;i<30;i++)view.emphasizeMarks(plan(),.016);
  assert.equal(items[1].displayFocus,0);
});

test('faint background connections use recorded owner directions, never shared audience or status', () => {
  const owner=mark(0,50,50), nodes=[owner,
    mark(1,70,50,6,{followed:true}),mark(2,50,70,6,{follows_me:true}),
    mark(3,80,80,6,{followed:true,follows_me:true}),
    mark(4,90,80,6,{source_count:8,status:'client'}),mark(5,100,80,6,{followed:'unknown'})];
  assert.deepEqual(recordedOwnerConnections({nodes},0).map(line=>[line.other.it.d.id,line.outgoing,line.incoming]),[
    [1,true,false],[2,false,true],[3,true,true],
  ]);
  assert.deepEqual(recordedOwnerConnections({nodes},0,2).map(line=>line.other.it.d.id),[1,3]);
  assert.equal(recordedOwnerConnections({nodes:nodes.slice(1)},0).length,0);
});

test('background paths are bounded, faint and restore canvas state', () => {
  const owner=mark(0,50,50), nodes=[owner,...Array.from({length:800},(_,i)=>mark(i+1,i,80,6,{followed:true}))];
  assert.equal(recordedOwnerConnections({nodes},0,null,1000).length,500);
  const strokes=[], ctx={save(){},restore(){this.restored=true;},setLineDash(){},beginPath(){},moveTo(){},lineTo(){},stroke(){strokes.push({alpha:this.globalAlpha,width:this.lineWidth});}};
  const view=viewFor();view.model.selected=null;
  view.paintBackgroundEdges(ctx,{fg3:'#777'},{nodes});
  assert.equal(strokes.length,500);assert.equal(strokes[0].alpha,.06);assert.equal(strokes[0].width,.7);
  assert.equal(ctx.restored,true);
});

test('old shared preference migrates once, then an explicit shared choice persists', () => {
  const values=new Map([['fortunate.map.view','shared']]);
  const context={MapCore:Core,MapModel:Model,localStorage:{getItem:key=>values.get(key),setItem:(key,value)=>values.set(key,value)}};
  vm.runInNewContext(fs.readFileSync(require.resolve('../web/map-view.js'),'utf8'),context);
  assert.equal(context.MapViewModule.savedViewMode(),'network');
  assert.equal(values.get('fortunate.map.view'),'network');
  values.set('fortunate.map.view','shared');
  assert.equal(context.MapViewModule.savedViewMode(),'shared');
});


test('background lines include bounded observed cohort edges and deduplicate owner spokes', () => {
  const nodes=[mark(0,10,10),mark(1,30,30,6,{followed:true}),mark(2,50,50),mark(3,70,70)];
  const connections=[{source:0,target:1,kind:'follow'},{source:1,target:2,kind:'follow'},
    {source:2,target:1,kind:'follow'},{source:2,target:3,kind:'overlap'},
    {source:2,target:99,kind:'follow'},{source:3,target:3,kind:'follow'}];
  const lines=recordedBackgroundConnections({nodes},0,null,connections);
  assert.deepEqual(lines.map(line=>[line.owner.it.d.id,line.other.it.d.id]),[[0,1],[1,2]]);
  assert.equal(recordedBackgroundConnections({nodes},0,1,connections).length,0,'selected paths are painted separately');
});
