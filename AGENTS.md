# AGENTS.md

> Agent-native version control system for swarm collaboration. CRDT-based concurrency, probabilistic proposals, gated promotions.

## Setup Commands

- Install Python deps: `pip install -e ".[dev]"`
- Activate venv: `source .venv/bin/activate`
- Start backend: `python -m apps.aether_server.main` (port 8100)
- Start backend in MX proof mode: `AETHER_SERVER_PROFILE=mx_proof python -m apps.aether_server.main` (enables fast autopromotion defaults unless explicitly overridden)
- Start UI: `cd apps/aether_ui && npm run dev` (port 3000)
- Demo stack manager: `./scripts/demo_stack.sh start|status|stop|restart`
- Demo keepalive monitor: `./scripts/demo_keepalive.sh` (auto-restores server/UI if down)
- Demo uptime gate (P0): `./scripts/run_demo_p0_gate.sh` (set `AETHER_P0_RECOVERY_TEST=true` for forced recovery validation)
- Demo flow gate (P1): `./scripts/run_demo_p1_gate.sh` (repo -> proposal -> promotion -> physics)
- Demo risk-freeze gate (P2): `./scripts/run_demo_p2_gate.sh` (pre-demo guardrails + freeze checks)
- Demo fallback gate (P3): `./scripts/run_demo_p3_gate.sh` (pins known-good JSON + dashboard fallback state)
- Demo semantic/AIP gate (P4): `./scripts/run_demo_p4_gate.sh` (AIP schema + adapter tests + dual strict harness)
- Research alignment check: `./scripts/check_research_alignment.sh` (ADR + execution checklist guard)
- Optional backend HOME override for demos: `AETHER_DEMO_HOME=/path`
- Night suite (15m gate + 2x60m strict): `./scripts/run_phase1_night_suite.sh`
- Safe long-run wrapper (caffeinate + heartbeat + timeout): `./scripts/run_swarm_harness_safe.sh [swarm-harness args]`
- Latest soak/run status: `python -m apps.aether_cli.main swarm-run-status [--heartbeat /path/to/*.heartbeat.log] [--json]`
- Run Python tests: `python -m pytest tests/aether/ -v`
- CLI help: `python -m apps.aether_cli.main --help`

### Rust Workspace Setup & Commands

- Build Rust workspace: `cargo build --workspace`
- Run Rust tests (SQLite/default): `cargo test --workspace`
- Run Rust tests (Postgres profile): `DATABASE_URL=postgres://localhost/postgres cargo test --workspace --no-default-features --features db-postgres`
- Run Rust linter: `cargo clippy --workspace --all-targets --all-features`

### Long-Run Soak Policy

- Treat long soaks as single-purpose night runs on this shared MacBook.
- Canonical baseline soaks must not overlap with MX proof/scaling traffic on the same Aether instance.
- `./scripts/run_swarm_harness_safe.sh` now enforces:
  - night-only starts for long runs (default threshold `4h`)
  - no concurrent MX workload processes for `canonical_baseline` runs
- Run classes:
  - `AETHER_SWARM_RUN_CLASS=canonical_baseline` (default)
  - `AETHER_SWARM_RUN_CLASS=mixed_source_stress`
- Explicit overrides:
  - `AETHER_ALLOW_DAYTIME_LONG_RUN=1`
  - `AETHER_ALLOW_MIXED_SOURCE_STRESS=1`
- Runtime artifact root:
  - `AETHER_RUNTIME_ARTIFACT_ROOT` (default `~/.aether/runtime_artifacts`)
- Tunables:
  - `AETHER_SOAK_LONG_THRESHOLD_SECONDS` (default `14400`)
  - `AETHER_SOAK_NIGHT_START_HOUR` (default `20`)
  - `AETHER_SOAK_NIGHT_END_HOUR` (default `8`)

### Repo Hygiene

- Local runtime cleanup: `rm -rf .run .pytest-artifacts aether.db aether_storage apps/aether_server/aether.db apps/aether_ui/ui.log *.log`
- Safe backup branch push: `git checkout -b codex/backup-YYYYMMDD-HHMM && git push -u origin HEAD`
- Keep local runtime files ignored via `.gitignore` (logs, local DBs, artifact dirs)

### Autonomous Promotion Worker (optional)

Background auto-promotion now runs in a separate worker process (disabled by default).

- Enable: `AETHER_AUTOPROMOTE_ENABLED=true`
- Poll interval seconds: `AETHER_AUTOPROMOTE_INTERVAL_SECONDS` (default `15`)
- Per-repo timeout seconds: `AETHER_AUTOPROMOTE_REPO_TIMEOUT_SECONDS` (default `30`)
- Promotion batch size: `AETHER_AUTOPROMOTE_BATCH_SIZE` (default `10`)
- Shutdown grace seconds: `AETHER_AUTOPROMOTE_SHUTDOWN_GRACE_SECONDS` (default `8`)
- Named startup profile:
  - `AETHER_SERVER_PROFILE=mx_proof` enables proof-friendly autopromotion defaults (`enabled=true`, `interval=5s`) unless explicit `AETHER_AUTOPROMOTE_*` env vars override them

### Intake Coherence Backpressure (optional)

API intake now supports a queue-depth valve to protect canonicalization liveness.

- Enable threshold (active backlog max): `AETHER_MAX_COHERENCE_BACKLOG` (default `1000`)
- Resume threshold after throttling: `AETHER_BACKLOG_RESUME_LEVEL` (default `0.8 * max`)
- Rate window for lambda/mu telemetry: `AETHER_COHERENCE_WINDOW_SECONDS` (default `60`)
- Max `Retry-After` clamp in seconds: `AETHER_RETRY_AFTER_MAX_SECONDS` (default `300`)
- Optional per-path hotspot limiter (active INGESTED+PENDING+EVALUATING): `AETHER_MAX_PATH_ACTIVE_PROPOSALS` (default disabled)
- Optional accepted-rate limiter: `AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT` (default disabled; set e.g. `0.9`)
- Minimum promotion rate to activate ratio limiter: `AETHER_RATIO_CONTROL_MIN_MU_TPS` (default `0.2`)
- Minimum promotion events in window to activate ratio limiter: `AETHER_RATIO_CONTROL_MIN_MU_EVENTS` (default `5`)

## Code Style

- Python 3.11+ with type hints (tested with Python 3.14.3)
- FastAPI for REST endpoints
- SQLModel for database entities
- Async/await patterns for I/O
- Services use dependency injection via constructor
- Rust 1.88+ for the CRDT backend workspace (`aether-rs`; `>=1.88` required by locked `home 0.5.12` in `aether-rs/Cargo.lock`, tested with `rustc 1.94.1`)
- Node.js 20.9.0+ for the Next.js dashboard (`apps/aether_ui`; `>=20.9.0` required by `next@16.1.5`, tested with `Node.js v25.3.0`)

## Testing Instructions

```bash
# Run all Python tests
python -m pytest tests/aether/ -v

# Run specific Python test file
python -m pytest tests/aether/test_proposal_service.py -v

# Run Python coverage
python -m pytest tests/aether/ --cov=packages/aether

# Run all Rust tests (SQLite)
cargo test --workspace

# Run Rust tests under Postgres feature profile
DATABASE_URL=postgres://localhost/postgres cargo test --workspace --no-default-features --features db-postgres
```

Before submitting changes:
- All tests must pass (both Python and Rust suites)
- No new linting errors (PEP8 / cargo clippy clean)
- API endpoints should return proper status codes

## PR Instructions

- Use conventional commits: `fix:`, `feat:`, `docs:`, `refactor:`
- Keep PRs focused on single concern
- Include test coverage for new features
- Update AGENTS.md if adding new commands or changing architecture

---

## Architecture

### Rust Crate Workspace (`aether-rs`)

```
aether-rs/
├── crates/
│   ├── crdt/           # Core CRDT layer (RGA, head frontier, meet_frontier, bounded alignment)
│   │   ├── src/
│   │   │   ├── rga.rs
│   │   │   └── head_crdt.rs
│   │   └── tests/
│   │       ├── rga_proptest.rs
│   │       └── rga_vacuum_tests.rs
│   ├── models/         # SQLModel equivalent shared Entities (Proposals, Events, Repositories)
│   ├── services/       # Orchestration services (compaction logic, gate verification runner)
│   │   └── src/
│   │       ├── promotion_orchestrator.rs
│   │       └── gate_runner.rs
│   └── server/         # Axum server housing promotion and VCS REST controllers
│       ├── src/
│       │   └── crdt_promotion.rs  # Enforces frontier width cap backpressure
│       └── tests/
│           └── test_pg_concurrency.rs
```

### Legacy Python Stack

```
packages/aether/
├── crdt/           # CRDT implementation (RGA for text)
│   ├── rga.py              # Replicated Growable Array
│   ├── document_store.py   # Multi-file document management
│   └── structured_doc.py   # AST-level semantic merging (.py files)
├── models/         # Data models
│   ├── entities.py         # SQLModel entities
│   ├── aip.py              # AIP v0 mutator schema + validation helpers
│   └── events.py           # Event types for pub/sub
├── services/       # Core services
│   ├── proposal_service.py     # Proposal submission & conflict detection
│   ├── promotion_engine.py     # Probabilistic scoring & promotion
│   ├── snapshot_service.py     # Deterministic snapshots
│   ├── gate_runner.py          # Verification gates
│   ├── repository_service.py   # Repository management
│   └── event_bus.py            # Realtime event streaming
└── api/            # FastAPI routes
    ├── routes.py       # REST endpoints
    └── websocket.py    # WebSocket streaming

apps/
├── aether_cli/     # CLI tool for agents
├── aether_server/  # Server entrypoint
└── aether_ui/      # Next.js dashboard
```

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/repos` | GET | List all repositories |
| `/v1/repos` | POST | Create repository |
| `/v1/repos/{id}` | GET | Get repository details |
| `/v1/repos/{id}/proposals` | POST | Submit proposal |
| `/v1/repos/{id}/proposals` | GET | List proposals |
| `/v1/repos/{id}/head` | GET | Get canonical head |
| `/v1/repos/{id}/promotions` | GET | List promotions |
| `/v1/repos/{id}/promotions` | POST | Request promotion |
| `/v1/repos/{id}/snapshots/{id}` | GET | Get snapshot |
| `/v1/system/physics` | GET | Queue physics telemetry (`lambda`, `mu`, backlog, semantic fallback hit rate + by-language/by-path split) |
| `/v1/health` | GET | Health check |

## CLI Commands

| Command | Description |
|---------|-------------|
| `aether repos` | Manage repositories |
| `aether push` | Submit a proposal |
| `aether status` | Get repository status |
| `aether head` | Get current head |
| `aether watch` | Watch realtime events |
| `aether snapshot` | Create/download snapshots |
| `aether promote` | Request promotion |
| `aether swarm-harness` | Run continuous high-agent flow simulation + SLO checks |
| `aether git-import` | Import Git repo |
| `aether git-export` | Export to Git |

## Key Concepts

**Proposals**: Agent-submitted changes with `agent_id`, `task_id`, `confidence_score` (0-1), `affected_paths`, and `dependencies`.

**Conflict Sets**: Overlapping proposals form conflict sets. Promotion engine evaluates top-k candidates.

**Promotion**: Advances canonical head via gate verification → probabilistic scoring → event publication.

**CRDT**: RGA ensures concurrent edits converge. StructuredDocument provides AST-level merging for Python files.

---

## Known Issues

### Sustained high-agent load (1000-agent strict profiles)
- **Status**: active optimization area
- **Symptom**: submit throughput can exceed promotion service rate in long windows, causing backlog growth
- **Current controls**: backlog valve (`AETHER_MAX_COHERENCE_BACKLOG`), resume watermark, optional accepted/promote ratio limiter, path hotspot limiter
- **Focus**: improve promotion service rate while preserving deterministic replay and gate safety

---

## AI Team

| Agent | Environment | Role |
|-------|-------------|------|
| **Antigravity (Gemini)** | IDE Dashboard | Lead Architect, UI/UX |
| **Claude Code** | Terminal CLI | Tactical debugging, task execution |
| **GitHub Copilot** | Terminal CLI | Test execution, verification |
| **Codex** | Terminal CLI | Code generation, integration |

Agents work in parallel, submitting proposals evaluated by the Promotion Engine. Roles are fluid - any agent can pick up any task.

## Storage

- Database: `~/.aether/aether.db` (SQLite with WAL mode)
- Snapshots: `~/.aether/aether_storage/`
- Not stored in iCloud to avoid sync conflicts

## Project Stats

| Component | Lines of Code |
|-----------|---------------|
| Canonical product scope (tracked source/test/scripts) | 20,232 |
| Python (core/server/cli only) | 11,898 |
| TypeScript (frontend) | 2,299 |

---

## Research Diagnostics & Simulation

As part of the Sprint 4D and 4E database scalability analysis under code freeze, the following local diagnostic and simulation tools are available in the repository:

### 1. Topology Sizing & Autovacuum Baseline
*   **Harness Script**: `scripts/run_topology_baseline.sh`
    Standardizes directional/short-run sweeps across 8 isolated topologies, dynamically capturing table sizes, autovacuum counts, and P95/P99 latency percentiles.
*   **Postgres OID & Sizing Diagnostic**: `psql -d aether -f scripts/diagnostics_toast.sql`
    Pulls exact live/dead OIDs, relation bytes, and autovacuum timestamps directly from the PostgreSQL catalog.
*   **Latency Decomposition Diagnostic**: `python3 scripts/diagnostics_latency.py`
    Computes percentiles for transaction wait (`t_lock_wait`), checkout (`t_pool_acquire_ms`), and execution timings.

### 2. Database Access-Pattern Simulation
*   **Microbenchmark Script**: `python3 scratch/simulate_append_only.py --concurrency <C> --iterations <I>`
    Contrasts `UPDATE`-in-place with `INSERT`-append storage sizing footprints, index overheads, and write transaction latencies. (Measures physical DB storage/write-latency only; does not simulate CRDT server runtime logic).

