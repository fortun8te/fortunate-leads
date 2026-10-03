# Live collection verification

A passing simulator proves recovery against controlled responses. A real Instagram run must be recorded separately. Do not describe simulated hours as live hours.

## Before starting

- Confirm the installed server and extension versions agree with the release being tested. Preserve the existing database and collection checkpoints.
- Use an authorized alternate account with a current, verified Instagram identity. The main account stays personal. Warned accounts remain blocked.
- Choose Following only and one target for the first run. Start through the app's collection control. An existing warning requires the explicit review or selected-account authorization supported by that release.
- Keep the current pacing, budgets, cooldowns and warning gates. A test must never increase them or clear protective state to make progress.
- Record the start time, account, target, list direction, current saved count and cursor. Keep this record local.

## Observe

Read `GET /api/scraper` before starting, after the first saved page, after 5 minutes, after 15 minutes, and then about every 30 minutes while collection remains active. Each read is a dated snapshot, not proof of continuous observation.

For a local snapshot on the default port:

```sh
curl --fail --silent --show-error --max-time 20 http://127.0.0.1:8777/api/scraper
```

| Evidence | What to check |
|---|---|
| `accounts` | Intended alternate is online, identity matches, warning/cooldown/hold stays visible. Blocked accounts and main account remain excluded. |
| `lists` | The target's current-run saved count advances. Cursor changes between pages. Complete requires proven page history; partial remains partial. |
| `collection` | Progress distinguishes pending, partial, finished and needs review. Unknown sizes do not become invented completion percentages. |
| `soak` | Saved pages and people advance during active work. Report observed values, without a promised rate. |
| `queue`, account budgets | Queue and daily allowance explain waits. A paused or waiting account must make no new requests. |
| `alerts`, account error fields | First warning, login/security page, restriction or cooldown is recorded immediately. The existing collector must stop or wait according to its protective policy. |

Stop at the first Instagram warning. Do not rotate identities, repeatedly retry a failed endpoint, or resume merely because the cooldown time passed. Resolve or review the condition through the supported operator flow.

## Recovery check

After several pages have been saved, use Stop in the app. Confirm counts and cursor stay saved and no new Instagram requests begin. Start again only while the authorized account remains healthy. Confirm the next page continues from the saved cursor and counts do not duplicate.

Use the simulator for forced server outages, lost acknowledgments and stopped workers. Do not force those faults against the live account or restart its server for a soak test.

```sh
PYTHONPATH=server python3 -m unittest discover -s server/tests -p 'test_collection*recovery.py'
node --test extension/test/background-controls.test.mjs
node tests/e2e/driver.mjs --hours 24 --keep
node tests/e2e/driver.mjs --lanes 2 --hours 12 --keep
```

The simulator uses a temporary database and free ports. It loads the actual extension and server, but Instagram, Chrome worker scheduling and time are emulated. Its result cannot establish whether Instagram will accept the real account for hours.

## Record the result

Record actual elapsed live time, snapshot times, target, account, starting and ending counts, pages saved, completion state, interruption/resume result, first warning time and stop reason. Attach version identities and locally saved evidence. Label unattended gaps, missing extension readback and unfinished targets explicitly. A first saved page proves collection started; only sustained recorded progress supports a long-run claim.
