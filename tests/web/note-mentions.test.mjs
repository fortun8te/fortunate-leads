import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../../web/note-mentions.js', import.meta.url), 'utf8');
function fixture() {
  const document = { activeElement: null, createElement: () => new Element() };
  class Element {
    constructor() { this.attrs = {}; this.events = {}; this.children = []; this.value = ''; this.isConnected = true; this.dataset = { id: '42' }; }
    setAttribute(k,v) { this.attrs[k] = v; }
    removeAttribute(k) { delete this.attrs[k]; }
    addEventListener(k, fn) { (this.events[k] ||= []).push(fn); }
    dispatchEvent(e) { for (const fn of this.events[e.type] || []) fn(e); }
    insertAdjacentElement(_, el) { this.picker = el; }
    replaceChildren() { this.children = []; }
    append(...els) { this.children.push(...els); }
    focus() { document.activeElement = this; }
    setSelectionRange(a,b) { this.selectionStart = a; this.selectionEnd = b; }
  }
  const context = vm.createContext({ document, setTimeout, clearTimeout, Event: class { constructor(type) { this.type=type; } } });
  vm.runInContext(source, context);
  const input = new Element(); input.focus();
  function type(value) { input.value = value; input.setSelectionRange(value.length,value.length); input.dispatchEvent({type:'input'}); }
  function key(key) { const e = { type:'keydown', key, preventDefault() { this.prevented = true; }, stopPropagation() {} }; input.dispatchEvent(e); return e; }
  return { api: context.LeadNoteMentions, input, type, key, createInput: () => new Element() };
}
const wait = () => new Promise(resolve => setTimeout(resolve, 190));
test('exact surviving references reject partial handles and allow sentence punctuation', () => {
  const { api } = fixture();
  assert.equal(api.contains('Friends with @alex.', '@alex'), true);
  assert.equal(api.contains('@alex.other', '@alex'), false);
  assert.equal(api.contains('email@alex', '@alex'), false);
  assert.equal(api.surviving('Gone', [{ token:'@alex', person_id:2 }]).length, 0);
});
test('keyboard selection inserts the handle and stores its identity before the input save', async () => {
  const { api, input, type, key } = fixture();
  let refs = [], saved;
  api.attach(input, { search: async () => ({people:[{id:2,handle:'alex'}, {id:3,handle:'alice'}]}), change: value => { refs=value; } });
  input.addEventListener('input', () => { saved = refs.map(r => ({...r})); });
  type('Friends with @al'); await wait();
  assert.equal(input.attrs['aria-expanded'], 'true');
  assert.equal(key('ArrowDown').prevented, true);
  assert.equal(key('Enter').prevented, true);
  assert.equal(input.value, 'Friends with @alice ');
  assert.deepEqual(JSON.parse(JSON.stringify(saved)), [{person_id:3,token:'@alice'}]);
  assert.equal(input.attrs['aria-expanded'], 'false');
});
test('editing the token removes the selected reference', () => {
  const { api, input, type } = fixture(); let refs;
  input.value = '@alex';
  api.attach(input, { selected:[{person_id:2,token:'@alex'}], search: async () => ({people:[]}), change:value => { refs=value; } });
  type('Someone else'); assert.equal(refs.length, 0);
});
test('late results cannot reopen a picker after escape or blur', async () => {
  const { api, input, type } = fixture(); let complete;
  api.attach(input, { search: () => new Promise(resolve => { complete=resolve; }), change:() => {} });
  type('@al'); await wait();
  input.dispatchEvent({type:'blur'});
  complete({people:[{id:2,handle:'alex'}]}); await Promise.resolve();
  assert.equal(input.attrs['aria-expanded'], 'false');
});
test('escape closes results without clearing the note or allowing the modal shortcut', async () => {
  const { api, input, type, key } = fixture();
  api.attach(input, { search:async () => ({people:[{id:2,handle:'alex'}]}), change:() => {} });
  type('@al'); await wait();
  assert.equal(key('Escape').prevented, true);
  assert.equal(input.value, '@al');
  assert.equal(input.attrs['aria-expanded'], 'false');
});
test('typing only @ opens the initial profile menu', async () => {
  const { api, input, type } = fixture(); let query;
  api.attach(input, { search:async value => { query=value; return {people:[{id:2,handle:'alex'}]}; }, change:() => {} });
  type('Knows @'); await wait();
  assert.equal(query, '');
  assert.equal(input.attrs['aria-expanded'], 'true');
  assert.equal(input.picker.children[0].attrs['aria-label'], 'alex, @alex');
});
function rerender(f, options) {
  const saved = f.api.capture(f.input), input = f.createInput();
  input.value = f.input.value;
  input.setSelectionRange(f.input.selectionStart, f.input.selectionEnd);
  f.input.isConnected = false;
  f.api.attach(input, options);
  input.focus();
  f.api.restore(input, saved);
  return input;
}
test('autosave rerender preserves visible options, caret and keyboard selection', async () => {
  const f = fixture(); let calls = 0;
  const options = { search: async () => { calls++; return { people:[{id:2,handle:'alex'},{id:3,handle:'alice'}] }; }, change:()=>{} };
  f.api.attach(f.input, options); f.type('Knows @'); await wait(); f.key('ArrowDown');
  const fresh = rerender(f, options);
  assert.equal(fresh.attrs['aria-expanded'], 'true');
  assert.equal(fresh.attrs['aria-activedescendant'], 'note-mention-option-1');
  assert.equal(fresh.selectionStart, 'Knows @'.length);
  fresh.dispatchEvent({type:'keydown', key:'Enter', preventDefault(){}, stopPropagation(){}});
  assert.equal(fresh.value, 'Knows @alice ');
  assert.equal(calls, 1, 'visible options survive without repeating the search');
});
test('autosave rerender restarts an active query whose results have not arrived', async () => {
  const f = fixture();
  const options = {search:async () => ({people:[{id:2,handle:'alex'}]}), change:()=>{}};
  f.api.attach(f.input, options); f.type('@');
  const fresh = rerender(f, options); await wait();
  assert.equal(fresh.attrs['aria-expanded'], 'true');
  assert.equal(fresh.picker.children[0].attrs['aria-label'], 'alex, @alex');
});
test('suggestions display an avatar, readable identity and at most two saved badges', async () => {
  const { api, input, type } = fixture();
  api.attach(input, { search:async () => ({people:[{id:2,handle:'alex',name:'Alex Lee',pic:'/img/2',badges:['Friend','Colleague','Client']}]}), change:()=>{} });
  type('@al'); await wait();
  const option = input.picker.children[0];
  assert.equal(option.attrs['aria-label'], 'Alex Lee, @alex, saved: Friend, Colleague');
  assert.equal(option.children[0].children[0].src, '/img/2');
  assert.equal(option.children[1].children[0].children[0].textContent, 'Alex Lee');
  assert.equal(option.children[1].children[1].children.length, 2);
});
test('autosave rerender does not reopen a picker dismissed with Escape', async () => {
  const f = fixture();
  const options = {search:async () => ({people:[{id:2,handle:'alex'}]}), change:()=>{}};
  f.api.attach(f.input, options); f.type('@'); await wait(); f.key('Escape');
  const fresh = rerender(f, options); await wait();
  assert.equal(fresh.attrs['aria-expanded'], 'false');
  assert.equal(fresh.picker.children.length, 0);
});
test('autosave rerender does not reopen after selecting a profile or escaping a pending query', async () => {
  const f = fixture();
  const options = {search:async () => ({people:[{id:2,handle:'alex'}]}), change:()=>{}};
  f.api.attach(f.input, options); f.type('@'); await wait(); f.key('Enter');
  const fresh = rerender(f, options);
  assert.equal(fresh.value, '@alex ');
  assert.equal(fresh.attrs['aria-expanded'], 'false');
  const pending = fixture(); pending.api.attach(pending.input, options); pending.type('@'); pending.key('Escape');
  const next = rerender(pending, options); await wait();
  assert.equal(next.attrs['aria-expanded'], 'false');
});
