# Viewport map performance

The viewport API reads precomputed layouts. It sends at most 1,500 people plus aggregated bubbles;
zooming requests a new rectangle. Existing pair comparison remains independent of this display budget.
This is a design for large databases, not a verified claim of handling 10 million people.

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

10-million-person build cost, disk use, peak memory, dense identical-rank cases, rare filter classes,
and ongoing collection under maintenance have not been measured. Prefix handle search uses an index;
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

A **10,000,001-person** fixture run was started and remained in progress at this checkpoint.
**A completed 10-million build/request benchmark is not yet available.** No 10-million smoothness
claim follows from the million-person results. The benchmark checks for at least 8 GiB free before
loading batches and preserves its generated database so stages can run in separate processes.

```sh
FL_NO_ORSLOT=1 python3 tests/bench_map_large.py --directory /tmp/fortunate-scale-1m --people 1000001 --stage all
FL_NO_ORSLOT=1 python3 tests/bench_map_large.py --directory /tmp/fortunate-scale-10m --people 10000001 --stage fixture
FL_NO_ORSLOT=1 python3 tests/bench_map_large.py --directory /tmp/fortunate-scale-10m --stage build
FL_NO_ORSLOT=1 python3 tests/bench_map_large.py --directory /tmp/fortunate-scale-10m --stage requests
```

Each stage writes its JSON results beside the synthetic database. Request validation includes
whole-world, zoom, pan, maximum budget, fit/client filters, search and two lead pages. Remove the
synthetic directory explicitly when the evidence is no longer needed; the benchmark never deletes
an existing database. Rendering still needs separate browser measurement on bounded responses.
