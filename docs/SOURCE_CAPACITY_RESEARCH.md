# Collection capacity research

Read-only audit, 3 October 2026, using the canonical code and a local database snapshot. No live collection or account changes were made.

The present free native collection routes do not establish one million fresh follower/following entries a day from a few accounts, or a bulk bio route for arbitrary personal accounts. The promising improvement is better accounting of useful output and workload, followed by measured runs under the existing protections.

## Which limits belong to the app

| Limit | Verified source | Meaning |
| --- | --- | --- |
| Global 200 list requests and 100 profile reads per day | Read-only `settings.budget` in canonical `data/leads.sqlite` | Locally configured allowance. |
| Selected `trashtheturtles` account: 400 list requests and 100 profile reads | Read-only `accounts.budget` | Local per-account override. Current collection is following-only; the profile allowance does not mean bios are running. |
| Default 3,000 list requests and 300 profiles; configurable maximum 3,000/5,000 | `server/db.py`, `server/accounts.py`, `extension/lib/core.js` | App defaults and validation ceilings, not published Instagram quotas. |
| Separate list/profile clocks and counters; combined 72 requests per 11 minutes | `extension/lib/core.js` | Local pacing protection shared by both workloads for one identity. |
| A single active request across the workspace, with a two-second floor after release | `server/accounts.py:request_permit` | Local shared request protection. Browser work and response time also consume time. |
| Following request size 200; follower size 25 | Current production parser/page-size policy and saved September 28 route evidence | Requested size is not achieved yield. Followers also have upstream target caps. |
| Scraping-warning holds, HTTP 429, challenges, explicit retry deadlines and `should_limit_list_of_followers` | Response classification and saved local observations | Provider outcomes, independent of the local daily allowance. |

A list run refreshes its seed profile/count by opening the native profile page and charges that action to the **list** counter. A normal bio job opens a native profile page and charges the **profile** counter. Both require the same protected request permit. Both share account identity ownership and the combined request window. A second browser profile signed into the same Instagram identity cannot create another budget or escape its existing cooldown.

These local counters measure controlled collector actions. A profile-page navigation can generate additional page, asset and data requests inside Instagram; one local profile debit does not prove one underlying HTTP request.

Code references: `extension/background.js:runList`, `runProfile`, `lookupViaPage`; `extension/lib/core.js:afterRequest`, `budgetLeft`; `server/accounts.py:request_budget_left`, `identity_owner`, `identity_cooling`, `request_permit`.

## What the numbers support

At 400 list requests and a full 200 entries on every following page, the arithmetic ceiling is 80,000 returned entries per account per day. Seed lookups and other list work reduce the number of actual pages. Short pages reduce yield. Repeated people across seeds reduce distinct people. Existing directed links reduce new edges.

| Healthy accounts at the unchanged 400-request allowance | Upper bound for returned following entries |
| --- | --- |
| 1 | 80,000/day |
| 2 | 160,000/day |
| 3 | 240,000/day |
| 5 | 400,000/day |
| 13 | 1,040,000/day |

These are arithmetic upper bounds, not sustainable platform capacity or a recommendation to add 13 accounts. The present warning-recovery selection only authorizes the existing selected identity. Changing its selection or registering more accounts is separate from this research.

At 100 ordinary profile reads per account, two accounts have a local ceiling of 200 active profile reads a day. Passive capture can save bios already loaded during ordinary browsing without another request; it does not turn an arbitrary handle list into a bulk fetch.

For future measurements, record separate totals for HTTP dispatches, successful list pages, returned rows, fresh current-run memberships, new directed edges, distinct new people, profile reads, saved fresh bios, and coverage completion. A million imported historical edges, a million returned rows and a million unique people are different results.

## Can bios explain the warning?

Not from the current evidence. The persistent warning marker records `2026-10-01T13:07:19.193498+00:00` on the lane registered to `dihfluencer`. The current `collector_events` table has zero rows for that lane, or any lane, in the preceding hour. The warning could have been observed after earlier work; its timestamp is not proof of the triggering request.

Five profile HTTP 429 outcomes are recorded on September 27. That establishes those five responses, not the cause of the October 1 warning. Successful collection events use server receipt time, and queued outbox delivery can separate receipt from dispatch. Seed profile/count lookups have no standalone collector event. Native response handling reads `Retry-After` and the Instagram claim header, but the event table retains neither a complete request history nor quota headers. No numerical upstream native-browser daily quota can be reconstructed from those records.

Workload separation can make per-identity measurements clearer and keep one queue from consuming another queue's local allowance. It does not establish independent provider quotas for lists and profiles on the same identity, and it does not prevent shared workspace protection from stopping both.

## Other sources checked

**Native profile libraries.** Current upstream Instagrapi source has individual `users/{user_id}/info/`, `users/{username}/usernameinfo_stream/` and `users/{user_id}/info_stream/` paths. The streamed response merges partial rows into one user. “Streamed” is not evidence of multiple independent user bios per request. No bulk arbitrary-personal-account bio endpoint was established. These methods were source-inspected, not live-tested, and should not be sold as new sustainable capacity. [Upstream user source](https://github.com/subzeroid/instagrapi/blob/master/instagrapi/mixins/user.py), [user documentation](https://github.com/subzeroid/instagrapi/blob/master/docs/usage-guide/user.md).

**Owner-provided exports.** Instagram officially supports exporting selected information from an account to a device. A consenting seed owner could provide their own network snapshot for an offline import. It does not export an arbitrary third party's network and does not create continuing daily collection capacity. The official help page confirms export support and settings; an actual current archive still needs validation for directions, file format, timestamps, counts and completeness before import. No contributors were contacted. [Instagram export help](https://help.instagram.com/181231772500920/).

**Free research datasets.** The dataset owner's `im_instagram_70k` page describes 70,409 users and 1,007,107 connections, collected in April–May 2020 around 24 private universities in Malaysia, under CC BY 4.0. It supplies an immediate historical network for an offline fixture, not Michael's current seed network or current commercial leads. Importing it must retain its original date/source and must not update current collection coverage. It was inspected, not downloaded or imported. [Dataset owner's description and licence](https://www.kaggle.com/datasets/krpurba/im-instagram-70k).

**Existing multiple accounts.** Instagram's current help page supports adding and switching legitimate existing profiles and presently says up to ten logged-in accounts at a time. This is a session-management feature, not a collection quota or an assurance against warnings. Separate identity-bound local accounts are a technical option only after an explicit choice and login by their owner; they are not automatic warning recovery. [Instagram account switching help](https://help.instagram.com/1696686240613595/).

**Official Meta APIs.** Business Discovery and Creator Marketplace were checked as distinct profile sources, but are excluded by Michael's earlier explicit choice against the Meta API. They also do not meet the arbitrary personal-account follower/following requirement. No setup or implementation is proposed. Published Graph API quotas must not be applied to private Instagram browser endpoints. [Meta rate limits](https://developers.facebook.com/docs/graph-api/overview/rate-limiting), [Instagram overview](https://developers.facebook.com/documentation/instagram-platform/overview), [Creator Marketplace](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login/creator-marketplace).

## Useful next experiments within existing protections

1. Record a small, local metadata ledger for already-permitted dispatches. Store identity binding, request kind/route, dispatch and receipt times, requested/returned count, fixed result classification and any explicit retry deadline. Distinguish collector actions from the additional requests generated by a native profile page. Exclude credentials, raw profile bodies, private notes and URLs containing secrets. This adds observability, not Instagram requests.
2. Measure the already-authorized following-only window at unchanged allowance and pacing. Compare returned rows with distinct fresh people and fresh directed edges. Complete the required clean observation period before calling throughput sustained.
3. If a later bio-only window is authorized for an already healthy alternate, measure it separately at its existing profile allowance. Leave warning holds and shared request protections intact. A clean window is evidence for that window, not a guarantee about future warnings or a causal proof against a different workload.
4. Validate one owner-provided follower/following export entirely offline if a file is supplied. Retain source owner, direction, export/capture time, stable-ID uncertainty, and complete/partial status. Do not promote handle-only historic imports to verified present-day relationships.

The practical recommendation is to keep the current collection settings, improve dispatch and novelty accounting, and use provided exports where available. No free source found in this audit delivers one million fresh edges a day for arbitrary targets, from a few native accounts, with demonstrated freedom from warnings.
