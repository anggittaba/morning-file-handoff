#!/usr/bin/env bash
# Rebuild the two frozen-clock databases, serve them, capture the screenshots, stop the servers.
set -euo pipefail
cd "$(dirname "$0")/.."
export HANDOFF_LLM=off
HANDOFF_VAR=var/shot-mid python -m handoff simulate --until 06:50 --out var/shot-mid/results.json >/dev/null
HANDOFF_VAR=var/shot-end python -m handoff simulate --out var/shot-end/results.json >/dev/null
HANDOFF_VAR=var/shot-mid HANDOFF_NOW=2026-10-08T06:50:00 python -m handoff serve --no-loop --port 8766 & MID=$!
HANDOFF_VAR=var/shot-end HANDOFF_NOW=2026-10-08T07:46:00 python -m handoff serve --no-loop --port 8767 & END=$!
trap 'kill $MID $END 2>/dev/null || true' EXIT
for port in 8766 8767; do
  for _ in $(seq 50); do curl -fs "http://127.0.0.1:$port/healthz" >/dev/null && break; sleep 0.2; done
done
python scripts/capture_screenshots.py "$@"
