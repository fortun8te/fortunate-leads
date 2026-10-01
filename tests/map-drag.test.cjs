const test = require('node:test');
const assert = require('node:assert/strict');
const {MapModel} = require('../web/map-model.js');
const person = (id) => ({id,x:.4,y:.5,handle:'person'+id,fit:'good'});

test('portrait inspection drag leaves evidence and camera unchanged, then settles home', () => {
 const {MapView}=require('../web/map-view.js');
 const model=new MapModel(); model.world.me={id:99};
 model.scene.apply({nodes:[person(1),person(99)],clusters:[]},0,{instant:true});
 const view=Object.assign(Object.create(MapView.prototype),{model,invalidate(){}});
 const it=model.scene.get(1), initial={...it.d}, camera=[model.cam.cx,model.cam.cy,model.cam.k];
 assert.equal(view.startNodeDrag({kind:'n',it:model.scene.get(99)}),false);
 assert.equal(view.startNodeDrag({kind:'n',it}),true);
 view.moveNodeDrag(60,-30);
 assert.equal(it.x,initial.x+60/model.cam.scale);assert.equal(it.y,initial.y-30/model.cam.scale);
 assert.deepEqual(it.d,initial);assert.deepEqual([model.cam.cx,model.cam.cy,model.cam.k],camera);
 view.finishNodeDrag(); assert.equal(it.dur,320);
 model.scene.step(it.t0+321,.321,false);
 assert.equal(it.x,initial.x);assert.equal(it.y,initial.y);assert.equal(it.dur,0);
});
test('cancelled or reduced-motion portrait drag restores immediately without requests', () => {
 const {MapView}=require('../web/map-view.js');
 for(const reduced of [true,false]) {
  const model=new MapModel({reduced,fetchJson:()=>assert.fail('drag must not request data')});
  model.scene.apply({nodes:[person(1)],clusters:[]},0,{instant:true});
  const view=Object.assign(Object.create(MapView.prototype),{model,invalidate(){}}),it=model.scene.get(1);
  view.startNodeDrag({kind:'n',it});view.moveNodeDrag(80,40);view.finishNodeDrag(!reduced);
  assert.equal(it.x,it.tx);assert.equal(it.y,it.ty);assert.equal(it.dur,0);assert.equal(view.nodeDrag,null);
 }
});

function interactiveView() {
 const fs=require('node:fs'),vm=require('node:vm');
 const handlers={};
 const element={addEventListener(){},classList:{add(){},remove(){}},style:{},focus(){},setPointerCapture(){},getBoundingClientRect(){return {left:0,top:0};}};
 const canvas={...element,addEventListener(name,fn){handlers[name]=fn;}};
 const context={MapCore:require('../web/map-core.js'),MapModel:require('../web/map-model.js'),document:{addEventListener(){},documentElement:{}},performance,MutationObserver:class{observe(){}},addEventListener(){}};
 vm.runInNewContext(fs.readFileSync(require.resolve('../web/map-view.js'),'utf8'),context);
 const refs=new Proxy({filters:new Proxy({},{get:()=>element})},{get:(o,k)=>o[k]||element});
 const model=new MapModel({reduced:false});model.world.me={id:99};
 model.scene.apply({nodes:[person(1),person(99)],clusters:[]},0,{instant:true});
 let pans=0,activations=0,moved=0;
 model.pan=()=>pans++;model.moved=()=>moved++;
 const view=Object.assign(Object.create(context.MapViewModule.MapView.prototype),{canvas,r:refs,model,invalidate(){},hideTip(){},activate(){activations++;},pick(){return this.hit;}});
 view.bind();
 const event=(x,y,extra={})=>({pointerId:1,pointerType:'mouse',button:0,clientX:x,clientY:y,...extra});
 return {view,model,send:(name,x,y,extra)=>handlers[name](event(x,y,extra)),counts:()=>({pans,activations,moved})};
}
test('pointer gestures distinguish clicks, portrait dragging, empty-space pan, and cancellation',()=>{
 const {view,model,send,counts}=interactiveView(),it=model.scene.get(1);
 view.hit={kind:'n',it};
 send('pointerdown',100,100);send('pointermove',102,100);send('pointerup',102,100);
 assert.equal(counts().activations,1);assert.equal(it.x,it.tx);
 send('pointerdown',100,100);send('pointermove',160,120);
 assert.notEqual(it.x,it.tx);assert.equal(counts().pans,0);
 send('pointerup',160,120);assert.equal(counts().activations,1);assert.equal(it.dur,320);
 model.scene.step(it.t0+321,.321,false);
 view.hit=null;send('pointerdown',100,100);send('pointermove',160,120);send('pointerup',160,120);
 assert.equal(counts().pans,1);
 view.hit={kind:'n',it};send('pointerdown',100,100);send('pointermove',160,120);send('pointercancel',160,120);
 assert.equal(it.x,it.tx);assert.equal(it.y,it.ty);assert.equal(view.nodeDrag,null);
});
test('second touch cancels portrait displacement before pinch, and owner never drags independently',()=>{
 const {view,model,send,counts}=interactiveView(),it=model.scene.get(1);
 view.hit={kind:'n',it};send('pointerdown',100,100,{pointerType:'touch'});send('pointermove',140,120,{pointerType:'touch'});
 send('pointerdown',200,200,{pointerType:'touch',pointerId:2});
 assert.equal(it.x,it.tx);assert.equal(view.nodeDrag,null);
 send('pointercancel',200,200,{pointerId:2});
 const owner=model.scene.get(99);view.hit={kind:'n',it:owner};
 send('pointerdown',100,100);send('pointermove',160,120);send('pointerup',160,120);
 assert.equal(owner.x,owner.tx);assert.equal(counts().pans,1);
});
