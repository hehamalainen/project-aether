# Project Aether

**Agent-Native Version Control for the Swarm Era**

> **IMPORTANT**: This document outlines the high-level vision and architecture of Aether. For the **authoritative engineering truth set, current readiness, claims, and roadmap**, please start at [`docs/README.md`](docs/README.md).

---

## Vision

Aether is to agent swarms what Git was to distributed human teams: a collaboration substrate that makes high-parallel contribution **safe**, **fast**, and **convergent**.

Traditional Git assumes human-speed commits and manual conflict resolution. At swarm scale (100–1000 concurrent agents), those assumptions collapse. Aether solves this with:

- **CRDT-Based Concurrency** — Conflict-free text editing via RGA (Replicated Growable Array)
- **Probabilistic Proposals** — Agents submit changes with confidence scores and metadata
- **Gated Promotions** — Canonical head only advances after verification gates pass
- **Realtime Coordination** — WebSocket event streaming for agent observation

---

## Architecture Overview

Aether operates as a dual-stack environment to balance rapid prototyping with high-performance concurrency.

1. **Rust / PostgreSQL control plane (active scaling target)**: Native runtime under ongoing soak validation. Current repo evidence shows a Stage 3 baseline pass under a calibrated contention gate with named limits; see [`docs/README.md`](docs/README.md) for the current proof status and caveats.
2. **Python Reference Implementation**: The original prototyping implementation used for defining the scoring mechanisms, API contracts, and CRDT structures.
3. **Next.js UI**: The frontend dashboard located in `apps/aether_ui` for real-time observability of the agent swarm and repository state.

### Core Structure

```
aether-rs/              # High-performance Rust/Postgres Control Plane
apps/aether_ui/         # Next.js UI Dashboard
packages/aether/        # Python reference server, models, and services
apps/aether_cli/        # Agent CLI tool
tests/aether/           # Test suite
```

---

## Quick Start (Python Reference Stack)

*Note: For the native Rust/Postgres soak validation, refer to `scripts/soak_native_rust_pg.sh`.*

### Installation

```bash
# Clone and install the Python CLI/backend
git clone https://github.com/hehamalainen/Aether.git
cd Aether
pip install -e ".[dev]"
```

### Start Services

```bash
# Start backend (Python reference server; binds to 127.0.0.1:8100 by default)
python -m apps.aether_server.main
# Server starts at http://127.0.0.1:8100

# Start UI
cd apps/aether_ui
npm install
npm run dev
# Dashboard at http://localhost:3000
```

### Verification Gates: Simulation Defaults vs. Real Gate Enforcement

By default, newly created repositories use explicit **`SIMULATED:` no-op gates** (`SIMULATED: build skipped - no-op default gate`, `SIMULATED: unit_tests skipped - no-op default gate`) and record `"_verification_mode": "SIMULATED"` in promotion evidence. This allows swarm coordination and queue-physics benchmarks to run safely without spawning arbitrary shell commands.

To run **real verification gates** (such as `python -m py_compile` and `pytest`) that reject broken proposals before they can advance canonical head:

```bash
# Run the self-contained reproducible example (promotes v1, rejects a broken v2 via pytest, promotes fixed v3)
python examples/reproducible_gate_verification_demo.py
```

> **Security Note:**
> - **Loopback-only default & API token authentication (Python & Rust):** Both the Python server (`apps/aether_server`) and the Rust server (`aether-rs/crates/server`, across both SQLite and PostgreSQL feature profiles) bind to `127.0.0.1` by default and refuse non-loopback `AETHER_HOST` values (e.g. `0.0.0.0`) unless both `AETHER_ALLOW_UNSAFE_NETWORK_BIND=true` and a non-empty `AETHER_API_TOKEN` are configured. When `AETHER_API_TOKEN` is set, all `/v1/*` HTTP endpoints (except `/v1/health`) and both WebSocket endpoints (`/v1/events`, `/v1/repos/{id}/events`) require valid credentials (`Authorization: Bearer <token>`, `X-Aether-Token: <token>`, or `?token=<token>` on WebSockets).
> - **Browser Origin & CORS restrictions:** Browser CORS origins and WebSocket `Origin` headers are restricted to local development origins (`http://localhost:3000`, `http://127.0.0.1:3000`, `http://localhost:8100`, `http://127.0.0.1:8100`) unless explicitly configured via `AETHER_CORS_ORIGINS`.
> - **Trusted gate execution boundary:** Custom shell gate commands are disabled by default over the REST API (`403 Forbidden`) and in `GateRunner` unless `AETHER_ALLOW_CUSTOM_GATE_COMMANDS=true` is explicitly enabled in a trusted, isolated environment.

### CLI Usage

```bash
# Manage repositories
aether repos create --name "my-project"
aether repos list

# Submit a proposal
aether push --repo repo_xxx --diff patch.diff --confidence 0.87 --task task_123

# Watch events
aether watch --repo repo_xxx

# Get current head
aether head --repo repo_xxx
```

---

## API Reference

Aether exposes a shared REST surface across the Python server and Rust/Postgres runtime. The following is an illustrative subset of key routes:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/repos` | GET, POST | List all repos, or Create new repository |
| `/v1/repos/{id}` | GET | Get repository details |
| `/v1/repos/{id}/head` | GET | Get canonical head |
| `/v1/repos/{id}/proposals` | GET, POST | List proposals, or Submit new proposal |
| `/v1/repos/{id}/promotions` | GET, POST | List promotions, or Request promotion |
| `/v1/repos/{id}/snapshots` | GET, POST | List snapshots, or Create snapshot |
| `/v1/repos/{id}/snapshots/{id}` | GET | Get snapshot details |
| `/v1/repos/{id}/snapshots/{id}/download` | GET | Download snapshot blob |
| `/v1/system/physics` | GET | Queue physics telemetry (lambda, mu, backlog, etc.) |
| `/v1/health` | GET | Health check |
| `/v1/repos/{id}/events` | WS | Realtime events |

---

## License

MIT License — See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for details.
