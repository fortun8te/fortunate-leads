import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../../web/app.js',import.meta.url),'utf8');
const snippet=source.slice(source.indexOf('function accountInstagramHTML('),source.indexOf('function accountRow('));
function harness({port='8777',connected=true,lane='main',reply={ok:true,opened:true,account_verified:true}}={}) {
 const sent=[],messages=[];
 const context={location:{port},setTimeout(){return 1;},clearTimeout(){},toast:m=>messages.push(m),api:{get:async()=>({extension_id:'extension-id',instagram:{connected,lane_id:lane,ig_id:'123'}})},window:{chrome:{runtime:{sendMessage:(id,body,callback)=>{sent.push({id,body});callback(reply);}}}}};
 const functions=vm.runInNewContext(`${snippet}\n({accountInstagramHTML,openInstagramAccount})`,context);
 return{...functions,sent,messages};
}
test('preview Inbox shortcut opens Instagram with no connection claim',()=>{
 const h=harness({port:'8880'}),html=h.accountInstagramHTML({is_main:true});
 assert.match(html,/https:\/\/www.instagram.com\/direct\/inbox\//);assert.match(html,/check the signed-in account/);assert.doesNotMatch(html,/data-instagram/);assert.equal(h.accountInstagramHTML({is_main:false}),'');
});
test('connected Inbox action sends fresh identity to the extension and requires verification',async()=>{
 const h=harness();await h.openInstagramAccount('main','inbox');
 assert.deepEqual(JSON.parse(JSON.stringify(h.sent)),[{id:'extension-id',body:{type:'OPEN_INSTAGRAM',destination:'inbox',expected_lane_id:'main',expected_account_id:'123'}}]);
 assert.deepEqual(h.messages,['Instagram opened in your connected profile.']);
});
test('offline or different profile never sends an open command',async()=>{
 for(const options of [{connected:false},{lane:'other'}]){const h=harness(options);await h.openInstagramAccount('main','inbox');assert.equal(h.sent.length,0);assert.match(h.messages[0],/sign in/);}
});
test('unverified extension reply cannot claim the connected Inbox opened',async()=>{
 const h=harness({reply:{ok:true,opened:true,account_verified:false}});await h.openInstagramAccount('main','inbox');assert.match(h.messages[0],/Couldn't open/);assert.doesNotMatch(h.messages[0],/opened in/);
});
