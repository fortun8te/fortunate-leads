# More useful connections per request

This change focuses on follower/following collection. It uses the existing local
database, extension, account permissions and request scheduler. It adds no paid
service, account creation, proxy, new collection process or bio-analysis work.

## Collection flow

1. A list job with a known, unambiguous numeric Instagram ID starts directly at
   its saved page. The server supplies identity and count provenance tied to the
   exact run. Unknown or conflicting IDs and pinned page-size experiments retain
   the existing lookup path. Every list request still obtains its normal permit.
2. A normal continuation retains the first page, saved members and proven cursor
   chain. An explicit refresh, cursor cycle or invalid history starts over.
3. After three consecutive nonterminal pages add no distinct members to the
   current run, save the observations, mark the list partial and move on. Finding
   people already elsewhere in the database still counts as useful progress.
4. Explicit caps, repeated-person runs and runs without a current count remain
   saved partials; the repair loop does not replay them daily. They remain
   available for explicit review/refresh. Other recoverable failures retain their
   existing bounded retry policy.
5. At an eligible idle poll, discovery first preserves existing owner/manual/fit
   priorities, then considers public accounts observed in at least two distinct
   collected accounts' current lists. Only their following lists enter this
   fallback. No bio or qualification job is required. Existing auto-discovery
   controls, two-outstanding-job bound, rejection filters and collector account
   exclusions still apply.

The discovery frontier advances in indexed windows and persists across restart.
It can move past a window full of excluded accounts. At the end it waits five
minutes before revisiting. Previews and paused/held collectors do not advance it.

## Coverage and warning handling

Cached counts are display hints. Complete coverage still requires a current-run
count, first-page evidence and a valid page chain. Skipping the initial lookup
can therefore leave an exhausted list marked partial if Instagram's list
response supplies no current count. Its observed connections remain usable;
absence from that partial list does not remove previously observed connections.
A follow observation is not a friendship or business-fit claim.

Pacing, daily allowances, warning holds, account identity checks, protected owner
accounts and durable page delivery are unchanged. This cannot guarantee that
Instagram never warns an account. A warned account stays held; other work can
continue only where the existing account/workspace rules permit it.

## What the measurements mean

The collapsed **List collection results** section in detailed activity reports
successful pages over the last 24 hours, separately for each direction. It shows
distinct saved members per page within each run and newly recorded connections.
People can appear in several lists. Failed requests and lookups also consume
allowance and are excluded from these page-yield figures. Missing old metrics
stay unknown. At more than 10,000 matching events the display labels its sample.

A read-only copy of collection data on 2026-10-03 contained 208 accepted following
pages that day: 37,270 returned entries, 35,448 saved run memberships and 20,624
new connection rows. That is 179.2 returned or 170.4 saved memberships per page.
These are historical observations, not a before/after result for this change.

The existing following request already asks for 200 rows; followers ask for 25
by default. With an assumed 400-request daily allowance, 200 rows on every
request would mean 80,000 returned entries before overhead and duplication.
One million fresh returned entries would need at least 5,000 such pages.
Local processing, inference or larger requested counts do not create those
Instagram responses. No sustained million-per-day collection result is claimed.

## Alternatives checked

[Agent Reach](https://github.com/Panniantong/Agent-Reach) routes Instagram through
logged-in browser tooling; it does not supply an independent bulk graph source.
Its upstream [OpenCLI follower command](https://github.com/jackwener/opencli/blob/main/clis/instagram/followers.js)
reads one page, while its [following command](https://github.com/jackwener/opencli/blob/main/clis/instagram/following.js)
paginates smaller requests without this collector's durable coverage history.
Replacing the existing collector with it is not an evidenced throughput gain.
It was inspected, not installed or run against an Instagram account.

## Verification

Tests cover known-ID lookup avoidance, permit and warning enforcement, restored
counts, ambiguous identity fallback, duplicate-page stopping, positive progress
reset, explicit continuation versus refresh, incomplete coverage preservation,
public graph candidates, protected accounts and persisted frontier traversal.

On an isolated copy of the saved database, the six-account shortlist took 806 ms
cold and 75–80 ms warm; the results summary took 0.24–0.50 ms. These are local
read timings, not Instagram throughput. A 100,000-account equal-degree fixture
needed about 48,000 SQLite operations for both the first and last 2,000-account
windows. A separate 100,000-irrelevant-event fixture verifies that results
reporting does not scan profile-event history. Partial covering indexes keep
those reads bounded.

Validation uses temporary databases and a separate browser. No production
server, live account session or collection setting is changed by this work.

Completed checks on 2026-10-03:

- Server regression suite: 1,290 tests on Python 3.14, three optional skips.
  The optional transport suite also passed all 13 tests separately on Python
  3.12 with the project's pinned `instagrapi==3.0.14`. The first system-Python
  3.9 run exposed an incompatible preinstalled library; that is not the runtime
  used for the passing transport check.
- Root collection/provenance tests: 21 passed; sidecar tests: 27 passed.
- Extension: 201 passed in each of UTC, Amsterdam, New York and Auckland.
- Web: 413 passed. The eight-hour interruption/recovery simulation passed all
  82 checks; it is simulated time, not eight hours of live Instagram collection.
- Browser inspection at 1,440 px and 390 px: results closed by default,
  disclosure opens correctly, saved-data values agree with the source and
  text fits without horizontal overflow.
- Independent review: both identified database performance issues were fixed
  and rechecked with 100,000-row bounded-work tests; no remaining findings.
