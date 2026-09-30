# Handoff — 30 September 2026

## Neutral shared map and simpler local workflow, 30 September 2026

All map modes now share exact positions and the same circular arrangement. The owner is smaller, with a fixed caption; the legend is a compact key. Removed warm colour overrides. Lead rows show the actual direction of saved follow observations, with additional sources in a tooltip. This adds no per-row graph traversal or new page query.

Removed the separate Get started page; missing setup lives in Accounts. The header contains one collection action and a current state/reason. Qualification, model setup and stage detail are under Activity. Accounts has an account-verified Instagram Inbox shortcut when the updated extension is loaded. The preview cannot claim a verified browser identity. Imported message history remains metadata only and optional.

An unfinished benchmark can be reviewed and stopped through a narrow operator action. It preserves the original unknown outcome, all queue/cursor data, cooldowns and holds. The real-data-copy proof retained 14,623 jobs and 135 lists unchanged. It refuses active requests or unrelated attention. Activation and live recovery require explicit user approval; Michael gave that approval during this session. Status of the actual rollout is recorded separately from these source tests.

Validation: 1,159 backend tests passed with two existing skips, plus 49 focused recovery/workflow tests after stricter attention matching; 473 frontend/extension tests passed after the final cleanup. The eight-hour collection simulation passed 82 checks. All four schema-6 views passed the fresh million-person benchmark and a SQL comparison of every person's position/community. See [measurements](MAP_VIEW_PERFORMANCE.md). The exact current layout remains unmeasured at ten million.


## Laptop photo network and workflow revision, 30 September 2026

The map now fills one owner-centred circular disk with cached profile-photo bubbles instead of separate sparse audience circles. Network, Fit, Sources and Status views share bounded loading, exact group counts and follow-direction filters. Missing follow evidence is explicitly unknown. The owner's current saved lists do not establish complete outgoing coverage, so the UI cannot honestly claim every person not followed yet.

Collection entry now offers Followers/Following scope, Add to queue and explicit Start collection above the connected-account list. Stop/Continue preserves saved checkpoints and reports actual request activity. Qualification has independent progress and Stop/Continue controls; waiting work is labelled Waiting rather than Running. Rules-only mode remains explicit. No collection pacing or AI authority was expanded.

Validation: **1,152 backend tests passed, two existing skips; 463 frontend/extension tests passed; 82 collection simulator checks passed across eight simulated hours**. Laptop browser review used a consistent copy of 111,699 saved people and cached photos. All four current layouts were tested at one million synthetic people; the ten-million measurement belongs to the earlier layout and is not a claim for this exact revision. See [current measurements and limits](MAP_VIEW_PERFORMANCE.md).

Port 8880 is an isolated saved-data preview with no background collectors. Its edits stay in the copy. The existing live service on port 8777 remains on its earlier build; pushing main does not restart or deploy that service. Prepare derived layouts and indexes on a consistent copy before rollout.



## Feedback revision, 30 September 2026

The synthetic grid preview was rejected. The revised default map puts Michael in the centre, with four surrounding evidence spheres, a finite zoom range, collision-limited labels and all saved profiles, including people with no collected connections. Desktop and phone checks now use a consistent copy of the actual local database: 111,699 people and 12,033 bios. The real app on port 8777 remains on its earlier checkout; the copied-data preview on port 8880 serves the integration build without background workers.

Restored the earlier tag hierarchy and icons, with visible evidence chips on phones and correct virtualized row heights. Setup and Review now disclose failed refreshes and offer Retry instead of presenting stale results as current.

Ordinary Fit/Score lead pages and unfiltered counts now have exact maintained indexes and summaries. Page hydration uses person-first source membership lookups. Default tag facets use a separately prepared exact projection; filtered requests retain the original query. Existing databases require explicit preparation on a consistent copy first; see [LEAD_SCALE.md](LEAD_SCALE.md).

## Current validation scope

The full backend suite passed 1,135 tests (two existing skips). A final focused
21-test tag suite covered the later readiness guards and interrupted preparation.
A final focused 25-test map/search run covered the full-prefix shortcut and
saved disconnected profiles. All 24 zero-degree profiles in the actual copy were
found with prepared positions; 23 had previously been omitted by search.
The frontend suite passed 445 tests. The collection simulation passed all 82
checks over eight simulated hours, including outages, restart recovery and cooldowns.
These use isolated copied or synthetic data; no live Instagram soak was performed.
Generated million/ten-million benchmark database files were removed after validation
removing 10.08 GiB of generated files; measurement JSON, logs and manifests remain.


The current default owner-centred map was built and queried on a fixture containing 10,000,001 synthetic people. Warm whole-world requests took 5–6 ms, zoom/pan 9–14 ms and filtered zoom 133–143 ms, with slower first requests. The first layout build took 22.6 minutes and peaked at 463 MiB process RSS. Only the default mode is measured at ten million; the other three modes, broader tag density, rare filters and ongoing collection at that size need separate validation. See [measured performance and limits](MAP_VIEW_PERFORMANCE.md).

On the actual 111,699-person saved-data copy, all four current layouts were built and browser checks covered desktop/phone, both themes, tags, lead rows/detail, profile selection, group expansion and return to overview. This preview uses copied data without background collection; changes there do not update the live database. The live service remains on its earlier build on port 8777.

Recovered the unfinished Claude map/API/UI work and integrated it with the already merged cloud fixes. Collection has Start, Stop and Continue with saved-progress and waiting states. Legacy map panels, unused rendering assets and redundant refreshes were removed. No live service, collection pacing or AI authority was changed.

For rollout, prepare layouts and indexes explicitly on a consistent database copy first using [MAP_VIEW_API.md](MAP_VIEW_API.md) and [LEAD_SCALE.md](LEAD_SCALE.md). The original query fallback remains usable on unprepared databases. Backups preserve source data; layouts are derived and rebuilt separately. Earlier sections below describe historical functionality and validation.

Current update, 27 September 2026: bulk selection/editing, saved views and in-app CSV exports have been removed. Individual relationship, tag and note edits remain. Existing saved-view data and workspace backups are preserved. References to those removed features below describe the earlier implementation. See [CONTRACT.md](CONTRACT.md) for current APIs.


## Update, 2026-09-26

- Completed the researched lead workflow gaps: dated follow-ups and shared filters, paginated activity history, filtered/selected CSV exports, profile freshness and refresh in the main detail panel. See [usage](LEAD_WORKFLOWS.md) and [research with sources and rejected alternatives](FEATURE_RESEARCH.md).
- Fixed lost/stale note drafts, saved-view false success, retained rows after completing the last due reminder, and the initial Laya health-cache check on clocks starting near zero.
- Database additions are automatic and additive. History starts with this version. CSV is a current lead snapshot; the existing database backup remains the complete backup.
- Verified on macOS with an isolated sample database. Server: 144 tests, one existing live-model skip. Sidecar: 6 passed. Extension: 42 passed. Web: 12 passed. The 8-hour collection simulator passed all 83 checks, and the 1/2/4-account runs all passed. Live browser checks covered scheduling/completion, saved views, profile-refresh queueing, independent note saves, actual filtered/selected CSV downloads and the 390-pixel layout without horizontal overflow. No browser errors were recorded.

The original collection and account-soak notes below remain relevant. This update did not perform a live Instagram collection soak.

Start with [SETUP.md](SETUP.md) (install, run, troubleshoot), then [CONTRACT.md](CONTRACT.md) (API, schema, pacing) and
[RESEARCH.md](RESEARCH.md) (Instagram endpoints and limits).

## What is in the product now
- **Multi-account lanes** (server/accounts.py, extension 3.5.0): one Chrome profile per Instagram account, per-lane leasing,
  a list sticks to its lane and moves on from its saved cursor when that lane logs out, pauses or cools down. Accounts page
  with the add-account wizard, rename (label), role, main account, per-account budget, pause, remove, alerts.
- **Throughput defaults** (owner's choice): per account per day 3000 list pages and 300 bios (0 = no daily number). Following
  lists before followers. Bios on their own clock fill the list gaps and breaks; any two requests 2–5 s apart; hard cap 72 requests
  per 11 min per account. List gaps (7–12 s, break every 40–60 pages) are unchanged. `bio_min` (25) keeps hopeless handles out of
  planned bio reads; while lists are still collecting, people in 2+ lists already get bios.
- **Qualification**: network first. Every score is 60 % network strength (lists, seeds that follow them, seed yield from marks,
  links to Michael and his good/client accounts) and 40 % the profile read (rules or the LLM fit, role-capped but softer:
  connectors and creators are no longer pushed to zero). Staged: prefilter → rule tags/verdict → optional Laya → LLM for the top.
- **Settings page**: OpenRouter keys (masked, add, test, remove, status, requests today), free-model order, daily limit,
  model calls at once, model threshold, bio floor, main account list share, proxy and Laya health.
- **Web**: redesigned shell (Inter, tokens, sidebar), fit-first rows via `sort=fit`, filter-aware counts, map nodes with `fit`.
- **Speed** (tests/bench.py, synthetic 100k people / 133k edges / 500k tags, median ms before → after):
  leads connected 224 → 169, leads min_lists=2 262 → 157, tags warm 279 → 6 (cached), map warm 260 → 6 (cached),
  map cold 483 → 382, seed_links 206 → 132, soak 27 → 5.5, network_context(200) 8.4 → 4.3, data_rev 9.1 → 5.6.

## Earlier verification (2026-09-24 Linux build)
- `python3 -m unittest discover -s server/tests`, `python3 -m unittest sidecar/test_laya.py`, `node --test extension/test/*.test.mjs`.
- `node tests/e2e/driver.mjs` (8 simulated hours, faults, outage; checks list gaps, request spacing and the 11-min window) and
  `--lanes 1,2,4` (exclusivity, handoff, per-lane cooldowns, window per account).
- `bash -n` and shellcheck on ops/. `ops/doctor.sh` and `ops/backup.sh` against a temp DB (macOS-only checks skip).
- Playwright (Chromium) on every page at 1440 and 390 px against the real server on a seeded temp DB and against `?mock=1`:
  no console errors, no horizontal overflow. Screenshots in `docs/ui/` (mock data).

## Earlier Mac checklist (historical)
1. The server LaunchAgent was installed on the Mac on 2026-09-26. Use `ops/doctor.sh` for current health.
2. Reload the extension in every profile (3.5.0) and add the scout accounts through Accounts → Add account.
3. Soak: a few hours with 2+ accounts; watch Accounts for limits. `ops/soak.md` has the checklist. The 72/11 min window and the
   300 bios/day default are conservative guesses; tune only after a clean soak.
4. Followers lists failing on page 1 (`classify()` → `other`, no `users` field) was open before; the e2e fake serves the
   `search_surface=follow_list_page` shape, a real capture is still needed.
5. Add OpenRouter keys in Settings; check `/api/llm` shows them Ready.
6. Optional: Laya sidecar venv (SETUP.md §7).

## Rules
- Honour Instagram's own limits (429 / "please wait") with backoff; never retry through them; never loosen list pacing.
- UI copy: short product language, no filler.

## 2026-09-24 late
- Control strip (2026-09-24): `/api/control` + web/controls.js on every page + widget stage rows (extension 3.7.0): Collect lists / Read bios / AI scoring pause separately, Stop all.
- Extension 3.6.0: in-page widget on instagram.com (widget.js, closed Shadow DOM, data only via the service worker).
- The list error `other on @x following · page 1 (HTTP 0)` (no reason in brackets) is the pre-3.3 message format: an old
  extension build posted it at 18:07 today (georgebrocklehurst). No account row runs <3.5.1, so it is likely an old copy in
  another Chrome profile or a replayed outbox item. Check chrome://extensions in every profile; 3.5.1+ always sends a reason.
- Statuses are a pipeline: interested, contacted, talking, client, no (Not a fit). Migration in db.init (backup
  data/leads.backup-pre-statuses-*.sqlite). Status, note and manual tags feed the LLM prompt and input_hash.
- Map: hit test against drawn radius on live nodes (the cached quadtree missed new/big nodes); seeds with a person row open
  that person's panel; solid line = follows the seed, dashed = seed follows them.

## 2026-09-26

- Qualification accepts `:free` models only. Rule verdicts remain available if no free model answers. A model
  verdict or high request rate is not evidence that a lead is a qualified buyer.

Earlier verification and Mac follow-ups above describe the 2026-09-24 build. Recheck them against this build
before treating those tests or setup steps as current results.

## Current integration notes

- Extension 3.8.0 sends lease tokens and requested page cursors. A repeated page cursor preserves returned
  people and marks the list partial. Per-run member counts remain separate from historical connection edges.
- AI off is durable, including website summaries. The collection worker can run without AI scoring or an
  active Instagram browser session; logged-in list collection still needs the extension.
- Standalone callback delivery is at least once. A crash after the callback succeeds but before its saved
  acknowledgement can replay that item; integrations should upsert by stable identity.
- A follow is observed direction evidence, not proof that two people know each other. Known relationships
  and manually entered notes remain distinct from automatically collected edges.
