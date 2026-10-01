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
test('shared Instagram pause explains its retry time without promising completion',()=>{
 const ctx=vm.createContext({int:String,esc:String,Date,backgroundAIControlsHTML:()=>'',localProcessingSummary:()=> 'K2 waiting'});
 vm.runInContext(source.slice(source.indexOf('function collectionReason('),source.indexOf('function accountAccess(')),ctx);
 vm.runInContext(source.slice(source.indexOf('function collectionCoverageHTML('),source.indexOf('function renderAccounts()')),ctx);
 const sc={stages:[{id:'lists',state:'waiting',wait:{scope:'workspace',why:'Instagram requested a pause',until:'2099-01-01T20:08:00Z'}},{id:'bios',state:'waiting'}]};
 const html=ctx.collectionCoverageHTML(sc);
 assert.match(html,/Waiting for Instagram to allow requests/);
 assert.match(html,/Retries automatically at.*Progress is saved/);
 assert.doesNotMatch(html,/finished at|complete at|left/);
 const manual=ctx.collectionCoverageHTML({...sc,paused:true});
 assert.match(manual,/Paused by you/);assert.doesNotMatch(manual,/Retries automatically/);
 assert.match(ctx.collectionCoverageHTML({...sc,paused:true,control:{collection:{stopping:true,stop_acknowledged:false}}}),/Stopping.*current request/);
 assert.match(ctx.collectionCoverageHTML({...sc,paused:true,control:{collection:{stop_acknowledged:false}}}),/Checking the last request/);
 assert.match(ctx.collectionCoverageHTML({...sc,paused:true,control:{collection:{stop_acknowledged:true}}}),/Stopped/);
 assert.match(ctx.collectionCoverageHTML({...sc,paused:true,stages:[{id:'lists',paused:true,active:true,state:'stopping',stop_acknowledged:false},{id:'bios',paused:true,stop_acknowledged:true}]}),/Stopping.*current request/);
 assert.match(ctx.collectionCoverageHTML({...sc,paused:true,stages:[{id:'lists',paused:true,stop_acknowledged:true},{id:'bios',paused:true,stop_acknowledged:true}]}),/Stopped/);
});


test('queue summary scopes the ETA and separates unknown and limited lists',()=>{
 const html=c.collectionCoverageHTML({collection:{finished:4,pending:7,limited:2,needs_review:1,unknown_lists:3,open_ended:true,eta:{scope:'known_lists',low_minutes:20,high_minutes:35}},progress:{bios:{left:80}}});
 assert.match(html,/4 finished · 7 queued · 2 limited · 1 need review/);
 assert.match(html,/Known lists: about 20 min–35 min/);
 assert.match(html,/3 list sizes unknown/);
 assert.match(html,/Auto-discovery on/);
 assert.doesNotMatch(html,/Local AI|Choose AI|all.*finished|complete at/);
 const stopped=c.collectionCoverageHTML({paused:true,collection:{finished:4,pending:7,eta:{scope:'current_queue',low_minutes:20,high_minutes:35}}});
 assert.doesNotMatch(stopped,/about 20|35 min/);
});


test('list filters use completion categories consistently including stopped trials',()=>{
 const elements=new Map();
 const $=key=>{if(!elements.has(key))elements.set(key,{innerHTML:'',textContent:'',contains:()=>false});return elements.get(key)};
 const ctx=vm.createContext({SET:{localProcessing:null},backgroundAIControlsHTML:()=>'',backgroundAIState:()=> 'Off',localProcessingSummary:()=> '',collectionReason:()=> 'Waiting',mountCollectionTargets:()=>{},$,S:{sc:{ext:{online:true},lists:[{seed:'stopped',direction:'following',state:'paused',completion:'blocked',saved_entries:20,expected:30},{seed:'waiting',direction:'following',state:'queued',completion:'waiting',saved_entries:0,expected:null},{seed:'done',direction:'following',state:'done',completion:'complete',saved_entries:30,expected:30}],progress:{lists:{},bios:{},qualify:{}}}},document:{activeElement:null},listFilter:'all',listsShown:10,LIST_STATE:{},ST_LABEL:{},int:String,esc:String,ucf:String,left:String,ago:String,backIn:String,eta:()=>null});
 vm.runInContext(source.slice(source.indexOf('function renderScraper()'),source.indexOf('let listsShown =')),ctx);
 ctx.renderScraper();
 assert.match($('#lists-f').innerHTML,/In queue <span class="num">1<\/span>/);
 assert.match($('#lists-f').innerHTML,/Complete <span class="num">1<\/span>/);
 assert.match($('#lists-f').innerHTML,/Incomplete <span class="num">1<\/span>/);
});

test('account daily limit takes precedence over queue estimates and shows real new profiles',()=>{
 const html=c.collectionCoverageHTML({control:{progress:{unique_new_profiles:{today:12},saved_list_entries:{today:90},bios_read:{today:4}}},stages:[{id:'lists',state:'waiting',wait:{why:'Daily request budget reached',seconds:3600},now:'Daily limit reached. Resumes tonight.'},{id:'bios',state:'waiting'}],collection:{finished:2,pending:40,eta:{scope:'current_queue',low_minutes:5,high_minutes:10},message:'Measuring current pace'}});
 assert.match(html,/12 new profiles today/);assert.match(html,/90 list entries saved/);assert.match(html,/4 bios read/);
 assert.match(html,/Daily limit reached/);assert.doesNotMatch(html,/about 5|Measuring current pace/);
});
