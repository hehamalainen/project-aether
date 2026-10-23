# Aether Claims & Evidence Matrix

> **Research Prototype Positioning:** Aether is an experimental repository control plane for concurrent coding agents. This document classifies all project claims into four strict categories:
> 1. **Behavior Verified on the Current Release Candidate (`HEAD`)**
> 2. **Committed Historical Reports (Bundled in Repository, Executed on Pre-Release Revisions)**
> 3. **Historical Summaries Whose Raw Evidence Is Not Bundled (`Not Independently Verifiable from This Release`)**
> 4. **Unproven Hypotheses and Future Work**

---

## 1. Behavior Verified on the Current Release Candidate (`HEAD`)

The following behaviors are directly implemented and verifiable by running the included test suites and scripts on this checkout:

- **Loopback-Only Default & Opt-In Network Bind Guard (Python & Rust):**
  - Both the Python server ([`apps/aether_server/main.py`](../apps/aether_server/main.py)) and the Rust server ([`aether-rs/crates/server/src/main.rs`](../aether-rs/crates/server/src/main.rs), across `db-sqlite` and `db-postgres`) default to `127.0.0.1:8100` and refuse non-loopback `AETHER_HOST` values unless both `AETHER_ALLOW_UNSAFE_NETWORK_BIND=true` and a non-empty `AETHER_API_TOKEN` are configured.
- **API Token Authentication & Browser Origin Restrictions (Scoped by Backend):**
  - When `AETHER_API_TOKEN` is configured, Python protects `/v1/*` HTTP endpoints (except `/v1/health`) and both WebSocket endpoints (`/v1/events`, `/v1/repos/{id}/events`), and restricts CORS / WebSocket `Origin` headers to `AETHER_CORS_ORIGINS` (defaulting to localhost ports `3000` and `8100`).
  - When `AETHER_API_TOKEN` is configured, Rust (`db-sqlite` and `db-postgres`) protects HTTP endpoints (except `OPTIONS`, `/`, and `/v1/health`) via `api_token_auth_middleware`. Rust does not implement WebSocket endpoints.
  - **Limitation:** Authentication is not execution isolation and does not sandbox gate subprocesses.
- **Python Gate Command Boundary vs. Rust Trusted Gate Execution:**
  - In Python, `AETHER_ALLOW_CUSTOM_GATE_COMMANDS=true` is required before `POST /v1/repos` accepts custom `gate_config` (`403 Forbidden` otherwise) and before `GateRunner.run_gate` executes non-simulated shell commands (`GateStatus.ERROR` otherwise). Default Python repository gates are explicit `SIMULATED:` no-op commands (`_verification_mode = "SIMULATED"`) that do not validate code correctness.
  - **Limitation:** `AETHER_ALLOW_CUSTOM_GATE_COMMANDS` is **not enforced in Rust**. Any `gate_config` stored in SQLite or PostgreSQL is executed directly by [`aether-rs/crates/services/src/gate_runner.rs`](../aether-rs/crates/services/src/gate_runner.rs) via `sh -c` with the server process's privileges.
- **Evaluated Promotion vs. Rust Trusted External-Commit Hook (`commit_promotion`):**
  - Evaluated promotion (`POST /v1/repos/{id}/promotions`) executes orchestration and configured gates before advancing canonical head, and preserves partial promotion winners during sequential batch fallback ([`tests/aether/test_verification_gate_rejection.py`](../tests/aether/test_verification_gate_rejection.py)).
  - **Limitation:** The Rust server (`db-sqlite` and `db-postgres`) also exposes `POST /v1/repos/:id/commit_promotion`, a trusted external-commit hook that commits caller-supplied promotion records and advances canonical head under SQL CAS **without independently executing verification gates**.
- **Real Gate Rejection Demo (Python + SQLite):**
  - [`examples/reproducible_gate_verification_demo.py`](../examples/reproducible_gate_verification_demo.py) demonstrates a repository configured with `python -m py_compile` and `pytest` rejecting a faulty proposal (`GateStatus.FAIL`, head unchanged at sequence `1`) and promoting a fixed proposal (`GateStatus.PASS`, head advanced to sequence `2`, `_verification_mode == "EXECUTED"`).
- **Python AST Structural Merge (`libcst`) & Deterministic Synthesis-Worker Simulator Plumbing:**
  - Disjoint method additions and edits within a Python class are merged structurally via `libcst` in [`packages/aether/crdt/structured_doc.py`](../packages/aether/crdt/structured_doc.py).
  - Same-method conflicts can be routed to [`packages/aether/synthesis_worker/main.py`](../packages/aether/synthesis_worker/main.py), which is a **deterministic regex/line-combining simulator (`model_version="1.0-fake"`), not a live LLM**.

---

## 2. Committed Historical Reports (Bundled in Repository, Executed on Pre-Release Revisions)

> **Provenance & Redaction Disclosure:** The artifacts below are committed in this repository and were generated on pre-release development commits whose SHAs (`024163b8...`, `ab03e58a...`, `52814ac0...`, `696c9ad...`, `890b356...`, `edb1f77...`, `92d841b...`, `ad18334...`) do not resolve in this single-root release repository. Local filesystem paths and usernames in these historical files were redacted to generic placeholders (`<REPO_ROOT>`, `<USER_HOME>`, `<WORKSPACE_ROOT>`, `aether-user`) for privacy.

1. **Committed 1,000-Agent 60-Minute Swarm-Harness Preflight — FAILED (`passed = false`):**
   - **Artifact:** [`peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json`](../peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json)
   - **Configuration:** `num_agents = 1000` simulated client loops (`agent_interval_min_ms = 350`, `agent_interval_max_ms = 1400`), `num_files = 500`, `max_inflight_submissions = 250`, adaptive intake throttling enabled (`high_watermark = 220`, `low_watermark = 170`), `run_seconds = 3600.0`, `drain_seconds = 600.0`, default `SIMULATED:` gates.
   - **Measured Outcomes:**
     - **Overall Verdict:** `"passed": false` (`"submit_p95_ok": false`).
     - **Submission P95 Latency:** `5,759.84 ms` (~`5.76 seconds`), failing the `5,000.0 ms` (`5.0 second`) threshold.
     - **Simulated Client Attempts vs. Throttling vs. Acceptance:** Out of `1,334,063` total client submission attempts (`submitted_total`), `1,328,003` (`99.55%`, `submit_throttled_rate = 0.995457`) were throttled by client-side intake backpressure before submission, `5,637` were accepted (`submitted_success`, `submit_throughput = 1.57` proposals/sec during the active phase; `1.34` proposals/sec across the `4,204.29s` total phase), and `422` failed (`submit_failures`, `0.032%`).
     - **Promotions & Terminal Proposal States:** `684` promotions were created, promoting `4,829` proposals (`promotion_throughput = 1.15` proposals promoted/sec; `684 / 4204.29s = 0.16` promotion batches/sec), with `589` proposals `SUPERSEDED`, `428` `QUARANTINED`, and `214` remaining `PENDING` at the end of the drain window.
2. **Committed 4-Case Synthetic Semantic Merge & Contention Reports (SQLite & PostgreSQL):**
   - **Artifacts:** [`qualification/semantic_suite/report_sqlite_v2_024163b8a3ebf951923a8e6899051af2793f1748.json`](../qualification/semantic_suite/report_sqlite_v2_024163b8a3ebf951923a8e6899051af2793f1748.json) and [`qualification/semantic_suite/report_postgres_v2_024163b8a3ebf951923a8e6899051af2793f1748.json`](../qualification/semantic_suite/report_postgres_v2_024163b8a3ebf951923a8e6899051af2793f1748.json) (recorded on historical commit `024163b8a3ebf951923a8e6899051af2793f1748`).
   - **What They Establish & Limitations:** They verify 4 scripted two-proposal fixtures (`class_member_collisions`, `ast_edit_block_overlaps`, `synthesis_fallback_merges`, `negative_control`) using `model_configuration = "simulated-synthesis-worker"` and `echo 'ok'` gates. They do **not** test live-LLM reconciliation, application-level semantic correctness, or non-Python languages.
3. **Committed Stage 3 Rust/PostgreSQL 1-Hour Synthetic Soak Report:**
   - **Artifact:** [`docs/aether_stage3_pg_baseline_closeout_2026_05_10.md`](aether_stage3_pg_baseline_closeout_2026_05_10.md) (driver: [`scripts/soak_native_rust_pg.sh`](../scripts/soak_native_rust_pg.sh)).
   - **Scope & Limitations:** 50 simulated agent loops across 10 repositories (`5` agents/repo) for `3,600s + 30s` drain submitting 3-line `"full_file"` replacements with an `echo SOAK_GATE_OK` gate against the Rust PostgreSQL server (`--features db-postgres`; whether `SOAK_CRDT_ENABLED` was set is not recorded in the report). Recorded `37,715` `submit_ok`, `17,363` `promote_ok`, `20,352` HTTP `409 Conflict` responses (`53.96%` contention rate), and `0` database invariant violations. The `~60.3%` Poisson contention threshold relied on a hardcoded `175ms` fallback service time and does not prove workload validity, CRDT merge complexity, or performance with real test gates.
4. **Committed Sprint 4D & 4E PostgreSQL Topology & Autovacuum Starvation Reports:**
   - **Artifacts:** [`docs/sprint4d_topology_baseline.md`](sprint4d_topology_baseline.md) (`300s` sweeps, historical commit `ab03e58a8faf1c16a993d1ce3fd74c6a28b26fdf`) and [`docs/sprint4e_mitigation_experiments.md`](sprint4e_mitigation_experiments.md) (`60s` sweeps, historical commit `52814ac0a1312906d9bbc406ac01e9f760c00d61`).
   - **Findings:** Documented severe lock-wait contention on single-repository and hot-repository skew topologies (`P99 Lock Wait` between `3,230.58 ms` and `4,351.15 ms`; `P99 Occupancy` up to `6,004.84 ms`) and showed that forced 5-second `VACUUM` cycles did not eliminate serialization bottlenecks.

---

## 3. Historical Summaries Whose Raw Evidence Is Not Bundled (`Not Independently Verifiable from This Release`)

The following runs are discussed in historical engineering documents ([`docs/aether_readiness_package_2026_04_11.md`](aether_readiness_package_2026_04_11.md), [`docs/aether_artifact_index_2026_04_13.md`](aether_artifact_index_2026_04_13.md), [`docs/phase1_baseline_freeze_2026_03_11.md`](phase1_baseline_freeze_2026_03_11.md), [`docs/aether_mx_swarm_state_and_gap_analysis_2026_03_15.md`](aether_mx_swarm_state_and_gap_analysis_2026_03_15.md)), but their raw JSON/log outputs (`.pytest-artifacts/*` and `<MX_SWARM_ROOT>/reports/*`) are excluded from this repository. **Because the raw artifacts are not bundled, these summaries cannot be independently verified from this release and must not be cited as verified release-candidate capacity guarantees:**

- **Historical Python `SwarmHarness` Long-Run Summaries (`AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0.425` / `0.420`, `1,000` simulated agents, `500` files):**
  - Reported passing windows in historical summaries: `25m`, `60m`, `4h`, and `8h` (`.pytest-artifacts/phase1-suite/soak-8h-mx-intake-r0425-20260331-stable.json`, summarized at `submit_throughput = 0.8550` accepted proposals/s, `promotion_throughput = 0.8037` proposals promoted/s [metric counts proposals promoted/s, not promotion batches/s], `p95_submit_latency_ms = 1,527.22 ms`, `rho_window = 0.9961`).
  - Reported failing / degraded windows in historical summaries:
    - `10h` (`soak-10h-mx-intake-r0425-20260408-retry.json`): **failed** strict `window_rho_ok` (`1.011` vs `1.0` threshold).
    - `12h` (`soak-12h-mx-intake-r0420-20260411-day.json`): **surfaced late-window `mark_evaluating` event-loop quarantine wave**.
    - `24h` (`soak-24h-mx-intake-r0420-20260412.json`): **failed** submit and promotion throughput thresholds.
- **Historical External MX Swarm Live-Proof Summaries (`<MX_SWARM_ROOT>/reports/*`):**
  - Describe a narrow single-worker slice (`jsonpeek` using local `ollama/mistral:7b`, `3/3` local repeatability) submitting a proposal into Aether. Raw MX Swarm proof artifacts resided in a separate local repository (`<MX_SWARM_ROOT>`) and are not bundled here.

---

## 4. Unproven Hypotheses and Future Work

The following remain **unproven** and are **not** supported by current evidence:

- **Production Readiness & Multi-Tenant Safety:** Aether is a research prototype; gate execution is not sandboxed, and production operational hardening is not complete.
- **Live-LLM Semantic Conflict Resolution:** The bundled synthesis worker is a deterministic simulator; live-model merge accuracy, safety, and cost across real codebases are unproven.
- **Sustained High-Throughput Large-Swarm Scalability:** Clean, reproducible `12h`, `24h`, and multi-day high-agent soaks with real verification gates are not proven.
- **Elimination of Hot-Repository Serialization Bottlenecks:** High contention on single/hot repositories under PostgreSQL advisory and row locks remains an open architectural challenge (see [`docs/sprint4e_mitigation_experiments.md`](sprint4e_mitigation_experiments.md)).
- **System-Wide Formal Verification:** [`docs/architecture/aether_cas_correctness_proof.md`](architecture/aether_cas_correctness_proof.md) is a conditional pen-and-paper design argument under explicit assumptions, not a machine-checked or system-wide formal verification proof.
- **Empirical Superiority Over Git at a Specific Agent Threshold:** Motivating observations about rebase churn under concurrent agent edits are architectural motivation, not measured comparative benchmarks against Git.

---

## 5. Master Claims-to-Evidence Reference Table

See Section 4 of [`README.md`](../README.md#4-claims-to-evidence-table) for the consolidated Claims-to-Evidence Table mapping each claim to its backend/mode, test or artifact path, current vs. historical status, limitations, and exact reproduction command.
