# Second Chrome profile on one Instagram account: the test

Written 4 October 2026. The question: would a second Chrome profile signed in to the same Instagram account double collection speed? This page is the evidence, the guarded way to find out, and an honest prior.

Nothing here runs by itself. The mode is off, it can only ever be switched on for @trashtheturtles, and nothing in this change sends an Instagram request.

## Short answer

Almost certainly not, and not worth the risk. Three reasons, each from our own data:

1. **The daily budget belongs to the Instagram account, not to the profile.** The server adds both profiles' requests against one daily limit (tested). Two profiles spend 900 requests in half the time; they do not collect more people per day.
2. **There is no speed to buy inside the safe range.** One profile with the idle wait removed does about 231 list pages an hour ([PACING_EVIDENCE](PACING_EVIDENCE.md), modeled). The highest hour any account ran clean is 271 (284 over a rolling hour). Two profiles would have to reach about 460 an hour, 1.6 times the highest hour ever seen.
3. **Most of the missing speed is self-inflicted.** On 3 to 4 October the account sent requests for 6.1 of 24 hours; 17.6 hours were stops the app imposed on itself, and 2.2 hours were an idle wait with no safety purpose. A second profile cannot recover that, and a single profile can.

The test exists so the question can be answered with data instead of argument, if Michael still wants it. The cap makes it safe to ask; it cannot make a yes likely.

## What our data says about how Instagram limits us

All numbers come from the snapshot of 4 October (`leads.sqlite`, 6,499 saved list pages, 2,690 collector events) and `docs/PACING_EVIDENCE.md`. Reproduce the tables below with `python3 ops/twin_evidence.py PATH/TO/leads.sqlite` (read-only).

| Question | What the data shows | Strength |
|---|---|---|
| Did Instagram ever limit list pages? | No. Zero HTTP 429 on any list endpoint in 6,195 dated pages across three accounts. | Strong: it is a count |
| What did the 429s hit? | The bio route only, and not because of volume. Of the 5 recorded in the event table, 3 came with zero list pages from that account in the previous hour (the pacing report infers 3 more; 4 of its 8 had at most 2 pages before them). One target (@markbuildsbrands) took 4 hits across two accounts. | Strong |
| Are limits shared between accounts on one IP and Mac? | Not for list pages. Two accounts together saved 512 pages in the busiest rolling hour (ending 27 Sep 11:26 UTC; 251 and 265 each) from one IP and one Mac with no list event. | Medium: one hour |
| Did a bio 429 on one account coincide with the other? | Once: @dihfluencer at 11:45:24 and @trashtheturtles at 11:45:32 UTC, eight seconds apart, then @fortun8te 17 minutes later. If the five 429s fell at random over the run, a pair on different accounts this close has a 0.4% chance, and all three accounts inside 17 minutes about 1%. So something shared (IP, device or the route itself) acted on the bio route that morning. | Weak: five events, chosen after looking |
| Do flags spread to other accounts on the same IP and Mac? | No. @dihfluencer got a scraping warning on 30 Sep 22:37 UTC while @fortun8te and @trashtheturtles ran beside it with no event, then and since. | Medium: one case |
| What caused that warning? | Not volume: it had made four requests in 45 hours. The one structured difference from @trashtheturtles was 54 requests from a non-browser client on its session (the 28 Sep benchmark), followed by a soft block. Treat that as a suspicion, not a finding. | Weak |
| Is the limit per account, per session or per IP? | **Our data cannot say.** We never reached a volume limit, so nothing separates the three. | n/a |

What the public record adds, and why it is weak evidence: the instagrapi maintainers' guide says the "please wait" limit is account-bound, not just IP-bound; Instaloader's documentation says its rate controller "makes no distinction between multiple accounts on the same IP" and a user there reports 429s on a logged-in account after only 20 requests; vendor posts about IP-based limits (about 200 requests an hour per IP, logged out) are marketing for proxy services and cover logged-out scraping. Anti-detect browser vendors claim Instagram links accounts by device fingerprint and IP. None of these measured two concurrent sessions of one account. Sources: [instagrapi](https://instagrapi.com/guides/errors/please-wait-a-few-minutes/), [Instaloader troubleshooting](https://instaloader.github.io/troubleshooting.html), [Instaloader issue 864](https://github.com/instaloader/instaloader/issues/864).

**Reading:** the only enforcement we have seen is per account (the warning), per route (the bio 429s) or per target (home-page redirects). The honest model is that the account carries the risk. A second profile adds a second session, not a second account.

## What breaks if two profiles share one Instagram ID

This is how the code treats it today, and what the opt-in mode changes. Reference lines are in `server/accounts.py` unless noted.

| Piece | Today (default) | In twin mode |
|---|---|---|
| Who may work | One profile per Instagram ID: `identity_owner` keeps the oldest available `both` profile; the duplicate gets no jobs and no request permits. The Accounts page says "logged in on 2 Chrome profiles". | Both work, only while the test is `twin` and only for the two named profiles. |
| Waiting for the other profile | `identity_handoff_pending`: a new owner waits for the old one's live lease. | Not applied between the twins. |
| Cooldowns, list and bio | Already keyed by Instagram ID (`identity_cooling` reads every profile of the ID plus the saved identity ledger). One profile's wait stops both. | Same. The reporting profile's wait is also copied to its twin at once. |
| Daily budgets | Already shared: the strictest configured limit applies, usage is summed across profiles (`request_budget_left`). Tested: 6 + 3 + 1 against a limit of 10 stops both. | Same. Both profiles must have identical limits or the test will not start. |
| Warning hold | Keyed by lane or Instagram ID (`warning_for`), so both profiles are held. | Same. A warning page on either profile ends the test. |
| Login or security hold | Per profile. | Copied to the twin; its leases are released. |
| The 429 stop | Any rate limit, soft block, login or security response sets the workspace-wide pause (`server.py ext_error`). | Same, plus the test ends and the second profile is paused. |
| Account selection (`instagram_collection_isolation`) | Lists lane and ID pairs; duplicate IDs are refused at Start. | Unchanged. The twin passes the check through its own state, so Start/Resume on the original profile keeps working. |
| Duplicate work | Not possible: a job leases to one profile; a list sticks to the profile that started it; a bio target is never leased twice. Tested. | Same. A single list cannot be shared, so two profiles only help while 2 or more lists are queued. |
| Request permits | One Instagram request in flight across the whole workspace, 2 s apart. | Same, plus a combined cap on the account (below). |
| Pacing inside the extension | Per profile (its own gaps, breaks and 72-in-11-minutes window). Two profiles would double the account's rate. | Capped on the server at the combined limit. The extension is untouched. |
| Extension identity | Each profile binds to the ID it sees (`ds_user_id`) and sends it with every call. Two profiles bind to the same ID without conflict. | Unchanged. |

The 429 circuit as documented in `PRODUCT.md` ("three 429s in an hour stop everything until midnight") is not what the code does. The code (core.js, since 27 Sep) rests a request type 10 minutes after the first hit, doubling per hit within 24 hours, at least 2 hours after three. The twin makes that moot: the first adverse event ends the test.

## What the opt-in mode is

`server/account_twin.py`, `POST/GET /api/account-twin`, `ops/account_twin.py`. Default off. State lives in two settings rows (`account_twin`, `account_twin_log`) that do not exist until Michael starts a baseline.

**Who:** only the Instagram ID 64189916997 with handle `trashtheturtles`. The main account, the warned account, any other account and any profile that claims the handle with a different ID are refused in code, not by configuration. Both profiles must be on extension 3.9.30 or newer, online, free of holds and cooldowns, with the same finite daily limits, and no third profile may be signed in to the account.

**Speed:** the mode has no speed setting. Each profile runs at the pace it always has (the popup speed, the speed control). The mode only caps the two profiles' combined requests, in the same permit every Instagram request already needs:

- The cap starts at the **measured baseline** (what one profile actually did in an hour, with bios off), so switching the twin on cannot raise requests per hour.
- It can only be raised in 25% steps, one clean full hour at a time, and never above **what one profile may do under the extension's own pacing** (`PACE` in `extension/lib/core.js`, read from that file, about 295 an hour for lists only). There is no way to raise it from this mode: to allow more, change the pace with the speed control and the ceiling follows.
- Requests are spaced evenly (not bursts), never more than 72 in any 11 minutes, and never more than the cap in any rolling hour.
- The popup speed setting (`collectionSpeed`, 8 to 30 s) is the idle recheck only; it never changes request gaps, so it cannot carry a cap. Because it is tied to the original profile's lane, the second profile rechecks at the standard 15 s. If the speed control later keys on the Instagram ID instead of the lane, both profiles will follow it.

**The test ends on the first adverse event:** any rate limit, soft block, login or security check, scraping warning, home-page redirect on a list, new cooldown or hold on either profile, an unconfirmed request, 3 unexplained errors in one step, or more requests in an hour than the cap. Then both profiles stop, the second profile is paused, collection is paused (stage pauses on), the wait is copied to both, and nothing resumes by itself. Private profiles and deleted profiles do not count.

## Before you start

Do none of this unless you decided to run it. It needs about 7 hours of attention.

1. Read the recommendation at the end. If you still want the answer, continue.
2. Run it with bios off. A first-request bio 429 is route behaviour, not volume, and would stop the test for no reason. Use the same Start you use now for lists only.
3. Queue at least 4 following lists of 1,000 or more people. A list can only be worked by one profile.
4. Give both profiles the same daily limits, finite, high enough for the test day (about 1,000 list requests is enough for the plan below; the pacing report proposes 900 as the daily ceiling, so do not raise it for this). In Accounts, or:
   `curl -s -X POST -H 'Origin: http://127.0.0.1:8777' -H 'Content-Type: application/json' -d '{"budget":{"list":1000,"profile":100}}' http://127.0.0.1:8777/api/accounts/LANE_ID`
5. Create the second profile yourself:
   - Chrome, profile menu, Add, new profile named "turtles twin". Do not copy cookies or an existing profile folder.
   - In that profile open `chrome://extensions`, Developer mode, Load unpacked, pick the `extension` folder of this repo.
   - Open instagram.com in that profile and sign in as @trashtheturtles yourself. Complete any Instagram email or phone check yourself. **If Instagram shows any security check here, stop and do not continue: that is the answer to "does a second session trigger a flag" and it costs nothing else.**
   - Do not add this profile to `data/browser-startup.json`. Do not let Chrome reopen it in the background.
6. Wait at least 24 hours with the second profile signed in and idle. Accounts will show a note that @trashtheturtles is logged in on 2 Chrome profiles; that is expected. If nothing happens in 24 hours, continue.
7. Check the readiness: `python3 ops/account_twin.py status`. It must say `ready yes`. It lists what is missing in plain words.

## The run

All commands are `python3 ops/account_twin.py ...` (add `--base-url http://127.0.0.1:PORT` for another server). Each one that changes anything prints what it will do and needs `--yes`. LANE_A is the original profile's lane id, LANE_B the new one's (Accounts, or `GET /api/accounts`).

| # | When | You do | Expect |
|---|---|---|---|
| 1 | any time | `baseline-start --first LANE_A --yes`, then press Start collecting as usual (lists only) | The second profile stays idle. One profile collects. |
| 2 | 60 minutes later | `baseline-finish --yes` | Refused if shorter than 60 minutes, fewer than 60 requests, or the profile was idle too often. Run it again later. It prints requests and list pages per hour. This is the baseline. |
| 3 | right after | `enable --first LANE_A --second LANE_B --yes` | Both profiles work. The cap equals the baseline. |
| 4 | keep it running | `watch` in a terminal | One line a minute. Exits with code 2 the moment the test stops itself. |
| 5 | after 60 minutes of step 1 | `advance --yes` | Only if the step ran its full hour, used at least 80% of its cap and had no adverse event. Cap grows 25%. |
| 6 | repeat 5 | `advance --yes` | Stops with a plain message at the ceiling. Do not go further. |
| 7 | at the end | `report --out ~/Desktop/twin-report.json`, then `disable --yes --reason finished` | Second profile paused. |

Plan: baseline 1 hour, same total 1 hour, then at most 3 more steps. About 5 hours. Stay near the Mac for the first steps; the test is automatic, but a stop needs you.

## What counts as an answer

The metric is **successful list pages per hour for the Instagram account**, both profiles added, as the status shows under `list_pages_per_hour`, and the ratio to the baseline (`vs_baseline`). The pages come from saved pages (one per page, never counted twice), not from request counts.

| Read | Meaning |
|---|---|
| A step shows list pages per hour near 2 times the baseline, ran its full hour, used at least 80% of its cap, and had zero adverse events | The twin did double that step. It says nothing about day two or a week later. |
| The ratio stays near 1 although the cap rose | Something other than the cap is limiting (job supply, the extension's own gaps). Doubling is not available. |
| A step stops itself | The account objected, or a shared limit was hit. Read `trip.after_minutes` (time to first warning) and `trip.code`. One event is the answer. |
| Zero events in 5 hours | "No event seen up to X an hour", never "X is safe". After 10 clean days a 30% daily chance is still allowed (pacing report, rule of three). |

Also record: whether signing in the second profile triggered any security check; adverse events per step; requests per hour against the cap; split between profiles; people saved per hour. The daily total does not change: both profiles draw on one budget.

## Stop rules

Automatic, no action needed: see "The test ends on the first adverse event" above.

Manual, stop at once with `disable --yes`:

- Instagram emails or notifications about a new login, an unusual login or a security check on @trashtheturtles.
- Any security prompt, phone or email check, or logout in either profile.
- The Accounts page shows an account other than @trashtheturtles in the second profile.
- You are not at the Mac for a step.

## Rollback

Everything the test changes is two settings rows, a pause on the second profile and the combined cap. Default behaviour is unchanged otherwise.

1. `python3 ops/account_twin.py disable --yes --reason done`. The second profile is paused; the first keeps its normal settings. Lists it was working move to the original profile from their saved cursors.
2. `python3 ops/account_twin.py reset --yes` clears the test state (never resumes collection).
3. Sign out of Instagram in the second profile through Instagram itself, and remove the extension there. Optionally revoke the session under Instagram's "Where you're logged in".
4. Check Accounts shows one profile for @trashtheturtles and its daily limits are what you want.
5. If the test stopped itself: do not resume. Open @trashtheturtles in its original Chrome profile and look at the account, as for any warning ([warning recovery](INSTAGRAM_WARNING_RECOVERY.md)). Do not clear cookies, do not sign in with another account, do not rotate identities.

## Honest prior

These are judgments from the data above, not measurements. The sample is three accounts over ten days and one warning.

| Outcome | My estimate | Why |
|---|---|---|
| At least 1.8 times the list pages per hour for a full step with no event, with the ceiling as shipped | Under 10%. After the idle-wait fix it is not reachable: one profile already gets 231 an hour, 295 is the ceiling (1.3 times). Before the fix, one profile does 130 to 160, so the twin would only repeat what removing the idle wait gives for free. | Arithmetic and the pacing report |
| Doubling holds if the ceiling is lifted past the highest clean hour (271) | 10 to 20% for a few hours, below 10% for days | No data above 284 an hour. A volume limit has never been seen, so the real ceiling is unknown. |
| Some adverse event during the 5-hour stepped test (second-profile security check, 429, soft block, redirect, warning) | About 30 to 45% | Volume is probably not the main risk (zero list events in 6,195 pages bounds that at under 0.05% per page at 95%). A new session and a second login on the account are. Redirects are target effects and would stop the test for no account reason. |
| A scraping warning or hold on @trashtheturtles within a week of the test | About 8 to 15%, against a few percent without it | One warning in three accounts in ten days; the cause was probably not volume; no data on two sessions. |
| People per day higher than one profile with the same daily budget | About 0 | One shared budget. |

Where I could be wrong: Instagram may treat a second authenticated session as normal (people use a phone and a laptop), and per-session buckets may exist. That would make the twin work. Nothing we have says so; the only public statement that speaks to scope says the limit is on the account.

## Better routes to speed

In order of size, all from the pacing report and our data.

| Route | Gain | Added Instagram risk |
|---|---|---|
| Wait for the list clock instead of a flat 15 s idle wait | 146 to about 231 pages an hour (modeled, not yet run). Also closes the gap from 21.9 s to about 13 s per page. | None added to the configured pattern: it restores the 11 s cadence of 24 Sep to 1 Oct. |
| Remove the self-imposed stops | 17.6 of 24 hours on 3 to 4 Oct. Budget stops 2.4 h, a followers redirect revoking the selection 4.8 h, an unconfirmed request 10.5 h. Recovering half is about 8 more collecting hours a day. | None: safe stops stay, only the avoidable ones go. |
| Following lists before followers | 4.8 times the new people per request (176 against 23.5 returned per page) | None |
| Daily budget ramp: 900, then +10% per clean day to 1,200 | 1.33 times the people per day | The real lever, and the one that needs the ramp's stop rules |
| More real accounts, each with its own budget and its own Chrome profile | Roughly 900 more requests a day per healthy account | Each account carries its own risk; `MORE_ACCOUNTS.md` says there is no verified warm-up rule. @dihfluencer stays held until Michael reviews it. |

## Recommendation

Do not run the second-profile test now. Do the first three rows above, run the pacing report's one-account ramp, and add a second real account if you need more than about 1,200 requests a day. Come back to the twin only if all of these hold: a full week of clean days at the new speed, the daily ceiling reached early every day, and a reason to believe the account can take more than the 271 an hour ever seen. Then the cap still needs the speed control, not this mode, to move.

## Limits of this work

- Not run live. No Instagram traffic, no real profile, no real database was touched. Tests use a temp database and a simulated clock.
- The twin can only be checked against simulated Instagram. The simulator proves the cap, the split and the stop; it says nothing about how Instagram treats two sessions.
- The extension is unmodified. The second profile uses the standard 15 s idle recheck; the original profile uses its popup setting. After the speed control lands, repoint `account_twin.pacing()` at its single source (one function) so the ceiling follows it.
- The pacing report proposes an hourly cap of 250 for this account. That cap is not in code yet. Until it is, the twin ceiling is 295 an hour for lists only (363 with bios).
