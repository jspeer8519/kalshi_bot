#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
umask 077
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
# Fewer malloc arenas: stops a long-running, multi-threaded app from holding hundreds of MB of freed memory.
export MALLOC_ARENA_MAX=2
exec .venv/bin/python app.py
