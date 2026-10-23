#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT_DIR="${ROOT_DIR}/.pytest-artifacts/demo"
STACK_SCRIPT="${ROOT_DIR}/scripts/demo_stack.sh"
FLOW_SCRIPT="${ROOT_DIR}/scripts/run_demo_p1_flow.py"

mkdir -p "${ARTIFACT_DIR}"
timestamp="$(date +%Y%m%d-%H%M%S)"
out_json="${ARTIFACT_DIR}/p1-flow-${timestamp}.json"
out_log="${ARTIFACT_DIR}/p1-flow-${timestamp}.log"

echo "==> P1 Gate: stack health pre-check"
"${STACK_SCRIPT}" status

echo "==> P1 Gate: executing flow rehearsal"
cd "${ROOT_DIR}"
"${ROOT_DIR}/.venv/bin/python" "${FLOW_SCRIPT}" --output-json "${out_json}" | tee "${out_log}"

echo "==> P1 Gate: PASS"
echo "JSON=${out_json}"
echo "LOG=${out_log}"
