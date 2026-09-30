# Collection progress

The profile form has one primary action: **Start collecting** while lists are
stopped, or **Add profiles** while they are already enabled. Adding during an
active run does not change pauses, AI modes or account settings. Stop retains
saved progress.

The compact summary separates verified finished lists, pending lists, partial
lists and lists needing review. A partial or Instagram-limited list never counts
as finished. Bios have a separate pending count.

## Time estimates

`/api/scraper` includes a `collection` summary. Its ETA is a planning range for the
current known queue, never a promise to finish an expanding database:

- Only totals observed in the current list run count as known. Saved profile
  follower counts and older run totals remain estimates and are shown separately.
- `collector_events.saved_entries` records the increase in distinct current-run
  members on a persisted list page. Duplicate returned people contribute zero.
  Old events without this measurement cannot supply a rate.
- The rate uses at most 2,000 events from the last 30 minutes, only current runs
  in the known pending queue and currently available collection lanes. It includes
  elapsed waiting time. At least six pages, five minutes, and a saved page within
  the last five minutes are required.
- The displayed range allows a pace between 65% and 125% of the observed rate.
  This is a deliberately broad planning range, not a statistical confidence
  interval. New limits or changes in the queue can move it.
- Unknown list sizes do not erase a useful estimate for the known subset. That
  estimate explicitly says **Known lists** and shows the unknown-list count.
- Pauses, shared holds, unavailable accounts and insufficient remaining daily
  budgets suppress the estimate in favor of a short waiting reason.
- Auto-discovery is explicitly marked. **Current queue** never includes profiles
  that discovery might add in the future.

The new nullable event column is installed by normal database initialization.
Existing installations initially show a measuring/unknown state until enough
new, measured pages arrive. No backfill guesses are made and no pacing changes
are required.
