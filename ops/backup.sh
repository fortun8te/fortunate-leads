#!/bin/bash
# Consistent online backup of the leads DB via the SQLite backup API (safe while the server writes;
# never cp a WAL database). Also rotates the server log once it passes 20 MB.
#
#   ops/backup.sh [--db PATH] [--dest DIR] [--keep N] [--no-rotate]
#
# Defaults: --db <repo>/data/leads.sqlite  --dest <repo>/data/backups  --keep 14
# Output:   DEST/leads-YYYYmmdd-HHMMSS-PID-RANDOM.sqlite (single verified file)
# Restore:  stop both LaunchAgents and every other process using the DB; check with
#           lsof data/leads.sqlite{,-wal,-shm}. Move the original DB and any WAL/SHM
#           files together into a private recovery directory. Copy a verified backup
#           to data/leads.sqlite, chmod it 600, then run install-launchagent.sh
#           --with-backup. Keep the originals until the restored server is verified.
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
    -h | --help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done
case "$keep" in '' | *[!0-9]* ) echo "--keep must be a positive integer" >&2; exit 2 ;; esac

die() { echo "$(date '+%F %T') backup FAILED: $*" >&2; exit 1; }

[ -f "$db" ] || die "no database at $db"
python_ok || die "$FL_PYTHON missing or unusable (set PYTHON=...)"
"$FL_PYTHON" -c 'import sys; sys.exit(0 if int(sys.argv[1]) > 0 else 1)' "$keep" || die "--keep must be a positive integer"
mkdir -p "$dest"
[ "$db" != "$FL_REPO/data/leads.sqlite" ] || chmod 700 "$FL_REPO/data" || die "cannot restrict data directory permissions"
chmod 700 "$dest" || die "cannot restrict backup directory permissions"

part="$(mktemp "$dest/.leads-backup.XXXXXXXX")"
chmod 600 "$part"
trap 'rm -f "$part" "$part-journal" "$part-wal" "$part-shm"' EXIT
out="$dest/leads-$(date +%Y%m%d-%H%M%S)-$$-$RANDOM.sqlite"

if ! summary="$("$FL_PYTHON" - "$db" "$part" <<'PY'
import sqlite3, sys
from pathlib import Path
src_path, out = sys.argv[1:]
src = sqlite3.connect(Path(src_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=60)
src.execute('PRAGMA busy_timeout=60000')
dst = sqlite3.connect(out)
src.backup(dst)                                # one consistent snapshot, even mid-write
mode = dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
if mode != 'delete':
    sys.exit(f'backup journal mode is {mode}, expected delete')
check = [row[0] for row in dst.execute('PRAGMA quick_check')]
tables = {row[0] for row in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
required = {'people', 'seeds', 'lists', 'edges', 'accounts', 'settings'}
missing = sorted(required - tables)
people = dst.execute('SELECT count(*) FROM people').fetchone()[0] if not missing else None
dst.close()
src.close()
if check != ['ok']:
    sys.exit(f'quick_check on backup: {check}')
if missing:
    sys.exit(f'backup is not a Fortunate Leads database; missing tables: {", ".join(missing)}')
print(f'people={people}')
PY
)"; then
  die "sqlite backup of $db"
fi
[ ! -e "$part-wal" ] || die "backup still has a WAL; refusing to publish an incomplete file"
# A closed DELETE-mode database needs no shared-memory sidecar. SQLite can leave one behind.
rm -f "$part-shm"
# Hard-link publication is atomic and fails rather than overwriting an existing backup.
"$FL_PYTHON" - "$part" "$out" <<'PY' || die "cannot publish backup without overwriting $out"
import os, sys
os.link(sys.argv[1], sys.argv[2])
PY
rm -f "$part"
trap - EXIT
echo "$(date '+%F %T') backup ok: $out ($(human_bytes "$(file_size "$out")"), $summary)"

# Retain only recognized, complete backup files. Never remove staging files,
# symlinks, or arbitrary leads-*.sqlite files placed in this directory.
"$FL_PYTHON" - "$dest" "$keep" "$out" <<'PYKEEP'
import pathlib, re, sys
root, keep, current = pathlib.Path(sys.argv[1]), int(sys.argv[2]), pathlib.Path(sys.argv[3])
pattern = re.compile(r'leads-\d{8}-\d{6}-\d+-\d+\.sqlite')
backups = sorted(p for p in root.iterdir() if pattern.fullmatch(p.name) and p.is_file() and not p.is_symlink())
for old in backups[:max(0, len(backups) - keep)]:
    if old != current:
        try:
            old.unlink()
        except FileNotFoundError:  # Another successful backup already pruned it.
            continue
        print(f'pruned {old.name}')
PYKEEP

# Log rotation without sudo/newsyslog: launchd opens the log with O_APPEND, so copy + truncate
# is safe while the server runs (a line written during the copy may be lost). Keeps 3 gzipped.
if [ "$rotate" = 1 ] && [ -f "$FL_LOG" ] && [ "$(file_size "$FL_LOG")" -gt $((20 * 1024 * 1024)) ]; then
  [ -f "$FL_LOG.2.gz" ] && mv -f "$FL_LOG.2.gz" "$FL_LOG.3.gz"
  [ -f "$FL_LOG.1.gz" ] && mv -f "$FL_LOG.1.gz" "$FL_LOG.2.gz"
  cp "$FL_LOG" "$FL_LOG.1" && : >"$FL_LOG" && gzip -f "$FL_LOG.1"
  chmod 600 "$FL_LOG" "$FL_LOG.1.gz"
  echo "rotated $FL_LOG"
fi
