# Logging and crash forensics

To see what happened, run `ops/diagnose`. It reads files only, so it works when the server is down.

```sh
ops/diagnose                                # last 2 hours, plain text, about 60-80 lines
ops/diagnose --since 6h --account trashtheturtles
ops/diagnose --json                         # the same facts for a script or an agent
ops/diagnose --tail 40                      # the last 40 events, one per line (filters by --account)
ops/diagnose --since 1d --full              # no per-section line caps
```

The report answers, in this order: is the server up and how did each earlier run end (clean, crashed, killed);
which errors happened, grouped by signature, with count, first and last time and one sample stack; which warnings;
the ten slowest endpoints; database lock waits; for each Instagram account the requests per hour, last success,
current state and the last adverse Instagram response with the 20 events before it; extension heartbeat health per
lane; and, as a fallback, the old unstructured stderr log.

## Common questions

| Question | Look at |
|---|---|
| Did it crash, or was it killed? | `RESTARTS`: `CRASHED` has a dump in `crash/`; `KILLED` means no shutdown record (SIGKILL, memory pressure, power loss). `clean (SIGTERM)` is launchd or a person stopping it. |
| Why is an account not collecting? | `COLLECTION`: its state, `now denied: blocked (hold:challenge)` or `(cooldown_until=...)`, and the `STATE` line. Then `ops/diagnose --account NAME --tail 60`. |
| What happened just before Instagram pushed back? | `LAST INSTAGRAM ADVERSE EVENT`: the 20 events before it for that lane: requests, pages, cursors, the extension's own `ext.ig.req` lines. |
| Is the server hung? | `SERVER` says `ALIVE BUT SILENT` when heartbeats stop. `WARNINGS` shows `watchdog stuck_request`. Run `kill -USR1 <pid>` and read `crash/faulthandler.log`. |
| Why was the UI slow? | `SLOW ENDPOINTS` (p95 per route) and `DB` (lock waits, busy errors); `--tail` for the requests over 500 ms (`http.req` warn). |
| Is the extension alive? | `EXTENSION LANES`: last heartbeat age, gaps over 90 s, worker restarts (`sw_start`). Events from the browser itself appear as `ext.*`. |

## Where the files are

Next to the database (`data/` for the live workspace):

| File | What it is |
|---|---|
| `logs/events.jsonl`, `.1` to `.4` | One JSON event per line. 10 MB each, 5 files, oldest dropped. Mode 0600. |
| `logs/run.json` | Run marker: pid, run id, `running`, `clean` or `crashed`, last-alive time. Rewritten every minute and on exit. |
| `crash/<time>.json` | Written on a fatal error: the failing exception, the last 400 events, every thread's stack, requests in flight, worker-loop counters. The newest 20 are kept. |
| `crash/faulthandler.log` | Native crashes (segfault in SQLite or a C library) and `kill -USR1 <pid>` thread dumps. Moved to `crash/<time>-faulthandler.txt` at the next start. |
| `logs/preview-<port>/` | The same files for a `--no-workers` saved-data preview, so it never overwrites the live run marker. |
| `~/Library/Logs/fortunate-leads.log` | The LaunchAgent's stdout and stderr. Still receives tracebacks and anything printed before logging starts (an import error, for example). Unstructured, no timestamps; `ops/diagnose` groups its tracebacks. |

`data/` is git-ignored and `ops/backup-workspace.py` does not copy it, so logs never travel with a backup.

## Event format

```json
{"ts":"2026-10-04T14:42:02.804Z","level":"warn","component":"ig","event":"adverse","request_id":"5003.6",
 "lane":"3f9a...","account":"trashtheturtles","code":"rate_limit","reason":"please_wait","http_status":429,"kind":"list"}
```

`ts` is UTC. `level` is `debug`, `info`, `warn` or `error` (default floor `info`; `FL_LOG_LEVEL=debug` lowers it).
`request_id` joins every event of one HTTP request (the request line, its DB events, the setting it changed).
`lane` is the extension install, `account` its Instagram handle. `dur_ms` is a duration. Anything else is a flat
field. Events from the extension carry `via:"ext-log"`, `lag_s` (seconds between the event and its arrival) and
`ext_v` (extension version); `ts` is the time it happened in the browser.

| `component.event` | Level | When | Key fields |
|---|---|---|---|
| `proc.start`, `proc.exit`, `proc.signal` | info, warn | Every run: pid, run id, git sha, why it ended (`SIGTERM`, `normal`, `crash`). | `reason`, `up_s` |
| `proc.previous_run` | info, warn | At start: how the previous run ended and its last 12 events. | `outcome` (`first`, `clean`, `crashed`, `killed`, `unknown`), `exit_reason`, `crash_dump`, `last_events` |
| `proc.heartbeat` | info | Every 60 s. A missing heartbeat is the hang signal. | `rss_peak_mb`, `threads`, `fds`, `load1`, `queue`, `dropped`, `http_active`, `http_oldest_s`, `loops`, `http`, `lanes` |
| `proc.slept` | info | The Mac slept (wall clock ran ahead of the monotonic clock). | `slept_s` |
| `proc.crash`, `thread.uncaught` | error | Uncaught exception on the main thread or any other thread. | `exc_type`, `sig`, `stack` |
| `watchdog.stuck_request`, `watchdog.stuck_loop`, `watchdog.stall` | warn | A request or worker step over 30 s (with its stack), or the watchdog thread itself starved. | `route`, `age_s`, `stack` |
| `app.initialize`, `app.listening`, `app.closed` | info | Start-up phases and shutdown. | `dur_ms` |
| `worker.start`, `worker.stop`, `worker.slow_step`, `worker.step_error` | info, warn, error | One line per loop start; a step over 1 s; a step that raised. | `loop`, `dur_ms` |
| `http.req` | info, warn, error | A write, a failure, or any request over 500 ms (warn). 5xx is error; 503 busy is warn. | `method`, `route`, `status`, `dur_ms`, `bytes`, `err`, `err_msg` |
| `http.agg` | info | Everything else, folded per route every 60 s: counts, histogram, status codes. | `key`, `n`, `p95_ms`, `hist`, `labels`, `bytes` |
| `http.exception` | error | An unhandled error in an endpoint, with signature and stack. | `sig`, `stack`, `exc_type` |
| `db.slow`, `db.lock_wait`, `db.busy`, `db.error`, `db.q` | warn, info | Statement over 250 ms; `BEGIN IMMEDIATE` waiting over 50 ms; "database is locked"; SQL that failed; per-statement totals for anything over 20 ms every 2 minutes. | `sql` (literals removed), `dur_ms` |
| `collect.permit` | info | Every granted Instagram request permit. A refusal is logged once per change of reason, with how many repeats the previous reason had. | `granted`, `kind`, `why`, `hint`, `after_denials` |
| `collect.lease` / `collect.lease_none` | info | A job handed to a lane; or why none was (on change). | `kind`, `seed`, `direction`, `has_cursor` |
| `collect.page` | info, warn | Every list page the extension delivered. | `seed`, `direction`, `returned`, `requested`, `http_status`, `cursor_in`, `cursor_out` (8-character fingerprints), `outcome` (`ok`, `duplicate`, `stale`, `stalled`, `trial_stopped`) |
| `collect.bio` | info | Every bio read. | `handle`, `route`, `saved` |
| `collect.lane_state` | info | A lane's state, hold, pause, cooldown or stage changed (diff against the last heartbeat). | `state`, `tab`, `hold`, `paused`, `cooldown_until`, `stages` |
| `collect.control` | info | An operator action (start, stop, pause, resume). | `action`, `stage`, `target` |
| `ig.adverse`, `ig.error` | warn, error | The extension reported a 429, soft block, login or security check, scraping warning, or home-page redirect. `error` level for login, challenge and scraping warning. | `code`, `reason`, `http_status`, `kind`, `retry_after` |
| `state.setting` | info, warn | A control setting really changed: pauses, `cooldown`, `instagram_scraping_warning`, isolation, budget, mode. Carries the `request_id` and `route` that caused it. | `key`, `value`, `prev` |
| `ext.heartbeat_gap`, `ext.heartbeat_silent`, `ext.heartbeat_back` | warn, info | A lane's heartbeat arrived more than 90 s late; went silent for 2 minutes; came back. | `gap_s`, `age_s` |
| `ext.*` | varies | Events posted by the extension (below). | |
| `llm.call` | info, warn | One OpenRouter, K2 or Laya call: model, duration, token counts, result class. Never a prompt or reply. | `provider`, `op`, `status`, `dur_ms`, `tokens_in`, `tokens_out` |
| `map.layout_apply`, `map.layout_plan_stale` | info, warn | Background map layout work (at most one progress line per 30 s). | `applied`, `pending` |
| `obs.dropped` | warn | The writer fell behind and events were discarded (they are still in the crash ring). | `n`, `total` |

`collect.permit` counts permits the server allowed, which is the Instagram request count. The extension's own
`ext.ig.req` line says what Instagram answered.

## Volume

Routine traffic is folded, not dropped: heartbeats, lease polls, permit retries, read endpoints and photos become
one `http.agg` line per route per minute, and `lane_state`, `permit` refusals and `lease_none` are logged on
change. The rule is that something an agent would ask about ("how many requests did @x make", "when did it last
succeed", "what changed before the 429") is a line of its own. Measured volume is in the report that shipped with
this change.

## What is never logged

Credentials and typed text are removed before an event is queued, so they cannot reach the disk, the crash ring or
a crash dump (`obs.clean`, tested in `server/tests/test_obs.py`):

- Fields named like `cookie`, `session*`, `csrf*`, `token` (but not `tokens_in`), `secret`, `password`, `api_key`,
  `authorization`, `bearer`, `credential`, `x-ig-*`, `ds_user_id`, and the exact names `note`, `notes`, `dm`, `bio`,
  `text`, `body`, `html`, `content`, `prompt`, `caption`, `sample`, `reply`, `response`, `headers`, `email`
  become `[redacted]`.
- Inside any string: `sessionid=...` style cookie pairs, `Authorization`/`Bearer` values, `sk-...` API keys,
  40-plus-character opaque tokens, and everything after `?` in a URL or path.
- Strings are cut at 300 characters, lists at 24 items, nesting at 2 levels. SQL is logged with literals replaced by `?`.
- Instagram pagination cursors are logged only as an 8-character hash. Request bodies, response bodies, bios and
  message text are never logged. Query strings are never logged.

The stderr log is not filtered: a Python traceback prints its exception message as before, so keep secrets out of
exception messages. To redact a new field, add its name to `_DENY` in `server/obs.py`.

## Crash forensics

| Failure | What is left behind | What the next start says |
|---|---|---|
| Uncaught exception on the main thread | `proc.crash` event, `crash/<time>.json`, marker `crashed`, traceback on stderr as before | `previous_run outcome=crashed`, dump path, last events |
| Exception that kills another thread | `thread.uncaught` event and a dump (at most one per minute); the run continues | none, the run is still `clean` if it ends normally |
| `SIGTERM`, `SIGINT`, `SIGHUP` | `proc.signal` with the signal name, `proc.exit`, marker `clean` with the reason | `outcome=clean`, `exit_reason=SIGTERM` |
| `SIGKILL`, out of memory, power loss | nothing at the time; the marker still says `running` and its last-alive time is at most a minute old | `outcome=killed`, last alive time, the last 12 events of the dead run |
| Segfault in a C library | `crash/faulthandler.log` with every thread's stack | `outcome=crashed`, `exit_reason=native crash` |
| Hang | no heartbeat for 3 minutes in the log; `watchdog.stuck_request` / `stuck_loop` with stacks after 30 s; after 5 minutes of nothing the watchdog's faulthandler timer dumps all stacks; `kill -USR1 <pid>` dumps them on demand | `ops/diagnose` flags "alive but silent" |

A request or worker step that raises is not a crash: it is a `http.exception` or `worker.step_error` error event
with a stable signature (`ExceptionType@module:function|message with numbers removed`), which `ops/diagnose` groups.

## Extension events

`extension/lib/log.js` keeps up to 300 small events in `chrome.storage.local` (key `elog`), so a stopped service
worker loses almost nothing. Heartbeats flush them (at most every 20 s, 100 per batch) to `POST /api/ext-log`; a
warning or error flushes within 3 s. Events leave the buffer only after the server accepted them. Overflow drops the
oldest and reports the count (`ext.log_dropped`). What is sent: statuses, timings, classifications, short error
names. Never cookies, tokens, page or API content, or query strings; `sanitize()` enforces that.

| Event | Meaning |
|---|---|
| `ext.sw_start` | The service worker started (with its boot count). Many in a row means Chrome keeps stopping it. |
| `ext.ig.req` | One Instagram request: kind, path without query, HTTP status, milliseconds, classification. |
| `ext.ig.page` | A profile-page lookup. |
| `ext.fail` | A request failed: code, reason, status, hold, cooldown minutes. |
| `ext.permit_denied` | The server refused a permit (rate limited to one per 5 minutes per reason). |
| `ext.server_offline`, `ext.server_online` | The local server became unreachable or came back. |
| `ext.outbox_retry`, `ext.outbox_parked` | A saved result could not be delivered, or the server rejected it for good. |
| `ext.step_threw`, `ext.uncaught`, `ext.unhandled_rejection`, `ext.loop_stalled` | Errors inside the worker. |
| `ext.server_pause`, `ext.account`, `ext.self_update`, `ext.installed`, `ext.browser_startup` | Control and lifecycle changes. |

The endpoint accepts at most 200 events per call, only from the extension origin or a local `X-FL` client.

## Overhead

Logging adds, per request, one dict, one deque append and a few timestamps (about 7 microseconds on the idle
benchmark), plus about 0.5 microseconds per SQL statement. The writer thread does the JSON and the disk I/O. Numbers
from `tests/bench_logging.py` are in the change report; run it to repeat them.

## Maintaining it

- Add an event: `obs.info('component', 'name', field=value)`. It is a no-op until `server.main()` calls `obs.start()`, so
  tests and CLIs are unaffected. Use `obs.transition(...)` for state that polls repeat, `obs.agg(...)` for chatty
  per-key timings, `obs.throttle(key, seconds)` to rate-limit.
- Hooks live in: `server/server.py` (start, SIGTERM), `server/backend_http.py` (request wrapper, `handle_error`),
  `server/backend/runtime.py` (worker steps), `server/backend/app.py` (route, gauges, start-up events),
  `server/storage/connection.py` (timing connection), `server/storage/settings.py` (control setting changes),
  `server/llm.py`, `server/local_model.py`, `server/laya.py` (model-call decorators).
- Collection-specific interpretation (permits, pages, adverse responses, lane state) is in `server/obs_events.py`
  and reads requests and responses from outside the collection service, so it can never change what the service does.
- Environment: `FL_LOG_OFF=1` disables everything, `FL_LOG_LEVEL`, `FL_LOG_MB` (default 10), `FL_LOG_FILES` (default 5),
  `FL_LOG_DIR` (write elsewhere).

## Not verified

- A laptop sleep and wake with the live server (the `proc.slept` and false-hang-dump behaviour is reasoned from
  the clocks, not observed).
- A native crash (the faulthandler path is covered for `SIGUSR1`; a real segfault was not provoked).
- Behaviour with the extension loaded in a real Chrome profile. The extension code is unit-tested and ran in the
  8-hour simulator; nothing was sent to Instagram.
