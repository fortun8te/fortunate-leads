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

for label in "${labels[@]}"; do
  plist="$FL_AGENTS/$label.plist"
  if agent_loaded "$label"; then
    agent_unload "$label" && echo "unloaded $label"
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
