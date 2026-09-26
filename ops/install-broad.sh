#!/bin/bash
# Installs the Broad sidecar on 127.0.0.1:18742. Training and deployment are manual
# until independent owner-reviewed lead quality has been measured.
# First run sets up sidecar/.venv (laya + scikit-learn, ~1 GB with torch). Model weights must already be in
# sidecar/.cache/huggingface or be fetched once with: HF_HUB_OFFLINE=0 sidecar/.venv/bin/python -c "import laya; laya.load('convaiinnovations/laya', subfolder='multilingual')"
#
#   ops/install-broad.sh            install / update and (re)start
#   ops/install-broad.sh --remove   stop and remove both agents
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
PY="$REPO/sidecar/.venv/bin/python"
LOG="$HOME/Library/Logs/fortunate-leads-broad.log"
dom="gui/$(id -u)"

if [ "${1:-}" = --remove ]; then
  for l in com.fortunate.leads.broad com.fortunate.leads.broad-train; do
    launchctl bootout "$dom/$l" 2>/dev/null || true
    rm -f "$AGENTS/$l.plist"
  done
  echo removed; exit 0
fi

if [ ! -x "$PY" ]; then
  python3.11 -m venv "$REPO/sidecar/.venv"
  "$PY" -m pip install -q -r "$REPO/sidecar/requirements.txt"
fi
"$PY" -c 'import laya, sklearn' || { echo "sidecar/.venv is missing laya or scikit-learn" >&2; exit 1; }
"$PY" - "$REPO/data/broad_head.pkl" "$REPO/sidecar" <<'PY' || { echo "promote a reviewed Broad candidate before installing" >&2; exit 1; }
import pickle, sys
sys.path.insert(0, sys.argv[2])
import broad
with open(sys.argv[1], 'rb') as f:
    head = pickle.load(f)
assert head.get('feature_version') == broad.FEATURE_VERSION and head.get('tags') == list(broad.TAGS)
PY

plist() {  # label, program args (xml), extra keys (xml)
  cat > "$AGENTS/$1.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key><array>$2</array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  $3
</dict>
</plist>
EOF
  launchctl bootout "$dom/$1" 2>/dev/null || true
  launchctl bootstrap "$dom" "$AGENTS/$1.plist"
}
mkdir -p "$AGENTS" "$HOME/Library/Logs"
# The old zero-shot Laya sidecar (com.fortunate.laya) used the same port; Broad replaces it.
launchctl bootout "$dom/com.fortunate.laya" 2>/dev/null || true
rm -f "$AGENTS/com.fortunate.laya.plist"
plist com.fortunate.leads.broad "<string>$PY</string><string>$REPO/sidecar/broad_server.py</string>" \
  "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>10</integer>"
launchctl bootout "$dom/com.fortunate.leads.broad-train" 2>/dev/null || true
rm -f "$AGENTS/com.fortunate.leads.broad-train.plist"
echo "installed: broad sidecar (log $LOG); training is manual"
