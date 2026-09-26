# Instagram connection mapping: research and implementation plan

Research date: 26 September 2026. Repository audited at `9bac87489adad3637213c67780411f9b9127ec7c`.

## Decision

Build a browser for observed connections that helps Michael answer a specific question: "What have we actually collected that connects this account to that account?" Let him inspect the people in between and the source of every arrow. Keep commercial fit, observed network structure, and confirmed personal relationships separate.

The first implementation in this PR adds pair comparison, direct follows, two-hop patterns, evidence inspection, direction-specific collection coverage, stable-ID reconciliation at query time, and durable observations for newly accepted list pages. It also replaces automatically generated "knows you" tags with factual Instagram-link or bio-mention tags.

The ranking is an experiment. No research located establishes that its ordering identifies the best Instagram introducers. The research supports preserving direction, exposing missingness, and testing simple baselines. It does not establish friendship, trust, willingness to introduce, or a probability of getting a reply.

## What the existing product gets right and where it fails

The current Python/SQLite application already stores followers and following separately, deduplicates edges, reconciles many person records by Instagram ID, preserves manually supplied notes, and has a bounded canvas map. These are useful foundations. A graph database is unnecessary for the first useful version.

| Existing behavior | Consequence | Decision |
| --- | --- | --- |
| An edge has source seed, person, direction and first-seen time | Good direction evidence, but no record of later re-observation | Add page-level observations; leave legacy freshness unknown |
| `received` counts the lifetime union of imported edges | It is not a count of a clean current snapshot | Show counts with that limitation; do not manufacture a completeness percentage |
| `done` comes from the collector | A terminated list can still be short or have unknown size | Say collector-reported completion, and flag count mismatches |
| Seed-to-seed overlap mixes directions | A shared follower and a shared followee look equivalent | Classify directional patterns explicitly in comparison |
| The map selects many profiles by list degree | Well-sampled or ubiquitous accounts dominate | Separate exploration ordering from qualification; show the reason |
| Person identity uses IDs, while seed references use handles | Renames can split one account or confuse a reused handle | Reconcile by ID in comparison and refuse ambiguous endpoint selection |
| A follow or bio mention adds "knows you" | The product states a relationship it did not observe | Use factual tags; reserve personal knowledge for explicit owner input |
| Interest/talking/client statuses feed `client_seeds` in qualification | Prospect interest is conflated with client proximity | Document as a separate ranking debt; do not use this field in connection comparison |

Code anchors: `server/db.py` (`upsert_person`, `add_edge`), `server/server.py` (`ext_list_page`, `seed_links`, `map_graph`, `network_context`), `server/qualify.py` (`rule_tags`, `network_strength`). The old lead workspace under the September 23 project is a backup, not this PR's implementation base.

## What research supports

### Missing data changes the answer

Scraped lists are an egocentric sample around chosen seeds. They are not a random sample of Instagram. Non-uniform missing-edge patterns affect link-prediction performance; egocentric sampling is one identified source of missingness. A low observed overlap can mean little overlap, poor collection, or both. [He et al., PLOS ONE](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0306883)

Product consequence: missing means unknown. Keep followers and following coverage separate for each source. A path that exists in the collected evidence can be displayed, but a missing path cannot establish disconnection. Do not divide an observed score by estimated coverage and call the result corrected. Unknown sampling order and correlated omissions prevent that claim.

### A follow graph is not a friendship graph

Research on Twitter distinguishes declared follower networks from the smaller network of actual interaction. Tie-strength research used richer features and user-rated relationships, rather than follower adjacency alone. These findings caution against inferring interpersonal access from scraped Instagram lists. They are not Instagram-specific accuracy estimates. [Huberman, Romero and Wu](https://arxiv.org/abs/0812.1045), [Gilbert and Karahalios](https://dl.acm.org/doi/10.1145/1518701.1518736)

Product consequence: say "observed follow," "shared follower," or "two-hop follow path." An owner tag that says Michael knows X is useful context about Michael and X. It says nothing about X's relationship with the target or willingness to introduce them.

### Short paths are useful candidates, not proof

Classic link-prediction research compares common neighbors, Jaccard overlap, Adamic-Adar, preferential attachment and path-based measures. Shortest distance alone was weaker than several other methods in its collaboration-network experiments. Many distance-two pairs did not later collaborate. [Liben-Nowell and Kleinberg](https://www.cs.cornell.edu/home/kleinber/link-pred.pdf)

A microblog study found useful signals in directed two-hop patterns, but also showed that sampling could distort motif distributions and prediction performance. Its target was future follows, not introductions. [Link Formation Analysis in Microblogs](https://www.cse.lehigh.edu/~brian/pubs/2011/SIGIR/link-formation.pdf)

Product consequence: start with a comprehensible local comparison. Do not claim that two hops is a universal optimum. Longer paths may eventually be useful, but each extra intermediary adds interpretation and interface costs.

### Popularity can make weak models look strong

A link-prediction benchmark can reward node-degree bias, with a degree-only baseline appearing nearly optimal under common edge sampling. Evaluation must separate popularity from recommendation usefulness. [Aiyappa et al., ICML](https://proceedings.mlr.press/v267/aiyappa25a.html)

Adamic-Adar reduces the contribution of highly connected common neighbors. Standard NetworkX Adamic-Adar is defined for undirected graphs and rejects directed graphs. Our use of a related degree penalty inside a directional pattern class is a product heuristic, not a claim to implement standard directed Adamic-Adar. [NetworkX documentation](https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.link_prediction.adamic_adar_index.html)

Product consequence: disclose whether degree means observed degree in this dataset or a public profile count. Neither is a complete measurement of relationship quality. Repeated imports and reciprocal directions must not inflate unique-neighbor counts.

### Time requires its own evidence

Dynamic link-prediction research finds that recurring edges and easy negative samples can flatter results. It proposes tougher temporal evaluation and a memorization baseline. [Poursafaei et al.](https://arxiv.org/abs/2207.10128)

Product consequence: first observed is not follow-start time. A page replay is not a new observation. An old edge remains historical evidence unless a defensible later snapshot establishes its absence. This release records new positive observations; it does not infer unfollows or declare edges currently active.

### Focus and inspection beat an unreadable overview

Neo4j Bloom organizes exploration around searches, selected results, perspectives and inspection of node and relationship properties. That is a useful interaction precedent. It does not require adopting Neo4j. [Bloom documentation](https://neo4j.com/docs/bloom-user-guide/current/about-bloom/)

Sigma separates graph algorithms from rendering and uses WebGL for larger visualizations. Its maintainers recommend D3 for smaller, heavily customized graphs. The existing canvas overview and a small SVG comparison are sufficient for the current interaction. Revisit WebGL only after measuring a real rendering bottleneck. [Sigma documentation](https://www.sigmajs.org/)

Meta's documented Instagram APIs center on authorized professional-account management. This review did not establish an official arbitrary-profile follower-list API. The mapping layer must operate on available exports and preserve their provenance, rather than assume an unlimited official collection feed. [Meta Instagram Platform](https://developers.facebook.com/documentation/instagram-platform)

## The graph model

Use four distinct layers.

1. **Account identity.** An Instagram account ID where available, its current handle, and the evidence used to resolve it. A username is an alias, not an eternal identifier. People and seeds representing the same ID become one graph node. Accounts with conflicting IDs must not merge just because a handle matches. Unresolved handle-only identities stay provisional.
2. **Observations.** A particular source list contained a particular account in a particular direction when a page was accepted. Store the source seed, person, direction, page identity, job and observation time. Preserve independent observations of the same follow without counting them as separate follows.
3. **Derived topology.** Directed follows, reciprocation, directional two-hop patterns and overlap, each backed by observations. This layer can be rebuilt and audited.
4. **Human context.** Owner-confirmed familiarity, explicit relationship notes, and eventually specific introduction outcomes. Commercial workflow states remain separate.

The source conversion is fixed:

| Imported list | Observed directed edge |
| --- | --- |
| B occurs in A's following list | A → B |
| B occurs in A's followers list | B → A |
| Both observations refer to A → B | One directed edge with two evidence records |
| A → B and B → A observed | Two directed edges, described as observed reciprocal follows |

Discard self-loops from connection discovery. Keep endpoint identity collisions visible as errors rather than choose silently. A deleted or renamed account is not evidence that a different account now using its old handle owns its historical follows.

### Coverage vocabulary

The UI should distinguish these cases without turning them into a confidence percentage:

| State | Meaning |
| --- | --- |
| Uncollected | No corresponding list record exists |
| Partial | Collection has not reported finishing, including running, paused, blocked or error states |
| Reported complete | Collector says it finished; current storage does not prove a complete, fresh snapshot |
| Count mismatch | Recorded count exceeds the reported total, or collection ended with different recorded and reported counts |
| Freshness unknown | Legacy edge has a first-seen date but no durable later observation |

A reported total can change during collection. `received > total` may reflect a historical union, account churn, hidden accounts or collection inconsistencies. It must not become "more than 100% complete." List `updated_at` is list activity, not the last time every edge was observed. Observation timestamps prove collection history, not simultaneous existence of an entire path.

## Pair comparison and ranking

For selected accounts A and B, find direct observed follows and intersect their observed neighbor sets. Evaluate each shared account X using all directed edges among A, X and B.

| Pattern | Display meaning | What it does not prove |
| --- | --- | --- |
| A → X → B | Follow path from A to B via X | X can introduce A to B |
| B → X → A | Follow path in the reverse direction | A has access to B |
| A → X ← B | Both follow X | A and B have met X |
| A ← X → B | X follows both | X knows either personally |
| A ↔ X ↔ B | Reciprocal follows on both legs | Closeness, consent or willingness |

A candidate can have multiple patterns. Preserve the full set rather than force one mutually exclusive label. Direct relationships are shown separately from the ranked intermediaries.

The initial ordering groups reciprocal support first, then directed paths, then shared-audience patterns. Within a group, smaller observed neighbor counts receive greater weight using `1 / log2(2 + observed_degree)`. A deterministic handle/ID tie break makes the result reproducible. The number is an ordering device, not a probability. Future human evaluation may justify changing the group order or eliminating the penalty.

The query evaluates all observed candidates for the selected pair before applying its result limit. It returns total candidates, returned candidates and a truncation flag. The ordinary overview map's display cap and lead filters must not silently narrow comparison. No list overlap denominator is called Instagram-wide overlap.

For future seed comparisons, keep three separate denominators: following/following overlap, followers/followers overlap, and direction-specific cross overlap. Jaccard can describe overlap between the two **observed sets** if those sets are explicitly named. It cannot recover true full-list similarity from biased partial lists.

### Alternatives considered

| Method | Useful for | Why it is not the default |
| --- | --- | --- |
| Raw shared count | Transparent baseline | Large lists and common hubs dominate |
| Observed-set Jaccard | Comparing two collected audiences | Partial-list denominators can mislead |
| Adamic-Adar-like penalty | Dampening ubiquitous intermediaries | Observed degree is biased and incomplete |
| Personalized PageRank | Broader relevance around chosen anchors | Requires calibrated edge semantics; harder explanations |
| Shortest paths / k-shortest paths | Exploring longer explicit routes | Short paths through celebrities need not be useful |
| Communities | Navigating large subnetworks | Clusters can reflect seed choice and collection coverage |
| Embeddings / graph neural networks | Learned recommendations with outcomes | No defensible target labels or benchmark yet |
| LLM-written relationship guesses | None in the evidence layer | Cannot create a missing follow or a confirmed relationship |

An LLM can summarize displayed evidence later, provided every sentence links to that evidence and unobserved claims are rejected. It should not decide account identity or silently create graph edges.

## Collection strategy and the eventual snapshot design

Collection should resolve a decision, not chase the largest graph. For a selected comparison, show which endpoint directions are missing, then let the owner decide whether a new collection is worth the cost. Preserve account pacing and existing backoff. Do not automatically start a scrape simply because someone opens a comparison.

A future queue priority can combine an unresolved user question, direction missingness, observation age and expected new information. These are proposed criteria, not a validated formula. Give exploratory accounts a share of the queue so the system does not collect only already highly ranked profiles and then mistake that feedback loop for importance.

The proper next migration is explicit collection runs:

- `collection_runs`: stable seed account ID, direction, start/end, collector/source, stop reason, displayed count at start/end, terminal cursor status, errors and a run identifier.
- `run_members`: run ID, account ID, page identifier and observation time, unique per account within a run.
- `follow_observations`: identity-resolved follower/followee, run reference, first/last observed and evidence state.
- `relationship_assertions`: subject/object, assertion type, entered by, timestamp, source note and optional correction/revocation.
- `connection_outcomes`: reviewed route, useful/irrelevant/unknown, person actually known, introduction offered/requested/completed and response if the owner records it.

Only a trustworthy complete run can contribute evidence of absence. Even then, use "not present in the later completed snapshot," retain the older observation, and distinguish a likely unfollow from collection errors. Two complete runs should not overwrite history. A partial run must never retract an edge. The additive observation table shipped here prepares evidence inspection without pretending the old accumulated list table already has run semantics.

## Performance and product boundaries

Keep SQLite as the source of truth. Use the current seed/person indexes to load endpoint neighborhoods, intersect canonical IDs, and retrieve candidate degrees in batches. Avoid enumerating every account pair or fetching the entire database into the browser. Return a bounded result set and detailed evidence only for returned routes.

The overview answers "what did we collect?" The comparison answers "what evidence connects these two accounts?" The leads table answers "who fits the agency?" Distinct questions can share data without sharing one opaque score.

New observation storage grows with accepted pages and memberships. Monitor row counts and disk use. A later retention policy must preserve the first/last observation and sufficient audit references before compacting old page history. Do not add live account exports, access keys, browser sessions or personal relationship notes to Git fixtures. Tests and screenshots use synthetic profiles.

## How to tell whether it works

Correctness comes before recommendation metrics:

- Direction holds for follower imports and following imports.
- Two sources supporting the same directed follow produce one topology edge.
- Replayed pages do not refresh observation time or add new people.
- Stable IDs preserve identity across aliases; conflicting ID/handle records never merge silently.
- No path is returned unless every arrow has source evidence.
- Missing lists and terminal count mismatches remain visible.
- Ranking operates before display truncation and is deterministic.
- A historical first-seen date is never shown as a follow-start date or a fresh recheck.
- Partial collection never removes historical edges.
- Profile text cannot inject markup into the evidence view.

For usefulness, create a consented owner-reviewed evaluation set of actual target questions. Compare raw overlap, observed-degree ordering, the proposed directional ordering, and a simple familiarity-first baseline. Record top-5 useful candidates, time to find an inspectable route, rejected misleading suggestions, and introductions confirmed by the owner. "Useful" and "unreviewed" need separate labels.

Split results by low/high collection coverage, observed degree, new/existing targets and known/unknown source relationships. Keep all representations of an Instagram ID in one split. Use a time-based holdout when testing recommendations based on past observations. A repeated import of an old edge is not a future outcome. Include difficult candidate comparisons rather than only random disconnected negatives.

Proposed performance targets, to be verified on Michael's machine: comparison p95 below one second on 100k people with a representative edge distribution; bounded API graph at most 102 nodes at the maximum 100 intermediaries; responsive keyboard selection and no horizontal overflow at 390px. These are engineering acceptance goals, not research results or production guarantees. The UI requests 20 intermediaries and draws only the selected two- or three-account route. Measure high-degree endpoint cases separately.

## Adversarial check

The initial proposition was that reciprocal and directed two-hop patterns would find the best prospective connectors. The evidence does not validate that proposition for incomplete Instagram lists. The strongest supportive papers predict follows or collaborations in other networks. Declared follows can differ greatly from actual interaction, and sampling can change apparent motif quality.

The surviving proposition is narrower: an identity-aware, direction-preserving evidence browser makes the collected data inspectable and avoids several concrete errors in the existing map. The experimental ranking is a navigational convenience that should earn its place through owner review.

## Delivery stages

**This PR:** additive observation ledger and replay protection; pair comparison API; focused map and evidence/coverage panel; honest source tags; tests; this research and the API contract. The existing qualification formula and global overview selection remain as separate product behavior.

**Next:** snapshot/run model; explicit relationship assertions and corrections; outcome capture; tests using realistic missingness and handle changes; validated ranking comparison.

**Only after those are useful:** longer paths, saved comparisons, collection suggestions tied to questions, cluster summaries and alternative renderers if benchmarks justify them.

No follow-based introduction probability, inferred friendships, automatic outreach, bulk re-collection or production deployment is part of this PR.
