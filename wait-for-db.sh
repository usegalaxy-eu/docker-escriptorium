#!/bin/bash
set -euo pipefail
state_dir="${SESSION_STATE_DIR:-/run/escriptorium}"
until [ -f "$state_dir/database-ready" ]; do
    if [ -f "$state_dir/database-failed" ]; then
        cat "$state_dir/database-failed" >&2
        exit 1
    fi
    sleep 1
done
exec "$@"
