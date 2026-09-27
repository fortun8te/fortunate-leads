import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../../web/k2-connection.js', import.meta.url), 'utf8');
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };

function harness() {
  const requests=[];
  const nodes={};
  const node=() => ({value:'',disabled:false,hidden:false,textContent:'',listeners:{},addEventListener(name,callback){this.listeners[name]=callback;},setAttribute(name,value){this[name]=value;}});
  for (const id of ['k2-connection-form','k2-connection-status','k2-host','k2-port','k2-key','k2-lan-fields','k2-save','k2-test']) nodes[`#${id}`]=node();
  const choices=['this_mac','other_pc'].map(location=>({...node(),dataset:{k2Location:location}}));
  nodes['#k2-connection-form'].querySelectorAll=()=>choices;
  const document={querySelector:selector=>nodes[selector]};
  vm.runInNewContext(source,{document,AbortController,setTimeout:()=>1,clearTimeout(){},fetch:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}))});
  const respond=(index,data,ok=true)=>requests[index].resolve({ok,status:ok?200:400,json:async()=>data});
  return {requests,respond,nodes,choices,clickLocation:location=>nodes['#k2-connection-form'].listeners.click({target:{closest:()=>choices.find(button=>button.dataset.k2Location===location)}})};
}

test('loading the saved connection never starts or enables K2', async () => {
  const h=harness();h.respond(0,{location:'this_mac',host:'',port:11436});await settle();
  assert.deepEqual(h.requests.map(request=>request.url),['/api/k2/connection']);
  assert.equal(h.nodes['#k2-test'].disabled,false);
  assert.equal(h.nodes['#k2-save'].disabled,true);
});

test('LAN setup saves separately, then the explicit test probes the saved address', async () => {
  const h=harness();h.respond(0,{location:'this_mac',host:'',port:11436});await settle();
  h.clickLocation('other_pc');
  h.nodes['#k2-host'].value='192.168.1.20';h.nodes['#k2-port'].value='1234';
  h.nodes['#k2-connection-form'].listeners.input();
  assert.equal(h.nodes['#k2-test'].disabled,true);
  assert.equal(h.nodes['#k2-save'].disabled,false);
  h.nodes['#k2-connection-form'].listeners.submit({preventDefault(){}});
  assert.deepEqual(JSON.parse(h.requests[1].options.body),{location:'other_pc',host:'192.168.1.20',port:1234});
  assert.equal(h.requests[1].url,'/api/k2/connection');
  h.respond(1,{location:'other_pc',host:'192.168.1.20',port:1234});await settle();
  assert.equal(h.nodes['#k2-test'].disabled,false);
  h.nodes['#k2-test'].listeners.click();
  assert.equal(h.requests[2].url,'/api/k2/connection/test');
  h.respond(2,{ok:false,message:'Helper endpoints unavailable'});await settle();
  assert.match(h.nodes['#k2-connection-status'].textContent,/Helper endpoints unavailable/);
  assert.equal(h.requests.some(request=>request.url==='/api/engines'),false);
});

test('optional LAN key is sent only on save and cleared from the form afterwards', async () => {
  const h=harness();h.respond(0,{location:'other_pc',host:'192.168.1.20',port:1234,has_api_key:true});await settle();
  assert.equal(h.nodes['#k2-key'].value,'');
  h.nodes['#k2-key'].value='secret-example';h.nodes['#k2-connection-form'].listeners.input();
  assert.equal(h.nodes['#k2-test'].disabled,true);
  h.nodes['#k2-connection-form'].listeners.submit({preventDefault(){}});
  assert.equal(JSON.parse(h.requests[1].options.body).api_key,'secret-example');
  h.respond(1,{location:'other_pc',host:'192.168.1.20',port:1234,has_api_key:true});await settle();
  assert.equal(h.nodes['#k2-key'].value,'');
  h.nodes['#k2-test'].listeners.click();
  assert.equal(JSON.parse(h.requests[2].options.body).api_key,undefined);
});
