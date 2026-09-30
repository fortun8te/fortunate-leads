import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const read = p => readFileSync(new URL('../../web/' + p, import.meta.url), 'utf8');
const source=read('start.js');
const window={};vm.runInNewContext(source,{window,document:undefined});
const V=window.StartView;

test('setup lives in Accounts and old Start links resolve there', () => {
  const html=read('index.html'),app=read('app.js');
  assert.match(html,/id="account-setup" hidden/);
  assert.doesNotMatch(html,/id="tab-start"|id="view-start"|Get started/);
  assert.match(app,/v === 'start' \? 'accounts'/);
  assert.match(app,/window\.AccountSetup\?\.show/);
  assert.match(app,/href="#\/accounts">Add an account/);
});

test('missing connection has one action and escapes account details', () => {
  const html=V.stepHTML({id:'extension',title:'Chrome extension',status:'todo',detail:'Open Chrome',optional:false,items:[{name:'<b>@a</b>',state:'wait',text:'Offline'}],action:{kind:'wizard',label:'Connect a profile'}});
  assert.match(html,/data-st-wizard>Connect a profile/);
  assert.match(html,/&lt;b&gt;@a/);assert.doesNotMatch(html,/data-st-skip/);
});

function harness(steps) {
  const listeners={},messages=[];let reads=0,offline=false,active=true;
  const root={hidden:true,innerHTML:'',querySelector:()=>null,addEventListener:(event,fn)=>listeners[event]=fn};
  const document={hidden:false,getElementById:id=>id==='account-setup'?root:id==='view-accounts'?{classList:{contains:()=>active}}:null,addEventListener(){}};
  const window={addEventListener(){},toast:message=>messages.push(message)};
  vm.runInNewContext(source,{window,document,setTimeout(){},clearTimeout(){},fetch:async(url,options)=>{
    if(options?.method)return{ok:false};reads++;if(offline)throw Error('offline');return{ok:true,json:async()=>({steps})};
  }});
  return {window,root,listeners,messages,get reads(){return reads;},offline(v){offline=v;},active(v){active=v;}};
}
const required={id:'extension',title:'Chrome extension',status:'todo',detail:'Connect a profile',optional:false,items:[],action:{route:'#/accounts',label:'Show accounts'}};

test('only missing setup appears; ready workspace shows no setup panel',async()=>{
  const h=harness([{...required,status:'ok'}]);await h.window.AccountSetup.refresh();assert.equal(h.root.hidden,true);
  const missing=harness([required,{...required,id:'server',status:'ok'}]);await missing.window.AccountSetup.refresh();assert.equal(missing.root.hidden,false);assert.match(missing.root.innerHTML,/Connection needed/);assert.doesNotMatch(missing.root.innerHTML,/<details[^>]*open/);assert.doesNotMatch(missing.root.innerHTML,/data-step="server"/);
});

test('optional setup stays collapsed',async()=>{
  const h=harness([{...required,id:'backups',optional:true}]);await h.window.AccountSetup.refresh();assert.match(h.root.innerHTML,/Optional setup/);assert.doesNotMatch(h.root.innerHTML,/<details[^>]*open/);
});

test('overlapping setup reads share one request and offline status is visible',async()=>{
  const h=harness([required]);await Promise.all([h.window.AccountSetup.refresh(),h.window.AccountSetup.refresh()]);assert.equal(h.reads,1);
  h.offline(true);await h.window.AccountSetup.refresh();assert.match(h.root.innerHTML,/Showing saved setup/);
  h.offline(false);await h.listeners.click({target:{closest:s=>s==='[data-st-retry]'?{}:null}});assert.doesNotMatch(h.root.innerHTML,/Showing saved setup/);
});

test('a failed optional setup save stays visible as feedback',async()=>{
  const h=harness([{...required,id:'backups',optional:true}]);await h.window.AccountSetup.refresh();await h.listeners.click({target:{closest:s=>s==='[data-st-skip]'?{dataset:{stSkip:'backups'}}:null}});assert.deepEqual(h.messages,["Couldn't save setup. Try again."]);
});
