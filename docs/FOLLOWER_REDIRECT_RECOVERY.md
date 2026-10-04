# Follower-list redirect recovery

Built for review on 4 October 2026. Not deployed; collection and its speed monitor remain paused.

## Recorded incident

Turtles received redirected HTTP 200 homepage HTML while reading page 101 of
`lukas.ntambala` followers at 20:17:58 UTC on 3 October. A normal-control recovery
retried the same saved cursor at 22:22:05 UTC and received the same response.
The server stayed running; no new scraping warning, 429 or login challenge was
recorded in these attempts. This does not prove the account was unrestricted.

The list retained 1,998 saved entries against a reported total of 4,253.
The response cannot establish whether the cause is the target, cursor or account
endpoint. It occurred historically before the polling speed adjustment too;
the incident does not establish a causal link to that adjustment.

## Recovery boundary

The default policy still stops collection for an ambiguous homepage redirect.
The existing explicit `follower_redirect_recovery=route` policy can preserve
an already authorized, identity-bound selection only when recent saved following
pages establish that the other direction worked for that identity. Enabled
state and a heartbeat are insufficient. It rests the follower route and failed
target without deleting saved cursors, entries or request counters.

New accepted page events record their verified viewer identity. Recovery uses
that saved identity rather than a job's current owner, which can change later.
Older events with no verified viewer identity cannot authorize continuation.

New warnings, login/security checks, identity uncertainty, stale leases and
protective waits retain the full stop. Main-account protection, pacing and
allowances are unchanged. No automatic profile lookups or exploratory requests
are added to diagnose a redirect. The excluded account's old warning remains.

Repeated follower redirects within the existing six-hour evidence history now
receive the longer four-hour route wait. A two-hour rest plus a few minutes of
polling or recovery overhead no longer resets the repetition detector.

## Activation

This patch does not enable the route policy, clear the current review hold,
resume collection, restart the live server or restart the speed monitor.
The actual turtles account state still needs review before live recovery.

## Verification

The isolation-loss and delayed-retry regressions failed before the correction.
The final full backend run completed 1,438 tests with three optional skips and
no failures. The final focused backend run passed 88 tests. Independent safety review passed
51 tests; all 644 interface and extension checks passed. Tests cover immutable
viewer capture, legacy migration, changed identity, reassigned jobs, absent or
stale health evidence, non-200 transport, old and new warnings, cursor retention
and active identity waits. These are isolated tests, not a successful live soak.

A pre-existing limitation remains outside this change: the default error handler
normalizes a malformed shared cooldown to a timed wait. The route exception
declines such a value and the normal isolation fallback pauses collection; this
patch does not claim exact preservation of malformed shared cooldown data.
