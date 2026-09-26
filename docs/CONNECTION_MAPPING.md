# Instagram connection mapping

Research and implementation decision, 26 September 2026.

The useful product is a connection evidence explorer: choose a starting account and a target, inspect short recorded follow paths, and see exactly what supports them. The map should help answer a question, rather than require the user to interpret a whole network at once. Whether a route leads to a useful introduction still needs human judgment and outcome data.

This document separates published findings, engineering deductions, implemented behavior, and work that remains. The research informed the implementation in this PR. It is not a validation study of Instagram introduction success.

## What the existing project actually contains

The current repository has a Python/SQLite server, a plain JavaScript interface, and a canvas/D3 overview. It differs from the earlier research workspace. Its `people` table records account IDs and current handles; `seeds` identifies source accounts; `lists` holds current collection state; `edges` holds deduplicated source-list memberships.

Before this change, a membership retained `seed`, `person_id`, `direction`, and `first_seen`. The direction was available and decoded by the interface. However, the overview's source-to-source overlap combined followers and following, and its displayed subset did not disclose the total matching population. The overview's source endpoint used a handle, while current source metadata could later overwrite that handle's Instagram ID. Repeated observations did not update an edge recording date. These details constrain what a trustworthy mapping system can infer.

The task is therefore two problems: preserve account identity and collection evidence, then make those observations understandable. Adding a better force layout alone cannot fix an identity or evidence error.

## Research findings that change the design

### A follow is a directed observation

Use `A → B` to mean that A follows B. The same observation can arrive through A's following list or B's followers list. It is one directed edge with two supporting source records, not two relationships. A separate `B → A` observation establishes reciprocal follows in the saved data.

For a pair A and B, a shared account C can mean four different things:

| Pattern | Meaning | Product label |
|---|---|---|
| A → C ← B | Both follow C | Accounts both follow |
| A ← C → B | C follows both | Accounts following both |
| A → C → B | A follows C, who follows B | Two-step forward chain |
| A ← C ← B | B follows C, who follows A | Two-step reverse chain |

These are definitions, not interchangeable signals. Directed-network research also distinguishes the structure lost when direction is discarded. [Directed-network study](https://www.nature.com/articles/s41467-020-14394-x)

An undirected traversal can be useful for exploration, but it must not change the displayed arrows. The shared-publication example `A → magazine ← B` should remain visibly different from a forward chain. Neither proves that the magazine can introduce A to B.

### Declared connections are not demonstrated relationships

Huberman, Romero and Wu found that interaction networks and declared social connections differ substantially. This is Twitter evidence, not a measured Instagram conversion model, but it supports refusing to treat a following link as friendship. [Social networks that matter](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1313405)

Affinity's relationship-strength feature relies on actual communication frequency and recency. Its target workflow includes reviewing connection evidence before requesting an introduction. That is a useful product reference, but its strength labels cannot be transferred to Instagram follow lists, which lack that evidence. [Affinity relationship strengths](https://support.affinity.co/s/article/Leveraging-your-Connections-and-Relationship-Strengths), [Affinity target introductions](https://support.affinity.co/s/article/Find-warm-introductions-to-a-target)

**Decision:** call results recorded paths. Reserve confirmed relationships and willingness to introduce for explicit user-supplied evidence. Keep lead fit, follow structure, and data coverage separate. Do not create a percentage that pretends to combine them into a calibrated probability.

### Missingness changes the graph

Network missing-data research shows that omitted nodes and edges affect reconstructed structure. Work on non-random sampling explains why a crawl centered on chosen sources is not a representative sample of the whole network. [Kossinets, missing data in social networks](https://www.sciencedirect.com/science/article/pii/S0378873305000511), [Non-randomly sampled networks](https://www.nber.org/system/files/working_papers/w25270/revisions/w25270.rev0.pdf)

For this application, an absent edge can mean no collection, a partial list, an error, an old capture, an unidentified account, or an actual absence. The first five are enough to prevent treating absence as a negative relationship fact. Even a collector that reaches the end may have received a capped or changing list.

**Decision:** report source and direction separately, show captured count and expected count when available, preserve the stop/error state, and say "no path found in the searched records within three steps." A count match is a count match, not proof of complete current coverage.

### A temporal union can invent apparent reachability

Temporal-network research distinguishes aggregated structure from connections available through time. [Holme and Saramäki, Temporal Networks](https://arxiv.org/abs/1108.1780)

Our deduction is specific: follows first recorded in different imports may not have coexisted. A follow is a persistent relationship rather than a momentary contact event, so sorting observation timestamps into chronological order would not solve this. We need repeated observations or defensible validity intervals to make current or simultaneous-path claims.

**Decision:** this release shows a historical union. "First recorded" and "last recorded" are server receipt/import dates, not the start and end of a follow relationship. A list's `updated_at` is operational metadata, because queueing and status changes can update it. It is never used as edge freshness.

### A bounded local view is a stronger starting point than a global drawing

Cytoscape's maintainer recommends selecting a relevant subset around a chosen node, combining graph distance with a cutoff, then letting the user move through subsets. Graph-visualization research finds that useful representations depend on task and density. [Cytoscape discussion](https://github.com/cytoscape/cytoscape.js/issues/875), [Graph visualization research](https://link.springer.com/article/10.1186/s42492-021-00088-8)

**Decision:** keep the existing overview for exploration. Default the Map page to a pairwise explorer with a short list of routes and a small diagram for the selected route. Search the full imported account index, independently of overview lead filters. Present the actual evidence beneath each diagram.

## Ranking methods and what they can honestly say

Let `N(A)` be a deliberately chosen neighborhood, such as A's followees. Common neighbors is `|N(A) ∩ N(B)|`. Jaccard divides that count by `|N(A) ∪ N(B)|`. Adamic–Adar sums `1 / log(degree(C))` over common neighbors. These are established structural similarity baselines. Their original use does not turn them into introduction probabilities. [Liben-Nowell and Kleinberg](https://www.cs.cornell.edu/home/kleinber/link-pred.pdf)

Resource allocation instead sums `1 / degree(C)`. The original paper reports results on six networks; it does not establish a universal winner for incomplete Instagram lists. [Zhou, Lü and Zhang](https://doi.org/10.1140/epjb/e2009-00335-8)

| Candidate method | Useful for | Main failure in this dataset | Decision |
|---|---|---|---|
| Typed common-neighbor counts | Explaining overlap | Missing lists hide overlap; reciprocal motifs can occupy several categories | Ship raw observed counts per type |
| Jaccard | Comparing comparable complete sets | Truncation changes both numerator and denominator | Research metric after comparable snapshots exist |
| Adamic–Adar / resource allocation | Reducing common-hub influence | Observed degree is not global degree | Do not use for production ranking yet |
| Short paths | Inspectable structural routes | A short chain can be socially useless | Ship with provenance and qualified labels |
| Reciprocal-hop tie break | Deterministic ordering of equivalent-length paths | Two recorded directions still do not imply friendship | Ship as a disclosed heuristic |
| Personalized PageRank | Broader relevance exploration | Seeds, missingness, hubs and restart settings drive results | Offline experiment after a judged baseline |
| Community detection | Browsing groups of accounts | Partial collection and resolution choices change clusters | Later optional view, not inferred social groups |
| Learned ranking / embeddings | Combining measured predictive signals | No validated outcome labels; leakage and opacity | Defer until evaluation data exists |

The hub problem deserves special attention. If a celebrity has only two recorded connections in our crawl, using that observed degree in `1 / log(d)` makes it look unusually specific. An ordinary account with a thoroughly collected list could be penalized more heavily. This follows directly from the formula and sampling bias; the magnitude in Fortunate's data has not been measured. Unknown follower counts must not become zero, and observed degree must be labeled as observed.

The implemented route order is fewer hops, then more hops with both directions recorded, then stable account IDs. It does not use follower popularity, lead scores, or a learned weight. If exploration is capped, it is an order within found candidates, not the globally best set.

## Identity and evidence architecture

### Implemented now

The existing `edges` table stays intact for collection counts, the lead list, and the overview. A new `edge_observations` table preserves the source ID and member ID supplied with each observation, alongside the original source handle, list direction, local person reference, and first/last recording dates. Identity fields are part of the key, so a reused handle cannot overwrite a previously captured identity.

This is a compact observation summary, not an immutable event-per-page archive. Repeated real imports update the recording range; duplicate outbox deliveries do not. Different source lists that establish the same directed follow combine into one graph edge with separate evidence entries.

The graph uses captured IDs for both endpoints. Current display names can improve labels when they match the captured identity; they cannot rewrite historical ownership. Missing IDs are explicit unknowns:

- A source handle with no captured source ID becomes a historical source node.
- A member record with no captured member ID becomes a historical member node.
- These records can be selected as endpoints and inspected, but cannot serve as intermediate bridges.
- Later profile enrichment does not promote old unidentified evidence into identified evidence.
- Unverified historical handles are not clickable links to today's profile owner.

This conservative choice can show fewer routes in old data. That is preferable to joining different owners because they shared a username. The earlier research database importer now preserves original entity IDs where those were actually present. Existing handle-only rows are never backfilled from today's metadata.

Official Meta documentation also distinguishes scoped IDs. The messaging user-profile endpoint uses an Instagram-scoped ID obtained through a webhook and requires messaging permissions. A provider's numerical ID is therefore not automatically interchangeable with another provider's ID. This repository currently assumes the collector's Instagram account-ID namespace; adding other providers requires an explicit namespace. [Meta platform overview](https://developers.facebook.com/docs/instagram-platform/overview), [Meta messaging user-profile API](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api/user-profile.md)

The documented API scope should not be mistaken for a supported arbitrary follower-network export. This implementation operates on imported observations and does not add a new collection method.

### The next storage layer

For snapshot comparisons, use explicit collection runs rather than overloading current `lists` rows. A run needs source ID and namespace, list direction, start/end time, expected totals at capture, unique member count, terminal cursor status, stop reason, collector/import version, and source-file or page fingerprint. Each page should have a retry-safe identity, and each membership observation should reference its run/page and both captured endpoint IDs.

Keep raw imports or checksummed references locally. Derive the graph from observations, rather than make a rendered graph the database. Separate a user's confirmed relationship note from a follow observation. That note should have its own author, date, evidence and optional willingness-to-introduce field; it must not be inferred from imported data.

An unfollow detector belongs after this run model. Only compare compatible, sufficiently complete runs with known identities and explicit time semantics. An absent row in a partial run cannot end a relationship interval. Even complete runs give observation bounds, not an exact event time.

## Query and interface contract

`GET /api/connections/search?q=prefix` searches current imported account handles, source accounts and recorded historical source identities. Unidentified member variants are available under their current local record handle; older member handles are not indexed. Own accounts sort first. `GET /api/connections?from=...&to=...&mode=all|following` finds cycle-free paths of at most three hops. The response contains canonical accounts, typed overlap counts, paths, directed edges, original evidence, list coverage, and search-limit metadata.

`all` allows exploring edges in either direction. `following` only traverses the actual follow direction, regardless of which kind of source list supplied the evidence. Direct pairs are queried separately so high-degree endpoints cannot hide a known direct observation behind a neighborhood cap.

The initial bounds are engineering choices, not published usability thresholds: 1,000 observations per endpoint/direct query, 5,000 middle observations, 200 middle queries, 5,000 path candidates, and 12 displayed paths by default. Return limits and exploration limits are distinct. The response reports query/observation counts and separate search/display truncation flags. A clipped neighborhood can miss a shorter path; the interface says so.

The overview remains a list-membership visualization. Its source-overlap links are derived shared-member counts, not direct follows. It now returns and displays the shown-versus-matching population. The pairwise explorer supplies the stronger identity and direction semantics.

No new graph-rendering dependency is necessary for a selected route of at most four accounts. A DOM diagram is inspectable, accessible and cheap to update. For a future large interactive graph, keep analysis separate from drawing. Cytoscape documents styling/edge/label cost, and Sigma separates Graphology data processing from WebGL rendering. Benchmark the real workload before changing libraries. [Cytoscape performance](https://js.cytoscape.org/#performance), [Sigma lifecycle](https://www.sigmajs.org/docs/advanced/lifecycle/)

## What would establish that it finds better connections?

The current evidence establishes a more inspectable representation. It does not establish better sales or introduction outcomes. The adversarial review rejected that stronger claim and led to captured IDs for both endpoints, exclusion of unidentified intermediates, and explicit temporal limitations.

Build a small judged dataset next. Select diverse target/start pairs, including small accounts, large accounts, weakly covered accounts, isolated accounts, renamed handles, and paths through high-degree sources. Have the user judge relevance and identify whether they actually know the intermediate account. Record useful route, misleading route, insufficient data, and known relationship separately. Do not train on a model's own labels.

Measure precision among the first few routes, useful-route discovery rate, time to understand the evidence, coverage, and the frequency of misleading claims. Compare raw overlap, typed overlap, this deterministic path order, and any later normalized or learned ranking. Report results by collection coverage rather than only one overall average.

Use chronological holdouts. Keep future evidence and later user notes out of the features used to rank an earlier target. Simulate realistic missingness by removing whole source lists, truncating pages, withholding member IDs, and aging entire runs. Random-edge deletion alone is too friendly to seed-biased crawls; a comparative study across 250 networks and 20 missingness patterns shows evaluation is sensitive to how edges are missing. [Missing-edge evaluation study](https://arxiv.org/abs/2401.15140)

For algorithm correctness, compare the implementation with exhaustive simple-path enumeration on small graphs. Test the cap boundaries, both traversal modes, reciprocal follows, duplicate evidence, same-account endpoints, identity collisions, missing lists, empty later imports, and malformed selectors. These are implemented regression cases or explicit future snapshot cases, not a substitute for judging usefulness.

## Delivery sequence

1. **This PR:** identified observation summaries, conservative legacy handling, target/source search, typed overlap, short directed evidence paths, coverage, honest bounds, overview population disclosure, synthetic benchmarks and tests.
2. **Next:** collection-run/page provenance, namespace-aware identity, source coverage bound to IDs, and a way to confirm personal relationships independently of scraping.
3. **After judged data:** compare ranking baselines, tune the order with transparent measurements, then consider retrieval or learned ranking.
4. **After snapshot semantics:** network change tracking, expansion suggestions and an explicit refresh plan. Prioritize a missing list that could answer the user's current question, rather than collect another high-volume list by default.

Unresolved limitations: existing current-list metadata is still keyed by mutable source handle; old unknown identities cannot be reconstructed automatically; first/last summaries do not prove continuity; bounded queries can omit routes; introduction usefulness is unvalidated. The implementation exposes these limits and preserves the evidence needed for the next steps.

See [connection-mapping validation](CONNECTION_MAPPING_VALIDATION.md) for the commands, measured results and browser checks from this PR.
