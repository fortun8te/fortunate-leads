# Instagram scraping warnings

An Instagram `/accounts/scraping_warning/` page stops collection across all accounts. The persistent `instagram_scraping_warning` marker survives restarts, healthy heartbeats, and ordinary Pause/Resume. Existing cursors and saved people remain intact. Changing accounts is not recovery.

Review the affected account in its original Chrome profile. Do not clear cookies or dismiss a warning automatically. A fresh extension version 3.9.30 or later must report that no warning tab remains. The control snapshot exposes the marker as `instagram_request_attention`, with `lane` and `review_ready`.

After the owner has reviewed the warning, acknowledge it explicitly with `POST /api/control` and `{ "action": "acknowledge_scraping_warning", "account": "<affected lane>", "reviewed": true }`. The server refuses acknowledgement while the warning is visible, the extension is old, or the account heartbeat is stale. This leaves collection paused and preserves all cooldowns. Each affected account requires its own review; acknowledging one warning keeps any other warning in place. A separate Start collecting action is required after all reviews. The acknowledgment timestamp clears only the corresponding extension warning hold, not later warnings.

No request rate or daily cap guarantees that Instagram will permit automated collection. Zero daily budgets are unlimited; request spacing and rolling-window limits are separate guards, not daily limits.
