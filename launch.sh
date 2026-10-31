#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
(sleep 3; xdg-open http://127.0.0.1:8765 >/dev/null 2>&1 || true) &
exec ./run.sh
