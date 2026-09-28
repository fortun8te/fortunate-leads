# Optional mobile list trial

This backend is **off by default**. It is an offline-tested benchmark candidate,
not a proven replacement for Chrome. Michael currently has only a Chrome login,
so a live mobile comparison is blocked on a separately established, legitimate
mobile session. This tool never logs in, imports browser cookies, solves a
challenge, or changes device identities.

## Requirements and explicit setup

Use Python 3.10+ with the optional, pinned `instagrapi==3.0.14` dependency in a
separate environment. No dependency is installed by the app. Supply an existing
instagrapi settings file belonging to the chosen alternate, with its saved device
and authorization fields and file permissions `0600`. A browser-cookie-only file
is rejected. The main account cannot be assigned this backend.

Pause collection first. With the migrated app running, the explicit commands are:

```sh
python ops/mobile_collector.py configure --enable --db /path/to/app.sqlite --lane ALT_LANE --viewer-id ALT_NUMERIC_ID
python ops/mobile_collector.py check --lane ALT_LANE --viewer-id ALT_NUMERIC_ID
python ops/mobile_collector.py queue --lane ALT_LANE --viewer-id ALT_NUMERIC_ID --seed NEW_TARGET --direction followers --count 200
# Resume only the intended list collection through the normal app controls.
python ops/mobile_collector.py run --lane ALT_LANE --viewer-id ALT_NUMERIC_ID --settings /private/mobile-settings.json --max-pages 4
```

`configure` changes backend assignment only; it preserves pauses, warnings and
cooldowns. Queueing initially accepts only an untouched target whose numeric
Instagram ID is already saved. Existing Chrome runs, snapshots and cursors are
never adopted. To return the lane to Chrome, pause collection and wait until no
request or leased job is outstanding, then run:

```sh
python ops/mobile_collector.py disable --db /path/to/app.sqlite --lane ALT_LANE --viewer-id ALT_NUMERIC_ID
```

Mobile jobs and cursors remain saved and isolated after disabling. They cannot
be resumed by Chrome. Reassigning the same alternate to mobile can resume them.

## Boundaries and measurements

Each granted shared permit allows one direct HTTP GET on the session prepared by
instagrapi. The retrying private API and multi-page helpers are not used. HTTP
retries, redirects and proxy inheritance are disabled. Every granted attempt
consumes the normal account budget; existing shared spacing, ownership, pauses,
warning holds and ingestion deduplication apply. The CLI allows at most four
pages per invocation. The server persists a minimum 12-second interval for each
Instagram identity across processes and restarts. It stops on
any warning or uncertain outcome, without switching accounts or backends.

A denied permit retains the current job locally. A received result is saved in a
private outbox before local ingestion; replay sends only that saved outcome and
makes no additional Instagram request. A timeout leaves the shared permit
outstanding so the existing expiry attention hold applies. Do not delete these
files to work around a hold. Each lane defaults to its own private outbox directory.
Two existing alternates can each use their own saved mobile settings and CLI
process; both still share the same request permit and holds. Never reuse one
session settings file for two different identities.

Reports include requested page size, actual returned rows, distinct row count,
request duration in milliseconds, job ID and upstream request attempts. An attempt
is the single HTTP call, not proof of successful delivery. `200` is a requested
page size, not a guarantee of 200 rows. No response contents or credentials are
printed.

Fresh authoritative total counts are not collected by this prototype. A terminal
page therefore remains partial/uncertified under the existing completeness
rules; it does not prove the whole list was collected. No throughput or full-list
completion time has been established. Fresh-target isolation also means this
version cannot support a fair same-target Chrome/mobile comparison.

Newest/earliest ordering is **not implemented**. The maintained library exposes
follower order options, but stable ordering for another person's entire list is
unverified. A future delta experiment must record positive observations only;
stopping at the first known ID cannot certify completeness.

## Pinned implementation reference

The reviewed library version is 3.0.14, commit
`13ebe3b73f9a3fc2d495124c94d958d7b438e007`:
[users transport](https://github.com/subzeroid/instagrapi/blob/13ebe3b73f9a3fc2d495124c94d958d7b438e007/instagrapi/mixins/user.py),
[private request behavior](https://github.com/subzeroid/instagrapi/blob/13ebe3b73f9a3fc2d495124c94d958d7b438e007/instagrapi/mixins/private.py),
[session handling](https://github.com/subzeroid/instagrapi/blob/13ebe3b73f9a3fc2d495124c94d958d7b438e007/instagrapi/mixins/auth.py).
