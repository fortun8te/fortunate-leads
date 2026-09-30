# Viewport map performance

The viewport API reads precomputed layouts. It sends at most 1,500 people plus aggregated bubbles;
zooming requests a new rectangle. Existing pair comparison remains independent of this display budget.
The measurements distinguish preparation, indexed requests and browser drawing. The current schema-5 photo network has a fresh million-person measurement below. Older measurements are historical; the ten-million run used the earlier schema-4 geometry.

## Current shared circular map, schema 6

All four modes use identical per-person positions and community anchors. Network, Fit, Sources and Status change rank, size and visual emphasis, while retaining the same owner-centred disk. The owner's portrait is 60 px, with its caption below; the legend is a compact key with detailed meanings on hover or keyboard focus. Base UI surfaces use the neutral grey tokens rather than warm overrides.

The fresh **1,000,001-person** fixture built all four layouts in **168.57 seconds**, with **641 MiB peak process RSS**. Prepared source/layout/projection files occupied **1.30 GiB**; the separate request process peaked at **509 MiB RSS**. Across all modes, first whole-world samples took **153–214 ms**, then **7–10 ms**. A direct SQL comparison of every layout row verified **zero position or community mismatches** between Network and each other view across the full million-person fixture. Bounded request counts and aggregate conservation also passed. These local samples do not establish worst-case latency or whole-system memory.

Actual saved-data laptop checks used 111,699 people. All four current layouts rebuilt in 7.48 seconds. Profile images still use the bounded progressive cache; changing views retains the circular arrangement. This exact schema has not had a ten-million run. The historical schema-4 run below remains evidence for the bounded approach, not a performance guarantee for current all-mode rendering.

Evidence: [fixture](benchmarks/map-shared-disk-1m-fixture.json), [build](benchmarks/map-shared-disk-1m-build.json), [requests](benchmarks/map-shared-disk-1m-requests.json), [full-population geometry oracle](benchmarks/map-shared-disk-1m-geometry.json).

## Earlier photo network, schema 5

The owner stays at the centre of one circular network disk. Source audiences receive angular space proportional to their membership. Broad distance bands describe saved connection evidence; spacing within an audience exists for readability. Cached profile photos load progressively; absent photos use initials. Unknown follow direction stays unknown, including when the owner's outgoing list has not been collected completely.

A fresh fixture contains **1,000,001 physical synthetic people**. Generation took 15.90 seconds; all four layouts built in **119.01 seconds**, peaking at **548 MiB process RSS**. Source, four layouts and prepared projections occupied **1.26 GiB**. The separate request process peaked at **514 MiB RSS**. Warm whole-world requests across all modes took **4.9–7.0 ms**; first requests took **115–170 ms**. Warm zoom/pan took **7.9–16.3 ms** and maximum-budget requests **13.3–17.0 ms**. Filtered zoom samples ranged from **0.36–45.18 ms**. These are local samples, not latency guarantees.

Overview requests mix ranked people with spatial coverage within the same bounded budget. Independent SQL checks verified all six follow filters and conserved every matching person through individual nodes and aggregate counts. The fixture's absent outgoing evidence correctly produces no claimed following, mutual or not-following results. Warm overview/direction samples took approximately **1–18 ms**. Prepared Score/Fit lead pages returned exact independently checked first-100 orderings in **28–83 ms**. Prepared default tags took **1.18 ms first** and **0.13–0.19 ms warm**.

On the consistent **111,699-person saved-data copy**, laptop checks covered four viewing modes, follow filters, profile selection and returning to overview. Actual photos and aggregate counts were visually checked at 1440×1000 and 1280×800. Rendered people plus group counts equal the full matching population even when collisions prevent an individual photo from being drawn. The cache retains at most **160 resized images**, loads **four concurrently**, and paints at most **140 photos per frame**. Warm canvas drawing measured **1.4 ms median / 1.8 ms p95** separately from API latency.

**This exact schema-5 geometry has not been benchmarked at ten million.** The older default layout's ten-million results below validate the bounded storage/query approach at that size, but do not establish ten-million performance for every current mode, photo view, dense filter distribution or active collector.

Synthetic evidence: [fixture](benchmarks/map-photo-1m-fixture.json), [four-layout build](benchmarks/map-photo-1m-build.json), [requests](benchmarks/map-photo-1m-requests.json), [overview and direction oracle](benchmarks/map-photo-1m-directions.json). Generated large fixture databases can be removed after retaining these measurements; they are not application data.

## Historical measurements

The following measurements precede the current photo network unless otherwise stated.

## Synthetic measurement, 30 September 2026

On the local Mac with Python 3.11, 100,000 fictional people plus 16 source profiles, 30 source lists.
The fixture took 4.24 seconds to prepare; all four layouts took 7.89 seconds to build sequentially.
Five uncached whole-world requests per mode used the default 600-person budget and scope=all.
Other test processes were running, so these are representative samples rather than isolated capacity limits.

| Mode | Individual people | Matching people | Hidden people | Response bytes | Five samples (ms) |
| --- | ---: | ---: | ---: | ---: | --- |
| Audiences | 600 | 99,279 | 98,679 | 110,242 | 23.73, 13.25, 17.42, 20.64, 17.27 |
| Fit | 600 | 99,279 | 98,679 | 102,618 | 15.36, 11.2, 12.43, 12.11, 13.46 |
| Sources | 600 | 99,279 | 98,679 | 116,982 | 15.8, 13.64, 15.69, 13.9, 14.7 |
| Status | 600 | 100,016 | 99,416 | 106,711 | 12.45, 10.9, 10.69, 10.64, 13.56 |

Every response's summed bubble count exactly equalled hidden people. Other modes omit not-a-fit people
by default; status mode includes them. This explains the differing totals.

Reproduce without live data:

```sh
FL_NO_ORSLOT=1 python3 tests/bench_map_view.py --people 100000 --runs 5
```

The benchmark creates and removes its own temporary database. The default audience layout was measured after the compact-community revision. Tests also compare all displayed
people and totals against exhaustive layout rows across 48 deterministic viewports. They check
incremental status updates, source deletion, HTTP routes, conditional 304 responses, input validation,
and honest missing-layout states.

## Limits still to measure

Other layout modes at ten million, broader dense identical-rank distributions, rare filter classes,
and ongoing collection under maintenance still need separate measurement. Prefix handle search uses an index;
name and substring search deliberately covers a bounded engaged/top-ranked population. Layouts need
an explicit first build. No request starts collection or a full layout rebuild.

## Verified million-person run

The streaming benchmark fixture contained **1,000,001 actual synthetic people**, skewed membership across
30 source accounts, overlaps, observed owner follows, qualification records and workflow statuses.
It uses the real application schema and restores production summary triggers after loading.
Data generation holds only 25,000 ids at once. No live database or collection is used.

On 30 September 2026, sequential build of all four layouts took **117.04 seconds** after
**21.78 seconds** of fixture generation. The database plus layouts occupied **1,267,968,671 bytes**
(1.18 GiB). Peak process RSS for fixture, builds and requests was **495,091,712 bytes** (472 MiB).
A separate request process peaked at **474,611,712 bytes** (453 MiB), including map/search and lead
table requests. This is process resident memory, including mapped SQLite pages; it is not just
Python allocation or a whole-system memory measurement.

Uncached map responses kept the default 600-person or maximum 1,500-person budgets. Whole-world
requests across all modes took **6–169 ms** for the first sample and **6–9 ms** for subsequent samples.
Zoom/pan samples after the first request took **8–16 ms**; maximum-budget samples took **15–21 ms**.
Qualified and client filters returned exact counts. Bubble counts matched hidden people in every
response. The first status-world response counted all 1,000,001 people; other modes excluded
not-a-fit records according to their documented defaults.

The initial lead-table measurements on this fixture exposed a separate bottleneck: first page
2,725 ms, offset 50 page 1,013 ms. These measurements precede any subsequent lead-query optimization.
They are preserved as baseline evidence, not presented as a smooth end-to-end application.

Raw synthetic evidence is in [the complete-stage results](benchmarks/map-1m-all.json) and
[the separate request results](benchmarks/map-1m-requests.json). Results were collected with other
local work running; they are reproducible samples, not capacity guarantees.

### Storage and memory controls

Layout build sorting uses disk-backed temporary storage, a 32 MiB SQLite page cache and a 256 MiB
mapping ceiling per connection. Readers use a 16 MiB page cache and the same mapping ceiling;
at most eight idle reader connections are retained globally across current and old layout files.
Whole-world ranking merges indexed, ordered class cursors and stops at the display budget.
Equal-rank cases do not scan/sort the whole population in the whole-world request.

### Ten-million run and reproduction

The earlier schema-4 **default owner-centred layout** completed on **10,000,001 actual synthetic
people**, 11,628,537 edges and 11,609,490 distinct source memberships. Fixture generation took
914.95 seconds. Default layout preparation took **1,354.02 seconds (22.6 minutes)**, with peak RSS
**485,900,288 bytes (463 MiB)**. This includes the first build, not ongoing interactive work.
Only the default mode was built at this size; the other three modes remain unverified at ten million.
The fixture includes all profiles with graph membership, so the subsequent zero-edge inclusion fix
does not change its layout rows. Raw evidence retains the build and request source hashes.

A separate request process for that earlier revision peaked at **348,651,520 bytes (333 MiB)**. Source, default
layout and prepared lead indexes occupied **9,088,486,631 bytes (8.46 GiB)**. These are retained
file sizes and per-process RSS, not free disk requirements or whole-machine memory guarantees.
The benchmark checks for at least 8 GiB free before loading fixture batches and preserves its database.

| Earlier schema-4 default case | First sample (ms) | Later four samples (ms) | People / aggregate bubbles | Bytes |
| --- | ---: | --- | ---: | ---: |
| Whole world | 442.24 | 5.07–6.05 | 600 / 20 | 110,131 |
| Qualified | 231.44 | 4.99–7.05 | 600 / 20 | 106,500 |
| Clients | 6.14 | 5.48–7.80 | 600 / 4 | 108,509 |
| Zoom | 479.83 | 8.64–10.77 | 600 / 10 | 105,343 |
| Pan | 266.46 | 12.87–14.05 | 600 / 4 | 105,005 |
| Maximum budget | 286.56 | 13.73–14.50 | 1,500 / 20 | 270,159 |
| Qualified zoom | 523.95 | 133.45–141.05 | 600 / 41 | 108,480 |
| Client zoom | 243.76 | 133.72–143.48 | 600 / 9 | 108,742 |

All responses were ready, met their individual-person budget, and had aggregate counts equal to
hidden people. Whole-world matching count was 9,983,334, including the owner; the lead list excludes
the owner and has 9,983,333 open leads. Requests bypassed the application response cache, but the
operating-system disk cache was not flushed. These are repeated local samples, not worst-case bounds.

Indexed lead preparation took **50.69 seconds**. Current request-process Score pages took
**1,505 / 554 ms** (offsets 0 / 50), and Fit pages **628 / 620 ms**. Independent arithmetic fixture
oracles verified both first 100 orderings and exact total. The separate three-repeat page run had
Fit first-page samples 3,664 / 471 / 440 ms and Score 620 / 2,060 / 1,045 ms, demonstrating disk-cache
variation. Unfiltered count computation took 2.23 ms initially and 0.10–0.13 ms afterward.

**Whole-app smoothness is not established at ten million.** Direct uncached tag facets took
**17,276 ms initially**, then **1,290–2,245 ms**. Prefix search took 1,447 ms in its first sample;
name search covered its documented bounded population and took 37 ms. The synthetic profile rows
have no bios or manual tags; one million qualification rows contribute projected fit facets. Broader
profile/tag density and live collection need their own tests. Browser drawing is measured separately
on the actual-data copy below, not inferred from database timings.

Retained evidence: [fixture](benchmarks/map-10m-fixture.json),
[default layout build](benchmarks/map-10m-build.json),
[current API requests](benchmarks/map-10m-requests.json), and
[index preparation and repeated lead pages](benchmarks/leads-10m-indexed.json).

```sh
FL_NO_ORSLOT=1 python3 tests/bench_map_large.py --directory /tmp/fortunate-scale-1m --people 1000001 --stage all
FL_NO_ORSLOT=1 python3 tests/bench_map_large.py --directory /tmp/fortunate-scale-10m --people 10000001 --stage fixture
FL_NO_ORSLOT=1 python3 tests/bench_map_large.py --directory /tmp/fortunate-scale-10m --people 10000001 --stage build --modes closeness
FL_NO_ORSLOT=1 python3 tests/bench_map_large.py --directory /tmp/fortunate-scale-10m --people 10000001 --stage requests
```

Prepare the synthetic lead projection between build and requests with
`python3 server/lead_rank.py --db /tmp/fortunate-scale-10m/synthetic.sqlite`.
Each stage writes its JSON results beside the synthetic database. Request validation includes
whole-world, zoom, pan, maximum budget, fit/client filters, search and two lead pages. Remove the
synthetic directory explicitly when the evidence is no longer needed; the benchmark never deletes
an existing database. Rendering still needs separate browser measurement on bounded responses.

### Lead-page follow-up

A thin-ID sort followed by hydration of only the selected page preserves all lead sort/filter semantics. On the same million-person disposable database, first-page time was 2,371 ms and offset-50 time was 816 ms (baseline 2,725 / 1,013 ms); total remained 998,333. This is a modest improvement, not a smooth-at-ten-million claim. Exact fit ordering still needs a matching maintained index before end-to-end scale can be claimed. Oracle tests compare every sort, six filter variants and three offsets against the original query.

## Actual-data feedback revision

The revised owner-centred default was built on a consistent copy of 111,699 saved people. All four current layouts built in 7.41 seconds after the zero-connection inclusion fix. Desktop and 390-pixel phone checks used that copy, including profile clicks, group expansion and return to the overview. The canvas with 728 loaded people painted in a measured 0.9 ms median and 1.3 ms p95. This browser drawing sample is separate from query/network latency and whole-system resource usage.

Ordinary lead pages and unfiltered counts now have explicit maintained indexes; preparation and current measurements are in [LEAD_SCALE.md](LEAD_SCALE.md). Default tag facets no longer materialize every person ID. On the actual copy, identical facet results took 331 ms initially and 275–288 ms afterward, against 996 ms for the earlier query. On the million-person synthetic fixture, the initial new query took 1,679 ms and warm samples 134–183 ms, against 3,922 ms previously. These fixtures differ in tag density; their timings do not predict every ten-million database.

### Source feedback hydration follow-up

The source-yield lookup now uses the marked person's membership index instead of
scanning entire source audiences. Exact complete responses matched the original
implementation on the actual copy and ten-million fixture for both Fit/Score
first and second pages. Ten-million warm pages were mostly 272–317 ms, against
399–552 ms previously, with occasional 472/727 ms spikes. This is a measured
improvement, not a worst-case latency bound; explicit feedback aggregation still
follows the marked population. See [lead scale evidence](LEAD_SCALE.md).

### Prepared default tag facets follow-up

The earlier 17,276 ms tag measurement above is retained as baseline. A new exact
prepared projection answers the default tag request from small transactional
facet totals. On this ten-million fixture, preparation took 7.93 seconds and
reads took 3.59 ms initially, then 0.093–0.220 ms. The independent original-query
oracle matched exactly; that separate legacy query took 30.62 seconds. The
benchmark process peaked at 90.8 MiB RSS. The actual-data copy retained 157
facet summaries and read them in 3.95 ms initially, then 0.50–0.60 ms. Filtered
requests retain the original SQL relation and need separate latency validation.

A transactional sample of 100 new profiles with verdict, tag and workflow mark
took 70.0 ms with prepared maintenance and 45.2 ms without it on the ten-million
fixture. Per-person refresh query plans use indexed searches. Existing databases
are not backfilled automatically. See [preparation and exact semantics](LEAD_SCALE.md)
and [ten-million raw evidence](benchmarks/tag-facets-10m-prepared.json).

### Combined requests after lead and tag fixes

A separate current-code run verified exact top-100 Fit/Score orderings and lead
total again. Map budgets and aggregate counts remained exact. Warm world
requests took 5.1–5.6 ms, zoom/pan 8.5–12.4 ms and filtered zoom 122.8–146.6 ms.
First map samples reached 484.5 ms. Score first/second pages took 683.9/315.5 ms;
Fit took 322.5/332.8 ms. Tags took 2.43 ms initially and 0.12–0.21 ms afterward;
counts took 0.10–0.17 ms. Process RSS peaked at 373 MiB and retained files
occupied 8.50 GiB after tag preparation.

The same prefix-search sample took 14,663.7 ms, compared with 1,447 ms in the
earlier process. This is preserved as baseline evidence; the prefix-pool fix below addresses this
specific case. Fast drawing and prepared facets do not establish every search bound. Bounded
name search took 36.6 ms. Disk cache was not flushed and other local validation
was running. [Complete final request samples](benchmarks/map-10m-final-requests.json)
preserve this limitation and the successful correctness checks.

### Full handle-prefix search follow-up

The slow handle-prefix sample unconditionally warmed a pool of up to 265,536
profile names, despite already having a full eligible prefix page. Exact/prefix
handles outrank all name and substring matches. The lookup now returns a full
prefix page directly, preserving exact-handle priority and the capped indicator;
insufficient eligible prefixes continue through the original name fallback.

On the same ten-million fixture, the complete `user000123` response matched all
20 IDs. The original first request took 17,127.5 ms, then 34.6/33.0 ms; the revised
first request took 1.78 ms, then 0.44/0.42 ms. This is a same-process comparison,
not an operating-system cache-flushed cold-read bound. Names and incomplete
prefixes can still require the initial bounded name-pool build.
[Exact before/after response samples](benchmarks/map-10m-prefix-search.json).

After validation, only generated scale-test SQLite databases/layout files were
removed: 10,825,131,161 bytes (10.08 GiB) of generated files. JSON measurements, logs and
layout manifests remain; the actual-data preview and live database remain intact.
Storage totals above describe the measured fixtures before cleanup. Reproduce
with the fixture/build stages rather than assuming those large files remain.

### Saved profiles without active connections

The actual copy contained 24 saved profiles without active connection degree.
All 24 were present in the prepared default layout, but search previously omitted
the 23 whose handles were not sources. Search now checks the prepared layout's
indexed profile IDs alongside existing graph/source presence. All 24 exact-handle
searches returned their prepared positions. The focused marked/unmarked regression
and 25 map/search tests passed. [Sanitized actual-data presence evidence](benchmarks/map-real-disconnected-search.json).
