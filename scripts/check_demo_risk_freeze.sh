#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="check"
BASE_REF="HEAD"
BASELINE_FILE="${AETHER_DEMO_FREEZE_BASELINE_FILE:-${ROOT_DIR}/.run/demo_risk_freeze_baseline.txt}"
MAX_CHANGED_FILES="${AETHER_DEMO_MAX_CHANGED_FILES:-30}"
ALLOW_RAW="${AETHER_DEMO_FREEZE_ALLOW_PATTERNS:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --init)
      MODE="init"
      shift
      ;;
    --base-ref)
      BASE_REF="${2:-}"
      if [[ -z "${BASE_REF}" ]]; then
        echo "risk-freeze: --base-ref requires a value"
        exit 2
      fi
      shift 2
      ;;
    *)
      BASE_REF="$1"
      shift
      ;;
  esac
done

cd "${ROOT_DIR}"

if ! git rev-parse --verify "${BASE_REF}" >/dev/null 2>&1; then
  echo "risk-freeze: invalid base ref: ${BASE_REF}"
  exit 2
fi

collect_current_changes() {
  {
    git diff --name-only "${BASE_REF}"
    git ls-files --others --exclude-standard
  } | sed '/^$/d' | sort -u
}

tmp_current="$(mktemp)"
tmp_delta="$(mktemp)"
trap 'rm -f "${tmp_current}" "${tmp_delta}"' EXIT
collect_current_changes > "${tmp_current}"

current_count="$(wc -l < "${tmp_current}" | tr -d '[:space:]')"

if [[ "${MODE}" == "init" ]]; then
  mkdir -p "$(dirname "${BASELINE_FILE}")"
  cp "${tmp_current}" "${BASELINE_FILE}"
  echo "risk-freeze: baseline initialized"
  echo "base_ref=${BASE_REF}"
  echo "baseline_file=${BASELINE_FILE}"
  echo "baseline_entries=${current_count}"
  exit 0
fi

if [[ -f "${BASELINE_FILE}" ]]; then
  grep -Fxv -f "${BASELINE_FILE}" "${tmp_current}" > "${tmp_delta}" || true
  baseline_count="$(wc -l < "${BASELINE_FILE}" | tr -d '[:space:]')"
else
  cp "${tmp_current}" "${tmp_delta}"
  baseline_count="0"
fi

declare -a BLOCKED_PATTERNS=(
  "packages/aether/services/promotion_engine.py"
  "packages/aether/services/snapshot_service.py"
  "packages/aether/services/proposal_service.py"
  "packages/aether/models/entities.py"
  "packages/aether/crdt/*"
)

declare -a changed_files=()
while IFS= read -r file; do
  [[ -z "${file}" ]] && continue
  changed_files+=("${file}")
done < "${tmp_delta}"

is_allowed_override() {
  local file="$1"
  local allow
  if [[ -z "${ALLOW_RAW}" ]]; then
    return 1
  fi
  local old_ifs="${IFS}"
  IFS=','
  for allow in ${ALLOW_RAW}; do
    [[ -z "${allow}" ]] && continue
    if [[ "${file}" == ${allow} ]]; then
      IFS="${old_ifs}"
      return 0
    fi
  done
  IFS="${old_ifs}"
  return 1
}

declare -a violations=()
for file in "${changed_files[@]}"; do
  pattern_hit=""
  for pattern in "${BLOCKED_PATTERNS[@]}"; do
    if [[ "${file}" == ${pattern} ]]; then
      pattern_hit="${pattern}"
      break
    fi
  done
  if [[ -n "${pattern_hit}" ]] && ! is_allowed_override "${file}"; then
    violations+=("${file} (blocked by ${pattern_hit})")
  fi
done

if (( ${#changed_files[@]} > MAX_CHANGED_FILES )); then
  violations+=("new changed file count ${#changed_files[@]} exceeds AETHER_DEMO_MAX_CHANGED_FILES=${MAX_CHANGED_FILES}")
fi

if (( ${#violations[@]} > 0 )); then
  echo "risk-freeze: FAIL"
  echo "base_ref=${BASE_REF}"
  echo "baseline_file=${BASELINE_FILE}"
  echo "baseline_entries=${baseline_count}"
  echo "current_entries=${current_count}"
  echo "new_changes=${#changed_files[@]}"
  echo "violations:"
  for violation in "${violations[@]}"; do
    echo "  - ${violation}"
  done
  if [[ -n "${ALLOW_RAW}" ]]; then
    echo "allow_override_patterns=${ALLOW_RAW}"
  fi
  exit 1
fi

echo "risk-freeze: PASS"
echo "base_ref=${BASE_REF}"
echo "baseline_file=${BASELINE_FILE}"
echo "baseline_entries=${baseline_count}"
echo "current_entries=${current_count}"
echo "new_changes=${#changed_files[@]}"
if [[ -n "${ALLOW_RAW}" ]]; then
  echo "allow_override_patterns=${ALLOW_RAW}"
fi
