# Shared helpers for ops/*.sh. Source it; do not run it.
# shellcheck shell=bash
# shellcheck disable=SC2034  # variables are used by the scripts that source this file

# Repo root = parent of the ops/ directory holding this file, wherever the clone lives.
FL_OPS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
FL_REPO="$(dirname "$FL_OPS")"
FL_LABEL=com.fortunate.leads
FL_BACKUP_LABEL=com.fortunate.leads.backup
FL_PORT="${FL_PORT:-8777}"
FL_OLD_PORT=8766
FL_PYTHON="${PYTHON:-/usr/bin/python3}"
FL_DB="${FL_DB:-$FL_REPO/data/leads.sqlite}"
FL_AGENTS="$HOME/Library/LaunchAgents"
FL_LOG="$HOME/Library/Logs/fortunate-leads.log"
FL_BACKUP_LOG="$HOME/Library/Logs/fortunate-leads-backup.log"
FL_DOMAIN="gui/$(id -u)"

is_macos() { [ "$(uname -s)" = Darwin ]; }
have() { command -v "$1" >/dev/null 2>&1; }

# python3 that can run the server (3.9+: server.py uses dict | dict) with sqlite3.
python_ok() {
  [ -x "$FL_PYTHON" ] &&
    "$FL_PYTHON" -c 'import sqlite3, sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1
}

# PIDs listening on TCP port $1, one per line (empty if none or lsof missing).
port_pids() {
  have lsof || return 0
  lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | sort -u
}

proc_cmd() { ps -o command= -p "$1" 2>/dev/null | cut -c1-200; }
proc_cwd() {
  have lsof || return 0
  lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
}

# TERM, wait up to 3 s, then KILL whatever is left.
stop_pids() {
  local alive pid
  [ $# -gt 0 ] || return 0
  kill "$@" 2>/dev/null || true
  for _ in 1 2 3 4 5 6; do
    alive=
    for pid in "$@"; do kill -0 "$pid" 2>/dev/null && alive=1; done
    [ -n "$alive" ] || return 0
    sleep 0.5
  done
  kill -9 "$@" 2>/dev/null || true
}

agent_loaded() { have launchctl && launchctl print "$FL_DOMAIN/$1" >/dev/null 2>&1; }
# One field ("pid", "state", "last exit code") from launchctl print; empty if absent.
agent_field() {
  have launchctl || return 0
  { launchctl print "$FL_DOMAIN/$1" 2>/dev/null || true; } |   # not loaded: exit 113, must not trip set -e/pipefail
    awk -v k="$2" -F' = ' '{sub(/^[ \t]+/, "", $1)} $1 == k {print $2; exit}'
}

# bootout and wait until launchd has really dropped the service (bootstrap right after fails otherwise).
agent_unload() {
  agent_loaded "$1" || return 0
  launchctl bootout "$FL_DOMAIN/$1" 2>/dev/null || true
  for _ in $(seq 1 20); do
    agent_loaded "$1" || return 0
    sleep 0.5
  done
  echo "warning: $1 still loaded after bootout" >&2
  return 1
}

# render_plist TEMPLATE OUT: fill __REPO__/__HOME__/__PYTHON__ (XML-escaped) and validate with plistlib.
render_plist() {
  "$FL_PYTHON" - "$1" "$2" "$FL_REPO" "$HOME" "$FL_PYTHON" <<'PY'
import plistlib, sys
from xml.sax.saxutils import escape
src, out, repo, home, py = sys.argv[1:]
text = open(src).read()
for k, v in (('__REPO__', repo), ('__HOME__', home), ('__PYTHON__', py)):
    text = text.replace(k, escape(v))
plistlib.loads(text.encode())  # raises on malformed output
open(out, 'w').write(text)
PY
}

# http_ok PATH: 200 from the local server within 3 s.
http_ok() { curl -fsS -m 3 -o /dev/null "http://127.0.0.1:$FL_PORT$1" 2>/dev/null; }

# wait_http PATH SECONDS
wait_http() {
  for _ in $(seq 1 "$(($2 * 2))"); do
    http_ok "$1" && return 0
    sleep 0.5
  done
  return 1
}

human_bytes() {
  awk -v b="${1:-0}" 'BEGIN { split("B KB MB GB TB", u); i = 1; while (b >= 1024 && i < 5) { b /= 1024; i++ }
    printf (i == 1 ? "%d %s" : "%.1f %s"), b, u[i] }'
}

file_size() { wc -c <"$1" 2>/dev/null | tr -d ' '; }
