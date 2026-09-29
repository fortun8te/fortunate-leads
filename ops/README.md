# ops quickstart (macOS)

After the one-time service install below, double-click `ops/start-all.command` to
start the server, Laya, the Michael/BOT/BOT2 Chrome profiles, and the saved work
stages. External AI scoring resumes only when local-only mode is off; it may use paid provider calls. Instagram
login holds and cooldowns still apply; the Accounts page shows them.

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
