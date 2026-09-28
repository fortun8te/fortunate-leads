# Raw-edge route benchmark

This optional benchmark compares Chrome REST, mobile REST, and private mobile GraphQL using one alternate viewer and a frozen known-edge snapshot. It does not ingest benchmark observations into the live graph or change the UI. Normal list/profile collection is reserved while it is armed.

## Arms and bounds

- Web REST: requested 50, 100, 200.
- Mobile REST: requested 50, 100, 200.
- Private mobile GraphQL: native size. The pinned library has no confirmed count parameter; this arm is never labelled 200.
- Following first: 12 fixed public targets, four in each following-size band (<500, 500–1999, >=2000), aiming for two verified and two unverified in each band. Any same-size verification fill is disclosed.
- Seven single-request warmups, then explicit measured-phase advancement. Up to two pages per target/arm, 175 requests total and a 60-minute bound. A two-page stop is partial coverage.
- Route order rotates across targets using a recorded seed. Each arm gets an independent novelty set against the identical baseline; warmup observations do not affect measured novelty.

The snapshot contains canonical numeric source/target pairs. Historic IDs are resolved from existing person/seed records: the older schema cannot prove capture-time identity for all historic observations. The report discloses this limitation.

## Running

Use the installed Python environment containing pinned Instagrapi 3.0.14 for the mobile runner. The collector loads an existing private saved session; it never logs in or changes device identity automatically.

1. `ops/edge_benchmark.py create --live-db LIVE --bench-db ISOLATED --viewer-id ID`
2. Deploy extension 3.9.22 and the matching server. Verify both transports use the same viewer ID.
3. `ops/edge_benchmark.py activate --live-db LIVE --bench-db ISOLATED --lane-id LANE`
4. `ops/edge_mobile_run.py --settings SESSION --lane LANE --viewer-id ID --max-requests 4 --max-seconds 600`
5. Inspect `ops/edge_benchmark.py report --bench-db ISOLATED`. Advance only after all warmups succeed: `ops/edge_benchmark.py advance --live-db LIVE`.
6. Run the bounded measured cohort, then `ops/edge_benchmark.py deactivate --live-db LIVE` once permits have drained. This never clears warning holds or cooldowns.

Activation refuses active/queued permits. It does not clear existing pacing or protection. The shared benchmark policy adds 12 seconds between requests and a 180-second break after 40 attempts. Existing account, workspace, Chrome sliding-window and initial wait restrictions remain effective across routes.

## Accounting and recovery

Each permitted request has a durable UUID before dispatch. Chrome makes one manual-redirect fetch; mobile uses one instrumented Session.send with library retries, hooks and redirects disabled. Convenience pagination and route fallback are not used. Unknown transport outcomes are reported as uncertain, not guessed to be successful requests, and are never automatically replayed.

Results are persisted locally before upload. Replayed acknowledgements cannot duplicate edges. Cursors are per task/viewer/route/page-size arm. Repeated or contradictory cursors, identity changes, login/challenge/soft-block/429 responses, and uncertain transports stop the cohort. Normal collection remains parked until explicit disarm; warning holds remain in place.

## Reading results

Reports include actual HTTP calls, returned rows, independently new directed edges, duplicates, successful calls, transport p50/p95, pagination outcomes, errors and classified waits. Arm wall time includes the waiting before its request; the report also supplies the full measured-cohort denominator. Warmups are excluded from arm throughput. Allocated rates from this short experiment are observations, not sustainable-capacity claims. Repeat independent windows with the same controls before naming a winner.

## Optional ceiling and follower tests

Use `--preset chrome-large-following` for a separate 200/300/500/1500 Chrome comparison. Its excluded warmup progresses from 200 upward; measured arm order still rotates. Default: two public targets, 20 total requests, 15 minutes.

Use `--preset followers-feasibility` for a separate follower comparison of Chrome and mobile REST at 50/100/200 plus native private GraphQL. Default: two public targets, 35 total requests, 15 minutes. An explicit `--target-ids ID,ID` fixes the public target corpus without substitutions. These small feasibility runs do not establish sustained throughput.

Existing plan files remain immutable. Each plan stores its own arms, direction, bounds and target set. New runs on the same viewer preserve the previous benchmark's pacing deadline and break counter. A local daily budget can stop a test even without an Instagram warning; reports must distinguish this from provider restrictions.
