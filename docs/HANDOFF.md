# Fortunate Leads handoff

Updated 1 October 2026. This file describes the current implementation. Earlier iterations and their validation remain in Git history.

## Current product

The connection map keeps Michael at the centre of an irregular circular arrangement of photos. All four views keep the same people, positions and camera. Size is independently selectable: Followers, Fit, Connections or Equal. The owner is 48 pixels at the default zoom. Ordinary photos have no bright outline; solid rings mean Michael follows them, dashed rings mean they follow Michael, and both mean mutual. Unknown evidence gets no ring. A follow or shared source does not establish friendship or an introduction.

Zoom magnifies the same people. Explicit pages offer 250, 500 or 1,000 people, default 500. Search and filters reach other people without zoom gating. The selected card provides status and existing manual tags directly. Add tag opens a small editor; connection facts sit under Connections. Esc closes the editor without discarding a draft. Photo loading is limited to six concurrent requests with a 1,024-entry cache. A full saved-data walk verified all 111,698 non-owner people across 112 pages, without duplicates or omissions. Median HTTP page time was 19.13 ms, maximum 70.76 ms. See [measurements and limits](MAP_VIEW_PERFORMANCE.md).

The UI uses neutral greys. Get started is removed; setup belongs in Accounts. Collection has one Start/Stop control and a compact summary of finished, queued, limited and review-needed lists. ETA covers only the known current queue, uses distinct newly saved entries, and initially measures the pace rather than inventing a finish time. Optional suggestions and AI setup are collapsed. Local AI startup belongs in Settings; Accounts stays focused on collection. Review shows two connection lines with the rest expandable and puts supporting evidence behind a disclosure. Lead rows retain recorded connection direction at laptop widths; opening details temporarily makes room by closing filters and restores them afterward. Qualification remains separately controllable; rules-only collection does not activate AI scoring.

The Instagram Inbox shortcut opens the verified main account through the extension. It does not import or send messages. Optional message-history import retains metadata only.

## Local operation

The canonical checkout is `/Users/michael/Projects/Fortunate-Leads/app`, with the real workspace on port 8777. Port 8880 is an isolated saved-data preview with no collectors; changes there do not update live leads. Always open 8777 for normal use and feedback on live collection.

Private `data/browser-startup.json` binds the three saved Chrome profiles to their verified lanes and Instagram identities. Server startup and explicit collection Resume reopen those profiles in the background. Stopped collection and paused accounts remain stopped. No repeated watchdog reopens Chrome after a manual close. See [configuration](../ops/README.md).

Extension 3.9.30 recognizes Instagram scraping warnings and stops every collector. Warning holds survive healthy heartbeats, ordinary Resume and restarts. Multiple affected accounts require separate review; switching identities does not qualify as recovery. Explicit acknowledgment requires the matching account and a fresh updated-extension heartbeat, then leaves collection paused. See [warning recovery](INSTAGRAM_WARNING_RECOVERY.md). Ordinary login/security pages and connection errors remain distinct. A retained lease does not prove collection is running.

Michael authorized tested live updates and extension reloads. After Instagram warned @dihfluencer, collection was stopped; his latest instruction is to leave it paused and finish other work while he sleeps. Do not resume or acknowledge the warning on his behalf. Other sessions must obtain equivalent authorization before a live restart. Preserve all cursors, observations, cooldowns, account limits and AI-off settings. Do not start another collector or requeue existing lists to recover progress.

## Validation and next work

Validation for this update is recorded in the release evidence. The latest backend suite completed 1,192 tests with two existing skips; all 509 frontend/extension tests and all 82 checks in the eight-hour collection simulation passed. Warning tests cover multiple accounts, identity changes, stale acknowledgments and confirmed stopped state. The indexed pagination correction also has bounded-work checks. Actual saved-data browser checks cover both 1280 and 1440 laptop widths, inline tag/status writes with restoration, exact stable membership during zoom, all 1,001 photos loaded at maximum density, and no page overflow. The complete pagination oracle is saved in `benchmarks/map-cohort-real-20261001.json`.

The earlier connection-error diagnosis for @dihfluencer was incomplete: an explicit Instagram scraping-warning URL was recorded at 00:37 Amsterdam on 1 October. All collection was then stopped with no outstanding request. Leave it stopped until Michael reviews the affected account. Do not clear cookies, dismiss warnings automatically or switch accounts to continue. AI remains paused in Rules mode. This rollout sets daily defaults to the existing main-account allowance of 200 list requests and 100 bio requests per account; these are ceilings, not a guarantee against restriction. Confirm live settings and warning state before operating.

The exact current map still needs a physical ten-million-person run, broad rare-filter measurements and a long collection soak at that size. Earlier ten-million measurements belong to the spatial API, not this new page renderer. A cold Chrome launch after logout/reboot has not been physically tested; startup behavior is covered by tests and Chromium's documented startup path. Do not present those as completed checks.

For ongoing work, start with [PRODUCT](PRODUCT.md), [CONTRACT](CONTRACT.md), [SETUP](SETUP.md), [LEAD_SCALE](LEAD_SCALE.md) and [OPEN_ITEMS](OPEN_ITEMS.md). Keep the existing compact workflow and use actual saved data for visual review. Do not add dashboards or alternate layouts without user feedback.
