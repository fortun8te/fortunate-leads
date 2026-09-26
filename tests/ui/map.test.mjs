import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const code = source.slice(source.indexOf('const LEAD_R ='), source.indexOf('function hoverCard('));
function harness() {
  const elements = new Map(), images = [];
  const $ = id => {
    if (!elements.has(id)) elements.set(id, {value:'', textContent:'', classList:{toggle(){}}});
    return elements.get(id);
  };
  const c = vm.createContext({$, store:{get:()=>true}, debounce:f=>f, filterCount:()=>0,
    fitOf:n=>n.fit || 'unread', int:String, plural:(n,s)=>`${n} ${s}`, S:{f:{},view:'map'},
    toQuery:()=>new URLSearchParams(), URLSearchParams, LeadWorkflow:{runtimeQuery:q=>q},
    Image:class {constructor(){images.push(this);}}, document:{createElement:()=>({getContext:()=>null})},
    openDetail(){}, openSeed(){}});
  vm.runInContext(code + '\nthis.map = M; this.cache = PICS; this.queue = picQueue;', c);
  c.map.simulate = () => {}; c.map.draw = () => {}; c.map.schedule = () => {};
  return {c, $, images, m:c.map};
}
const seed = name => ({id:'s:'+name,kind:'seed',label:name,degree:1,pid:null});
const lead = id => ({id:'p:'+id,kind:'lead',label:'person'+id,lists:1,degree:1});
test('map resolves history endpoints but excludes history from neighbours and distinct counts', () => {
  const {m,$} = harness();
  m.build({nodes:[seed('a'),seed('b'),seed('c'),lead(1)],total:1,limit:3000, links:[
    {source:'s:a',target:'p:1',direction:'followers',state:'observed'},
    {source:'s:a',target:'p:1',direction:'following',state:'observed'},
    {source:'s:b',target:'p:1',direction:'following',state:'absent'},
    {source:'s:c',target:'p:1',direction:'followers',state:'unverified'},
    {source:'s:a',target:'s:b',direction:'followers',state:'observed'},
    {source:'s:b',target:'s:a',direction:'following',state:'observed'},
  ]});
  assert.equal(m.links.length,3);
  assert.match($('#map-count').title,/3 observed, 1 absent, 1 unverified/);
  assert.equal(m.links[0].dir,'both');
  assert.deepEqual([...m.nbr.get('p:1')],['s:a']);
  assert.equal(m.byId.get('s:a').vis,2);
  assert.equal(m.byId.get('s:b').vis,1);
  for (const edge of m.historyLinks) {
    assert.equal(edge.source,m.byId.get(edge.source.id));
    assert.equal(edge.target,m.byId.get(edge.target.id));
    assert.ok(Number.isFinite(edge.source.x) && Number.isFinite(edge.target.x));
  }
});
test('map reports displayed sample and search scope with same client/server cap', () => {
  const {m,$} = harness();m.scope='all';
  assert.match(m.url(),/limit=3000/);
  m.build({nodes:[seed('a'),lead(1)],links:[],total:50000,limit:3000});
  assert.match($('#map-count').textContent,/1 of 50000 matching people shown/);
  assert.match($('#map-count').textContent,/map limit 3000/);
  $('#map-q').value='person';m.search();
  assert.equal($('#map-hits').textContent,'1 displayed');
  $('#map-q').value='not loaded';m.search();
  assert.equal($('#map-hits').textContent,'0 displayed');
});
test('map photo cache, work queue and concurrent loads stay bounded through churn and failure', () => {
  const {c,images} = harness();
  vm.runInContext('for(let i=0;i<6000;i++)mapPic("/img/"+i)',c);
  assert.equal(c.cache.size,3200);
  assert.equal(images.length,8);
  assert.equal(c.queue.length,3192);
  let done=0;
  while(done<images.length) {images[done++].onerror(); assert.ok(c.cache.size<=3200);}
  assert.equal(c.queue.length,0);
  assert.equal(vm.runInContext('picsLoading',c),0);
  assert.equal(images.length,3200);
  // Failed image rendering must also release its loading slot.
  vm.runInContext('mapPic("/img/new")',c);
  images.at(-1).naturalWidth=64;images.at(-1).naturalHeight=64;images.at(-1).onload();
  assert.equal(vm.runInContext('picsLoading',c),0);
});
