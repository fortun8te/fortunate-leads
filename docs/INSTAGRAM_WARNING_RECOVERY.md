# Instagram scraping warnings

An Instagram `/accounts/scraping_warning/` page stops collection across all accounts. The persistent `instagram_scraping_warning` marker survives restarts, healthy heartbeats, and ordinary Pause/Resume. Existing cursors and saved people remain intact. Changing accounts is not recovery.

Review the affected account in its original Chrome profile. Do not clear cookies or dismiss a warning automatically. A fresh extension version 3.9.30 or later must report that no warning tab remains. The control snapshot exposes the marker as `instagram_request_attention`, with `lane` and `review_ready`.

After the owner has reviewed the warning, acknowledge it explicitly with `POST /api/control` and `{ "action": "acknowledge_scraping_warning", "account": "<affected lane>", "reviewed": true }`. The server refuses acknowledgement while collection or a request is active, the warning is visible, the extension is old, or the account heartbeat is stale. This leaves collection paused and preserves all cooldowns. Each affected account requires its own review; acknowledging one warning keeps any other warning in place. A separate Start collecting action is required after all reviews. The acknowledgment timestamp clears only the corresponding extension warning hold, not later warnings.

No request rate or daily cap guarantees that Instagram will permit automated collection. Zero daily budgets are unlimited; request spacing and rolling-window limits are separate guards, not daily limits.

## Explicitly selected accounts

After Michael explicitly authorizes two already healthy accounts to continue, `resume_selected_accounts` accepts exactly two existing `{lane_id, ig_id}` pairs. It preserves the warning and pauses every unselected account. This is an operator action, never automatic account rotation. Selection is refused during an active request, protective wait, stale heartbeat, identity cooldown, account hold, or unlimited daily budget. The allowlist is checked again before each job and request permit. Changing identities cannot transfer that permission.

Any new warning, login/security check, rate limit or cooldown observation revokes the selection and stops collection. Ordinary Stop/Continue preserves the selected pair; Start all cannot clear the warning. Warning acknowledgment requires collection to be stopped and requests drained, and leaves it stopped afterward.
