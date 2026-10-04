# Backend V2 plan

Requested 3 October 2026. Base: `7ad97519abaaaabad11a86babc2c15be95c56393` on main.

## Goal and scope

Replace the backend's shared orchestration with clearly owned modules, bounded request handling, explicit storage lifetimes and stoppable workers. Preserve the existing website, extension and saved SQLite data. Performance must be measured against the same base and fixture. Zero lag across all workloads is not a verifiable promise.

The source audit covers all 49 production Python modules, 20,561 lines, plus web, extension, sidecar, operations and test integrations. The 4,496-line server mixes HTTP, ingestion, records, scoring, pictures and workers. Existing domain algorithms and schema rules remain where they already express required behavior. This is an architecture rewrite, not a new scoring policy or a replacement frontend.

## Findings to address

- HTTP currently starts unbounded request threads, opens and configures SQLite per request, and can wait 15 seconds for a writer.
- Twelve background loops share process globals, create additional model/research workers and have no retained shutdown lifecycle in main.
- Configuration, response caches, image cursors and pools have dispersed ownership. Some adapters receive the whole server module.
- Concurrent cache misses can compute the same expensive response repeatedly.
- Identity merges, migrations, derived indexes and evidence writes are mixed in one storage file. These contain important correctness protections, not disposable legacy code.
- Historical documentation lags current code. The latest following-only/progress corrections are included in the base.

## Architecture and work order

1. Establish baseline backend checks and generated-data timings before editing.
2. Extract ordinary business modules for shared contracts, evidence/network context, collection, lead records, map reads, processing, qualification, profile planning and pictures. Dependencies point to their actual owners; no dynamically executed source or copied global namespaces.
3. Separate persistence into connection lifecycle, schema/migrations, identity and evidence owners. Retain ordinary compatibility exports for existing tools. Preserve schema and atomic data semantics.
4. Replace transport with explicit application dependencies, compiled route dispatch, bounded request admission, body/socket deadlines, small request connection budgets and retryable busy responses.
5. Introduce a runtime owner that retains stop signals, worker handles and pool lifecycle. Keep startup, migration and serving distinct. Isolated workspaces must not use production service or data paths.
6. Deduplicate concurrent cache computations within a database/revision/query, retain bounded storage and never publish uncommitted or obsolete results.
7. Run full regression checks and matched performance measurements; independently review the final integrated code. Open the V2 PR with exact results and outstanding limitations.

GPT-6.1 Sol agents implement separate areas. The main agent owns integration review and evidence. Parallel edits have explicit file ownership. The live checkout, database, browser profiles and collectors are outside the worktree test environment.

## Required behavior from previous work

- Preserve main-identity exclusion, warning holds, cooldowns, request pacing, selected-lane identity checks and AI-off settings. Startup and healthy heartbeats cannot clear holds.
- Preserve leases, tokens, cursors, current-run membership, capture times, source identity, count provenance and idempotent imports. A saved follow is not a personal relationship or complete collection proof.
- Keep notes local. Reject stale note/mark changes and stale model results. Preserve relationship-only records and every person-owned table during identity merges.
- Keep the compact portrait map, bounded pages and current filters. Do not restore rejected map defaults, export controls or collection experiments.
- Preserve extension acknowledgment shapes, same-origin writes, extension Origin/X-FL checks, security headers, binary map responses, picture ownership and redirect restrictions.

Evidence reviewed: current PRODUCT/HANDOFF/CONTRACT and tests; DALI knowledge topics and DALI-L001 through L020; original collection, connection, warning, workflow and map performance passages; bounded historical chat retrieval. The available chat archive is curated, not an exhaustive record of every conversation.

## Verification gates

- Server, top-level Python, sidecar and operations tests.
- All web/extension JavaScript tests, including CommonJS tests; extension timezone checks.
- Existing isolated collection simulator plus multi-lane recovery. No live collection.
- New transport admission, timeout, rollback, busy-writer, shutdown and workspace-isolation tests.
- Compare baseline and V2 response contracts and latency on matched synthetic datasets. Report cold and warm behavior separately and include tail latency, concurrency and fixture size. A synthetic benchmark is not a live collection soak or physical browser verification.
- Inspect source and final diff independently, verify PR head, and record what remains unverified. No merge or deployment is part of this PR task.

## Rollback

Retain the existing CLI and HTTP contracts and the current data format. The PR can be reverted without a destructive data conversion. Test migrations only on temporary fixtures. Deployment and backup verification belong to a separate rollout.
