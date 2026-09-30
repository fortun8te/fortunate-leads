import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const code = source.slice(source.indexOf('const HUMAN_RELATIONSHIPS ='), source.indexOf('function noteInsightsHTML('));
function harness(person) {
 const calls=[];
 const context=vm.createContext({S:{person},esc:String,STATUSES:['interested','contacted','talking','spoke_before','no'],slabel:s=>s,serializeMutation:fn=>fn(),noteQueue:{flush:async()=>calls.push('flush')},invalidatePersonRead(){},api:{post:async(path,body)=>calls.push(body)},refreshPerson:async()=>calls.push('refresh'),loadCounts(){},loadFacetsSoon(){},toast:msg=>calls.push(msg)});
 vm.runInContext(code,context); return {context,calls};
}
test('history and familiarity do not invent a conversation stage',()=>{
 const {context}=harness({});
 const html=context.humanRelationshipHTML({status:'client',familiarity:null});
 assert.match(html,/d-relationship-client[^>]*aria-pressed="true"/);
 assert.match(html,/d-relationship-worked_with[^>]*aria-pressed="true"/);
 assert.doesNotMatch(html,/d-status-client/);
 assert.match(html,/How well\? <em>Optional<\/em>/);
 assert.doesNotMatch(html,/data-familiarity="[^"]+" aria-pressed="true"/);
 assert.match(html,/d-status-spoke_before/);
});
test('adding friend keeps work history and stage and saves against current revision',async()=>{
 const {context,calls}=harness({id:7,relationships:['worked_with'],status:'talking',mark_rev:'r2'});
 await context.updateHumanRelationship(7,'relationships','friend');
 assert.equal(calls[0],'flush');
 assert.deepEqual(JSON.parse(JSON.stringify(calls[1])),{relationships:['worked_with','friend'],if_match:'r2'});
 assert.equal(calls[2],'refresh');
});
test('clearing worked with clears its client refinement; familiarity toggles clear',async()=>{
 const {context,calls}=harness({id:7,relationships:['worked_with','client','friend'],familiarity:'close',mark_rev:'r3'});
 await context.updateHumanRelationship(7,'relationships','worked_with');
 assert.deepEqual(JSON.parse(JSON.stringify(calls[1])),{relationships:['friend'],if_match:'r3'});
 await context.updateHumanRelationship(7,'familiarity','close');
 assert.deepEqual(JSON.parse(JSON.stringify(calls[4])),{familiarity:null,if_match:'r3'});
});
test('conflicting save refreshes and never pretends relationship was saved',async()=>{
 const {context,calls}=harness({id:7,relationships:[],mark_rev:'r1'});
 context.api.post=async()=>{throw Object.assign(Error('conflict'),{status:409})};
 await context.updateHumanRelationship(7,'relationships','friend');
 assert.deepEqual(calls,['flush','refresh','This profile changed. Review it and try again.']);
 assert.deepEqual(context.S.person.relationships,[]);
});
test('note confirmation unions relationships and ignores injected non-enum fields',async()=>{
 const fact={kind:'friend',label:'Friend',quote:'We are friends.',relationships:['client']};
 const person={id:7,relationships:['colleague'],mark_rev:'r4',note:'We are friends.',note_interpretation:{state:'ready',facts:[fact]}};
 const {context,calls}=harness(person);
 await context.applyNoteFact(7,0);
 assert.deepEqual(JSON.parse(JSON.stringify(calls[1])),{relationships:['colleague','friend'],if_match:'r4'});
 assert.equal(context.noteFactPatch({...person,status:'talking'},{kind:'spoke_before'}),null,'note cannot replace explicit current conversation');
});
test('note confirmation rejects stale quote after note changes',async()=>{
 const person={id:7,relationships:[],note:'We are friends.',note_interpretation:{state:'ready',facts:[{kind:'friend',quote:'We are friends.'}]}};
 const {context,calls}=harness(person);
 context.noteQueue.flush=async()=>{person.note='We are not friends.'};
 await context.applyNoteFact(7,0);
 assert.deepEqual(calls,[]);
});
test('canonical relationship labels have no global rename/delete controls',()=>{
 const elements=new Map();
 const ctx=vm.createContext({$:key=>{if(!elements.has(key))elements.set(key,{});return elements.get(key)},int:String,esc:String,KIND:{manual:'manual'},tagStyleAttrs:()=>'',tagContent:t=>t.tag});
 vm.runInContext(source.slice(source.indexOf('const T = {'), source.indexOf("$('#tg-q').addEventListener"))+';globalThis.manager=T;',ctx);
 ctx.manager.renderGroups=()=>{};
 ctx.manager.list=[{tag:'Client',grp:'relationship',sources:['manual'],kind:'manual',total:5},{tag:'Friend',grp:'relationship',sources:['manual'],kind:'manual',total:4},{tag:'Met at studio',grp:'custom',sources:['manual'],kind:'manual',total:1}];
 ctx.manager.render();
 const html=elements.get('#tg-body').innerHTML;
 assert.doesNotMatch(html,/data-(?:edit|del)="(?:Client|Friend)"/);
 assert.match(html,/data-edit="Met at studio"/);
 assert.match(html,/data-del="Met at studio"/);
 assert.equal(ctx.manager.editable({grp:'relationship',sources:['manual']}),false);
});
