# Connection collection, map and saved-profile processing

Built in an isolated checkout on 3 October 2026. This change does not deploy itself,
start collectors, enable AI, alter collection allowances, or clean the live database.

## What changes

- The existing portrait map gains 5,000, 10,000 and 20,000-person pages, gradual
  portrait appearance, viewport culling, bounded image loading and clearer independent
  distance/size controls. Connection evidence controls the default distance; business
  fit and follower size cannot stand in for closeness. Shared sources remain observed
  overlap, not friendship. The layout schema changes, requiring a local layout rebuild.
  Continuous organic packing spreads tied scores through the disk without empty
  rings or a spiral grid. Large overviews progressively retain saved portrait pixels
  in a bounded raster, so the decoded-image budget does not permanently hide people.
  The explicit fit button measures the loaded bounds instead of forcing a fixed zoom.
- Local photo serving reuses a protected read-only database connection while checking
  current ownership on every request. File validation and photo freshness remain.
  A 120-photo local HTTP trial at 12 concurrent requests on Python 3.14 improved
  from 3.22–3.51 seconds to 0.076–0.123 seconds across two runs of each variant.
  This measures cached local photos only, not Instagram collection.
- Each accepted list page adds a stable-ID directed connection to a local evidence
  index. The same A-follows-B fact can be read from either end without another fetch.
  This does not establish either account's complete list. Existing list completion,
  cursor and warning rules remain authoritative.
- Saved JSONL list captures can be previewed, imported in bounded transactions,
  resumed after interruption and replayed without duplicate writes. Original dates
  and directions survive. Unknown handles remain staged by ID; no names are invented.
- Connection counts now update once when a distinct source membership changes.
  The old implementation recounted a person's entire source network on every edge
  write. Counts remain exact through duplicate directions, retirements, merges,
  rollback and interrupted migration; ordinary collection uses the improvement too.
- The existing collector keeps its default scheduler. An explicit experimental
  `observed_yield` policy may interleave ordinary equal-priority lists based on saved
  page yield, with exploration and existing four-page fairness. Four saved ordinary
  backlog pages can also earn one automatic fresh-network lease opportunity, so a
  large old backlog cannot indefinitely prevent new discovery. Actual leasing spends
  that opportunity, including failed attempts; read-only queue checks do not.
  At most two automatic targets remain pending. Positive-priority work, trials,
  direction fallbacks, eligibility, account ownership, holds and pacing retain
  precedence. A lease can include the existing lookup or confirmation behavior;
  it is not a guaranteed single underlying HTTP request or a fixed request share.
- A metadata-only permit audit distinguishes list pages, seed lookups and profile
  views. It records known HTTP outcomes separately from operations with unknown HTTP
  counts. Native profile navigation can generate several underlying requests.
- Saved-profile processing can clear validated cached results while another item
  needs an unavailable model. Every reused result must still match its saved content
  and current evidence rules. Empty profiles do not become invented qualifications.
- Unverified, stale or quarantined deep-research claims are withheld from the lead
  view. Verified evidence is displayed as attributed quotes. A separate offline
  tool previews, quarantines and restores suspect records without deleting notes,
  manual tags or relationship evidence.

## Measurements and their limits

### Captured page allocation

The October 3 snapshot contains 14 jobs that existed before the replay cutoff and
92 recorded list-page responses for those jobs. Jobs created later were excluded.
Each policy sees only the pages it has already selected, not future outcomes.

| Page budget | Existing chronological order: new edges / people | Round-robin | Experimental adaptive |
| --- | --- | --- | --- |
| 20 | 38 / 31 | 487 / 386 | 366 / 355 |
| 50 | 582 / 166 | 849 / 509 | 897 / 503 |
| All 92 | 1,014 / 527 | 1,014 / 527 | 1,014 / 527 |

At 50 recorded pages, the adaptive replay found 54% more canonical edges and about
three times the new people than chronological order. Round-robin found slightly
more people than adaptive. This is one historical cohort, not a prospective daily
rate or proof that the adaptive policy should be the default. It excludes unknown
transport attempts, profile reads and seed lookups. Missing historical identities
and legacy timestamps are disclosed in the replay report.

### Fresh-target admission audit

A later read-only audit used accepted page events from 12:20:16 to 16:27 UTC on
October 3. Jobs already present at the cutoff produced 651 newly stored historical
links from 84 pages; 50 jobs admitted afterward produced 39,540 from 243 pages.
Those later jobs match discovery history. Their first admission was at 13:02:18,
just after an older crawl finished at 13:01:59. No observed page preceded its job's
creation time. This identifies an initial discovery delay of roughly 42 minutes;
it does not establish a sustained speed improvement or a controlled comparison.
Historical link additions are a different metric from canonical replay novelty.

The fixed-cohort replay correctly excludes those 243 later pages. Within its
remaining 84-page cohort, the first 50 chronological pages yielded 628 canonical
new connections; round-robin yielded 951 and adaptive yielded 936. All policies
converged to 1,009 connections after all 84 pages. This supports bounded exploration,
but does not demonstrate that adaptive scheduling beats simple round-robin.

The audit also reproduced a missing admission path on temporary data: an ordinary
priority-zero backlog prevented a fresh automatic candidate from being admitted,
even when the experimental scheduler was enabled and no automatic target was
pending. Admission and dispatch must both be bounded; merely admitting a lower
priority discovery job would still leave it behind the same ordinary backlog.
The experimental policy now implements that bounded opportunity, including when
the fresh target lies outside the first 64 ordinary queued jobs. The allowance is
stored inside the actual lease transaction and survives restart; rollback restores
it. Ordinary saved-cursor work resumes after the opportunity. This remains an
offline-tested scheduling change, with no live throughput claim.

### Saved-list intake

The 100,000-connection local benchmark improved from 45.161 to 28.031 seconds after
the incremental count change: 3,567 connections/second in the latter run. These are
individual runs under concurrent development load, not an isolated repeated trial.
The fixture has 1,100 profile identities
and shared members across sources; this is not 100,000 freshly fetched people.
Preview took 1.124 seconds. Replaying made zero writes. Database size was 118 MB.
The full-size run imported **1,000,000 synthetic directed connections in 385.068
seconds (6 minutes 25 seconds)**, with exact canonical and visible graph counts.
This fixture has 2,000 profile identities and exercises shared members across
1,000 source lists. Preview took 11.569 seconds; replay took 19.498 seconds and
made zero writes. The resulting database occupied 1.19 GB. No Instagram requests
or model calls occurred, and the temporary fixture was removed afterward.

Recovery of the existing benchmark captures on a temporary production copy read
57 saved pages containing 3,506 observations and 1,231 distinct directed ID pairs.
The index recognized 186 previously known pairs and added 1,045 indexed pairs;
282 connections became visible in the existing graph. Another 764 member identities
remain staged without invented usernames. Import took 0.706 seconds and replay
made zero writes. These are historical captures, not fresh Instagram observations.
Collection jobs, cursors, current-run membership, accounts, notes and owner context
were identical afterward. Only the expected graph revision settings changed.

### Qualification and cleanup

An actual 100,000-profile synthetic database selected and inspected a 64-item
saved-content batch in 0.0123 seconds using the queue index and no model calls.
The real snapshot's next 64 items all require fresh review, so there is no measured
token saving for that batch. A worker integration test proves a later valid cached
result clears without calling the model while an earlier uncached item remains.

The offline cleanup rehearsal quarantined and restored all 176 unverified deep
research records. Notes, manual tags, relationships, settings and Rules mode were
preserved. The preview flagged authority wording in 125 records and budget wording
in 27; these indicate missing evidence, not proof the claims are false. Live data
has not been quarantined or deleted.

## Using the tools

Use an initialized offline database copy. These commands do not make Instagram
requests. Import and cleanup commands refuse the canonical live database.

```sh
python3 ops/import_lists.py --db /path/offline.sqlite --file /path/lists.jsonl
python3 ops/import_lists.py --db /path/offline.sqlite --file /path/lists.jsonl \
  --apply --sha256 EXACT_PREVIEW_SHA256
```

One JSON object per line:

```json
{"source_owner_id":"100","source_owner_handle":"example_owner","direction":"following","observed_at":"2026-01-01T12:00:00+00:00","members":[{"ig_id":"200","handle":"example_member"}]}
```

Numeric stable IDs and a timezone-aware observation date are required. Handles
are optional when identity cannot be established; unknown IDs are staged. A page
has at most 2,000 members. Historical aliases never rename current profiles or
collection jobs. The full immutable source is validated before any chunk writes;
the exact digest binds preview and import. Accepted chunks survive interruption.
Imports never mark a follower/following run complete. Native account-export files
need a provenance-aware adapter before this format; handle-only archives are not
silently promoted into stable-ID facts.

```sh
python3 ops/recover_benchmark_lists.py --source /path/benchmark.sqlite \
  --identity-db /path/offline.sqlite --output /path/new-capture.jsonl
python3 ops/acquisition_replay.py --db /path/offline.sqlite \
  --since 2026-10-03T00:00:00+00:00 --until 2026-10-04T00:00:00+00:00 --budget 50
python3 ops/benchmark_acquisition.py --edges 1000000
```

Recovery never imports the benchmark's baseline as a fresh observation. It copies
only confirmed saved observations, retaining source request ID, route, warning,
warmup flag and capture date. It never reads stored transport fingerprints.
The synthetic benchmark creates and removes its own temporary files by default.

```sh
python3 ops/enrichment_review.py plan --db /path/offline.sqlite --limit 64
python3 ops/enrichment_review.py preview --db /path/offline.sqlite --out /path/review.json
python3 ops/enrichment_review.py quarantine --db /path/offline.sqlite \
  --manifest /path/review.json --review-hash EXACT_REVIEW_HASH
python3 ops/enrichment_review.py restore-preview --db /path/offline.sqlite --out /path/restore.json
python3 ops/enrichment_review.py restore --db /path/offline.sqlite \
  --manifest /path/restore.json --review-hash EXACT_RESTORE_HASH
```

Cleanup fingerprints bind the reviewed rows, generated tags and identity to the
apply operation. Stale manifests fail instead of acting on changed evidence.
Reversible quarantine affects active display, not the original source record.

`GET /api/scraper/acquisition` returns indexed graph counts and today's UTC permit
audit. Optional `since`, `until` and `viewer` filter the audit. `ig_id`, `direction`,
`after` and `limit` read an indexed neighbor page. Coverage is explicitly partial.
`POST` with `{"policy":"observed_yield"}` enables the experimental dispatcher;
`{"policy":"current"}` restores the default. This setting does not activate any
collection stage or bypass a pause. It remains off in the shipped default.

## The remaining Instagram bottleneck

The selected account's 400-list-request allowance is a local setting, not a known
Instagram daily quota. At a full 200 results on every successful request, that
setting permits at most 80,000 returned following rows before lookups, repeats,
short pages and failures. Follower pages have shown a much lower yield and upstream
caps. The warning's triggering cause is not established by the saved logs.

Agent Reach and its OpenCLI Instagram commands were also inspected. They use an
authenticated browser, with no independent graph source or demonstrated gain over
the existing Chrome collector. See [the earlier route comparison](COLLECTION_EFFICIENCY.md#alternatives-checked).

The free routes researched do not establish one million fresh daily follower and
following entries from a few accounts. Local processing throughput cannot create
upstream access. Existing authenticated accounts remain behind the same protections;
creating or buying accounts is not part of this change and is not a ban-free plan.

The useful next measurement is new unique people and connections per total permitted
operation during an explicitly authorized observation window, compared with the
existing Chrome route at unchanged settings. Browser navigation's underlying HTTP
count is still unknown. No paid service, proxy, Tor, restriction workaround or Meta
API is introduced. See [the source audit](SOURCE_CAPACITY_RESEARCH.md).

## Validation

After the fresh-network admission change, the complete server suite ran 1,420
tests on Python 3.14 in 252.728 seconds: successful, with three optional dependency
skips. Its independent review passed 135 focused scheduling and safety tests. The optional transport suite passed all 13 tests separately on
Python 3.12 with Instagrapi 3.0.14. Root and sidecar Python suites passed 21 and 27
tests. The extension passed 208 tests in each of UTC, Europe/Amsterdam,
America/Los_Angeles and Asia/Tokyo. The original complete web suite passed 424 tests. The map correction passed all
427 web tests and all 14 picture-serving tests on Python 3.14. After the final
bitmap-lifetime and page-transition guards, all 23 focused map regressions passed.
After the final malformed-research display guard, all 33 affected evidence,
research-pipeline and owner-authority tests passed again. The final collection
simulator passed all 82 checks across eight simulated hours, including warning
holds, lost responses, restarts and saved-cursor recovery. This is a simulator,
not a live Instagram soak.

Independent review checked scheduler fairness, preserved account guards, import
transactions and identity handling, cache evidence, quarantine batching and the
incremental graph counts. Reproduced defects were fixed and covered by regressions.
All database experiments used temporary copies or synthetic data. The canonical
checkout stayed clean; no live account activity, cleanup or deployment was performed.

The first rendered map passed functional checks, but Michael rejected its rigid
rings, large empty gaps and grey placeholder wall. Those checks were not visual
acceptance. The corrected preview was then independently inspected with the actual saved
profiles at 3,000 and 20,000 people: continuous organic spacing, no empty score
bands, no spiral/grid pattern and no bright placeholder wall. The 20,000-person
overview reached all 20,000 rendered entries and returned to an idle frame loop;
page-size switching and zoom were checked without browser errors. A completed
20,000-person raster survived a visit to 3,000 and returned fully rendered without
queued photo requests. A bitmap
lifetime defect found during this review was fixed before sign-off. These are
implementation checks, not Michael's appearance approval. The separate preview
remains paused with no collection or research workers running.
