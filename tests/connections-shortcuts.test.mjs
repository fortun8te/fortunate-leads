import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// Execute the production key handler with recorded UI effects, without booting the app.
const app = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const start = app.indexOf("document.addEventListener('keydown', (e) => {", app.indexOf('// ---------- keyboard ----------'));
const end = app.indexOf('\n});', start) + 4;
assert.ok(start >= 0 && end > start, 'production keyboard handler exists');
const handlerCode = app.slice(start, end);

function mount() {
  let handler;
  const effects = [];
  const record = name => (...args) => effects.push([name, ...args]);
  const classes = new Set(['comparing']);
  const help = { hidden: true };
  const nodes = {
    '#pane-map': { classList: { contains: name => classes.has(name) } },
    '#help': help,
    '#tag-in': { focus: record('tag-focus') },
    '#q': { focus: record('search-focus') }
  };
  const location = { hash: '#/map' };
  const context = {
    document: { addEventListener: (_, fn) => { handler = fn; }, documentElement: { dataset: { density: 'comfortable' } } },
    $: selector => nodes[selector],
    S: { view: 'map', person: { id: 123, handle: 'overview_person', status: null } },
    M: { fit: record('fit'), zoomBy: record('zoom'), toggleLabels: record('labels'), next: record('next') },
    gPending: 0, CYCLE: [null, 'saved'], STATUSES: ['saved', 'contacted', 'replied', 'done', 'skipped'],
    mark: record('mark'), window: { open: record('open') }, applyDensity: record('density'),
    location, hashFor: view => '#/' + view
  };
  vm.runInNewContext(handlerCode, context);
  const press = (key, tagName = 'BUTTON') => handler({ key, target: { tagName }, preventDefault() {}, metaKey: false, ctrlKey: false, altKey: false });
  return { effects, classes, help, location, press };
}

test('candidate focus cannot run overview navigation or mutate/open the retained profile', () => {
  const ui = mount();
  for (const key of ['m', '0', '1', '2', '3', '4', '5', 'o', 't', 'f', '+', '=', '-', 'l', 'n']) ui.press(key);
  assert.deepEqual(ui.effects, []);
});

test('returning to overview restores navigation and actions for its selected person', () => {
  const ui = mount();
  ui.classes.delete('comparing');
  for (const key of ['m', '0', '2', 'o', 't', 'f', '+', '-', 'l', 'n']) ui.press(key);
  assert.deepEqual(ui.effects, [
    ['mark', 123, 'saved'], ['mark', 123, null], ['mark', 123, 'contacted'],
    ['open', 'https://www.instagram.com/overview_person/', '_blank', 'noopener'],
    ['tag-focus'], ['fit'], ['zoom', 1.4], ['zoom', 1 / 1.4], ['labels'], ['next']
  ]);
});

test('comparison retains app help, density, search and navigation shortcuts', () => {
  const ui = mount();
  ui.press('?');
  assert.equal(ui.help.hidden, false);
  ui.press('d');
  ui.press('/');
  ui.press('g');
  ui.press('l');
  assert.deepEqual(ui.effects, [['density', 'compact'], ['search-focus']]);
  assert.equal(ui.location.hash, '#/leads');
});

test('typing a shortcut in an input never acts on the overview selection', () => {
  const ui = mount();
  ui.classes.delete('comparing');
  for (const key of ['m', '1', 'o', 't']) ui.press(key, 'INPUT');
  assert.deepEqual(ui.effects, []);
});
