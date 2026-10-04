import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {openInstagramInbox}=createRequire(import.meta.url)('../../web/accounts.js');
function harness({port='8777',connected=true,lane='main',reply={ok:true,opened:true,account_verified:true},chrome=true}={}) {
 const sent=[],messages=[];
 const env={port,toast:m=>messages.push(m),setTimeout(){return 1;},clearTimeout(){},
  getSetup:async()=>({extension_id:'extension-id',instagram:{connected,lane_id:lane,ig_id:'123'}}),
  chrome:chrome?{runtime:{sendMessage:(id,body,callback)=>{sent.push({id,body});callback(reply);}}}:undefined};
 return{env,sent,messages};
}
test('preview and plain-browser use leave the link alone and claim nothing',async()=>{
 for(const options of [{port:'8880'},{chrome:false}]){const h=harness(options);assert.equal(await openInstagramInbox('main',h.env),'link');assert.equal(h.sent.length,0);assert.deepEqual(h.messages,[]);}
});
test('connected Inbox action sends fresh identity to the extension and requires verification',async()=>{
 const h=harness();assert.equal(await openInstagramInbox('main',h.env),'opened');
 assert.deepEqual(JSON.parse(JSON.stringify(h.sent)),[{id:'extension-id',body:{type:'OPEN_INSTAGRAM',destination:'inbox',expected_lane_id:'main',expected_account_id:'123'}}]);
 assert.deepEqual(h.messages,['Instagram opened in your connected profile.']);
});
test('offline or different profile never sends an open command',async()=>{
 for(const options of [{connected:false},{lane:'other'}]){const h=harness(options);assert.equal(await openInstagramInbox('main',h.env),'refused');assert.equal(h.sent.length,0);assert.match(h.messages[0],/sign in/);}
});
test('unverified extension reply cannot claim the connected Inbox opened',async()=>{
 const h=harness({reply:{ok:true,opened:true,account_verified:false}});assert.equal(await openInstagramInbox('main',h.env),'failed');assert.match(h.messages[0],/Couldn't open/);assert.doesNotMatch(h.messages[0],/opened in/);
});
