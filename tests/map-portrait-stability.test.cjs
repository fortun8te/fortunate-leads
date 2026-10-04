const test=require('node:test');
const assert=require('node:assert/strict');
const {PortraitCache}=require('../web/map-core.js');
function fixture(max=3) {
 let clock=0;const images=[];
 const cache=new PortraitCache({max,concurrency:1,now:()=>clock,createImage:()=>{
  const image={naturalWidth:64,naturalHeight:64};images.push(image);return image;
 }});
 return {cache,images,time:v=>clock=v,finish(){while(cache.active)images.at(-1).onload();}};
}
test('paint reads never enqueue another portrait request',()=>{
 const {cache,images}=fixture();
 assert.equal(typeof cache.peek,'function');
 for(let i=0;i<60;i++)cache.peek('/img/1');
 assert.equal(images.length,0);assert.equal(cache.entries.size,0);
});
test('an oversized visible set stays admitted across frames instead of evicting ready faces',()=>{
 const f=fixture(),urls=['/img/1','/img/2','/img/3','/img/4'];
 assert.equal(typeof f.cache.prioritize,'function');
 f.cache.prioritize(urls);f.finish();
 const first=f.cache.peek('/img/1'),loads=f.images.length;
 for(let frame=0;frame<30;frame++) {f.cache.prioritize(urls);for(const url of urls)f.cache.peek(url);f.finish();}
 assert.equal(f.cache.peek('/img/1'),first);assert.equal(f.images.length,loads);
 assert.equal(f.cache.entries.size,3);assert.equal(loads,3);
});
test('ready portraits ease in once and retain their reveal time on repeated reads',()=>{
 const f=fixture();f.cache.get('/img/1');f.finish();
 assert.equal(typeof f.cache.reveal,'function');
 assert.equal(f.cache.reveal('/img/1',0),0);
 const half=f.cache.reveal('/img/1',140);assert.ok(half>.5 && half<1);
 for(let i=0;i<20;i++)f.cache.get('/img/1');
 assert.equal(f.cache.reveal('/img/1',280),1);assert.equal(f.images.length,1);
});
