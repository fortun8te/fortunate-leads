#!/bin/bash
# Removes igscrape: the ~/ig-scraper folder (environment, cache, saved results, egresses.yaml) and the launcher.
set -eu
IG_HOME="${IG_HOME:-$HOME/ig-scraper}"
IG_BIN="${IG_BIN:-$HOME/.local/bin}"
echo "This deletes $IG_HOME (including results/ and egresses.yaml) and $IG_BIN/igscrape."
if [ -t 0 ] && [ -z "${IG_YES:-}" ]; then
  printf 'Continue? [y/N] '; read -r a; case "$a" in y|Y|yes) ;; *) echo "cancelled"; exit 0 ;; esac
fi
rm -rf "$IG_HOME" "$IG_BIN/igscrape"
for rc in "$HOME/.zshrc" "$HOME/.bash_profile" "$HOME/.bashrc" "$HOME/.profile"; do
  [ -f "$rc" ] && grep -q "added by igscrape install.sh" "$rc" && {
    grep -v "added by igscrape install.sh" "$rc" > "$rc.igtmp" && cat "$rc.igtmp" > "$rc" && rm -f "$rc.igtmp"; }
done
echo "igscrape removed."
