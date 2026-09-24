#!/bin/bash
# Installs the Fortunate Leads server as a LaunchAgent (starts at login, restarts if it dies).
# Safe to re-run: an unchanged, running agent is left alone.
#
#   ops/install-launchagent.sh                 install / update the server agent
#   ops/install-launchagent.sh --with-backup   also install the daily 03:30 DB backup agent
#   ops/install-launchagent.sh --restart       reload the agent even if nothing changed
#   ops/install-launchagent.sh --no-kill       do not stop other servers on :8766 / :8777
#
# The repo path comes from this script's location; python defaults to /usr/bin/python3
# (override: PYTHON=/path/to/python3 ops/install-launchagent.sh).
set -euo pipefail
# shellcheck source=ops/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

with_backup=0 restart=0 kill_others=1
for arg in "$@"; do
  case "$arg" in
    --with-backup) with_backup=1 ;;
    --restart) restart=1 ;;
    --no-kill) kill_others=0 ;;
    -h | --help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }

# --- preflight -------------------------------------------------------------
is_macos || die "LaunchAgents are macOS only (uname: $(uname -s)). Run python3 server/server.py by hand instead."
[ -f "$FL_REPO/server/server.py" ] || die "no server/server.py under $FL_REPO"
if ! python_ok; then
  die "$FL_PYTHON missing, older than 3.9, or without sqlite3.
  /usr/bin/python3 needs the Command Line Tools: xcode-select --install
  or point at another interpreter: PYTHON=/opt/homebrew/bin/python3 $0"
fi
case "$FL_REPO/" in
  "$HOME"/Documents/* | "$HOME"/Desktop/* | "$HOME"/Downloads/*)
    echo "warning: $FL_REPO is in a privacy-protected folder; launchd agents may get" >&2
    echo "         'Operation not permitted'. ~/fortunate-leads is the expected location." >&2 ;;
esac
mkdir -p "$FL_AGENTS" "$HOME/Library/Logs" "$FL_REPO/data"
echo "repo:   $FL_REPO"
echo "python: $FL_PYTHON ($("$FL_PYTHON" -c 'import platform; print(platform.python_version())'))"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# --- stray servers -----------------------------------------------------------
# :8766 = the old project's server (a Cursor session in ~/Documents/Codex keeps restarting it).
# :8777 held by anything but this agent = a hand-started server; the agent would crash-loop on "address in use".
agent_pid="$(agent_field "$FL_LABEL" pid)"
for port in "$FL_OLD_PORT" "$FL_PORT"; do
  pids=()
  while read -r pid; do
    [ -n "$pid" ] && [ "$pid" != "$agent_pid" ] && pids+=("$pid")
  done < <(port_pids "$port")
  [ "${#pids[@]}" -gt 0 ] || continue
  for pid in "${pids[@]}"; do
    echo ":$port pid $pid  $(proc_cmd "$pid")  cwd=$(proc_cwd "$pid")"
  done
  if [ "$kill_others" = 1 ]; then
    echo "stopping ${pids[*]} on :$port"
    stop_pids "${pids[@]}"
  else
    echo "left running (--no-kill)"
  fi
done
if [ -n "$(port_pids "$FL_OLD_PORT")" ]; then
  echo "note: something restarted :$FL_OLD_PORT - close the Cursor window on the old project (~/Documents/Codex/...)." >&2
fi

# --- server agent --------------------------------------------------------------
dst="$FL_AGENTS/$FL_LABEL.plist"
render_plist "$FL_OPS/$FL_LABEL.plist" "$tmp/$FL_LABEL.plist"
plutil -lint "$tmp/$FL_LABEL.plist" >/dev/null

if [ "$restart" = 0 ] && cmp -s "$tmp/$FL_LABEL.plist" "$dst" && agent_loaded "$FL_LABEL" &&
  [ "$(agent_field "$FL_LABEL" state)" = running ] && http_ok /api/counts; then
  echo "server agent already installed and running (unchanged)"
else
  agent_unload "$FL_LABEL" || true
  cp "$tmp/$FL_LABEL.plist" "$dst"
  launchctl enable "$FL_DOMAIN/$FL_LABEL"   # a disabled label refuses bootstrap
  if ! launchctl bootstrap "$FL_DOMAIN" "$dst" 2>"$tmp/err"; then
    sleep 2   # "Bootstrap failed: 5: Input/output error" is usually a bootout still settling
    launchctl bootstrap "$FL_DOMAIN" "$dst" 2>"$tmp/err" || die "launchctl bootstrap: $(cat "$tmp/err")"
  fi
  echo "server agent loaded: $dst"
fi

# --- backup agent (optional) ---------------------------------------------------
if [ "$with_backup" = 1 ]; then
  bdst="$FL_AGENTS/$FL_BACKUP_LABEL.plist"
  render_plist "$FL_OPS/$FL_BACKUP_LABEL.plist" "$tmp/$FL_BACKUP_LABEL.plist"
  plutil -lint "$tmp/$FL_BACKUP_LABEL.plist" >/dev/null
  if cmp -s "$tmp/$FL_BACKUP_LABEL.plist" "$bdst" && agent_loaded "$FL_BACKUP_LABEL"; then
    echo "backup agent already installed (unchanged)"
  else
    agent_unload "$FL_BACKUP_LABEL" || true
    cp "$tmp/$FL_BACKUP_LABEL.plist" "$bdst"
    launchctl enable "$FL_DOMAIN/$FL_BACKUP_LABEL"
    launchctl bootstrap "$FL_DOMAIN" "$bdst" || die "launchctl bootstrap $FL_BACKUP_LABEL failed"
    echo "backup agent loaded: daily 03:30 -> $FL_REPO/data/backups (log $FL_BACKUP_LOG)"
  fi
  echo "run one now: launchctl kickstart $FL_DOMAIN/$FL_BACKUP_LABEL   (or ops/backup.sh)"
elif agent_loaded "$FL_BACKUP_LABEL"; then
  echo "backup agent: installed (left as is)"
else
  echo "backup agent: not installed (add --with-backup)"
fi

# --- verify --------------------------------------------------------------------
if wait_http /api/counts 15; then
  echo "ok: http://127.0.0.1:$FL_PORT  log: $FL_LOG"
else
  echo "server not answering on :$FL_PORT after 15 s; last log lines:" >&2
  tail -n 20 "$FL_LOG" >&2 2>/dev/null || true
  echo "check: ops/doctor.sh" >&2
  exit 1
fi

# Log rotation: launchd appends to one file forever. ops/backup.sh rotates it (copy + truncate)
# once it passes 20 MB, and ops/doctor.sh warns about size, so install --with-backup to keep it bounded.
if [ -f "$FL_LOG" ]; then
  size="$(file_size "$FL_LOG")"
  if [ "${size:-0}" -gt $((20 * 1024 * 1024)) ] && [ "$with_backup" = 0 ] && ! agent_loaded "$FL_BACKUP_LABEL"; then
    echo "note: $FL_LOG is $(human_bytes "$size"); ops/backup.sh (or --with-backup) rotates it." >&2
  fi
fi
echo "health check: $FL_OPS/doctor.sh"
