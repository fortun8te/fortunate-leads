# Automatic speed ramp

Off until switched on. Raises the speed of a healthy collector step by step, backs off at the first sign of trouble,
and needs no extension change or reload (each step travels through the ordinary pace delivery, `docs/PACE_CONTROL.md`).
Code: `server/ramp.py` (controller, runs from a server worker every 30 s), `ops/set-pace --ramp`, tests `server/tests/test_ramp.py`,
simulator `node tests/e2e/driver.mjs --ramp`. Failure handling: `docs/RECOVERY.md`.

```sh
ops/set-pace --ramp on                  # default: @trashtheturtles, x1.5 after every 2 h of clean collection, ceiling 285 pages/h
ops/set-pace --ramp status              # what it decided and why, in plain words
ops/set-pace --ramp off                 # stop stepping; the pace stays where it is   (--ramp restore: also reset the pace)
ops/set-pace --ramp resume --account @trashtheturtles   # after the ramp stopped and a person fixed the cause
ops/set-pace --ramp on --interval 2 --step-factor 1.5 --account @trashtheturtles
ops/set-pace --ceiling 330              # separate, explicit choice. Above 285 nothing has been measured.
```

## The rule

Every `interval` of clean, continuous collection the ramp measures the account's real pages per hour in that window and
aims at `measured x step_factor`, never above `ceiling` (default 285, the top of the clean envelope in `docs/PACING_EVIDENCE.md`).
It changes only the list gap (a range kept in the same shape), holds the extra time per page that Instagram and the app add
(measured, kept constant), and rounds toward slower, so a step can undershoot its target but never overshoot the ceiling.
At the ceiling it holds and says "at the ceiling". Above the ceiling is only possible by the explicit `--ceiling N`, and every readout then says it is unmeasured.
It never goes below the lower list-gap bound in `extension/lib/pace-limits.js` (4.8 to 8.2 s); if that bound stops a step it says "at the fastest allowed gap".

A window counts only if collection ran at least 90% of it (no stretch of 20 minutes without a saved page), at least 30 pages
were saved, and none of these happened: 429 or soft block, challenge, login, scraping warning, hold, unconfirmed request, a cooldown or pause, the
extension offline, two or more followers home redirects, five empty pages in a row, low disk (under 2 GB), response time materially up.
"Response time" is a proxy: the median time between saved pages minus the configured mean gap (the extension does not report round-trip time).
A window that does not count restarts the clock; nothing steps.

## What it does on trouble (the existing safety stops are never touched and always win)

| event | ramp does |
|---|---|
| 429 or soft block | previous level at once, hold 2 clean windows, then climb again. A second one within 24 h: stop, a person resumes. |
| challenge, login, scraping warning, hold, unconfirmed request | previous level, stop, a person resumes (`--ramp resume`) |
| two or more followers home redirects in a window | previous level, hold 1 window |
| response time up once | hold (early warning). Twice in a row: previous level, hold 2 windows |
| step that made it slower than before (under 90%) | undo it, hold 2 windows |
| outage over 30 min (server down, Mac asleep, Chrome closed) | resume one level lower, never from the top |
| somebody changes the pace by hand | the ramp stands down and leaves the pace alone |
| main account, held or warned account, other handles | never ramped |

No daily cap is involved. State lives in the `ramp` setting, so it survives restarts. Every decision is a `ramp.*` event in the structured log and in the status readout.
