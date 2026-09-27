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
  const ctx = vm.createContext({S:{rows:[{id:7,status:null}],open:7},invalidatePersonRead:()=>{},patchRow:(_id,p)=>calls.push(['patch',p.status]),api:{post:async()=>calls.push(['saved'])},loadCounts:()=>{},loadFacetsSoon:()=>{},refreshActivity:()=>calls.push(['activity']),refreshPerson:()=>new Promise(resolve=>{calls.push(['read']);finishRead=resolve}),toast:()=>{}});
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
  const ctx=vm.createContext({S:{rows:[{id:7,status:'talking'}],open:7},invalidatePersonRead:()=>{},patchRow:(_id,p)=>calls.push(p.status),api:{post:async()=>{throw Error('offline')}},loadCounts:()=>{},loadFacetsSoon:()=>{},refreshActivity:()=>{},refreshPerson:()=>{throw Error('must not refresh')},toast:()=>calls.push('error')});
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
  vm.runInContext(source.slice(source.indexOf('function noteInsightsHTML('),source.indexOf('function renderNoteState(')),ctx);
  const person={id:1,note:'He is my client.',note_interpretation:{state:'ready',facts:[{kind:'current_client',label:'Current client',quote:'He is my client.'}]}};
  assert.match(ctx.noteInsightsHTML(person),/He is my client.*Set Client/);
  assert.equal(ctx.noteInsightsHTML({...person,status:'client'}),'');
  assert.doesNotMatch(ctx.noteInsightsHTML({...person,status:'no'}),/data-s=/);
  assert.match(ctx.noteInsightsHTML({...person,note_interpretation:{state:'unavailable'}}),/Note saved/);
});
