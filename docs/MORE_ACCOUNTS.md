# Collection accounts

Updated 2 October 2026. Additional accounts do not remove Instagram restriction risk. There is no verified account-age rule, warm-up schedule or accounts-to-daily-volume conversion for this workspace.

## Current behavior

The extension uses an existing logged-in Chrome profile. Keep one verified Instagram identity per saved collection profile. The main account stays personal and excluded from automation. A warning stops the workspace and remains held until Michael reviews it.

Adding a profile does not authorize collection on it. The current selective-resume action accepts exactly the two already approved healthy account identities; it does not support an arbitrary number of accounts. Preserve that gate and all pacing, warning, identity and budget checks.

Before changing the account setup, read INSTAGRAM_WARNING_RECOVERY.md, PRODUCT.md and the current HANDOFF.md. Verify account consent, identity binding and live warning state. Never use another identity to bypass a restriction or restore proxy/Tor collection.

## Following-only option

The internal follower_lists database setting defaults to true. Setting the stored JSON boolean to false prevents the scheduler from selecting new follower-list jobs. Queued follower jobs and saved cursors remain available if the setting is restored. Existing active requests are not cancelled by this selector. This option is not exposed in Settings or the accounts API; do not edit the live database to try it.

Following lists produced more usable coverage in the September28 trials. Follower endpoints returned capped results. This does not establish a future collection rate or restriction-free pace. See scraping-throughput-trials.md and OPEN_ITEMS.md for measurement limits.

A clean long multi-account soak and controlled comparison of real follower routes remain unverified.
