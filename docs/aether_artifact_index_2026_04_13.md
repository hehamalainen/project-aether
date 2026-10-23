# Aether Artifact Index

## Purpose

This document is the single reference list for Aether runtime proof artifacts in this public research-prototype release, classifying evidence into:
1. **Behavior reproducible on the current release candidate (`HEAD`)** via tests, scripts, and CLI commands,
2. **Committed, directly inspectable historical reports** included in this repository (recording both passes and failures accurately), and
3. **Historical local soak and external workspace summaries** (`.pytest-artifacts/*`, `<MX_SWARM_ROOT>/reports/*`) whose raw JSON outputs are excluded from git and are **not independently verifiable from this release**.

> **Historical Commit SHA & Redaction Provenance Note:**
> - **Single-Root Snapshot:** Because this public release repository is published as a clean single-root snapshot on `main`, pre-release commit SHAs or branch names embedded in committed report filenames or headers (such as `024163b8a3ebf951923a8e6899051af2793f1748`, `ab03e58a8faf1c16a993d1ce3fd74c6a28b26fdf`, `52814ac0a1312906d9bbc406ac01e9f760c00d61`, `e5643a9f3e76a674ccac527fa96828bfdc675b95`, `f8787e2b`, or `soak/sprint-a-20260616`) refer to historical pre-release development revisions and are not reachable Git objects in this repository's commit graph.
> - **Privacy Redaction Disclosure:** Local filesystem paths and usernames inside committed logs and reports were replaced with `<AETHER_WORKSPACE>`, `<AETHER_RUNTIME_ARTIFACTS>`, `<MX_SWARM_ROOT>`, and `<REDACTED_USER>` during public-release sanitization. This redaction is why the `config_fingerprint` recorded inside the committed `report_*_v2_024163b8a3ebf951923a8e6899051af2793f1748.json` reports corresponds to the pre-redaction historical file state at commit `024163b8a3ebf951923a8e6899051af2793f1748`, while re-running `qualification/semantic_suite/run.py` today computes a fresh fingerprint over the current `HEAD` tree.
> - **Re-running Today vs. Historical Reproduction:** Running a test or qualification command today exercises the current `HEAD` code and produces a new runtime report; it does not retroactively verify unbundled historical soak artifacts.

---

## 1. Committed & Directly Inspectable Evidence in This Release Tree

Readers can directly inspect or re-run the following tracked artifacts from a clean checkout:

- **Reproducible Gate Verification & Rejection Demo (Executable on `HEAD`)**
  - [`examples/reproducible_gate_verification_demo.py`](../examples/reproducible_gate_verification_demo.py)
  - Demonstrates explicit `SIMULATED:` default gates vs. real `py_compile` + `pytest` gates on the Python reference backend rejecting a broken proposal (`GateStatus.FAIL`, canonical head unchanged) and promoting a fixed proposal (`_verification_mode == "EXECUTED"`).
  - Run: `python examples/reproducible_gate_verification_demo.py`
- **Semantic Merge & Contention Qualification Reports (SQLite & PostgreSQL — Deterministic Simulator)**
  - [`qualification/semantic_suite/report_sqlite_v2_024163b8a3ebf951923a8e6899051af2793f1748.json`](../qualification/semantic_suite/report_sqlite_v2_024163b8a3ebf951923a8e6899051af2793f1748.json) (historical pre-release commit `024163b8a3ebf951923a8e6899051af2793f1748`, `db_profile=sqlite`)
  - [`qualification/semantic_suite/report_postgres_v2_024163b8a3ebf951923a8e6899051af2793f1748.json`](../qualification/semantic_suite/report_postgres_v2_024163b8a3ebf951923a8e6899051af2793f1748.json) (historical pre-release commit `024163b8a3ebf951923a8e6899051af2793f1748`, `db_profile=postgres`)
  - [`qualification/semantic_suite/report_sqlite.json`](../qualification/semantic_suite/report_sqlite.json) and [`qualification/semantic_suite/report_postgres.json`](../qualification/semantic_suite/report_postgres.json)
  - **What these reports actually exercise:** Each report records `"model_configuration": "simulated-synthesis-worker"` (`report_sqlite.json` also records `"gate_configuration": "echo 'ok' (test_gate, timeout=60s)"`, as configured across all 4 cases in `qualification/semantic_suite/run.py`). Case 3 (`synthesis_fallback_merges`) exercises the deterministic Python synthesis-worker simulator in [`packages/aether/synthesis_worker/main.py`](../packages/aether/synthesis_worker/main.py) (`model_version="1.0-fake"`), which concatenates non-deleted diff additions and runs `ast.parse`; it does **not** invoke a live LLM or run a real application test suite.
  - Re-run SQLite qualification locally on `HEAD`: `python qualification/semantic_suite/run.py --db-profile sqlite --output-dir /tmp`
- **Committed Phase 1 Swarm Harness Gate & Preflight Artifacts (`1000` Simulated Agents — Historical Run Failed Strict Gate)**
  - [`peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json`](../peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json)
    - **Recorded Verdict (`passed: false`):** This committed `60m` preflight report (`3,600s` active + `600s` drain = `4,204.29s` total, `1,000` simulated agents, `500` files, `contention=low`, `submit_via_api_route=true`, `adaptive_intake_throttle=true`) **FAILED** its strict SLO gate because `submit_p95_ok` was `false`: P95 submit latency reached **`5,759.84 ms` (~`5.76s`)** against the `5,000.0 ms` threshold (`p50 = 1,414.43 ms`, `p99 = 9,205.51 ms`).
    - **Recorded Throughput & Throttling:** Out of `1,334,063` total loop iterations (`submitted_total`), **`1,328,003` (`99.55%`) were throttled at intake** (`submit_throttled`), **`5,637` proposals were accepted** (`1.57` accepted/s during the `3,600s` active window; `1.34`/s across the full `4,204.29s` run), `422` submissions failed (`submit_failures`), and **`4,829` proposals were promoted** across `684` promotion batches (**`promotion_throughput = 1.15` proposals promoted/s**, or **`0.16` promotion batches/s** over `4,204.29s`), leaving `214` `PENDING`, `589` `SUPERSEDED`, and `428` `QUARANTINED`. This artifact is evidence of intake backpressure behavior and submit-tail latency under overload, **not** a passing 1,000-agent benchmark.
  - [`peer-review-artifacts/20260304/phase1-calibration/step2-calibration-20260304-111939-summary.tsv`](../peer-review-artifacts/20260304/phase1-calibration/step2-calibration-20260304-111939-summary.tsv) (`900s` calibration sweep across ratio limits `0.40`, `0.45`, and `0.50`)
  - [`peer-review-artifacts/20260304/phase1/phase1-60m-preflight-safe-20260304-133325.heartbeat.log`](../peer-review-artifacts/20260304/phase1/phase1-60m-preflight-safe-20260304-133325.heartbeat.log) and [`peer-review-artifacts/20260304/phase1/phase1-60m-preflight-safe-20260304-133325.log`](../peer-review-artifacts/20260304/phase1/phase1-60m-preflight-safe-20260304-133325.log) (operator wrapper logs from a subsequent run interrupted at `elapsed_sec=2095` with `rc=143`)
- **Committed Phase 4 / 4D Verification Logs & Provenance Contracts**
  - [`docs/evidence/gate_provenance_table.md`](evidence/gate_provenance_table.md)
  - [`docs/evidence/exclusivity_audit.log`](evidence/exclusivity_audit.log)
  - [`docs/evidence/phase4/provenance_contract.md`](evidence/phase4/provenance_contract.md)
  - [`docs/evidence/phase4/battery_red_proof.log`](evidence/phase4/battery_red_proof.log), [`docs/evidence/phase4/fail_closed_red.log`](evidence/phase4/fail_closed_red.log), [`docs/evidence/phase4/pg_concurrency_red.log`](evidence/phase4/pg_concurrency_red.log)
  - [`docs/evidence/phase4d/pytest_green.log`](evidence/phase4d/pytest_green.log) and [`docs/evidence/phase4d/pytest_red.log`](evidence/phase4d/pytest_red.log)
- **Committed Rust/PostgreSQL Control Plane & Database Scaling Reports (Synthetic Harness Summary Docs — Raw Logs Not Bundled)**
  - [`docs/aether_stage3_pg_baseline_closeout_2026_05_10.md`](aether_stage3_pg_baseline_closeout_2026_05_10.md)
    - Summarizes a `3,600s + 30s` drain run of [`scripts/soak_native_rust_pg.sh`](../scripts/soak_native_rust_pg.sh) (`50` agent loops across `10` repos) submitting 3-line synthetic `"full_file"` replacements (`src/agent_<id>_file.py`) with an `echo SOAK_GATE_OK` shell gate (`37,715` `submit_ok`, `17,363` `promote_ok`, `20,352` HTTP `409` responses = `53.96%` contention, using a hardcoded `175ms` fallback service time in the Poisson threshold calculation). Whether `SOAK_CRDT_ENABLED` / `AETHER_CRDT_MODE` was enabled in that run is not recorded in the summary.
  - [`docs/sprint4d_topology_baseline.md`](sprint4d_topology_baseline.md)
    - Directional `120s` topology sweep across 8 topologies (`N × R`), documenting severe PostgreSQL relation bloat (`15 MB` to `340 MB` at `100` agents / `1` repo) and high HTTP `409` contention (`90.1%–95.3%` at `N/R >= 50`).
  - [`docs/sprint4e_mitigation_experiments.md`](sprint4e_mitigation_experiments.md)
    - Follow-up `120s` mitigation experiments (`VACUUM` tuning and append-only prototype measurements) showing why multi-second P95 latency under high single-repo concurrency remained dominated by application-level serialization (`pg_advisory_xact_lock` + `tokio::process::Command` gate spawning).

---

## 2. Historical Local Soak Reports (Raw `.pytest-artifacts/*` Files Excluded from Git — Not Independently Verifiable from This Release)

> **Availability Qualification:** The `.pytest-artifacts/` directory and external `<MX_SWARM_ROOT>/reports/` directories are excluded from git. The entries below are **historical local soak summaries** whose raw JSON files are **not bundled in this repository and cannot be independently verified from this release**. They are retained strictly as historical development logs, including their failure modes.

### Historical Python synthetic soak summaries (`AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0.425`, `1000` simulated agents, `500` files)

- `25m` historical local summary (`.pytest-artifacts/phase1-repro/mx-intake-cleanup-25m-r0425-20260325.json` — raw JSON not in repo)
- `60m` historical local summary (`.pytest-artifacts/phase1-repro/mx-intake-cleanup-60m-r0425-20260325.json` — raw JSON not in repo)
- `4h` historical local summary (`.pytest-artifacts/phase1-suite/diag-4h-mark-evaluating-r0425-20260329c.json` — raw JSON not in repo)
- `8h` historical local summary (`.pytest-artifacts/phase1-suite/soak-8h-mx-intake-r0425-20260331-stable.json` — raw JSON not in repo)
  - Recorded summary metrics in historical notes: `submit_throughput = 0.8550` accepted proposals/s, `promotion_throughput = 0.8037` proposals promoted/s (metric counts promoted proposals/s, not promotion batches/s), `p95_submit_latency_ms = 1527.22`, `rho_window = 0.9961`, `backlog_end = 0`, `submit_failure_rate = 0.0`, `create_snapshot_quarantines = 0`, `materialize_gate_workspace_quarantines = 0`. (Note: under `AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0.425`, intake throttling caps accepted throughput below ~`1` proposal/s to keep the promotion queue bounded.)

### Historical diagnostic & failed long-run soak summaries (raw JSON not in repo)

- `10h` strict failure (`window_rho_ok` miss) (`.pytest-artifacts/phase1-suite/soak-10h-mx-intake-r0425-20260408-retry.json` — raw JSON not in repo)
  - Completed runtime window, but **failed** strict `window_rho_ok` (`1.011` vs `1.0` threshold).
- `12h` quarantine-wave bug exposure (`.pytest-artifacts/phase1-suite/soak-12h-mx-intake-r0420-20260411-day.json`, ratio limit `0.420` — raw JSON not in repo)
  - Exposed a late-window `mark_evaluating` event-loop quarantine wave (`198` quarantines) and is **not** a clean baseline.
- `24h` throughput failure (`.pytest-artifacts/phase1-suite/soak-24h-mx-intake-r0420-20260412.json`, ratio limit `0.420` — raw JSON not in repo)
  - Completed the 24-hour window without a quarantine wave, but **failed** strict submit and promotion throughput thresholds.

### Running a fresh local swarm-harness smoke check on `HEAD`

To run the Python `swarm-harness` CLI today and generate a fresh local JSON report (this exercises current `HEAD` code in a new short run; it does not reproduce or validate the unbundled multi-hour historical files above):

```bash
AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0.425 \
  python -m apps.aether_cli.main swarm-harness \
  --agents 100 --files 100 --run-seconds 60 --drain-seconds 30 \
  --contention low --submit-via-api-route --adaptive-intake-throttle \
  --output-json /tmp/aether-soak-smoke.json --strict
```

---

## Current Evidence Status Summary

- **Executable unit/integration tests and gate verification demo** (`tests/aether/`, `aether-rs/crates/*/tests/`, `examples/reproducible_gate_verification_demo.py`): **directly inspectable and runnable on `HEAD`**.
- **Semantic merge & contention qualification reports** (`qualification/semantic_suite/report_*.json`): **committed and inspectable**, exercising LibCST AST merges, deterministic RGA fallback, and the **simulated synthesis worker** (`1.0-fake`) with `echo 'ok'` gates.
- **Committed `1000`-agent `60m` preflight artifact** (`peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json`): **committed and inspectable, and records a strict SLO failure (`passed: false`, P95 submit latency `5.76s` > `5.0s`, `99.55%` throttled, `1.15` proposals promoted/s across `684` promotion batches [`0.16` promotion batches/s])**.
- **Historical `25m` / `60m` / `4h` / `8h` / `10h` / `12h` / `24h` soak summaries** (`.pytest-artifacts/*`): **historical summaries only — raw JSON files are not bundled and are not independently verifiable from this release**.
