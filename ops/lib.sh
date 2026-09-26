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
FL_DB="${FL_DB:-$FL_REPO/data/leads.sqlite}"
FL_AGENTS="$HOME/Library/LaunchAgents"
FL_LOG="$HOME/Library/Logs/fortunate-leads.log"
FL_BACKUP_LOG="$HOME/Library/Logs/fortunate-leads-backup.log"
FL_DOMAIN="gui/$(id -u)"
umask 077

is_macos() { [ "$(uname -s)" = Darwin ]; }
have() { command -v "$1" >/dev/null 2>&1; }

# Keep the service's configured interpreter. A broken existing path must fail
# preflight rather than silently replacing it with a different Python.
select_python() {
  local plist reader candidate
  if [ -n "${PYTHON:-}" ]; then printf '%s\n' "$PYTHON"; return; fi
  plist="$FL_AGENTS/$FL_LABEL.plist"
  if [ -f "$plist" ]; then
    reader=
    for candidate in /usr/bin/python3 "$FL_REPO/data/scraper-venv/bin/python" "$(command -v python3 || true)"; do
      if [ -x "$candidate" ] && "$candidate" -c 'import plistlib' >/dev/null 2>&1; then
        reader="$candidate"; break
      fi
    done
    [ -n "$reader" ] || { echo "cannot read installed Python; set PYTHON explicitly" >&2; return 1; }
    "$reader" - "$plist" <<'PY'
import plistlib, sys
try:
    with open(sys.argv[1], 'rb') as stream:
        args = plistlib.load(stream)['ProgramArguments']
    if not isinstance(args, list) or not args or not isinstance(args[0], str) or not args[0].startswith('/'):
        raise ValueError('expected an absolute interpreter path')
    print(args[0])
except (OSError, ValueError, KeyError, TypeError) as exc:
    print('cannot read installed Python; set PYTHON explicitly: ' + str(exc), file=sys.stderr)
    sys.exit(1)
PY
    return
  fi
  if [ -e "$FL_REPO/data/scraper-venv/bin/python" ] || [ -L "$FL_REPO/data/scraper-venv/bin/python" ]; then
    printf '%s\n' "$FL_REPO/data/scraper-venv/bin/python"
  else
    printf '%s\n' /usr/bin/python3
  fi
}

FL_PYTHON="$(select_python)" || return 1

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

agent_owns_port() {
  local expected pid seen=0
  have lsof || return 1
  expected="$(agent_field "$FL_LABEL" pid)"
  [ -n "$expected" ] || return 1
  while read -r pid; do
    [ -n "$pid" ] || continue
    [ "$pid" = "$expected" ] || return 1
    seen=1
  done < <(port_pids "$FL_PORT")
  [ "$seen" = 1 ]
}

proc_cmd() { ps -o command= -p "$1" 2>/dev/null; }
proc_cwd() {
  have lsof || return 0
  lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
}

# Only a server running from this exact checkout may be stopped. A port number,
# process name, or a different checkout is never sufficient proof of ownership.
owned_server_pid() {
  local pid="$1" cmd cwd uid
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  uid="$(ps -o uid= -p "$pid" 2>/dev/null | tr -d ' ')"
  [ "$uid" = "$(id -u)" ] || return 1
  cwd="$(proc_cwd "$pid")"
  [ "$cwd" = "$FL_REPO" ] || return 1
  cmd="$(proc_cmd "$pid")"
  # ps flattens argv, so accept only the configured Python followed directly
  # by our script, with the common unbuffered option. Never match later args.
  local interpreter resolved script
  resolved="$("$FL_PYTHON" -c 'import os,sys; print(os.path.realpath(sys.executable))')" || return 1
  for interpreter in "$FL_PYTHON" "$resolved"; do
    for script in "$FL_REPO/server/server.py" server/server.py; do
      case "$cmd" in
        "$interpreter $script"|"$interpreter $script "*|"$interpreter -u $script"|"$interpreter -u $script "*) return 0 ;;
      esac
    done
  done
  return 1
}

# TERM only after ownership is checked. Never escalate a PID that might be reused.
stop_pids() {
  local alive pid
  [ $# -gt 0 ] || return 0
  for pid in "$@"; do
    if ! owned_server_pid "$pid"; then
      echo "refusing to stop pid $pid: cannot verify this checkout owns it (cwd=$(proc_cwd "$pid"), command=$(proc_cmd "$pid"))" >&2
      return 1
    fi
  done
  for pid in "$@"; do
    owned_server_pid "$pid" || return 1
    kill -TERM "$pid" || return 1
  done
  for _ in 1 2 3 4 5 6; do
    alive=
    for pid in "$@"; do
      if kill -0 "$pid" 2>/dev/null; then
        owned_server_pid "$pid" || { echo "pid $pid changed identity while stopping; refusing further action" >&2; return 1; }
        alive=1
      fi
    done
    [ -n "$alive" ] || return 0
    sleep 0.5
  done
  echo "server did not exit after TERM; stop it manually" >&2
  return 1
}

source_fingerprint() {
  "$FL_PYTHON" - "$FL_REPO" <<'PY'
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
paths = set((root / 'server').glob('*.py'))
paths.update((root / 'scraper/app').rglob('*.py'))
paths.update(root / name for name in ('scraper/workspace_worker.py', 'scraper/requirements.txt',
                                    'data/scraper-venv/pyvenv.cfg'))
# Dependency metadata changes on installation/upgrades, including editable and
# direct URL installs. Never hash caches, databases or private egress settings.
venv = root / 'data/scraper-venv'
for name in ('METADATA', 'RECORD', 'direct_url.json'):
    paths.update(venv.glob('lib/python*/site-packages/*.dist-info/' + name))
for path in sorted(paths):
    if path.is_file():
        digest.update(str(path.relative_to(root)).encode() + b'\0')
        digest.update(hashlib.sha256(path.read_bytes()).digest())
print(digest.hexdigest())
PY
}

agent_loaded() { have launchctl && launchctl print "$FL_DOMAIN/$1" >/dev/null 2>&1; }

# Check both the saved configuration and launchd's loaded configuration. The
# label alone is not proof: another checkout can use the same service name.
agent_checkout_owned() {
  local label="$1" entry="$2" plist snapshot
  plist="$FL_AGENTS/$label.plist"
  if [ -f "$plist" ]; then
    "$FL_PYTHON" - "$plist" "$FL_REPO" "$entry" "$label" <<'PY' || return 1
import plistlib, sys
try:
    with open(sys.argv[1], 'rb') as stream:
        data = plistlib.load(stream)
    args = data.get('ProgramArguments', [])
    valid = (data.get('Label') == sys.argv[4] and data.get('WorkingDirectory') == sys.argv[2]
             and len(args) >= 2 and args[1] == sys.argv[2] + '/' + sys.argv[3])
except (OSError, ValueError, TypeError):
    valid = False
sys.exit(0 if valid else 1)
PY
  fi
  if agent_loaded "$label"; then
    [ -f "$plist" ] || return 1  # retain a recoverable configuration before replacing it
    snapshot="$(launchctl print "$FL_DOMAIN/$label")" || return 1
    printf '%s\n' "$snapshot" | "$FL_PYTHON" -c '
import re, sys
text = sys.stdin.read()
cwd = re.search(r"(?m)^\s*working directory = (.+)$", text)
block = re.search(r"(?m)^\s*arguments = \{\n(.*?)^\s*\}", text, re.S)
args = [line.strip() for line in block.group(1).splitlines()] if block else []
sys.exit(0 if cwd and cwd.group(1) == sys.argv[1] and len(args) >= 2
         and args[1] == sys.argv[1] + "/" + sys.argv[2] else 1)
' "$FL_REPO" "$entry" || return 1
  fi
}

# Restore only the prior service configuration, never source or database files.
restore_agent() {
  local label="$1" entry="$2" saved="$3" dst="$4" had_plist="$5" was_loaded="$6"
  agent_checkout_owned "$label" "$entry" || {
    echo "rollback refused: $label no longer belongs to this checkout" >&2; return 1;
  }
  agent_unload "$label" || return 1
  if [ "$had_plist" = 1 ]; then cp "$saved" "$dst" || return 1; else rm -f "$dst" || return 1; fi
  if [ "$was_loaded" = 1 ]; then
    launchctl enable "$FL_DOMAIN/$label" || return 1
    launchctl bootstrap "$FL_DOMAIN" "$dst" || return 1
  fi
  echo "previous $label configuration restored; source files were not rolled back" >&2
}

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
