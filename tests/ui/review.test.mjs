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
  return vm.createContext({$,elements,S:{rows:[],pick:new Set(),tagBy:new Map(),gen:1,nextOffset:0,rev:null,loading:false,error:false,done:false,views:[]},M:{byId:new Map(),patch(){},resize(){}},api:{},renderRows(){},renderDetail(){},renderFilters(){},renderBulk(){},toast(){},loadFacetsSoon(){},loadCounts(){},refreshPerson(){},resetLeads(){},PAGE:100,URLSearchParams,toQuery:()=>new URLSearchParams(),LeadWorkflow:{runtimeQuery:q=>q},int:String,ucf:String,slabel:String,esc:s=>String(s??'').replaceAll('<','&lt;'),plural:(n,s)=>`${n} ${s}`,safeUrl:u=>/^https?:\/\//.test(u||'')?u:null,setTimeout:()=>1,clearTimeout(){},...extra});
}
const pagination=section('async function resetLeads(', 'const rowH =');
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
test('website evidence escapes tags and suppresses stale claims',()=>{
  const c=base();vm.runInContext(section('function websiteEvidence(', 'const evidenceOf ='),c);const site={url:'https://example.org',tags:['<unsafe>','Has shop'],summary:'Current shop',at:'2026-09-26',stale:false};assert.match(c.websiteEvidence(site),/Website evidence/);assert.match(c.websiteEvidence(site),/&lt;unsafe>/);site.stale=true;const out=c.websiteEvidence(site);assert.doesNotMatch(out,/Has shop|Current shop|&lt;unsafe>/);assert.match(out,/Older website evidence/);
});
