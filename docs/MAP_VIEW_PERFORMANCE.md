# Viewport map performance

The viewport API reads precomputed layouts. It sends at most 1,500 people plus aggregated bubbles;
zooming requests a new rectangle. Existing pair comparison remains independent of this display budget.
The measurements below distinguish preparation, indexed requests and browser drawing. The earlier 100k and million-person map samples used the pre-feedback audience arrangement. The current default restores the owner-centred network concept; its separate ten-million-person measurement is recorded below.

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

The retained streaming fixture contains **1,000,001 actual people**, skewed membership across
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

The current schema-4 **default owner-centred layout** completed on **10,000,001 actual synthetic
people**, 11,628,537 edges and 11,609,490 distinct source memberships. Fixture generation took
914.95 seconds. Default layout preparation took **1,354.02 seconds (22.6 minutes)**, with peak RSS
**485,900,288 bytes (463 MiB)**. This includes the first build, not ongoing interactive work.
Only the default mode was built at this size; the other three modes remain unverified at ten million.
The fixture includes all profiles with graph membership, so the subsequent zero-edge inclusion fix
does not change its layout rows. Raw evidence retains the build and request source hashes.

A separate current-code request process peaked at **348,651,520 bytes (333 MiB)**. Source, default
layout and prepared lead indexes occupied **9,088,486,631 bytes (8.46 GiB)**. These are retained
file sizes and per-process RSS, not free disk requirements or whole-machine memory guarantees.
The benchmark checks for at least 8 GiB free before loading fixture batches and preserves its database.

| Current default case | First sample (ms) | Later four samples (ms) | People / aggregate bubbles | Bytes |
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
earlier process. This remains an unresolved latency concern; fast map drawing
and prepared default facets do not establish uniformly fast search. Bounded
name search took 36.6 ms. Disk cache was not flushed and other local validation
was running. [Complete final request samples](benchmarks/map-10m-final-requests.json)
preserve this limitation and the successful correctness checks.
