#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADR_FILE="${ROOT_DIR}/docs/adr_2026_02_agentic_semantic_control_plane.md"
CHECKLIST_FILE="${ROOT_DIR}/docs/research_execution_checklist.md"

fail() {
  echo "research-alignment: FAIL"
  echo "reason=$1"
  exit 1
}

[[ -f "${ADR_FILE}" ]] || fail "missing ADR file (${ADR_FILE})"
[[ -f "${CHECKLIST_FILE}" ]] || fail "missing checklist file (${CHECKLIST_FILE})"

grep -Eq "^## Falsification Criteria" "${ADR_FILE}" || fail "ADR missing falsification criteria section"
grep -Eq "^- Status: Accepted" "${ADR_FILE}" || fail "ADR status is not accepted"

grep -Eq "^- \\[x\\] P0 uptime gate passed" "${CHECKLIST_FILE}" || fail "P0 completion not marked in checklist"
grep -Eq "^- \\[x\\] P1 flow gate passed" "${CHECKLIST_FILE}" || fail "P1 completion not marked in checklist"
grep -Eq "^- \\[x\\] P2 guardrail gate passed" "${CHECKLIST_FILE}" || fail "P2 completion not marked in checklist"
if ! grep -Eq "^- \\[(x| )\\] P3 fallback package locked" "${CHECKLIST_FILE}"; then
  fail "P3 checklist entry missing"
fi

echo "research-alignment: PASS"
echo "adr=${ADR_FILE}"
echo "checklist=${CHECKLIST_FILE}"
