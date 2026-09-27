import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source = readFileSync(new URL('../../web/mock.js', import.meta.url), 'utf8');
function demo(scenario = 'cooldown') {
  const window = {fetch: async () => {throw Error('Unexpected network request');}};
  const c = vm.createContext({window, location:{origin:'http://demo',search:'?mock=1&mock_scenario='+scenario}, URL,URLSearchParams,Response,console,setInterval(){},setTimeout:fn=>fn()});
  vm.runInContext(source,c);
  return async (path,body,method) => {
    const response = await window.fetch('/api/'+path,body === undefined ? {method:method || 'GET'} : {method:method || 'POST',body:typeof body === 'string' ? body : JSON.stringify(body)});
    return {status:response.status, data:await response.json()};
  };
}
test('details and lead rows carry current, historical and business evidence',async()=>{
  const api=demo();
  const {data:p}=await api('person/1');
  assert.equal(p.status,'client');assert.ok(p.note);assert.equal(typeof p.mark_rev,'string');
  for(const key of ['business_fit','connection_strength','relationship','history_via','history_lists']) assert.ok(key in p,key);
  const {data:old}=await api('person/11');assert.equal(old.lists,0);assert.deepEqual(old.edges,[]);assert.ok(old.edge_history.every(e=>e.state==='unverified'));assert.ok(old.history_lists>0);
  const {data:absent}=await api('person/7');assert.equal(absent.lists,0);assert.ok(absent.edge_history.every(e=>e.state==='absent'));
  // Sample across orders: the best-scored page alone holds no unread people.
  const pages=await Promise.all(['leads?status=all&limit=500','leads?status=all&limit=500&sort=recent','leads?status=client&limit=50'].map(q=>api(q)));
  const rows={rows:pages.flatMap(({data})=>data.rows)};
  assert.ok(rows.rows.some(p=>p.business_fit != null));assert.ok(rows.rows.some(p=>p.business_fit===null));
  assert.ok(rows.rows.some(p=>p.note));assert.ok(rows.rows.every(p=>p.history_lists>=p.lists));
});
test('note-only save preserves mark, stale save returns 409 current record, clear retains note',async()=>{
  const api=demo();const {data:p}=await api('person/1');
  const a=await api('person/1/mark',{note:'First edit',if_match:p.mark_rev});
  assert.equal(a.status,200);assert.equal(a.data.status,'client');assert.notEqual(a.data.mark_rev,p.mark_rev);
  const stale=await api('person/1/mark',{note:'Overwrite',if_match:p.mark_rev});
  assert.equal(stale.status,409);assert.equal(stale.data.current.note,'First edit');assert.equal(stale.data.mark_rev,a.data.mark_rev);
  const clear=await api('person/1/mark',{status:null,mark_rev:a.data.mark_rev});assert.equal(clear.data.note,'First edit');assert.equal(clear.data.status,null);
  const empty=await api('person/1/mark',{note:null,if_match:clear.data.mark_rev});assert.equal(empty.data.mark_rev,'');
});
test('invalid mark values and malformed bodies fail without losing saved data',async()=>{
  const api=demo();
  for(const body of [{status:'unknown'},{note:9},{note:'a'.repeat(5001)},{if_match:2},{if_match:'a',mark_rev:'b'},[], 'not json']) assert.equal((await api('person/1/mark',body)).status,400);
  assert.equal((await api('person/1')).data.status,'client');
  assert.equal((await api('person/999999/mark',{note:'x'})).status,404);
  assert.equal((await api('person/1/mark')).status,404);
});
test('manual removal protects rule and automatic tags; read queue is idempotent',async()=>{
  const api=demo();const {data:p}=await api('person/1');const auto=p.tags.find(t=>t.source==='auto');assert.ok(auto);
  await api('person/1/tags',{remove:[auto.tag],add:['  Human   note  ']});
  const {data:after}=await api('person/1');assert.ok(after.tags.some(t=>t.tag===auto.tag));assert.ok(after.tags.some(t=>t.tag==='Human note'&&t.source==='manual'));
  const {data:rows}=await api('leads?status=all&tags=Amazon&limit=1');assert.ok(rows.rows.length);
  const id=rows.rows[0].id;await api(`person/${id}/tags`,{remove:['Amazon']});assert.ok((await api(`person/${id}`)).data.tags.some(t=>t.tag==='Amazon'&&t.source==='rule'));
  await api('person/1/read',{});await api('person/1/read',{});assert.equal((await api('scraper')).data.queue.profile,1);
  assert.equal((await api('person/1/tags',{add:['bad,tag']})).status,400);
});
test('lead pagination is stable and mutations change revisions; invalid pagination is 400',async()=>{
  const api=demo();const a=(await api('leads?status=all&limit=10')).data,b=(await api('leads?status=all&limit=10&offset=10')).data;
  assert.equal(a.rev,b.rev);assert.equal(new Set([...a.rows,...b.rows].map(p=>p.id)).size,20);
  for(const query of ['offset=nope','limit=2.5','sort=bad']) assert.equal((await api('leads?'+query)).status,400);
  await api('person/1/mark',{note:'Revision changed'});assert.notEqual((await api('leads')).data.rev,a.rev);
});
test('map reports capped current population and historical edge states separately',async()=>{
  const api=demo();const {data:m}=await api('map?scope=all&limit=10');
  assert.equal(m.limit,10);assert.ok(m.total>10);assert.equal(m.nodes.filter(n=>n.kind==='lead').length,10);
  const {data:full}=await api('map?scope=all&limit=5000');assert.equal(full.limit,3000);
  assert.ok(full.links.some(e=>e.state==='absent'));assert.ok(full.links.some(e=>e.state==='unverified'));
  for(const p of full.nodes.filter(n=>n.kind==='lead')){
    const current=new Set(full.links.filter(e=>e.target===p.id&&e.state==='observed').map(e=>e.source));assert.equal(p.lists,current.size);assert.ok('business_fit' in p);assert.ok('note' in p);
  }
});
test('account controls affect one lane and reject invalid edits atomically',async()=>{
  const api=demo();const {data:{accounts}}=await api('accounts');const id=accounts[0].lane_id;
  assert.equal((await api(`accounts/${id}`,{role:'lists',paused:'yes'})).status,400);assert.equal((await api('accounts')).data.accounts[0].role,accounts[0].role);
  const paused=await api('control',{account:id,action:'pause'});assert.equal(paused.status,200);assert.equal(paused.data.accounts[0].paused,true);assert.equal(paused.data.accounts[1].paused,false);
  assert.equal((await api('control',{account:'missing',action:'pause'})).status,404);
  assert.equal((await api('settings/accounts',{main_list_share:'1'})).status,400);
});
test('start all resumes collection and paused lanes without changing AI choice',async()=>{
  const api=demo();
  const before=(await api('accounts')).data.accounts;
  await api('control',{account:before[0].lane_id,action:'pause'});
  await api('control',{stage:'all',action:'pause'});
  const started=await api('control',{action:'start_all'});
  assert.equal(started.status,200);
  assert.ok(started.data.stages.filter((stage)=>stage.id!=='ai').every((stage)=>!stage.paused));
  assert.equal(started.data.stages.find((stage)=>stage.id==='ai').paused,true);
  assert.ok(started.data.accounts.every((account)=>!account.paused));
  const after=(await api('accounts')).data.accounts;
  assert.equal(after.find((a)=>a.hold==='login').status,'needs_login');
  assert.equal(after.find((a)=>a.cooldown_until).status,'cooldown');
});
