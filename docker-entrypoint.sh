#!/bin/bash
set -euo pipefail
python3 -c 'from galaxy_runtime import prepare_runtime; prepare_runtime()'
if [ "$#" -eq 0 ]; then
    exec python3 /opt/galaxy/run_stack.py
fi
exec "$@"
