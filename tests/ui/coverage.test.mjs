import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../../web/app.js',import.meta.url),'utf8');
const c=vm.createContext({int:String,esc:String,backgroundAIControlsHTML:()=>'',backgroundAIState:()=> 'Off',localProcessingSummary:()=> 'Local AI off'});
vm.runInContext(source.slice(source.indexOf('function collectionCoverageHTML('),source.indexOf('function renderAccounts()')),c);
test('compact summary distinguishes saved list entries from unread bios and never claims cache-only profiles checked',()=>{
 const html=c.collectionCoverageHTML({coverage:{lists:{saved_entries:600,complete_lists:2},local:{processed_profiles:103000,eligible_profiles:103000,pending_profiles:0}},local_laya:true,progress:{bios:{left:400,per_minute:3}}});
 assert.match(html,/600 list entries saved/);assert.match(html,/2 complete lists/);assert.match(html,/400 bios waiting/);assert.match(html,/3 bios read this minute/);assert.doesNotMatch(html,/103000|profiles checked|coverage-item/);
});
test('missing counts stay unknown and disabled local checks stay visible',()=>{
 const html=c.collectionCoverageHTML({local_laya:false});
 assert.match(html,/Counting saved entries/);assert.match(html,/Counting unread bios/);assert.match(html,/Local AI off/);assert.doesNotMatch(html,/0 bios waiting|Collecting/);
});

test('paused collection does not animate old running rows or claim unverified completion',()=>{
 const elements=new Map();
 const $=key=>{if(!elements.has(key))elements.set(key,{innerHTML:'',textContent:'',contains:()=>false});return elements.get(key)};
 const ctx=vm.createContext({SET:{localProcessing:null},backgroundAIControlsHTML:()=>'',backgroundAIState:()=> 'Off',localProcessingSummary:()=> 'Local AI status unavailable',collectionReason:()=>"Waiting",mountCollectionTargets:()=>{},$,S:{sc:{paused:false,stages:[{id:'lists',paused:true,state:'paused'}],ext:{online:true},lists:[{seed:'example',direction:'followers',state:'running',saved_entries:20,expected:30},{seed:'old',direction:'followers',state:'done',saved_entries:30,expected:30,completion:'unverified'},{seed:'retry',direction:'followers',state:'queued',saved_entries:0,expected:2000,error:'list_html_home_redirect <html>rawsecret</html>'}],progress:{lists:{},bios:{left:1},qualify:{}}}},document:{activeElement:null},listFilter:'all',listsShown:10,LIST_STATE:{running:'Reading now',done:'Done'},ST_LABEL:{},int:String,esc:String,ucf:String,left:String,ago:String,backIn:String,eta:()=>null});
 vm.runInContext(source.slice(source.indexOf('function renderScraper()'),source.indexOf('let listsShown =')),ctx);
 ctx.renderScraper();
 assert.match($('#lists-body').innerHTML,/Paused/);
 assert.match($('#lists-body').innerHTML,/Needs review/);
 assert.match($('#lists-body').innerHTML,/Waiting to retry/);
 assert.doesNotMatch($('#lists-body').innerHTML,/rawsecret|<html>/);
 assert.doesNotMatch($('#lists-body').innerHTML,/dot run|bar-p done|width:100%/);
});


test('raw server errors cannot leak into concise collection reasons',()=>{
 const ctx=vm.createContext({});
 vm.runInContext(source.slice(source.indexOf('function collectionReason('),source.indexOf('function accountAccess(')),ctx);
 for(const raw of ['list_html_home_redirect <html>private server trace</html>', 'HTTP 429 {secret:123}', 'Unknown SQL /Users/michael/private']) {
   const safe=ctx.collectionReason(raw,'Try this list again later');
   assert.doesNotMatch(safe,/secret|SQL|Users|<html>|trace|429/);
 }
});
