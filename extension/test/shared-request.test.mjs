import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source = readFileSync(new URL('../background.js', import.meta.url), 'utf8');
function harness(grant = {granted:true, token:'token', expires_at:new Date(Date.now()+90000).toISOString()}) {
  const calls = [], mem = {}, cur = {job:{id:1},at:0};
  const context = vm.createContext({Date, Number, Math, mem, FL:{controlAllows:()=>true}, ControlPaused:class extends Error {},
    assertControl:async()=>{}, api:async(path,body)=>{ calls.push(body); if(grant instanceof Error) throw grant; return {status:200,json:grant}; },
    get:async key=>key==='cur'?cur:false, set:async()=>{}, sleep:async()=>{}, chrome:{tabs:{get:async()=>({status:'complete'})}} });
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

test('a delayed permit is released unused and retries remain bounded', async()=>{
  const h=harness({granted:true, token:'late', expires_at:new Date(Date.now()+10000).toISOString()});
  let opened=false;
  await assert.rejects(h.context.navigateWithPermit('list',async()=>{opened=true;}));
  assert.equal(opened,false);
  assert.deepEqual(h.calls.map(c=>c.action),['acquire','release']);
  assert.ok(h.mem.sharedWaitUntil >= Date.now()+14000);
});

function lookupHarness(remove) {
  const releases=[], saved=[];
  const context=vm.createContext({ Date, encodeURIComponent, setTimeout:fn=>{fn();},
    mem:{gen:1,lookups:new Set()}, waiters:{}, IG:'https://www.instagram.com',
    FL:{laneBusy:()=>false,afterRequest:()=>{},pageVerdict:()=>({code:'network'})},
    Superseded:class extends Error {}, assertControl:async()=>{}, get:async()=>null,
    set:async value=>saved.push(value), editSt:async fn=>fn({}), trail:async()=>{},
    checkedProfileDom:async()=>({url:'https://www.instagram.com/example/'}),
    acquireSharedRequest:async()=>'lookup-token', releaseSharedRequest:async t=>releases.push(t),
    chrome:{tabs:{create:async()=>({id:12}),remove}} });
  vm.runInContext(source.slice(source.indexOf('async function lookupViaPage('),source.indexOf('// ---- passive capture')),context);
  return {context,releases,saved};
}
test('profile lookup keeps ownership until tab closure is confirmed',async()=>{
  let close, entered;
  const closing=new Promise(resolve=>{entered=resolve;});
  const h=lookupHarness(()=>{entered();return new Promise(resolve=>{close=resolve;});});
  const lookup=h.context.lookupViaPage(1,'example','profile');
  await closing;
  assert.deepEqual(h.releases,[]);
  close();
  await lookup;
  assert.deepEqual(h.releases,['lookup-token']);
});
test('failed profile-tab close keeps ownership for the server fail-closed expiry',async()=>{
  const h=lookupHarness(async()=>{throw Error('close unconfirmed');});
  await h.context.lookupViaPage(1,'example','profile');
  assert.deepEqual(h.releases,[]);
  assert.equal(h.saved.at(-1).lane.after,'close_failed');
});

test('an already expired response releases the unused token without starting a request',async()=>{
  const h=harness({granted:true,token:'expired-unused',expires_at:new Date(Date.now()-1000).toISOString()});
  let opened=false;
  await assert.rejects(h.context.navigateWithPermit('list',async()=>{opened=true;}));
  assert.equal(opened,false);
  assert.equal(h.calls.at(-1).action,'release');
});

test('account change while waiting for a permit cannot start the old lookup',async()=>{
  const h=lookupHarness(async()=>{});
  let opened=false;
  h.context.chrome.tabs.create=async()=>{opened=true;return {id:12};};
  h.context.acquireSharedRequest=async()=>{h.context.mem.gen=2;return 'unused-old-account';};
  await assert.rejects(h.context.lookupViaPage(1,'example','profile'));
  assert.equal(opened,false);
  assert.deepEqual(h.releases,['unused-old-account']);
});

test('stop while navigation permit is pending releases it without opening a tab',async()=>{
  const h=harness();
  let opened=false;
  h.context.acquireSharedRequest=async()=>{h.mem.gen=1;return 'cancelled-navigation';};
  await assert.rejects(h.context.navigateWithPermit('list',async()=>{opened=true;}));
  assert.equal(opened,false);
  assert.equal(h.calls.at(-1).action,'release');
  assert.equal(h.calls.at(-1).token,'cancelled-navigation');
});

test('local pause while a grant arrives releases it without a browser action',async()=>{
  const h=harness();
  let opened=false;
  h.context.get=async key=>key==='localPaused';
  await assert.rejects(h.context.navigateWithPermit('list',async()=>{opened=true;}));
  assert.equal(opened,false);
  assert.deepEqual(h.calls.map(c=>c.action),['acquire','release']);
});
