#!/bin/bash
# igscrape installer (macOS + Linux). Safe to re-run: it upgrades in place and keeps your egresses.yaml.
#
#   bash install.sh
#
# Installs into ~/ig-scraper (change with IG_HOME=...) and puts the `igscrape` command in ~/.local/bin
# (change with IG_BIN=...). Uses `uv` if it is installed (fast, can fetch its own Python), otherwise
# python3 3.10 or newer. Options (environment variables):
#   IG_NO_UV=1        don't use uv even if it's installed
#   IG_PYTHON=/path   use this exact python
#   IG_AUTO_UV=1      if no suitable Python is found, install uv automatically without asking
#   IG_NO_PATH_EDIT=1 don't add ~/.local/bin to your shell startup file
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ORIG_PATH="$PATH"
IG_HOME="${IG_HOME:-$HOME/ig-scraper}"
IG_BIN="${IG_BIN:-$HOME/.local/bin}"
VENV="$IG_HOME/venv"
MIN_MINOR=10
umask 077

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ -d "$HERE/app/igscraper" ] && [ -d "$HERE/app/igscrape_app" ] \
  || die "install.sh must be run from the unpacked release folder (missing app/igscraper or app/igscrape_app)."

# Homebrew (incl. a custom prefix like /Users/mk/homebrew) and user-local bins, so we find uv/python there.
for d in "$HOME/homebrew/bin" /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin" "$HOME/.cargo/bin"; do
  case ":$PATH:" in *":$d:"*) ;; *) [ -d "$d" ] && PATH="$PATH:$d" ;; esac
done
export PATH

# ---- disk space (the install needs roughly 60 MB, ~100 MB more if uv has to download Python)
free_kb=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')
if [ -n "${free_kb:-}" ] && [ "$free_kb" -lt 204800 ]; then
  warn "only $((free_kb / 1024)) MB free on your disk. The install needs about 60-160 MB."
  [ "$free_kb" -lt 81920 ] && die "not enough free disk space ($((free_kb / 1024)) MB). Free up ~200 MB and re-run."
fi

py_ok() {  # $1 = python executable; succeeds if it is 3.10+ and has venv
  "$1" -c "import sys, venv; sys.exit(0 if sys.version_info >= (3, $MIN_MINOR) else 1)" >/dev/null 2>&1
}

find_python() {
  if [ -n "${IG_PYTHON:-}" ]; then echo "$IG_PYTHON"; return 0; fi
  for c in python3.13 python3.12 python3.11 python3.10 python3.14 python3; do
    p=$(command -v "$c" 2>/dev/null) || continue
    py_ok "$p" && { echo "$p"; return 0; }
  done
  return 1
}

find_uv() {
  [ -n "${IG_NO_UV:-}" ] && return 1
  command -v uv 2>/dev/null
}

install_uv() {  # prints only the uv path on stdout
  say >&2 "installing uv (a small, fast Python installer from astral.sh) into $IG_BIN ..."
  command -v curl >/dev/null 2>&1 || die "curl is missing; can't download uv."
  mkdir -p "$IG_BIN"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$IG_BIN" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh >&2 \
    || die "downloading uv failed. Check your internet connection, or install Python 3.$MIN_MINOR+ from python.org and re-run."
  [ -x "$IG_BIN/uv" ] || die "uv install finished but $IG_BIN/uv is missing."
  echo "$IG_BIN/uv"
}

# ---- choose uv or python
UV=""; PY=""
if [ -n "${IG_PYTHON:-}" ]; then
  py_ok "$IG_PYTHON" || die "IG_PYTHON=$IG_PYTHON is not Python 3.$MIN_MINOR+ (or lacks the venv module)."
  IG_NO_UV=1
fi
if UV=$(find_uv); then
  say "using uv: $UV"
elif PY=$(find_python); then
  say "using $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"
else
  echo
  warn "no Python 3.$MIN_MINOR+ found (macOS's built-in python3 is usually 3.9, which is too old)."
  answer=""
  if [ -n "${IG_AUTO_UV:-}" ]; then
    answer=y
  elif [ -t 0 ]; then
    printf 'Install "uv" now? It downloads its own Python (~100 MB) and touches nothing else. [Y/n] '
    read -r answer || answer=n
    [ -z "$answer" ] && answer=y
  fi
  case "$answer" in
    y|Y|yes|YES) UV=$(install_uv) ;;
    *) die "Python 3.$MIN_MINOR+ is required. Fix it with ONE of these, then re-run 'bash install.sh':
    1) IG_AUTO_UV=1 bash install.sh           (installs uv, which brings its own Python)
    2) brew install python@3.12               (if you use Homebrew)
    3) download Python from https://www.python.org/downloads/macos/" ;;
  esac
fi

mkdir -p "$IG_HOME" "$IG_BIN"
chmod 700 "$IG_HOME"
stage="$(mktemp -d "$IG_HOME/.install.XXXXXXXX")"
committed=0 old_app_moved=0 old_venv_moved=0 new_app_moved=0 new_venv_moved=0
launcher_changed=0
metadata_changed=0
launcher_tmp=""
metadata=(QUICKSTART.md egresses.example.yaml VERSION uninstall.sh egresses.yaml)
rollback() {
  if [ "$committed" = 1 ]; then
    [ "$new_app_moved" = 0 ] || rm -rf "$IG_HOME/app"
    [ "$new_venv_moved" = 0 ] || rm -rf "$VENV"
    [ "$old_app_moved" = 0 ] || mv "$stage/old.app" "$IG_HOME/app"
    [ "$old_venv_moved" = 0 ] || mv "$stage/old.venv" "$VENV"
  fi
  if [ "$committed" = 1 ]; then
    if [ "$launcher_changed" = 1 ]; then
      rm -f "$IG_BIN/igscrape"
      [ ! -e "$stage/old.launcher" ] && [ ! -L "$stage/old.launcher" ] || mv "$stage/old.launcher" "$IG_BIN/igscrape"
    fi
    if [ "$metadata_changed" = 1 ]; then
      for name in "${metadata[@]}"; do
        rm -f "$IG_HOME/$name"
        [ ! -e "$stage/metadata/$name" ] && [ ! -L "$stage/metadata/$name" ] || mv "$stage/metadata/$name" "$IG_HOME/$name"
      done
    fi
  fi
  [ -z "$launcher_tmp" ] || rm -f "$launcher_tmp"
  rm -rf "$stage"
}
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Build the entire replacement beside the live install. A failed download or
# self-test cannot remove the currently working app or environment.
say "creating replacement environment"
if [ -n "$UV" ]; then
  "$UV" venv --quiet --python ">=3.$MIN_MINOR" "$stage/venv" \
    || die "uv could not create the environment (it may need internet to fetch Python the first time)."
else
  "$PY" -m venv "$stage/venv" 2>/dev/null || die "'$PY -m venv' failed. On Debian/Ubuntu: sudo apt install python3-venv"
fi

# ---- dependencies (runtime only, no caches kept, to save disk)
say "installing dependencies (httpx, fastapi, uvicorn, pyyaml) ..."
if [ -n "$UV" ]; then
  "$UV" pip install --quiet --no-cache --python "$stage/venv/bin/python" -r "$HERE/requirements.txt" \
    || die "installing dependencies failed (needs internet). Re-run when online."
else
  "$stage/venv/bin/python" -m pip install --quiet --no-cache-dir --disable-pip-version-check -r "$HERE/requirements.txt" \
    || die "installing dependencies failed (needs internet). Re-run when online."
fi

# ---- the app itself: plain source folders, made importable through a .pth file (no build step)
say "staging the scraper"
mkdir -p "$stage/app"
cp -R "$HERE/app/igscraper" "$HERE/app/igscrape_app" "$stage/app/"
find "$stage/app" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
say "checking replacement before upgrade (offline)"
PYTHONPATH="$stage/app" "$stage/venv/bin/python" -m igscrape_app version \
  || die "replacement does not start; existing install was kept"
PYTHONPATH="$stage/app" "$stage/venv/bin/python" -m igscrape_app selftest \
  || die "replacement self-test failed; existing install was kept"

committed=1
[ ! -e "$IG_HOME/app" ] || { mv "$IG_HOME/app" "$stage/old.app"; old_app_moved=1; }
[ ! -e "$VENV" ] || { mv "$VENV" "$stage/old.venv"; old_venv_moved=1; }
mv "$stage/app" "$IG_HOME/app"
new_app_moved=1
mv "$stage/venv" "$VENV"
new_venv_moved=1
# venv console scripts and activation files retain the staging path after mv.
# Rewrite text launchers only; never follow Python executable symlinks.
"$VENV/bin/python" - "$stage/venv" "$VENV" <<'RELOCATE'
from pathlib import Path
import sys
old, new = sys.argv[1:]
for path in [*Path(new, "bin").iterdir(), Path(new, "pyvenv.cfg")]:
    if path.is_symlink() or not path.is_file():
        continue
    data = path.read_bytes()
    if b"\0" not in data and old.encode() in data:
        path.write_bytes(data.replace(old.encode(), new.encode()))
RELOCATE
SITE=$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
printf '%s\n' 'import os, sys; sys.path.insert(0, os.path.join(sys.prefix, "..", "app"))' > "$SITE/igscrape_app.pth"
"$VENV/bin/python" -m igscrape_app version || die "installed replacement does not start; restoring previous install"
"$VENV/bin/python" -m igscrape_app selftest || die "installed replacement self-test failed; restoring previous install"

mkdir "$stage/metadata"
for name in "${metadata[@]}"; do
  if [ -e "$IG_HOME/$name" ] || [ -L "$IG_HOME/$name" ]; then
    cp -pP "$IG_HOME/$name" "$stage/metadata/$name"
  fi
done
metadata_changed=1
cp "$HERE/QUICKSTART.md" "$IG_HOME/QUICKSTART.md"
cp "$HERE/egresses.example.yaml" "$IG_HOME/egresses.example.yaml"
[ -f "$HERE/VERSION" ] && cp "$HERE/VERSION" "$IG_HOME/VERSION"
cp "$HERE/uninstall.sh" "$IG_HOME/uninstall.sh" && chmod +x "$IG_HOME/uninstall.sh"
if [ -f "$IG_HOME/egresses.yaml" ]; then
  say "keeping your existing $IG_HOME/egresses.yaml (new template: egresses.example.yaml)"
else
  cp "$HERE/egresses.example.yaml" "$IG_HOME/egresses.yaml"
fi
chmod 600 "$IG_HOME/egresses.yaml"

# ---- launcher
launcher_tmp="$(mktemp "$IG_BIN/.igscrape.XXXXXXXX")"
"$VENV/bin/python" - "$launcher_tmp" "$IG_HOME" <<'LAUNCHER'
from pathlib import Path
import shlex
import sys
Path(sys.argv[1]).write_text("#!/bin/sh\n" +
    "IG_HOME=${IG_HOME:-" + shlex.quote(sys.argv[2]) + "}\nexport IG_HOME\n" +
    'exec "$IG_HOME/venv/bin/python" -m igscrape_app "$@"\n')
LAUNCHER
chmod 700 "$launcher_tmp"
if [ -e "$IG_BIN/igscrape" ] || [ -L "$IG_BIN/igscrape" ]; then
  cp -pP "$IG_BIN/igscrape" "$stage/old.launcher"
fi
launcher_changed=1
mv -f "$launcher_tmp" "$IG_BIN/igscrape"

# Keep backups until the public command passes its final checks.
say "checking the install (offline self-test, no Instagram requests) ..."
"$IG_BIN/igscrape" version || die "the igscrape command does not start; restoring previous install"
"$IG_BIN/igscrape" selftest || die "final self-test failed; restoring previous install"
committed=0
rm -rf "$stage"
trap - EXIT INT TERM

# ---- make sure ~/.local/bin is on PATH for new terminals
path_note=""
case ":$ORIG_PATH:" in
  *":$IG_BIN:"*) ;;
  *)
    if [ -z "${IG_NO_PATH_EDIT:-}" ]; then
      line="export PATH=\"$IG_BIN:\$PATH\"  # added by igscrape install.sh"
      edited=""
      for rc in "$HOME/.zshrc" "$HOME/.bash_profile" "$HOME/.bashrc"; do
        case "$rc" in
          */.zshrc) [ "$(uname)" = Darwin ] || [ -f "$rc" ] || continue ;;   # zsh is the macOS default
          *) [ -f "$rc" ] || continue ;;
        esac
        grep -qs "added by igscrape install.sh" "$rc" || printf '\n%s\n' "$line" >> "$rc"
        edited=1
      done
      if [ -z "$edited" ]; then
        grep -qs "added by igscrape install.sh" "$HOME/.profile" || printf '\n%s\n' "$line" >> "$HOME/.profile"
      fi
      path_note="Open a NEW terminal window (or run:  export PATH=\"$IG_BIN:\$PATH\")  so the igscrape command is found."
    else
      path_note="Add $IG_BIN to your PATH to use the igscrape command."
    fi
    ;;
esac


echo
say "installed! (self-test passed)"
echo "   try:   igscrape nasa natgeo"
echo "   or:    igscrape ui          (opens a page in your browser)"
echo "   help:  igscrape --help      guide: $IG_HOME/QUICKSTART.md"
[ -n "$path_note" ] && { echo; echo "   $path_note"; }
exit 0
