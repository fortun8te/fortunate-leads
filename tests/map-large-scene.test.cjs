const test=require('node:test');
const assert=require('node:assert/strict');
const {performance}=require('node:perf_hooks');
const {cohortLayout,Scene,Camera,displayPlan}=require('../web/map-core.js');
const {MapModel}=require('../web/map-model.js');
const people=n=>Array.from({length:n},(_,i)=>({id:i+1,x:.5,y:.5,closeness:1-i/n,rank:1-i/n,followers:10**(i%7),source_count:i%30}));
let scene,layout;
test('200k real records lay out and index within a bounded build without spread or candidate explosion',()=>{
 const start=performance.now(),cpuStart=process.cpuUsage();layout=cohortLayout(people(200000),{id:0});scene=new Scene();scene.apply({nodes:layout},0,{instant:true});
 assert.equal(scene.nodes.length,200001);assert.equal(new Set(scene.nodes.map(it=>it.d.id)).size,200001);
 // The combined suite runs beside browser and server tests. Keep the 5s CPU
 // budget (isolated measurement 1.5–1.8s) without counting another process's work.
 const cpu=process.cpuUsage(cpuStart);
 assert.ok((cpu.user+cpu.system)/1000<5000,'layout and index CPU remains bounded');
 assert.ok(performance.now()-start<15000,'wall-time guard still catches stalls');
 assert.ok(scene.spatial);assert.equal(scene.large,true);
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
test('overview retains ready photos after bitmap eviction and never paints a grey placeholder wall',()=>{
 const fs=require('node:fs'),vm=require('node:vm');let arcs=0,blits=0,stamps=0;
 const draw={save(){},restore(){},beginPath(){},arc(){arcs++;},clip(){},drawImage(){stamps++;}};
 const context={MapCore:require('../web/map-core.js'),MapModel:require('../web/map-model.js'),performance,document:{createElement(){return {getContext:()=>draw};}}};
 vm.runInNewContext(fs.readFileSync(require.resolve('../web/map-view.js'),'utf8'),context);
 const sample=layout.slice(0,3).map(n=>({...n,pic:'/img/'+n.id}));
 const photoScene=new Scene();photoScene.apply({nodes:[...sample,...layout.slice(3).map(n=>({...n,pic:'/img/'+n.id}))]},0,{instant:true});
 const entries=new Map(sample.map(n=>[n.pic,{url:n.pic,state:'ready',readyAt:0,image:{width:64,height:64}}]));
 const view=Object.assign(Object.create(context.MapViewModule.MapView.prototype),{model:{scene:photoScene,cam:new Camera(),world:{me:{id:0}},reduced:true},portraits:{entries},stats:{}});
 const ctx={drawImage(){blits++;}};
 view.paintLargeOverview(ctx,{fg3:'#888'},{vector:false});
 assert.equal(arcs,3,'only real decoded portraits are painted');assert.equal(stamps,3);
 assert.equal(view.overviewRaster.complete.size,3);assert.equal(view.overviewRaster.nodes.size,200000);
 assert.equal(view.overviewRaster.canvas.width,2048);
 entries.clear();view.model.cam.panBy(100,50);
 view.paintLargeOverview(ctx,{fg3:'#888'},{vector:false});
 assert.equal(arcs,3);assert.equal(stamps,3);assert.equal(blits,2,'cached pixels survive decoding-cache eviction and pan');
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

test('shared photo URLs keep separate people and absent photos retain faint selectable marks through repacking',()=>{
 const fs=require('node:fs'),vm=require('node:vm');let photos=0,missing=0;
 const draw={save(){},restore(){},beginPath(){},arc(){},clip(){},fill(){missing++;},drawImage(){photos++;},globalAlpha:1};
 const context={MapCore:require('../web/map-core.js'),MapModel:require('../web/map-model.js'),performance,document:{createElement(){return {getContext:()=>draw};}}};
 vm.runInNewContext(fs.readFileSync(require.resolve('../web/map-view.js'),'utf8'),context);
 const scene=new Scene(),nodes=[{id:1,pic:'/img/1',x:.2,y:.2,portraitRadius:.02},{id:2,pic:'/img/1',x:.4,y:.4,portraitRadius:.01},{id:3,pic:null,x:.7,y:.7,portraitRadius:.015}];
 scene.apply({nodes},0,{instant:true});
 const entries=new Map([['/img/1',{url:'/img/1',state:'ready',readyAt:0,image:{width:64,height:64}}]]);
 const view=Object.assign(Object.create(context.MapViewModule.MapView.prototype),{model:{scene,cam:new Camera(),world:{},reduced:true},portraits:{entries},stats:{}});
 view.paintLargeOverview({drawImage(){}},{fg3:'#888'},{});
 assert.equal(photos,2);assert.equal(missing,1);assert.equal(view.overviewRaster.complete.size,3);
 assert.equal(view.overviewRaster.byPic.get('/img/1').length,2);
 assert.ok(scene.get(1)&&scene.get(2)&&scene.get(3),'all loaded identities remain selectable');
 entries.clear();scene.apply({nodes:nodes.map(n=>({...n,x:n.x+.1}))},0,{instant:true});
 view.paintLargeOverview({drawImage(){}},{fg3:'#888'},{});
 assert.equal(photos,5,'each old rendered individual transfers to its new position without another photo request');
 assert.equal(view.overviewRaster.complete.size,3);assert.equal(missing,1,'missing mark also transfers once');
});

test('an incoming bitmap cannot be evicted before stamping, and a detached older bitmap recovers',()=>{
 const fs=require('node:fs'),vm=require('node:vm');let closes=0,wakes=0;
 const draw={save(){},restore(){},beginPath(){},arc(){},clip(){},fill(){},drawImage(source){assert.ok(source.width>0,'never draw a detached bitmap');},globalAlpha:1};
 const context={MapCore:require('../web/map-core.js'),MapModel:require('../web/map-model.js'),performance,document:{createElement(){return {getContext:()=>draw};},documentElement:{dataset:{theme:'light'}}},getComputedStyle(){return {getPropertyValue(){return '';}};}};
 vm.runInNewContext(fs.readFileSync(require.resolve('../web/map-view.js'),'utf8'),context);
 const {PortraitCache}=require('../web/map-core.js'),requests=[];
 const cache=new PortraitCache({max:2,concurrency:1,createImage:()=>{const image={};requests.push(image);return image;}});
 const bitmap={width:64,height:64,close(){closes++;this.width=this.height=0;}},entry={url:'/img/1',state:'ready',readyAt:0,image:bitmap};
 cache.entries.set(entry.url,entry);
 const scene=new Scene();scene.apply({nodes:[{id:1,pic:'/img/1',x:.3,y:.3,portraitRadius:.01}]},0,{instant:true});
 const view=Object.assign(Object.create(context.MapViewModule.MapView.prototype),{model:{scene,cam:new Camera(),world:{},reduced:true},portraits:cache,stats:{},ctx:draw,invalidate(){wakes++;}});
 view.prepareOverview();assert.equal(entry.retained,true);
 cache.get('/img/2');cache.get('/img/3');
 assert.equal(closes,0,'cache pressure cannot detach a pending raster image');assert.equal(cache.entries.get('/img/1'),entry);
 const atlas=view.overviewRaster;view.readPalette();assert.equal(view.overviewRaster,atlas,'theme changes preserve both pixels and pending image holds');
 view.paintLargeOverview({drawImage(){}},{fg3:'#888'},{});assert.equal(entry.retained,false);assert.equal(atlas.complete.size,1);
 cache.get('/img/3');assert.equal(closes,1,'bitmap becomes evictable once its pixels are retained');
 // Reproduce the original failure from an older unprotected cache entry.
 scene.apply({nodes:[{id:1,pic:'/img/1',x:.3,y:.3,portraitRadius:.01}]},0,{instant:true});
 view.overviewRaster=null;cache.entries.clear();cache.queue=[];cache.active=0;
 cache.entries.set(entry.url,entry);view.prepareOverview();
 assert.doesNotThrow(()=>view.paintLargeOverview({drawImage(){}},{fg3:'#888'},{}));
 assert.equal(cache.entries.has('/img/1'),false);assert.equal(view.overviewRaster.cursor,0);assert.ok(wakes>0);
 view.queuePortraits([],null);cache.get('/img/1');assert.equal(requests.at(-1).src,'/img/1','expired image is eligible for a fresh local request');
});
test('a small page does not replace a completed large raster while departed people fade out',()=>{
 const scene=new Scene();scene.apply({nodes:Array.from({length:6000},(_,i)=>({id:i,x:.3,y:.3}))},0,{instant:true});
 assert.equal(scene.large,true);
 scene.apply({nodes:Array.from({length:1000},(_,i)=>({id:i,x:.3,y:.3}))},1);
 assert.equal(scene.nodes.length,6000,'departed people can finish their fade');assert.equal(scene.large,false,'render mode follows the currently loaded page');
});
