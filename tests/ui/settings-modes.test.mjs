import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../../web/app.js',import.meta.url),'utf8');
function harness({failPost=false, failRead=false,reorder=false}={}) {
 const nodes=new Map(),calls=[],messages=[];
 const $=id=>{if(!nodes.has(id))nodes.set(id,{disabled:false,classList:{toggle(){}},setAttribute(){},querySelectorAll:()=>[],addEventListener(k,fn){this[k]=fn}});return nodes.get(id)};
 const S={sc:{processing:{mode:'RLAI',generation:1}}};
 const SET={processing:{mode:'RLAI',generation:1},processingStale:false,models:['vendor/first','vendor/second'],dirty:true,scout:{model:'grok'}};
 let saved=SET.processing;
 const ctx=vm.createContext({$,S,SET,window:{dispatchEvent(){}},Event,int:String,toast:s=>messages.push(s),renderSettings(){},renderScout(){},loadScout:async()=>{},renderModels(){},ucf:s=>s,api:{
  post:async(url,body)=>{calls.push({url,body});if(failPost)throw Error('offline');if(url==='/api/local-processing')return {enabled:true,paused:body.paused,stop_acknowledged:true,ready:true,queue:42,reviewed:318,runtime:{resources:{allowed:true}}};if(url==='/api/llm/models')return {models:reorder?[...body.models].reverse():body.models};return saved={mode:body.mode,generation:2,capabilities:{rules:true,laya:body.mode!=='R',local_qualification:body.mode!=='R',external:body.mode==='RLEAI'}}},
  get:async(url)=>{if(failRead)throw Error('offline');return url==='/api/processing-mode'?saved:{enabled:saved.mode!=='R',model:'K2 Horizon 3.7B',ready:true,reviewed:318,queue:42,notes_pending:2,needs_research:19}}
 }});
 vm.runInContext(source.slice(source.indexOf('const PROCESSING_LABELS ='),source.indexOf('// Leadscout:')),ctx);
 const start=source.indexOf("$('#set-models-save').onclick = async () => {");
 vm.runInContext(source.slice(start,source.indexOf("$('#view-settings').addEventListener",start)),ctx);
 return {$,S,SET,ctx,calls,messages,choose:mode=>$('#set-mode').click({target:{closest:()=>({dataset:{mode},disabled:false})}})};
}
test('three modes use one atomic endpoint and cumulative authoritative readback',async()=>{
 for(const mode of ['R','RLEAI']){const h=harness();await h.choose(mode);assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/api/processing-mode');assert.equal(h.calls[0].body.mode,mode);assert.equal(h.SET.processing.mode,mode);assert.equal(h.SET.processing.capabilities.local_qualification,mode!=='R');assert.equal(h.messages.at(-1),'Checking mode saved')}
});
test('failed mode change stays uncertain and does not claim success',async()=>{
 const h=harness({failPost:true,failRead:true});await h.choose('RLEAI');h.ctx.renderCheckingMode();assert.equal(h.SET.processingStale,true);assert.match(h.$('#set-mode-status').textContent,/unavailable/);assert.doesNotMatch(h.messages.at(-1),/saved/);
});
test('local progress shows actual saved bio queue and separate research needs',async()=>{
 const h=harness();await h.ctx.loadProcessingStatus();h.ctx.renderCheckingMode();assert.match(h.$('#set-local-progress').textContent,/318 bios reviewed.*42 waiting.*2 notes waiting.*19 need more research/);assert.match(h.$('#set-mode-models').textContent,/K2 Horizon/);
});
test('new mode beats stale scraper snapshot, external summary preserves local and identifies research model',async()=>{
 const h=harness();await h.choose('RLEAI');h.S.sc.processing={mode:'R',generation:1};h.ctx.renderCheckingMode();assert.match(h.$('#set-mode-status').textContent,/Rules \+ local \+ external AI selected/);assert.match(h.$('#set-mode-models').textContent,/Laya are included.*grok.*optional/);
});
test('legacy model save preserves order without changing mode',async()=>{
 const h=harness();await h.$('#set-models-save').onclick();assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/api/llm/models');assert.equal(h.SET.processing.mode,'RLAI');assert.equal(h.SET.dirty,false);
});
test('unexpected saved legacy model order reports failure',async()=>{
 const h=harness({reorder:true});await h.$('#set-models-save').onclick();assert.equal(h.SET.dirty,true);assert.match(h.messages.at(-1),/Could not confirm/);
});
test('queued work never claims model is running when service is unavailable',()=>{
 const h=harness();h.SET.localProcessing={enabled:true,ready:false,state:'unavailable',reviewed:2,queue:30,seeding:true};
 assert.match(h.ctx.localProcessingSummary(),/Background AI unavailable.*2 bios reviewed.*30 waiting.*finding more saved bios/);
 h.SET.localProcessing.enabled=false;assert.equal(h.ctx.localProcessingSummary(),'Local AI off');
});
test('resource pause and manual pause override a ready running model',()=>{
 const h=harness();
 const state={enabled:true,ready:true,state:'working',queue:42,reviewed:318,runtime:{resources:{allowed:false,recovering:true}}};
 assert.equal(h.ctx.backgroundAIState(state),'Waiting for Mac');
 assert.equal(h.ctx.backgroundAIState({...state,paused:true,stop_acknowledged:false}),'Stopping');
 assert.equal(h.ctx.backgroundAIState({...state,paused:true,stop_acknowledged:true}),'Paused');
 h.SET.localProcessing=state;
 assert.match(h.ctx.localProcessingSummary(),/waiting for mac.*42 waiting/);
});
test('background pause uses separate endpoint and preserves processing mode',async()=>{
 const h=harness();h.SET.localProcessing={enabled:true,paused:false,ready:true};
 await h.$('#local-ai-toggle').click();
 assert.deepEqual(JSON.parse(JSON.stringify(h.calls[0])),{url:'/api/local-processing',body:{paused:true}});
 assert.equal(h.SET.processing.mode,'RLAI');assert.equal(h.SET.localProcessing.paused,true);
 assert.equal(h.$('#local-ai-state').textContent,'Paused');assert.equal(h.$('#local-ai-toggle').textContent,'Resume');
 assert.match(h.$('#local-ai-help').textContent,/queue is saved.*rules and scraping continue/);
});
test('unconfirmed background pause is shown as unknown, never still running',async()=>{
 const h=harness({failPost:true});h.SET.localProcessing={enabled:true,ready:true,state:'working'};
 await h.$('#local-ai-toggle').click();
 assert.equal(h.SET.localProcessing,null);assert.equal(h.$('#local-ai-state').textContent,'Status unavailable');assert.equal(h.$('#local-ai-toggle').disabled,true);
});
test('compact shared controls show resource waiting and pause without changing mode',()=>{
 const h=harness();h.ctx.esc=String;
 h.SET.localProcessing={enabled:true,paused:false,ready:true,state:'working',runtime:{resources:{allowed:false,recovering:true}}};
 assert.match(h.ctx.backgroundAIControlsHTML(),/Background AI · Waiting for Mac.*data-local-ai-toggle[^>]*>Pause/s);
 h.SET.localProcessing.paused=true;h.SET.localProcessing.stop_acknowledged=true;
 assert.match(h.ctx.backgroundAIControlsHTML(),/Background AI · Paused.*>Resume/s);
});

test('active inference and normal pacing are not shown as resource pauses',()=>{
 const h=harness();
 const base={enabled:true,ready:true,state:'working'};
 assert.equal(h.ctx.backgroundAIState({...base,runtime:{resources:{allowed:false,busy:true}}}),'Running');
 assert.equal(h.ctx.backgroundAIState({...base,state:'ready',runtime:{resources:{allowed:false,busy:false,retry_in:2}}}),'Running');
 assert.equal(h.ctx.backgroundAIState({...base,runtime:{resources:{allowed:false,busy:false,thermal_limited:true}}}),'Waiting for Mac');
 assert.equal(h.ctx.backgroundAIState({...base,runtime:{resources:{allowed:false,busy:false,error:'Unable to read pressure'}}}),'Waiting for Mac');
});
test('terminal note failures are shown separately without claiming running work',()=>{
 const h=harness();h.SET.localProcessing={enabled:true,ready:true,state:'ready',reviewed:4,queue:0,notes_pending:0,notes_failed:2};
 assert.equal(h.ctx.backgroundAIState(),'Ready');
 assert.match(h.ctx.localProcessingSummary(),/2 notes need review/);
 assert.doesNotMatch(h.ctx.localProcessingSummary(),/2 notes waiting/);
});
test('unverified local outputs stay separate from completed bio reviews and queued work',()=>{
 const h=harness();h.SET.localProcessing={enabled:true,ready:true,state:'ready',reviewed:4,queue:0,unverified:3,notes_failed:2};
 assert.match(h.ctx.localProcessingSummary(),/4 bios reviewed.*0 waiting.*2 notes need review.*3 profiles need review/);
 assert.doesNotMatch(h.ctx.localProcessingSummary(),/7 bios reviewed|3 waiting/);
 assert.equal(h.ctx.backgroundAIState(),'Ready');
 h.SET.localProcessing.paused=true;h.SET.localProcessing.stop_acknowledged=true;
 assert.match(h.ctx.localProcessingSummary(),/paused.*3 profiles need review/);
});
