import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const P=createRequire(import.meta.url)('../../web/accounts.js');
const ui=(extra={})=>({open:new Set(),sel:new Set(),openCards:new Set(),busy:new Set(),errors:{},confirm:null,focus:null,...extra});
const list=(status,label,extra={})=>({direction:'following',wanted:true,status,label,saved:0,expected:null,estimate:false,note:'',...extra});
const seed=(handle,extra={})=>({handle,name:'Brand Name',pic:null,followers:10,following:20,private:false,identity:'known',added_at:'2026-10-03T10:00:00Z',yours:false,source:'you',
  status:'collecting',label:'Collecting',attention:false,saved:2187,expected:3289,pct:66,renamed_from:[],
  lists:{followers:list('off','Off',{direction:'followers',wanted:false}),following:list('collecting','Collecting',{saved:2187,expected:3289})},...extra});

test('every separator, link shape and @ form reads as one handle each', () => {
  const items=P.readPaste('@Alpha, https://www.instagram.com/beta/?hl=en;gamma\n  instagram.com/delta#x  (@epsilon) "zeta"');
  assert.deepEqual(items.map(i=>i.handle),['alpha','beta','gamma','delta','epsilon','zeta']);
});
test('bad input gets a plain reason and good input is never blocked by it', () => {
  const items=P.readPaste('good https://instagram.com/p/Cxyz/ https://evil.test/x thirty_one_characters_long_name_xx a!b good');
  assert.deepEqual(items.map(i=>[i.state,i.reason]),[['new',null],['invalid','post_link'],['invalid','not_instagram'],['invalid','too_long'],['invalid','not_valid'],['duplicate','duplicate']]);
  assert.ok(items.filter(i=>i.state!=='new').every(i=>i.message));
  assert.equal(P.summaryText(P.summarize(items)),"1 ready · 1 repeated · 4 can't be read");
});
test('the browser and the server read the same links the same way', () => {
  const good=['@NASA','HTTPS://M.INSTAGRAM.COM/%4eASA/?hl=en','instagram.com/nasa#profile','https://www.instagr.am/NASA/'];
  assert.deepEqual(good.map(t=>P.readToken(t).handle),['nasa','nasa','nasa','nasa']);
  for(const bad of ['https://instagram.com.evil.test/nasa','https://evil.test@instagram.com/nasa','https://www.instagram.com/reel/abc','https://www.instagram.com/nasa/extra','https://www.instagram.com/','ftp://instagram.com/nasa','alice~123'])
    assert.equal(P.readToken(bad).handle,null,bad);
});
test('a row says what is happening in words, with a bar only when there is something to show', () => {
  const {html,attrs}=P.rowView(seed('austinsutor'),ui());
  assert.match(html,/@austinsutor/);assert.match(html,/Following 2,187 of 3,289/);assert.match(html,/Collecting/);assert.match(html,/style="width:66%"/);
  assert.equal(attrs['data-status'],'collecting');
  const done=P.rowView(seed('x',{status:'done',label:'Done',pct:100,lists:{followers:list('off','Off',{direction:'followers',wanted:false}),following:list('done','Done',{saved:90,expected:90})}}),ui());
  assert.match(done.html,/acx-bar none/);assert.match(done.html,/Following done, 90/);
});
test('private, capped and unreadable lists are named plainly and flagged', () => {
  const closed=P.rowView(seed('closed',{status:'private',label:'Private',attention:true,lists:{followers:list('off','Off',{direction:'followers',wanted:false}),following:list('private','Private')}}),ui());
  assert.match(closed.html,/Following private/);assert.match(closed.html,/Needs a look/);assert.match(closed.html,/>Private</);
  const capped=P.rowView(seed('capped',{status:'capped',label:'Capped by Instagram',lists:{followers:list('capped','Capped by Instagram',{direction:'followers',saved:50,expected:9000}),following:list('off','Off',{wanted:false})}}),ui());
  assert.match(capped.html,/Followers 50 of 9,000/);assert.match(capped.html,/Capped by Instagram/);
});
test('a renamed profile says so, and a long handle cannot break the row', () => {
  const long='a'.repeat(30);
  const h=P.rowView(seed(long,{renamed_from:[{from:'oldname',at:'2026-10-01'}],name:'N'.repeat(80)}),ui()).html;
  assert.match(h,/Renamed from @oldname/);assert.match(h,new RegExp('@'+long));
  assert.match(h,/class="acx-handle"/);
});
test('selection, open details and focus come from state, so a refresh keeps them', () => {
  const s=seed('keep');
  const a=P.rowView(s,ui({sel:new Set(['keep']),open:new Set(['keep']),focus:'keep'}));
  assert.match(a.attrs.class,/is-selected/);assert.match(a.attrs.class,/is-open/);assert.equal(a.attrs.tabindex,'0');assert.equal(a.attrs['aria-selected'],'true');
  assert.match(a.html,/checked/);assert.match(a.html,/Collect following of @keep/);assert.match(a.html,/Do next/);assert.match(a.html,/Open on Instagram/);
  const b=P.rowView(s,ui());
  assert.equal(b.attrs.tabindex,'-1');assert.doesNotMatch(b.html,/Do next/);
});
test('each row has one inline action that matches its state', () => {
  assert.match(P.rowView(seed('a'),ui()).html,/data-act="pause"[^>]*aria-label="Pause @a"/);
  const paused=seed('b',{status:'paused',label:'Paused',lists:{followers:list('off','Off',{direction:'followers',wanted:false}),following:list('paused','Paused')}});
  assert.match(P.rowView(paused,ui()).html,/data-act="resume"[^>]*aria-label="Resume @b"/);
  assert.match(P.rowView(seed('c',{status:'removed',label:'Removed'}),ui()).html,/data-act="restore"/);
  const done=seed('d',{status:'done',label:'Done',lists:{followers:list('off','Off',{direction:'followers',wanted:false}),following:list('done','Done')}});
  assert.doesNotMatch(P.rowView(done,ui()).html,/data-act="pause"|data-act="resume"/);
});
test('an optimistic add shows a quiet pending row and cannot be selected', () => {
  const row=P.provisional({handle:'new.brand',person:null},['following']);
  const h=P.rowView(row,ui());
  assert.match(h.html,/Adding…/);assert.match(h.attrs.class,/is-pending/);assert.match(h.html,/data-sel[^>]*disabled/);
  assert.equal(row.lists.followers.status,'off');
});
test('optimistic actions answer at once and the originals stay untouched for rollback', () => {
  const items=[seed('a'),seed('b')];
  const frozen=JSON.stringify(items);
  assert.deepEqual(P.applyLocal(items,'remove',['a']).map(s=>s.handle),['b']);
  assert.equal(P.applyLocal(items,'pause',['a'])[0].status,'paused');
  assert.equal(P.applyLocal(P.applyLocal(items,'pause',['a']),'resume',['a'])[0].status,'waiting');
  const dirs=P.applyLocal(items,'directions',['b'],['followers','following'])[1].lists;
  assert.deepEqual([dirs.followers.wanted,dirs.following.wanted],[true,true]);
  assert.equal(JSON.stringify(items),frozen);
});
test('the queue sentence only says what the server measured', () => {
  assert.equal(P.queueText({status:'estimated',eta:{scope:'known_lists',low_minutes:90,high_minutes:180}}),'Known lists: about 2 h to 3 h');
  assert.equal(P.queueText({status:'estimated',eta:{scope:'current_queue',low_minutes:20,high_minutes:35}}),'Current queue: about 20 min to 35 min');
  assert.equal(P.queueText({status:'paused',message:'Lists paused · progress saved'}),'Lists paused · progress saved');
  assert.equal(P.queueText({status:'warming_up',message:'Waiting for saved pages'}),'');
  assert.equal(P.queueText(null),'');
});
test('numbers and times follow the copy guide', () => {
  assert.equal(P.plural(1,'profile'),'1 profile');assert.equal(P.plural(2,'profile'),'2 profiles');assert.equal(P.plural(12345,'profile'),'12,345 profiles');
  const now=Date.parse('2026-10-04T12:00:00Z');
  assert.deepEqual(['2026-10-04T11:59:58Z','2026-10-04T11:55:00Z','2026-10-04T09:00:00Z','2026-10-02T12:00:00Z'].map(t=>P.relative(t,now)),['moments ago','5m ago','3h ago','2d ago']);
});
