# Fortunate Leads handoff

Updated 1 October 2026. This file describes the current implementation. Earlier iterations and their validation remain in Git history.

## Current product

The connection map keeps Michael at the centre of an irregular circular arrangement of photos. All four views keep the same people, positions and camera. Size is independently selectable: Followers, Fit, Connections or Equal. The owner is 48 pixels at the default zoom. Ordinary photos have no bright outline; solid rings mean Michael follows them, dashed rings mean they follow Michael, and both mean mutual. Unknown evidence gets no ring. A follow or shared source does not establish friendship or an introduction.

Zoom magnifies the same people. Explicit pages offer 250, 500 or 1,000 people, default 500. Search and filters reach other people without zoom gating. The selected card provides status and manual tags directly. Photo loading is limited to six concurrent requests with a 1,024-entry cache. A full saved-data walk verified all 111,698 non-owner people across 112 pages, without duplicates or omissions. Median HTTP page time was 19.13 ms, maximum 70.76 ms. See [measurements and limits](MAP_VIEW_PERFORMANCE.md).

The UI uses neutral greys. Get started is removed; setup belongs in Accounts. Collection has one Start/Stop control and a compact summary of finished, queued, limited and review-needed lists. ETA covers only the known current queue, uses distinct newly saved entries, and initially measures the pace rather than inventing a finish time. Optional suggestions and AI setup are collapsed. Lead rows retain recorded connection direction at laptop widths; opening details temporarily makes room by closing filters and restores them afterward. Qualification remains separately controllable; rules-only collection does not activate AI scoring.

The Instagram Inbox shortcut opens the verified main account through the extension. It does not import or send messages. Optional message-history import retains metadata only.

## Local operation

The canonical checkout is `/Users/michael/Projects/Fortunate-Leads/app`, with the real workspace on port 8777. Port 8880 is an isolated saved-data preview with no collectors; changes there do not update live leads. Always open 8777 for normal use and feedback on live collection.

Private `data/browser-startup.json` binds the three saved Chrome profiles to their verified lanes and Instagram identities. Server startup and explicit collection Resume reopen those profiles in the background. Stopped collection and paused accounts remain stopped. No repeated watchdog reopens Chrome after a manual close. See [configuration](../ops/README.md).

Extension 3.9.29 reports login/security pages as account attention and unresolved network failures as connection trouble. A retained lease or queue poll cannot make a blocked account appear to be scraping. A healthy heartbeat clears that observed tab hold. Existing loaded extensions' exact login/security messages are also supported. Completing Instagram's human verification still requires the account owner.

Michael authorized applying these changes to the live service, reloading extensions and resuming the saved queue. Other sessions must obtain equivalent authorization before a live restart. Preserve all cursors, observations, cooldowns, account limits and AI-off settings. Do not start another collector or requeue existing lists to recover progress.

## Validation and next work

Validation for this update is recorded in the release evidence. The full backend suite passed 1,187 tests with two existing skips; all 503 frontend/extension tests and all 82 checks in the eight-hour collection simulation passed. A subsequent indexed pagination correction has separate focused checks. Actual saved-data browser checks cover both 1280 and 1440 laptop widths, inline tag/status writes with restoration, exact stable membership during zoom, all 1,001 photos loaded at maximum density, and no page overflow. The complete pagination oracle is saved in `benchmarks/map-cohort-real-20261001.json`.

The two working accounts were verified saving new list pages before rollout. Dihfluencer had browser/network trouble and no recent successful results; its retained job did not prove it was working. Do not clear account cookies or bypass Instagram checks. AI remained paused. The current implementation fixes that misleading running label without changing retry pacing, limits or saved progress.

The exact current map still needs a physical ten-million-person run, broad rare-filter measurements and a long collection soak at that size. Earlier ten-million measurements belong to the spatial API, not this new page renderer. A cold Chrome launch after logout/reboot has not been physically tested; startup behavior is covered by tests and Chromium's documented startup path. Do not present those as completed checks.

For ongoing work, start with [PRODUCT](PRODUCT.md), [CONTRACT](CONTRACT.md), [SETUP](SETUP.md), [LEAD_SCALE](LEAD_SCALE.md) and [OPEN_ITEMS](OPEN_ITEMS.md). Keep the existing compact workflow and use actual saved data for visual review. Do not add dashboards or alternate layouts without user feedback.
