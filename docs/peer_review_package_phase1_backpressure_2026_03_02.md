# Phase 1 Backpressure Calibration - Peer Review Package

Date: 2026-03-02
Updated: 2026-03-04
Scope: Fast backpressure calibration (1000 agents), winner selection, strict verification run, nightly-suite staging.

## 1) Goal and gate criteria

Calibration gates used for candidate selection:

- `rho <= 1.0`
- `submit_p95_ms` down vs baseline
- `backlog_end` bounded (target <= 500; observed best = 0)
- `submit_throttled_rate` reduced vs baseline

## 2) Baseline (pre-calibration)

Artifacts:

- `.pytest-artifacts/phase1/phase1-60m-pass1-20260302-072930.json`
- `.pytest-artifacts/phase1/phase1-60m-pass2-20260302-072930.json`

Key baseline signals:

- pass1: `rho=1.0486`, `submit_p95_ms=6799.69`, `backlog_end=0`, `throttled_rate=0.9974`
- pass2: `rho=1.0439`, `submit_p95_ms=36743.29`, `backlog_end=0`, `throttled_rate=0.9968`

## 3) Calibration matrix evidence

Calibration artifacts:

- `.pytest-artifacts/phase1-calibration/calibration-20260302-195415-summary.tsv`
- `.pytest-artifacts/phase1-calibration/calibration-20260302-201653-summary.tsv`
- `.pytest-artifacts/phase1-calibration/calibration-20260302-202830-summary.tsv`
- `.pytest-artifacts/phase1-calibration/calibration-20260302-203709-summary.tsv`

Top feasible candidates (`rho <= 1.0` and `backlog_end=0`):

- `e1`: `rho=1.0`, `submit_p95_ms=3252.39`, `throttled_rate=0.9889`, `submit_tps=3.075`, `promo_tps=2.191`
- `e2`: `rho=1.0`, `submit_p95_ms=2665.94`, `throttled_rate=0.9932`, `submit_tps=2.325`, `promo_tps=1.650`

Selected winner: `e1` (better throttle reduction and higher submit/promotion throughput while keeping `rho<=1`).

## 4) Winner config

Backpressure controls:

- `AETHER_MAX_COHERENCE_BACKLOG=260`
- `AETHER_BACKLOG_RESUME_LEVEL=180`
- `AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0.40`
- `AETHER_RATIO_CONTROL_MIN_MU_TPS=0.05`
- `AETHER_RATIO_CONTROL_MIN_MU_EVENTS=1`

Harness intake throttle controls:

- `--adaptive-intake-throttle`
- `--intake-backlog-high-watermark 180`
- `--intake-backlog-low-watermark 140`

## 5) Strict verification of winner

Artifact:

- `.pytest-artifacts/phase1-calibration/verify-e1-20260302-204319.json`

Result:

- `passed=true`
- `rho=1.0`
- `submit_p95_ms=2417.45`
- `backlog_end=0`
- `throttled_rate=0.9893`
- all strict checks pass

## 6) Night-suite staging status

Script updated to use winner defaults and keep override flexibility:

- `scripts/run_phase1_night_suite.sh`

Changes include:

- winner default backpressure env values
- adaptive intake throttle default enabled
- explicit intake high/low watermark parameters
- existing continuation behavior retained (run all three phases even if one fails)

## 7) Formal review artifacts to share

Primary package directory:

- `.pytest-artifacts/peer-review/phase1-backpressure-20260302/`

Included:

- `trend_table.tsv` (baseline vs calibration vs strict verification)

Supporting run files:

- all JSON/LOG files under `.pytest-artifacts/phase1-calibration/` for 20260302 runs
- baseline strict phase artifacts under `.pytest-artifacts/phase1/phase1-*-20260302-072930.*`

## 8) Remaining known limits (for peer reviewers)

- throttling remains high (~98.9%) even after calibration; still improved from baseline (~99.7%).
- this phase improved boundedness + latency gates, but does not yet represent final 1000-agent long-run production envelope.
- next decision point remains tonight's `15m + 2x60m` strict run with winner defaults.

## 9) Addendum (2026-03-04): Step-2 tuning matrix rerun

New matrix artifact:

- `.pytest-artifacts/phase1-calibration/step2-calibration-20260304-111939-summary.tsv`

Matrix dimensions (backpressure-only; no architecture changes):

- ratio limit (`AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT`)
- backlog/resume watermarks
- intake high/low watermarks
- intake throttle check interval
- coherence window

Top feasible candidates (`rho <= 1.0`, bounded backlog):

- `m3`: `rho=1.0`, `submit_p95_ms=3939.80`, `backlog_end=0`, `throttled_rate=0.9945`, `submit_tps=2.0367`, `promo_tps=1.3660`
- `m6`: `rho=0.6625`, `submit_p95_ms=5798.47`, `backlog_end=0`, `throttled_rate=0.9896`, `submit_tps=2.4067`, `promo_tps=1.6019`

Selected for preflight:

- `m3`
- `AETHER_MAX_COHERENCE_BACKLOG=320`
- `AETHER_BACKLOG_RESUME_LEVEL=220`
- `AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0.45`
- intake high/low `220/170`
- intake throttle check interval `80ms`
- coherence window `60s`

## 10) Addendum (2026-03-04): 60m strict preflight and timeout finding

### 10.1 Preflight (direct harness invocation)

Artifact:

- `.pytest-artifacts/phase1/phase1-60m-preflight-20260304-121522.json`

Result:

- `passed=false` (strict failure: `submit_p95_ok`)
- `submit_p95_ms=5759.84` (> 5000 threshold)
- `rho=null` (window had zero `mu` sample at capture point)
- `backlog_end=214` (bounded; below 500 gate)
- `submit_tps=1.5658`, `promo_tps=1.1486`
- `submit_throttled_rate=0.9955`

Interpretation:

- Queue boundedness held; p95 latency still breached at 60m.
- Long-window sampling remains unstable (`rho` null in this run).

### 10.2 Safe-wrapper rerun (heartbeat + timeout guard)

Artifacts:

- `.pytest-artifacts/phase1/phase1-60m-preflight-safe-20260304-133325.log`
- `.pytest-artifacts/phase1/phase1-60m-preflight-safe-20260304-133325.heartbeat.log`

Result:

- run did not complete to JSON; wrapper hit timeout and terminated (`timeout_reached=1`).
- heartbeat shows prolonged stalls (long stretches with minimal log growth), then eventual forced termination.

Evidence sample from heartbeat:

- `t+3650s log_bytes=6257071 rc=None`
- `t+4658s log_bytes=6274048 rc=None`
- `t+6132s log_bytes=6534809 rc=None`
- `timeout_reached=1 terminating`

Operational action taken:

- all soak/demo processes were stopped and validated down before next cycle.

## 11) Peer-review package delta to share (2026-03-04)

Include these new artifacts in review handoff:

- `.pytest-artifacts/phase1-calibration/step2-calibration-20260304-111939-summary.tsv`
- `.pytest-artifacts/phase1/phase1-60m-preflight-20260304-121522.json`
- `.pytest-artifacts/phase1/phase1-60m-preflight-20260304-121522.log`
- `.pytest-artifacts/phase1/phase1-60m-preflight-safe-20260304-133325.log`
- `.pytest-artifacts/phase1/phase1-60m-preflight-safe-20260304-133325.heartbeat.log`

Reviewer focus request:

- explain `rho=null` capture cases under high throttle,
- explain long gate/promotion stalls in safe-wrapper run,
- propose minimal harness/runtime changes to guarantee completion + JSON emission for 60m strict preflight.
