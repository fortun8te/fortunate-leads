import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {webcrypto,createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../background.js',import.meta.url),'utf8');
function worker(data={},responses=[]) {
  const calls=[];
  const context=vm.createContext({Date,Number,Math,mem:{gen:0},FL:{controlAllows:()=>true},
    ControlPaused:class extends Error{},assertControl:async()=>{},
    tagged:async b=>({...b,lane_id:'turtles',account:{ig_id:'64189916997'}}),
    get:async k=>data[k],set:async v=>Object.assign(data,v),locked:async f=>f(),trail:async()=>{},sleep:async()=>{},
    api:async(path,body)=>{calls.push(body);const r=responses.shift();if(r instanceof Error)throw r;return r||{status:200,json:body.action==='release'?{released:true}:{granted:true,token:'t',expires_at:new Date(Date.now()+90000).toISOString()}};},
    chrome:{tabs:{get:async()=>({status:'complete'})}}});
  vm.runInContext(source.slice(source.indexOf('async function acquireSharedRequest('),source.indexOf('async function igRequest(')),context);
  return{context,data,calls};
}
test('a known finished request survives failed release and replays before new dispatch',async()=>{
  const w=worker({},[{status:200,json:{granted:true,token:'t',expires_at:new Date(Date.now()+90000).toISOString()}},{status:503,json:{}}]);
  await w.context.navigateWithPermit('list',async()=>({id:1}));
  assert.equal(w.data.sharedRequest?.phase,'completed');
  assert.equal(w.data.sharedRequest?.body.token,'t');
  assert.equal(await w.context.flushSharedRequest(),true);
  assert.equal(w.data.sharedRequest,null);
  assert.deepEqual(w.calls.map(c=>c.action),['acquire','release','release']);
});

test('worker restart retries only settled or never-sent completion, not unknown in-flight',async()=>{
  for(const phase of ['acquiring','started']) {
    const data={sharedRequest:{phase,body:{action:'release',token:'old',lane_id:'oldlane',account:{ig_id:'oldviewer'}},at:1}};
    const w=worker(data);let opened=false;
    assert.equal(await w.context.flushSharedRequest(),false);
    await assert.rejects(w.context.navigateWithPermit('list',async()=>{opened=true;}));
    assert.equal(opened,false);assert.equal(w.calls.length,0);assert.equal(data.sharedRequest.phase,phase);
  }
  for(const phase of ['not_sent','completed']) {
    const w=worker({sharedRequest:{phase,body:{action:'release',token:'old',lane_id:'oldlane',account:{ig_id:'oldviewer'}},at:1}});
    assert.equal(await w.context.flushSharedRequest(),true);
    assert.equal(w.calls[0].lane_id,'oldlane');assert.equal(w.calls[0].account.ig_id,'oldviewer');
    assert.equal(w.data.sharedRequest,null);
  }
});
test('lost or malformed acquire response stays unknown and is never blindly reacquired',async()=>{
  for(const response of [new Error('response lost'),{status:200,json:{}},{status:503,json:{}},{status:200,json:{granted:true,token:'t',expires_at:'invalid'}}]) {
    const w=worker({},[response]);let opened=false;
    await assert.rejects(w.context.navigateWithPermit('list',async()=>{opened=true;}));
    await assert.rejects(w.context.navigateWithPermit('list',async()=>{opened=true;}));
    assert.equal(opened,false);assert.equal(w.calls.length,1);assert.equal(w.data.sharedRequest.phase,'acquiring');
  }
});
test('release rejection never drops evidence and simultaneous flush is single-flight',async()=>{
  const w=worker({sharedRequest:{phase:'completed',body:{action:'release',token:'t'}}},[{status:200,json:{released:false}},{status:200,json:{released:true,replayed:true}}]);
  assert.equal(await w.context.flushSharedRequest(),false);assert.equal(w.data.sharedRequest.body.token,'t');
  assert.deepEqual(await Promise.all([w.context.flushSharedRequest(),w.context.flushSharedRequest()]),[true,true]);
  assert.equal(w.calls.length,2);assert.equal(w.data.sharedRequest,null);
});
test('an acquire marker exists before server I/O and a started marker before browser action',async()=>{
  const w=worker();const api=w.context.api;
  w.context.api=async(path,body)=>{if(body.action==='acquire')assert.equal(w.data.sharedRequest.phase,'acquiring');return api(path,body);};
  await w.context.navigateWithPermit('list',async()=>{assert.equal(w.data.sharedRequest.phase,'started');return{id:1};});
  assert.equal(w.data.sharedRequest,null);
});
test('ambiguous navigation rejection is held across restart, never released as not-sent',async()=>{
  const w=worker();await assert.rejects(w.context.navigateWithPermit('list',async()=>{throw Error('Chrome transport lost');}));
  assert.equal(w.data.sharedRequest.phase,'started');assert.equal(w.calls.length,1);
  const restarted=worker(w.data);assert.equal(await restarted.context.flushSharedRequest(),false);assert.equal(restarted.calls.length,0);
});

test('protective waits refresh the watchdog instead of reporting false loop stalls',async()=>{
  let now=1000;const mem={gen:0,looping:false,beat:0};const ControlPaused=class extends Error{};
  const context=vm.createContext({Date:class extends Date{static now(){return now;}},Math,mem,ControlPaused,Superseded:class extends Error{},LOOP_STALE:240000,
    booted:Promise.resolve(),trail:async()=>{},step:async()=>{now=9000;throw new ControlPaused();},
    sleep:async()=>{mem.gen++;},heartbeat:async()=>{},status:async()=>{},editSt:async()=>{}});
  vm.runInContext(source.slice(source.indexOf('async function loop()'),source.indexOf('// ---- profile page loads')),context);
  await context.loop();assert.equal(mem.beat,9000);
});

test('MAIN-world transport loss remains unknown even when it is not a timeout string',async()=>{
  for(const message of ['Chrome connection disconnected','Instagram tab did not respond in 45 s']) {
    const context=vm.createContext({Date,FL:{parseBody:()=>null},withTimeout:async(ms,p)=>p,
      chrome:{scripting:{executeScript:async()=>{throw Error(message);}}}});
    vm.runInContext(source.slice(source.indexOf('async function inTab('),source.indexOf('// One Instagram request.')),context);
    const result=await context.inTab(1,'https://www.instagram.com/api/list');
    assert.equal(result.uncertain,true);assert.equal(result.sent,false);
  }
});
test('a thrown or ambiguous fetch callback preserves started ownership',async()=>{
  for(const callback of [async()=>{throw Error('unexpected script failure');},async()=>({uncertain:true,timedOut:false,sent:false})]) {
    const w=worker();
    Object.assign(w.context,{Superseded:class extends Error{},inTab:callback});
    w.context.mem.gen=1;
    w.context.FL.laneBusy=()=>false;
    vm.runInContext(source.slice(source.indexOf('async function igRequest('),source.indexOf('const HOLD_MSG')),w.context);
    await assert.rejects(w.context.igRequest(1,{id:1},'https://www.instagram.com/api/list','list'));
    assert.equal(w.data.sharedRequest.phase,'started');assert.equal(w.calls.length,1);
    assert.equal(await w.context.flushSharedRequest(),false);
  }
});


test('operator review clears only the exact reviewed journal and preserves all other progress',async()=>{
  const at=Date.now()-10000,token='reviewed-token';
  const pending={phase:'started',at,body:{action:'release',token,lane_id:'turtles',account:{ig_id:'64189916997'}}};
  const marker={lane:'turtles',ig_id:'64189916997',attention_at:new Date(at+1000).toISOString(),at:new Date(at+2000).toISOString(),outcome:'operator_confirmed_stopped',token_ref:createHash('sha256').update(token).digest('hex').slice(0,16)};
  for(const change of [null,{lane:'other'},{ig_id:'999'},{at:new Date(at-1000).toISOString()},{attention_at:new Date(at-1000).toISOString()},{token_ref:'other'},{outcome:'unknown'}]) {
    const data={sharedRequest:structuredClone(pending),account:{ig_id:'64189916997'},laneId:'turtles',st:{accountIgId:'64189916997'},box:[{saved:'page'}],prog:{cursor:'keep'},localPaused:true};
    const w=worker(data);Object.assign(w.context,{crypto:webcrypto,TextEncoder,Uint8Array});
    w.context.api=async()=>({status:200,json:{instagram_request_review:change===null?marker:{...marker,...change}}});
    if(change===null){assert.equal((await w.context.reviewSharedRequest()).cleared,true);assert.equal(data.sharedRequest,null);}
    else{await assert.rejects(w.context.reviewSharedRequest());assert.equal(data.sharedRequest.phase,'started');}
    assert.equal(data.localPaused,true);assert.equal(data.prog.cursor,'keep');assert.equal(data.box.length,1);
  }
});
test('operator review cannot clear a new security hold or changed account while waiting',async()=>{
  const at=Date.now()-10000;
  const w=worker({sharedRequest:{phase:'acquiring',at,body:{lane_id:'turtles',account:{ig_id:'64189916997'}}},account:{ig_id:'64189916997'},laneId:'turtles',st:{accountIgId:'64189916997'}});
  w.context.api=async()=>{w.data.st.hold={manualReview:true};return{status:200,json:{instagram_request_review:{lane:'turtles',ig_id:'64189916997',attention_at:new Date(at+1000).toISOString(),at:new Date(at+2000).toISOString(),outcome:'operator_confirmed_stopped'}}};};
  await assert.rejects(w.context.reviewSharedRequest());assert.equal(w.data.sharedRequest.phase,'acquiring');assert.equal(w.data.st.hold.manualReview,true);
});

test('local resume respects unknown requests and current warnings, while excluded historical warnings stay harmless',async()=>{
  for(const block of ['none','request','warning','journal','manualReview']) {
    const data={localPaused:true,st:{},...(block==='journal'?{sharedRequest:{phase:'started',body:{token:'t'}}}:{})};
    if(block==='manualReview')data.st.hold={manualReview:true};
    const w=worker(data);let listener,resumed=0;
    Object.assign(w.context,{editSt:async fn=>fn(data.st),status:async()=>{},loop:()=>{resumed++;}});
    w.context.FL.succeeded=()=>{};
    w.context.chrome.runtime={onMessage:{addListener:fn=>{listener=fn;}}};
    w.context.api=async()=>({status:200,json:{instagram_request_attention:{kind:'scraping_warning',message:'Old excluded warning'},collection:{unconfirmed:block==='request'},collection_blockers:[{blocking:block==='warning'}]}});
    vm.runInContext(source.slice(source.indexOf('chrome.runtime.onMessage.addListener('),source.indexOf('// ---- lifecycle')),w.context);
    const reply=await new Promise(resolve=>listener({cmd:'resume'},{},resolve));
    assert.equal(reply.ok,block==='none');assert.equal(data.localPaused,block!=='none');assert.equal(resumed,block==='none'?1:0);
  }
});


test('runtime speed change completes without a nested storage lock or clearing protection',async()=>{
  const data={account:{ig_id:'64189916997'},laneId:'turtles',st:{accountIgId:'64189916997',hold:{manualReview:true}},localPaused:true,sharedRequest:{phase:'started',body:{token:'keep'}}};
  const w=worker(data);let listener;let chain=Promise.resolve();
  w.context.locked=fn=>{const promise=chain.then(fn);chain=promise.catch(()=>{});return promise;};
  w.context.trail=async()=>w.context.locked(async()=>{});
  w.context.FL.collectionSpeedSetting=(account,lane,value)=>({ig_id:account.ig_id,lane_id:lane,poll_wait_ms:value});
  w.context.chrome.runtime={onMessage:{addListener:fn=>{listener=fn;}}};
  vm.runInContext(source.slice(source.indexOf('chrome.runtime.onMessage.addListener('),source.indexOf('// ---- lifecycle')),w.context);
  const reply=await Promise.race([new Promise(resolve=>listener({cmd:'set_collection_speed',poll_wait_ms:10000},{},resolve)),new Promise((_,reject)=>setTimeout(()=>reject(Error('storage lock deadlocked')),200))]);
  assert.equal(reply.ok,true);assert.equal(data.collectionSpeed.poll_wait_ms,10000);
  assert.equal(data.localPaused,true);assert.equal(data.st.hold.manualReview,true);assert.equal(data.sharedRequest.body.token,'keep');
});


test('late completion after gate expiry keeps original ownership time for explicit manual review',async()=>{
  let now=1000;const token='late-finished';
  const w=worker({account:{ig_id:'64189916997'},laneId:'turtles',st:{accountIgId:'64189916997'},localPaused:true,prog:{cursor:'keep'}});
  w.context.Date=class extends Date{static now(){return now;}};
  Object.assign(w.context,{crypto:webcrypto,TextEncoder,Uint8Array});
  w.context.api=async(path,body)=>body?.action==='acquire'?{status:200,json:{granted:true,token,expires_at:new Date(91000).toISOString()}}:body?.action==='release'?{status:503,json:{}}:{status:200,json:{instagram_request_review:{lane:'turtles',ig_id:'64189916997',attention_at:new Date(92000).toISOString(),at:new Date(120000).toISOString(),outcome:'operator_confirmed_stopped',token_ref:createHash('sha256').update(token).digest('hex').slice(0,16)}}};
  // assertControl is mocked; localPaused must not interfere with grant admission.
  w.data.localPaused=false;
  await w.context.navigateWithPermit('list',async()=>{now=100000;return{id:1};});
  assert.equal(w.data.sharedRequest.at,1000);assert.equal(w.data.sharedRequest.completed_at,100000);
  w.data.localPaused=true;
  assert.equal((await w.context.reviewSharedRequest()).cleared,true);
  assert.equal(w.data.sharedRequest,null);assert.equal(w.data.localPaused,true);assert.equal(w.data.prog.cursor,'keep');
});
test('a delayed unused grant preserves acquire time so exact expired-request review can finish',async()=>{
  let now=1000;const token='late-unused';
  const w=worker({account:{ig_id:'64189916997'},laneId:'turtles',st:{accountIgId:'64189916997'}});
  w.context.Date=class extends Date{static now(){return now;}};
  Object.assign(w.context,{crypto:webcrypto,TextEncoder,Uint8Array});
  w.context.api=async(path,body)=>{
    if(body?.action==='acquire'){now=100000;return{status:200,json:{granted:true,token,expires_at:new Date(91000).toISOString()}};}
    if(body?.action==='release')return{status:200,json:{released:false}};
    return{status:200,json:{instagram_request_review:{lane:'turtles',ig_id:'64189916997',attention_at:new Date(92000).toISOString(),at:new Date(120000).toISOString(),outcome:'operator_confirmed_stopped',token_ref:createHash('sha256').update(token).digest('hex').slice(0,16)}}};
  };
  let opened=false;await assert.rejects(w.context.navigateWithPermit('list',async()=>{opened=true;}));
  assert.equal(opened,false);assert.equal(w.data.sharedRequest.at,1000);assert.equal(w.data.sharedRequest.completed_at,100000);
  assert.equal((await w.context.reviewSharedRequest()).cleared,true);
});
