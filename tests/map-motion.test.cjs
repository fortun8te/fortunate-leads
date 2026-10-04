const test=require('node:test');
const assert=require('node:assert/strict');
const {Scene,Flight,Camera,cohortLayout,displayPlan,easeOut,easeInOut}=require('../web/map-core.js');
const response=(x,r)=>({nodes:[{id:1,x,y:.5,portraitRadius:r}],clusters:[]});
test('scene motion moves promptly, eases heavily, and morphs radius without modifying the response',()=>{
 const scene=new Scene();scene.apply(response(.2,.01),0,{instant:true});
 const target=response(.8,.02);scene.apply(target,0,{morph:200});
 assert.equal(scene.get(1).x,.2);assert.equal(scene.get(1).d.portraitRadius,.01);
 scene.step(100,.1,false);
 assert.ok(scene.get(1).x>.75,'most movement completes in the first half');
 assert.ok(scene.get(1).d.portraitRadius>.019);
 assert.equal(target.nodes[0].portraitRadius,.02,'target response remains immutable');
 const current=scene.get(1).x,currentRadius=scene.get(1).d.portraitRadius;
 scene.apply(response(.4,.006),100,{morph:200});
 assert.equal(scene.get(1).x,current);assert.equal(scene.get(1).d.portraitRadius,currentRadius);
 scene.step(300,.2,false);assert.equal(scene.get(1).x,.4);assert.equal(scene.get(1).d.portraitRadius,.006);
 assert.equal(scene.animating,false);
});
test('radius-only repacking animates and reduced motion settles immediately',()=>{
 const scene=new Scene();scene.apply(response(.2,.01),0,{instant:true});
 scene.apply(response(.2,.02),0,{morph:200});scene.step(50,.05,false);
 assert.ok(scene.get(1).d.portraitRadius>.01&&scene.get(1).d.portraitRadius<.02);
 scene.step(51,.001,true);assert.equal(scene.get(1).d.portraitRadius,.02);assert.equal(scene.animating,false);
});
test('camera flight eases into and out of a bounded move',()=>{
 const flight=new Flight({cx:.2,cy:.2,k:1},{cx:.8,cy:.6,k:4},0,200);
 assert.ok(flight.at(20).cx<.201);assert.equal(flight.at(100).k,2);
 assert.ok(flight.at(180).cx>.799);assert.equal(flight.at(200).done,true);
 for(const easing of [easeOut,easeInOut]){
  assert.equal(easing(-1),0);assert.equal(easing(2),1);
  for(let i=1;i<=100;i++)assert.ok(easing(i/100)>=easing((i-1)/100));
 }
});
test('the 3000-person page retains every identity behind the bounded photo raster',()=>{
 const cam=new Camera();cam.resize(1400,1000);const scene=new Scene();
 const nodes=cohortLayout(Array.from({length:3000},(_,i)=>({id:i+1,closeness:1-i/3000,followers:1000})),{id:0});
 scene.apply({nodes,clusters:[]},0,{instant:true});
 assert.equal(scene.large,true);
 const plan=displayPlan(scene,cam,[],null,null,0);
 assert.equal(plan.marks.length,3001);assert.ok(plan.nodes.length<=1000);
 assert.equal(scene.spatial.query({x0:0,y0:0,x1:1,y1:1}).length,3001);
});
test('large scene spatial queries follow moving portraits',()=>{
 const scene=new Scene();const nodes=Array.from({length:5001},(_,i)=>({id:i,x:.1,y:.1,portraitRadius:.001}));
 scene.apply({nodes,clusters:[]},0,{instant:true});
 scene.apply({nodes:nodes.map(n=>({...n,x:.8,y:.8})),clusters:[]},0,{morph:200});
 scene.step(200,.2,false);
 assert.equal(scene.spatial.query({x0:.79,x1:.81,y0:.79,y1:.81}).length,5001);
 assert.equal(scene.spatial.query({x0:.09,x1:.11,y0:.09,y1:.11}).length,0);
});
