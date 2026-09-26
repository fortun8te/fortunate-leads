#!/bin/bash
# Double-click to start the installed local services, the three Chrome profiles,
# and all three pipeline stages (including AI provider calls).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

is_macos || { echo 'This one-click launcher is for macOS.' >&2; exit 1; }
agent_checkout_owned "$FL_LABEL" server/server.py || {
  echo 'The installed server belongs to another checkout; no service was started.' >&2; exit 1;
}

echo 'Starting Fortunate Leads (lists, bios, and AI)...'
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

"$FL_PYTHON" "$FL_REPO/sidecar/laya_service.py" start --timeout 180 || {
  echo 'Laya did not become ready; collection and AI were left as they were.' >&2; exit 1;
}

chrome_state="$HOME/Library/Application Support/Google/Chrome/Local State"
profiles="$("$FL_PYTHON" - "$chrome_state" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as stream:
        cache = json.load(stream)['profile']['info_cache']
    for name in ('Michael', 'BOT', 'BOT2'):
        found = [directory for directory, info in cache.items() if info.get('name') == name]
        if len(found) != 1:
            raise ValueError('Expected exactly one Chrome profile named ' + name)
        print(found[0])
except (OSError, ValueError, KeyError, TypeError) as exc:
    print('Chrome profiles could not be found: ' + str(exc), file=sys.stderr)
    sys.exit(1)
PY
)" || exit 1

while IFS= read -r profile_dir; do
  [ -n "$profile_dir" ] || continue
  open -a 'Google Chrome' --args "--profile-directory=$profile_dir" 'https://www.instagram.com/'
done <<< "$profiles"

agent_owns_port || {
  echo 'The server changed before startup; no stages were started.' >&2; exit 1;
}
curl -fsS -m 15 -o /dev/null -H "Origin: http://127.0.0.1:$FL_PORT" \
  -H 'Content-Type: application/json' -d '{"action":"start_all"}' \
  "http://127.0.0.1:$FL_PORT/api/control" || {
    echo 'Services and Chrome opened, but the app could not start all stages.' >&2; exit 1;
  }
open "http://127.0.0.1:$FL_PORT/#/accounts"
echo 'Started. The Accounts page shows any Instagram cooldown or access restriction.'
