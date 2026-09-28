# Scraping throughput changes

## Enabled behavior

Collection now yields after four saved list pages: an eligible profile gets a
turn, and another eligible list can progress. Cursors, account ownership,
request spacing and warning holds are retained. A large list can still take
days; it no longer gets exclusive precedence over other ready work.

## Optional experiments (off by default)

- `auto_discover_policy=bounded_completion` prioritizes smaller following lists
  within the same evidence group. Every fourth admission uses the original
  ranking. This may finish more lists while finding fewer new people in a short
  window. Default: `saved_fit`.
- `follower_redirect_recovery=route` scopes a verified HTTP 200 follower-list
  homepage redirect to a workspace-wide follower endpoint hold. Existing shared
  holds are preserved. Rate limits, security warnings, ambiguous failures and
  explicit retry deadlines keep their shared handling. Default: shared policy.
- `ops/assign_page_experiment.py --direction following --targets a,b,c
  --viewer-ig-id ID --db PATH` previews fresh 50/100/200-row arms. Applying needs
  `--apply`, a known alternate running extension 3.9.21, known public targets
  and no previous collection history for those targets. All arms stay pinned
  to that viewer; no cross-account cursor handoff. Default following size: 50.
  Three different targets are a feasibility check, not a matched causal test.

`ops/collection_benchmark.py` reports the recorded trial, returned rows, newly
saved links, unique people and errors. Requested size is not achieved yield.
`ops/stop_page_experiment.py --db PATH --apply` stops the latest trial and retains
saved partial results. These tools do not waive normal request permits.

## Offline prototypes

`extension/lib/native-list-adapter.js` normalizes saved response fixtures for
comparison; it is not wired as a replacement network collector.
`extension/experiments/profile-page-budget.mjs` demonstrates blocking images,
media and fonts in an owned temporary profile tab. It is not wired into the
extension and adds no production permissions. Synthetic byte savings do not
establish actual Instagram bandwidth, memory savings or profiles per hour.

## Evidence limits

An offline replay of 50 recorded following targets completed 13 rather than 7
lists at a 200-request allowance, but found 104 rather than 143 new people.
At 400 requests it completed 20 rather than 11 lists and found 244 rather than
189 new people. Current saved counts influenced the ordering; this is not a
prospective daily forecast. No sustained 40,000-profiles-per-hour result has
been demonstrated.

Following page trials stop each arm after four saved pages. An Instagram warning
or reported transport failure stops the cohort. Saved data remains partial.
