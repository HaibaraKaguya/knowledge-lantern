#!/usr/bin/env bash
# Start the local FastAPI process independently of the Windows shell that invokes WSL.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p data

nohup env PYTHONPATH=src HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  .venv/bin/python -m uvicorn src.server:app --host 0.0.0.0 --port 8000 \
  >> data/knowledge-lantern-restart.log 2>&1 < /dev/null &

echo $!
