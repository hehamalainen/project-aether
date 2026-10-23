# Swarm Scalability Runbook

Operational playbook for running chunk gates and collecting reproducible artifacts.

## Preconditions

- Workspace: `<REPO_ROOT>`
- Virtual env activated: `source .venv/bin/activate`
- Laptop on power adapter
- Sleep prevention enabled during long soak

## Baseline Commands

### Test Baseline

```bash
python -m pytest tests/aether/test_services.py tests/aether/test_swarm_harness.py -q
```

### Burst Gate (1000 agents, strict)

```bash
python -m apps.aether_cli.main swarm-harness \
  --agents 1000 --files 500 --run-seconds 90 --drain-seconds 20 \
  --contention low --min-interval-ms 350 --max-interval-ms 1400 \
  --inflight 400 --promotion-batch 30 \
  --promotion-non-conflicting-batches 12 --promotion-max-conflict-sets 1 \
  --gate-execution-mode parallel --gate-max-parallel 4 \
  --promotion-interval-ms 40 --promotion-iteration-timeout-seconds 30 \
  --materialization-batch-size 2000 --materialization-interval-ms 10 \
  --monitor-interval-ms 500 \
  --min-submit-throughput 30 --min-promotion-throughput 0.15 \
  --max-submit-p95-ms 220 --max-conflict-rate 0.35 \
  --max-backlog-end 12000 --max-submit-failure-rate 0.02 \
  --seed 2026 --output-json .pytest-artifacts/swarm-burst-1000.json --strict
```

### Stability Soak (4h + drain)

```bash
python -m apps.aether_cli.main swarm-harness \
  --agents 1000 --files 500 --run-seconds 14400 --drain-seconds 1200 \
  --contention low --min-interval-ms 350 --max-interval-ms 1400 \
  --inflight 400 --promotion-batch 30 \
  --promotion-non-conflicting-batches 12 --promotion-max-conflict-sets 1 \
  --gate-execution-mode parallel --gate-max-parallel 4 \
  --promotion-interval-ms 40 --promotion-iteration-timeout-seconds 30 \
  --materialization-batch-size 2000 --materialization-interval-ms 10 \
  --monitor-interval-ms 500 \
  --min-submit-throughput 30 --min-promotion-throughput 0.15 \
  --max-submit-p95-ms 220 --max-conflict-rate 0.35 \
  --max-backlog-end 12000 --max-submit-failure-rate 0.02 \
  --seed 2026 --output-json .pytest-artifacts/soak/swarm-soak-4h-1000.json --strict
```

## Gate Review Checklist

For each JSON artifact, verify:

- `checks` block and `passed` status
- throughput metrics:
  - `submit_throughput`
  - `promotion_throughput`
- latency metrics:
  - `p95_submit_latency_ms`
  - `p95_ack_to_promote_latency_ms`
- queue stability:
  - `backlog_peak`
  - `backlog_end`
- promotion phase costs:
  - `promotion_phase_avg_ms.select_ms`
  - `promotion_phase_avg_ms.evaluate_ms`
  - `promotion_phase_avg_ms.snapshot_ms`
  - `promotion_phase_avg_ms.gates_ms`
  - `promotion_phase_avg_ms.commit_ms`

## Failure Triage Heuristics

- High `submit_p95`, low `submit_throughput`:
  - intake path blocked (API/DB lock contention).
- Normal `submit_p95`, low `submit_throughput`, high throttle:
  - adaptive throttle protecting backlog, promotion capacity is limiting.
- High `gates_ms`:
  - gate execution dominates; optimize gate ordering/parallelism.
- High `snapshot_ms`:
  - snapshot materialization dominates; optimize delta materialization.
- High `evaluate_ms` with moderate `snapshot_ms`:
  - candidate evaluation/selection logic or clone path dominates.

## Recordkeeping Contract

After each run:

1. Save artifact path and summary in `docs/swarm_chunk_status.md`.
2. Mark decision as `GO` or `NO-GO`.
3. If `NO-GO`, log corrective action before next run.
