# Connection compare scaling

`/api/connections` comparison stages endpoint edges and candidate degrees in SQLite. Python only hydrates the two requested profiles and up to 100 displayed connectors. Directed source records, aliases, ranking, coverage, and historical observation evidence are unchanged.

For a standalone API comparison, the function temporarily uses SQLite's file temp store and restores the connection setting after its read transaction. This keeps dense temporary intersections out of process memory. If a caller already owns a transaction or has temp objects, its temp-store policy is preserved; a caller using memory temp store may use more RAM.

## Reproduction

The benchmark operates only on synthetic databases. Each sample runs in a fresh process and reports end-to-end `compare` time, peak RSS, candidate count, and a hash of the returned JSON:

```sh
python3 tests/bench_connections.py \
  --db /private/tmp/fl-map-scale-100k.sqlite \
  --db /private/tmp/fl-map-scale-1m.sqlite \
  --db /private/tmp/fl-map-scale-5m.sqlite \
  --runs 5 --dense-people 20000
```

The 100k, 1M, and 5M fixtures have two source handles with respectively 1,667, 16,667, and 83,334 shared candidates. The generated dense-hub fixture has two handles following the same 20,000 people. Measurements below were taken on 2026-09-27 on the local Mac. Filesystem cache and temp-file I/O can make first samples slower.

| Edges | Shared candidates | p50 | p95 | Peak RSS |
| ---: | ---: | ---: | ---: | ---: |
| 100,000 | 1,667 | 54 ms | 119 ms | 32 MB |
| 1,000,000 | 16,667 | 946 ms | 1,363 ms | 63 MB |
| 5,000,000 | 83,334 | 2,880 ms | 5,399 ms | 131 MB |
| 40,000 dense hub edges | 20,000 | 147 ms | 147 ms | 37 MB |

The prior 5M path took 8.63 seconds and 1.45 GB peak RSS in one fresh-process run. Its full JSON result matched the new path on all three fixtures. A separate 150-case seeded differential check covered identity aliases, edge directions, and source provenance. The connection-specific test suite passes 44 tests. This is a substantial memory and latency improvement, though a cold 5M comparison may still take several seconds.
