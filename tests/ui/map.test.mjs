import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');
const style = readFileSync(new URL('../../web/app.css', import.meta.url), 'utf8');
const markup = readFileSync(new URL('../../web/index.html', import.meta.url), 'utf8');
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
    esc:String, fitOf:n=>n.fit || 'unread', int:String, plural:(n,s)=>`${n} ${s}`, S:{f:{},view:'map'},
    emptyFilter:()=>({}), toQuery:()=>new URLSearchParams(), URLSearchParams,
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
test('map accents follow the tag palette with visible contrast in both themes', () => {
  assert.match(source,/css\('--t-caution'\)/);
  assert.match(source,/css\('--t-map-strong'\)/);

  assert.doesNotMatch(markup,/background:#ff8a1f|background:#e5484d/);
  const dark = {}, light = {};
  for (const block of style.matchAll(/:root(\[data-theme="light"\])?\s*\{([^}]+)\}/g)) {
    const tokens = block[1] ? light : dark;
    for (const token of block[2].matchAll(/(--[\w-]+):\s*(#[0-9a-f]{6}|var\(--[\w-]+\))/gi)) tokens[token[1]] = token[2];
  }
  const luminance = hex => {
    const channels = [1,3,5].map(i => parseInt(hex.slice(i,i+2),16)/255).map(v => v<=.04045 ? v/12.92 : ((v+.055)/1.055)**2.4);
    return channels[0]*.2126+channels[1]*.7152+channels[2]*.0722;
  };
  const resolved = (tokens, name) => {
    const value = tokens[name] || dark[name];
    return value?.startsWith('var(') ? resolved(tokens, value.slice(4, -1)) : value;
  };
  for (const tokens of [dark,light]) for (const name of ['--t-map-strong','--t-caution']) {
    const a=luminance(resolved(tokens, name)), b=luminance(resolved(tokens, '--bg'));
    assert.ok((Math.max(a,b)+.05)/(Math.min(a,b)+.05)>=3, `${name} needs visible contrast on ${tokens['--bg']}`);
  }
});
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
  assert.equal($('#map-me').title,'Centre on @fortun8te');
  assert.equal(m.selfRelation.get('p:0'),'followers');
  assert.equal(m.seeds[0].fx,m.seeds[0].x,'source account is a fixed landmark');
  assert.equal(m.seeds[0].fy,m.seeds[0].y);
  for (let i=0; i<people.length; i++) for (let j=i+1; j<people.length; j++) {
    const a=m.byId.get(people[i].id), b=m.byId.get(people[j].id);
    assert.ok(Math.hypot(a.x-b.x,a.y-b.y)>a.r+b.r, `people ${i} and ${j} overlap`);
  }
  m.build({nodes:[seed('other')],total:0,links:[]});
  assert.equal($('#map-me').hidden,true);
});
test('wide map spreads source accounts across the canvas and keeps their anchors on refresh', () => {
  const {m} = harness();
  m.w=1500; m.h=600;
  const sources = Array.from({length:15}, (_, i) => seed('source'+i));
  const people = Array.from({length:400}, (_, i) => lead(i));
  const links = people.map((p,i) => ({source:sources[i%sources.length].id,target:p.id,direction:'followers',state:'observed'}));
  m.build({nodes:[...sources,...people],total:400,links});
  const nodes=m.seeds;
  const span=(key)=>Math.max(...nodes.map((n)=>n[key]))-Math.min(...nodes.map((n)=>n[key]));
  assert.ok(span('x')/span('y')>1.4,'source anchors should use a wide viewport');
  m.fit();
  const [x0,,x1]=m.bounds(m.nodes);
  assert.ok((x1-x0)*m.k > m.w*0.4,'compact map should remain readable');
  assert.ok((x1-x0)*m.k <= m.w,'initial map fits the available width');
  const first=nodes[0];
  const home=[first.homeX,first.homeY];
  first.x+=35;
  m.build({nodes:sources.map((n)=>seed(n.label)),total:0,links:[]});
  assert.equal(m.seeds[0].homeX,home[0]);
  assert.equal(m.seeds[0].homeY,home[1]);
});
test('map reports a bounded sample and search scope', () => {
  const {m,$} = harness();m.scope='all';
  assert.match(m.url(),/limit=400/);
  assert.match(m.url(),/today=\d{4}-\d{2}-\d{2}/);
  m.limit=3000; assert.match(m.url(),/limit=3000/);
  m.build({nodes:[seed('a'),lead(1)],links:[],total:50000,limit:3000});
  assert.match($('#map-count').textContent,/1 \/ 50000 people/);
  assert.match(markup, /id="map-q"/);
  assert.doesNotMatch(markup, /id="map-labels"/);
  assert.equal(m.scope, 'all');
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
  assert.match($('#map-count').textContent,/2 \/ 2400 people/);
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

test('metadata refresh stays still while changed follow evidence gets a small settle', () => {
  const {m} = harness(), alphas=[];
  m.simulate = alpha => alphas.push(alpha);
  const data = () => ({nodes:[seed('a'),lead(1)],total:1,links:[{source:'s:a',target:'p:1',direction:'followers',state:'observed'}]});
  m.build(data());
  m.byId.get('p:1').x=123; m.byId.get('p:1').y=456;
  const updated=data();updated.nodes[1].status='contacted';
  m.build(updated);
  assert.equal(alphas.at(-1),0,'status updates must not reheat the graph');
  assert.equal(m.byId.get('p:1').x,123);
  assert.equal(m.byId.get('p:1').y,456);
  const changed=data();changed.links[0].direction='following';
  m.build(changed);
  assert.equal(alphas.at(-1),0.12,'new follow evidence receives a bounded settle');
});

test('animation draws without changing the camera on each frame', () => {
  const {c,m} = harness();let draws=0,fits=0,frame;
  c.requestAnimationFrame=fn=>{frame=fn;return 1;};
  const schedule=code.slice(code.indexOf('  schedule() {'),code.indexOf('  status(t) {'));
  vm.runInContext('this.schedule = ({'+schedule+'}).schedule',c);
  m.draw=()=>draws++;m.fit=()=>fits++;m.autoFit=true;
  c.schedule.call(m);frame();
  assert.equal(draws,1);assert.equal(fits,0);
});

test('opening secondary comparison pauses graph fetching', async () => {
  const {m, $, api} = harness();let requests=0;
  api.get=()=>{requests++;return Promise.resolve({rev:1,nodes:[],links:[]});};
  $('#connections-panel').hidden=false;
  await m.load();
  assert.equal(requests,0);
  $('#connections-panel').hidden=true;
  await m.load();
  assert.equal(requests,1);
});

test('default map uses compact anchors and readable avatar sizes', () => {
  const {m} = harness(); m.w=1200; m.h=700;
  const seeds=Array.from({length:22},(_,i)=>seed(String(i)));
  const people=Array.from({length:400},(_,i)=>lead(i));
  m.build({nodes:[...seeds,...people],links:people.map((n,i)=>({source:seeds[i%22].id,target:n.id,state:'observed'})),total:400});
  const span=Math.max(...m.seeds.map(n=>n.x))-Math.min(...m.seeds.map(n=>n.x));
  assert.ok(span<800, `sources spread over ${span}`);
  assert.ok(m.leads.every(n=>n.r>=11));
  for (let i=0;i<m.nodes.length;i++) for (let j=i+1;j<m.nodes.length;j++) {
    const a=m.nodes[i],b=m.nodes[j];assert.ok(Math.hypot(a.x-b.x,a.y-b.y)>=a.r+b.r,'packed avatars must not overlap');
  }
  assert.equal(m.scope,'all');
});

test('map search ignores hidden Leads filters and positions are organic', () => {
  const {c,m}=harness();let query;
  c.S.f={q:'Apparel',tags:['Apparel']};
  c.toQuery=f=>{query=f;return new URLSearchParams();};
  m.query='alice';m.url();
  assert.equal(query.q,'alice');assert.equal(query.tags,undefined);
  const people=Array.from({length:80},(_,i)=>lead(i));
  m.build({nodes:[seed('a'),...people],links:people.map(p=>({source:'s:a',target:p.id,state:'observed'})),total:80});
  const uniqueX=new Set(m.leads.map(n=>Math.round(n.x)));
  assert.ok(uniqueX.size>60,'avatars must not form rows of grid cells');
});


test('recorded overlap brings related sources closer and keeps you at the centre', () => {
  const data = () => {
    const seeds = ['me','a','b','c','d','e'].map(seed); seeds[0].is_me=true;
    const people = Array.from({length:72},(_,i)=>lead(i));
    const links = people.flatMap((p,i) => [
      {source:seeds[Math.floor(i/12)].id,target:p.id,state:'observed'},
      ...(i<24 ? [{source:i<12?'s:a':'s:me',target:p.id,state:'observed'}] : []),
      ...(i>=24&&i<48 ? [{source:i<36?'s:c':'s:b',target:p.id,state:'observed'}] : [])
    ]);
    return {nodes:[...seeds,...people],links,total:people.length};
  };
  const {m}=harness();m.build(data());
  const distance=(a,b)=>Math.hypot(m.byId.get('s:'+a).x-m.byId.get('s:'+b).x,m.byId.get('s:'+a).y-m.byId.get('s:'+b).y);
  const related=(distance('me','a')+distance('b','c'))/2;
  const unrelated=(distance('me','d')+distance('me','e')+distance('a','d')+distance('b','e'))/4;
  assert.ok(related<unrelated*0.7, `related ${related}, unrelated ${unrelated}`);
  assert.equal(m.byId.get('s:me').x,0);assert.equal(m.byId.get('s:me').y,0);
  for(let i=0;i<m.nodes.length;i++)for(let j=i+1;j<m.nodes.length;j++) {
    const a=m.nodes[i],b=m.nodes[j];assert.ok(Math.hypot(a.x-b.x,a.y-b.y)>=a.r+b.r);
  }
  const other=harness().m;other.build(data());
  assert.deepEqual(m.nodes.map(n=>[n.x,n.y]),other.nodes.map(n=>[n.x,n.y]));
});

test('selection gently opens nearby avatars, stops, and respects reduced motion', () => {
  const run = reduced => {
    const {c,m}=harness(), frames=[];let draws=0;
    c.matchMedia=()=>({matches:reduced});c.requestAnimationFrame=fn=>frames.push(fn);
    m.draw=()=>draws++;
    const selected={id:'p:a',x:0,y:0,r:12};
    const nearby={id:'p:b',x:30,y:0,r:12};
    const distant={id:'p:c',x:150,y:0,r:12};
    m.nodes=[selected,nearby,distant];m.relax(selected,18,40,12);
    let count=0;while(frames.length){frames.shift()();count++;assert.ok(count<=18);}
    assert.ok(nearby.x>36);assert.equal(selected.x,0);assert.equal(distant.x,150);
    assert.ok(draws>0);if(reduced)assert.equal(count,0);
    return nearby.x;
  };
  assert.equal(run(false),run(true));
});

test('typing immediately invalidates an older map response before debounce finishes', async () => {
  const {m,api,c}=harness(),pending=[];
  c.toQuery=f=>new URLSearchParams({q:f.q || ''});
  api.get=url=>new Promise(resolve=>pending.push({url,resolve}));
  const old=m.load();
  m.search('alice');
  pending[0].resolve({rev:1,nodes:[seed('a'),lead(1)],links:[],total:1});
  await old;
  assert.equal(m.nodes.length,0,'old query must not flash results while typing');
  assert.equal(m.query,'alice');
});

test('3k map starts with separate avatars and keeps their positions on refresh', () => {
  const {m}=harness();m.w=1400;m.h=800;
  const sources=Array.from({length:22},(_,i)=>seed('source'+i));
  const people=Array.from({length:3000},(_,i)=>lead(i));
  const data=()=>({nodes:[...sources.map(n=>({...n})),...people.map(n=>({...n}))],total:100000,links:people.map((p,i)=>({source:sources[i%22].id,target:p.id,state:'observed'}))});
  m.build(data());
  let overlaps=0;
  for(let i=0;i<m.nodes.length;i++)for(let j=i+1;j<m.nodes.length;j++) {
    const a=m.nodes[i],b=m.nodes[j];if(Math.hypot(a.x-b.x,a.y-b.y)<a.r+b.r)overlaps++;
  }
  assert.equal(overlaps,0,'initial 3k avatars overlap');
  const before=m.nodes.map(n=>[n.x,n.y]);m.build(data());
  assert.deepEqual(m.nodes.map(n=>[n.x,n.y]),before);
});

test('clearing search restores the overview camera and existing avatar positions', () => {
  const {m}=harness();
  const data=()=>({nodes:[seed('a'),lead(1)],links:[],total:1});
  m.build(data());m.k=.7;m.x=85;m.y=93;m.byId.get('p:1').x=124;
  m.search('alice');m.build({nodes:[lead(9)],links:[],total:1});
  m.k=2;m.x=1000;m.y=2000;
  m.search('');m.build(data());
  assert.equal(m.k,.7);assert.equal(m.x,85);assert.equal(m.y,93);
  assert.equal(m.byId.get('p:1').x,124);
});

test('search responses arriving out of order retain the newest matching profiles', async () => {
  const {m,api,c}=harness(),pending=[];
  c.toQuery=f=>new URLSearchParams({q:f.q || ''});
  api.get=url=>new Promise(resolve=>pending.push({url,resolve}));
  m.search('ali');const old=m.load();m.search('alice');const latest=m.load();
  pending[1].resolve({rev:2,nodes:[lead(2)],links:[],total:1});await latest;
  pending[0].resolve({rev:1,nodes:[lead(1)],links:[],total:1});await old;
  assert.deepEqual([...m.byId.keys()],['p:2']);
  assert.match(m.rev,/q=alice/);
});

test('normalized search finds a source by name and prioritizes an exact handle', () => {
  const {m,$}=harness();m.query='https://www.instagram.com/alice/';
  const data={nodes:[{...seed('alice_source'),name:'Alice Jones'}, {...lead(2),handle:'alice',name:'Alice'}],links:[],total:2,search_query:'alice'};
  m.rawData=data;m.build(data);
  assert.ok(m.byId.has('s:alice_source'),'matching source must not disappear with disconnected matches');
  const html=$('#map-search-results').innerHTML;
  assert.ok(html.indexOf('data-map-person="p:2"')<html.indexOf('data-map-person="s:alice_source"'));
});
