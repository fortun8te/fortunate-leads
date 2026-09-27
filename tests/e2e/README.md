# End-to-end simulator

```
node tests/e2e/driver.mjs
```

This runs 8 simulated hours of scraping in about 50 s of real time. It needs Node 18 or later and Python 3, and has no dependencies. It exits 0 when every check passes and 1 otherwise. A failed run keeps its temp dir, whose path is printed; it holds `result.json`, `log.json`, `server.log` and `fake_ig.log`.

Options:
- `--hours N`: simulated hours. The default is 8. A 24 h run takes about 90 s.
- `--outage-min N`: how long the server stays offline. The default is 4.
- `--keep`: keep the temp dir.
- `--verbose`: print the timeline, the passed checks and the extension's own trail.
- `--lanes N`: 1–16 accounts instead (see below). `--lanes 1,2,4` runs each count in its own process and prints the time to 10k connections side by side. For a larger-account check, run `--lanes 4,10 --hours 3`.

## Lanes (`--lanes N`)

N emulated Chrome profiles, each with its own storage, tabs, alarms and service worker running the real extension, all against one server. Each profile is logged in to its own fake Instagram account (`X-Sim-Account`; the home page names the viewer so the extension can detect it) with its own failures, counted per account: lane 1 gets a 429 on its 200th list request, lane 2 is logged out on its 20th (Michael logs it back in 45 min later), lane 3 a soft block on its 45th, lane 4 "please wait" on its 60th. No harness faults. 10 lists, 16.9k connections; the run stops when every list is done (cap 12 simulated hours).

Checks: every list complete with edges equal to what the fake served, no page fetched twice across lanes and none posted twice, a list is never requested by a second lane while the lane that had it is still working it, the logged-out lane's list moves to another lane from its saved cursor (and the server records the handoff), no lane requests inside its own cooldown, a shared warning stops new requests across other lanes, the server knows each lane's account, every outbox ends empty.

```
lanes  time to 10k connections  all lists done
    1                   1.42 h          2.50 h
    2                   1.00 h          1.67 h
    4                   0.50 h          0.92 h
```
Lane 2 is logged out for 45 min in the 2- and 4-lane runs, which is why 2 lanes are not twice as fast.

## What runs

| piece | what it is |
|---|---|
| extension | The real `extension/background.js`, `lib/core.js`, `bridge.js` and `relay.js`, loaded unmodified into `vm` contexts. The harness emulates the Chrome APIs they use: `storage.local` (kept across worker restarts), `alarms`, `tabs`, `scripting.executeScript` (the injected function is serialized and runs in the tab's context, as in Chrome), `runtime` messaging, and a stopped worker being restarted by an alarm or a message. |
| server | The real `server/server.py`, run through `sim_server.py`. The shim changes two things. First, `datetime.now()` follows the simulated clock, sent by the driver as `X-Sim-Now`, so leases, soak windows and `today` agree with compressed time. Second, the background workers (`qualify_batch`, `plan_profiles`) run through `GET /__sim/tick` every 30 simulated seconds instead of on wall-clock threads. `llm_step` and `pfp_step` are not run. The server has a `--port` flag, and the driver picks free ports. |
| Instagram | `fake_ig.py`. It serves followers pages of about 18-25 users with `QVFE..` cursors, following pages of 50 with numeric cursors, `has_more` that is sometimes missing at the tail, `should_limit_list_of_followers` (a capped verified account), a private account, `/users/{pk}/info/`, and profile HTML pages that `bridge.js` scans. `web_profile_info` always returns 429 and must never be called. |
| clock | Every timer in the worker, the tabs and the operator goes into one virtual event queue. Time only advances when no real HTTP is in flight, so 10 s gaps and 3 h cooldowns cost nothing. |

## Scenario (default)

The run starts on Thu 24 Sep at 19:00 in Europe/Amsterdam, so local midnight falls inside it. It queues 15 seeds: 10 followers lists of 50 to 3000 people (one capped at 49, one private) and 5 following lists of 200 to 2800. Michael has one pinned background instagram.com tab open.

Instagram failures come from `SCHEDULE` in `driver.mjs`, keyed by request number:
- `for (;;);` prefix
- a slow answer (20 s)
- 429
- a hang (the page's 30 s abort)
- a redirect to the login page
- "please wait a few minutes"
- a frozen tab (the 45 s executeScript timeout, followed by a tab reload)
- HTTP 500
- `checkpoint_required`
- an HTML login page
- a soft block (`users:[]` with `has_more`). This is the third list hit, so lists rest for at least 2 h while bios remain available.
- on bios: a 429 with `Retry-After: 3600`, then `useragent mismatch`, which switches bios to page loads for 6 h
- a profile page that never loads

Harness faults come from `FAULT` in `driver.mjs`:
- server responses lost after the commit
- the worker stopped mid-request
- the worker stopped after the server committed a page but before the outbox was updated
- an idle worker stopped by Chrome and woken by the alarm
- the server offline for 4 min

The operator resumes holds 12 to 15 min after they appear, following `HOLD_POLICY` (workspace, popup, workspace).

## Checks

- Every list ends `done` or `private`, a capped list ends `partial` with its 49 returned users, or it waits in a running cooldown or hold. A `done` list has exactly its size.
- Edges per list match exactly the set of users the fake served in usable answers. There are no duplicate edge rows.
- Pages resent after the cursor advances get `stale` back (older servers may say `duplicate`); neither response changes the count.
- After each restart, the next request for that list uses the right cursor, and no list starts over from page 1.
- Each observed hit matches an independent model of the policy (10 min doubling per hit, cap 6 h, a 2 h minimum rest after 3 hits of one request kind, and longer Retry-After deadlines). It also matches `core.js applyHit` replayed on the exact state from before the hit.
- No Instagram request happens during its request kind's cooldown window, a hold or a pause, before `nextAt`, or while another request holds the lane.
- Gaps between requests are at least `PACE.listGap[0]`, and gaps between bio reads are at least `PACE.profileGap[0]`.
- Every item that entered the outbox was acknowledged by the server. The outbox ends empty, and the server returns no 4xx to the extension.
- `/api/ext/*` refuses a foreign Origin. `web_profile_info` is never called. The extension throws no uncaught errors. Resume works from the popup. Local list progress matches the server's count.
