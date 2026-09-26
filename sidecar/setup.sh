#!/bin/sh
# Explicit installation only. Does not start the service or download model weights.
set -eu
SIDECAR_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LAYA_PYTHON=${LAYA_PYTHON:-python3}
"$LAYA_PYTHON" -m venv "$SIDECAR_DIR/.venv"
"$SIDECAR_DIR/.venv/bin/python" -m pip install -r "$SIDECAR_DIR/requirements.txt"
"$SIDECAR_DIR/.venv/bin/python" -c 'from importlib.metadata import version; assert version("laya") == "0.3.20"'
printf '%s\n' "Installed Laya 0.3.20. Model weights were not fetched." "Start with: $SIDECAR_DIR/.venv/bin/python $SIDECAR_DIR/laya_server.py"
