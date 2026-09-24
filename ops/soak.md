# Soak test — 10–15 accounts back to back

Goal: hours of list collection at 7–12 s gaps with no 429 / "please wait".

## Before
- Server running via LaunchAgent (`ops/install-launchagent.sh --with-backup`); `ops/doctor.sh` all PASS (nothing on :8766).
- Extension reloaded, manifest version bumped; `/api/scraper` → `ext.version` shows it, `ext.online=true`.
- One logged-in instagram.com tab. Qualification off (`POST /api/settings/qualify {"on":false}`) so only lists run;
  `qualify_auto` true switches it on by itself once every list is done.
- Budget: `POST /api/scraper/budget {"list":3000}` if the day's list budget would stop the run early.

## Run
- Queue 10–15 accounts: `POST /api/scraper/seeds {"handles":[...],"directions":["following"]}` (add followers once that parser is fixed).
- Leave it. Check `curl -s 127.0.0.1:8777/api/scraper | python3 -m json.tool` every ~30 min.

## Watch
| field | healthy |
|---|---|
| `soak.1h.pages` | ~300–500 (one page per 7–12 s) |
| `soak.1h.people` | ~pages × 25–50 |
| `ext.rate.pages_hour` / `people_hour` | matches `soak.1h` within ~10% |
| `ext.cooldown_until` | null the whole run |
| `ext.last_error` | null; any `rate_limit`/`soft_block` = stop and note the time + `soak.6h` totals |
| `lists[].state` | moves queued → running → done one at a time; no `error` |
| `queue.list` | drops by one per finished list |
| `ext.today.list` vs `ext.budget.list` | stays under budget |

## Record afterwards
Accounts done, hours run, total pages/people (`soak.6h`), first 429 time (if any), gaps used.
Tail `~/Library/Logs/fortunate-leads.log` for tracebacks (`ops/doctor.sh` counts recent ones).
