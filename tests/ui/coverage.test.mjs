import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../../web/app.js',import.meta.url),'utf8');
const c=vm.createContext({int:String,esc:String});
vm.runInContext(source.slice(source.indexOf('function collectionCoverageHTML('),source.indexOf('function renderAccounts()')),c);
test('coverage separates list attempts from unknown targets and unique people',()=>{
 const html=c.collectionCoverageHTML({coverage:{lists:{saved_entries:600,expected_entries:500,known_targets:1,unknown_targets:2,total_lists:3}},local_laya:true,qualify:false,progress:{lists:{per_minute:12}}});
 assert.match(html,/600 this attempt/);assert.match(html,/500 expected in 1 known lists/);assert.match(html,/2 targets unknown/);assert.match(html,/12 rows returned this minute/);assert.doesNotMatch(html,/600 \/ 500|600 people/);assert.match(html,/External AI<\/span><b>Off/);
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
