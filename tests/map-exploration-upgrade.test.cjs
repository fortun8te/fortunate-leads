const test=require('node:test');
const assert=require('node:assert/strict');
const {MapModel}=require('../web/map-model.js');
const {cohortLayout,Scene,PortraitCache}=require('../web/map-core.js');
const radius=n=>Math.hypot(n.x-.5,n.y-.5);

test('weak and unknown evidence stays outside even when the page has no closer people',()=>{
 for(const count of [1,500,5000,20000]) {
  const people=Array.from({length:count},(_,i)=>({id:i+1,closeness:i%2?.03:null,followers:i%2?1:100000000,rank:1}));
  const nodes=cohortLayout(people,{id:0});
  assert.equal(nodes.length,count+1);
  assert.ok(nodes.slice(0,-1).every(n=>radius(n)-n.portraitRadius>=.425-1e-10));
 }
});
test('a famous disconnected profile cannot outrank or move inside a recorded connection',()=>{
 const people=[{id:1,closeness:.03,followers:100000000,rank:1},{id:2,closeness:.4,followers:100,rank:.1}];
 for(const size of ['followers','fit','connections','equal']) {
  const placed=cohortLayout(people,{id:0},size);
  assert.equal(placed[0].id,2);assert.ok(radius(placed[0])<radius(placed[1]));
  assert.ok(radius(placed[1])>=.425);
 }
});
test('all requested page sizes are accepted and retain the compact request bound',()=>{
 for(const density of [3000,5000,10000,20000]) {
  const model=new MapModel({density});assert.equal(model.density,density);
  assert.equal(model.params().get('compact'),'1');assert.ok(+model.params().get('budget')<=20000);
 }
});
test('distance and size controls stay independent and preserve selection and camera',()=>{
 const model=new MapModel({reduced:true,size:'equal'});
 model.apply({rev:1,total:2,nodes:[{id:1,x:.1,y:.2,closeness:.4,fit:80,source_count:3}],world:{me:{id:0}}},{});
 const selected=model.scene.get(1).d;model.select=()=>{};model.selected=selected;model.cam.set(.3,.6,2);
 const camera=model.cam.state();
 model.setViewMode('shared');assert.equal(model.size,'equal');assert.deepEqual(model.cam.state(),camera);assert.equal(model.selected.id,1);
 model.setViewMode('fit');assert.equal(model.size,'equal');assert.equal(model.selected.id,1);
 model.setSizeEncoding('followers');assert.equal(model.viewMode,'fit');assert.deepEqual(model.cam.state(),camera);
});
test('movement during an unfinished page load does not cancel and restart it',async()=>{
 let finish,requests=0,scheduled=0;
 const model=new MapModel({fetchJson:()=>{requests++;return new Promise(resolve=>finish=resolve);}});
 model.schedule=Object.assign(()=>scheduled++,{cancel(){}});
 const pending=model.load();model.pan(20,10);model.zoomBy(1.2,200,100);model.tick(16,.016);
 assert.equal(requests,1);assert.equal(scheduled,0);assert.equal(model.viewReq.controller.signal.aborted,false);
 finish({rev:1,total:0,nodes:[],world:{}});await pending;
});
test('moving the pointer during zoom anchors to its current world position',()=>{
 const model=new MapModel();model.cam.resize(1000,700);model.loaded={};
 model.zoomBy(2,100,100);model.tick(16,.016);
 const point=model.cam.toWorld(800,500);
 model.zoomBy(1.2,800,500);
 assert.deepEqual([model.goal.wx,model.goal.wy],point);
});
test('refresh retains the same scene item and eases new people instead of clearing photos',()=>{
 const model=new MapModel({now:()=>100});
 model.apply({rev:1,total:1,nodes:[{id:1,x:.3,y:.4,closeness:.4}],world:{}},{});
 model.scene.step(500,.5,false);const first=model.scene.get(1);
 model.apply({rev:2,total:2,nodes:[{id:1,x:.3,y:.4,closeness:.4},{id:2,x:.2,y:.7,closeness:.1}],world:{}},{});
 assert.equal(model.scene.get(1),first);assert.equal(first.a,1);assert.equal(model.scene.get(2).a,0);
 model.scene.step(116,.016,false);assert.ok(model.scene.get(2).a>0 && model.scene.get(2).a<1);
});
test('decoded photos reveal monotonically and reduced motion reveals immediately',()=>{
 let now=100;const images=[];
 const cache=new PortraitCache({now:()=>now,createImage:()=>{const image={naturalWidth:100};images.push(image);return image;}});
 cache.get('/img/1');images[0].onload();
 assert.equal(cache.reveal('/img/1',100),0);const half=cache.reveal('/img/1',240);assert.ok(half>.9&&half<1);
 assert.equal(cache.reveal('/img/1',380),1);assert.equal(cache.reveal('/img/1',100,true),1);
 assert.equal(cache.peek('/img/2'),null);assert.equal(images.length,1,'painting never queues unseen photos');
});
test('new viewport photos replace queued offscreen work without unbounded requests',()=>{
 const images=[];const cache=new PortraitCache({concurrency:1,createImage:()=>{const image={naturalWidth:100};images.push(image);return image;}});
 cache.prioritize(['/img/1','/img/2','/img/3']);assert.equal(images.length,1);
 cache.prioritize(['/img/9']);assert.equal(cache.entries.has('/img/2'),false);assert.equal(cache.entries.has('/img/3'),false);
 images[0].onload();assert.equal(images[1].src,'/img/9');assert.equal(cache.active,1);
});

test('twenty thousand people in one evidence band have real geometric clearance in every size mode',()=>{
 for(const size of ['followers','fit','connections','equal'])for(const closeness of [.8,.5,.3,.1,.03]) {
  const people=Array.from({length:20000},(_,i)=>({id:i+1,closeness,followers:i%3?1:100000000,fit:['strong','weak','unread'][i%3],source_count:i%19}));
  const nodes=cohortLayout(people,null,size),width=nodes.reduce((max,n)=>Math.max(max,n.portraitRadius*2),0),cells=new Map();
  assert.equal(nodes.length,20000);
  for(const n of nodes) {
   const x=Math.floor(n.x/width),y=Math.floor(n.y/width);
   for(let dy=-1;dy<=1;dy++)for(let dx=-1;dx<=1;dx++)for(const other of cells.get(`${x+dx}:${y+dy}`)||[])
    assert.ok(Math.hypot(n.x-other.x,n.y-other.y)+1e-12>=n.portraitRadius+other.portraitRadius,`${size}, evidence ${closeness}: ${n.id}/${other.id}`);
   const key=`${x}:${y}`;if(!cells.has(key))cells.set(key,[]);cells.get(key).push(n);
  }
 }
});
