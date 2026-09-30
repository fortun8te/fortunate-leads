import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source = readFileSync(new URL('../lib/workspace.js', import.meta.url), 'utf8');
function harness({id='123', lane='main-lane', tabs=[]}={}) {
  const calls=[];
  const chrome={storage:{local:{get:async()=>({laneId:lane})}}, cookies:{get:async()=>id ? {value:id} : null},
    tabs:{query:async()=>tabs,update:async(tabId,options)=>{calls.push(['update',tabId,options]);return {id:tabId,windowId:1};},
      create:async(options)=>{calls.push(['create',options]);return {id:9,windowId:1};}},
    windows:{update:async(...args)=>calls.push(['window',...args])}};
  const context=vm.createContext({chrome});vm.runInContext(source,context);
  return {calls,open:message=>context.openWorkspaceInstagram(chrome,message)};
}
const inbox={destination:'inbox',expected_lane_id:'main-lane',expected_account_id:'123'};
test('Inbox opens a separate tab without navigating the collector tab',async()=>{
  const h=harness({tabs:[{id:1,url:'https://www.instagram.com/brand/'}]});
  const result=await h.open(inbox);
  assert.equal(result.ok,true);assert.equal(result.account_verified,true);
  assert.equal(h.calls[0][0],'create');assert.equal(h.calls[0][1].url,'https://www.instagram.com/direct/inbox/');
});
test('existing Inbox is focused without reload',async()=>{
  const h=harness({tabs:[{id:2,url:'https://www.instagram.com/direct/inbox/'}]});await h.open(inbox);
  assert.equal(h.calls[0][0],'update');assert.equal(h.calls[0][1],2);assert.deepEqual(Object.keys(h.calls[0][2]),['active']);
});
test('wrong profile, signed-out account and arbitrary destinations cannot open Inbox',async()=>{
  for (const config of [{id:'456'},{id:null},{lane:'other'}]) {
    const h=harness(config);await assert.rejects(h.open(inbox));assert.equal(h.calls.length,0);
  }
  const h=harness();await assert.rejects(h.open({...inbox,destination:'https://bad.example/'}));
  await assert.rejects(h.open({destination:'inbox'}));assert.equal(h.calls.length,0);
});
test('fresh setup can open Instagram while signed out, without claiming connection',async()=>{
  const h=harness({id:null});const result=await h.open({destination:'home'});
  assert.equal(result.ok,true);assert.equal(result.account_verified,false);
});
