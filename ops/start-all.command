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

"$FL_PYTHON" "$FL_REPO/sidecar/laya_service.py" start --timeout 180 || {
  echo 'Laya did not become ready; collection and AI were left as they were.' >&2; exit 1;
}

# Optional local note reader: use an installed Ollama only; never install or pull models here.
ollama_ready() { curl -fsS -m 2 -o /dev/null http://127.0.0.1:11434/api/tags 2>/dev/null; }
if ! ollama_ready; then
  if [ -d /Applications/Ollama.app ]; then
    open -g -a /Applications/Ollama.app || true
    for attempt in 1 2 3 4 5; do
      ollama_ready && break
      sleep 1
    done
  fi
  if ! ollama_ready; then
    echo 'The local note reader is unavailable. Fortunate Leads can still run; saved notes remain available.' >&2
  fi
fi

if [ "$OPEN_DASHBOARD" -eq 1 ]; then
  open "http://127.0.0.1:$FL_PORT/#/accounts"
  echo 'Local services ready. Open Accounts to connect an account when needed; collection settings were not changed.'
fi
printf 'ENGINE_STARTED:0\n'
