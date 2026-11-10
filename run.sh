#!/usr/bin/env bash
# Contract: ./run.sh <SIM_URL> starts the controller and runs until the scenario finishes.
#
# No Docker needed: Python 3.10+ with the packages in requirements.txt (numpy, httpx; the rest are
# the simulator's). State lives in one SQLite file, state/<scenario>.db by default (override with
# CTL_DB=...). Re-running this script after a kill resumes from that file; pointing it at a fresh
# simulator archives the old file and starts clean.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
PY="${PYTHON:-$(command -v python3 || command -v python)}"
mkdir -p state
exec "$PY" -m controller run --url "$SIM_URL" --db "${CTL_DB:-auto}" --step "${CTL_STEP:-60}"
