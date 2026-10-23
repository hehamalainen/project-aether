#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK_SCRIPT="${ROOT_DIR}/scripts/demo_stack.sh"
INTERVAL_SECONDS="${AETHER_DEMO_KEEPALIVE_INTERVAL_SECONDS:-20}"

echo "demo-keepalive: monitoring every ${INTERVAL_SECONDS}s"
echo "demo-keepalive: press Ctrl+C to stop"

while true; do
  if ! "${STACK_SCRIPT}" status >/dev/null 2>&1; then
    "${STACK_SCRIPT}" start >/dev/null 2>&1 || true
  fi
  "${STACK_SCRIPT}" status || true
  sleep "${INTERVAL_SECONDS}"
done
