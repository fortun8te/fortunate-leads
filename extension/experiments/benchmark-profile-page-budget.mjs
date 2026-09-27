// node extension/experiments/benchmark-profile-page-budget.mjs [local.har]
// With no file, reports a SYNTHETIC workload. Reads files only, never fetches.
import {readFileSync} from 'node:fs';
import {resourceBudget} from './profile-page-budget.mjs';
const path = process.argv[2];
const entries = path ? JSON.parse(readFileSync(path, 'utf8')).log.entries : [
  ['document', 180000], ['script', 2100000], ['stylesheet', 200000],
  ['xhr', 35000], ['fetch', 65000], ['font', 120000], ['media', 2000000],
  ...Array.from({length: 12}, () => ['image', 250000]),
].map(([_resourceType, bytes]) => ({_resourceType, response: {_transferSize: bytes}}));
console.log(JSON.stringify({source: path ? 'local_har_replay' : 'synthetic_not_instagram_measurement',
  ...resourceBudget(entries),
  throughputGainMeasured: false, ramGainMeasured: false,
  note: 'Transfer upper bound only. Request gap, holds, API payloads and main document stay unchanged.'}, null, 2));
