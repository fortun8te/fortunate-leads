#!/bin/bash
# Installs the Fortunate Leads server as a LaunchAgent (starts at login, restarts if it dies).
set -euo pipefail
LABEL=com.fortunate.leads
SRC="$(cd "$(dirname "$0")" && pwd)/$LABEL.plist"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true   # replace an older copy of this agent

for port in 8766 8777; do   # 8766 = old server, 8777 = a hand-started new one
  pids=$(lsof -nP -tiTCP:$port -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$pids" ]; then
    echo "killing $pids on :$port"
    kill $pids 2>/dev/null || true
    sleep 1
    kill -9 $pids 2>/dev/null || true
  fi
done

sed "s#__HOME__#$HOME#g" "$SRC" > "$DST"
plutil -lint "$DST" >/dev/null
launchctl bootstrap "$DOMAIN" "$DST"
launchctl enable "$DOMAIN/$LABEL"
sleep 2
if curl -fsS -o /dev/null http://127.0.0.1:8777/api/counts; then
  echo "ok: http://127.0.0.1:8777  log: ~/Library/Logs/fortunate-leads.log"
else
  echo "server not answering yet; see ~/Library/Logs/fortunate-leads.log" >&2
  exit 1
fi
# Uninstall: launchctl bootout $DOMAIN/$LABEL && rm "$DST"
