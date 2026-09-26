import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const section = (a,b) => source.slice(source.indexOf(a), source.indexOf(b, source.indexOf(a)));
const tick = () => new Promise(r => setImmediate(r));
function base(extra={}) {
  const elements = new Map();
  const $ = key => {if (!elements.has(key)) elements.set(key, {innerHTML:'', textContent:'',className:'',style:{},classList:{toggle(){}}}); return elements.get(key);};
  return vm.createContext({$, elements, S:{views:[],rows:[],pick:new Set(),tagBy:new Map()}, M:{byId:new Map(),patch(){}},
    toast(){}, renderRows(){},renderFilters(){},loadFacetsSoon(){},loadCounts(){}, refreshPerson(){},
    store:{get:()=>[],set(){}},toQuery:()=>new URLSearchParams(), api:{}, URLSearchParams,
    int:v=>v==null?'–':String(v),esc:String,ucf:String,plural:(n,s)=>`${n} ${s}`,slabel:String,
    setTimeout,clearTimeout, ...extra});
}
test('failed view save never reports success or reloads views', async()=>{
  const messages=[]; const c=base({toast:m=>messages.push(m),api:{post:async()=>{throw Error('offline')}}});
  vm.runInContext(section('async function saveView(', 'async function deleteView('),c);
  await c.saveView('Founders'); assert.deepEqual(messages,['View not saved. Reconnect and try again.']); assert.equal(c.S.saving,true);
});
test('temporary view outage retains server views and old local views survive reconnect',async()=>{
  let online=false;
  const c=base({store:{get:()=>[{id:1,name:'Local',query:'q=local'}]},api:{get:async()=>{if(!online)throw Error();return [{id:1,name:'Server',query:''}];}}});
  c.S.views=[{id:7,name:'Previously loaded'}];
  vm.runInContext(section('async function loadViews()', '// ---------- sidebar'),c);
  await c.loadViews();assert.equal(c.S.views[0].id,7);
  online=true;await c.loadViews();assert.equal(c.S.views.length,2);assert.equal(c.S.views[1].id,'local:1');
});
test('bulk tag change offers no destructive inverse undo',async()=>{
  const messages=[];const c=base({api:{post:async()=>({updated:2})},toast:(...args)=>messages.push(args)});
  c.S.rows=[{id:1,tags:[{tag:'Founder',source:'manual'}]},{id:2,tags:[]}];
  vm.runInContext('let bulkKey="";'+section('async function bulk(', '// ---------- marking'),c);
  await c.bulk({add:['Founder']},[1,2]);assert.equal(messages[0][1],undefined);assert.match(messages[0][0],/Undo unavailable/);assert.equal(c.S.rows[0].tags.length,1);
});
test('note edits serialize and coalesce per person without dropping another person',async()=>{
  const requests=[];const c=base({api:{post:(url,body)=>new Promise(resolve=>requests.push({url,body,resolve}))}});
  vm.runInContext(section('const noteSaves =', "$('#detail').addEventListener('input'"),c);
  vm.runInContext("saveNote(1,'old'); flushNote(1,noteSaves.get(1)); saveNote(1,'middle'); saveNote(1,'latest'); saveNote(2,'other'); flushNote(2,noteSaves.get(2));",c);
  assert.equal(requests.length,2);requests[0].resolve();await tick();
  assert.equal(requests.length,3);assert.equal(requests[2].body.note,'latest');assert.match(requests[1].url,/person\/2/);
  requests[1].resolve();requests[2].resolve();await tick();
  vm.runInContext('for(const job of noteSaves.values())clearTimeout(job.timer)',c);
});
test('running list with unknown total never renders Done; unknown bio metrics remain unknown',()=>{
  const c=base({PUBLIC_BIOS:{data:{on:true,ready:true,state:'running',running:true,remaining:null,freshPerMinute:null},stale:false},ago:()=>'',eta:()=>null, publicBioState:()=> 'Running', ST_LABEL:{},ST_DOT:{},LIST_STATE:{},listFilter:'all',listsAll:false});
  c.S.sc={ext:{online:true}, lists:[{state:'running',seed:'test',received:100,total:null}], progress:{lists:{left:0}}};
  vm.runInContext(section('function renderScraper()', 'let listsAll ='),c);c.renderScraper();
  const html=c.$('#stages').innerHTML;assert.match(html,/remaining count unknown/);assert.match(html,/Reading now/);assert.doesNotMatch(html.split('2. Read bios')[0],/>Done</);assert.match(html,/– queued bios waiting · – fresh bios/);
  c.S.scStale=true;c.renderScraper();assert.match(c.$('#now').innerHTML,/Connection lost/);
});
test('controls expose failed action and disable stale controls',async()=>{
  const el={isConnected:true,innerHTML:'',dataset:{},setAttribute(){},addEventListener(){},querySelectorAll:()=>[]};
  let offline=false,postFail=false;
  const data={all_paused:false,stages:[{id:'lists',label:'Lists',state:'running',paused:false,now:'Reading',help:'Pause lists'}]};
  let code=readFileSync(new URL('../../web/controls.js',import.meta.url),'utf8');
  code=code.replace('  function start() {','  window.testControls = {load, send};\n  function start() {');
  const c=vm.createContext({window:{},document:{createElement:()=>el,readyState:'loading',addEventListener(){}},fetch:async(u,o)=>{if(offline)throw Error();return {ok:!(o?.method&&postFail),json:async()=>o?.method&&postFail?{error:'failed'}:data};},setInterval(){},clearInterval(){},Date,confirm:()=>true});
  vm.runInContext(code,c);await c.window.testControls.load();postFail=true;await c.window.testControls.send({stage:'lists',action:'pause'});
  assert.match(el.innerHTML,/Change failed/);assert.doesNotMatch(el.innerHTML,/disabled/);
  offline=true;await c.window.testControls.load();assert.match(el.innerHTML,/disabled/);
});
test('demo implements controls, qualification and public bio scope',async()=>{
  const w={fetch:async()=>{throw Error('unexpected network')}};
  const c=vm.createContext({window:w,location:{origin:'http://demo'},URL,URLSearchParams,Response,console,setInterval(){},setTimeout:fn=>fn()});
  vm.runInContext(readFileSync(new URL('../../web/mock.js',import.meta.url),'utf8'),c);
  const control=await (await w.fetch('/api/control')).json();assert.equal(control.stages.length,3);
  const paused=await (await w.fetch('/api/control',{method:'POST',body:JSON.stringify({stage:'all',action:'pause'})})).json();assert.equal(paused.all_paused,true);
  const qual=await (await w.fetch('/api/qual?view=all&limit=3')).json();assert.equal(qual.rows.length,3);assert.ok(qual.summary.verdicts>0);
  const pb=await (await w.fetch('/api/settings/public-bios',{method:'POST',body:JSON.stringify({scope:'missing'})})).json();assert.equal(pb.scope,'missing');
});
