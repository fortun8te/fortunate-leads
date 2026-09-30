# Explore connections without loading the whole database

The daily workflow stays simple: collect from chosen accounts, review ranked leads, open a person,
record what happened, and complete or reschedule their follow-up. The map helps answer a specific
question about a person or audience. It should not become another inbox.

## Map workflow

Start with the owner in the centre and surrounding evidence spheres. Open a sphere to reveal people, with a limited zoom range and a return to the overview. Search for a handle
to go directly to that person. Select a person to read their profile and recorded connections;
compare two profiles when the question is who follows whom or which accounts appear on both sides.
Keep the evidence, capture time, and collection limits available beside the drawing.

The map has a bounded number of individual nodes per viewport. Aggregated bubbles account for
the matching people outside that drawing budget. A bubble count means people in the stored data,
not Instagram's entire audience. Directional follows, shared source lists, and manually recorded
relationships remain separate.

## Why this approach

[Sigma.js](https://sigmajs.org/) uses WebGL for thousands of nodes and edges. Its rendering engine
does not solve fetching or querying an entire database. [Graphistry's client documentation](https://github.com/graphistry/graphistry-js)
describes server GPU processing and streamed WebGL views for much larger graphs. That is a different
deployment cost from this private Mac application. [Neo4j Bloom](https://neo4j.com/docs/bloom-user-guide/current/bloom-visual-tour/bloom-scene-interactions/)
supports graph exploration and several layouts, including GPU accelerated layout.

The applicable pattern here is to query a bounded view of indexed, stable positions and request
connection evidence only for selected people. A fixed drawing budget limits browser work as the
database grows. Precomputed positions and aggregate counts move full-data work away from interactive
requests. Layout freshness must stay visible while updates are pending.

For the Leads table, [SQLite's scrolling-window guidance](https://sqlite.org/rowvalue.html)
explains why large offsets grow slower and indexed continuation keys avoid discarding earlier rows.
The current table still uses offset pagination. Replacing that contract requires preserving filters,
sort ties, export behavior, and saved views; it is separate from the map upgrade.

## Verification boundary

Ten million people is a design target, not a performance guarantee. Report fixture size, build cost,
request latency, response size, and browser frame cost separately. Synthetic collection tests do not
establish real Instagram coverage or authorize faster pacing. Existing map measurements in
`MAP_SCALE.md` describe the previous endpoint; pair comparison improvements are documented separately
in `connection_compare_performance.md`.
