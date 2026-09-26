#!/bin/bash
# Removes the Fortunate Leads LaunchAgents (server + daily backup). Data, backups and logs stay.
#
#   ops/uninstall-launchagent.sh               remove both agents
#   ops/uninstall-launchagent.sh --keep-backup remove only the server agent
set -euo pipefail
# shellcheck source=ops/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

labels=("$FL_LABEL" "$FL_BACKUP_LABEL")
for arg in "$@"; do
  case "$arg" in
    --keep-backup) labels=("$FL_LABEL") ;;
    -h | --help) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

if ! is_macos; then
  echo "not macOS ($(uname -s)): no LaunchAgents to remove" >&2
  exit 1
fi

# Verify every requested label before removing either one.
for label in "${labels[@]}"; do
  entry=server/server.py
  [ "$label" != "$FL_BACKUP_LABEL" ] || entry=ops/backup.sh
  agent_checkout_owned "$label" "$entry" || {
    echo "error: $label belongs to another checkout; refusing removal" >&2
    exit 1
  }
done

for label in "${labels[@]}"; do
  plist="$FL_AGENTS/$label.plist"
  if agent_loaded "$label"; then
    if ! agent_unload "$label"; then
      echo "error: $label is still loaded; leaving its plist in place" >&2
      exit 1
    fi
    echo "unloaded $label"
  fi
  if [ -f "$plist" ]; then
    rm -f "$plist" && echo "removed $plist"
  elif ! agent_loaded "$label"; then
    echo "$label: not installed"
  fi
done

if [ -n "$(port_pids "$FL_PORT")" ]; then
  echo "note: something still listens on :$FL_PORT (a hand-started server?): $(port_pids "$FL_PORT" | tr '\n' ' ')"
fi
echo "kept: $FL_REPO/data (DB, backups), $FL_LOG"
echo "reinstall: $FL_OPS/install-launchagent.sh [--with-backup]"
