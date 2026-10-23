# Project Aether (Research Prototype)

> **Research Prototype Status:** Aether is an experimental repository control plane for concurrent coding agents. It implements proposal intake, CRDT-based document operations, selected structural merge workflows, snapshots, and configurable promotion gates. Current evidence includes automated tests and bounded synthetic experiments; production safety, general semantic correctness, and sustained large-swarm performance remain unproven.

For the detailed documentation index, artifact provenance, and historical benchmark caveats, see [`docs/README.md`](docs/README.md), [`docs/aether_claims_matrix_2026_04_13.md`](docs/aether_claims_matrix_2026_04_13.md), and [`docs/aether_artifact_index_2026_04_13.md`](docs/aether_artifact_index_2026_04_13.md).

---

## 1. Architectural Motivation vs. Measured Scope

### Architectural Motivation
Standard Git workflows are designed around human-paced pull requests and line-based three-way merges. When multiple automated agents concurrently propose edits against shared files, line-offset shifts and optimistic push races can cause frequent rebase churn. Aether explores an alternative control-plane architecture:
- **Proposal Intake & Queue Backpressure:** Agents submit candidate changes (`full_file`, `unified_diff`, or structured AIP mutations) with confidence metadata into a queued state (`INGESTED` / `PENDING`) rather than mutating the canonical branch directly.
- **CRDT & Python AST Structural Operations:** Text documents can be represented via a Replicated Growable Array (`RGA`), and Python source files (`.py`) can be decomposed into `libcst` syntax nodes (`CLASS:Name`, `FUNC:name`) to merge disjoint method edits within the same file.
- **Probabilistic Scoring & Gated Promotion:** A promotion engine scores candidate proposals, materializes candidate snapshots, executes configured gate commands, and advances the canonical head (`AetherHead`) if required gates succeed.

### Measured Scope vs. Unproven Hypotheses
- **What is measured in this repository:** Functional unit and integration tests (`tests/aether/`), a standalone real-gate rejection script (`examples/reproducible_gate_verification_demo.py`), a 4-case synthetic semantic-merge qualification script using a deterministic synthesis simulator and `echo 'ok'` gates (`qualification/semantic_suite/run.py`), and historical short/medium-window synthetic load reports (`peer-review-artifacts/`, `docs/`).
- **What is not proven:** This repository does **not** contain comparative benchmarks proving that Git fails at a specific agent count, does **not** demonstrate live-LLM semantic conflict resolution, does **not** provide container/VM sandboxing for gate execution, and does **not** prove sustained large-swarm production readiness.
- **Retained Predecessor-Project Files:** [`EXECUTIVE_SUMMARY.md`](EXECUTIVE_SUMMARY.md), [`PRD_REVIEW.md`](PRD_REVIEW.md), [`RELEASE_NOTES.md`](RELEASE_NOTES.md), and the `packages/idasdlc_core` / `apps/backend` / `apps/cli` directories are retained historical artifacts from an earlier predecessor project (`Evolution Coding` / `IDASDLC`, January 2026) and do not represent current Project Aether capabilities.

---

## 2. Implemented Capabilities & Backend Capability Matrix

Aether contains two server implementations in the same repository—a **Python reference server** (`apps/aether_server` + `packages/aether`) and a **Rust server workspace** (`aether-rs`, compiled with either default feature `db-sqlite` or `--no-default-features --features db-postgres`)—plus a Next.js dashboard (`apps/aether_ui`).

**The Python and Rust backends do not have full API or feature parity.** The table below reflects the actual router definitions and service implementations in [`packages/aether/api/routes.py`](packages/aether/api/routes.py), [`packages/aether/api/websocket.py`](packages/aether/api/websocket.py), [`aether-rs/crates/server/src/main.rs`](aether-rs/crates/server/src/main.rs), and [`aether-rs/crates/server/src/db_postgres.rs`](aether-rs/crates/server/src/db_postgres.rs):

| Capability / Surface | Python Reference Server (`apps/aether_server.main`) | Rust Server — SQLite (`aether-rs`, default `db-sqlite`) | Rust Server — PostgreSQL (`aether-rs`, `--features db-postgres`) |
| :--- | :--- | :--- | :--- |
| **Default Bind & Non-Loopback Guard** | **Implemented:** Binds `127.0.0.1:8100`; non-loopback `AETHER_HOST` requires `AETHER_ALLOW_UNSAFE_NETWORK_BIND=true` + `AETHER_API_TOKEN` | **Implemented:** Binds `127.0.0.1:8100`; non-loopback `AETHER_HOST` requires `AETHER_ALLOW_UNSAFE_NETWORK_BIND=true` + `AETHER_API_TOKEN` | **Implemented:** Binds `127.0.0.1:8100`; non-loopback `AETHER_HOST` requires `AETHER_ALLOW_UNSAFE_NETWORK_BIND=true` + `AETHER_API_TOKEN` |
| **HTTP API Token Auth (`AETHER_API_TOKEN`)** | **Implemented:** Protects `/v1/*` HTTP routes (except `/v1/health`) via `Bearer` or `X-Aether-Token` | **Implemented:** Protects HTTP routes (except `OPTIONS`, `/`, `/v1/health`) via `Bearer` or `X-Aether-Token` | **Implemented:** Protects HTTP routes (except `OPTIONS`, `/`, `/v1/health`) via `Bearer` or `X-Aether-Token` |
| **WebSocket Streaming (`/v1/events`, `/v1/repos/{id}/events`)** | **Implemented:** Enforces `AETHER_API_TOKEN` (`Bearer`, `X-Aether-Token`, or `?token=`) and `AETHER_CORS_ORIGINS` `Origin` header check before `accept()` | **Unsupported** (no WebSocket routes mounted) | **Unsupported** (no WebSocket routes mounted) |
| **Repository CRUD (`/v1/repos`, `/v1/repos/{id}`)** | **Implemented:** `GET`, `POST`, `GET /{id}`, `DELETE /{id}` | **Partial:** `GET`, `POST` (`name`/`description` only), `GET /:id` (`DELETE` unsupported) | **Partial:** `GET`, `POST` (`name`/`description` only), `GET /:id` (`DELETE` unsupported) |
| **Proposal Intake & Query (`/v1/repos/{id}/proposals*`)** | **Implemented:** `POST`, `GET`, `GET /{proposal_id}`, `GET /{proposal_id}/provenance` | **Partial:** `POST`, `GET` (`GET /:proposal_id` and `/provenance` unsupported) | **Partial:** `POST`, `GET` (`GET /:proposal_id` and `/provenance` unsupported) |
| **Canonical Head (`GET /v1/repos/{id}/head`)** | **Implemented** | **Implemented** | **Implemented** |
| **Causal Frontier (`GET /v1/repos/{id}/frontier`)** | **Unsupported** | **Unsupported** | **Implemented (Experimental CRDT mode)** |
| **Snapshots REST & Download (`/v1/repos/{id}/snapshots*`)** | **Implemented:** `POST`, `GET`, `GET /{snapshot_id}`, `GET /{snapshot_id}/download`, `GET /{snapshot_id}/files/{path}` | **Unsupported over HTTP** (snapshots materialized on disk during promotion, no REST routes) | **Unsupported over HTTP** (snapshots materialized on disk during promotion, no REST routes) |
| **Evaluated Promotion (`POST /v1/repos/{id}/promotions`)** | **Implemented:** Runs conflict scoring, snapshot creation, and `GateRunner`; supports `GET /promotions`, `GET /promotions/{id}`, `GET /promotions/{id}/verify`, `POST /cohort/evaluate` | **Partial:** `GET`, `POST /v1/repos/:id/promotions` (runs `PromotionOrchestrator` + `GateRunner::run_all_gates` + SQLite CAS) | **Partial:** `GET`, `POST /v1/repos/:id/promotions` (scalar mode by default; experimental CRDT/advisory/frontier modes via `crdt_enabled` + `AETHER_CRDT_MODE`) |
| **Trusted External-Commit Hook (`POST /v1/repos/:id/commit_promotion`)** | **Unsupported** | **Implemented (Trusted-Only):** Commits caller-supplied promotion & advances head under SQL CAS **without running gates** | **Implemented (Trusted-Only):** Commits caller-supplied promotion & advances head under SQL CAS **without running gates** |
| **Custom Gate Command Guard (`AETHER_ALLOW_CUSTOM_GATE_COMMANDS`)** | **Implemented:** Rejects custom `gate_config` on `POST /v1/repos` (`403`) and fails closed in `GateRunner` unless `AETHER_ALLOW_CUSTOM_GATE_COMMANDS=true` | **Not Enforced in Rust:** `POST /v1/repos` omits `gate_config`, but any `gate_config` stored in SQLite is executed directly via `sh -c` | **Not Enforced in Rust:** `POST /v1/repos` omits `gate_config`, but any `gate_config` stored in PostgreSQL is executed directly via `sh -c` |
| **Gate Execution Isolation** | **Trusted Host Subprocess (`sh -c`):** Not sandboxed when custom gates are enabled; default gates are `SIMULATED:` no-ops | **Trusted Host Subprocess (`sh -c`):** Not sandboxed; default `gate_config` is `NULL` (0 gates run) | **Trusted Host Subprocess (`sh -c`):** Not sandboxed; default `gate_config` is `NULL` (0 gates run) |
| **Queue Physics Telemetry (`GET /v1/system/physics`)** | **Implemented:** Returns `lambda_tps`, `mu_tps`, `rho`, queue depth, status counts, semantic fallback rate + by-language/path split, and intake backpressure state | **Unsupported** (route not mounted in `db-sqlite`) | **Partial:** Returns `lambda_tps`, `mu_tps`, `rho`, queue depth, status counts, event counts, and projected cache counters |
| **Operation Graph & Conflicts (`/operation-graph*`, `/conflicts`, `/telemetry/events`, `/system/build`)** | **Implemented** | **Unsupported** | **Unsupported** |

---

## 3. Security and Promotion Boundaries

1. **Loopback-Only Default & API Token Authentication (Not Execution Isolation):**
   - Both the Python server ([`apps/aether_server/main.py`](apps/aether_server/main.py)) and the Rust server ([`aether-rs/crates/server/src/main.rs`](aether-rs/crates/server/src/main.rs)) bind to `127.0.0.1:8100` by default and refuse non-loopback `AETHER_HOST` binds unless `AETHER_ALLOW_UNSAFE_NETWORK_BIND=true` and a non-empty `AETHER_API_TOKEN` are set.
   - **Authentication is not execution isolation:** `AETHER_API_TOKEN` authenticates HTTP/WebSocket callers, but does not sandbox or isolate shell commands executed by verification gates.
2. **Backend Scope of `AETHER_ALLOW_CUSTOM_GATE_COMMANDS`:**
   - `AETHER_ALLOW_CUSTOM_GATE_COMMANDS=true` protects **only the Python gate path** ([`packages/aether/api/routes.py`](packages/aether/api/routes.py), [`packages/aether/services/gate_runner.py`](packages/aether/services/gate_runner.py)).
   - **Rust does not enforce `AETHER_ALLOW_CUSTOM_GATE_COMMANDS`:** [`aether-rs/crates/services/src/gate_runner.rs`](aether-rs/crates/services/src/gate_runner.rs) executes any `GateConfig.command` stored in `aether_repositories.gate_config` directly via `sh -c` under the server process's OS account and environment. Configured shell gates in both Python (when enabled) and Rust must be treated as **trusted host code execution, not sandboxed**.
3. **Default Simulation Gates Do Not Validate Code Correctness:**
   - In Python, newly created repositories default to `SIMULATED: build skipped - no-op default gate` and `SIMULATED: unit_tests skipped - no-op default gate` ([`packages/aether/services/repository_service.py`](packages/aether/services/repository_service.py)), which return `GateStatus.PASS` without running any compiler or test runner and record `"_verification_mode": "SIMULATED"` in promotion evidence. In Rust, `POST /v1/repos` initializes `gate_config` as `NULL` (zero gates executed unless updated in the database).
4. **Evaluated Promotion vs. Trusted External-Commit Hook (`commit_promotion`):**
   - Calling `POST /v1/repos/{id}/promotions` runs orchestration and configured gates before advancing canonical head.
   - However, the Rust server (`db-sqlite` and `db-postgres`) also exposes `POST /v1/repos/:id/commit_promotion`, a trusted external-commit hook that accepts caller-supplied `included_proposals`, `snapshot_id`, and `evidence` and advances `aether_repositories.head_id` / `head_sequence` under SQL CAS **without independently running verification gates**. Consequently, not every canonical head advance in the Rust runtime independently executes verification gates.

---

## 4. Claims-to-Evidence Table

Every quantitative statement and capability claim in this repository is scoped to the table below:

| Claim | Backend / Mode | Test or Artifact | Status (Current vs. Historical) | Limitations & Caveats | Reproduction Command (or "Not bundled") |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Loopback bind default, API token auth, CORS/WS Origin checks, and Python custom-gate Opt-In (`403` / fail-closed)** | Python (`apps/aether_server`) & Rust (`aether-rs`, unit tests) | [`tests/aether/test_security_defaults.py`](tests/aether/test_security_defaults.py), [`aether-rs/crates/server/src/main.rs`](aether-rs/crates/server/src/main.rs) (`security_tests`) | **Current** (verified on release candidate) | Auth is not sandboxing; `AETHER_ALLOW_CUSTOM_GATE_COMMANDS` is enforced in Python only, not in Rust `GateRunner` | `pytest tests/aether/test_security_defaults.py -v` and `cargo test --manifest-path aether-rs/Cargo.toml -p server` |
| **Real `py_compile` + `pytest` gate execution rejecting a broken proposal (`GateStatus.FAIL`) and promoting a valid fix (`_verification_mode = "EXECUTED"`)** | Python (`GateRunner(allow_custom_commands=True)`, SQLite) | [`examples/reproducible_gate_verification_demo.py`](examples/reproducible_gate_verification_demo.py), [`tests/aether/test_verification_gate_rejection.py`](tests/aether/test_verification_gate_rejection.py) | **Current** (verified on release candidate) | Runs trusted local `python -m py_compile` and `pytest` subprocesses on a small single-file calculator repo | `python examples/reproducible_gate_verification_demo.py` and `pytest tests/aether/test_verification_gate_rejection.py -v` |
| **Mixed-batch sequential fallback preserves promoted winner (`PROMOTED`) when a sibling proposal fails gates (`REJECTED`), with `force=False` and `force=True`** | Python (`PromotionEngine` + FastAPI route `/v1/repos/{id}/promotions`) | [`tests/aether/test_verification_gate_rejection.py`](tests/aether/test_verification_gate_rejection.py) | **Current** (verified on release candidate) | Tested on 2-proposal batches in SQLite | `pytest tests/aether/test_verification_gate_rejection.py -v` |
| **Python AST structural merge (`libcst`) for disjoint class methods & deterministic synthesis-worker fallback simulator** | Python (`StructuredDocument` + `packages/aether/synthesis_worker/main.py`, SQLite & PostgreSQL) | [`qualification/semantic_suite/run.py`](qualification/semantic_suite/run.py), committed reports [`report_sqlite_v2_024163b8a3ebf951923a8e6899051af2793f1748.json`](qualification/semantic_suite/report_sqlite_v2_024163b8a3ebf951923a8e6899051af2793f1748.json) & [`report_postgres_v2_024163b8a3ebf951923a8e6899051af2793f1748.json`](qualification/semantic_suite/report_postgres_v2_024163b8a3ebf951923a8e6899051af2793f1748.json) | **Current** (SQLite runnable on RC) / **Committed Historical** (`024163b8...` reports for SQLite & PG) | **Not a live LLM:** `packages/aether/synthesis_worker/main.py` is a deterministic regex/line-combining simulator (`model_configuration="simulated-synthesis-worker"`, `model_version="1.0-fake"`). All 4 qualification cases use `echo 'ok'` gates, which do not validate application-level semantic correctness | `python qualification/semantic_suite/run.py --db-profile sqlite --output-dir /tmp` (PG profile requires live local PostgreSQL) |
| **Committed 1,000-agent 60-minute swarm-harness preflight (`run_seconds=3600`, `drain_seconds=600`, `num_files=500`) — FAILED (`passed=false`)** | Python reference stack (in-process `SwarmHarness` with `--submit-via-api-route`, SQLite) | [`peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json`](peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json) | **Committed Historical Report (FAILED)** | **Failed SLO (`passed=false`, `submit_p95_ok=false`):** `p95_submit_latency_ms = 5,759.84 ms` (~5.76s vs `5,000 ms` limit). Out of `1,334,063` client loop attempts (`max_inflight_submissions=250`), `1,328,003` (`99.55%`) were throttled at intake, `5,637` were accepted (`1.57` proposals/s active phase; `1.34` proposals/s total phase), `422` failed, and `4,829` proposals were promoted across `684` promotion batches (`promotion_throughput = 1.15` proposals promoted/s; `0.16` promotion batches/s over `4,204.29s`; `589` superseded, `428` quarantined, `214` pending at end) using default `SIMULATED:` gates | Historical artifact inspectable directly; short smoke run: `python -m apps.aether_cli.main swarm-harness --agents 20 --files 50 --run-seconds 15 --drain-seconds 10 --submit-via-api-route --output-json /tmp/smoke.json` |
| **Historical Python soak summaries (`25m`, `60m`, `4h`, `8h` passes at `ratio_limit=0.425`; `10h` `window_rho_ok=1.011` miss; `12h` `mark_evaluating` quarantine wave; `24h` throughput miss)** | Python reference stack (`SwarmHarness`) | Summarized in [`docs/aether_artifact_index_2026_04_13.md`](docs/aether_artifact_index_2026_04_13.md) and [`docs/aether_readiness_package_2026_04_11.md`](docs/aether_readiness_package_2026_04_11.md) (raw `.pytest-artifacts/*.json` excluded from git) | **Historical Summary Only (Raw Evidence Not Bundled)** | **Not independently verifiable from this release** because raw `.pytest-artifacts/` JSON files are git-ignored; `10h`, `12h`, and `24h` runs failed strict acceptance criteria | Not bundled |
| **Historical MX Swarm integration summary (single narrow slice `jsonpeek` via `ollama/mistral:7b`, 3/3 local repeatability)** | Python reference stack + external MX Swarm repo | Summarized in [`docs/aether_mx_swarm_state_and_gap_analysis_2026_03_15.md`](docs/aether_mx_swarm_state_and_gap_analysis_2026_03_15.md) and [`docs/mx_aether_proof_ladder_and_slice_matrix_2026_03_15.md`](docs/mx_aether_proof_ladder_and_slice_matrix_2026_03_15.md) | **Historical Summary Only (External Repo Artifacts Not Bundled)** | External `<MX_SWARM_ROOT>/reports/*` files are not bundled in this repository; broader slice coverage and concurrent MX load remain unproven | Not bundled (AIP adapter unit tests runnable via `pytest tests/aether/test_aip_mutations.py -v`) |
| **Stage 3 Rust/PostgreSQL 1-hour synthetic soak (`50` agent loops across `10` repos, `3,600s + 30s` drain): `37,715` `submit_ok`, `17,363` `promote_ok`, `20,352` HTTP 409 (`53.96%`), `0` DB invariant violations** | Rust PostgreSQL server (`aether-rs`, `--features db-postgres`), driven by [`scripts/soak_native_rust_pg.sh`](scripts/soak_native_rust_pg.sh) | [`docs/aether_stage3_pg_baseline_closeout_2026_05_10.md`](docs/aether_stage3_pg_baseline_closeout_2026_05_10.md) | **Committed Historical Report** (raw DB/logs not bundled) | **Synthetic workload:** 3-line `full_file` replacements with `echo SOAK_GATE_OK` gate (not complex CRDT diffs or real test suites); whether `SOAK_CRDT_ENABLED` was set is not recorded; `53.96%` of promotion requests returned HTTP `409 Conflict`; Poisson gate (`~60.3%` limit) used a hardcoded `175ms` fallback service time and does not prove workload validity or general scalability | Requires live local PostgreSQL: `SOAK_AGENTS=50 SOAK_REPOS=10 SOAK_RUN_SECONDS=3600 ./scripts/soak_native_rust_pg.sh` |
| **Sprint 4D & 4E PostgreSQL topology & autovacuum starvation sweeps (`60s`–`300s`, `80`–`160` agents across `1`–`100` repos)** | Rust PostgreSQL server (`aether-rs`, `--features db-postgres`, `crdt_enabled=true`) | [`docs/sprint4d_topology_baseline.md`](docs/sprint4d_topology_baseline.md), [`docs/sprint4e_mitigation_experiments.md`](docs/sprint4e_mitigation_experiments.md) | **Committed Historical Reports** (executed on historical commits `ab03e58a...` and `52814ac0...`) | Short-window (`60s`–`300s`) diagnostic sweeps; demonstrated severe single-repo / hot-repo contention (P99 lock waits of `3,230 ms` to `4,779 ms` and P99 occupancy up to `6,004.84 ms`) that forced vacuuming did not resolve | Requires live local PostgreSQL: `./scripts/run_topology_baseline.sh` and `./scripts/run_sprint_e_experiments.sh` |
| **Conditional OCC & advisory-locking race-window argument** | Rust PostgreSQL server (`crdt_promotion.rs`, `db_postgres.rs`) | [`docs/architecture/aether_cas_correctness_proof.md`](docs/architecture/aether_cas_correctness_proof.md) | **Conditional Design Argument** (not a formal or mechanized proof) | Holds only under explicit assumptions: no uncovered secondary-table writers mutating fingerprinted fields without bumping `head_sequence`, caller checks of `rows_affected == 1`, and trusted callers on `commit_promotion` | Design document in [`docs/architecture/aether_cas_correctness_proof.md`](docs/architecture/aether_cas_correctness_proof.md) |

---

## 5. Known Limitations & Evidence Provenance Notes

1. **No Execution Sandboxing:** Neither the Python `GateRunner` nor the Rust `GateRunner` isolates shell commands inside containers, VMs, or restricted OS namespaces. Custom gate commands must only be enabled (`AETHER_ALLOW_CUSTOM_GATE_COMMANDS=true` in Python, or non-empty `gate_config` in Rust) in trusted, externally isolated environments.
2. **Deterministic Synthesis Simulator Only:** The bundled synthesis worker ([`packages/aether/synthesis_worker/main.py`](packages/aether/synthesis_worker/main.py)) is a deterministic statement-combining simulator for testing fallback plumbing and provenance schemas. Live-LLM synthesis merge quality is not implemented or demonstrated in this release.
3. **High Contention on Hot Repositories:** Both Python SQLite soaks (where `phase1-60m-preflight-20260304-121522.json` throttled `99.55%` of client attempts and failed the `5,000 ms` P95 submit latency target at `5,759.84 ms`) and Rust PostgreSQL hot-repo sweeps (where Stage 3 observed `53.96%` HTTP `409` promotion responses and Sprint 4D/4E observed multi-second P99 lock waits) show that high-concurrency contention on shared repositories or paths remains an open systems problem.
4. **Single-Root Release History & Privacy Redaction Provenance:**
   - This public release repository is initialized as a clean single-root snapshot on `main`. Commit SHAs and branch names cited inside historical reports and filenames (such as `024163b8a3ebf951923a8e6899051af2793f1748`, `ab03e58a8faf1c16a993d1ce3fd74c6a28b26fdf`, `52814ac0a1312906d9bbc406ac01e9f760c00d61`, `e5643a9ff418`, `f8787e2b54c8`, `696c9ad`, `890b356`, `edb1f77`, `92d841b`, `ad18334`, and `soak/sprint-a-20260616`) are **historical identifiers from pre-release development** and do not resolve as Git objects in this repository.
   - Committed historical logs, JSON reports, and documentation (`docs/evidence/**`, `qualification/semantic_suite/report_*.json`, `peer-review-artifacts/**`, `docs/**`) underwent deterministic privacy path/username redaction prior to release (replacing local workstation/iCloud/worktree paths and usernames with `<REPO_ROOT>`, `<USER_HOME>`, `<WORKSPACE_ROOT>`, `aether-user`, etc.).
   - Rerunning test or qualification commands today executes against the current release candidate (`HEAD`) and will record the current checkout's Git identity and local wall-clock timings rather than reproducing historical pre-release SHAs or identical latencies.

---

## 6. Quick Start & Reproducible Verification Commands

### Prerequisites
- Python `>=3.11` with `virtualenv` / `pip` (tested with Python `3.14.3`)
- Rust `>=1.88` (`cargo`; minimum `1.88` required by locked `home 0.5.12` in `aether-rs/Cargo.lock`, tested with `rustc 1.94.1`) for the `aether-rs` workspace
- Optional: Node.js `>=20.9.0` (`npm`; minimum `20.9.0` required by `next@16.1.5` in `apps/aether_ui/package.json`, tested with `Node.js v25.3.0`) for `apps/aether_ui`, and a local PostgreSQL server (`localhost:5432`) for PostgreSQL-specific tests and scripts

### 6.1 Install & Run the Python Reference Server (`apps/aether_server`)

```bash
# Create a virtual environment and install Python dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Start the Python reference server (binds to http://127.0.0.1:8100 by default)
python -m apps.aether_server.main
```

In a second terminal (with `.venv` activated), you can use the CLI against the Python server:

```bash
aether repos create --name "demo-repo"
aether repos list
```

### 6.2 Run Reproducible Verification Checks on This Release Candidate

```bash
# 1. Research alignment guard (verifies ADR + research execution checklist sections)
./scripts/check_research_alignment.sh

# 2. Release hygiene, license, artifact-index, and full-history privacy/fsck suite
python -m pytest tests/aether/test_release_hygiene.py -v

# 3. Security defaults (loopback bind, API token auth, WebSocket auth/Origin, custom gate guard)
python -m pytest tests/aether/test_security_defaults.py -v

# 4. Real gate verification & rejection demo (executes real py_compile + pytest gates)
python examples/reproducible_gate_verification_demo.py

# 5. Synthetic 4-case semantic merge & simulated-synthesis qualification suite (SQLite in-memory)
python qualification/semantic_suite/run.py --db-profile sqlite --output-dir /tmp

# 6. Full Python test suite (289 passed, 7 skipped by default: 5 live-PostgreSQL tests in test_postgres_concurrency.py and 2 opt-in load tests gated by AETHER_ENABLE_CONTINUOUS_SWARM_TESTS and AETHER_ENABLE_SOAK_TESTS)
python -m pytest tests/aether/ -v
```

### 6.3 Build & Test the Rust Server Workspace (`aether-rs`)

```bash
# Build and run Rust tests under default SQLite profile (db-sqlite)
cargo test --manifest-path aether-rs/Cargo.toml --workspace

# Start the Rust SQLite server on http://127.0.0.1:8100 (supports subset of REST routes; see Section 2)
cargo run --manifest-path aether-rs/Cargo.toml -p server

# Optional: Run Rust tests under PostgreSQL profile (requires local PostgreSQL at postgres://localhost/postgres)
DATABASE_URL=postgres://localhost/postgres cargo test --manifest-path aether-rs/Cargo.toml --workspace --no-default-features --features db-postgres
```

### 6.4 Optional Next.js Dashboard (`apps/aether_ui`)

```bash
# Requires the Python reference server running on http://127.0.0.1:8100 for full REST + WebSocket support
cd apps/aether_ui
npm install
npm run dev
# Opens at http://localhost:3000
```

---

## License

MIT License — See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for details.
