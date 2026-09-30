# Open items (2026-09-30)

Updated from GitHub merge state, the recovered Claude session, and fresh tests on September 30. Historical verification is identified separately from current checks.

## Simulator regression resolved
1. The profile-page fixture failure was fixed in PR #74. A fresh run on September 30 against `c4fa39b` passed all 82 checks across eight simulated hours, including cooldowns, outbox replay, interrupted requests, and explicit login/security recovery. This does not verify live Instagram capture or a multi-account soak.

## P1 - collection
2. **Followers are capped by Instagram.** Benchmarks on 2026-09-28 returned 25 (mobile REST/GraphQL) and 12 to 50 (native window) followers per target, all flagged `should_limit_list_of_followers`. Followers collection yield is therefore small; following lists are the productive route. The follower-route comparison (Chrome REST, web search, modal) was never completed; it stopped on an unconfirmed request outcome, correctly, per protocol.
3. **PR #73 merged September 30.** Follower caps are reported as `target_cap` and permit attempts are persisted before requesting grants. The controlled real follower-route comparison remains unverified.
4. **Real follower capture** (HANDOFF "Earlier Mac checklist" 4): the e2e fake serves the `follow_list_page` shape; a real capture of a working follower page has never been verified.
5. **Soak** (checklist 3, `ops/soak.md`): 72 requests / 11 min and 300 bios/day are conservative guesses; tune only after a clean multi-account soak. No live soak has been run since the 2026-09-26 update. The live checkout was moved to `Projects/Fortunate-Leads/app` by the Claude cleanup. A running process may still have earlier code loaded; verify its version before any live test.

## P2 - setup and hygiene
6. Reload the extension in every Chrome profile and confirm the version (3.9.27 after PR #73). An old build posting `other ... (HTTP 0)` was seen on 2026-09-24.
7. Add OpenRouter keys in Settings and confirm `/api/llm` shows Ready (checklist 5). Laya sidecar venv is optional (SETUP.md section 7).
8. **PR #14 `feat/grok-bulk`** (Grok 4.7 bulk qualification via Hermes): open since 2026-09-27, 10 unmerged commits, stale against main (conflicts in 11 files, 282-file diff from a 1000+-commit-old base). Needs a decision: rebase or close.
9. `fix/deep-bug-audit-2026-09-27` adds only `.github/workflows/bug-audit.yml`; `integrity-audit.yml` on main already runs the same suites. Likely redundant.
10. `feat/scale-scraping` (closed PR #61) is Tor/per-profile egress PAC code. Project rule: no proxy/Tor/block-evasion code. Do not merge; consider deleting the remote branch (Michael's call).
11. Stale local worktrees (Codex/2026-09-26 and 2026-09-27 folders, /private/tmp/fortunate-profile-planner-20260927, archive/safety-hold-pr73) can be pruned once their PRs are settled.

## Verification before this continuation

On `c4fa39b`: all 438 JavaScript tests and all 82 simulator checks passed. The system Python 3.9 server run completed 1,096 tests with two skips and one error importing the optional mobile library, which requires Python 3.10+. Use a supported isolated environment for the optional mobile tests. Final integrated results are recorded in the continuation handoff.
