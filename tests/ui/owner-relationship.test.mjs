import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const s=readFileSync(new URL('../../web/app.js',import.meta.url),'utf8');
const c=vm.createContext({esc:String});
vm.runInContext(s.slice(s.indexOf('const RELATIONSHIP_LABELS ='),s.indexOf('const seedList =')),c);
test('observed directions have distinct labels and dated source evidence',()=>{
 for(const [value,label] of [['follows','Follows you'],['followed','You follow'],['mutual','Follow each other']]){
  const html=c.relationshipHTML({owner_relationship:value,relationship_owner:'fortun8te',relationship_evidence:[{seed:'fortun8te',direction:'followers',observed_at:'2026-09-02T12:00:00Z'}]});
  assert.ok(html.includes(`>${label}</span>`));assert.match(html,/@fortun8te followers list.*observed/);
 }
});
test('explicit unknown relationship overrides stale generated tags and legacy summary',()=>{
 assert.equal(c.relationshipHTML({owner_relationship:null,relationship:'mutual',tags:[{tag:'follows you'}]}),'');
 assert.equal(c.relationshipHTML({tags:[{tag:'follows you'}],via:['fortun8te']}),'');
});
test('follow filter is shared by Leads and Connections and includes all statuses only by default',()=>{
 const ctx=vm.createContext({S:{f:{status:'',relationship:''}},RELATIONSHIP_LABELS:{follows:'Follows you'},hosts:[],document:{createElement(){return {dataset:{},setAttribute(){},addEventListener(_e,fn){this.change=fn}}}},filtersChanged(){},$:()=>({append(el){ctx.hosts.push(el)}})});
 vm.runInContext(s.slice(s.indexOf('// Both views share'),s.indexOf('// ---------- query bar')),ctx);
 assert.equal(ctx.hosts.length,2);
 ctx.hosts[0].value='follows';ctx.hosts[0].change();assert.equal(ctx.S.f.status,'all');
 ctx.S.f.status='no';ctx.hosts[1].value='followed';ctx.hosts[1].change();assert.equal(ctx.S.f.status,'no');
});
