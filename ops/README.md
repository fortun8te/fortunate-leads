# ops quickstart (macOS)

```sh
ops/install-launchagent.sh --with-backup   # server at login on :8777 + daily 03:30 DB backup; safe to re-run
ops/doctor.sh                              # PASS/WARN/FAIL health check; exit 1 on any FAIL
ops/doctor.sh --fix                        # stop the old :8766 / hand-started :8777 servers, restart the agent
ops/backup.sh                              # backup now -> data/backups/leads-YYYYmmdd-HHMM.sqlite (keeps 14)
ops/uninstall-launchagent.sh               # remove both agents; data, backups and logs stay
```
Logs: `~/Library/Logs/fortunate-leads.log` (server), `~/Library/Logs/fortunate-leads-backup.log` (backup).
The server log is rotated by `backup.sh` past 20 MB (copy + truncate, 3 `.gz` kept); `doctor.sh` warns on size.
Repo path is taken from the script location; python is `/usr/bin/python3` unless `PYTHON=...` is set.
Soak test checklist: `ops/soak.md`.
