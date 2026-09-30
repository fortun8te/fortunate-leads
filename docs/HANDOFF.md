# Fortunate Leads handoff

Updated 30 September 2026. This file describes the current implementation. Earlier iterations and their validation remain in Git history.

## Current product

The connection map places Michael at the centre of one circular network. All four views share the same person coordinates. Size is independently selectable: Followers, Fit, Connections or Equal. The owner is 48 pixels, the outer border is removed, and portraits remain bounded and progressively loaded. Search, direction filters, profile selection and comparison use saved evidence. Missing follow evidence stays unknown. A follow or shared source does not establish friendship or an introduction.

The dense overview showed 248 photos at 1440 by 1000 and 160 at 1280 by 800 on a consistent copy of 111,699 saved people. Group counts preserve every matching person. Current all-mode geometry was tested at one million people; the ten-million benchmark belongs to an earlier layout. See [measurements and limits](MAP_VIEW_PERFORMANCE.md).

The UI uses neutral greys. Get started is removed; missing setup belongs in Accounts. Collection has one Stop/Continue action, saved progress and the current waiting reason. Optional suggestions are collapsed. Lead rows show the actual direction of recorded connections. Qualification remains separately controllable under Activity. Rules-only collection does not activate AI scoring.

The Instagram Inbox shortcut opens the verified main account through the extension. It does not import or send messages. Optional message-history import retains metadata only.

## Local operation

The canonical checkout is `/Users/michael/Projects/Fortunate-Leads/app`, with the real workspace on port 8777. Port 8880 is an isolated saved-data preview with no collectors; changes there do not update live leads. Always open 8777 for normal use and feedback on live collection.

Private `data/browser-startup.json` binds the three saved Chrome profiles to their verified lanes and Instagram identities. Server startup and explicit collection Resume reopen those profiles in the background. Stopped collection and paused accounts remain stopped. No repeated watchdog reopens Chrome after a manual close. See [configuration](../ops/README.md).

Extension 3.9.28 reports login and security pages as account attention. A retained lease or queue poll cannot make a blocked account appear to be scraping. A healthy heartbeat clears that observed tab hold. Existing loaded extensions' exact login/security messages are also supported. Completing Instagram's human verification still requires the account owner.

Michael authorized applying these changes to the live service, reloading extensions and resuming the saved queue. Other sessions must obtain equivalent authorization before a live restart. Preserve all cursors, observations, cooldowns, account limits and AI-off settings. Do not start another collector or requeue existing lists to recover progress.

## Validation and next work

The final backend suite passed 1,173 tests with two existing skips; all 482 frontend and extension checks passed. The collection simulation passed all 82 checks across eight simulated hours, including outages, worker restarts, login holds and cooldowns. Focused checks cover spatial count conservation, retained top-ranked profiles, saved follower counts, drag cancellation, background profile startup and security-page recovery.

The update was applied to the live service and all three extensions reported version 3.9.28. Background startup was configured and invoked for all three verified profiles. The two healthy accounts resumed from saved progress. At delivery, @dihfluencer remained on Instagram's human-verification page and was correctly marked as needing attention. AI stayed off. Live browser review showed 260 bubbles and 259 loaded portraits for 114,726 represented people; these counts change as collection proceeds.

The exact current photo overview still needs a physical ten-million-person run, broad rare-filter measurements and a long collection soak at that size. A completely cold Chrome launch after logout/reboot has not been physically tested; startup behavior is covered by tests and Chromium's documented startup path. Do not present those as completed checks.

For ongoing work, start with [PRODUCT](PRODUCT.md), [CONTRACT](CONTRACT.md), [SETUP](SETUP.md), [LEAD_SCALE](LEAD_SCALE.md) and [OPEN_ITEMS](OPEN_ITEMS.md). Keep the existing compact workflow and use actual saved data for visual review. Do not add dashboards or alternate layouts without user feedback.
