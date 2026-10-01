const test=require('node:test');
const assert=require('node:assert/strict');
const {UniverseMapModel}=require('../web/map-universe-model.js');
const manifest={available:true,version:'test',node_count:200000,anchor:{person_id:1,handle:'owner'}};
function fixture(fetchJson){
 let engine;const model=new UniverseMapModel({reduced:true,fetchJson,engineFactory:opts=>(engine={opts,updates:0,paused:false,disposed:false,
  async mount(){return true;},async updateCamera(){this.updates++;},pause(on){this.paused=on;},dispose(){this.disposed=true;},
  normalize(x,y){return{x,y};},async locate(id){return {id:+id,x:.2+(+id)/100,y:.4,portraitRadius:.002,handle:'person'+id};}
 })});model.setSize(1000,800);return{model,get engine(){return engine;}};
}
test('available universe bypasses cohorts and reuses local camera',async()=>{
 const calls=[];const f=fixture(async url=>{calls.push(url);assert.equal(url,'/api/map/universe/manifest');return manifest;});
 await f.model.load();assert.ok(f.model.universe);assert.equal(f.model.scene.nodes.length,0);assert.equal(f.model.total,200000);
 f.model.cam.set(.6,.4,4);await f.model.load();assert.equal(f.engine.updates,2);assert.deepEqual(calls,['/api/map/universe/manifest']);
 f.model.pause(true);assert.equal(f.engine.paused,true);f.model.pause(false);assert.equal(f.engine.paused,false);f.model.dispose();assert.equal(f.engine.disposed,true);
});
test('absent universe retains the paged 500-person fallback',async()=>{
 const calls=[];const f=fixture(async url=>{calls.push(url);if(url.includes('/manifest'))return{available:false};assert.ok(url.startsWith('/api/map/view?'));return{ready:true,nodes:[],total:0,world:{}};});
 await f.model.load();assert.equal(f.model.universe,null);assert.equal(f.model.density,500);assert.equal(calls.length,2);assert.ok(calls[1].includes('budget=500'));assert.ok(calls[1].includes('cohort=1'));
});
test('click selection keeps the camera and search locates at readable zoom',async()=>{
 const f=fixture(async url=>url.includes('/manifest')?manifest:{edges:[],nodes:[]});await f.model.load();
 const camera=f.model.cam.state();f.model.select({id:2,x:.22,y:.4,portraitRadius:.002});assert.deepEqual(f.model.cam.state(),camera);
 await f.model.goTo({id:3,x:999,y:999});assert.equal(f.model.selected.x,.23);assert.equal(f.model.cam.cx,.23);assert.ok(f.model.cam.k>1);
 f.model.dispose();
});
test('universe connection endpoints are resolved and never use legacy coordinates',async()=>{
 const f=fixture(async url=>url.includes('/manifest')?manifest:url.includes('/locate?')?{nodes:[{id:2,x:.22,y:.4},{id:3,x:.23,y:.4}]}:{edges:[{source:2,target:3,kind:'follows'}],nodes:[{id:3,x:999,y:999}]});await f.model.load();
 f.model.selected={id:2,x:.22,y:.4};await f.model.loadEdges(f.model.selected);assert.equal(f.model.edges.state,'ready');assert.equal(f.model.edges.lines[0].b.x,.23);assert.equal(f.model.edges.lines[0].a.x,.22);f.model.dispose();
});
test('unsupported controls cannot mutate full-map settings or fetch cohorts',async()=>{
 const f=fixture(async url=>{assert.equal(url,'/api/map/universe/manifest');return manifest;});await f.model.load();f.model.setFilters({status:'client'});f.model.setDensity(50000);f.model.setViewMode('shared');assert.equal(f.model.status,'');assert.equal(f.model.density,500);assert.equal(f.model.viewMode,'network');f.model.dispose();
});
test('manifest pending never starts legacy JSON loads, and hiding cancels activation',async()=>{
 let resolve;const calls=[];const f=fixture((url,{signal}={})=>{calls.push(url);assert.equal(url,'/api/map/universe/manifest');return new Promise(done=>{resolve=done;});});
 const first=f.model.load(),second=f.model.load();assert.deepEqual(calls,['/api/map/universe/manifest']);f.model.pause(true);resolve(manifest);await Promise.all([first,second]);assert.equal(f.model.universe,null);assert.equal(f.model.universeChecked,false);
});
test('full-map search can exceed legacy zoom without changing the fallback limit',async()=>{
 const {Camera}=require('../web/map-core.js');const legacy=new Camera();legacy.set(.5,.5,9999);assert.equal(legacy.k,128);
 const f=fixture(async url=>url.includes('/manifest')?manifest:{edges:[],nodes:[]});await f.model.load();f.engine.locate=async id=>({id,x:.6,y:.6,portraitRadius:.000001});await f.model.goTo({id:9});assert.ok(f.model.cam.k>128);assert.ok(f.model.cam.k<=16384);f.model.dispose();
});
test('a cancelled manifest probe restarts when the map is shown again',async()=>{
 let resolve,calls=0;const f=fixture(url=>{assert.equal(url,'/api/map/universe/manifest');calls++;return calls===1?new Promise(done=>{resolve=done;}):Promise.resolve(manifest);});
 const loading=f.model.load();f.model.pause(true);f.model.pause(false);resolve(manifest);await loading;assert.equal(calls,2);assert.ok(f.model.universe);f.model.dispose();
});
test('hide/show during GPU mounting disposes only the cancelled engine',async()=>{
 let mounted,release;const started=new Promise(done=>{mounted=done;});const engines=[];
 const model=new UniverseMapModel({fetchJson:async()=>manifest,engineFactory:()=>{
  const index=engines.length,engine={disposed:0,pause(){},dispose(){this.disposed++;},updateCamera:async()=>{},mount:async()=>{if(index===0){mounted();return new Promise(done=>{release=done;});}return true;}};engines.push(engine);return engine;
 }});
 const loading=model.load();await started;model.pause(true);model.pause(false);release(true);await loading;
 assert.equal(engines.length,2);assert.equal(engines[0].disposed,1);assert.equal(engines[1].disposed,0);assert.equal(model.universe,engines[1]);model.dispose();
});
test('fresh universe opens the inner neighborhood and selections never reset it',async()=>{
 const f=fixture(async url=>url.includes('/manifest')?manifest:{edges:[],nodes:[]});await f.model.load();assert.deepEqual(f.model.cam.state(),{cx:.5,cy:.5,k:4});
 const start=f.model.cam.state();f.model.select({id:2,x:.22,y:.4,portraitRadius:.002});assert.deepEqual(f.model.cam.state(),start);
 f.model.cam.set(.7,.3,8);await f.model.load();assert.deepEqual(f.model.cam.state(),{cx:.7,cy:.3,k:8});f.model.dispose();
});
test('an existing camera survives universe startup',async()=>{
 const f=fixture(async()=>manifest);f.model.cam.set(.6,.4,2);await f.model.load();assert.deepEqual(f.model.cam.state(),{cx:.6,cy:.4,k:2});f.model.dispose();
});
test('failed GPU startup restores the original fallback camera',async()=>{
 const model=new UniverseMapModel({fetchJson:async url=>url.includes('/manifest')?manifest:{nodes:[],total:0,world:{}},engineFactory:()=>({mount:async()=>false,dispose(){}})});
 await model.load();assert.equal(model.universe,null);assert.deepEqual(model.cam.state(),{cx:.5,cy:.5,k:1});model.dispose();
});

test('initial neighborhood framing stays readable as the outer graph grows',async()=>{
 const near={...manifest,anchor:{...manifest.anchor,x:0,y:0},bounds:[-10000,-10000,10000,10000],initial_camera:{radius:400}};
 const f=fixture(async()=>near);await f.model.load();
 assert.equal(f.model.cam.k,27.5);
 assert.equal(f.model.cam.scale/22000,0.9);
 f.model.dispose();
});
