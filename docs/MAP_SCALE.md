# Overview map scale, September 2026

This change keeps the map an evidence view: an arrow records a directed follow observation, and source overlap counts people observed in two collected lists. Neither implies friendship or an introduction. Historical edges remain visible with their separate state and never enter current degree or overlap counts.

## Read path and freshness

- `map_person_degree` holds one row per existing person with a current observed source, its distinct current-source count, ranking score, and hidden status. `map_source_handles` holds the handles used by seed records or any historical edge. `map_seed_member` holds one row per current person/source pair; `map_seed_degree` counts those rows by source. Storage and mutation work are linear in observations and current memberships. There is no stored source-pair product.
- Triggers maintain each summary in the same SQLite transaction as changes to edges, decisive evidence, people, verdicts, marks, and source handles. Timestamp-only evidence updates skip the expensive membership recomputation. The map's simple unfiltered ranking uses two indexes and hydrates only displayed rows. Arbitrary filters use the exact general query.
- `/api/map` reads revision, chosen people, and source overlap in one snapshot. Its complete response cache and overlap cache are keyed to the exact committed data revision; a newly committed observation appears on the next poll. Uncommitted reads bypass both caches. If readiness markers or maintenance triggers are missing, reads use the original `current_edges` path; `db.init()` rebuilds a missing summary on startup. A failed backfill leaves the ready marker unset and can be retried.
- The canvas keeps positions and pinned nodes when a new revision arrives. A request sequence rejects late responses, including two requests for the same URL. At distant zoom, crowded individual follow lines are hidden with an on-canvas zoom hint; selected or hovered connections remain visible.

## Synthetic measurements

Generated with `tests/bench_map_scale.py` on this Mac. Each fixture has 12 source accounts, one or two directed current edges per person, exact evidence, verdict scores, and no live records. These are local in-process handler measurements, excluding HTTP transport and browser parsing, not production guarantees.

The UI includes `today` in every request. It affects results only when a follow-up filter is active. Earlier tests omitted it, so they missed a full-sort path that the UI took. The map now opens with a 400-person sample and offers 1,000 and 3,000 explicitly. The table measures the expanded 3,000-person request, `today=2026-09-27&scope=leads&limit=3000`. It uses ten runs for the optimized path, with nearest-rank p95. The forced-general-plan column is a single same-fixture diagnostic that reproduces the old query choice; it is not a ten-run baseline.

| Edges / people | Forced general plan, one cold run | UI cold p50 / p95 | Same-revision UI poll p50 / p95 | Compact JSON | Peak process RSS |
| --- | ---: | ---: | ---: | ---: | ---: |
| 100k / 80k | 256 ms | 90 / 147 ms | 0.20 / 0.27 ms | 1.80 MB | 123 MB |
| 1M / 800k | 1,693 ms | 402 / 730 ms | 0.19 / 0.31 ms | 1.85 MB | 302 MB |
| 5M / 4M | 5,915 ms | 1,793 / 3,003 ms | 0.18 / 0.30 ms | 2.03 MB | 326 MB |

The original uncached 400-person stages took 268/116 ms at 100k, 2,869/1,356 ms at 1M, and 16,787/7,406 ms at 5M for map selection and overlap respectively. Those stages were measured separately and must not be added and presented as an API timing. A same-fixture 5M read-through check, with all summary readiness markers disabled, returned exactly the same 400-person API data as the optimized path. That check took 28.26 seconds versus 1.93 seconds for the optimized complete API in the same process. The UI query now uses `map_score_rank` and an indexed source-handle lookup; it no longer sorts 4M people to choose the displayed sample.

The broad 10,000-person overview remains bounded, but is heavier: one measured 5M cold request took 4.85 seconds and returned 6.77 MB compact JSON with 10,000 people and 19,901 links. A true all-edges canvas remains outside this endpoint's contract.

Historical measurement before the pair-comparison optimization: `/api/connections` loaded full endpoint neighborhoods. On these same fixtures, comparing two high-degree source handles took 179 ms and 76 MB process RSS at 100k edges, 2.13 seconds and 458 MB at 1M, and 8.96 seconds and 1.61 GB at 5M. The 5M case found 83,333 candidates but returned only 20 and about 40 KB JSON. This was addressed by the later SQL-staged comparison in `connection_compare_performance.md`. Its separate measurements preserve historical directed evidence; the numbers above are not current comparison performance.

## Migration and write cost

| Fixture | Before database | After database | Extra |
| --- | ---: | ---: | ---: |
| 100k edges | 51.8 MB | 61.4 MB | 9.6 MB |
| 1M edges | 527.7 MB | 626.1 MB | 98.4 MB |
| 5M edges | 2.663 GB | 3.160 GB | 497 MB |

One-shot `db.init()` backfill measured 0.5 seconds at 100k and 5.7 seconds at 1M. A one-shot rebuild of all summaries on the 5M fixture took 54.13 seconds and reached 560 MB process RSS. The fixture was also migrated in four development stages: 16.75 seconds for degree, 4.38 seconds for sources, 10.4 seconds for rank fields/indexes, and 24.79 seconds for memberships. Normal reopening of the fully migrated 5M fixture took 0.01 seconds.

On the 5M fixture, 500 new current memberships took median 27.0 ms with summary triggers versus 4.24 ms with those triggers temporarily removed in a rollback-only probe. Five hundred active-to-absent flips took 8.88 versus 2.62 ms. Five hundred timestamp-only edits took 3.13 versus 2.65 ms. A single person with 1,000 distinct source accounts took about 1.07 ms for one add or flip; there is no quadratic pair table. These are bulk SQLite statement timings, not collector throughput forecasts.

## Reproduce and verify

Use only an explicit synthetic scratch path. The generator bulk-loads without summary triggers, then `--migrate` performs the one-time backfill. A typical invocation is:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/bench_map_scale.py --edges 100000 --db /private/tmp/fl-map-scale-100k.sqlite --build --migrate --api-only --ui-shaped --runs 10
PYTHONDONTWRITEBYTECODE=1 python3 tests/bench_map_writes.py /private/tmp/fl-map-scale-100k.sqlite
PYTHONPATH=server PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s server/tests -p test_map_summary.py -q
node --test tests/ui/map.test.mjs
```

The migration is additive: prior code can still read the original edge/evidence tables. On code rollback, leaving the new tables and triggers installed preserves their bookkeeping for a later retry. Do not manually drop triggers while the new reader is serving requests; the reader detects missing triggers and uses the exact read-through path until `db.init()` repairs them. No live database was migrated, and no collector was run for these measurements.
