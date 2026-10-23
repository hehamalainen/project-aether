#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK_SCRIPT="${ROOT_DIR}/scripts/demo_stack.sh"
KEEPALIVE_SCRIPT="${ROOT_DIR}/scripts/demo_keepalive.sh"
RUN_DIR="${ROOT_DIR}/.run"
LOG_DIR="${RUN_DIR}/logs"
KEEPALIVE_LOG="${LOG_DIR}/demo_keepalive_gate.log"

STATUS_CHECKS="${AETHER_P0_STATUS_CHECKS:-3}"
STATUS_INTERVAL_SECONDS="${AETHER_P0_STATUS_INTERVAL_SECONDS:-5}"
RECOVERY_TEST="${AETHER_P0_RECOVERY_TEST:-false}"
RECOVERY_TIMEOUT_SECONDS="${AETHER_P0_RECOVERY_TIMEOUT_SECONDS:-90}"
KEEPALIVE_INTERVAL_SECONDS="${AETHER_DEMO_KEEPALIVE_INTERVAL_SECONDS:-5}"

mkdir -p "${LOG_DIR}"

cleanup() {
  local keepalive_pid="${1:-}"
  if [[ -n "${keepalive_pid}" ]] && kill -0 "${keepalive_pid}" 2>/dev/null; then
    kill "${keepalive_pid}" 2>/dev/null || true
    wait "${keepalive_pid}" 2>/dev/null || true
  fi
}

echo "==> P0 Gate: start stack"
"${STACK_SCRIPT}" start

echo "==> P0 Gate: ${STATUS_CHECKS} consecutive healthy checks"
for ((i = 1; i <= STATUS_CHECKS; i++)); do
  echo "check ${i}/${STATUS_CHECKS}"
  "${STACK_SCRIPT}" status
  sleep "${STATUS_INTERVAL_SECONDS}"
done

if [[ "${RECOVERY_TEST}" == "true" ]]; then
  echo "==> P0 Gate: recovery test enabled (keepalive + restart from down state)"
  AETHER_DEMO_KEEPALIVE_INTERVAL_SECONDS="${KEEPALIVE_INTERVAL_SECONDS}" \
    "${KEEPALIVE_SCRIPT}" >"${KEEPALIVE_LOG}" 2>&1 &
  keepalive_pid="$!"
  trap 'cleanup "${keepalive_pid}"' EXIT

  echo "forcing stack down"
  "${STACK_SCRIPT}" stop

  echo "waiting for keepalive recovery (timeout=${RECOVERY_TIMEOUT_SECONDS}s)"
  start_ts="$(date +%s)"
  recovered="false"
  while true; do
    if "${STACK_SCRIPT}" status >/dev/null 2>&1; then
      recovered="true"
      break
    fi
    if (( "$(date +%s)" - start_ts >= RECOVERY_TIMEOUT_SECONDS )); then
      break
    fi
    sleep 2
  done

  if [[ "${recovered}" != "true" ]]; then
    echo "P0 Gate FAILED: keepalive did not recover stack in time."
    exit 1
  fi

  echo "recovered; final status"
  "${STACK_SCRIPT}" status
fi

echo "P0 Gate PASSED"
