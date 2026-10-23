# Aether Soak Scheduling Policy - 2026-03-26

## Summary

Long Aether soaks on the current single-machine setup are now treated as single-purpose night runs.

The objective is to preserve canonical Aether evidence:
- one workload class per run;
- no overlapping MX proof/scaling traffic during canonical baseline soaks;
- explicit naming and opt-in for later mixed-source stress runs.

## Run classes

### Canonical baseline

Use when the goal is clean Aether evidence.

Rules:
- run only the Aether harness;
- no concurrent MX worker-load, live-proof, or scale-control-plane traffic against the same Aether instance;
- run overnight only on the shared MacBook;
- current duration ladder:
  - 8h
  - 10-12h
  - 24h
  - multi-day

Environment:
- `AETHER_SWARM_RUN_CLASS=canonical_baseline` (default)

### Mixed-source stress

Use only after the canonical baseline is stable again.

Rules:
- explicitly named as mixed traffic;
- separate source tags and run IDs are required;
- intended to measure interaction between MX traffic and synthetic harness pressure, not baseline Aether performance.

Environment:
- `AETHER_SWARM_RUN_CLASS=mixed_source_stress`
- `AETHER_ALLOW_MIXED_SOURCE_STRESS=1`

## Enforcement

`./scripts/run_swarm_harness_safe.sh` now enforces the policy for long runs:
- default long-run threshold: `4h`
- default night window: `20:00` to `08:59`
- canonical baseline runs are blocked if active MX processes are detected

Matching MX processes currently include:
- `scale-control-plane`
- `scale-worker-load`
- `run_aether_live_proof`
- `run_aether_worker_proof`
- `run_aether_adapter_proof`
- `run_delivery_proof`
- `mx aether-live-proof`
- `mx aether-worker-proof`
- `mx aether-adapter-proof`

Explicit overrides:
- `AETHER_ALLOW_DAYTIME_LONG_RUN=1`
- `AETHER_ALLOW_MIXED_SOURCE_STRESS=1`

Tunable parameters:
- `AETHER_SOAK_LONG_THRESHOLD_SECONDS` (default `14400`)
- `AETHER_SOAK_NIGHT_START_HOUR` (default `20`)
- `AETHER_SOAK_NIGHT_END_HOUR` (default `8`)

## Practical operating split

### Daytime
- MX experiments
- Aether focused tests
- Aether short gates only if MX is idle

### Night
- Aether long soak only
- charger connected
- lid open
- machine awake
- no concurrent MX background runs for canonical baseline

## Mixed-source design intent

When mixed-source runs are introduced, keep them separate from baseline artifacts:
- separate `source` tags:
  - `mx_swarm`
  - `synthetic_harness`
- separate `run_id`s
- separate repo IDs when possible

Acceptance should evaluate:
- total throughput
- `by_source` status counts
- quarantine counts by source
- whether one source degrades the other

## Current assumption

This policy is optimized for the current environment:
- one shared MacBook
- one local Aether instance
- long runs are only trustworthy if uninterrupted and isolated
