# Backend V2

V2 replaces the shared server orchestration with an application that owns its services, caches, connections and background work. The website and extension keep their existing routes and saved-data format. The scoring policies, collection pacing and evidence rules are preserved.

The implementation follows [the plan](BACKEND_V2_PLAN.md). This document describes a proposed PR, not a deployed release.

## Source audit and ownership

The review covered the 49 production server modules and their web, extension, optional sidecar, operations and test consumers. The two largest mixed-responsibility files were `server.py`, 4,496 lines, and `db.py`, 1,411 lines. The new entry points retain ordinary compatibility imports and wrappers for existing tools.

| Responsibility | Owner |
| --- | --- |
| Application assembly, startup and shutdown | `server/backend/app.py` |
| Configuration, validation and common contracts | `server/backend/common.py` |
| HTTP admission, deadlines, Origin/Host checks, files and errors | `server/backend_http.py` |
| Collection, accounts, permits, checkpoints and progress API | `server/backend/collection.py` with existing account/control policy modules |
| Lead records, tags, notes and relationship edits | `server/backend/leads.py`, `queries.py`, `owner_edits.py` |
| Evidence and network context | `server/backend/evidence.py` |
| Map API and overlap reads | `server/backend/maps.py` with existing bounded map read models |
| Rules, local/external qualification, processing controls and planning | `server/backend/qualification.py`, `processing.py`, `profile_planner.py` |
| Pictures | `server/backend/photos.py` |
| Background loop and local-service lifecycle | `server/backend/runtime.py` |
| Bounded model/research execution | `server/backend/executor.py` |
| Schema, connections, identity, evidence, checkpoints and settings | `server/storage/` |
| Shared concurrent cache misses | `server/backend_cache.py` |

Existing pure/domain modules remain authoritative for account protection, collection policy, ranking, local/private note handling, rules, provider adapters, optional mobile collection and map preparation. Moving those rules into an unfamiliar implementation would add risk without itself reducing latency. Domain adapters no longer receive the entire server module.

## Performance changes

- Requests have a finite handler limit, socket/body deadlines and short SQLite writer waits. Busy writes return `503` and `Retry-After`, allowing the client to retain and retry its operation instead of sitting behind a long lock. This is a changed failure boundary, not a successful-save speedup.
- Request connections use a 2 MiB SQLite page-cache budget. Background connections retain their established budgets. This is a per-connection setting, not a total process-memory guarantee.
- Equal concurrent cache misses share one computation. Entries remain bounded, database/revision/query-specific and excluded from uncommitted or in-memory reads.
- Filtered tag counts use one selected-person relation and maintained exact projection rows/totals. The original query remains the fallback on unprepared databases.
- Simple fit and minimum-connection lead filters use maintained ranking rows and hydrate only the chosen page. Connected ordering gains an optional index through explicit offline preparation; an existing populated database keeps its fallback until prepared.
- Pools are created only when needed, background work has explicit ownership, and shutdown rejects new work and suppresses late results.

The public-site research client retains one lazy, bounded process-wide daemon pool so closing one workspace cannot cancel another workspace's shared reads. Application model and qualification pools have their own owners.

An external subprocess already running at shutdown may continue until its existing deadline. The daemon pool prevents it from holding the Python process open; this is not forceful subprocess cancellation or a guarantee of immediate temporary-file cleanup.

The HTTP handler limit is 32, plus four bounded overload-drain workers. Each rejected ordinary upload gets a short drain window so the browser can read its retry response. At extreme overload beyond both limits, rejection is best effort. The 128 MiB aggregate body limit counts encoded request bytes; decoded JSON, responses and SQLite also consume memory.

## Compatibility and safety

No data-format conversion is required. Startup migrations and triggers retain their existing semantics, and storage initialization now closes and rolls back after failure. Current-run provenance, identity-safe merges, warning holds, cursors, private notes and AI-off state remain covered by regression tests. Read and update APIs preserve their response contracts, including stale-edit conflicts, extension acknowledgments, binary map responses and picture ownership checks.

The application accepts an explicit workspace configuration. Temporary saved-data previews cannot launch or reconfigure host services through the normal controls. `--no-workers` serves an explicitly selected database without background processing. It still performs normal database initialization on that selected copy, so it is not a read-only SQLite mode.

Tests and simulations use temporary databases. The collection simulator explicitly constructs its HTTP application and never invokes production startup or background workers. It advances the actual module owners' virtual clocks and runs only its permitted manual ticks.

## Reproducing comparisons

`tests/bench_backend_v2.py` accepts only a fixture marked by `tests/bench.py`, copies it into a distinct temporary database and verifies its complete pre-migration fingerprint. Run the baseline and candidate in separate processes with the same interpreter and fixture. The comparison retains saved timestamps, IDs, order, totals and revisions. It excludes only the validated current-response clock `control.body.at`; the raw captures retain that field too.

The optional `--prepare-ranking` pass records preparation time separately. Never present that result as the default upgrade path. Application-cache-cold measurements keep operating-system disk caches warm. A generated HTTP fixture does not prove real collection throughput, prepared-map rendering, portrait loading or model latency.

Final measurements and checks are recorded in [the validation report](BACKEND_V2_VALIDATION.md).

## Rollout

This work does not merge, deploy, restart the live server or resume collection. A rollout should use the established backup and release procedure. The unchanged storage format permits reverting the PR without reversing a destructive migration. Explicit ranking preparation is optional and should be measured on a stopped copy before a production rollout.
