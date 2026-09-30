# Handoff — 30 September 2026


## Current continuation, 30 September 2026

Recovered the unfinished Claude map/API/UI work and integrated it with the already merged cloud fixes. Connections now uses compact audience groups, progressive detail, four viewing modes, filters, search and selected-profile evidence. Collection uses Start, Stop and Continue with saved-progress and waiting states. Legacy map panels, unused rendering assets and redundant refreshes were removed.

The viewport map limits each response to 1,500 people and aggregated counts. A real synthetic 1,000,001-person fixture was built and queried across all four modes; see [measured performance and limits](MAP_VIEW_PERFORMANCE.md). The 10,000,001-person fixture is still in progress. Do not claim that ten-million scale or collection under that load has been validated.

For rollout, use [MAP_VIEW_API.md](MAP_VIEW_API.md) to prepare layouts explicitly on a database copy first. This continuation changed no live database, collection pacing or running service. The unprepared-map fallback remains available. Backups preserve source data; layouts are derived and rebuilt separately.

Visual checks used isolated synthetic previews, including desktop and 390-pixel mobile. The JavaScript suite passed 438 tests; collection simulation passed 82 checks over eight simulated hours. Server results and lead-list timing evidence are recorded in the continuation commit and performance documents. Earlier sections below describe historical functionality and validation.

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
