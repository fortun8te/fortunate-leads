# Backend V2 validation

3 October 2026. Compared with `7ad97519abaaaabad11a86babc2c15be95c56393` on main. All checks use the isolated V2 worktree, temporary data and separate ports. No live server, collector, model service or browser profile was started or reconfigured.

## Test environment

Backend checks use Python 3.12.14 in an isolated environment with the repository-pinned optional `instagrapi==3.0.14`. A baseline run under the host's Python 3.9.6 exposed an installed optional-package typing incompatibility; the baseline mobile transport tests pass under this same 3.12 environment. That host installation issue is not counted as a V2 improvement.

All JavaScript test extensions are included, including CommonJS files. Test migrations change mocks to their actual service owners or bind explicit temporary applications; existing behavioral assertions remain. New regressions cover request/body admission, deadlines, busy-write rollback, startup/shutdown races, cache publication, memory-database isolation, workspace settings isolation, bounded executors and late-result suppression.

## Completed checks

| Check | Result |
| --- | --- |
| Complete backend suite, Python 3.12.14 | 1,360 run in 252.81 seconds; no failures; two explicit live-model skips |
| Top-level Python provenance/benchmark tests | 21 passed |
| Optional sidecar tests | 27 passed |
| Operations/install/release tests | 55 passed |
| Complete web and extension JavaScript set | 603 passed |
| Extension timezone checks | 190 passed in each of UTC, Europe/Amsterdam, America/New_York and Pacific/Auckland |
| Default eight-hour virtual collection simulation | 82 passed, zero failed |
| One-lane / two-lane / four-lane simulations | 33 / 40 / 50 passed, zero failed |
| Whitespace/error check | `git diff --check` passed |

The two backend skips require an installed local Ollama model and an explicitly enabled live external-model run. They were not exercised. Counts for focused checks and the timezone repetitions overlap the main suites and are not added into a single inflated total.

The final complete backend run includes all integration corrections. Each multi-lane simulation saved the expected 16,900 connections across 585 pages, including cursor handoffs and warning/recovery behavior. These are virtual-clock fixtures, not live Instagram requests or a production collection soak.

Reproduction uses the repository commands: `python -m unittest discover -s server/tests -v`, `python -m unittest discover -s tests -p 'test_*.py'`, the two sidecar test modules, `python -m unittest discover -s ops -p 'test_*.py'`, every `.test.mjs`/`.test.cjs`/`.test.js` file under tests/web/extension, and `node tests/e2e/driver.mjs`. The simulator's lane comparison is a separate explicit run. Use a temporary environment for optional Python dependencies.

## Matched HTTP measurements

The fixture contains 100,000 generated people, 132,660 recorded edges and 500,524 raw tags. Every run starts from a complete SQLite backup with the same pre-migration SHA-256. Both versions ran under the same Python 3.9.6 executable, with five cold and five warm samples per route, 24 simultaneous clients per burst and no other test workload during measurement. Cold means application caches cleared; operating-system caches remain warm.

All 13 selected read responses match in status, data, order, IDs, revisions and saved timestamps. The only excluded value is `control.body.at`, which `control.snapshot` generates from the current request time. Raw reports retain it; the comparison names that one field and reason. Both runs completed all 130 timed sequential reads and 72 concurrent reads with HTTP 200 and no read errors.

Default upgrade, without extra ranking preparation. Values are milliseconds; medians are shown for sequential requests.

| Read | Base cold | V2 cold | Base warm | V2 warm |
| --- | ---: | ---: | ---: | ---: |
| Lead score order | 13.46 | 13.21 | 13.49 | 13.68 |
| Connected order | 79.62 | 80.52 | 79.57 | 79.90 |
| Minimum-list filter | 122.36 | 18.18 | 122.29 | 17.61 |
| Strong-fit filter | 105.41 | 17.93 | 105.32 | 19.13 |
| Lead search | 111.97 | 112.19 | 111.36 | 111.64 |
| Counts | 5.57 | 5.29 | 4.84 | 5.25 |
| Filtered counts | 124.28 | 125.05 | 5.04 | 5.26 |
| Tags | 5.35 | 5.14 | 5.60 | 4.99 |
| Filtered tags | 749.92 | 164.59 | 4.93 | 5.34 |
| Controls | 7.99 | 8.70 | 8.10 | 8.58 |
| Map page | 97.93 | 98.78 | 12.65 | 12.77 |
| Map view fallback | 7.98 | 7.87 | 7.49 | 7.79 |
| Map search | 5.82 | 5.70 | 5.64 | 5.91 |

| Concurrent burst, 24 clients | Base median | V2 median | Base p95 | V2 p95 |
| --- | ---: | ---: | ---: | ---: |
| Warm mixed reads | 62.39 | 68.12 | 190.00 | 151.33 |
| Cold identical map reads | 7363.05 | 268.56 | 7557.95 | 315.14 |
| Warm identical control reads | 62.07 | 78.03 | 83.92 | 94.46 |

The strongest gains are the filtered lead queries, filtered tags and simultaneous cold map requests. Most other reads are similar. Warm mixed median and warm-control tail latency were slower in this run; these results do not support a universal speedup. Five samples are descriptive measurements, not statistical confidence intervals.

The 24-client burst did not saturate the 32-request limit. Sampled non-client threads peaked at 26 for both versions on the cold map burst; this is not evidence of lower overall memory use. Connection open/close medians were effectively unchanged: 4.10 ms versus 4.05 ms.

With a one-second writer lock, the base saved successfully after 1,074.66 ms. V2 returned `503` with `Retry-After: 1` after 308.04 ms and left the record unchanged. That is a bounded retry response, not a faster successful save. Dedicated tests separately cover rollback, overload and client-visible responses.

Optional explicit ranking preparation took 437.06 ms on the temporary copy. Connected-order medians then fell from 79.62 to 16.30 ms cold and 79.57 to 16.42 ms warm. The unprepared default remained about 80 ms. Existing workspaces need that separate preparation before this improvement applies.

Committed aggregate evidence: [default upgrade](../benchmarks/backend-v2-20261003/default-upgrade.json) and [explicit ranking preparation](../benchmarks/backend-v2-20261003/explicit-ranking.json). Full captures retain generated response bodies in the local validation directory; fixture and complete pre-migration snapshot fingerprints are recorded in both aggregates. The [benchmark runner](../tests/bench_backend_v2.py) documents commands for separate base and candidate processes.

## Browser verification

An isolated headless Chromium session loaded a generated 100,000-person database at 1440 by 1000 pixels. The lead list and profile card rendered, changing a lead to Contacted produced the expected UI state, and a separate SQLite read verified that saved status. The lead and map screens had no horizontal overflow. Screenshots were inspected.

The Connections page loaded its unprepared fallback and reported 400 of 99,501 eligible people. The fixture contains no saved portraits; this does not verify the prepared compact portrait map, real photo loading or large-map graphics performance. This browser check preceded only the final stricter settings guard and thread-start failure fixes, which have dedicated regressions.

## Review scope and limits

GPT-6.1 Sol agents implemented separately owned architecture, HTTP and storage changes after the saved plan. Main-thread integration checks and a separate read-only reviewer covered the resulting code. Review corrections include shutdown-before-serve races, the shared upload budget, memory-only cache contamination, saved-data host-setting access and failed thread-start cleanup.

The original domain rules remain authoritative. API comparisons on selected routes and the complete regression suite support compatibility; they are not a mathematical proof that every possible input is identical. The application still depends on Instagram, model and public-site response times. An external subprocess already in flight is not forcibly killed at shutdown. No real collection soak, cold disk benchmark, production memory ceiling or zero-lag guarantee is claimed.

The PR is not merged or deployed. The canonical checkout remains unchanged. See [architecture and rollout notes](BACKEND_V2.md).
