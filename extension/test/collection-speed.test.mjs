import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import FL from '../lib/core.js';
const account = {ig_id:'64189916997'}, lane = 'ln_sqrscja8tx6z';

test('idle check preference is persisted and bound to both exact account and install', () => {
  const before = JSON.stringify(FL.PACE);
  const setting = FL.collectionSpeedSetting(account, lane, 8000, 0);
  assert.equal(FL.pollWaitFor(account, lane, setting), 8000);
  assert.equal(setting.updated_at, '1970-01-01T00:00:00.000Z');
  assert.equal(FL.pollWaitFor({ig_id:'55966675376'}, lane, setting), 15000);
  assert.equal(FL.pollWaitFor({ig_id:'25764895364'}, lane, setting), 15000);
  assert.equal(FL.pollWaitFor(account, 'another-install', setting), 15000);
  assert.equal(FL.pollWaitFor(null, lane, setting), 15000);
  assert.equal(FL.pollWaitFor(account, lane, {...setting,ig_id:'old'}), 11650);
  assert.equal(FL.pollWaitFor(account, lane, {...setting,lane_id:'old'}), 11650);
  assert.equal(JSON.stringify(FL.PACE), before);
});

test('malformed values cannot lower request protections or escape the idle-check bounds', () => {
  for (const value of [0, 7999, 30001, NaN, Infinity, '8000', 8000.5, null]) {
    assert.throws(() => FL.collectionSpeedSetting(account, lane, value));
    assert.equal(FL.pollWaitFor(account, lane, {v:1,ig_id:account.ig_id,lane_id:lane,poll_wait_ms:value}),11650);
  }
  assert.throws(() => FL.collectionSpeedSetting({ig_id:'55966675376'},lane,8000));
  assert.throws(() => FL.collectionSpeedSetting(account,'another-install',8000));
  assert.equal(FL.pollWaitFor(account,lane,FL.collectionSpeedSetting(account,lane,30000)),30000);
  assert.deepEqual(FL.PACE.listGap,[7000,12000]);
  assert.equal(FL.PACE.windowMax,72);
});

function popup(store) {
  const elements = new Map(), listeners = [], calls = [];
  const document = {getElementById(id) { if (!elements.has(id)) elements.set(id,{dataset:{}}); return elements.get(id); }};
  const source = fs.readFileSync(new URL('../popup.js',import.meta.url),'utf8');
  const chrome = {storage:{local:{get:async keys => typeof keys === 'string' ? {[keys]:store[keys]} : Object.fromEntries(keys.map(k=>[k,store[k]]))},onChanged:{addListener:f=>listeners.push(f)}},
    runtime:{getManifest:()=>({version:'test'}),sendMessage:async message => {calls.push(message);return {ok:true,poll_wait_ms:message.poll_wait_ms};}},tabs:{create(){}}};
  const ctx = vm.createContext({FL,chrome,document,Date,Math,setInterval(){},setTimeout(){}});
  vm.runInContext(source,ctx);
  return {elements,listeners,calls,ctx};
}

test('popup applies runtime changes without reload and hides controls on identity changes', async () => {
  const h = popup({account,laneId:lane,st:{accountIgId:account.ig_id},view:{rate:{pages_hour:18}}});
  await Promise.resolve(); await Promise.resolve();
  assert.equal(h.elements.get('speedwrap').hidden,false);
  assert.equal(h.elements.get('speed').value,'11.65');
  h.elements.get('speed').value='8';
  await h.elements.get('speedapply').onclick();
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls)),[{cmd:'set_collection_speed',poll_wait_ms:8000}]);
  assert.match(h.elements.get('speedstatus').textContent,/next check.*no reload/);
  for (const listener of h.listeners) listener({account:{newValue:{ig_id:'55966675376'}}});
  assert.equal(h.elements.get('speedwrap').hidden,true);
  await h.elements.get('speedapply').onclick();
  assert.equal(h.calls.length,1);
});

test('popup blocks stale state identity and invalid inputs without sending a command', async () => {
  const stale = popup({account,laneId:lane,st:{accountIgId:'55966675376'}});
  await Promise.resolve(); await Promise.resolve();
  assert.equal(stale.elements.get('speedwrap').hidden,true);
  await stale.elements.get('speedapply').onclick();
  assert.equal(stale.calls.length,0);
  const valid = popup({account,laneId:lane,st:{accountIgId:account.ig_id}});
  await Promise.resolve(); await Promise.resolve();
  valid.elements.get('speed').value='0';
  await valid.elements.get('speedapply').onclick();
  assert.equal(valid.calls.length,0);
  assert.match(valid.elements.get('speedstatus').textContent,/8 to 30 seconds/);
});

test('compact diagnostics omit tokens, HTML, cookies, cursors and unknown metric fields', () => {
  const h = popup({});
  const report = h.ctx.compactLocalReport({account,laneId:lane,st:{hold:{code:'unknown_request',message:'private'}},
    debug:{sample:'private HTML'},cur:{job:{id:1,kind:'list',direction:'following',lease_token:'private',cursor:'private'}},
    box:[{body:{cookie:'private'}}],view:{today:{list:4,extra:'private'},rate:{pages_hour:2,html:'private'}},
    sharedRequest:{phase:'started',at:Date.now(),body:{kind:'list',job_id:1,token:'private',session:'private'}}});
  const serialized = JSON.stringify(report);
  assert.doesNotMatch(serialized,/private|lease_token|cursor|cookie|HTML|session/);
  assert.equal(report.counts.outbox,1);
  assert.equal(report.request.phase,'started');
  assert.equal(report.state.hold,'unknown_request');
});

test('popup preset updates desired checks and status updates preserve edits while reporting real counts', async () => {
  const h = popup({account,laneId:lane,st:{accountIgId:account.ig_id},view:{rate:{pages_hour:72},stages:{profile:false}}});
  await Promise.resolve(); await Promise.resolve();
  assert.match(h.elements.get('speedactual').textContent,/72 pages saved/);
  assert.equal(h.elements.get('bios').textContent,'Off');
  h.elements.get('speedpreset').value='30000'; h.elements.get('speedpreset').onchange();
  assert.equal(h.elements.get('speed').value,'30');
  for (const listener of h.listeners) listener({view:{newValue:{rate:{pages_hour:73},stages:{profile:false}}}});
  assert.equal(h.elements.get('speed').value,'30');
  await h.elements.get('speedapply').onclick();
  assert.equal(h.calls[0].poll_wait_ms,30000);
});

test('uncertain requests offer explicit review, prevent resume and never resume after review', async () => {
  const h = popup({view:{state:'paused',text:'Request completion unknown. Check the account tab.',request:{phase:'started',at:Date.now()}}});
  await Promise.resolve(); await Promise.resolve();
  assert.equal(h.elements.get('requestreview').hidden,false);
  assert.equal(h.elements.get('toggle').disabled,true);
  h.elements.get('toggle').onclick();
  assert.equal(h.calls.length,0);
  await h.elements.get('finishreview').onclick();
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls)),[{cmd:'review_shared_request'}]);
  assert.match(h.elements.get('reviewstatus').textContent,/remains paused/);
  assert.equal(h.elements.get('toggle').disabled,true);
});

test('review cannot be sent without uncertain request and stale error cannot block normal local resume', async () => {
  const h = popup({view:{state:'paused',text:'Paused',lastError:'The request did not confirm',request:null}});
  await Promise.resolve(); await Promise.resolve();
  assert.equal(h.elements.get('requestreview').hidden,true);
  assert.equal(h.elements.get('toggle').disabled,false);
  await h.elements.get('finishreview').onclick();
  assert.equal(h.calls.length,0);
  h.elements.get('toggle').onclick();
  assert.equal(h.calls[0].cmd,'resume');
});

test('finished requests with an unsettled release still offer explicit matching review', async () => {
  for (const phase of ['completed','not_sent']) {
    const h = popup({view:{state:'paused',text:'Request review needed',request:{phase,at:Date.now()}}});
    await Promise.resolve(); await Promise.resolve();
    assert.equal(h.elements.get('requestreview').hidden,false);
    assert.equal(h.elements.get('toggle').disabled,true);
    await h.elements.get('finishreview').onclick();
    assert.equal(h.calls.length,1);
    assert.equal(h.calls[0].cmd,'review_shared_request');
    assert.match(h.elements.get('reviewstatus').textContent,/remains paused/);
  }
});
