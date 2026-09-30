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

On a 1,000,001-person synthetic database, preparation took 5.56 seconds. Warm first/second pages took 38–40 milliseconds, including hydration, and unfiltered count computation took about 0.08 milliseconds. These are local samples, not capacity guarantees. Network/HTTP overhead is separate. A current ten-million-person run is being measured separately in [MAP_VIEW_PERFORMANCE.md](MAP_VIEW_PERFORMANCE.md).

Tests compare both orderings, totals and facets against the original queries through profile, verdict, mark, connection, bio and owner changes. They also verify missing-index/trigger fallback and interrupted preparation on an autocommit connection.
