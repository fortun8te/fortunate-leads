import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source = readFileSync(new URL('../background.js', import.meta.url), 'utf8');
function harness(grant = {granted:true, token:'token', expires_at:new Date(Date.now()+90000).toISOString()}) {
  const calls = [], mem = {}, cur = {job:{id:1},at:0};
  const context = vm.createContext({Date, Number, Math, mem, ControlPaused:class extends Error {},
    assertControl:async()=>{}, api:async(path,body)=>{ calls.push(body); if(grant instanceof Error) throw grant; return {status:200,json:grant}; },
    get:async()=>cur, set:async()=>{}, sleep:async()=>{}, chrome:{tabs:{get:async()=>({status:'complete'})}} });
  vm.runInContext(source.slice(source.indexOf('async function acquireSharedRequest('),source.indexOf('async function igRequest(')),context);
  return {context,calls,mem};
}
test('permit denial or unavailable server never starts navigation', async()=>{
  for(const grant of [{granted:false,wait_ms:15000},new Error('offline'),{granted:true,token:'x',expires_at:'invalid'}]) {
    const h=harness(grant); let opened=false;
    await assert.rejects(h.context.navigateWithPermit('list',async()=>{opened=true;}));
    assert.equal(opened,false);
  }
});
test('failed browser action releases its permit immediately',async()=>{
  const h=harness();
  await assert.rejects(h.context.navigateWithPermit('list',async()=>{throw Error('tab refused');}),/tab refused/);
  assert.equal(h.calls.at(-1).action,'release');
  assert.equal(h.calls.at(-1).token,'token');
});
test('completed navigation releases and next action needs a new acquire',async()=>{
  const h=harness();
  await h.context.navigateWithPermit('list',async()=>({id:1}));
  await h.context.navigateWithPermit('profile',async()=>({id:2}));
  assert.deepEqual(h.calls.map(c=>c.action),['acquire','release','acquire','release']);
});
