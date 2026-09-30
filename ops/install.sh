#!/bin/bash
# One command for a new Mac: server at login, daily backup, health check, then the web app opens.
# Safe to re-run: unchanged pieces are left alone and nothing is stopped that is not this checkout's.
#
#   ops/install.sh               install, check, open the web app
#   ops/install.sh --no-open     do not open Chrome or the browser (scripts, tests)
#   ops/install.sh --dry-run     print the steps and change nothing
#
# After it finishes, load the extension once per Chrome profile (Get started in the web app walks through it).
set -uo pipefail
# shellcheck source=ops/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

open_apps=1 dry=0
for arg in "$@"; do
  case "$arg" in
    --no-open) open_apps=0 ;;
    --dry-run) dry=1; open_apps=0 ;;
    -h | --help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n[%s/4] %s\n' "$1" "$2"; }
die() { echo "error: $*" >&2; exit 1; }

is_macos || die "this installer is for macOS (uname: $(uname -s)). Elsewhere run: python3 server/server.py"
python_ok || die "$FL_PYTHON is missing, older than 3.9, or has no sqlite3. Run: xcode-select --install"

step 1 "Checking this Mac"
echo "folder: $FL_REPO"
echo "python: $FL_PYTHON"
if [ -d "/Applications/Google Chrome.app" ] || [ -d "$HOME/Applications/Google Chrome.app" ]; then
  echo "chrome: found"
else
  echo "chrome: not found. Install Google Chrome from google.com/chrome, then re-run this."
fi

step 2 "Installing the server and daily backup"
if [ "$dry" = 1 ]; then
  echo "would run: ops/install-launchagent.sh --with-backup"
else
  "$FL_OPS/install-launchagent.sh" --with-backup || die "the server did not install. Nothing else was changed. Log: $FL_LOG"
fi

step 3 "Health check"
if [ "$dry" = 1 ]; then
  echo "would run: ops/doctor.sh --quick"
else
  # Warnings are expected until the extension is loaded, so the result is shown, not enforced.
  "$FL_OPS/doctor.sh" --quick || echo "note: fix any FAIL above, then re-run ops/install.sh"
fi

step 4 "Next"
url="http://127.0.0.1:$FL_PORT/"
ext="$FL_REPO/extension"
if [ "$dry" = 1 ]; then
  echo "would open: $url"
  echo "extension folder: $ext"
  exit 0
fi
if [ "$open_apps" = 1 ]; then
  printf '%s' "$ext" | pbcopy 2>/dev/null && echo "extension folder copied to the clipboard"
  open -a "Google Chrome" "chrome://extensions" 2>/dev/null || true
  open "$url" 2>/dev/null || true
fi
cat <<MSG
Web app:   $url
Extension: in each Chrome profile, open chrome://extensions, turn on Developer mode,
           Load unpacked, choose: $ext
Then log in to instagram.com in that profile. The web app's Get started page ticks each step.
MSG
