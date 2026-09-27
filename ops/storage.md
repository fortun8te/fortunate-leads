# Storage maintenance

No background job is installed. Run a dry-run when disk usage needs attention:

```sh
python3 ops/storage_cleanup.py --data /Users/michael/fortunate-leads/data
```

The JSON report lists every removable file and its size. Add `--apply` to remove that freshly checked list. Add `--include-benchmarks` to include the three known disposable `/tmp/fl-map-backend-{100k,1m,5m}.sqlite` benchmark databases and their sidecars. Open benchmark files are skipped. Any open or changed file detected immediately before deletion aborts the operation.

Generated backups retain the newest three snapshots and the newest snapshot on each of seven dates. Named checkpoints and older backup naming formats are left alone. Before pruning, the newest retained snapshot must pass SQLite integrity and lead-table checks. This command never edits the live database, downloaded pictures, models or public bio cache.

Use `ops/backup.sh` with its ordinary retention setting for new backups; do not repeatedly pass `--keep 999`. Cleanup is manual, so no scheduled task is required or enabled.

The main database measured 300,777,472 bytes on 2026-09-27, with only 2,633,728 bytes of unused pages. Those pages are reused by SQLite: a live VACUUM would recover less than 1%, so it is unnecessary. Large graph tables and their indexes contain real saved follow evidence and are retained.

The separate public bio cache contains 27,940 full profile responses (about 2.19 GB), no queued delivery records and no free pages. Preserve it until the cache reader's expiry and payload contract has been confirmed. Deleting these responses merely because they are large could trigger extra Instagram requests; removing fields could break reuse. Most immediately recoverable space is redundant backups and synthetic benchmark databases.
