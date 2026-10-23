#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -x ".venv/bin/python" ]]; then
  echo "error: .venv/bin/python not found; create/activate the project virtualenv first" >&2
  exit 1
fi

mkdir -p .pytest-artifacts/chunk3

run_case() {
  local label="$1"
  local agents="$2"
  local inflight="$3"
  local promotion_batch="$4"
  local min_submit="$5"
  local max_submit_p95="$6"
  local max_backlog_end="$7"
  local out_json="$8"
  local out_log="$9"

  echo "==> Running ${label}"
  set +e
  .venv/bin/python -m apps.aether_cli.main swarm-harness \
    --agents "${agents}" --files 500 --run-seconds 900 --drain-seconds 120 \
    --contention low --min-interval-ms 350 --max-interval-ms 1400 \
    --inflight "${inflight}" --promotion-batch "${promotion_batch}" \
    --promotion-non-conflicting-batches 12 --promotion-max-conflict-sets 1 \
    --gate-execution-mode parallel --gate-max-parallel 4 \
    --promotion-interval-ms 40 --promotion-iteration-timeout-seconds 30 \
    --materialization-batch-size 2000 --materialization-interval-ms 10 \
    --monitor-interval-ms 500 \
    --min-submit-throughput "${min_submit}" --min-promotion-throughput 0.15 \
    --max-submit-p95-ms "${max_submit_p95}" --max-conflict-rate 0.35 \
    --max-backlog-end "${max_backlog_end}" --max-submit-failure-rate 0.02 \
    --seed 2026 --output-json "${out_json}" --strict \
    | tee "${out_log}"
  local rc=$?
  set -e
  return "${rc}"
}

rc_a=0
rc_b=0

run_case \
  "Chunk 3 Stage A (100 agents, 15m)" \
  "100" \
  "150" \
  "20" \
  "20" \
  "180" \
  "2000" \
  ".pytest-artifacts/chunk3/chunk3-15m-stage-a.json" \
  ".pytest-artifacts/chunk3/chunk3-15m-stage-a.log" || rc_a=$?

run_case \
  "Chunk 3 Stage B (500 agents, 15m)" \
  "500" \
  "400" \
  "30" \
  "30" \
  "220" \
  "6000" \
  ".pytest-artifacts/chunk3/chunk3-15m-stage-b.json" \
  ".pytest-artifacts/chunk3/chunk3-15m-stage-b.log" || rc_b=$?

echo
echo "Chunk 3 15m A/B complete."
if [[ -f ".pytest-artifacts/chunk3/chunk3-15m-stage-a.json" ]]; then
  jq -r '"stage_a=\(.passed) submit_tps=\(.metrics.submit_throughput) promo_tps=\(.metrics.promotion_throughput) snapshot_ms=\(.metrics.promotion_phase_avg_ms.snapshot_ms) snapshot_mat_ms=\(.metrics.promotion_phase_avg_ms.snapshot_materialize_ms)"' \
    ".pytest-artifacts/chunk3/chunk3-15m-stage-a.json"
fi
if [[ -f ".pytest-artifacts/chunk3/chunk3-15m-stage-b.json" ]]; then
  jq -r '"stage_b=\(.passed) submit_tps=\(.metrics.submit_throughput) promo_tps=\(.metrics.promotion_throughput) snapshot_ms=\(.metrics.promotion_phase_avg_ms.snapshot_ms) snapshot_mat_ms=\(.metrics.promotion_phase_avg_ms.snapshot_materialize_ms)"' \
    ".pytest-artifacts/chunk3/chunk3-15m-stage-b.json"
fi

if [[ "${rc_a}" -ne 0 || "${rc_b}" -ne 0 ]]; then
  exit 1
fi

