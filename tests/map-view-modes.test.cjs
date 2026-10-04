const test=require('node:test');
const assert=require('node:assert/strict');
const {MapModel,VIEW_MODES}=require('../web/map-model.js');
const {cohortLayout}=require('../web/map-core.js');
const nodes=[
 {id:1,handle:'close',x:.4,y:.4,closeness:.9,followers:100,source_count:1,fit:'good'},
 {id:2,handle:'shared',x:.7,y:.2,closeness:.2,followers:10000,source_count:8,fit:'good'},
 {id:3,handle:'large',x:.8,y:.7,closeness:.5,followers:1000000,source_count:3,fit:'good'}
];
const distance=n=>Math.hypot(n.x-.5,n.y-.5);
test('My network combines connection distance with follower size; shared uses recorded audience overlap',()=>{
 const network=cohortLayout(nodes,{id:0},'followers','network');
 assert.deepEqual(network.slice(0,-1).map(n=>n.id),[1,3,2]);
 assert.ok(network[0].portraitRadius<network[1].portraitRadius);
 assert.ok(distance(network[0])<distance(network[1]));
 const shared=cohortLayout(nodes,{id:0},'connections','shared');
 assert.deepEqual(shared.slice(0,-1).map(n=>n.id),[2,3,1]);
 assert.ok(shared[0].portraitRadius>shared[1].portraitRadius);
 assert.ok(distance(shared[0])<distance(shared[1]));
 assert.equal(shared.at(-1).x,.5);assert.equal(shared.at(-1).y,.5);
 assert.ok(VIEW_MODES.find(mode=>mode.id==='shared').description.includes('not mutual friends'));
});
test('view presets repack the same people without changing camera, cursors, evidence, or fetching',()=>{
 const model=new MapModel({reduced:true,fetchJson:()=>assert.fail('mode change must stay local')});
 model.apply({nodes,world:{me:{id:0,x:.5,y:.5}},total:10000,next_cursor:'next'},{});
 model.selected=model.scene.get(1).d;model.scene.pin(model.selected);
 model.cam.set(.6,.4,2);model.cursor='saved';model.pages=['','saved'];model.pageIndex=1;
 const camera=model.cam.state(),ids=model.scene.nodes.map(it=>it.d.id).sort();
 model.setViewMode('shared');
 assert.equal(model.viewMode,'shared');assert.equal(model.size,'followers','distance never overrides the size choice');
 assert.deepEqual(model.scene.nodes.map(it=>it.d.id).sort(),ids);
 assert.deepEqual(model.cam.state(),camera);assert.equal(model.cursor,'saved');assert.equal(model.nextCursor,'next');assert.equal(model.pageIndex,1);
 assert.equal(model.selected.id,1);
 for(const n of nodes){const placed=model.scene.get(n.id).d;assert.equal(placed.evidenceX,n.x);assert.equal(placed.evidenceY,n.y);}
 model.setViewMode('network');assert.equal(model.size,'followers');assert.deepEqual(model.cam.state(),camera);
});
test('invalid or retired saved modes return to My network',()=>{
 for(const viewMode of [undefined,'equal','audience','bogus']){
  const model=new MapModel({viewMode});assert.equal(model.viewMode,'network');assert.equal(model.size,'followers');
 }
 assert.equal(new MapModel({viewMode:'shared',size:'connections'}).size,'connections');
 assert.equal(new MapModel({viewMode:'fit'}).viewMode,'fit');
});
test('selecting a person immediately shows their recorded connections',()=>{
 const model=new MapModel({fetchJson:()=>new Promise(()=>{})});
 model.scene.apply({nodes,clusters:[]},0,{instant:true});
 model.select(nodes[0]);assert.equal(model.connectionsVisible,true);
 model.setConnectionsVisible(true);assert.equal(model.connectionsVisible,true);
 model.select(nodes[1]);assert.equal(model.connectionsVisible,true);
 model.setConnectionsVisible(true);model.deselect();assert.equal(model.connectionsVisible,false);
 model.setConnectionsVisible(true);assert.equal(model.connectionsVisible,false);
});
test('edge endpoints retain ids so lines follow portrait positions after repacking',()=>{
 const model=new MapModel();model.scene.apply({nodes,clusters:[]},0,{instant:true});
 const edges=model.readEdges(nodes[0],{edges:[{kind:'follow',source:1,target:2}]});
 assert.deepEqual([edges.lines[0].a.id,edges.lines[0].b.id],[1,2]);
});
test('clicking a portrait selects it without moving the camera',()=>{
 const {MapView}=require('../web/map-view.js');let selected;
 const view={hideTip(){},model:{select(n){selected=n;}},ensureVisible(){assert.fail('selection must not pan');}};
 MapView.prototype.activate.call(view,{kind:'n',it:{d:nodes[0]}});
 assert.equal(selected,nodes[0]);
});
test('explicit large-map search makes the selected portrait readable without reducing a closer view',()=>{
 const model=new MapModel({reduced:true,fetchJson:()=>new Promise(()=>{})});
 const person={...nodes[0],portraitRadius:.001};
 model.scene.apply({nodes:[person],clusters:[]},0,{instant:true});model.scene.large=true;
 model.cam.resize(1440,900);model.cam.set(.5,.5,1);
 model.select(person);assert.equal(model.cam.k,1,'ordinary selection leaves camera fixed');
 model.goTo(person);
 assert.ok(Math.abs(person.portraitRadius*model.cam.scale-16)<1e-9);
 assert.equal(model.cam.cx,person.x);assert.equal(model.cam.cy,person.y);
 model.cam.set(.5,.5,100);model.goTo(person);assert.equal(model.cam.k,100);
 model.scene.get(person.id).d.portraitRadius=1e-10;model.cam.set(.5,.5,1);
 model.goTo(person);assert.equal(model.cam.k,128,'tiny portraits respect maximum zoom');
});

test('portrait map accepts the bounded 3000-person option',()=>{
 const model=new MapModel({density:3000});assert.equal(model.budget,3000);
});
