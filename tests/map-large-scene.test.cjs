const test=require('node:test');
const assert=require('node:assert/strict');
const {performance}=require('node:perf_hooks');
const {cohortLayout,Scene,Camera,displayPlan}=require('../web/map-core.js');
const {MapModel}=require('../web/map-model.js');
const people=n=>Array.from({length:n},(_,i)=>({id:i+1,x:.5,y:.5,closeness:1-i/n,rank:1-i/n,followers:10**(i%7),source_count:i%30}));
let scene,layout;
test('200k real records lay out and index within a bounded build without spread or candidate explosion',()=>{
 const start=performance.now();layout=cohortLayout(people(200000),{id:0});scene=new Scene();scene.apply({nodes:layout},0,{instant:true});
 assert.equal(scene.nodes.length,200001);assert.equal(new Set(scene.nodes.map(it=>it.d.id)).size,200001);
 assert.ok(performance.now()-start<5000);assert.ok(scene.spatial);assert.equal(scene.large,true);
 assert.equal(scene.step(100,.016,false),false);
 const cam=new Camera();cam.resize(1440,900);
 const overview=displayPlan(scene,cam,[],null,null,0);
 assert.equal(overview.large,true);assert.equal(overview.vector,false);assert.equal(overview.nodes.length,0);
 const target=layout[100000];cam.set(target.x,target.y,64);
 const close=displayPlan(scene,cam,[],null,null,0);
 assert.equal(close.vector,true);assert.ok(close.nodes.some(mark=>mark.it.d.id===target.id));assert.ok(close.nodes.length<=1000);
 const hits=scene.spatial.query({x0:target.x-.00001,y0:target.y-.00001,x1:target.x+.00001,y1:target.y+.00001});
 assert.ok(hits.some(it=>it.d.id===target.id));assert.ok(hits.length<100);
});
test('overview raster contains every person once and is reused while panning',()=>{
 const fs=require('node:fs'),vm=require('node:vm');let arcs=0,blits=0;
 const draw={beginPath(){},moveTo(){},arc(){arcs++;},fill(){}};
 const context={MapCore:require('../web/map-core.js'),MapModel:require('../web/map-model.js'),performance,document:{createElement(){return {getContext:()=>draw};}}};
 vm.runInNewContext(fs.readFileSync(require.resolve('../web/map-view.js'),'utf8'),context);
 const view={model:{scene,cam:new Camera(),world:{me:{id:0}}},stats:{}};
 const ctx={drawImage(){blits++;}};
 context.MapViewModule.MapView.prototype.paintLargeOverview.call(view,ctx,{fg3:'#888'},{vector:false});
 assert.equal(arcs,200000);assert.equal(view.overviewRaster.canvas.width,2048);
 view.model.cam.panBy(100,50);
 context.MapViewModule.MapView.prototype.paintLargeOverview.call(view,ctx,{fg3:'#888'},{vector:false});
 assert.equal(arcs,200000);assert.equal(blits,2);
});
test('compact chunks accumulate once, report progress, preserve IDs and stop exactly at the requested count',async()=>{
 let requests=0;const counts=[];
 const model=new MapModel({reduced:true,fetchJson:async url=>{
  requests++;const q=new URL('http://local'+url).searchParams,start=Number(q.get('after')||0),budget=Number(q.get('budget'));
  assert.equal(q.get('compact'),'1');assert.ok(budget<=20000);
  const end=Math.min(50000,start+budget);
  return {compact:true,rev:'stable',total:50000,world:{me:{id:0}},rows:Array.from({length:end-start},(_,i)=>[start+i+1,.5,.5,1,50,.8,100,0,2,1,1,0]),nodes:[],next_cursor:end<50000?String(end):null};
 }});
 model.density=50000;model.on(event=>{if(event==='busy'&&model.loadingCount)counts.push(model.loadingCount);});
 await model.load();assert.equal(requests,3);assert.equal(model.scene.nodes.length,50001);assert.equal(model.stats.applied,1);
 assert.ok(counts.includes(20000)&&counts.includes(40000)&&counts.includes(50000));
 assert.equal(model.scene.get(30000).d.pic,'/img/30000');assert.equal(model.scene.get(30000).d.follows_me,true);assert.equal(model.cache.limit,1);
});
test('snapshot revision change fails without applying mixed data or retrying endlessly',async()=>{
 let calls=0;const model=new MapModel({fetchJson:async()=>({compact:true,rev:++calls,total:30000,rows:[[calls,.5,.5,1,50,.8,100,0,0,1,0,0]],nodes:[],next_cursor:String(calls)})});
 model.density=50000;await model.load();assert.equal(calls,2);assert.equal(model.stats.applied,0);assert.equal(model.phase,'error');
});
test('aborting a compact snapshot stops subsequent chunks',async()=>{
 let calls=0;const model=new MapModel({fetchJson:async()=>{calls++;return {compact:true,rev:1,total:50000,rows:[[1,.5,.5,1,50,.8,100,0,0,1,0,0]],nodes:[],next_cursor:'next'};}});
 model.density=50000;model.on(event=>{if(event==='busy'&&model.loadingCount)model.pause(true);});await model.load();assert.equal(calls,1);assert.equal(model.stats.applied,0);
});
test('large circles have deterministic organic angles without changing distance order or overlap',()=>{
 const count=10000,source=people(count),nodes=cohortLayout(source,null),again=cohortLayout(source,null);
 assert.deepEqual(nodes,again);
 const cell=.58/Math.sqrt(count),cells=new Map();let previous=0;
 for(const n of nodes){
  const distance=Math.hypot(n.x-.5,n.y-.5);assert.ok(distance>=previous-1e-12);previous=distance;
  const x=Math.floor(n.x/cell),y=Math.floor(n.y/cell);
  for(let dy=-1;dy<=1;dy++)for(let dx=-1;dx<=1;dx++)for(const other of cells.get((x+dx)+':'+(y+dy))||[])
   assert.ok(Math.hypot(n.x-other.x,n.y-other.y)>=n.portraitRadius+other.portraitRadius);
  const key=x+':'+y;if(!cells.has(key))cells.set(key,[]);cells.get(key).push(n);
 }
});
test('saved supported density restores while invalid preferences keep default500',()=>{
 assert.equal(new MapModel({density:'200000'}).density,200000);
 for(const density of [undefined,null,'junk',-1,10000000])assert.equal(new MapModel({density}).density,500);
});
