const test=require('node:test');
const assert=require('node:assert/strict');
const {performance}=require('node:perf_hooks');
const {cohortLayout,radiusFor,RIM_SCALE,rimScale}=require('../web/map-core.js');
const owner={id:'owner',x:.5,y:.5};
const people=count=>Array.from({length:count},(_,i)=>({id:i,x:.2,y:.3,closeness:1-i/count,rank:1-i/count,followers:i%9===0?null:10**(i%7),source_count:i%30,fit:['unread','weak','good','strong'][i%4]}));
function checkPacking(nodes,count){
 assert.equal(nodes.length,count+1);
 assert.deepEqual([nodes.at(-1).x,nodes.at(-1).y],[.5,.5]);
 const members=nodes.slice(0,-1);
 for(let i=0;i<members.length;i++) {
  const n=members[i],distance=Math.hypot(n.x-.5,n.y-.5);
  assert.ok(Number.isFinite(n.portraitRadius)&&n.portraitRadius>0);
  assert.ok(distance+n.portraitRadius<=.46000001,'bubble stays inside disk');
  assert.ok(distance-n.portraitRadius>=.05999999,'owner has clear space');
  if(i)assert.ok(distance+1e-12>=Math.hypot(members[i-1].x-.5,members[i-1].y-.5),'evidence order preserved');
  for(let j=0;j<i;j++)assert.ok(Math.hypot(n.x-members[j].x,n.y-members[j].y)+1e-12>=n.portraitRadius+members[j].portraitRadius,'portraits do not overlap');
 }
}
for(const count of [1,250,500,1000,3000])test(`variable portraits pack ${count} people deterministically within bounded time`,()=>{
 const source=people(count),start=performance.now(),layout=cohortLayout(source,owner,'followers'),elapsed=performance.now()-start;
 assert.ok(elapsed<1500,`packing took ${elapsed.toFixed(1)}ms`);
 checkPacking(layout,count);
 assert.deepEqual(layout,cohortLayout(source,owner,'followers'));
 assert.ok(source.every(n=>n.x===.2&&n.y===.3),'source coordinates remain unchanged');
 assert.ok(layout.slice(0,-1).every(n=>n.portraitRadius<=.055),'all portrait sizes remain bounded');
});
test('repacking size modes preserves membership and original evidence',()=>{
 const first=cohortLayout(people(500),owner,'followers');
 for(const mode of ['fit','connections','equal']){
  const next=cohortLayout(first,owner,mode);
  checkPacking(next,500);
  assert.deepEqual(next.map(n=>n.id),first.map(n=>n.id));
  assert.ok(next.slice(0,-1).every(n=>n.evidenceX===.2&&n.evidenceY===.3));
  if(mode==='equal')assert.equal(new Set(next.slice(0,-1).map(n=>n.portraitRadius)).size,1);
 }
});
test('highly uneven follower distributions still include every person',()=>{
 const source=people(1000).map((n,i)=>({...n,followers:i<500?0:1e12}));
 checkPacking(cohortLayout(source,owner),1000);
});

test('default pages keep an organic dense circle with a gradual outward taper',()=>{
 for(const count of [500,1000,3000]){
  const nodes=cohortLayout(people(count).map(n=>({...n,followers:1000})),owner).slice(0,-1);
  const occupied=nodes.reduce((sum,n)=>sum+n.portraitRadius**2,0)/(.46**2-.06**2);
  assert.ok(occupied>.23,'packed bubbles occupy the circle rather than sparse points');
  // The evidence-order taper is unchanged; the rim fall-off is a separate factor on top of it.
  const rim=n=>rimScale(Math.hypot(n.x-.5,n.y-.5)/.46);
  const taper=(nodes.at(-1).portraitRadius/rim(nodes.at(-1)))/(nodes[0].portraitRadius/rim(nodes[0]));
  assert.ok(taper>.74);
  assert.ok(taper<.79);
  assert.ok(nodes.every((n,i)=>!i||n.portraitRadius<=nodes[i-1].portraitRadius),'equal follower counts shrink smoothly outward');
  assert.ok(new Set(nodes.map(n=>n.x.toFixed(6))).size>count*.98,'positions do not form grid columns');
 }
});
test('all size controls pack 3000 people without overlap',()=>{
 for(const size of ['fit','connections','equal'])checkPacking(cohortLayout(people(3000),owner,size),3000);
});

test('rim scale: a smoothstep from the start share to the configured floor, flat before it',()=>{
 assert.ok(Object.isFrozen(RIM_SCALE));
 assert.equal(rimScale(0),1);assert.equal(rimScale(RIM_SCALE.from),1);assert.equal(rimScale(1),RIM_SCALE.floor);
 assert.equal(rimScale(1.5),RIM_SCALE.floor,'beyond the rim the floor holds');
 let previous=1;
 for(let i=0;i<=100;i++){const v=rimScale(i/100);assert.ok(v<=previous+1e-12,'never grows outward');assert.ok(v>=RIM_SCALE.floor-1e-12);previous=v;}
 const eps=1e-3,start=RIM_SCALE.from;
 assert.ok(1-rimScale(start+eps)<1e-5,'eases in from the start without a visible kink');
 const middle=rimScale((start+1)/2);assert.ok(Math.abs(middle-(1+RIM_SCALE.floor)/2)<1e-9,'halfway is halfway');
});
test('bubbles shrink toward the rim in every view mode and size choice except Equal',()=>{
 for(const count of [500,1000,3000])for(const view of ['network','shared','fit'])for(const size of ['followers','fit','connections']){
  const nodes=cohortLayout(people(count).map(n=>({...n,followers:1000,source_count:5,fit:'good'})),owner,size,view).slice(0,-1);
  const dist=n=>Math.hypot(n.x-.5,n.y-.5);
  // Dividing out the evidence-order taper leaves the rim factor alone.
  const base=nodes.map((n,i)=>n.portraitRadius/(1-.24*Math.pow(i/Math.max(1,nodes.length-1),.8)));
  for(let i=0;i<nodes.length;i++)assert.ok(Math.abs(base[i]/base[0]-rimScale(dist(nodes[i])/.46))<1e-9,`${count} ${view} ${size} #${i}`);
  assert.ok(base.at(-1)/base[0]>=RIM_SCALE.floor-.01&&base.at(-1)/base[0]<RIM_SCALE.floor+.12,'the outermost bubbles reach about the floor');
  assert.ok(nodes.every(n=>dist(n)<=.46+1e-9),'the circle keeps its reach');
  assert.ok(Math.max(...nodes.map(n=>dist(n)+n.portraitRadius))>.40,'and still extends close to the rim');
 }
});
test('Equal keeps every bubble exactly the same size, rim included',()=>{
 for(const view of ['network','shared','fit']){
  const nodes=cohortLayout(people(1000),owner,'equal',view).slice(0,-1);
  assert.equal(new Set(nodes.map(n=>n.portraitRadius)).size,1);checkPacking([...nodes,{x:.5,y:.5}],1000);
 }
});
test('an individual follower-based size survives the rim scale: more followers still means a bigger bubble at the same distance band',()=>{
 const nodes=cohortLayout(people(1000).map((n,i)=>({...n,followers:i%2?10:10000000})),owner,'followers').slice(0,-1);
 const outer=nodes.filter(n=>Math.hypot(n.x-.5,n.y-.5)>.38),big=outer.filter(n=>n.followers>1e6),small=outer.filter(n=>n.followers<100);
 const mean=list=>list.reduce((s,n)=>s+n.portraitRadius,0)/list.length;
 assert.ok(big.length>20&&small.length>20);assert.ok(mean(big)>mean(small)*1.2);
});
