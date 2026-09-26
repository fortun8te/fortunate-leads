import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../../web/app.js',import.meta.url),'utf8');
const section=(a,b)=>{const i=source.indexOf(a),j=source.indexOf(b,i);assert.ok(i>=0&&j>i,`Missing source section ${a}`);return source.slice(i,j);};
const tick=()=>new Promise(r=>setImmediate(r));
function base(extra={}) {
  const elements=new Map();
  const $=key=>{if(!elements.has(key)) elements.set(key,{hidden:false,innerHTML:'',textContent:'',style:{},dataset:{},scrollTop:0,clientHeight:640,classList:{toggle(){}},setAttribute(k,v){this[k]=v},focus(){},setSelectionRange(){}});return elements.get(key)};
  return vm.createContext({$,elements,S:{rows:[],pick:new Set(),tagBy:new Map(),gen:1,nextOffset:0,rev:null,loading:false,error:false,done:false,views:[]},M:{byId:new Map(),patch(){},resize(){}},api:{},renderRows(){},renderDetail(){},renderFilters(){},renderBulk(){},toast(){},loadFacetsSoon(){},loadCounts(){},refreshPerson(){},resetLeads(){},PAGE:100,URLSearchParams,toQuery:()=>new URLSearchParams(),int:String,ucf:String,slabel:String,esc:s=>String(s??'').replaceAll('<','&lt;'),plural:(n,s)=>`${n} ${s}`,safeUrl:u=>/^https?:\/\//.test(u||'')?u:null,setTimeout:()=>1,clearTimeout(){},...extra});
}
const pagination=section('async function resetLeads(', 'const rowH =');
const notes=section('const noteSaves =', "$('#detail').addEventListener('input'");
const bulk=section('async function bulk(', '// ---------- marking');
test('refresh of loaded rows makes exactly one replacement request despite auto paging during render',async()=>{
  const calls=[];let c;c=base({renderRows(){if(c.S.rows.length&&!c.S.done)c.loadMore();},api:{get:async url=>{calls.push(url);return {rows:[{id:7}],total:1,rev:'b'};}}});
  c.S.rows=[{id:1}];vm.runInContext(pagination,c);await c.resetLeads(true);
  assert.equal(calls.length,1);assert.deepEqual(Array.from(c.S.rows,r=>r.id),[7]);assert.equal(c.S.nextOffset,1);
});
test('pagination failure remains paused until explicit retry, preserves rows and raw offset',async()=>{
  const calls=[];let fail=true;const c=base({api:{get:async url=>{calls.push(url);if(fail)throw Error('offline');return {rows:[{id:2},{id:3}],total:4,rev:'b',next_offset:4,has_more:false};}}});
  c.S.rows=[{id:1},{id:2}];c.S.nextOffset=2;c.S.rev='a';vm.runInContext(pagination,c);
  await c.loadMore();await c.loadMore();assert.equal(calls.length,1);assert.equal(c.S.nextOffset,2);assert.equal(c.S.rows.length,2);
  fail=false;c.S.error=false;await c.loadMore();assert.equal(calls.length,2);assert.match(calls[1],/offset=2/);assert.deepEqual(Array.from(c.S.rows,r=>r.id),[1,2,3]);assert.equal(c.S.nextOffset,4);assert.equal(c.S.stale,true);assert.equal(c.S.done,true);
});
test('late response from old filter cannot overwrite current results',async()=>{
  let finish;const c=base({api:{get:()=>new Promise(r=>finish=r)}});vm.runInContext(pagination,c);const pending=c.loadMore();c.S.gen=2;c.S.rows=[{id:99}];finish({rows:[{id:1}],total:1});await pending;assert.equal(c.S.rows[0].id,99);
});
test('closing a profile without any note job is safe',()=>{
  const c=base({noteSaves:new Map()});c.S.open=1;vm.runInContext(section('function closeDetail()', '// One row per seed'),c);c.closeDetail();assert.equal(c.S.open,null);assert.equal(c.$('#detail').hidden,true);
});
test('failed in-flight note retains newest draft and requires retry without looping',async()=>{
  const requests=[];const c=base({api:{post:(_u,body)=>new Promise((resolve,reject)=>requests.push({body,resolve,reject}))}});vm.runInContext(notes,c);
  vm.runInContext("saveNote(1,'old');flushNote(1,noteSaves.get(1));saveNote(1,'newest')",c);requests[0].reject(Error('offline'));await tick();
  assert.equal(requests.length,1);assert.equal(vm.runInContext('noteSaves.get(1).draft',c),'newest');assert.equal(vm.runInContext('noteSaves.get(1).error',c),true);
  vm.runInContext('noteSaves.get(1).error=false;flushNote(1,noteSaves.get(1))',c);assert.equal(requests[1].body.note,'newest');requests[1].resolve({mark_rev:'r2'});await tick();assert.equal(vm.runInContext('noteSaves.get(1).draft',c),null);
});
test('note conflict preserves draft and current saved note until explicit retry',async()=>{
  const requests=[];const c=base({api:{post:(_u,body)=>new Promise((resolve,reject)=>requests.push({body,resolve,reject}))}});c.S.person={id:1,mark_rev:'r1'};vm.runInContext(notes,c);
  vm.runInContext("saveNote(1,'mine');flushNote(1,noteSaves.get(1))",c);
  requests[0].reject(Object.assign(Error('conflict'),{status:409,detail:{current:{note:'theirs',mark_rev:'r2'}}}));await tick();
  vm.runInContext("saveNote(1,'mine edited');flushNote(1,noteSaves.get(1))",c);assert.equal(requests.length,1);assert.equal(vm.runInContext('noteSaves.get(1).conflict.note',c),'theirs');
  vm.runInContext('noteSaves.get(1).error=false;noteSaves.get(1).conflict=null;flushNote(1,noteSaves.get(1))',c);assert.equal(requests[1].body.note,'mine edited');assert.equal(requests[1].body.if_match,'r2');requests[1].resolve({mark_rev:'r3'});await tick();
});
test('bulk operations serialize and preserve nonmanual tags',async()=>{
  const requests=[];const c=base({api:{post:(_u,body)=>new Promise(resolve=>requests.push({body,resolve}))}});c.S.rows=[{id:1,tags:[{tag:'Founder',source:'auto'},{tag:'Custom',source:'manual'}]}];vm.runInContext('let bulkKey="";'+bulk,c);
  const a=c.bulk({remove:['Founder','Custom']},[1]);const b=c.bulk({status:'client'},[1]);await tick();assert.equal(requests.length,1);requests[0].resolve({updated:1});await a;await tick();assert.equal(requests.length,2);assert.equal(c.S.rows[0].tags.length,1);assert.equal(c.S.rows[0].tags[0].source,'auto');requests[1].resolve({updated:1});await b;
});
test('partial or unknown bulk counts refresh rather than falsely patching everyone',async()=>{
  const messages=[];let refreshed=0;const c=base({api:{post:async()=>({updated:1})},toast:(...a)=>messages.push(a),resetLeads:()=>refreshed++});c.S.rows=[{id:1,status:null},{id:2,status:null}];vm.runInContext('let bulkKey="";'+bulk,c);await c.bulk({status:'client'},[1,2]);assert.equal(refreshed,1);assert.equal(c.S.rows[0].status,null);assert.match(messages[0][0],/1 of 2/);assert.equal(messages[0][1],undefined);
  c.api.post=async()=>({});await c.bulk({status:'client'},[1]);assert.equal(refreshed,2);assert.match(messages[1][0],/confirm how many/);
});
test('bulk cap rejects oversized requests before sending',async()=>{
  let called=false;const c=base({api:{post:async()=>{called=true}}});vm.runInContext('let bulkKey="";'+bulk,c);await c.bulk({status:'client'},Array.from({length:5001},(_,i)=>i));assert.equal(called,false);
});
test('select all respects cap even when more than 5000 rows are already loaded and blocks stale pages',async()=>{
  let called=false;const c=base({fetchLeads:async()=>{called=true}});c.S.rows=Array.from({length:5100},(_,i)=>({id:i}));c.S.total=5100;c.S.nextOffset=5100;vm.runInContext(section('async function pickAllInFilter()', 'function clearPick()'),c);await c.pickAllInFilter();assert.equal(c.S.pick.size,5000);assert.equal(called,false);c.S.pick.clear();c.S.stale=true;await c.pickAllInFilter();assert.equal(c.S.pick.size,0);
});
test('saved view draft changed during save survives response and duplicate submits are ignored',async()=>{
  let finish,calls=0;const c=base({api:{post:()=>{calls++;return new Promise(r=>finish=r)}},loadViews:async()=>{}});vm.runInContext(section('async function saveView(', 'async function deleteView('),c);c.S.saving=true;const a=c.saveView('First');c.S.viewName='Second draft';await c.saveView('Second draft');assert.equal(calls,1);finish({});await a;assert.equal(c.S.viewName,'Second draft');assert.equal(c.S.saving,true);
});
test('late saved view response cannot replace newest response',async()=>{
  const requests=[];const c=base({store:{get:()=>[]},api:{get:()=>new Promise(r=>requests.push(r))}});vm.runInContext(section('async function loadViews()', '// ---------- sidebar'),c);const a=c.loadViews(),b=c.loadViews();requests[1]([{id:2,name:'New'}]);await b;requests[0]([{id:1,name:'Old'}]);await a;assert.equal(c.S.views[0].name,'New');
});
test('website evidence escapes tags and suppresses stale claims',()=>{
  const c=base();vm.runInContext(section('function websiteEvidence(', 'const evidenceOf ='),c);const site={url:'https://example.org',tags:['<unsafe>','Has shop'],summary:'Current shop',at:'2026-09-26',stale:false};assert.match(c.websiteEvidence(site),/Website evidence/);assert.match(c.websiteEvidence(site),/&lt;unsafe>/);site.stale=true;const out=c.websiteEvidence(site);assert.doesNotMatch(out,/Has shop|Current shop|&lt;unsafe>/);assert.match(out,/Older website evidence/);
});
