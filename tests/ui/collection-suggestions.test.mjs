import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../../web/app.js',import.meta.url),'utf8');
const suggestionCode=source.slice(source.indexOf('const collectionSuggestions ='),source.indexOf('// Move the existing source form'));
const c=vm.createContext({$:()=>null,esc:x=>String(x).replaceAll('<','&lt;'),toast(){},loadScraper(){},Date});
vm.runInContext(suggestionCode,c);
test('suggestions describe directions, escape text, and omit invalid targets',()=>{
 const html=c.collectionSuggestionsHTML([{handle:'brand',directions:['followers'],reason:'<custom>'},{handle:'../bad',directions:['following']},{handle:'empty',directions:[]}]);
 assert.match(html,/@brand/);assert.match(html,/Followers · Saved match/);assert.match(html,/&lt;custom>/);assert.doesNotMatch(html,/\.\.\/bad|@empty|<custom>/);
});
test('read-only suggestions never queue; explicit Add preserves supplied directions and ignores repeat clicks',async()=>{
 let posts=0,release;
 c.api={get:async()=>({suggestions:[{handle:'brand',directions:['following'],reason:'Client'}]}),post:async(path,body)=>{posts++;assert.equal(path,'/api/scraper/seeds');assert.equal(JSON.stringify(body),JSON.stringify({handles:['brand'],directions:['following']}));await new Promise(r=>release=r);return {queued:1}}};
 await c.loadCollectionSuggestions(true);assert.equal(posts,0);
 const add=c.addSuggestedTarget('brand');await c.addSuggestedTarget('brand');assert.equal(posts,1);
 release();await add;
 await c.addSuggestedTarget('brand');assert.equal(posts,1,'stale read must not resurface the target just added');
});
test('coverage only gives ETA for a progressing complete-known active queue',()=>{
 const ctx=vm.createContext({int:String,esc:String,backgroundAIControlsHTML:()=>'',backgroundAIState:()=> 'Off',localProcessingSummary:()=> 'K2 status',eta:h=>`${h} h`});
 vm.runInContext(source.slice(source.indexOf('function collectionCoverageHTML('),source.indexOf('function renderAccounts()')),ctx);
 const snapshot={lists:[{state:'running',expected:100,saved_entries:50}],coverage:{lists:{saved_entries:50,known_targets:1,expected_entries:100,unknown_targets:0,partial_lists:0}},stages:[{id:'lists',state:'running',paused:false}],progress:{lists:{left:50,eta_h:2,per_minute:3}}};
 assert.match(ctx.collectionCoverageHTML(snapshot),/Active queue: 2 h left/);
 assert.match(ctx.collectionCoverageHTML({...snapshot,coverage:{lists:{...snapshot.coverage.lists,partial_lists:73,unknown_targets:1}},lists:[...snapshot.lists,{state:'partial',expected:null}]}),/Active queue: 2 h left/);
 for(const override of [{paused:true},{progress:{lists:{left:50,eta_h:2,per_minute:0}}},{lists:[{state:'running',expected:null}]}])assert.doesNotMatch(ctx.collectionCoverageHTML({...snapshot,...override}),/Active queue:/);
});


test('suggested accounts starts closed, keeps errors visible, and preserves disclosure state',()=>{
 const box={innerHTML:'',querySelector:()=>null,contains:()=>false};
 const ctx=vm.createContext({$:()=>box,document:{activeElement:null},esc:String,Date,toast(){},loadScraper(){}});
 vm.runInContext(suggestionCode,ctx);
 vm.runInContext("collectionSuggestions.enabled=true;collectionSuggestions.items=[{handle:'a',directions:['following'],reason:'Marked as your client'}];collectionSuggestions.history=[{handle:'old',state:'queued',reason:'Verbose old reason'}];renderCollectionSuggestions()",ctx);
 assert.match(box.innerHTML,/<details class="suggested-accounts" >/);
 assert.match(box.innerHTML,/Suggested accounts/);
 assert.match(box.innerHTML,/Auto-discover on/);
 assert.doesNotMatch(box.innerHTML,/Verbose old reason|Added automatically|Suggested next/);
 box.querySelector=selector=>selector==='.suggested-accounts'?{open:true}:null;
 vm.runInContext("collectionSuggestions.error='Could not save discovery settings. Try again.';collectionSuggestions.saving=true;renderCollectionSuggestions()",ctx);
 assert.match(box.innerHTML,/<details class="suggested-accounts" open>/);
 assert.match(box.innerHTML,/disabled>Saving…/);
 assert.match(box.innerHTML,/<\/details><p class="muted" role="status">Could not save/);
});

test('compact reasons keep factual scope and full evidence available without a log',()=>{
 const items=[{handle:'client',directions:['followers','following'],reason:'Marked as your client · 20,955 followers on saved profile',followers:20955,observed_sources:5},{handle:'known',directions:['following'],reason:'Personal connection recorded by you'},{handle:'fit',directions:['following'],reason:'Strong saved business fit'},{handle:'fourth',directions:['following'],reason:'Good saved business fit'}];
 const html=c.collectionSuggestionsHTML(items,new Set(['client']));
 assert.match(html,/Both lists · Client · 21K followers · 5 source accounts/);
 assert.match(html,/Following · Recorded connection/);
 assert.match(html,/Following · Strong fit/);
 assert.match(html,/Marked as your client · 20,955 followers on saved profile/);
 assert.match(html,/disabled>Adding…/);
 assert.match(html,/data-discovery-hide="client"[^>]* disabled/);
 assert.doesNotMatch(html,/@fourth/);
});

test('failed Add keeps the suggested account available and reports the failure',async()=>{
 const ctx=vm.createContext({$:()=>null,esc:String,Date,toast(){},loadScraper(){},api:{post:async()=>{throw Error('offline')}}});
 vm.runInContext(suggestionCode,ctx);
 vm.runInContext("collectionSuggestions.items=[{handle:'brand',directions:['following']}];",ctx);
 await ctx.addSuggestedTarget('brand');
 const state=vm.runInContext('({items:collectionSuggestions.items,error:collectionSuggestions.error,busy:collectionSuggestions.busy.size})',ctx);
 assert.equal(state.items[0].handle,'brand');assert.equal(state.busy,0);assert.equal(state.error,"Couldn't add @brand. Try again.");
});
