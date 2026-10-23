#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT_DIR="${ROOT_DIR}/.pytest-artifacts/demo"
GATE_SCRIPT="${ROOT_DIR}/scripts/run_demo_p2_gate.py"

mkdir -p "${ARTIFACT_DIR}"
timestamp="$(date +%Y%m%d-%H%M%S)"
out_json="${ARTIFACT_DIR}/p2-guardrails-${timestamp}.json"
out_log="${ARTIFACT_DIR}/p2-guardrails-${timestamp}.log"

echo "==> P2 Gate: risk freeze + pre-demo guardrails"
cd "${ROOT_DIR}"
"${ROOT_DIR}/.venv/bin/python" "${GATE_SCRIPT}" --output-json "${out_json}" | tee "${out_log}"

echo "==> P2 Gate: PASS"
echo "JSON=${out_json}"
echo "LOG=${out_log}"
