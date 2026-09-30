# Open items (2026-09-30)

Written from a clean checkout of `live-main` (= `origin/main` at 87987b5), docs/HANDOFF.md, docs/SETUP.md, the 2026-09-28 Codex sessions and the open PRs. Prioritized; each item says what is verified and what is not.

## P0 - blocks trusting the simulator
1. **`node tests/e2e/driver.mjs` fails: 30 passed, 15 failed** (HANDOFF still claims 82 to 83 passing). Every list, followers and following, ends in `error`: the extension's profile-page lookup reports `no_profile_data` and never reaches the list API (71 page loads, 0 list calls). Bisected on first-parent merges: last green is 5f2a76f (#17), first red is PR #19 (`fix/scraper-cooldown-speed`, 24d2c7f). That PR changed core.js (soft_block to other, cooldown cap, strike pause), `retry_after` on the error post, and server-side retry delays. Either the harness expects the old behaviour or the change broke the page lookup; not diagnosed further. The unit suites do not cover it, so nobody noticed for ~30 merges.

## P1 - collection
2. **Followers are capped by Instagram.** Benchmarks on 2026-09-28 returned 25 (mobile REST/GraphQL) and 12 to 50 (native window) followers per target, all flagged `should_limit_list_of_followers`. Followers collection yield is therefore small; following lists are the productive route. The follower-route comparison (Chrome REST, web search, modal) was never completed; it stopped on an unconfirmed request outcome, correctly, per protocol.
3. **PR #73** (codex/chrome-follower-cap-classification): follower cap reported as `target_cap` and permit attempts persisted before requesting grants. Open, CI was pending. Not merged; needs Michael's merge decision. Its `benchmark.js` change is identical to the uncommitted 3.9.26 edit that was sitting in the live checkout (dropped as redundant; patch kept out-of-tree).
4. **Real follower capture** (HANDOFF "Earlier Mac checklist" 4): the e2e fake serves the `follow_list_page` shape; a real capture of a working follower page has never been verified.
5. **Soak** (checklist 3, `ops/soak.md`): 72 requests / 11 min and 300 bios/day are conservative guesses; tune only after a clean multi-account soak. No live soak has been run since the 2026-09-26 update. Live app still runs old uncommitted code paths per project notes; do not assume it equals `live-main`.

## P2 - setup and hygiene
6. Reload the extension in every Chrome profile and confirm the version (3.9.25 on main; PR #73 bumps to 3.9.27). An old build posting `other ... (HTTP 0)` was seen on 2026-09-24.
7. Add OpenRouter keys in Settings and confirm `/api/llm` shows Ready (checklist 5). Laya sidecar venv is optional (SETUP.md section 7).
8. **PR #14 `feat/grok-bulk`** (Grok 4.7 bulk qualification via Hermes): open since 2026-09-27, 10 unmerged commits, stale against main (conflicts in 11 files, 282-file diff from a 1000+-commit-old base). Needs a decision: rebase or close.
9. `fix/deep-bug-audit-2026-09-27` adds only `.github/workflows/bug-audit.yml`; `integrity-audit.yml` on main already runs the same suites. Likely redundant.
10. `feat/scale-scraping` (closed PR #61) is Tor/per-profile egress PAC code. Project rule: no proxy/Tor/block-evasion code. Do not merge; consider deleting the remote branch (Michael's call).
11. Stale local worktrees (Codex/2026-09-26 and 2026-09-27 folders, /private/tmp/fortunate-profile-planner-20260927, archive/safety-hold-pr73) can be pruned once their PRs are settled.

## Verified state of tests on live-main
Server 1071 (3 skipped), sidecar 17, extension 176, web + tests/*.mjs 239, tests/*.cjs 13, e2e driver FAIL (item 1).
