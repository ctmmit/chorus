#!/usr/bin/env bash
# path_test.sh — the golden path as an executable assertion. Delegates to the
# Python golden-path eval (scripts/golden_path.py), which drives the real API
# over the fixture model (POST /digest -> poll) and asserts latency, citation
# resolution, graceful skip, and refusal. Green or not. Runs every loop iteration.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PY="$PROJECT_DIR/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$PROJECT_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="python"

exec "$PY" "$PROJECT_DIR/scripts/golden_path.py"
