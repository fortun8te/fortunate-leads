import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url);
const P=require('../../web/accounts.js');
const html=readFileSync(new URL('../../web/index.html',import.meta.url),'utf8');
const source=readFileSync(new URL('../../web/app.js',import.meta.url),'utf8');
const NOW=Date.parse('2026-10-04T12:00:00Z');
const ui=(extra={})=>({open:new Set(),sel:new Set(),openCards:new Set(),busy:new Set(),errors:{},confirm:null,focus:null,...extra});
const card=(c={})=>({lane_id:'lane-1',ig_id:'1',handle:'scout',label:null,name:'@scout',avatar:{pic:null,initial:'S'},kind:'collector',state:'ready',tone:'quiet',
  headline:'Ready',detail:'Waiting for work.',until:null,action:{id:'pause',label:'Pause'},online:true,paused:false,disconnected:false,first_seen:'2026-10-01T00:00:00Z',
  last_seen:'2026-10-04T11:59:00Z',version:'3.9.30',allowance:{reads:'both',list:{used:83,limit:200},profile:{used:0,limit:100},resets_at:'2026-10-05T00:00:00+02:00'},
  details:{reads:'both',reads_label:'Lists and bios',budgets:{list:{limit:200,default:200,custom:false},profile:{limit:100,default:100,custom:false}},last_slowed:null,hour:{pages:0,people:0},events:[],can_forget:false,saved_profile:false,pace:null},...c});

test('a card leads with one health word, one reason and one action, with details closed', () => {
  const {html:h}=P.cardView(card(),ui(),NOW);
  assert.match(h,/<b data-headline="Ready"[^>]*>Ready<\/b>/);
  assert.match(h,/83 of 200 list pages today/);assert.match(h,/0 of 100 bios today/);
  assert.equal((h.match(/data-fid="primary"/g)||[]).length,1);
  assert.match(h,/data-card="pause"/);
  assert.doesNotMatch(h,/acx-more-body|Daily limits|Recent activity/);
  assert.match(h,/aria-expanded="false"/);
});
test('details hold reads, daily limits, the name, recent activity and an empty slot for pace', () => {
  const c=card({details:{...card().details,events:[{at:'2026-10-04T10:05:00Z',text:'Saved 177 people from @x'}]}});
  const {html:h}=P.cardView(c,ui({openCards:new Set(['lane-1'])}),NOW);
  for(const word of ['Reads','Daily limits','Name','Recent activity','Disconnect']) assert.match(h,new RegExp(word));
  assert.match(h,/data-slot="pace" hidden/);
  assert.match(h,/Saved 177 people from @x/);
  assert.match(h,/Whole numbers only/);
});
test('a waiting card counts down from an absolute time without asking the server again', () => {
  const c=card({state:'waiting',tone:'wait',headline:'Waiting {t}',detail:'Instagram asked this account to slow down.',until:'2026-10-04T12:04:00Z'});
  assert.equal(P.headlineText(c,NOW),'Waiting 4 min');
  assert.equal(P.headlineText(c,NOW+3*60000+30000),'Waiting 30 s');
  assert.match(P.cardView(c,ui(),NOW).html,/data-until="2026-10-04T12:04:00Z"/);
  assert.equal(P.headlineText({...c,until:null},NOW),'Waiting');
  assert.equal(P.countdown('2026-10-04T14:20:00Z',NOW),'2 h 20 min');
});
test('the warning review shows a disabled confirm with its reason until the extension agrees', () => {
  const c=card({state:'review',tone:'attention',headline:'Instagram asked for a review',action:{id:'acknowledge',label:"I've reviewed it",enabled:false,hint:'Waiting for the extension to see the warning page closed.'}});
  const h=P.cardView(c,ui(),NOW).html;
  assert.match(h,/data-card="acknowledge"[^>]*disabled/);assert.match(h,/Waiting for the extension/);
});
test('personal account has no pause, no limits and says so quietly', () => {
  const c=card({kind:'main',state:'personal',headline:'Personal account',detail:'Never used for collecting.',allowance:null,action:{id:'inbox',label:'Open Instagram'}});
  const h=P.cardView(c,ui({openCards:new Set(['lane-1'])}),NOW).html;
  assert.match(h,/href="https:\/\/www.instagram.com\/direct\/inbox\/"/);assert.doesNotMatch(h,/data-card="pause"|Daily limits|Disconnect|of \d+ list pages/);
  assert.match(h,/personal account/i);
});
test('a failed action stays on the card with a way to try again', () => {
  const h=P.cardView(card(),ui({errors:{'lane-1':"Couldn't do that. Try again."}}),NOW).html;
  assert.match(h,/role="alert"/);assert.match(h,/data-card="retry"/);
});
test('a busy card disables its action and never shows a spinner', () => {
  const h=P.cardView(card(),ui({busy:new Set(['lane-1'])}),NOW).html;
  assert.match(h,/disabled>Working…/);assert.doesNotMatch(h,/spinner|<progress/);
});
test('names, handles and reasons are escaped', () => {
  const h=P.cardView(card({handle:'<b>x</b>',label:'"Q"',detail:'<img src=x onerror=1>'}),ui(),NOW).html;
  assert.doesNotMatch(h,/<b>x<\/b>|<img src=x/);assert.match(h,/&lt;b&gt;x/);
});
test('no card state uses the words the copy guide bans', () => {
  const states=['ready','collecting','waiting','limit','paused','offline','needs_login','security_check','review','held_back','personal','disconnected','connecting','trouble'];
  for(const state of states){const h=P.cardView(card({state}),ui({openCards:new Set(['lane-1'])}),NOW).html;const text=h.replace(/<[^>]*>/g,' ');assert.doesNotMatch(text,/\blane\b|lease|permit|cooldown|\bhold\b|Error\b|Oops|Please|Successfully|!/i,state);}
});

test('add-account sequence follows what can be seen: waiting, extension, sign in, verified', () => {
  const base=new Set(['old']);const w={base,started:NOW-1000};
  const old=card({lane_id:'old',ig_id:'9',handle:'old'});
  assert.deepEqual([P.wizardState(w,[old],NOW).step,P.wizardState(w,[old],NOW).done],[2,false]);
  assert.equal(P.wizardState({...w,started:NOW-200000},[old],NOW).slow,true);
  const waiting=card({lane_id:'new',ig_id:null,handle:null,state:'needs_login'});
  assert.match(P.wizardState(w,[old,waiting],NOW).text,/Sign in to Instagram/);
  assert.match(P.wizardState(w,[old,{...waiting,state:'security_check',ig_id:'5'}],NOW).text,/security check/);
  const ok=card({lane_id:'new',ig_id:'5',handle:'fresh'});
  assert.deepEqual([P.wizardState(w,[old,ok],NOW).step,P.wizardState(w,[old,ok],NOW).done],[4,true]);
  assert.match(P.wizardState(w,[old,ok],NOW).text,/Verified. @fresh will join when collection runs/);
});
test('add-account refuses the personal account and a duplicate sign-in with a reason', () => {
  const w={base:new Set(['old']),started:NOW};const old=card({lane_id:'old',ig_id:'9',handle:'old'});
  assert.match(P.wizardState(w,[old,card({lane_id:'n',kind:'main',ig_id:'1'})],NOW).text,/personal account/);
  assert.match(P.wizardState(w,[old,card({lane_id:'n',ig_id:'9',handle:'old'})],NOW).text,/already connected in another Chrome profile/);
});
test('the sequence copies the real extension folder and keeps steps 1 and 2 open until the extension checks in', () => {
  const w={base:new Set(),started:NOW};
  const h=P.wizardHTML(w,[],{extension_path:'/Users/me/fl/extension'},NOW);
  assert.match(h,/data-copy="\/Users\/me\/fl\/extension"/);assert.match(h,/Load unpacked/);
  assert.equal((h.match(/aria-current="step"/g)||[]).length,2);
});

test('Accounts has a single page header, two sections, and no second Start button', () => {
  const view=html.slice(html.indexOf('id="view-accounts"'),html.indexOf('id="view-settings"'));
  assert.equal((view.match(/<h1>/g)||[]).length,1);
  assert.match(view,/id="acx-cards"/);assert.match(view,/id="acx-list"/);
  assert.doesNotMatch(view,/Start collecting|Connect accounts|id="seed-in"|id="acc-list"|id="wiz"/);
  assert.match(view,/aria-live="polite"/);
});
test('the old accounts code is gone from app.js and Local AI setup still owns model startup', () => {
  for(const gone of ['function accountRow(','function renderAccounts(','function openWizard(','function parseHandles(','mountCollectionTargets']) assert.ok(!source.includes(gone),gone);
  assert.match(html,/id="acc-start">Start local models/);assert.match(html,/id="acc-start-status"[^>]*role="status"/);
  assert.match(source,/A\.starting \|\| !S\.sc \|\| !!S\.scStale/);assert.match(source,/api\.post\('\/api\/engine\/start', \{\}\)/);
  const accounts=html.slice(html.indexOf('id="view-accounts"'),html.indexOf('id="view-settings"'));
  assert.doesNotMatch(accounts,/id="acc-start"/);
});
test('navigation uses a settings gear and keeps the keyboard shortcut help', () => {
  assert.match(html,/data-view="settings"[^>]*aria-label="Settings"><svg[^>]*><path/);
  assert.doesNotMatch(html,/id="help-btn"/);
  assert.match(source,/if \(k === '\?'\) \{ e\.preventDefault\(\); setHelp\(true\); return; \}/);
});
test('the sidebar badge counts accounts that need a person and ignores disconnected ones', () => {
  assert.match(source,/accs\.filter\(\(a\) => a\.hold && !a\.disconnected\)/);
});
