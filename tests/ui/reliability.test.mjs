import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const section = (a,b) => source.slice(source.indexOf(a), source.indexOf(b, source.indexOf(a)));
const tick = () => new Promise(r => setImmediate(r));
function base(extra={}) {
  const elements = new Map();
  const $ = key => {if (!elements.has(key)) elements.set(key, {innerHTML:'', textContent:'',className:'',style:{},classList:{toggle(){}},contains:()=>false}); return elements.get(key);};
  return vm.createContext({$, elements, S:{views:[],rows:[],pick:new Set(),tagBy:new Map()}, M:{byId:new Map(),patch(){}},
    toast(){}, renderRows(){},renderFilters(){},loadFacetsSoon(){},loadCounts(){}, refreshPerson(){},
    store:{get:()=>[],set(){}},toQuery:()=>new URLSearchParams(), api:{}, URLSearchParams,
    int:v=>v==null?'–':String(v),esc:String,ucf:String,plural:(n,s)=>`${n} ${s}`,slabel:String,
    setTimeout,clearTimeout, ...extra});
}
test('running list with unknown total never renders Done; unknown bio metrics remain unknown',()=>{
  const c=base({document:{activeElement:null},ago:()=>'',eta:()=>null, ST_LABEL:{},ST_DOT:{},LIST_STATE:{},listFilter:'all',listsAll:false});
  c.S.sc={ext:{online:true}, lists:[{state:'running',seed:'test',received:100,total:null}], soak:{'1h':{pages:1}}, progress:{lists:{left:5000,estimate:true,eta_h:null}}};
  vm.runInContext(section('function renderScraper()', 'let listsAll ='),c);c.renderScraper();
  const html=c.$('#stages').innerHTML;assert.match(html,/about 5000 left in active lists/);assert.match(html,/Reading now/);assert.doesNotMatch(html.split('2. Read bios')[0],/>Done</);
  c.S.scStale=true;c.renderScraper();assert.match(c.$('#now').innerHTML,/Connection lost/);
});
test('scraper separates active work from incomplete and capped list coverage',()=>{
  const c=base({document:{activeElement:null},ago:()=>'',eta:()=> 'about 2 h',ST_LABEL:{},ST_DOT:{},LIST_STATE:{},listFilter:'all',listsAll:false});
  c.S.sc={ext:{online:true},accounts:[{online:true,paused:false}],lists:[
    {state:'running',seed:'active',direction:'followers',received:40,total:100},
    {state:'partial',seed:'capped',direction:'followers',received:50,total:100},
    {state:'error',seed:'failed',direction:'following',received:10,total:100},
  ],soak:{'1h':{pages:2}},progress:{lists:{left:60,incomplete_lists:2,incomplete_left:140,capped_lists:1,per_minute:5,per_hour:100,eta_h:2}}};
  vm.runInContext(section('function renderScraper()', 'let listsAll ='),c);c.renderScraper();
  const lists=c.$('#stages').innerHTML.split('2. Read bios')[0];
  assert.match(lists,/100 list entries saved/);
  assert.match(lists,/60 left in active lists/);
  assert.match(lists,/2 incomplete lists \(about 140 entries missing\)/);
  assert.match(lists,/1 capped list/);
  assert.match(lists,/Active lists: about 2 h left/);
  assert.doesNotMatch(lists,/bar-p|people collected|Each extra Instagram account/);
  assert.match(c.$('#now').innerHTML,/list entries saved so far/);
  delete c.S.sc.progress.lists.incomplete_lists;
  delete c.S.sc.progress.lists.incomplete_left;
  delete c.S.sc.progress.lists.capped_lists;
  c.renderScraper();
  const legacy=c.$('#stages').innerHTML.split('2. Read bios')[0];
  assert.match(legacy,/2 incomplete lists/);
  assert.doesNotMatch(legacy,/entries missing|capped list|people collected/);
});
test('controls expose failed action and disable stale controls',async()=>{
  const el={isConnected:true,innerHTML:'',dataset:{},setAttribute(){},addEventListener(){},querySelectorAll:()=>[],querySelector:()=>null,contains:()=>false};
  let offline=false,postFail=false;
  const data={all_paused:false,stages:[{id:'lists',label:'Lists',state:'running',paused:false,now:'Reading',help:'Pause lists'}]};
  let code=readFileSync(new URL('../../web/controls.js',import.meta.url),'utf8');
  code=code.replace('  function start() {','  window.testControls = {load, send};\n  function start() {');
  const c=vm.createContext({window:{},document:{createElement:()=>el,readyState:'loading',addEventListener(){}},fetch:async(u,o)=>{if(offline)throw Error();return {ok:!(o?.method&&postFail),json:async()=>o?.method&&postFail?{error:'failed'}:data};},setInterval(){},clearInterval(){},Date,confirm:()=>true});
  vm.runInContext(code,c);await c.window.testControls.load();postFail=true;await c.window.testControls.send({stage:'lists',action:'pause'});
  assert.match(el.innerHTML,/Couldn’t confirm pause/);assert.doesNotMatch(el.innerHTML,/disabled/);
  offline=true;await c.window.testControls.load();assert.match(el.innerHTML,/disabled/);
});
test('demo implements controls and qualification',async()=>{
  const w={fetch:async()=>{throw Error('unexpected network')}};
  const c=vm.createContext({window:w,location:{origin:'http://demo'},URL,URLSearchParams,Response,console,setInterval(){},setTimeout:fn=>fn()});
  vm.runInContext(readFileSync(new URL('../../web/mock.js',import.meta.url),'utf8'),c);
  const control=await (await w.fetch('/api/control')).json();assert.equal(control.stages.length,3);
  const paused=await (await w.fetch('/api/control',{method:'POST',body:JSON.stringify({stage:'all',action:'pause'})})).json();assert.equal(paused.all_paused,true);
  const qual=await (await w.fetch('/api/qual?view=all&limit=3')).json();assert.equal(qual.rows.length,3);assert.ok(qual.summary.verdicts>0);
  assert.ok(qual.rows.every(row => Array.isArray(row.connection_edges)));
});
