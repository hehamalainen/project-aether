#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "error: .venv/bin/python not found; create/activate the project virtualenv first" >&2
  exit 1
fi

mkdir -p .pytest-artifacts/phase1

if [[ "${AETHER_PHASE1_FAST:-0}" == "1" ]]; then
  STAGE15_RUN_SECONDS=120
  STAGE15_DRAIN_SECONDS=30
  STAGE60_RUN_SECONDS=180
  STAGE60_DRAIN_SECONDS=45
  echo "phase1-night-suite: FAST mode enabled"
else
  STAGE15_RUN_SECONDS=900
  STAGE15_DRAIN_SECONDS=120
  STAGE60_RUN_SECONDS=3600
  STAGE60_DRAIN_SECONDS=600
fi

export AETHER_MAX_COHERENCE_BACKLOG="${AETHER_MAX_COHERENCE_BACKLOG:-260}"
export AETHER_BACKLOG_RESUME_LEVEL="${AETHER_BACKLOG_RESUME_LEVEL:-180}"
export AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT="${AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT:-0.40}"
export AETHER_RATIO_CONTROL_MIN_MU_TPS="${AETHER_RATIO_CONTROL_MIN_MU_TPS:-0.05}"
export AETHER_RATIO_CONTROL_MIN_MU_EVENTS="${AETHER_RATIO_CONTROL_MIN_MU_EVENTS:-1}"
export AETHER_COHERENCE_WINDOW_SECONDS="${AETHER_COHERENCE_WINDOW_SECONDS:-60}"
export AETHER_RETRY_AFTER_MAX_SECONDS="${AETHER_RETRY_AFTER_MAX_SECONDS:-300}"

PHASE1_ADAPTIVE_INTAKE_THROTTLE="${AETHER_PHASE1_ADAPTIVE_INTAKE_THROTTLE:-1}"
PHASE1_INTAKE_HIGH_WATERMARK="${AETHER_PHASE1_INTAKE_HIGH_WATERMARK:-180}"
PHASE1_INTAKE_LOW_WATERMARK="${AETHER_PHASE1_INTAKE_LOW_WATERMARK:-140}"

ADAPTIVE_INTAKE_FLAG="--adaptive-intake-throttle"
if [[ "${PHASE1_ADAPTIVE_INTAKE_THROTTLE}" == "0" ]]; then
  ADAPTIVE_INTAKE_FLAG="--no-adaptive-intake-throttle"
fi

PHYSICS_WINDOW_SECONDS="${AETHER_PHASE1_PHYSICS_WINDOW_SECONDS:-300}"
MAX_WINDOW_RHO="${AETHER_PHASE1_MAX_WINDOW_RHO:-1.0}"
MIN_SUBMIT_TPS="${AETHER_PHASE1_MIN_SUBMIT_TPS:-0.8}"
MIN_PROMO_TPS="${AETHER_PHASE1_MIN_PROMO_TPS:-0.8}"
MAX_SUBMIT_P95_MS="${AETHER_PHASE1_MAX_SUBMIT_P95_MS:-5000}"
MAX_CONFLICT_RATE="${AETHER_PHASE1_MAX_CONFLICT_RATE:-0.35}"
MAX_BACKLOG_END="${AETHER_PHASE1_MAX_BACKLOG_END:-500}"
MAX_SUBMIT_FAILURE_RATE="${AETHER_PHASE1_MAX_SUBMIT_FAILURE_RATE:-0.05}"

run_case() {
  local label="$1"
  local run_seconds="$2"
  local drain_seconds="$3"
  local seed="$4"
  local out_json="$5"
  local out_log="$6"
  local rc=0

  echo
  echo "==> ${label}"
  set +e
  .venv/bin/python -m apps.aether_cli.main swarm-harness \
    --agents 1000 --files 500 --run-seconds "${run_seconds}" --drain-seconds "${drain_seconds}" \
    --contention low --min-interval-ms 350 --max-interval-ms 1400 \
    --inflight 250 --promotion-batch 30 \
    --promotion-non-conflicting-batches 12 --promotion-max-conflict-sets 1 \
    --gate-execution-mode parallel --gate-max-parallel 4 \
    --promotion-interval-ms 40 --promotion-iteration-timeout-seconds 30 \
    --materialization-batch-size 2000 --materialization-interval-ms 10 \
    --monitor-interval-ms 500 \
    "${ADAPTIVE_INTAKE_FLAG}" \
    --intake-backlog-high-watermark "${PHASE1_INTAKE_HIGH_WATERMARK}" \
    --intake-backlog-low-watermark "${PHASE1_INTAKE_LOW_WATERMARK}" \
    --physics-window-seconds "${PHYSICS_WINDOW_SECONDS}" \
    --min-submit-throughput "${MIN_SUBMIT_TPS}" \
    --min-promotion-throughput "${MIN_PROMO_TPS}" \
    --min-mu-eff 0.5 \
    --max-submit-p95-ms "${MAX_SUBMIT_P95_MS}" \
    --max-conflict-rate "${MAX_CONFLICT_RATE}" \
    --max-backlog-end "${MAX_BACKLOG_END}" \
    --max-submit-failure-rate "${MAX_SUBMIT_FAILURE_RATE}" \
    --max-window-rho "${MAX_WINDOW_RHO}" \
    --seed "${seed}" --output-json "${out_json}" --strict | tee "${out_log}"
  rc=$?
  set -e
  return "${rc}"
}

print_summary() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    return 0
  fi
  jq -r '"passed=\(.passed) submit_tps=\(.metrics.submit_throughput) promo_tps=\(.metrics.promotion_throughput) complete_tps=\(.metrics.completion_throughput) rho=\(.metrics.rho_window) submit_p95_ms=\(.metrics.p95_submit_latency_ms) backlog_end=\(.metrics.backlog_end) throttled=\(.metrics.submit_throttled) throttled_rate=\(.metrics.submit_throttled_rate)"' "${path}"
}

ts="$(date +%Y%m%d-%H%M%S)"
stage15_json=".pytest-artifacts/phase1/phase1-15m-${ts}.json"
stage15_log=".pytest-artifacts/phase1/phase1-15m-${ts}.log"
rc15=0
rc60_1=0
rc60_2=0

run_case "Phase 1 gate (15m strict)" "${STAGE15_RUN_SECONDS}" "${STAGE15_DRAIN_SECONDS}" 4101 "${stage15_json}" "${stage15_log}" || rc15=$?
print_summary "${stage15_json}"

pass1_json=".pytest-artifacts/phase1/phase1-60m-pass1-${ts}.json"
pass1_log=".pytest-artifacts/phase1/phase1-60m-pass1-${ts}.log"
pass2_json=".pytest-artifacts/phase1/phase1-60m-pass2-${ts}.json"
pass2_log=".pytest-artifacts/phase1/phase1-60m-pass2-${ts}.log"

run_case "Phase 1 long pass 1 (60m strict)" "${STAGE60_RUN_SECONDS}" "${STAGE60_DRAIN_SECONDS}" 4102 "${pass1_json}" "${pass1_log}" || rc60_1=$?
print_summary "${pass1_json}"

run_case "Phase 1 long pass 2 (60m strict)" "${STAGE60_RUN_SECONDS}" "${STAGE60_DRAIN_SECONDS}" 4103 "${pass2_json}" "${pass2_log}" || rc60_2=$?
print_summary "${pass2_json}"

echo
echo "phase1-night-suite: complete"
echo "artifacts:"
echo "  ${stage15_json}"
echo "  ${pass1_json}"
echo "  ${pass2_json}"

if [[ "${rc15}" -ne 0 || "${rc60_1}" -ne 0 || "${rc60_2}" -ne 0 ]]; then
  echo "phase1-night-suite: one or more strict runs failed"
  exit 1
fi
