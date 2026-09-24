#!/bin/bash
# Consistent online backup of the leads DB via the SQLite backup API (safe while the server writes;
# never cp a WAL database). Also rotates the server log once it passes 20 MB.
#
#   ops/backup.sh [--db PATH] [--dest DIR] [--keep N] [--no-rotate]
#
# Defaults: --db <repo>/data/leads.sqlite  --dest <repo>/data/backups  --keep 14
# Output:   DEST/leads-YYYYmmdd-HHMM.sqlite (single file, journal_mode=DELETE, quick_check verified)
# Restore:  stop the agent (launchctl bootout gui/$(id -u)/com.fortunate.leads), remove
#           data/leads.sqlite{,-wal,-shm}, copy a backup to data/leads.sqlite, re-run install-launchagent.sh.
set -euo pipefail
# shellcheck source=ops/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

db="$FL_DB" dest="$FL_REPO/data/backups" keep=14 rotate=1
while [ $# -gt 0 ]; do
  case "$1" in
    --db) db="${2:?--db needs a path}"; shift 2 ;;
    --dest) dest="${2:?--dest needs a directory}"; shift 2 ;;
    --keep) keep="${2:?--keep needs a number}"; shift 2 ;;
    --no-rotate) rotate=0; shift ;;
    -h | --help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done
case "$keep" in '' | *[!0-9]* | 0) echo "--keep must be a positive integer" >&2; exit 2 ;; esac

die() { echo "$(date '+%F %T') backup FAILED: $*" >&2; exit 1; }

[ -f "$db" ] || die "no database at $db"
python_ok || die "$FL_PYTHON missing or unusable (set PYTHON=...)"
mkdir -p "$dest"

out="$dest/leads-$(date +%Y%m%d-%H%M).sqlite"
part="$out.partial"
rm -f "$part" "$part-journal"

if ! summary="$("$FL_PYTHON" - "$db" "$part" <<'PY'
import sqlite3, sys
src_path, out = sys.argv[1:]
src = sqlite3.connect(src_path, timeout=60)
src.execute('PRAGMA busy_timeout=60000')
dst = sqlite3.connect(out)
src.backup(dst)                                # one consistent snapshot, even mid-write
dst.execute('PRAGMA journal_mode=DELETE')      # self-contained file: no -wal/-shm needed to open it
check = dst.execute('PRAGMA quick_check').fetchone()[0]
people = dst.execute('SELECT count(*) FROM people').fetchone()[0] if dst.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='people'").fetchone() else 0
dst.close()
src.close()
if check != 'ok':
    sys.exit(f'quick_check on backup: {check}')
print(f'people={people}')
PY
)"; then
  rm -f "$part"
  die "sqlite backup of $db"
fi
mv -f "$part" "$out"
echo "$(date '+%F %T') backup ok: $out ($(human_bytes "$(file_size "$out")"), $summary)"

# keep the newest $keep (names sort chronologically)
shopt -s nullglob
backups=("$dest"/leads-*.sqlite)
if [ "${#backups[@]}" -gt "$keep" ]; then
  for old in "${backups[@]:0:${#backups[@]}-keep}"; do
    rm -f "$old" && echo "pruned $(basename "$old")"
  done
fi
for stale in "$dest"/leads-*.sqlite.partial; do rm -f "$stale"; done

# Log rotation without sudo/newsyslog: launchd opens the log with O_APPEND, so copy + truncate
# is safe while the server runs (a line written during the copy may be lost). Keeps 3 gzipped.
if [ "$rotate" = 1 ] && [ -f "$FL_LOG" ] && [ "$(file_size "$FL_LOG")" -gt $((20 * 1024 * 1024)) ]; then
  [ -f "$FL_LOG.2.gz" ] && mv -f "$FL_LOG.2.gz" "$FL_LOG.3.gz"
  [ -f "$FL_LOG.1.gz" ] && mv -f "$FL_LOG.1.gz" "$FL_LOG.2.gz"
  cp "$FL_LOG" "$FL_LOG.1" && : >"$FL_LOG" && gzip -f "$FL_LOG.1"
  echo "rotated $FL_LOG"
fi
