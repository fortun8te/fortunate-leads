# Fortunate Leads extension (3.9.18)
Install: chrome://extensions → Developer mode → remove the old "Follower export" → Load unpacked → this folder (same extension id).
Needs a logged-in Instagram session and the server on http://127.0.0.1:8777. It uses an open instagram.com tab; if none is left while there is work it reopens one as a pinned background tab (at most once per 10 min), and it wakes a discarded/frozen Instagram tab by reloading it (at most once per 3 min, never the tab you are looking at). The tab it uses is marked not auto-discardable.
What it does: asks the server for one job at a time (list page or profile read), runs it inside the Instagram tab, posts the result. One request lane; a stored lane marker stops a restarted worker from firing while an earlier request may still be running.
Lists: cursor saved after every page (server + local progress), followers request 25 by default, following 50. A small benchmark may assign 25 or 50 to an untouched follower job for its entire run; returned size is measured rather than assumed. `should_limit_list_of_followers` = list done and marked limited. An empty page that promises more, or does not explicitly confirm an end, cannot complete a list. A terminal empty page that contradicts a count verified in the current run is retried once without an extra cooldown, then recorded as partial coverage.
Bios: opens the normal profile page and captures the bio data Instagram loads for it. This route is chosen before requesting the profile. A warning stops that attempt; it never triggers a second route. Page loading can take longer and may time out if no complete profile data arrives. No live throughput benefit is claimed.
Passive: bios Instagram already loads while you browse are sent too (zero extra requests, once per handle per 6 h).
Pacing: list pages 7–12 s apart, a 90–180 s break every 40–60 pages; profile reads 35–70 s apart. Unknown answers back off 2→5 min, tab/network trouble 30 s→10 min.
Daily budgets are configurable per account; 0 means no daily cap. List and profile reads still follow their pacing clocks and response-based waits.
Limits: per-account clocks still pace lists and bios. A reported rate limit, soft block, login warning or security challenge also creates a persisted shared hold of at least 15 minutes across every account; a home-page list redirect creates at least 30 minutes. Longer reported waits are preserved. Home redirects are reported immediately without another privacy-check request. Collection cannot be resumed during an active shared hold. Start local services opens no Instagram profiles and does not resume collection. The reporting and control checks cannot cancel requests already in flight; a warning can also arrive between a control check and the browser request. These waits are precautions, not a guaranteed safe rate, and there is no preventive shared IP request budget.
Security check or logged out → that account remains paused with "!" until resumed. Both collection stages are also paused across the workspace until you explicitly resume them after resolving the warning. Expiry of the shared deadline alone does not resume collection; resuming an account does not clear that deadline.
Results wait in a local outbox until the server accepts them (survives restarts). Popup: state, last hour, per-bucket status, last error with the raw Instagram answer, "Copy debug" (state + samples + recent events as JSON).
Tests: node --test extension/test/*.test.mjs
Benchmark: `python3 ops/collection_benchmark.py --db data/leads.sqlite --hours 24` reports new saved links separately from returned page rows, current complete/partial coverage, and errors by account and request type. Account error history starts with this version; earlier errors cannot be reconstructed. `ops/assign_page_experiment.py` dry-runs a matched 25/50 comparison and creates only new follower jobs after `--apply`, with no pacing changes.
In-page widget (widget.js): bottom-right on instagram.com, collapses to a round pill (remembered), shows online dot, current step, today's counts, Pause/Resume and Open workspace. Closed Shadow DOM, data from the service worker only.

## Upgrade to 3.9.18

Keep both collection stages paused while upgrading. Update the folder each Chrome profile actually loaded, then open `chrome://extensions` in each profile and click Reload on Fortunate Leads. Verify version 3.9.18 on each extension card and in the Accounts page heartbeat before resuming anything. Starting local services does not reload browser extensions.

The worker has an explicit `selfUpdate()` check: it reads the on-disk manifest during a heartbeat and calls `chrome.runtime.reload()` when the version differs. This can reload an active worker automatically after the loaded folder changes; a sleeping worker, a different loaded folder, or a failed check may prevent that. Saving files alone is not verification. Reload each profile explicitly and verify its reported version. Reloading the extension does not clear saved server pauses or the shared hold.

## Shared request coordination

Each collector must obtain a server permit before an API fetch, profile lookup, tab opening, or reload. Permits are queued fairly across accounts, reserve one request at a time, and reuse the existing two-second minimum spacing across those collectors. Existing per-account windows remain unchanged; no shared hourly quota is invented. If a request has not confirmed completion after 90 seconds, collection pauses across accounts. Check the account tab, then explicitly resume collection; local processing remains available. A confirmed completion releases its permit normally. Valid jobs are renewed while waiting; stale/reassigned jobs are rejected before a request.

This coordinates collector actions, not every background request a browser page may make. A loading page can outlive its reservation, and requests already started cannot always be cancelled. Separate server-side Meta workers obey the shared safety hold but are not part of the extension permit queue. These controls reduce bursts; they do not guarantee account or IP safety or make unauthorized collection permitted.

Start local services now starts only the server and local helper. It neither opens Instagram profiles nor resumes collection. Connect an account explicitly from Accounts when needed, then use the collection controls deliberately. Older extensions receive an upgrade notice and cannot obtain new work until reloaded to 3.9.17.

### Passive native-list samples in 3.9.19

Normal Instagram fetch/XHR responses can leave up to30 sanitized local samples in
`chrome.storage.local.nativeListDiagnostics`. They identify list transport, public
GraphQL operation, row counts and pagination flags. Only `doc_id` and
`fb_api_req_friendly_name` may be read from an already-materialized GraphQL POST
body. No request streams, variables, cookies, headers, tokens or cursor values
are saved. These samples make no requests and are not list coverage or auto-imports.
Complete biographies already present in native list responses and bounded late
JSON scripts use the existing guarded passive profile path. Reload the extension
and an authorized tab when deploying; this change does not itself reload tabs.
