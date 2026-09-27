import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const workflowSource = readFileSync(new URL('../../web/workflow.js', import.meta.url), 'utf8');
const code = source.slice(source.indexOf('const LEAD_R ='), source.indexOf('function hoverCard('));
function harness() {
  const elements = new Map(), images = [];
  const api = {get:()=>Promise.reject(new Error('unmocked map request'))};
  const $ = id => {
    if (!elements.has(id)) elements.set(id, {value:'', textContent:'', classList:{toggle(){}}});
    return elements.get(id);
  };
  const c = vm.createContext({$, api, store:{get:()=>true}, debounce:f=>f, filterCount:()=>0,
    fitOf:n=>n.fit || 'unread', int:String, plural:(n,s)=>`${n} ${s}`, S:{f:{},view:'map'},
    toQuery:()=>new URLSearchParams(), URLSearchParams,
    Image:class {constructor(){images.push(this);}}, document:{createElement:()=>({getContext:()=>null})},
    openDetail(){}, openSeed(){}});
  vm.runInContext(workflowSource, c);
  vm.runInContext(code + '\nthis.map = M; this.cache = PICS; this.queue = picQueue; this.mapDots = mapDots;', c);
  const simulate = c.map.simulate;
  c.map.simulate = () => {}; c.map.draw = () => {}; c.map.schedule = () => {};
  return {c, $, images, m:c.map, api, simulate};
}
const seed = name => ({id:'s:'+name,kind:'seed',label:name,degree:1,pid:null});
const lead = id => ({id:'p:'+id,kind:'lead',label:'person'+id,lists:1,degree:1});
test('overview keeps one useful dot per crowded screen cell and preserves chosen people', () => {
  const {c} = harness();
  const dots = Array.from({length:10000}, (_, i) => ({id:'p:'+i,kind:'lead',x:i % 100,y:Math.floor(i / 100),r:4,L:1}));
  dots[23].L = 5;
  const shown = c.mapDots(dots, 0.2, 0, 0, 800, 600, new Set(['p:19']));
  assert.ok(shown.length < 100, `${shown.length} dots still drawn in a 20×20 px cluster`);
  assert.ok(shown.includes(dots[23]), 'higher-degree dot represents its cell');
  assert.ok(shown.includes(dots[19]), 'chosen person remains visible');
  assert.equal(c.mapDots(dots, 1, 0, 0, 800, 600, new Set()).length, 10000);
  assert.equal(c.mapDots(dots, 1, -1000, 0, 800, 600, new Set()).length, 0);
});
test('initial layout fits immediately without synchronous force ticks', () => {
  const {c,m,simulate} = harness();
  let ticks = 0, fits = 0, linkStrength; const forces = new Map();
  const fluent = () => ({id(){return this;},distance(){return this;},strength(){return this;},distanceMax(){return this;},theta(){return this;},radius(){return this;},iterations(){return this;}});
  const link = () => ({...fluent(), strength(v){linkStrength=v;return this;}});
  const sim = {force(name, value){forces.set(name, value);return this;},alpha(){return this;},alphaDecay(){return this;},alphaMin(){return this;},velocityDecay(){return this;},on(){return this;},stop(){return this;},tick(){ticks++;return this;}};
  c.window = {d3:{forceSimulation:()=>sim,forceLink:link,forceManyBody:fluent,forceCollide:fluent,forceX:fluent,forceY:fluent}};
  m.fit = () => { fits++; };
  m.nodes = Array.from({length:10000}, (_, i) => ({id:'p:'+i,kind:'lead'}));
  m.seeds = []; m.leads = m.nodes; m.links = []; m.seedLinks = [];
  simulate.call(m, 1);
  assert.equal(ticks, 0);
  assert.equal(fits, 1);
  assert.equal(forces.get('charge'), null);
  assert.equal(forces.get('collide'), null);
  assert.equal(linkStrength({ss:false,target:{L:1}}),0.008,'10k layout keeps spiral spacing during simulation');
});
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
test('map identifies your account and spaces a dense source group', () => {
  const {m,$} = harness();
  const mine = {...seed('fortun8te'), is_me:true, degree:120};
  const people = Array.from({length:120}, (_, i) => lead(i));
  m.build({nodes:[mine,...people],total:120,links:people.map((p) => ({
    source:mine.id,target:p.id,direction:'followers',state:'observed'
  }))});
  assert.equal($('#map-me').hidden,false);
  assert.equal($('#map-me').textContent,'You · @fortun8te');
  assert.equal(m.selfRelation.get('p:0'),'followers');
  for (let i=0; i<people.length; i++) for (let j=i+1; j<people.length; j++) {
    const a=m.byId.get(people[i].id), b=m.byId.get(people[j].id);
    assert.ok(Math.hypot(a.x-b.x,a.y-b.y)>a.r+b.r, `people ${i} and ${j} overlap`);
  }
  m.build({nodes:[seed('other')],total:0,links:[]});
  assert.equal($('#map-me').hidden,true);
});
test('map reports a bounded sample and search scope', () => {
  const {m,$} = harness();m.scope='all';
  assert.match(m.url(),/limit=400/);
  assert.match(m.url(),/today=\d{4}-\d{2}-\d{2}/);
  m.limit=3000; assert.match(m.url(),/limit=3000/);
  m.build({nodes:[seed('a'),lead(1)],links:[],total:50000,limit:3000});
  assert.match($('#map-count').textContent,/1 of 50000 matching people shown/);
  $('#map-q').value='person';m.search();
  assert.equal($('#map-hits').textContent,'1 displayed');
  $('#map-q').value='not loaded';m.search();
  assert.equal($('#map-hits').textContent,'0 displayed');
});
test('smaller map density keeps an open person from the same revision only', async () => {
  const {m,api,$} = harness(), pending=[];
  api.get = url => new Promise(resolve => pending.push({url,resolve}));
  m.limit=1000;
  const first=m.load();
  pending[0].resolve({rev:5,total:2400,limit:1000,nodes:[seed('a'),lead(1),lead(2)],
    links:[{source:'s:a',target:'p:1',direction:'followers',state:'observed'},
      {source:'s:a',target:'p:2',direction:'following',state:'observed'}],seed_links:[]});
  await first;
  m.focus=m.byId.get('p:2'); m.focus.x=73; m.focus.fx=73;
  m.drawnLeads=[m.focus];
  m.limit=400;
  const smaller=m.load();
  pending[1].resolve({rev:5,total:2400,limit:400,nodes:[seed('a'),lead(1)],
    links:[{source:'s:a',target:'p:1',direction:'followers',state:'observed'}],seed_links:[]});
  await smaller;
  assert.equal(m.drawnLeads,null,'reload clears the previous screen sample');
  assert.equal(m.focus,m.byId.get('p:2'));
  assert.equal(m.focus.x,73);
  assert.equal(m.focus.fx,73);
  assert.equal(m.links.length,2);
  assert.match($('#map-count').textContent,/2 of 2400 matching people shown/);
  assert.match($('#map-count').textContent,/selected person kept on map/);
  const changed=m.load();
  pending[2].resolve({rev:6,total:2400,limit:400,nodes:[seed('a'),lead(1)],
    links:[{source:'s:a',target:'p:1',direction:'followers',state:'observed'}],seed_links:[]});
  await changed;
  assert.equal(m.focus,null);
  assert.equal(m.links.length,1);
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
test('newer map revision wins a same-URL race and refresh preserves selection layout', async () => {
  const {m,api} = harness();
  const pending = [];
  api.get = url => new Promise((resolve,reject) => pending.push({url,resolve,reject}));
  const first = m.load();
  const second = m.load();
  const data = rev => ({rev,total:1,limit:3000,nodes:[seed('a'),lead(1)],
    links:[{source:'s:a',target:'p:1',direction:'followers',state:'observed'}],seed_links:[]});
  pending[1].resolve(data(2)); await second;
  assert.match(m.rev,/^2\|/);
  assert.equal(m.loading,false);
  m.byId.get('p:1').x=82; m.byId.get('p:1').y=41;
  m.byId.get('p:1').fx=82; m.byId.get('p:1').fy=41;
  m.focus=m.byId.get('p:1'); m.hover=m.focus;
  pending[0].resolve(data(1)); await first;
  assert.match(m.rev,/^2\|/);
  const third=m.load(); pending[2].resolve(data(3)); await third;
  assert.match(m.rev,/^3\|/);
  assert.equal(m.byId.get('p:1').x,82);
  assert.equal(m.byId.get('p:1').fx,82);
  assert.equal(m.focus,m.byId.get('p:1'));
  assert.equal(m.hover,m.byId.get('p:1'));
});
