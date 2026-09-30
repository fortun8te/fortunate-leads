const test = require('node:test');
const assert = require('node:assert/strict');
const { PortraitCache } = require('../web/map-core.js');
function fixture(options = {}) {
  const images = []; let changed = 0;
  const cache = new PortraitCache({createImage: () => {const image = {naturalWidth: 100};images.push(image);return image;}, changed: () => changed++, ...options});
  return {cache, images, changed: () => changed};
}
test('portraits only load saved local photos and reuse ready images', () => {
  const {cache, images, changed} = fixture();
  assert.equal(cache.get('https://instagram.com/photo.jpg'), null);
  assert.equal(cache.get('data:image/png;base64,x'), null);
  assert.equal(cache.get('/img/1'), null); assert.equal(images.length, 1);
  images[0].onload();
  assert.equal(cache.get('/img/1'), images[0]); assert.equal(changed(), 1);
});
test('portrait queue, decoded cache and concurrent requests stay bounded', () => {
  const {cache, images} = fixture({max: 12, concurrency: 3});
  for (let i=0;i<1000;i++) cache.get('/img/'+i);
  assert.equal(cache.entries.size, 12); assert.equal(cache.active, 3);
  assert.equal(cache.queue.length, 9); assert.equal(images.length, 3);
  images[0].onload(); assert.equal(cache.active, 3); assert.equal(images.length, 4);
});
test('failed saved photos stay fallback and do not retry every frame', () => {
  const {cache, images} = fixture(); cache.get('/img/5'); images[0].onerror();
  for(let i=0;i<20;i++) assert.equal(cache.get('/img/5'), null);
  assert.equal(images.length, 1); assert.equal(cache.active, 0);
});
test('map model accepts cached photo paths and strips external URLs', () => {
  const { cleanNode } = require('../web/map-model.js');
  const person = {id: 1,x:.5,y:.5,fit:80};
  assert.equal(cleanNode({...person,pic:'/img/1'}).pic, '/img/1');
  assert.equal(cleanNode({...person,pic:'https://cdn.example.com/a.jpg'}).pic, null);
  assert.equal(cleanNode({...person,pic:'/img/../../secret'}).pic, null);
});
test('decoded portraits are closed when the bounded cache evicts them', async () => {
  let closed = 0;
  const {cache, images} = fixture({max: 1, normalize: () => ({width:128,height:128,close: () => closed++})});
  cache.get('/img/1'); images[0].onload(); await Promise.resolve(); await Promise.resolve();
  assert.equal(cache.get('/img/1').width, 128);
  cache.get('/img/2'); assert.equal(closed, 1);
});

test('decoding failure releases a slot and preserves fallback', async () => {
  const {cache,images} = fixture({concurrency:1,normalize: () => {throw new Error('decode');}});
  cache.get('/img/1'); cache.get('/img/2'); images[0].onload();
  await Promise.resolve(); await Promise.resolve();
  assert.equal(cache.entries.get('/img/1').state,'failed');
  assert.equal(images.length,2);
});
test('following filters and balanced overview are explicit request parameters', () => {
  const {MapModel} = require('../web/map-model.js');
  const model = new MapModel({fetchJson: async () => ({rev:1,nodes:[],clusters:[]})});
  const rect = {x0:0,y0:0,x1:1,y1:1};
  assert.equal(model.params(rect).get('cohort'),'1');
  model.follow = 'not_following';
  assert.equal(model.params(rect).get('follow'),'not_following');
  assert.equal(model.filtersActive,1);
  model.mode = 'fit'; assert.equal(model.params(rect).has('cohort'),true);
});
test('unprepared fallback cannot claim an unsupported following filter', async () => {
  const {MapModel} = require('../web/map-model.js'); let requests=0;
  const model = new MapModel({fetchJson: async () => {requests++;return {ready:false,nodes:[],layout:{building:false}};}});
  model.follow='not_following'; await model.load();
  assert.equal(requests,1); assert.equal(model.phase,'unprepared'); assert.equal(model.pending,0);
});

test('size encoding changes presentation without requesting or moving people', () => {
  const {MapModel}=require('../web/map-model.js');let requests=0;
  const model=new MapModel({fetchJson:()=>{requests++;}}),before=model.cam.state();
  model.setSizeEncoding('connections');assert.equal(model.size,'connections');
  assert.equal(requests,0);assert.deepEqual(model.cam.state(),before);
  model.setSizeEncoding('invalid');assert.equal(model.size,'connections');
});
test('drag interrupts a held flight and prevents delayed location recentering', () => {
  const {MapModel}=require('../web/map-model.js');
  const model=new MapModel({fetchJson:async()=>({nodes:[],clusters:[]})});model.setSize(1000,700);
  const view=model.viewReq.begin(),locate=model.locateReq.begin();model.hold=true;model.pending=1;model.flight={};
  const before=model.cam.cx;model.pan(80,0);
  assert.equal(model.hold,false);assert.equal(model.flight,null);assert.equal(model.pending,0);
  assert.equal(view.live(),false);assert.equal(locate.live(),false);assert.notEqual(model.cam.cx,before);
  model.pause(true);
});
test('drag release caps fling speed so the map remains controllable', () => {
  const {MapModel}=require('../web/map-model.js');const model=new MapModel();
  model.release(5000,5000);assert.ok(Math.hypot(model.vel.vx,model.vel.vy)<=700.001);
});
test('dense photo bodies win over a neighboring expanded click target', () => {
  const {MapView}=require('../web/map-view.js');
  const left={x:0,y:0,r:15,it:{d:{id:1}}},right={x:24.5,y:0,r:8,it:{d:{id:2}}};
  const hit=MapView.prototype.pick.call({touch:false,displayMarks:{nodes:[left,right],groups:[]}},17,0);
  assert.equal(hit.it.d.id,2);
});
test('changing display mode retains selected person and camera without relocation requests', () => {
  const {MapModel}=require('../web/map-model.js');let requests=0;
  const model=new MapModel({fetchJson:()=>requests++,reduced:true});model.setSize(1000,700);
  model.selected={id:1,handle:'test',x:.5,y:.5};model.cam.set(.3,.6,2);
  const camera=model.cam.state();model.setMode('fit');
  assert.equal(requests,0);assert.deepEqual(model.cam.state(),camera);assert.equal(model.selected.id,1);
});
