# Aether Artifact Index

## Purpose

This document is the single reference list for Aether runtime proof artifacts, distinguishing between:
1. **Committed, directly inspectable evidence files** included in this public release repository, and
2. **Historical local soak reports** (`.pytest-artifacts/*`) whose raw runtime JSON outputs are excluded from git via `.gitignore` and whose recorded configurations and summary metrics are preserved below for historical context.

> **Historical Commit SHA Provenance Note:** Because this public release repository is published as a clean single-root snapshot on `main`, any pre-release commit SHAs or branch names embedded in committed report filenames or headers (such as `024163b8a3ebf951923a8e6899051af2793f1748`, `ab03e58a8faf1c16a993d1ce3fd74c6a28b26fdf`, `52814ac0a1312906d9bbc406ac01e9f760c00d61`, or `soak/sprint-a-20260616`) refer to historical pre-release development commits and are not reachable Git objects in this repository's commit graph.

---

## 1. Committed & Directly Inspectable Evidence in This Release Tree

Readers can directly inspect or re-run the following tracked artifacts from a clean checkout:

- **Reproducible Gate Verification & Rejection Demo (Executable)**
  - [`examples/reproducible_gate_verification_demo.py`](../examples/reproducible_gate_verification_demo.py)
  - Demonstrates explicit `SIMULATED:` default gates vs. real `py_compile` + `pytest` gates rejecting a broken proposal (`GateStatus.FAIL`, canonical head unchanged) and promoting a fixed proposal (`_verification_mode == "EXECUTED"`).
  - Run: `python examples/reproducible_gate_verification_demo.py`
- **Semantic Merge & Contention Qualification Reports (SQLite & PostgreSQL)**
  - [`qualification/semantic_suite/report_sqlite_v2_024163b8a3ebf951923a8e6899051af2793f1748.json`](../qualification/semantic_suite/report_sqlite_v2_024163b8a3ebf951923a8e6899051af2793f1748.json) (historical pre-release commit `024163b8a3ebf951923a8e6899051af2793f1748`, `db_profile=sqlite`)
  - [`qualification/semantic_suite/report_postgres_v2_024163b8a3ebf951923a8e6899051af2793f1748.json`](../qualification/semantic_suite/report_postgres_v2_024163b8a3ebf951923a8e6899051af2793f1748.json) (historical pre-release commit `024163b8a3ebf951923a8e6899051af2793f1748`, `db_profile=postgres`)
  - [`qualification/semantic_suite/report_sqlite.json`](../qualification/semantic_suite/report_sqlite.json) and [`qualification/semantic_suite/report_postgres.json`](../qualification/semantic_suite/report_postgres.json)
  - Re-run SQLite qualification locally: `python qualification/semantic_suite/run.py --db-profile sqlite --output-dir /tmp`
- **Committed Phase 1 Swarm Harness Gate & Preflight Artifacts (`1000` Simulated Agents)**
  - [`peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json`](../peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json) (`60m` preflight JSON payload)
  - [`peer-review-artifacts/20260304/phase1-calibration/step2-calibration-20260304-111939-summary.tsv`](../peer-review-artifacts/20260304/phase1-calibration/step2-calibration-20260304-111939-summary.tsv)
  - [`peer-review-artifacts/20260304/phase1/phase1-60m-preflight-safe-20260304-133325.heartbeat.log`](../peer-review-artifacts/20260304/phase1/phase1-60m-preflight-safe-20260304-133325.heartbeat.log) and [`peer-review-artifacts/20260304/phase1/phase1-60m-preflight-safe-20260304-133325.log`](../peer-review-artifacts/20260304/phase1/phase1-60m-preflight-safe-20260304-133325.log)
- **Committed Phase 4 / 4D Verification Logs & Provenance Contracts**
  - [`docs/evidence/gate_provenance_table.md`](evidence/gate_provenance_table.md)
  - [`docs/evidence/exclusivity_audit.log`](evidence/exclusivity_audit.log)
  - [`docs/evidence/phase4/provenance_contract.md`](evidence/phase4/provenance_contract.md)
  - [`docs/evidence/phase4/battery_red_proof.log`](evidence/phase4/battery_red_proof.log), [`docs/evidence/phase4/fail_closed_red.log`](evidence/phase4/fail_closed_red.log), [`docs/evidence/phase4/pg_concurrency_red.log`](evidence/phase4/pg_concurrency_red.log)
  - [`docs/evidence/phase4d/pytest_green.log`](evidence/phase4d/pytest_green.log) and [`docs/evidence/phase4d/pytest_red.log`](evidence/phase4d/pytest_red.log)
- **Committed Rust/PostgreSQL Control Plane & Database Scaling Reports**
  - [`docs/aether_stage3_pg_baseline_closeout_2026_05_10.md`](aether_stage3_pg_baseline_closeout_2026_05_10.md)
  - [`docs/sprint4d_topology_baseline.md`](sprint4d_topology_baseline.md)
  - [`docs/sprint4e_mitigation_experiments.md`](sprint4e_mitigation_experiments.md)

---

## 2. Historical Local Soak Reports (Raw `.pytest-artifacts/*` Files Excluded from Git Release)

> **Availability Qualification:** The `.pytest-artifacts/` directory is git-ignored to keep multi-megabyte runtime soak dumps out of the source tree. The entries below are **historical local soak reports** whose raw `.pytest-artifacts/*.json` files are not bundled in this repository. Their recorded operating parameters and headline metrics are preserved below, and fresh runs can be generated locally with `./scripts/run_swarm_harness_safe.sh`.

### Canonical baseline historical soak runs (`AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0.425`, `1000` agents, `500` files)

- `25m` clean gate (historical local artifact: `.pytest-artifacts/phase1-repro/mx-intake-cleanup-25m-r0425-20260325.json`)
- `60m` clean gate (historical local artifact: `.pytest-artifacts/phase1-repro/mx-intake-cleanup-60m-r0425-20260325.json`)
- `4h` clean gate (historical local artifact: `.pytest-artifacts/phase1-suite/diag-4h-mark-evaluating-r0425-20260329c.json`)
- `8h` clean long-run pass (historical local artifact: `.pytest-artifacts/phase1-suite/soak-8h-mx-intake-r0425-20260331-stable.json`)
  - Recorded summary metrics: `submit_throughput = 0.8550`, `promotion_throughput = 0.8037`, `p95_submit_latency_ms = 1527.22`, `rho_window = 0.9961`, `backlog_end = 0`, `submit_failure_rate = 0.0`, `create_snapshot_quarantines = 0`, `materialize_gate_workspace_quarantines = 0`.

### Important diagnostic historical soak runs

- `10h` near-pass (historical local artifact: `.pytest-artifacts/phase1-suite/soak-10h-mx-intake-r0425-20260408-retry.json`)
  - Fully completed, but failed only `window_rho_ok` (`1.011` vs `1.0` threshold).
- `12h` threshold pass with surfaced late-window bug (historical local artifact: `.pytest-artifacts/phase1-suite/soak-12h-mx-intake-r0420-20260411-day.json`, ratio limit `0.420`)
  - Passed stored thresholds, but exposed `mark_evaluating` event-loop quarantine wave.
- `24h` endurance-complete throughput miss (historical local artifact: `.pytest-artifacts/phase1-suite/soak-24h-mx-intake-r0420-20260412.json`, ratio limit `0.420`)
  - Runtime completed cleanly with no quarantine wave, but failed submit/promotion throughput thresholds.

### Reproducing a local swarm-harness soak artifact

To generate a fresh soak JSON artifact locally on your own machine:

```bash
AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0.425 \
  python -m apps.aether_cli.main swarm-harness \
  --agents 100 --files 100 --run-seconds 60 --drain-seconds 30 \
  --contention low --submit-via-api-route --adaptive-intake-throttle \
  --output-json /tmp/aether-soak-smoke.json --strict
```

---

## Current interpretation

- The strongest historical Python control-plane long-run baseline is the `8h` report summarized above (`AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0.425`).
- The `10h` report remained close to the target envelope (`window_rho = 1.011`).
- The `12h` report surfaced a real harness/event-loop bug that has since been patched in the route-path harness wiring.
- The `24h` report demonstrated runtime completion on the tested path, but did not meet target throughput on that operating profile.

## Current evidence status

- Committed qualification & gate verification suites (`qualification/semantic_suite/`, `examples/reproducible_gate_verification_demo.py`, `tests/aether/`): **directly inspectable and reproducible in this release**
- Clean short/medium baseline (`15m` committed in `peer-review-artifacts/`; `25m`/`60m`/`4h`/`8h` historical local soak reports): **documented with configuration and metrics**
- `12h` runtime endurance: **observed in historical local run, not accepted as clean baseline due surfaced bug**
- `24h` target throughput profile: **not yet proven**
