# ops quickstart (macOS)

After the one-time service install below, double-click `ops/start-all.command` to
start the server and reconcile local models with their saved settings. Collection
and AI modes retain their separate saved pause states.

Background Chrome startup is opt-in. Once each extension has reported its actual
Instagram account, bind its lane to the Chrome profile where you verified that
account is signed in:

```sh
python3 ops/configure-browser-startup.py --profile 'Profile 2=VERIFIED_LANE_ID' --profile 'Profile 5=SECOND_VERIFIED_LANE_ID'
python3 ops/configure-browser-startup.py --disable
```

Bindings stay private in `data/browser-startup.json`; no cookies or credentials are
copied. The server at login, the local launcher, and an explicit collection Resume
reopen only those profiles, only for unpaused collection. Chrome loads them without
a foreground window. The extension then reuses its Instagram tab or opens an
inactive/minimized one under the existing request gate. Authentication, challenges,
account budgets and cooldowns remain enforced. The Accounts page's live heartbeat
is the readiness check; launching Chrome does not confirm an Instagram login.

There is no browser watchdog: closing Chrome leaves it closed until the next
explicit start/resume or server startup. Stop is checked before each profile launch;
repeated starts within a minute do not dispatch the same profile again. An account
whose recorded Instagram identity has changed must be explicitly rebound.

```sh
ops/install.sh                             # new Mac: server + daily backup + health check, then opens the web app
ops/install-launchagent.sh --with-backup   # server at login on :8777 + daily 03:30 DB backup; safe to re-run
ops/doctor.sh                              # PASS/WARN/FAIL health check; exit 1 on any FAIL
ops/doctor.sh --fix                        # stop the old :8766 / hand-started :8777 servers, restart the agent
ops/backup.sh                              # backup now -> data/backups/leads-YYYYmmdd-HHMMSS-PID-RANDOM.sqlite
ops/uninstall-launchagent.sh               # remove both agents; data, backups and logs stay
```
Logs: `~/Library/Logs/fortunate-leads.log` (server), `~/Library/Logs/fortunate-leads-backup.log` (backup).
The server log is rotated by `backup.sh` past 20 MB (copy + truncate, 3 `.gz` kept); `doctor.sh` warns on size.
Generated database backups keep the three newest copies and one copy on each of the newest 14 dates by default. `--keep N` changes the number of dates. Named checkpoints are untouched.

For a manual private snapshot of the whole workspace data, run `python3 ops/backup-workspace.py --output /private/path/snapshot-name`, then `python3 ops/backup-workspace.py --verify /private/path/snapshot-name`. It uses online SQLite backups for the lead, public bio cache, and usage databases, and copies photos, reviews, labels, reports, checkpoints, and the startup profile configuration. It does not install a schedule or include provider keys, browser cookies, model weights, old backups, or work files. Keep the destination private and outside this repository.
Repo path is taken from the script location; python is `/usr/bin/python3` unless `PYTHON=...` is set.
Soak test checklist: `ops/soak.md`.
