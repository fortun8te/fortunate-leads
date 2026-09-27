import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../../web/app.js',import.meta.url),'utf8');
const c=vm.createContext({int:String,esc:String});
vm.runInContext(source.slice(source.indexOf('function collectionCoverageHTML('),source.indexOf('function renderAccounts()')),c);
test('coverage separates list attempts from unknown targets and unique people',()=>{
 const html=c.collectionCoverageHTML({coverage:{lists:{saved_entries:600,expected_entries:500,known_targets:1,unknown_targets:2,total_lists:3}},local_laya:true,qualify:false,progress:{lists:{per_minute:12}}});
 assert.match(html,/600 this attempt/);assert.match(html,/500 expected in 1 known lists/);assert.match(html,/2 targets unknown/);assert.match(html,/12 rows returned this minute/);assert.doesNotMatch(html,/600 \/ 500|600 people/);assert.doesNotMatch(html,/External review<\/span>/);assert.match(html,/2\. Read profiles/);assert.match(html,/3\. Local checks/);
});
test('local processing counts are shown only when provided',()=>{
 assert.doesNotMatch(c.collectionCoverageHTML({local_laya:true,qualify:false}),/0 processed/);
 assert.match(c.collectionCoverageHTML({local_laya:true,coverage:{local:{processed_profiles:40,eligible_profiles:45,pending_profiles:5,reason:'Waiting for profiles'}}}),/40 \/ 45 profiles checked[\s\S]*5 waiting/);
});

test('paused collection does not animate old running rows or claim unverified completion',()=>{
 const elements=new Map();
 const $=key=>{if(!elements.has(key))elements.set(key,{innerHTML:'',textContent:'',contains:()=>false});return elements.get(key)};
 const ctx=vm.createContext({mountCollectionTargets:()=>{},$,S:{sc:{paused:false,stages:[{id:'lists',paused:true,state:'paused'}],ext:{online:true},lists:[{seed:'example',direction:'followers',state:'running',saved_entries:20,expected:30},{seed:'old',direction:'followers',state:'done',saved_entries:30,expected:30,completion:'unverified'}],progress:{lists:{},bios:{left:1},qualify:{}}}},document:{activeElement:null},listFilter:'all',listsAll:false,LIST_STATE:{running:'Reading now',done:'Done'},ST_LABEL:{},int:String,esc:String,ucf:String,left:String,ago:String,backIn:String,eta:()=>null});
 vm.runInContext(source.slice(source.indexOf('function renderScraper()'),source.indexOf('let listsAll =')),ctx);
 ctx.renderScraper();
 assert.match($('#lists-body').innerHTML,/Paused/);
 assert.match($('#lists-body').innerHTML,/Needs review/);
 assert.doesNotMatch($('#lists-body').innerHTML,/dot run|bar-p done|width:100%/);
});


test('bio stage distinguishes unknown, paused, progressing, and caught up without an invented denominator',()=>{
 const source={progress:{bios:{left:120,per_minute:0,eta_h:3}},stages:[{id:'bios',state:'paused',paused:true}]};
 const paused=c.collectionCoverageHTML(source);
 assert.match(paused,/120 profiles waiting/);assert.match(paused,/Paused · 0 bios read this minute/);assert.doesNotMatch(paused,/left in current queue|120 \/|External review/);
 assert.match(c.collectionCoverageHTML({}),/Measuring queue/);
 assert.match(c.collectionCoverageHTML({progress:{bios:{left:0,per_minute:0}}}),/Up to date/);
 assert.match(c.collectionCoverageHTML({qualify:true}),/External review also enabled/);
 const running=vm.createContext({int:String,esc:String,eta:()=> 'about 2 h'});
 vm.runInContext(sourceCode(),running);
 const active=running.collectionCoverageHTML({progress:{bios:{left:100,per_minute:3,eta_h:2}},stages:[{id:'bios',state:'running',paused:false}]});
 assert.match(active,/100 profiles waiting/);assert.match(active,/Reading Instagram profiles · 3 bios read this minute · about 2 h left in current queue/);
});
function sourceCode(){return source.slice(source.indexOf('function collectionCoverageHTML('),source.indexOf('function renderAccounts()'));}
