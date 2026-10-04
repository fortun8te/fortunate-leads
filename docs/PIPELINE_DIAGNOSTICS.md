# Collection diagnostics

`GET /api/pipeline/summary` gives a compact, versioned local report for an agent or
operator. Default window: two hours. Supply `since` as a timezone-aware ISO timestamp,
`lane`, and `ig_id` to inspect one verified collecting identity. Seven days is the
maximum query window. An identity filter intentionally excludes old records whose
viewer identity was not captured. `workspace_new_profiles` remains a workspace
count and must not be attributed to that lane.

Schema `v: 1` separates `requests` (gate lifecycle cause counts), `directions`
(pages, returned rows, saved entries, new links), `failures`, `last_stop`,
`last_failure`, `blockers`, `collection`, and `cadence`. Recent lifecycle events are
limited to twelve. `active_mean_seconds` uses only same-job page intervals below
60 seconds; `observed_pages_per_hour` includes waits across the entire requested
window. Neither figure guarantees future Instagram throughput. Returned rows,
saved entries, new links, and newly seen profiles are different measures.

Reads select at most 5,001 rows per event stream using timestamp/lane indexes.
When the 5,000-row cap is exceeded, `coverage.complete` is false and counts describe
only the latest retained rows within the window. This bounds local query work and
response size; agents must not describe a truncated result as a full window total.
The report's last stop/failure are within this coverage window, while current
blockers are separately read from persisted control state.

Lifecycle writes share the request gate transaction. Codes are
`request_acquired`, `request_released`, `request_release_replayed`,
`request_release_rejected`, `request_expired_unknown`, `request_completed`, and
`request_release_failed`. Only actual server-observed transitions are written;
heartbeat/poll/ordinary wait calls do not produce events. A release acknowledgment
is distinct from a saved page and does not prove that Instagram returned a result.
Completion/release failure phases also remain in the extension's bounded recovery
journal when the server cannot receive a callback.

`pipeline_events` retains approximately 20,000 rows and at most 30 days, pruning
once per 128 inserts (at most 127 extra rows between pruning). Existing page
provenance in `collector_events` is kept independently; pruning diagnostics never
removes saved lists, cursors, connections or provenance. Tokens are represented
by a short SHA-256 reference, never the raw release credential. No HTML, response
samples, bios, private notes, cookies, auth, or cursor contents are accepted by
this log writer or read by this report. Cause codes are validated and unsafe legacy
free-text reasons are reduced to `other` in exported reports.

Control exposes the original scraping warning separately from the primary
`instagram_request_attention`, plus `collection_blockers` with cause, blocking
state and marker. A new blocking security warning remains primary. An older
warning excluded by a valid identity-bound selection cannot mask a current
unconfirmed request stop. These are display changes: no warning or hold is cleared.

## Runtime controls and request review

The extension's collection speed setting changes idle list-work checks while bios
are off. It does not change Instagram request gaps, rolling limits, periodic breaks,
cooldowns or warning stops. It is bound to the selected turtles identity and install;
other accounts retain their defaults. Applying a setting takes effect on the next
check without reloading. Compare measured saved-page cadence, not the preset name,
when deciding whether a setting helped.

An uncertain browser action remains paused across worker restarts. Its durable
completion journal retries only confirmed completion or a known unstarted action.
The operator must check that the correct account's request finished or its tab is
closed, then explicitly review the exact stopped request. Server review leaves
collection stopped. If the extension also has an uncertain local journal, review
that in the matching account's extension after the server confirms the same request.
This never substitutes for reviewing an Instagram warning or security check.

The portrait cache admits a bounded visible set outside the paint loop. Painting
does not enqueue or evict images, so repeated frames do not restart ready portraits.
Loaded portraits retain their reveal timestamp and ease in once. Local photo
delivery reuses a read-only SQLite connection but checks current ownership on every
request; it does not cache authorization or use SQLite immutable mode.

The lifecycle availability boundary is reported separately from page coverage.
A window before instrumentation began, or before retained lifecycle history,
never claims complete lifecycle counts. `request_ack_mean_seconds` pairs acquired
and acknowledged releases within the report; it measures permit lifetime,
including processing/release lag, rather than Instagram response time alone.

An operator may explicitly confirm a stopped account tab using
`POST /api/control` with `action: review_unconfirmed_request`, the marker's `lane`,
`ig_id`, exact `attention_at`, and both `reviewed: true` and
`checked_account_tab: true`. This is a compare-and-swap review, not a resume.
It rejects stale identity, main accounts, security holds, warnings requiring review,
current requests and offline identity evidence; all protective waits, pauses and
saved progress remain. The local `instagram_request_review` marker lets the
extension clear only its matching unknown journal after explicit confirmation.
Older markers without identity can be reviewed only when an existing identity-bound
selection predates that stop, the current lane still matches, and no gate remains.
No automated task should use this operator confirmation to bypass an unknown request.
