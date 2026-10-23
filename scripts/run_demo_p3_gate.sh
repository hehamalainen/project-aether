#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT_DIR="${ROOT_DIR}/.pytest-artifacts/demo"
GATE_SCRIPT="${ROOT_DIR}/scripts/run_demo_p3_gate.py"

mkdir -p "${ARTIFACT_DIR}"
timestamp="$(date +%Y%m%d-%H%M%S)"
out_json="${ARTIFACT_DIR}/p3-fallback-${timestamp}.json"
out_log="${ARTIFACT_DIR}/p3-fallback-${timestamp}.log"

echo "==> P3 Gate: fallback readiness"
cd "${ROOT_DIR}"
"${ROOT_DIR}/.venv/bin/python" "${GATE_SCRIPT}" --output-json "${out_json}" | tee "${out_log}"

echo "==> P3 Gate: PASS"
echo "JSON=${out_json}"
echo "LOG=${out_log}"
