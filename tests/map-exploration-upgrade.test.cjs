const test=require('node:test');
const assert=require('node:assert/strict');
const {MapModel}=require('../web/map-model.js');
const {cohortLayout,Scene,PortraitCache}=require('../web/map-core.js');
const radius=n=>Math.hypot(n.x-.5,n.y-.5);

test('unknown evidence remains unknown and ties fill a continuous organic disk',()=>{
 for(const count of [500,3000,20000]) {
  const people=Array.from({length:count},(_,i)=>({id:i+1,closeness:null,followers:1000,rank:1}));
  const nodes=cohortLayout(people,{id:0});
  assert.equal(nodes.length,count+1);assert.ok(nodes.slice(0,-1).every(n=>n.closeness===null));
  const distances=nodes.slice(0,-1).map(radius);
  assert.ok(distances.at(-1)-distances[0]>.3,'ties use the whole disk rather than a thin ring');
  assert.ok(distances.every((d,i)=>!i||d-distances[i-1]<.025),'no empty annular gaps');
 }
});
test('a famous disconnected profile cannot outrank or move inside a recorded connection',()=>{
 const people=[{id:1,closeness:.03,followers:100000000,rank:1},{id:2,closeness:.4,followers:100,rank:.1}];
 for(const size of ['followers','fit','connections','equal']) {
  const placed=cohortLayout(people,{id:0},size);
  assert.equal(placed[0].id,2);assert.ok(radius(placed[0])<radius(placed[1]));
 }
});
test('discrete network, shared and fit values never create rings or spiral steps at 3k and 20k',()=>{
 for(const count of [3000,20000])for(const view of ['network','shared','fit']) {
  const people=Array.from({length:count},(_,i)=>({id:i,closeness:i<count/2?.4:.03,source_count:i<count/2?8:1,fit:i<count/2?'strong':'unread',followers:1000}));
  const nodes=cohortLayout(people,null,'followers',view),distances=nodes.map(radius);
  assert.ok(distances.every((r,i)=>!i||r-distances[i-1]<.01),'values cannot produce a radial discontinuity');
  const steps=new Set(nodes.slice(1).map((n,i)=>{
    const step=Math.atan2(n.y-.5,n.x-.5)-Math.atan2(nodes[i].y-.5,nodes[i].x-.5);
    return Math.round(((step+Math.PI*4)%(Math.PI*2))*10);
  }));
  assert.ok(steps.size>50,'angles have organic variation rather than repeating a spiral');
  const taper=nodes.at(-1).portraitRadius/nodes[0].portraitRadius;
  assert.ok(taper>.75&&taper<.77,'the gradual taper spans the disk, including score ties');
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

test('twenty thousand people with tied evidence have real geometric clearance in every size mode',()=>{
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

test('show this page fits every loaded portrait inside desktop and phone insets',()=>{
 for(const [width,height] of [[1440,679],[390,640]])for(const count of [3000,20000]) {
  const model=new MapModel({reduced:true});model.cam.resize(width,height);model.cam.inset={top:30,right:0,bottom:70,left:0};
  model.scene.apply({nodes:cohortLayout(Array.from({length:count},(_,i)=>({id:i+1,closeness:.3,followers:1000})),{id:0})},0,{instant:true});
  model.cam.set(.5,.5,8);model.fit();
  for(const it of model.scene.nodes) {
   const [x,y]=model.cam.toScreen(it.x,it.y),r=(it.d.portraitRadius||0)*model.cam.scale;
   assert.ok(x-r>=20-1e-6&&x+r<=width-20+1e-6);assert.ok(y-r>=50-1e-6&&y+r<=height-90+1e-6);
  }
 }
});

test('the owner label does not carve a rectangular notch into world positions',()=>{
 const nodes=cohortLayout(Array.from({length:3000},(_,i)=>({id:i,followers:1000,closeness:.3})),{id:'owner'});
 const below=nodes.filter(n=>Math.abs(n.x-.5)<.045 && n.y>.565 && n.y<.60);
 assert.ok(below.length>10,'profiles continue immediately below the circular owner clearance');
});

test('fit during a morph includes destination portrait sizes and uses current positions after settling',()=>{
 for(const [width,height] of [[1440,679],[390,640]]) {
  const model=new MapModel();model.cam.resize(width,height);model.cam.inset={top:30,right:0,bottom:70,left:0};
  model.flyTo=target=>model.cam.set(target.cx,target.cy,target.k);
  model.scene.apply({nodes:[{id:1,x:.48,y:.48,portraitRadius:.005},{id:2,x:.52,y:.52,portraitRadius:.005}]},0,{instant:true});
  const destination=[{id:1,x:.08,y:.15,portraitRadius:.025},{id:2,x:.9,y:.91,portraitRadius:.055}];
  model.scene.apply({nodes:destination},100,{morph:260});
  assert.equal(model.scene.get(1).x,.48,'the fit request occurs before the portraits move');
  model.fit();const during=model.cam.state();
  const inside=nodes=>{
   for(const n of nodes) {
    const [x,y]=model.cam.toScreen(n.x,n.y),r=n.portraitRadius*model.cam.scale;
    assert.ok(x-r>=20-1e-6&&x+r<=width-20+1e-6,'complete destination portraits fit horizontally');
    assert.ok(y-r>=50-1e-6&&y+r<=height-90+1e-6,'complete destination portraits fit vertically');
   }
  };
  inside(destination);
  model.scene.step(500,.4,false);model.fit();assert.deepEqual(model.cam.state(),during,'finishing the morph does not invalidate its fit');
  // Temporary inspection drags keep their previous targets but change the
  // current world position. A settled fit must include what is now displayed.
  const dragged=model.scene.get(1);dragged.x=.7;dragged.y=.4;
  model.fit();assert.ok(Math.abs(model.cam.cx-.815)<1e-12);assert.ok(Math.abs(model.cam.cy-.67)<1e-12);
  inside(model.scene.nodes.map(it=>({x:it.x,y:it.y,portraitRadius:it.d.portraitRadius})));
 }
});
