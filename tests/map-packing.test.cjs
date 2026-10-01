const test=require('node:test');
const assert=require('node:assert/strict');
const {performance}=require('node:perf_hooks');
const {cohortLayout,radiusFor}=require('../web/map-core.js');
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
 const scale=layout[0].portraitRadius/radiusFor(source[0],1,'followers');
 for(const [i,n] of layout.slice(0,-1).entries())assert.ok(Math.abs(n.portraitRadius-radiusFor(n,1,'followers')*scale*(1-.24*Math.pow(i/Math.max(1,count-1),.8)))<1e-12,'modest follower sizing and gradual outward taper preserved');
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
  assert.ok(occupied>.4,'packed bubbles occupy the circle rather than sparse points');
  assert.ok(nodes.at(-1).portraitRadius/nodes[0].portraitRadius>.74);
  assert.ok(nodes.at(-1).portraitRadius/nodes[0].portraitRadius<.78);
  assert.ok(nodes.every((n,i)=>!i||n.portraitRadius<=nodes[i-1].portraitRadius),'equal follower counts shrink smoothly outward');
  assert.ok(new Set(nodes.map(n=>n.x.toFixed(6))).size>count*.98,'positions do not form grid columns');
 }
});
test('all size controls pack 3000 people without overlap',()=>{
 for(const size of ['fit','connections','equal'])checkPacking(cohortLayout(people(3000),owner,size),3000);
});
