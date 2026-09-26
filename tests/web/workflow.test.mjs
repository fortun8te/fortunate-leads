import test from 'node:test';
import assert from 'node:assert/strict';
import '../../web/workflow.js';
const { createNoteQueue, runtimeQuery, localToday } = globalThis.LeadWorkflow;
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return { promise, resolve, reject }; };
const tick = () => new Promise((resolve) => setImmediate(resolve));
test('notes are serialized per person and an old response never saves a newer draft', async () => {
  const calls=[], first=deferred(), second=deferred();
  const queue=createNoteQueue({delay:100000, save:(id,note) => {calls.push([id,note]);return calls.length===1?first.promise:second.promise;}});
  queue.edit(1,'first'); const done=queue.flush(1); await tick();
  queue.edit(1,'latest'); assert.equal(calls.length,1);
  first.resolve(); await tick();
  assert.deepEqual(calls,[[1,'first'],[1,'latest']]);
  assert.equal(queue.state(1).draft,'latest'); assert.equal(queue.state(1).saved,'first'); assert.equal(queue.dirty(),true);
  second.resolve(); await done; assert.equal(queue.dirty(),false); await queue.flushAll();
});
test('switching people preserves and flushes both independent drafts', async () => {
  const saved=[]; const queue=createNoteQueue({delay:100000,save:async(id,note)=>saved.push([id,note])});
  queue.edit(1,'one');queue.edit(2,'two');await queue.flushAll();
  assert.deepEqual(saved.sort(),[[1,'one'],[2,'two']]);assert.equal(queue.state(1).draft,'one');
});
test('failed writes remain scoped, recoverable and block export flush', async () => {
  let fail=true;const queue=createNoteQueue({delay:100000,save:async(id)=>{if(id===1 && fail)throw Error('offline');}});
  queue.edit(1,'keep');queue.edit(2,'safe');await assert.rejects(queue.flushAll(),/offline/);
  assert.deepEqual(queue.snapshot(),{1:'keep'});assert.equal(queue.state(2).error,null);
  fail=false;await queue.flushAll();assert.equal(queue.dirty(),false);
});
test('restored drafts survive reload and can be flushed', async () => {
  const saved=[];const queue=createNoteQueue({initial:{42:'recovered'},save:async(id,note)=>saved.push([id,note])});
  assert.equal(queue.state(42,'server').draft,'recovered');await queue.flushAll();assert.deepEqual(saved,[[42,'recovered']]);
});
test('relative queries receive the current local day without modifying saved views', () => {
  const saved=new URLSearchParams('follow_up=due&sort=follow_up&today=2000-01-01');
  assert.equal(runtimeQuery(saved,new Date(2026,8,26,23,59)).get('today'),'2026-09-26');
  assert.equal(runtimeQuery(saved,new Date(2026,8,27,0,1)).get('today'),'2026-09-27');
  assert.equal(saved.get('today'),'2000-01-01');assert.equal(localToday(new Date(2026,0,2)),'2026-01-02');
});

test('actual saved-view query roundtrip preserves follow-up but never freezes today', async () => {
  const { readFile } = await import('node:fs/promises');
  const vm = await import('node:vm');
  const source = await readFile(new URL('../../web/app.js', import.meta.url), 'utf8');
  const functions = source.slice(source.indexOf('function toQuery('), source.indexOf('const modeOf ='));
  const context = vm.createContext({ URLSearchParams, FIT_TIER: { strong: 'hot' }, TIER_FIT: { hot: 'strong' } });
  const result = vm.runInContext(functions + `\nconst parsed = fromQuery('follow_up=overdue&today=2020-01-01&sort=follow_up'); ({query:toQuery(parsed.f,parsed.sort).toString(), count:filterCount(parsed.f), filter:parsed.f.follow_up});`, context);
  assert.equal(result.query, 'follow_up=overdue&sort=follow_up');
  assert.equal(result.count, 1); assert.equal(result.filter, 'overdue');
});

test('loaded notes replace clean placeholder state without replacing an unsaved draft', () => {
  const queue=createNoteQueue({save:async()=>{}});
  queue.state(1,'');queue.reconcile(1,'Existing note');
  assert.equal(queue.state(1).draft,'Existing note');
  queue.reconcile(1,'Updated elsewhere');assert.equal(queue.state(1).draft,'Updated elsewhere');
  const restored=createNoteQueue({initial:{1:'Unsaved draft'},save:async()=>{}});
  restored.reconcile(1,'Server note');assert.equal(restored.state(1).draft,'Unsaved draft');
});
test('reverting to the old saved value while a write is pending still persists and warns', async () => {
  const gate=deferred(), calls=[];
  const queue=createNoteQueue({delay:100000,save:async(id,note)=>{calls.push(note);if(calls.length===1)await gate.promise;}});
  queue.state(1,'A');queue.edit(1,'B');const flushed=queue.flush(1);await tick();
  queue.edit(1,'A');assert.equal(queue.dirty(),true);assert.deepEqual(queue.snapshot(),{1:'A'});
  gate.resolve();await flushed;await queue.flushAll();assert.deepEqual(calls,['B','A']);assert.equal(queue.dirty(),false);
});

test('refreshing retained filtered rows fetches from zero and renders No matches when empty', async () => {
  const { readFile } = await import('node:fs/promises');
  const vm = await import('node:vm');
  const source = await readFile(new URL('../../web/app.js', import.meta.url), 'utf8');
  const listFunctions = source.slice(source.indexOf('async function resetLeads('), source.indexOf('const rowH ='));
  const renderFunction = source.slice(source.indexOf('function renderRows() {'), source.indexOf("$('#scroll').addEventListener('scroll'"));
  const state = { gen:0, rows:[{id:1}], total:1, done:true, loading:false, pick:new Set(), f:{follow_up:'due'}, sort:'follow_up' };
  const nodes = new Map();
  const select = (name) => { if (!nodes.has(name)) nodes.set(name, {style:{},classList:{toggle(){}},scrollTop:0,clientHeight:800,innerHTML:'',setAttribute(){},querySelector:()=>null,contains:()=>false});return nodes.get(name); };
  const urls = [];
  const context = vm.createContext({
    S:state,PAGE:100,$:select,document:{activeElement:null},CSS:{escape:String},LeadWorkflow:globalThis.LeadWorkflow,URLSearchParams,
    api:{get:async(url)=>{urls.push(url);return {rows:[],total:0};}},
    toQuery:()=>new URLSearchParams('follow_up=due'), rowH:()=>64, plural:(n)=>String(n), int:String,
    renderBulk(){}, exporting:false, rowHTML:()=>'<div>Old row</div>', filterCount:()=>1, offlineSince:null,
  });
  await vm.runInContext(listFunctions + renderFunction + '\nresetLeads(true);',context);
  assert.equal(urls.length,1,'only the replacement request is launched');
  assert.equal(new URL(urls[0],'http://localhost').searchParams.get('offset'),'0');
  assert.equal(state.rows.length,0); assert.equal(state.total,0); assert.equal(state.done,true);
  assert.match(select('#rows').innerHTML,/No matches/);assert.doesNotMatch(select('#rows').innerHTML,/Old row/);
});

test('activity edits are retained independently while the follow-up form is absent', async () => {
  const { readFile } = await import('node:fs/promises');const vm=await import('node:vm');
  const source=await readFile(new URL('../../web/app.js',import.meta.url),'utf8');
  const fn=source.slice(source.indexOf('function rememberWorkflowForm() {'),source.indexOf('function localDateTime()'));
  const drafts=new Map([[7,{kind:'dm',body:'old'}]]);
  const fields={'#activity-form':{dataset:{owner:'7'}},'#activity-kind':{value:'call'},'#activity-body':{value:'New draft during refresh'},'#activity-when':{value:'2026-09-26T12:00'}};
  vm.runInNewContext(fn+'\nrememberWorkflowForm();',{workflowDrafts:drafts,$:(s)=>fields[s]||null});
  assert.equal(drafts.get(7).body,'New draft during refresh');assert.equal(drafts.get(7).kind,'call');assert.equal('due' in drafts.get(7),false);
});

test('filtered export explicitly sends Best fit sorting even when saved queries omit it', async () => {
  const {readFile}=await import('node:fs/promises');const vm=await import('node:vm');
  const source=await readFile(new URL('../../web/app.js',import.meta.url),'utf8');
  const fn=source.slice(source.indexOf('async function exportLeads('),source.indexOf("$('#export-filtered').onclick"));
  let exported;
  const state={pick:new Set(),sort:'fit'};
  const nodes=new Map();const select=(s)=>{if(!nodes.has(s))nodes.set(s,{});return nodes.get(s);};
  const context=vm.createContext({S:state,exporting:false,LeadWorkflow:globalThis.LeadWorkflow,toQuery:()=>new URLSearchParams(),$:select,noteQueue:{flushAll:async()=>{}},fetch:async(url,options)=>{exported=JSON.parse(options.body);return {ok:false,status:500,json:async()=>({error:'stop after capturing request'})};}});
  await vm.runInContext(fn+'\nexportLeads(false);',context);
  assert.equal(new URLSearchParams(exported.query).get('sort'),'fit');
});
