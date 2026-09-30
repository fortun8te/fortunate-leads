import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../../web/controls.js', import.meta.url), 'utf8');
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };
const collection = (paused = true) => ({processing:{mode:'R'}, stages:['lists','bios','ai'].map(id => ({id,paused:id === 'ai' || paused,state:id === 'ai' || paused ? 'paused' : 'running',now:id === 'lists' ? 'Reading list page 2' : ''}))});
const engines = (override = {}) => ({processing:{mode:'R'},paused:false,engines:{laya:{enabled:false,allowed:false,state:'off',active:false,ready:false,reason:'Choose RLAI or RLEAI',stop_acknowledged:true},k2:{enabled:false,allowed:false,state:'off',active:false,ready:false,reason:'Choose RLAI or RLEAI',stop_acknowledged:true}},...override});

function harness() {
  const requests = [], listeners = {}, windowListeners = {};
  let timer, html = '', buttons = [];
  const panel = {scrollTop:0};
  const document = {readyState:'complete',visibilityState:'visible',body:{},activeElement:null,
    createElement:() => el,querySelector:s => s === '#stage-controls' ? {appendChild(node){node.isConnected=true;}} : null,
    addEventListener:(name, callback) => {listeners[name]=callback;}};
  document.activeElement = document.body;
  const el = {isConnected:false,dataset:{},setAttribute(){},contains:node => buttons.includes(node),
    addEventListener:(name, callback) => {listeners[`el:${name}`]=callback;},
    querySelector:s => s === '.fl-ctl-panel' ? panel : buttons.find(button => s.includes(`data-focus="${button.dataset.focus}"`)),
    get innerHTML(){return html;},set innerHTML(value){html=value;buttons=[...value.matchAll(/<button\b([^>]*)>/g)].map(([,attrs]) => {
      const dataset = {};for (const [,key,val] of attrs.matchAll(/data-([\w-]+)="([^"]+)"/g)) dataset[key.replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=val;
      const button = {dataset,disabled:/\sdisabled(?:\s|$)/.test(attrs),focus(){document.activeElement=this;}};
      return button;
    });}};
  vm.runInNewContext(source,{document,Date,AbortController,Event:class Event{constructor(type){this.type=type;}},
    window:{__flControls:false,addEventListener:(name,callback)=>{windowListeners[name]=callback;},dispatchEvent(event){windowListeners[event.type]?.();}},
    clearTimeout(){},setTimeout(callback){timer=callback;return 1;},
    fetch(url,options){return new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));}});
  const respond = (index,data,ok=true) => requests[index].resolve({ok,status:ok?200:500,json:async()=>data});
  const fail = index => requests[index].reject(new Error('offline'));
  const click = (selector) => {const button=buttons.find(selector);assert.ok(button);let stopped=false;const event={stopPropagation(){stopped=true;},target:{closest:query => query === '.fl-ctl-summary' && button.dataset.focus === 'panel' ? button : query === '[data-qualification]' && button.dataset.focus === 'qualification' ? button : query === '[data-engine]' && button.dataset.engine ? button : query === '[data-stage="collection"]' && button.dataset.stage === 'collection' ? button : null}};listeners['el:click'](event);if(!stopped)listeners.click?.(event);return button;};
  return {requests,respond,fail,click,el,document,buttons,poll:()=>timer?.(),visibility:()=>listeners.visibilitychange?.()};
}
async function ready() {const h=harness();h.respond(0,collection());h.respond(1,engines());await settle();return h;}

test('initial control state keeps scraping action and per-engine statuses visible', async () => {
  const h=await ready();
  assert.match(h.el.innerHTML,/fl-ctl-word">Stopped/);
  assert.match(h.el.innerHTML,/Ranking hints <span class="fl-engine-state">Off in R mode<\/span>/);
  assert.match(h.el.innerHTML,/Bio checks <span class="fl-engine-state">Off in R mode<\/span>/);
  assert.match(h.el.innerHTML,/Continue collecting/);
  assert.match(h.el.innerHTML,/aria-expanded="false"/);
});

test('panel opens without changing mode and offers independent engine actions', async () => {
  const h=await ready();h.click(button=>button.dataset.focus==='panel');
  assert.match(h.el.innerHTML,/aria-expanded="true"/);
  assert.match(h.el.innerHTML,/data-engine="laya"/);
  assert.match(h.el.innerHTML,/data-engine="k2"/);
  assert.match(h.el.innerHTML,/Ranking hints reorder people/);
  h.respond(2,collection());h.respond(3,engines({processing:{mode:'RLAI'},engines:{...engines().engines,k2:{enabled:true,allowed:true,state:'waiting',active:false,ready:true,reason:'Waiting for work.',stop_acknowledged:false}}}));h.respond(4,{reviewed:4,queue:2,notes_pending:1,progress:{active:{handle:'someone'}}});await settle();
  assert.match(h.el.innerHTML,/Checking @someone/);
  assert.match(h.el.innerHTML,/4 bios reviewed, 2 waiting, 1 note waiting/);
});

test('K2 action waits for backend confirmation and leaves mode unchanged', async () => {
  const h=await ready();h.click(button=>button.dataset.focus==='panel');
  const button=h.click(button=>button.dataset.engine==='k2');
  const post=h.requests.find(request=>request.url==='/api/engines' && request.options?.method==='POST');
  assert.deepEqual(JSON.parse(post.options.body),{engine:'k2',enabled:true});
  assert.match(h.el.innerHTML,/Saving…/);
  const reply=engines({engines:{...engines().engines,k2:{enabled:true,allowed:false,state:'off',active:false,ready:false,reason:'Choose RLAI or RLEAI',stop_acknowledged:true}}});
  post.resolve({ok:true,status:200,json:async()=>reply});await settle();
  assert.match(h.el.innerHTML,/Off in R mode/);
  assert.match(h.el.innerHTML,/Excluded from AI modes|Included when a local or external AI/);
  assert.match(h.el.innerHTML,/Rules/);
  assert.equal(button.dataset.engine,'k2');
});

test('stopping remains visible until the backend acknowledges the stop', async () => {
  const h=harness();h.respond(0,collection());
  const stopping=engines({processing:{mode:'RLAI'},paused:true,engines:{...engines().engines,k2:{enabled:true,allowed:true,state:'stopping',active:true,ready:true,reason:'Finishing the current request.',stop_acknowledged:false}}});
  h.respond(1,stopping);await settle();
  assert.match(h.el.innerHTML,/Bio checks <span class="fl-engine-state">Stopping<\/span>/);
  h.poll();h.respond(2,collection());h.respond(3,engines({processing:{mode:'RLAI'},paused:true,stop_acknowledged:true,engines:{...stopping.engines,k2:{...stopping.engines.k2,state:'off',active:false,stop_acknowledged:true}}}));h.respond(4,{paused:true,stop_acknowledged:true,state:'paused'});await settle();
  assert.match(h.el.innerHTML,/Bio checks <span class="fl-engine-state">Paused<\/span>/);
});

test('polling is single flight and a failed engine read disables its actions', async () => {
  const h=await ready();h.poll();h.poll();h.visibility();
  assert.equal(h.requests.length,4,'only one pair of status requests is pending');
  h.respond(2,collection());h.fail(3);await settle();
  h.click(button=>button.dataset.focus==='panel');
  assert.match(h.el.innerHTML,/Bio checks <span class="fl-engine-state">Unknown<\/span>/);
  assert.equal(h.buttons.find(button=>button.dataset.engine==='k2').disabled,true);
  assert.match(h.el.innerHTML,/Some statuses couldn&#39;t be confirmed/);
});

test('an incomplete collection snapshot locks the scraping action', async () => {
  const h=harness();
  const incomplete=collection(false);incomplete.stages=incomplete.stages.filter(stage=>stage.id!=='bios');
  h.respond(0,incomplete);h.respond(1,engines());await settle();
  assert.match(h.el.innerHTML,/fl-ctl-word">Status unavailable/);
  assert.equal(h.buttons.find(button=>button.dataset.stage==='collection').disabled,true);
});

test('a slow status response cannot undo a confirmed scraping pause', async () => {
  const h=await ready();
  h.poll();
  h.click(button=>button.dataset.stage==='collection');
  const post=h.requests.find(request=>request.url==='/api/control' && request.options?.method==='POST');
  assert.deepEqual(JSON.parse(post.options.body),{stage:'collection',action:'resume'});
  post.resolve({ok:true,status:200,json:async()=>collection(false)});await settle();
  assert.match(h.el.innerHTML,/Stop collecting/);
  h.respond(2,collection(true));h.respond(3,engines());await settle();
  assert.match(h.el.innerHTML,/Stop collecting/);
  const latest=h.requests.length;
  h.respond(latest-2,collection(false));h.respond(latest-1,engines());await settle();
  assert.match(h.el.innerHTML,/Stop collecting/);
});

test('a failed engine change reports the error and keeps the last confirmed state', async () => {
  const h=await ready();h.click(button=>button.dataset.focus==='panel');
  h.click(button=>button.dataset.engine==='laya');
  const post=h.requests.find(request=>request.url==='/api/engines' && request.options?.method==='POST');
  post.reject(new Error('offline'));await settle();
  assert.match(h.el.innerHTML,/Couldn&#39;t confirm ranking hints change/);
  assert.match(h.el.innerHTML,/Ranking hints <span class="fl-engine-state">Off in R mode<\/span>/);
});

test('idle and waiting collection never claim to be collecting', async () => {
  for (const [state, word] of [['idle', 'Ready'], ['waiting', 'Waiting']]) {
    const h = harness(), status = collection(false);
    status.stages.filter(stage => stage.id !== 'ai').forEach(stage => { stage.state = state; stage.now = ''; });
    h.respond(0, status); h.respond(1, engines()); await settle();
    assert.match(h.el.innerHTML, new RegExp(`fl-ctl-word">${word}`));
    assert.doesNotMatch(h.el.innerHTML, /fl-ctl-word">Collecting/);
    assert.match(h.el.innerHTML, /Stop keeps your progress/);
  }
});

test('stop waits for the active request to finish before offering Continue', async () => {
  const h = harness(), status = collection(false);
  h.respond(0, status); h.respond(1, engines()); await settle();
  h.click(button => button.dataset.stage === 'collection');
  assert.match(h.el.innerHTML, />Stopping…<\/button>/);
  const post = h.requests.find(request => request.options?.method === 'POST');
  assert.deepEqual(JSON.parse(post.options.body), { stage: 'collection', action: 'pause' });
  const stopping = collection(true); stopping.stages[0].active = true; stopping.stages[0].state = 'stopping';
  post.resolve({ ok: true, json: async () => stopping }); await settle();
  assert.match(h.el.innerHTML, /fl-ctl-word">Stopping…/);
  assert.match(h.el.innerHTML, /disabled>Stopping…/);
  assert.doesNotMatch(h.el.innerHTML, />Continue collecting<\/button>/);
  const last = h.requests.length;
  h.respond(last - 2, collection(true)); h.respond(last - 1, engines()); await settle();
  assert.match(h.el.innerHTML, />Continue collecting<\/button>/);
  assert.match(h.el.innerHTML, /Progress is saved/);
  assert.equal(h.requests.filter(request => !request.options?.method).length, 4, 'one status read after the action, not two');
});

test('an unchanged status poll preserves the control buttons and keyboard focus', async () => {
  const h = await ready(), button = h.el.querySelector('[data-focus="collection"]');
  button.focus();
  h.poll(); h.respond(2, collection()); h.respond(3, engines()); await settle();
  assert.equal(h.el.querySelector('[data-focus="collection"]'), button);
  assert.equal(h.document.activeElement, button);
});

test('a partial stop reply cannot pretend that collection stopped', async () => {
  const h = harness(); h.respond(0, collection(false)); h.respond(1, engines()); await settle();
  h.click(button => button.dataset.stage === 'collection');
  const post = h.requests.find(request => request.options?.method === 'POST');
  post.resolve({ ok: true, json: async () => ({ stages: [{ id: 'lists', paused: true }] }) }); await settle();
  assert.match(h.el.innerHTML, /Couldn&#39;t confirm collection change/);
  assert.match(h.el.innerHTML, /aria-expanded="true"/);
  assert.doesNotMatch(h.el.innerHTML, />Continue collecting<\/button>/);
});


test('bio checks stay visible with the panel closed and pause independently of collection', async () => {
  const h=harness();h.respond(0,collection(false));h.respond(1,engines({processing:{mode:'RLAI'},paused:false,stop_acknowledged:false}));await settle();
  h.poll();h.respond(2,collection(false));h.respond(3,engines({processing:{mode:'RLAI'},paused:false,stop_acknowledged:false}));h.respond(4,{paused:false,state:'working',queue:12,reviewed:4,stop_acknowledged:false,progress:{active:{handle:'owner'}}});await settle();
  assert.match(h.el.innerHTML,/fl-qual-status[^>]*>Qualification <b>Checking/);
  assert.match(h.el.innerHTML,/4 bios reviewed · 12 remaining/);
  h.click(b=>b.dataset.focus==='qualification');
  const post=h.requests.find(r=>r.options?.method==='POST');
  assert.equal(post.url,'/api/local-processing');assert.deepEqual(JSON.parse(post.options.body),{paused:true});
  post.resolve({ok:true,json:async()=>({paused:true,state:'stopping',stop_acknowledged:false})});await settle();
  assert.match(h.el.innerHTML,/disabled>Stopping…/);
  assert.match(h.el.innerHTML,/>Stop collecting<\/button>/);
  assert.doesNotMatch(h.el.innerHTML,/>Continue checks<\/button>/);
});

test('collection detail prioritizes a running bio stage over idle lists', async () => {
  const h=harness(),status=collection(false);status.stages[0].state='idle';status.stages[0].now='No lists waiting';status.stages[1].state='running';status.stages[1].now='Reading @founder';
  h.respond(0,status);h.respond(1,engines());await settle();
  assert.match(h.el.innerHTML,/fl-ctl-current">Reading @founder/);
});
