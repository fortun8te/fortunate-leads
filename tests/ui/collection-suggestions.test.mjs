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
 assert.match(html,/@brand/);assert.match(html,/Followers · &lt;custom>/);assert.doesNotMatch(html,/\.\.\/bad|@empty|<custom>/);
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
 const ctx=vm.createContext({int:String,esc:String,eta:h=>`${h} h`});
 vm.runInContext(source.slice(source.indexOf('function collectionCoverageHTML('),source.indexOf('function renderAccounts()')),ctx);
 const snapshot={lists:[{state:'running',expected:100,saved_entries:50}],coverage:{lists:{saved_entries:50,known_targets:1,expected_entries:100,unknown_targets:0,partial_lists:0}},stages:[{id:'lists',state:'running',paused:false}],progress:{lists:{left:50,eta_h:2,per_minute:3}}};
 assert.match(ctx.collectionCoverageHTML(snapshot),/Active queue: 2 h left/);
 assert.match(ctx.collectionCoverageHTML({...snapshot,coverage:{lists:{...snapshot.coverage.lists,partial_lists:73,unknown_targets:1}},lists:[...snapshot.lists,{state:'partial',expected:null}]}),/Active queue: 2 h left/);
 for(const override of [{paused:true},{progress:{lists:{left:50,eta_h:2,per_minute:0}}},{lists:[{state:'running',expected:null}]}])assert.doesNotMatch(ctx.collectionCoverageHTML({...snapshot,...override}),/Active queue:/);
});
