#!/bin/bash
# Start local services only. Connecting an Instagram account and resuming collection are separate actions.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

OPEN_DASHBOARD=1
case "${1:-}" in
  '') ;;
  --from-app) OPEN_DASHBOARD=0 ;;
  *) echo 'Usage: start-all.command [--from-app]' >&2; exit 2 ;;
esac

if [ "$OPEN_DASHBOARD" -eq 1 ] && [ -t 0 ]; then
  trap 'st=$?; [ "$st" -eq 0 ] || { echo; read -r -p "Something went wrong (see above). Press Return to close. " _; }' EXIT
fi

is_macos || { echo 'This one-click launcher is for macOS.' >&2; exit 1; }
agent_checkout_owned "$FL_LABEL" server/server.py || {
  echo 'The installed server belongs to another checkout; no service was started.' >&2; exit 1;
}

echo 'Starting Fortunate Leads...'
if wait_http /api/counts 3; then
  agent_owns_port || {
    echo 'Another server is answering on the Fortunate Leads port; no stages were started.' >&2; exit 1;
  }
else
  [ -z "$(port_pids "$FL_PORT")" ] || {
    echo 'An unverified server holds the Fortunate Leads port; no stages were started.' >&2; exit 1;
  }
  [ -f "$FL_AGENTS/$FL_LABEL.plist" ] || {
    # First run on this Mac: the installer sets up the server, the backup and the health check.
    [ "$OPEN_DASHBOARD" -eq 1 ] || { echo 'Install the server once with ops/install.sh.' >&2; exit 1; }
    exec "$FL_OPS/install.sh"
  }
  if agent_loaded "$FL_LABEL"; then
    launchctl kickstart -k "$FL_DOMAIN/$FL_LABEL"
  else
    launchctl enable "$FL_DOMAIN/$FL_LABEL"
    launchctl bootstrap "$FL_DOMAIN" "$FL_AGENTS/$FL_LABEL.plist"
  fi
  wait_http /api/counts 30 && agent_owns_port || {
    echo 'The installed server did not become ready on its own port.' >&2; exit 1;
  }
fi

# Reconcile each engine independently against current saved intent.
"$FL_PYTHON" - "$FL_REPO" "$FL_DB" "$OPEN_DASHBOARD" <<'PYENGINES'
import sqlite3, sys
sys.path.insert(0, sys.argv[1] + '/server')
import engine_start
import browser_startup
with sqlite3.connect('file:' + sys.argv[2] + '?mode=ro', uri=True) as conn:
    engine_start.reconcile_models(sys.argv[1], conn)
# Reopening saved browsers is opt-in and follows saved collection pause state.
if sys.argv[3] == '1':
    with sqlite3.connect(sys.argv[2]) as conn:
        conn.row_factory = sqlite3.Row
        browser_startup.launch_saved(sys.argv[1], conn)
PYENGINES

if [ "$OPEN_DASHBOARD" -eq 1 ]; then
  "$FL_OPS/doctor.sh" --quick 2>&1 | grep -E '^(WARN|FAIL|==)' || true
  # The web app shows Get started by itself while anything is missing, and Leads once everything is ready.
  open "http://127.0.0.1:$FL_PORT/"
  echo 'Local services ready. Collection settings were not changed.'
fi
printf 'ENGINE_STARTED:0\n'
