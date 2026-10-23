# Continuous 1000-Agent Flow Plan

## Goal

Prove that Aether can sustain continuous parallel coding flow where:

1. Proposals arrive continuously from a large swarm.
2. Promotions continue while intake is active.
3. Backlog remains bounded and reliability stays high.

This is the minimum bar for "faster than Git-style serialize/merge loops."

## Harness Model

Implemented in:

- `packages/aether/services/swarm_harness.py`
- `tests/aether/test_swarm_harness.py`

Simulation characteristics:

- 1,000 independent agents (configurable) emit proposals with jittered intervals.
- Promotion engine runs continuously in parallel with configurable batch and cadence.
- Backlog monitor samples pending/evaluating queue over time.
- Post-submit drain window verifies continuous promotion catch-up.

## SLO Gate (Default)

`ContinuousHarnessThresholds` defines pass/fail:

- `min_submit_throughput`: proposals/sec accepted during submission phase
- `min_promotion_throughput`: promoted proposals/sec across run + drain
- `max_submit_p95_ms`: p95 submission latency
- `max_conflict_rate`: conflict sets / successful submissions
- `max_backlog_end`: pending + evaluating at end of drain
- `max_submit_failure_rate`: failed submissions / total submissions

## How To Run

Run via CLI:

```bash
source .venv/bin/activate
aether swarm-harness --agents 1000 --files 500 --run-seconds 60 --drain-seconds 20 --output-json .pytest-artifacts/swarm-continuous-1000.json
```

Strict mode is on by default. Exit code `2` means SLO failure.

## Validation Progression

1. `100 agents / 10 min` continuous.
2. `500 agents / 20 min` continuous.
3. `1000 agents / 60 min` continuous.
4. Long soak (`>= 4h`) with identical config to production gate policy.

## Next Extensions

- Add real server mode (HTTP/WebSocket) alongside in-memory mode.
- Emit periodic CSV/JSON time-series for backlog/throughput trends.
- Add Git baseline harness to measure equivalent PR/merge throughput and conflict overhead.
