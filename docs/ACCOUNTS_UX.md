# Accounts page: workflow, states and API

Two things live on the Accounts page. Instagram accounts are the Chrome profiles that collect. Profiles to collect (seeds) are the Instagram profiles whose follower and following lists are read. Nothing here changes pacing, budgets, the main-account exclusion, warning holds, identity-bound resume or the 3x429 circuit.

## Seed state machine (per list: followers, following)
queued -> running (a request is out) -> done | partial (capped, short or stalled) | private | error. Operator overlay `lists.hold`: `operator` (Pause), `removed` (seed removed), `direction_off` (list turned off). A hold cancels the queued job; a request already on its way finishes and the pause lands between pages (`ext_list_page`). Resume clears the hold and queues from the saved cursor. `repair_lists`, `queue_list` and `pick_job` all skip held lists and removed seeds. Seed status shown: collecting, pausing, waiting, paused, partly collected, capped by Instagram, private, couldn't read, needs review, done, removed. `seeds.directions` (null = both, as before) is what the repair job respects; before this change it re-added the unchosen list within 15 minutes. `seeds.removed_at` is a tombstone: saved people and edges stay; adding the handle again restores it. Renames are remembered in `seed_aliases`, so a pasted old name finds the seed. Instagram IDs are never resolved on add: cached `people.ig_id` is used when known, otherwise the first read fills it.

## Seed API
- `GET /api/seeds?status=&q=&sort=&limit=` list, counts, queue sentence, ETag (304 when unchanged)
- `POST /api/seeds/check {text, directions}` verdict per token: new, extend, restore, exists, duplicate, invalid (with reason), warnings private/yours, renamed_from. Writes nothing.
- `POST /api/seeds/batch {text, directions, request_id}` atomic, one transaction, one SAVEPOINT per item; result per handle: added, extended, restored, exists, invalid, duplicate, failed. Same request_id returns the first answer.
- `POST /api/seeds/act {action, handles, directions?}` pause, resume, remove, restore, next (queue order only), directions. "Move" is implemented as Do next and Lists (turn followers/following on or off).
- `POST /api/seeds/undo {request_id}` deletes what the add created if nothing was fetched, otherwise pauses it.
`POST /api/scraper/seeds` and `/api/start` are unchanged for old callers.

## Account cards
First match wins: disconnected, connecting, personal, review (Instagram warning), security check, needs login, offline, connection trouble, on hold (not in the post-warning selection), paused, waiting (shared pause), ready (collection stopped), daily limit, waiting (this account), collecting, ready. Each card has one primary action: Pause, Resume, Open Chrome profile, Reconnect, I've reviewed it (same rules as the existing acknowledgement), Open Instagram (main). Details: reads, daily limits (whole numbers, empty = default), name, recent activity, Disconnect. Pace slot: `details.pace` is null and `[data-slot="pace"]` is hidden. `work/oct4-speed-knob/docs/PACE_CONTROL.md` did not exist when this was built; to hook up, fill `details.pace` in `account_view.card` and render it in `detailsCardHTML`.
- `GET /api/accounts/live[?setup=1]` cards, summary, workspace facts, ETag.
- `POST /api/accounts/<lane>/connect|disconnect|reconnect|forget`. Disconnect is durable (`accounts.disconnected_at`); the old Remove was undone by the extension's next check-in. Forget is refused while the profile still checks in. Connect can only target profiles allowed by the post-warning selection.

## Live updates
The page polls both endpoints every 4 s (2 s during add-account), only while visible; an unchanged answer is a 304. Rows and cards are keyed and redrawn only when their markup changed; edits in progress are never overwritten. Countdowns tick from absolute times.
