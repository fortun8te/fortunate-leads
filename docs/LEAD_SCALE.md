# Indexed lead pages and counts

The ordinary open lead list uses an exact maintained ordering for Fit and Score. It stores the profile's fit, degree, score, followers and workflow state in a compact projection, with matching ordering indexes. Only the chosen page is hydrated with bios, notes and tags. Small maintained summaries answer the unfiltered tier, status and bio counts.

The sort and owner-exclusion rules match the original queries. Profile, verdict, connection-degree and workflow changes update the projection transactionally. Missing ordering indexes, summary triggers or connection summaries disable the fast path. Existing filters keep the original exact query. Deep offsets still skip earlier index entries and can become slower.

## Explicit preparation

Installing this version does not scan an existing database to prepare its ordering. Validate on a consistent database copy, with background collection stopped for that copy:

```sh
python3 server/lead_rank.py --db /absolute/path/to/COPY.sqlite
```

The copy must already have the application's current schema and maintained connection summaries. Preparation publishes a complete projection atomically. An interruption rolls back; another reader continues seeing the previous complete snapshot. Existing unprepared databases remain usable with the original query.

Do not prepare or restart the live database during a code review. Layout preparation is separate; see [MAP_VIEW_API.md](MAP_VIEW_API.md).

## Measurements, 30 September 2026

On the actual saved-data copy containing 111,699 people, preparation took about 0.6 seconds. Fit and Score first/second pages matched the original rows and exact total of 111,698 after owner exclusion. Warm pages took 8–12 milliseconds; unfiltered count computation took 0.07–0.21 milliseconds.

On a 1,000,001-person synthetic database, preparation took 5.56 seconds. Warm first/second pages took 38–40 milliseconds, including hydration, and unfiltered count computation took about 0.08 milliseconds. These are local samples, not capacity guarantees. Network/HTTP overhead is separate. On the 10,000,001-person synthetic copy, preparation completed in 50.69 seconds. Current-code
first/second pages took 628/620 ms for Fit and 1,505/554 ms for Score. Independent arithmetic fixture
oracles checked the first 100 IDs and exact open-lead total of 9,983,333. A separate three-repeat
run ranged from 417 ms to 3,664 ms across these pages, so the indexed ordering does not establish
uniformly instant page hydration. Counts took 2.23 ms initially and 0.10–0.13 ms afterward.
[Raw preparation/page samples](benchmarks/leads-10m-indexed.json) and
[combined request results](benchmarks/map-10m-requests.json) preserve the measurements.
Full scope and remaining tag/search limits are in [MAP_VIEW_PERFORMANCE.md](MAP_VIEW_PERFORMANCE.md).

Tests compare both orderings, totals and facets against the original queries through profile, verdict, mark, connection, bio and owner changes. They also verify missing-index/trigger fallback and interrupted preparation on an autocommit connection.

## Source feedback hydration follow-up

Page hydration now looks up source memberships by marked person, using the
person-first membership index. It avoids scanning an entire audience for each
source on the selected page. Complete Fit and Score responses, at offsets 0 and 50,
matched the previous implementation on both the actual copied database and the
ten-million fixture.

On the saved-data copy, pages took approximately 3–6 ms, previously 6–11 ms.
Ten-million warm samples were mostly 272–317 ms, previously 399–552 ms; individual
requests still had disk-related spikes of 472 and 727 ms. Initial samples were
3,292 ms before and 1,657 ms after, in one process without flushing the disk cache;
these are not controlled cold-read measurements. Work still follows the number
of explicitly marked people (100,000 on this fixture), so hydration is not constant-time.
[Ten-million response comparison](benchmarks/network-yield-10m.json) and
[actual-data comparison](benchmarks/network-yield-real.json) preserve the samples.

## Prepared default tag facets

The ordinary open tag list reads a small exact summary, maintained in the same
transaction as tags, verdicts, workflow state, explicit relationships and profile
changes. It preserves projected fit labels, owner exclusion, dangling-row totals,
nullable groups and the original branch merge order. Filtered requests continue
using the original SQL relation.

On a stopped consistent copy with the current schema:

```sh
python3 server/tag_facets.py --db /absolute/path/to/COPY.sqlite
```

Preparation is atomic, including on autocommit connections. Existing unprepared
databases keep the original query; installing the schema does not scan or
partially populate existing data. Missing triggers or the person index disable
the shortcut until explicit repair and preparation. Per-person refreshes use
indexed lookups, so collection does not rescan the entire tag population.

The actual 111,699-person copied database retained 185,851 projected rows and
157 summary entries. Exact original-query comparison passed: first prepared
request 3.95 ms, later samples 0.50–0.60 ms. The million-person fixture's first
request took 2.62 ms and later samples 0.094–0.144 ms. A transactional sample
of 100 new profiles, each with verdict, tag and mark, showed approximately
20–33 ms added total maintenance cost over an unprepared copy, retaining all
other application triggers. These are local measurements, not worst-case bounds.
[Actual-data evidence](benchmarks/tag-facets-real-prepared.json) exposes only
counts/timings and an equality digest; saved lead or tag names are not published.
[Million-person evidence](benchmarks/tag-facets-1m-prepared.json) includes
the same independent oracle and ingestion sample.

On the ten-million fixture, explicit preparation took 7.93 seconds; 499,008
projected rows became two summary entries. Prepared reads took 3.59 ms initially
and 0.093–0.220 ms afterward, with exact original-query equality. The uncached
legacy oracle in that same benchmark took 30.62 seconds, compared with 17.28
seconds in an earlier process. Peak benchmark RSS was 90.8 MiB. This fixture
measures qualified fit tags; it does not establish limits for arbitrary dense
tag inventories. The 100-profile maintenance sample was 70.0 ms prepared
versus 45.2 ms unprepared. [Ten-million facet evidence](benchmarks/tag-facets-10m-prepared.json).
