#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "${ROOT_DIR}"

echo "==> P4 Gate: semantic/AIP regression + strict dual run"
source .venv/bin/activate
python scripts/run_demo_p4_gate.py "$@"
echo "==> P4 Gate: PASS"
