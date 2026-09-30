import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../../web/app.js',import.meta.url),'utf8');
test('opening a reviewed person preserves the lead search, filters and sort',()=>{
 const filter={q:'studio',tags:['Founder'],not:['Agency'],any:[],sort:'followers',min_followers:2000};
 const before=structuredClone(filter), calls=[];
 const S={view:'qual',f:filter};let click;
 const context={$:()=>({value:'studio',addEventListener:(_,fn)=>{click=fn}}),S,Q:{rows:[{id:42,handle:'someone'}]},emptyFilter:()=>({}),filtersChanged:()=>calls.push('filters'),setView:v=>calls.push(v),setURL:push=>calls.push(['url',push]),openDetail:id=>calls.push(id)};
 const start=source.indexOf("$('#ql-list').addEventListener('click'");
 vm.runInNewContext(source.slice(start,source.indexOf('// ---------- map ----------',start)),context);
 click({target:{closest:selector=>selector==='[data-open]'?{dataset:{open:'42'}}:null}});
 assert.deepEqual(S.f,before);
 assert.deepEqual(calls,['leads',['url',true],42]);
});
