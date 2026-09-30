import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const start = source.indexOf('function scoutHTML(');
const end = source.indexOf('function websiteEvidence(', start);
const context = vm.createContext({esc: (s) => String(s ?? '').replaceAll('<', '&lt;'), safeUrl: (s) => /^https?:\/\//.test(s) ? s : null, URL});
vm.runInContext(source.slice(start, end), context);
test('owner override makes old research historical while preserving the cited source', () => {
  const html = context.scoutHTML({overridden_by_owner:true, verdict:'no', reachable:false, summary:'Earlier assessment', sources:['https://example.com/about']});
  assert.match(html, /Earlier research/);
  assert.match(html, /relationship update takes priority/);
  assert.match(html, /Earlier assessment/);
  assert.match(html, /href="https:\/\/example.com\/about"/);
  assert.doesNotMatch(html, /not reachable|Not a lead/);
});
const marking = source.slice(source.indexOf('async function markNow('), source.indexOf('function patchRow('));
test('saving a relationship waits for full detail readback before finishing', async () => {
  const calls = [];
  let finishRead;
  const ctx = vm.createContext({S:{rows:[{id:7,status:null}],open:7},invalidatePersonRead:()=>{},patchRow:(_id,p)=>calls.push(['patch',p.status]),api:{post:async()=>calls.push(['saved'])},loadCounts:()=>{},loadFacetsSoon:()=>{},slabel:s=>s,mark:()=>{},refreshActivity:()=>calls.push(['activity']),refreshPerson:()=>new Promise(resolve=>{calls.push(['read']);finishRead=resolve}),toast:()=>{}});
  vm.runInContext(marking, ctx);
  let done=false;
  const pending=ctx.markNow(7,'client').then(()=>{done=true});
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(calls,[['patch','client'],['saved'],['read']]);
  assert.equal(done,false);
  finishRead(); await pending;
  assert.equal(done,true);
});
test('failed relationship save restores prior status without starting a detail read', async () => {
  const calls=[];
  const ctx=vm.createContext({S:{rows:[{id:7,status:'talking'}],open:7},invalidatePersonRead:()=>{},patchRow:(_id,p)=>calls.push(p.status),api:{post:async()=>{throw Error('offline')}},loadCounts:()=>{},loadFacetsSoon:()=>{},refreshActivity:()=>{},refreshPerson:()=>{throw Error('must not refresh')},slabel:x=>x,mark:()=>{},toast:(m)=>m==='Could not save'&&calls.push('error')});
  vm.runInContext(marking,ctx);
  await ctx.markNow(7,'no');
  assert.deepEqual(calls,['no','talking','error']);
});

const personRefresh = source.slice(source.indexOf('const personReads ='), source.indexOf('function closeDetail()'));
test('older detail responses cannot restore a pre-save relationship or note', async () => {
  const requests = [];
  const ctx = vm.createContext({S:{open:7,person:{id:7},rows:[{id:7}]},Map,setTimeout,clearTimeout,document:{hidden:false},api:{get:()=>new Promise(resolve=>requests.push(resolve))},noteQueue:{reconcile:()=>{}},renderDetail:()=>{},renderRows:()=>{}});
  vm.runInContext(personRefresh,ctx);
  const old = ctx.refreshPerson(7);
  ctx.invalidatePersonRead(7);
  const fresh = ctx.refreshPerson(7);
  assert.equal(requests.length,2,'post-save read must make a new request');
  requests[1]({id:7,status:'client',note:'New note',mark_rev:'r2'}); await fresh;
  requests[0]({id:7,status:null,note:'Old note',mark_rev:'r1'}); await old;
  assert.equal(ctx.S.person.status,'client');
  assert.equal(ctx.S.person.note,'New note');
});
test('note suggestions show exact quotes and only offer a non-conflicting relationship',()=>{
  const ctx=vm.createContext({noteQueue:{peek:()=>null},esc:String,slabel:s=>({client:'Client',talking:'Talking',no:'Not a fit'}[s])});
  vm.runInContext(source.slice(source.indexOf('const HUMAN_RELATIONSHIPS ='),source.indexOf('function renderNoteState(')),ctx);
  const person={id:1,note:'He is my client.',note_interpretation:{state:'ready',facts:[{kind:'current_client',label:'Current client',quote:'He is my client.'}]}};
  assert.match(ctx.noteInsightsHTML(person),/He is my client.*Set Client/);
  assert.equal(ctx.noteInsightsHTML({...person,status:'client'}),'');
  assert.doesNotMatch(ctx.noteInsightsHTML({...person,status:'no'}),/data-s=/);
  assert.match(ctx.noteInsightsHTML({...person,note_interpretation:{state:'unavailable'}}),/Note saved/);
});
test('failed note offers explicit retry only when server permits it',async()=>{
 const calls=[],person={id:7,note:'We worked together.',note_interpretation:{state:'failed',can_retry:true,message:'Could not understand this note. Your note is saved.'}};
 const ctx=vm.createContext({S:{person},noteQueue:{peek:()=>null,flush:async()=>{}},notePolls:new Map([[7,{attempts:20}]]),esc:String,renderNoteState(){},api:{post:async(path,body)=>calls.push({path,body})},refreshPerson:async()=>calls.push('refresh'),toast:s=>calls.push(s)});
 vm.runInContext(source.slice(source.indexOf('const HUMAN_RELATIONSHIPS ='),source.indexOf('function renderNoteState(')),ctx);
 assert.match(ctx.noteInsightsHTML(person),/data-note-retry.*Retry reading/);
 assert.doesNotMatch(ctx.noteInsightsHTML({...person,note_interpretation:{...person.note_interpretation,can_retry:false}}),/data-note-retry/);
 await ctx.retryNoteRead(7);
 assert.deepEqual(JSON.parse(JSON.stringify(calls)),[{path:'/api/person/7/note-retry',body:{}},'refresh']);
 assert.equal(ctx.notePolls.get(7).attempts,0);
});

test('edited ranking refresh polls briefly, stops on completion and restarts for another edit', async () => {
  const timers=[];
  let person={id:7,note:null,mark_rev:'r1',ranking_pending:true};
  const ctx=vm.createContext({S:{open:7,person:{id:7},rows:[{id:7}]},Map,
    setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},clearTimeout(){},
    document:{hidden:false},api:{get:async()=>({...person})},noteQueue:{reconcile(){}},renderDetail(){},renderRows(){}});
  vm.runInContext(personRefresh,ctx);
  for(let i=0;i<22;i++) await ctx.refreshPerson(7);
  assert.equal(timers.length,20,'bounded polling never spins indefinitely');
  assert.equal(timers[0].delay,3000);
  person.mark_rev='r2';
  await ctx.refreshPerson(7);
  assert.equal(timers.length,21,'another owner edit restarts its brief refresh');
  person.ranking_pending=false;person.score=88;
  await ctx.refreshPerson(7);
  assert.equal(timers.length,21,'completed ranking stops polling');
  assert.equal(ctx.S.rows[0].score,88,'updated rank reaches the visible row');
});
test('third-party note story links only selected profiles and offers no relationship action', () => {
  const ctx = vm.createContext({ noteQueue:{peek:()=>null}, esc:String });
  vm.runInContext(readFileSync(new URL('../../web/note-mentions.js', import.meta.url), 'utf8'), ctx);
  vm.runInContext(source.slice(source.indexOf('const HUMAN_RELATIONSHIPS ='),source.indexOf('function renderNoteState(')),ctx);
  const person = {id:1, note:'He is friends with @alex.', note_mentions:[{person_id:2,token:'@alex',handle:'alex_new',name:'Alex'}, {person_id:3,token:'@ali',handle:'ali'}],
    note_interpretation:{state:'ready', facts:[{kind:'mentioned_connection', label:'Connection described in your note', quote:'He is friends with @alex.'}]}};
  const html = ctx.noteInsightsHTML(person);
  assert.match(html, /data-note-profile="2"/);
  assert.match(html, /@alex_new · Alex/);
  assert.doesNotMatch(html, /data-note-profile="3"|data-note-fact|Set Friend/);
});

test('marking a status is optimistic, offers Undo and restores the previous status', async () => {
  const toasts=[], marks=[];
  const ctx=vm.createContext({S:{rows:[{id:7,handle:'ana',status:'contacted'}],open:null},invalidatePersonRead:()=>{},patchRow:()=>{},api:{post:async()=>({})},loadCounts:()=>{},loadFacetsSoon:()=>{},refreshActivity:()=>{},refreshPerson:()=>{},slabel:x=>x[0].toUpperCase()+x.slice(1),mark:(...a)=>marks.push(a),toast:(m,undo)=>toasts.push([m,undo])});
  vm.runInContext(marking,ctx);
  const pending=ctx.markNow(7,'talking');
  assert.equal(toasts.length,1,'feedback is shown before the save returns');
  assert.equal(toasts[0][0],'@ana marked Talking');
  toasts[0][1](); assert.equal(JSON.stringify(marks),JSON.stringify([[7,'contacted',{quiet:true}]]));
  await pending;
});
