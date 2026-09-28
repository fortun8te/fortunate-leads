import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import {webcrypto} from 'node:crypto';
import FL from '../lib/core.js';
import B from '../lib/benchmark.js';
const task = {task_id:'task-1', route:'web_rest', transport:'chrome', target_id:'42', viewer_id:'12', direction:'following', page_size:100, cursor:null};
function injected({viewer='12', response, fail=false}={}) {
  const calls=[];
  const context=vm.createContext({performance, AbortController, setTimeout, clearTimeout,
    document:{cookie:`ds_user_id=${viewer}; csrftoken=csrf`, readyState:'complete'},
    location:{hostname:'www.instagram.com',pathname:'/'}, sessionStorage:{getItem:()=>null,setItem(){}},
    window:{__flFetch:async (...args)=>{calls.push(args); if(fail)throw Error('network'); return response || {status:200,type:'basic',url:B.url(task,'12'),headers:{get:()=>null},text:async()=>JSON.stringify({status:'ok',users:[{pk:'8',username:'person'}],next_max_id:'next'})};}}});
  vm.runInContext(`globalThis.run=${B.fetchOnce.toString()}`,context);
  return {context,calls};
}
test('only validated numeric seed and matching viewer produce exact REST URLs',()=>{
  assert.equal(B.url(task,'12'),'https://www.instagram.com/api/v1/friendships/42/following/?count=100');
  assert.match(B.url({...task,cursor:'a&b',page_size:200},'12'),/max_id=a%26b$/);
  for(const change of [{target_id:'seed'},{viewer_id:'99'},{page_size:25},{route:'mobile_rest'},{direction:'profile'},{transport:'mobile'}]) assert.throws(()=>B.url({...task,...change},'12'));
});
test('MAIN dispatch is exactly one manual redirect fetch with existing headers',async()=>{
  const {context,calls}=injected(); const res=await context.run(B.url(task,'12'),'12',30000);
  assert.equal(calls.length,1); assert.equal(res.actual_http_requests,1); assert.equal(calls[0][1].redirect,'manual');
  assert.equal(calls[0][1].headers['X-IG-App-ID'],'936619743392459');
  const result=B.result(task,res,FL); assert.equal(result.status,'ok'); assert.equal(result.rows[0].ig_id,'8'); assert.equal(result.next_cursor,'next');assert.equal(result.has_more,true);assert.equal(result.reported_has_more,null);
});
test('identity mismatch blocks before any GET',async()=>{
  const {context,calls}=injected({viewer:'99'}); const res=await context.run(B.url(task,'12'),'12',30000);
  assert.equal(calls.length,0); assert.equal(res.actual_http_requests,0); assert.equal(res.error,'identity_or_tab_blocked');
});
test('manual redirect and transport errors stop without fallback',async()=>{
  const redirect={status:0,type:'opaqueredirect',headers:{get:()=>null},text:async()=>''};
  for(const opts of [{response:redirect},{fail:true}]){
    const {context,calls}=injected(opts); const res=await context.run(B.url(task,'12'),'12',30000);
    assert.equal(calls.length,1); assert.equal(res.actual_http_requests,opts.fail?null:1); assert.equal(res.fetch_dispatches,1); assert.notEqual(B.result(task,res,FL).status,'ok');
  }
});
function worker() {
  const data={account:{ig_id:'12',handle:'viewer'},laneId:'lane-1',st:FL.fresh()}; const calls=[];
  const context=vm.createContext({FL, FLBenchmark:B, Date, Set, URLSearchParams, AbortController,TextEncoder,Uint8Array,crypto:{randomUUID:()=> 'request-1',subtle:webcrypto.subtle},importScripts(){},setTimeout:()=>0,clearTimeout(){},
    chrome:{cookies:{get:async ({name})=>({value:name==='sessionid'?'test-session-secret':'12'})},runtime:{getManifest:()=>({version:'3.9.24'}),onMessage:{addListener(){}}},
      storage:{local:{get:async k=> typeof k==='string'?{[k]:data[k]}:Object.fromEntries(k.map(key=>[key,data[key]])),set:async x=>Object.assign(data,x)}},
      tabs:{query:async()=>[{id:3,status:'complete',url:'https://www.instagram.com/'}]},
      scripting:{executeScript:async()=>{calls.push('GET');return [{result:{status:200,text:JSON.stringify({status:'ok',users:[{pk:'8',username:'person'}]}),actual_http_requests:1,duration_ms:8,observed_viewer_id:'12'}}];}}}});
  vm.runInContext(fs.readFileSync(new URL('../background.js',import.meta.url),'utf8').split('// ---- lifecycle')[0],context);
  context.calls=calls;context.testTask=task;
  vm.runInContext(`heartbeat=async()=>{}; globalThis.run=()=>benchmarkStep(mem.gen); globalThis.flush=()=>flushBenchmark();
    globalThis.configure=(mode)=>{api=async(path,body)=>{calls.push({path,body});
      if(path.startsWith('/api/benchmark/next'))return {status:200,json:{enabled:true,task:testTask}};
      if(path==='/api/benchmark/permit')return {status:200,json:{granted:mode!=='blocked',token:'token-1',request_id:body.request_id,expires_at:new Date(Date.now()+90000).toISOString()}};
      if(path==='/api/benchmark/result')return mode==='offline'?{status:503,json:{}}:{status:200,json:{ok:true,acknowledged:mode!=='badack',request_id:body.request_id}};
      throw Error('unexpected API');};};`,context);
  return {context,data,calls};
}
test('denied permit makes no GET and retains stable request ID',async()=>{
  const {context,data,calls}=worker();context.configure('blocked');await context.run();await context.run();
  assert.equal(calls.filter(x=>x==='GET').length,0);assert.equal(data.benchmarkTask.request_id,'request-1');
});
test('persisted outcome replays identical bytes until explicit matching acknowledgement',async()=>{
  const {context,data,calls}=worker();context.configure('offline');await context.run();
  const original=JSON.stringify(data.benchmarkPending.result);assert.equal(calls.filter(x=>x==='GET').length,1);
  assert.equal(await context.flush(),false);assert.equal(JSON.stringify(data.benchmarkPending.result),original);
  context.configure('badack');assert.equal(await context.flush(),false);assert.ok(data.benchmarkPending);
  context.configure('ok');assert.equal(await context.flush(),true);assert.equal(data.benchmarkPending,null);
  assert.equal(calls.filter(x=>x==='GET').length,1);assert.equal(data.st.today.list,1);
});
test('worker death after permit is uncertain, never redispatched',async()=>{
  const {context,data,calls}=worker();data.benchmarkPending={body:{request_id:'old',task_id:'task-1',token:'token-1'}};
  context.configure('offline');await context.flush();assert.equal(data.benchmarkPending.result.uncertain,true);
  assert.equal(data.benchmarkPending.result.actual_http_requests,null);assert.equal(calls.filter(x=>x==='GET').length,0);
});
test('existing pacing and cooldown prevent permit and fetch',async()=>{
  for(const setup of [s=>{s.nextAt=Date.now()+60000},s=>{s.cool.list.until=Date.now()+60000},s=>{s.rlog=Array.from({length:72},()=>[Date.now(),'list'])}]){
    const {context,data,calls}=worker();setup(data.st);context.configure('ok');await context.run();
    assert.equal(calls.filter(x=>x==='GET').length,0);assert.equal(calls.filter(x=>x.path==='/api/benchmark/permit').length,0);
  }
});
test('reserved or stopped benchmark never falls through to ordinary collection',async()=>{
  for(const next of [{enabled:true,task:null},{enabled:true,stopped:true,task},{enabled:false,reserved:true,task:null},{enabled:false,reserved:true,task}]){
    const {context,calls}=worker();context.nextState=next;
    vm.runInContext(`api=async()=>({status:200,json:nextState}); nextJob=async()=>{throw Error('ordinary job must not start')};
      lookupViaPage=async()=>{throw Error('seed lookup must not start')}; pickTab=async()=>{throw Error('navigation must not start')};
      globalThis.fullStep=()=>step(mem.gen);`,context);
    assert.ok(await context.fullStep()>0);assert.equal(calls.filter(x=>x==='GET').length,0);
  }
});
test('unacknowledged benchmark outcome blocks all next-task and ordinary upstream work',async()=>{
  const {context,data,calls}=worker();context.configure('offline');await context.run();calls.length=0;
  vm.runInContext(`globalThis.fullStep=()=>step(mem.gen)`,context);await context.fullStep();
  assert.equal(calls.length,1);assert.equal(calls[0].path,'/api/benchmark/result');assert.ok(data.benchmarkPending);
});
test('raw benchmark does not call seed lookup or browser navigation',async()=>{
  const {context,calls}=worker();context.configure('ok');
  vm.runInContext(`lookupViaPage=async()=>{throw Error('unexpected seed lookup')}; pickTab=async()=>{throw Error('unexpected navigation')};`,context);
  await context.run();assert.equal(calls.filter(x=>x==='GET').length,1);
});

test('expired or missing permit deadline records zero sends and stops before MAIN dispatch',async()=>{
  for(const expires of [null,new Date(Date.now()+10000).toISOString()]){
    const {context,data,calls}=worker();context.configure('offline');context.expires=expires;
    vm.runInContext(`const oldApi=api;api=async(path,body)=>{const r=await oldApi(path,body);if(path==='/api/benchmark/permit')r.json.expires_at=expires;return r;}`,context);
    await context.run();assert.equal(calls.filter(x=>x==='GET').length,0);
    assert.equal(data.benchmarkPending.result.actual_http_requests,0);assert.equal(data.benchmarkPending.result.terminal_warning,'permit_expired');
  }
});

test('raw REST malformed rows or contradictory end flag stop instead of silently dropping evidence',()=>{
  for(const json of [{status:'ok',users:[{pk:'8',username:'person'},{}]}, {status:'ok',users:[{pk:'8',username:'person'}],has_more:false,next_max_id:'tail'}]){
    const result=B.result(task,{status:200,text:JSON.stringify(json),actual_http_requests:1},FL);
    assert.notEqual(result.status,'ok');assert.ok(result.terminal_warning);
  }
});

test('session fingerprint contains only a stable digest, never raw cookies',async()=>{
  const {context,data,calls}=worker();context.configure('offline');await context.run();
  const permit=calls.find(x=>x.path==='/api/benchmark/permit');assert.match(permit.body.fingerprints.session,/^[0-9a-f]{64}$/);
  assert.equal(JSON.stringify({data,calls}).includes('test-session-secret'),false);
  const saved=permit.body.fingerprints.session;assert.equal(data.benchmarkPending.result.fingerprints.session,saved);
});
test('same-viewer session replacement after permit sends zero upstream requests',async()=>{
  const {context,data,calls}=worker();context.configure('offline');let reads=0;
  context.chrome.cookies.get=async ({name})=>({value:name==='sessionid'?(++reads===1?'first-session':'replacement-session'):'12'});
  await context.run();assert.equal(calls.filter(x=>x==='GET').length,0);
  assert.equal(data.benchmarkPending.result.actual_http_requests,0);assert.equal(data.benchmarkPending.result.terminal_warning,'session_fingerprint_changed');
});
test('missing or mismatched session cookies block before permit',async()=>{
  for(const cookie of [null,{value:'wrong-viewer'}]){
    const {context,calls}=worker();context.configure('ok');context.chrome.cookies.get=async()=>cookie;await context.run();
    assert.equal(calls.filter(x=>x.path==='/api/benchmark/permit').length,0);assert.equal(calls.filter(x=>x==='GET').length,0);
  }
});

test('follower endpoint hold allows following but blocks followers and generic cooldown blocks both',async()=>{
  const following=worker();following.context.configure('offline');following.data.st.listEndpointUntil=Date.now()+60000;
  await following.context.run();assert.equal(following.calls.filter(x=>x==='GET').length,1);
  const followers=worker();followers.context.configure('offline');followers.context.testTask={...task,direction:'followers'};
  followers.data.st.listEndpointUntil=Date.now()+60000;await followers.context.run();
  assert.equal(followers.calls.filter(x=>x==='GET').length,0);
  assert.equal(followers.data.benchmarkPending.result.actual_http_requests,0);
  for(const direction of ['following','followers']){
    const held=worker();held.context.configure('ok');held.context.testTask={...task,direction};held.data.st.cool.list.until=Date.now()+60000;
    await held.context.run();assert.equal(held.calls.filter(x=>x==='GET').length,0);
    assert.equal(held.calls.filter(x=>x.path==='/api/benchmark/permit').length,0);
  }
});

test('benchmark allows exactly the requested sizes for both strict directions',async()=>{
  for(const direction of ['following','followers'])for(const page_size of [50,100,200,300,500,1500]){
    const url=B.url({...task,direction,page_size,cursor:'cursor&value'},'12');
    assert.equal(url,'https://www.instagram.com/api/v1/friendships/42/'+direction+'/?count='+page_size+'&max_id=cursor%26value'+(direction==='followers'?'&search_surface=follow_list_page':''));
    const {context,calls}=injected();const res=await context.run(url,'12',30000);
    assert.equal(calls.length,1);assert.equal(res.actual_http_requests,1);
  }
  for(const page_size of [0,25,250,1000,1501,'300'])assert.throws(()=>B.url({...task,page_size},'12'));
});
test('MAIN rejects wrong-direction parameters, unknown sizes and extra parameters before dispatch',async()=>{
  const prefix='https://www.instagram.com/api/v1/friendships/42/';
  for(const tail of ['followers/?count=300','following/?count=300&search_surface=follow_list_page','following/?count=301','following/?count=1500&extra=1','profile/?count=500']){
    const {context,calls}=injected();const res=await context.run(prefix+tail,'12',30000);
    assert.equal(calls.length,0);assert.equal(res.error,'invalid_url');
  }
});
