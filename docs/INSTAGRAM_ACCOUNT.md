# Instagram connection and Inbox

Accounts shows the main account observed by the Chrome extension. Connection and collection are
separate: an account can be connected while collection is paused or waiting on a safety hold.
`GET /api/setup` returns the main account's observed state in `instagram`. Offline accounts do not
count as connected. Sign-in and security checks stay in Instagram; Fortunate never asks for passwords.

The extension accepts `OPEN_INSTAGRAM` only from the local workspace on port 8777. The action opens
Instagram in that extension's Chrome profile. For Inbox, it checks the current `ds_user_id` against the
expected main account and refuses a different account, a signed-out profile, or a different saved lane.
It focuses an existing Inbox tab or opens a separate one, leaving the collector's tab in place.
The action reads no conversations and sends no messages. The 8880 review preview cannot talk to the
installed production extension and must show a plain link without claiming account verification.

A new Chrome profile still needs the unpacked extension loaded once. Chrome's extension installation,
Instagram login, and security checks require the user. Opening a page is not proof of connection.

## Unfinished benchmark recovery

An enabled raw-edge benchmark reserves collection even when its last request is old. Resuming the
normal queue alone cannot disable it. An expired permit is an unknown outcome, not proof of success.

After the updated backend is activated, an operator can review and stop the old experiment:

1. Check the Chrome profile belonging to the benchmark lane. Confirm it has stopped issuing requests.
   Checking a different main account does not satisfy this step.
2. `POST /api/control` with `{"action":"stop_benchmark","checked_account_tab":true}`.
   The server refuses an unexpired request, invalid timing, or a different account's unresolved request.
3. Inspect `GET /api/control`. Collection remains paused. The disabled benchmark retains its original
   inflight request and an `operator_stop` record with `outcome: abandoned_unconfirmed`. Its matching
   expired permit and prior attention remain in that record. No result is invented or replayed.
4. Resume the existing queue through the collection controls. Resume the chosen account separately if
   individually paused. Existing cooldowns, account holds, daily budgets, main-account protection, and
   request spacing still apply. Do not refresh lists or reset cursors to continue saved work.

The old deployed server has no supported benchmark stop endpoint or general setting-write endpoint.
Recovery requires activating the reviewed backend. Do not edit the live database to mimic this action.

## What an official message integration would need

The Inbox shortcut opens Instagram itself. Messages are not imported into Fortunate.

Meta's [Instagram Conversations API](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/conversations-api)
requires an Instagram professional account, a Meta app and login flow, an Instagram User access token,
and `instagram_business_basic` plus `instagram_business_manage_messages`. Standard Access covers
accounts owned or managed by the app operator and added to its App Dashboard; serving other accounts
requires Advanced Access. A Chrome collection login does not grant those API permissions.

The [Instagram Messaging overview](https://developers.facebook.com/documentation/business-messaging/instagram-messaging/overview)
supports Business and Creator professional accounts. It does not offer a personal account's full inbox.
The [Messaging API](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/messaging-api)
does not support group messages or native inbox folders. Requests inactive for 30 days are excluded;
the Conversations API only provides details for the most recent 20 messages in each conversation.
A future import must disclose those gaps rather than claim a complete archive.

No OAuth connection, access token storage, inbox synchronization, or message sending is implemented.
Opening Instagram remains useful for viewing the actual inbox in the account's existing browser.
Sources checked September 30, 2026.
