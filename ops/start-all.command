#!/bin/bash
# Double-click to start the installed local services, the configured Chrome profiles,
# and collection stages. External AI keeps its saved setting.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

OPEN_DASHBOARD=1
case "${1:-}" in
  '') ;;
  --from-app) OPEN_DASHBOARD=0 ;;
  *) echo 'Usage: start-all.command [--from-app]' >&2; exit 2 ;;
esac

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
    echo 'Install the server once with ops/install-launchagent.sh --with-backup.' >&2; exit 1;
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

profiles="$("$FL_PYTHON" "$FL_REPO/ops/chrome_profiles.py" --configured)" || exit 1
profile_count="$(printf '%s\n' "$profiles" | awk 'NF { n++ } END { print n+0 }')"
account_count="$("$FL_PYTHON" - "$FL_DB" <<'PY'
import sqlite3, sys
from pathlib import Path
try:
    path = Path(sys.argv[1]).resolve()
    with sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True) as conn:
        print(conn.execute('SELECT count(*) FROM accounts').fetchone()[0])
except (OSError, sqlite3.Error) as exc:
    print('App accounts could not be checked: ' + str(exc), file=sys.stderr)
    sys.exit(1)
PY
)" || exit 1
[ "$profile_count" -eq "$account_count" ] || {
  echo "Start engine is configured for $profile_count Chrome profiles, but the app has $account_count accounts. Update ops/startup_profiles.json to match; no profiles or stages were started." >&2
  exit 1
}

"$FL_PYTHON" "$FL_REPO/sidecar/laya_service.py" start --timeout 180 || {
  echo 'Laya did not become ready; collection and AI were left as they were.' >&2; exit 1;
}

profile_count=0

while IFS= read -r profile_dir; do
  [ -n "$profile_dir" ] || continue
  open -a 'Google Chrome' --args "--profile-directory=$profile_dir" 'https://www.instagram.com/'
  profile_count=$((profile_count + 1))
done <<< "$profiles"

agent_owns_port || {
  echo 'The server changed before startup; no stages were started.' >&2; exit 1;
}
curl -fsS -m 15 -o /dev/null -H "Origin: http://127.0.0.1:$FL_PORT" \
  -H 'Content-Type: application/json' -d '{"action":"start_all"}' \
  "http://127.0.0.1:$FL_PORT/api/control" || {
    echo 'Services and Chrome opened, but the app could not resume collection.' >&2; exit 1;
  }
if [ "$OPEN_DASHBOARD" -eq 1 ]; then
  open "http://127.0.0.1:$FL_PORT/#/accounts"
  echo 'Started. The Accounts page shows any Instagram cooldown or access restriction.'
fi
printf 'ENGINE_STARTED:%s\n' "$profile_count"
