#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Missing .venv/bin/python. Activate or create project venv first." >&2
  exit 1
fi

mkdir -p .pytest-artifacts/chunk3

run_one() {
  local seed="$1"
  local out_json="$2"
  local out_log="$3"

  # Stability profile: submit throughput is tracked but non-blocking.
  .venv/bin/python -m apps.aether_cli.main swarm-harness \
    --agents 1000 --files 500 --run-seconds 3600 --drain-seconds 600 \
    --contention low --min-interval-ms 350 --max-interval-ms 1400 \
    --inflight 400 --promotion-batch 30 \
    --promotion-non-conflicting-batches 12 --promotion-max-conflict-sets 1 \
    --gate-execution-mode parallel --gate-max-parallel 4 \
    --promotion-interval-ms 40 --promotion-iteration-timeout-seconds 30 \
    --materialization-batch-size 2000 --materialization-interval-ms 10 \
    --monitor-interval-ms 500 \
    --min-submit-throughput 0 --min-promotion-throughput 0.15 \
    --max-submit-p95-ms 220 --max-conflict-rate 0.35 \
    --max-backlog-end 12000 --max-submit-failure-rate 0.02 \
    --seed "$seed" --output-json "$out_json" --strict | tee "$out_log"
}

rc1=0
rc2=0

run_one 3030 \
  ".pytest-artifacts/chunk3/chunk3-60m-pass1.json" \
  ".pytest-artifacts/chunk3/chunk3-60m-pass1.log" || rc1=$?

run_one 3031 \
  ".pytest-artifacts/chunk3/chunk3-60m-pass2.json" \
  ".pytest-artifacts/chunk3/chunk3-60m-pass2.log" || rc2=$?

echo
echo "Chunk 3 60m pair complete."
if [[ -f ".pytest-artifacts/chunk3/chunk3-60m-pass1.json" ]]; then
  jq -r '"pass1=\(.passed) submit_tps=\(.metrics.submit_throughput) promo_tps=\(.metrics.promotion_throughput) snapshot_ms=\(.metrics.promotion_phase_avg_ms.snapshot_ms) snapshot_mat_ms=\(.metrics.promotion_phase_avg_ms.snapshot_materialize_ms)"' \
    ".pytest-artifacts/chunk3/chunk3-60m-pass1.json"
fi
if [[ -f ".pytest-artifacts/chunk3/chunk3-60m-pass2.json" ]]; then
  jq -r '"pass2=\(.passed) submit_tps=\(.metrics.submit_throughput) promo_tps=\(.metrics.promotion_throughput) snapshot_ms=\(.metrics.promotion_phase_avg_ms.snapshot_ms) snapshot_mat_ms=\(.metrics.promotion_phase_avg_ms.snapshot_materialize_ms)"' \
    ".pytest-artifacts/chunk3/chunk3-60m-pass2.json"
fi

if [[ "$rc1" -ne 0 || "$rc2" -ne 0 ]]; then
  exit 1
fi
