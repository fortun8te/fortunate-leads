# Viewport map performance

The viewport API reads precomputed layouts. It sends at most 1,500 people plus aggregated bubbles;
zooming requests a new rectangle. Existing pair comparison remains independent of this display budget.
This is a design for large databases, not a verified claim of handling 10 million people.

## Synthetic measurement, 30 September 2026

On the local Mac with Python 3.9, 100,000 fictional people plus 16 source profiles, 30 source lists.
The fixture took 4.41 seconds to prepare; all four layouts took 7.57 seconds to build sequentially.
Five uncached whole-world requests per mode used the default 600-person budget and scope=all.
Other test processes were running, so these are representative samples rather than isolated capacity limits.

| Mode | Individual people | Matching people | Hidden people | Response bytes | Five samples (ms) |
| --- | ---: | ---: | ---: | ---: | --- |
| Closeness | 600 | 99,279 | 98,679 | 117,618 | 51.87, 45.10, 23.82, 181.62, 32.54 |
| Fit | 600 | 99,279 | 98,679 | 102,618 | 21.47, 102.73, 15.79, 93.28, 64.62 |
| Sources | 600 | 99,279 | 98,679 | 116,982 | 24.64, 18.22, 27.90, 29.80, 66.29 |
| Status | 600 | 100,016 | 99,416 | 106,711 | 47.63, 26.23, 27.48, 30.72, 15.56 |

Every response's summed bubble count exactly equalled hidden people. Other modes omit not-a-fit people
by default; status mode includes them. This explains the differing totals.

Reproduce without live data:

```sh
FL_NO_ORSLOT=1 python3 tests/bench_map_view.py --people 100000 --runs 5
```

The benchmark creates and removes its own temporary database. Tests also compare all displayed
people and totals against exhaustive layout rows across 48 deterministic viewports. They check
incremental status updates, source deletion, HTTP routes, conditional 304 responses, input validation,
and honest missing-layout states.

## Limits still to measure

10-million-person build cost, disk use, peak memory, dense identical-rank cases, rare filter classes,
and ongoing collection under maintenance have not been measured. Prefix handle search uses an index;
name and substring search deliberately covers a bounded engaged/top-ranked population. Layouts need
an explicit first build. No request starts collection or a full layout rebuild.
