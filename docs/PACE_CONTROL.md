# Pace control

One place to change how fast an Instagram account collects, with no code change and no extension reload.
This file is the spec the Accounts page follows. Draft written first; the numbers at the end are measured.

## One line

```sh
ops/set-pace                         # show what is running now, and why
ops/set-pace slow                    # or normal, or fast (all accounts)
ops/set-pace slow --account @trashtheturtles
ops/set-pace --list-gap 9-14         # seconds between list pages, within the allowed range
ops/set-pace --budget-list 400       # daily list pages
```

Every command prints what changed and the new expected requests per hour. Nothing can go past the
bounds in `extension/lib/pace-limits.js`; a value outside them is clamped and the output says so.

## What a pace is

Six numbers per account. Seconds for time, pages or requests for counts. The extension draws each
gap at random inside its range (the range is the jitter).

| field | meaning | default |
|---|---|---|
| `list_gap` | seconds between list pages | 7 to 12 |
| `profile_gap` | seconds between bio reads | 35 to 70 |
| `spacing` | seconds between any two requests of one account | 2 to 5 |
| `break_every` | list pages between breaks | 40 to 60 |
| `break_len` | seconds per break | 90 to 180 |
| `window_max` | requests in any 11 minutes (a ceiling, never a target) | 72 |

Daily budgets are part of the same readout and the same command, but **no daily cap is the default** (Michael
removed the daily request budget as a stop). `0` means no cap. `ops/set-pace --no-daily-cap` sets both to 0; a number
(up to `budget_max`) sets an optional cap, and the readout then says when it would be reached, so a limit never arrives
as a surprise. Storage is unchanged: the global value in the `budget` setting, a per-account value in `accounts.budget`.
The stored defaults in `server/db.py` and `core.js` still say 3000/300 until the merge step "clear the live budget" runs.

Not tunable, on purpose: page size (following is 200 rows, followers 25 or 50, chosen by the job),
the 11 minute window length, cooldown lengths, strike rules, holds, the request permit gate, the main
account protection. See "What the knob can never change".

## Where the numbers live (one place)

`extension/lib/pace-limits.js` holds the defaults, the bounds (`floor`, `ceiling`, `min_spread` per
field, `budget_max`) and the three presets. The extension, the server (`server/pace_limits.py`), the
CLI and the tests all read that one file. To install evidence-backed bounds, edit the JSON block
between `BEGIN-PACE-LIMITS-JSON` and `END-PACE-LIMITS-JSON` and nothing else. The shipped bounds are
`provisional`: their fast side equals today's defaults, so the knob can slow down freely and cannot yet
go faster than the defaults. `fast` is therefore the same as `normal` until the bounds are widened.

## API

Both calls are on the workspace origin (port 8777, same rules as every `/api/*` call).

### `GET /api/pace`

Returns the limits, presets, global setting, and one entry per account:

```json
{
  "v": 1,
  "limits": { "status": "provisional", "note": "...", "fields": { "list_gap": { "label": "...", "unit": "s", "default": [7, 12], "floor": 7, "ceiling": 120, "min_spread": 1.25 } }, "budget_max": { "list": 3000, "profile": 5000 } },
  "presets": [ { "id": "slow", "label": "Slow", "values": { "list_gap": [14, 24] }, "clamped_fields": [] } ],
  "defaults": { "list_gap": [7, 12] },
  "global": { "preset": "normal", "values": { "list_gap": [7, 12] }, "id": "lg7-12|pg35-70|sp2-5|be40-60|bl90-180|wm72", "set_at": null },
  "accounts": [ {
    "lane_id": "ln_abc", "name": "@trashtheturtles", "handle": "trashtheturtles", "online": true, "collection_protected": false,
    "scope": "global", "preset": "normal", "values": { "...": "effective values" }, "id": "...",
    "applied": { "id": "...", "matches": true, "seen_at": "2026-10-04T12:00:00Z", "age_s": 9 },
    "budget": { "list": 400, "profile": 100, "used_today": { "list": 12, "profile": 0 } },
    "expected": { "list_per_hour": 285, "bio_per_hour": 69, "all_per_hour": 354, "binds": "gap", "text": "..." },
    "observed": { "pages_hour": 190, "requests_hour": 191, "bios_hour": 0 },
    "why": [ "plain sentences, ready to show" ]
  } ],
  "safety": { "fixed": [ "plain sentences" ] },
  "at": "2026-10-04T12:00:00Z"
}
```

`applied` is what the extension last reported back. `matches: false` means the extension has not
picked the new value up yet (normally under 30 seconds). `applied: null` means that extension predates
this feature and has not reloaded.

### `POST /api/pace`

```json
{ "scope": "global", "preset": "slow" }
{ "scope": "account", "lane_id": "ln_abc", "values": { "list_gap": [9, 14] } }
{ "scope": "account", "lane_id": "ln_abc", "reset": true }
{ "scope": "global", "budget": { "list": 400 } }
{ "scope": "global", "preset": "fast", "dry_run": true }
```

`scope` defaults to `global`. `values` may be partial; missing fields keep the current value at that
scope. A scalar (`"list_gap": 9`) scales the default range around that centre; a pair is taken as is.
An account setting overrides the global one for that account only, and is tied to the Instagram
identity it was set for: if the Chrome profile signs in as someone else, the override stops applying.
The main account cannot be given a pace.

Response:

```json
{ "ok": true, "saved": true, "scope": "global",
  "before": { "preset": "normal", "values": {}, "id": "..." },
  "after":  { "preset": "slow",   "values": {}, "id": "..." },
  "changed": ["list_gap", "profile_gap"],
  "clamped": [ { "field": "list_gap", "requested": [5, 9], "applied": [7, 12], "why": "The fastest allowed is 7 s." } ],
  "expected": { "before": { "all_per_hour": 354 }, "after": { "all_per_hour": 180 } },
  "delivery": "Reaches each extension within about 30 seconds and applies to its next request." }
```

Errors are `400 {"ok": false, "error": "plain sentence"}`.

## Relation to the popup "Collection speed" setting

Main already has a popup setting (`collectionSpeed`, bound to the turtles identity and install) that only changes how
often the worker rechecks for work while bio reading is paused. It does not change any request gap. It still works as
before and is verified end to end in `node tests/e2e/driver.mjs --pace`. The pace control above is what changes request
gaps, breaks and the window, for any account, from the server.

## How a change travels

server setting -> `ext_state()` -> heartbeat and `/api/ext/next` replies -> `applyServer()` ->
clamped again with the extension's own copy of the bounds -> stored in `chrome.storage.local`
(`paceCfg`) -> pace clocks rescheduled -> next request. The extension does a forced heartbeat at the
start of every step, so the lag is at most one loop sleep (15 s) plus one round trip. An extension that
is paused or offline picks it up when it resumes.

A change applies to the wait already running, not only to the next one: the pending gap is redrawn
from the last request time, so slowing down is immediate and speeding up is immediate (within the
bounds). A break in progress is never cut short.

## What the knob can never change

These run in the extension and the server independent of any pace value, and always win:

- Instagram rate limits and soft blocks: cooldown 10 minutes, doubling per hit within 24 hours up to 6 hours;
  three hits within 24 hours force at least 2 hours of rest for that request kind; Retry-After wins.
- The workspace-wide safety hold after any rate limit, soft block, login or security response.
- Scraping warning, login and security page holds; an unconfirmed request stops collection until reviewed.
- Paused stages, paused accounts, the offline state, local pause.
- An optional daily cap, if one is set (within `budget_max`).
- One Instagram request at a time across the workspace (server permit gate, 2 s spacing).
- Main account protection and the warned-account hold.

A pace only decides how long to wait *between* requests that are already allowed.

## Copy for the UI

Follow `docs/COPY.md`. Suggested labels: **Pace** with **Slow**, **Normal**, **Fast**; helper text from
`why[]`; show `expected.text` under it; show a quiet note when `applied.matches` is false ("Applying…").
Do not show field names or ids. If a preset is clamped (`clamped_fields` non-empty), disable it and say
"Not available yet".

## Measured results

Filled in at the end of the work (see `work/oct4-reports/speed-knob.md`).
