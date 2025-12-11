#!/usr/bin/env bash
# Compatibility shim: forwards to the Python implementation.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/mpv_send_key.py" "$@"
