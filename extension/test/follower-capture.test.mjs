import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
import C from '../lib/follower-capture.js';
const base=()=>({method:'GET',direction:'followers',viewer_id:'12',target_id:'42',path:'/api/v1/friendships/42/followers/',params:[['count','12'],['search_surface','follow_list_page']],at:Date.now()});
const task={route:'web_modal',direction:'followers',target_id:'42',viewer_id:'12',page_size:null,cursor:null};
async function signed(p=base()){return {...p,shape_hash:await C.hash(p,webcrypto)}}
test('only safe followers GET shapes persist and unknown/auth parameters invalidate the whole template',()=>{
  assert.ok(C.sanitize(base()));
  for(const change of [{method:'POST'},{direction:'following'},{viewer_id:'x'},{target_id:'99'},
    {path:'/api/v1/friendships/42/following/'},{params:[['count','12'],['access_token','secret']]},
    {params:[['count','12'],['count','50']]},{params:[['count','12'],['authorization','secret']]}])
    assert.equal(C.sanitize({...base(),...change}),null);
});
test('modal replay uses exact observed shape and binds viewer, target, hash, age and count',async()=>{
  const p=await signed();const prepared=await C.prepare(task,'12',[p],webcrypto);
  assert.equal(prepared.url,'https://www.instagram.com/api/v1/friendships/42/followers/?count=12&search_surface=follow_list_page');
  for(const change of [{viewer_id:'99'},{target_id:'99'},{page_size:50},{direction:'following'}])
    await assert.rejects(C.prepare({...task,...change},'12',[p],webcrypto));
  await assert.rejects(C.prepare(task,'12',[{...p,shape_hash:'a'.repeat(64)}],webcrypto));
  await assert.rejects(C.prepare(task,'12',[await signed({...base(),at:Date.now()-25*3600000})],webcrypto));
});
test('pagination key cannot be invented, and an observed tail cannot manufacture a first-page request',async()=>{
  await assert.rejects(C.prepare({...task,cursor:'next'},'12',[await signed()],webcrypto));
  const tail=await signed({...base(),params:[['count','12'],['max_id','old']]});
  await assert.rejects(C.prepare(task,'12',[tail],webcrypto));
  const prepared=await C.prepare({...task,cursor:'next&safe'},'12',[tail],webcrypto);
  assert.match(prepared.url,/max_id=next%26safe$/);
});
test('search needs observed query shape, explicit cap flag, query and bounded request index',async()=>{
  const search={...task,route:'web_search',query:'new query',capped_list_fallback:true,request_limit:7,request_index:0};
  await assert.rejects(C.prepare(search,'12',[await signed()],webcrypto));
  const captured=await signed({...base(),params:[['count','12'],['query','observed query']]});
  const prepared=await C.prepare(search,'12',[captured],webcrypto);assert.match(prepared.url,/query=new\+query$/);
  for(const change of [{query:''},{capped_list_fallback:false},{request_index:7},{request_limit:21},{request_index:-1}])
    await assert.rejects(C.prepare({...search,...change},'12',[captured],webcrypto));
  await assert.rejects(C.prepare(task,'12',[captured],webcrypto));
});
function passive({dialog=true,status=200,redirected=false,finalUrl=null,body={status:'ok',users:[{pk:'8',username:'private-handle'}]}}={}){
  const saved={},listeners=[],messages=[];let calls=0;
  const location={hostname:'www.instagram.com',origin:'https://www.instagram.com',href:'https://www.instagram.com/someone/'};
  const window={addEventListener:(name,cb)=>listeners.push(cb),postMessage:data=>{messages.push(data);for(const cb of listeners)cb({source:window,origin:location.origin,data})},fetch:async input=>{calls++;return {ok:status===200,status,redirected,url:finalUrl||String(input),clone:()=>({text:async()=>JSON.stringify(body)})}}};
  class XHR {open(){}send(){}addEventListener(){}}
  const context=vm.createContext({window,location,URL,URLSearchParams,FormData,Date,Set,TextEncoder,Uint8Array,crypto:webcrypto,XMLHttpRequest:XHR,
    document:{cookie:'ds_user_id=12; sessionid=never-store-this; csrftoken=never-store-csrf',addEventListener(){},querySelector:()=>dialog?{getClientRects:()=>[{}]}:null,querySelectorAll:()=>[]},
    chrome:{runtime:{sendMessage:async()=>{}},storage:{local:{get:async()=>saved,set:async data=>Object.assign(saved,data)}}}});
  for(const file of ['../lib/follower-capture.js','../bridge.js','../relay.js'])vm.runInContext(fs.readFileSync(new URL(file,import.meta.url),'utf8'),context);
  return {window,saved,messages,calls:()=>calls};
}
const settle=async()=>{for(let i=0;i<15;i++)await new Promise(resolve=>setImmediate(resolve))};
const observed='https://www.instagram.com/api/v1/friendships/42/followers/?count=12&search_surface=follow_list_page';
test('passive first-party dialog request saves safe hashed template with no extra HTTP or identities from rows',async()=>{
  const p=passive();await p.window.fetch(observed,{get headers(){throw Error('never inspect headers')}});await settle();
  assert.equal(p.calls(),1);const templates=p.saved.followerRequestTemplates;assert.equal(templates.length,1);
  assert.match(templates[0].shape_hash,/^[0-9a-f]{64}$/);assert.equal(templates[0].target_id,'42');assert.equal(templates[0].viewer_id,'12');
  assert.doesNotMatch(JSON.stringify(templates),/never-store|private-handle|csrftoken|sessionid|headers|users/);
});
test('no dialog, failed response, redirect, following route and auth query never establish a replay template',async()=>{
  for(const [opts,url] of [[{dialog:false},observed],[{status:429},observed],[{redirected:true},observed],
    [{finalUrl:'https://www.instagram.com/'},observed],[{},observed.replace('/followers/','/following/')],[{},observed+'&access_token=secret']]){
    const p=passive(opts);await p.window.fetch(url);await settle();assert.equal(p.saved.followerRequestTemplates,undefined);
  }
});
test('extension fetch bypass never captures itself as first-party UI evidence',async()=>{
  const p=passive();await p.window.__flFetch(observed);await settle();assert.equal(p.saved.followerRequestTemplates,undefined);assert.equal(p.calls(),1);
});
