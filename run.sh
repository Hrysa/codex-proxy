#!/bin/sh
set -eu
cd "$(dirname "$0")"
for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import tomllib' >/dev/null 2>&1; then
        exec "$candidate" codex_proxy.py "$@"
    fi
done
echo 'Python 3.11 or newer is required.' >&2
exit 1
