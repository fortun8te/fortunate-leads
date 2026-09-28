import test from 'node:test';
import assert from 'node:assert/strict';
import A from '../lib/native-list-adapter.js';
const time = minute => `2026-09-28T00:${String(minute).padStart(2,'0')}:00Z`;
const users = (...ids) => ids.map(pk => ({pk: String(pk)}));
function fixture() {
  const binding = {viewer_id:'1',target_id:'2',direction:'followers',transport:'rest',operation:'native-rest',session_key:'same-order-and-rank-token'};
  return {...binding,run_id:'a',started_at:time(0),ended_at:time(10),count_at:time(0),count_source:'current_run',expected_count:3,
    pages:[{request:{...binding,cursor:null,count:200},status:200,captured_at:time(1),response:{users:users(10,11),has_more:true,next_max_id:'opaque'}},
      {request:{...binding,cursor:'opaque',count:200},status:200,captured_at:time(2),response:{users:users(11,12),has_more:false}}]};
}
test('only unique IDs count toward a complete chain', () => {
  const r=A.evaluateRun(fixture()); assert.equal(r.status,'complete'); assert.equal(r.unique_people,3); assert.equal(r.duplicate_rows,1);
});
test('same-viewer, target, direction, operation and cursor family remain bound', () => {
  for(const key of ['viewer_id','target_id','direction','transport','operation','session_key']) {
    const f=fixture(); f.pages[1].request[key]='changed'; assert.equal(A.evaluateRun(f).status,'partial');
    assert.ok(A.evaluateRun(f).reasons.includes('request_binding_changed'));
  }
});
test('missing first page and foreign cursors cannot complete', () => {
  const f=fixture(); f.pages.shift(); assert.equal(A.evaluateRun(f).status,'unknown');
  const g=fixture(); g.pages[1].request.cursor='different'; assert.ok(A.evaluateRun(g).reasons.includes('cursor_chain_broken'));
});
test('repeated cursor retains people but marks partial', () => {
  const f=fixture(); Object.assign(f.pages[1].response,{has_more:true,next_max_id:'opaque'});
  const r=A.evaluateRun(f); assert.equal(r.status,'partial'); assert.equal(r.unique_people,3); assert.ok(r.reasons.includes('cursor_cycle'));
});
test('stale count, missing total, count mismatch and limiting cannot complete', () => {
  for (const mutate of [f=>f.count_source='cached',f=>f.expected_count=4,f=>f.count_at=time(11),f=>f.pages[1].response.should_limit_list_of_followers=true]) {
    const f=fixture(); mutate(f); assert.equal(A.evaluateRun(f).status,'partial');
  }
});
test('modern mobile GraphQL root preserves opaque cursors but omission is not proof of end', () => {
  const root='xdt_api__v1__friendships__followers';
  const p=A.normalizePage({data:{[root]:{users:users(10),next_max_id:'abc'}}},'followers','mobile_graphql');
  assert.equal(p.next_cursor,'abc'); assert.equal(p.has_more,null); assert.equal(p.terminal,false);
  assert.equal(A.normalizePage({data:{[root]:{users:[]}}},'followers','mobile_graphql').terminal,false);
  assert.throws(()=>A.normalizePage({data:{[root]:{users:users(10)}}},'following','mobile_graphql'),/unknown_schema/);
});
test('GraphQL parses only the requested edge when both directions are present', () => {
  const page={data:{user:{edge_followed_by:{edges:[{node:{id:'10'}}],page_info:{has_next_page:false}},edge_follow:{edges:[{node:{id:'20'}}],page_info:{has_next_page:true,end_cursor:'next'}}}}};
  assert.deepEqual(A.normalizePage(page,'following','web_graphql').ids,['20']);
  assert.equal(A.normalizePage(page,'followers','web_graphql').terminal,true);
});
test('malformed or restricted responses and numeric ID precision loss are rejected', () => {
  for(const body of [{users:users(10),has_more:'false'},{users:users(10),has_more:true},{users:[{pk:9007199254740992}],has_more:false},{users:users(10),errors:[{message:'limited'}]},{users:users(10),should_limit_list_of_followers:'false'}]) {
    assert.throws(()=>A.normalizePage(body,'followers','rest'));
  }
});
test('transport failures and redirects cannot become accepted pages', () => {
  for(const changes of [{status:429},{redirected:true},{status:0}]) {
    const f=fixture(); Object.assign(f.pages[0],changes); assert.equal(A.evaluateRun(f).status,'unknown');
  }
});
test('requested 200 clamped to two is reported as two rows per page', () => {
  const report=A.evaluateExperiment({started_at:time(0),ended_at:time(30),runs:[fixture()],existing_ids:['10']});
  assert.equal(report.rows_per_page,2); assert.equal(report.new_people,2); assert.equal(report.new_people_per_hour,4); assert.equal(report.complete_target_lists,1);
});
test('global duplicates and repeated completed lists never inflate yield', () => {
  const f=fixture(),g=fixture(); g.run_id='b'; g.viewer_id='3'; g.pages.forEach(p=>p.request.viewer_id='3');
  const r=A.evaluateExperiment({started_at:time(0),ended_at:time(30),runs:[f,g]});
  assert.equal(r.complete_target_lists,1); assert.equal(r.unique_people,3);
});
test('projection uses fully completed lists, leaving insufficient pages unused', () => {
  assert.equal(A.pageBudgetProjection({target_size:1000,accepted_page_size:25,usable_pages_per_day:100}).complete_lists_per_day,2);
  assert.equal(A.pageBudgetProjection({target_size:1000,accepted_page_size:200,usable_pages_per_day:100}).complete_lists_per_day,20);
});
test('empty list needs a fresh zero count and explicit end', () => {
  const f=fixture(); f.expected_count=0; f.pages.length=1; f.pages[0].response={users:[],has_more:false};
  assert.equal(A.evaluateRun(f).status,'complete'); delete f.pages[0].response.has_more; assert.equal(A.evaluateRun(f).status,'partial');
});

import FL from '../lib/core.js';
test('following defaults to 200 and pinned experiments retain explicit sizes', () => {
  assert.equal(FL.listPageSize({direction:'following'},'1'),200);
  for(const n of [50,100,200]) {
    const job={direction:'following',page_size:n,experiment_viewer_ig_id:'1'};
    assert.equal(FL.listPageSize(job,'1'),n);
    assert.throws(()=>FL.listPageSize(job,'2'));
    assert.throws(()=>FL.listPageSize({...job,direction:'followers'},'1'));
  }
  assert.throws(()=>FL.listPageSize({direction:'following',page_size:200},'1'));
  assert.throws(()=>FL.listPageSize({direction:'followers',page_size:200},'1'));
});

import vm from 'node:vm';
import fs from 'node:fs';
test('runList sends count200 and resumes the exact cursor through its existing request gate', async () => {
  const saved={account:{ig_id:'1'},ids:{seed:{ig_id:'99'}},prog:{'seed/following':{jobId:7,countAttempted:true,total:300,totalSource:'current_run',next:'opaque',pages:1}}};
  const context=vm.createContext({FL,Date,Set,URLSearchParams,AbortController,importScripts(){},setTimeout,clearTimeout,
    chrome:{runtime:{getManifest:()=>({version:'3.9.20'}),onMessage:{addListener(){}}},storage:{local:{get:async k=>({[k]:saved[k]}),set:async x=>Object.assign(saved,x)}}}});
  vm.runInContext(fs.readFileSync(new URL('../background.js',import.meta.url),'utf8').split('// ---- lifecycle')[0],context);
  await vm.runInContext(`(async()=>{
    igRequest=async(gen,tab,url,kind,ctx)=>{globalThis.request={url,kind,ctx};return {res:{status:200,json:{users:[{pk:'100',username:'one'}],has_more:true,next_max_id:'next'}},bad:null}};
    queueDone=async(path,body)=>{globalThis.sent=body};
    await runList(mem.gen,{id:7,seed:'seed',ig_id:'99',direction:'following',cursor:'opaque',received:50,page_size:200,experiment_viewer_ig_id:'1'},{id:1});
  })()`,context);
  const url=new URL(context.request.url);
  assert.equal(url.searchParams.get('count'),'200');assert.equal(url.searchParams.get('max_id'),'opaque');
  assert.equal(context.request.kind,'list');assert.equal(context.sent.requested_count,200);
});
