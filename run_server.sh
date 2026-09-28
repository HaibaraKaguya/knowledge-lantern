#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Missing .venv/bin/python. Create the virtual environment and install the project first." >&2
  exit 1
fi

export PYTHONPATH="$PROJECT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec .venv/bin/python -m uvicorn src.server:app --host "$HOST" --port "$PORT"
