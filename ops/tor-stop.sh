#!/bin/bash
# Stop only the PID whose ownership and configuration match this checkout.
set -euo pipefail
OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec "${PYTHON:-python3}" "$OPS_DIR/tor-status.py" stop "$@"
