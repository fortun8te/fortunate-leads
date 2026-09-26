#!/bin/bash
# Start only this checkout's Tor instance; wait for authenticated bootstrap readiness.
set -euo pipefail
OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec "${PYTHON:-python3}" "$OPS_DIR/tor-status.py" start "$@"
