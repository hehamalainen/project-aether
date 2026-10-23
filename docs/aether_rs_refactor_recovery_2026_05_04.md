
# Aether Rust Refactor Recovery Memo

## Purpose

This memo reconstructs the missing in-repo documentation for the local `aether-rs/` sprint after the live review session crashed before the Rust migration work was written down properly in this repository.

It is intentionally split into two parts:

- **recovered sprint narrative** from the lost post-mortem text;
- **locally verified implementation state** confirmed from the current `aether-rs/` code.

The goal is simple: preserve enough truth to continue the refactor and soak program safely.

## Recovery status

- A substantial Rust workspace exists locally under `aether-rs/`.
- The Python repo already contains active soak-hardening work (`swarm-run-status`, heartbeat logging, safe harness wrapper, and loop-survival behavior for long runs).
- The missing piece was not the implementation itself; it was the durable repo documentation for that implementation.

## Recovered sprint narrative

The original post-mortem described the sprint as a Python-to-Rust migration of the critical correctness path around:

- promotion;
- materialization;
- trust semantics;
- CRDT-backed canonicalization.

The sprint summary also described the outcome as achieving a **100/100 correctness seal** for the audited correctness loop around promotion, materialization, and trust semantics.

The main architectural claims from that recovered write-up were:

- `crates/crdt`
  - Rust port of the RGA and structured document logic.
- `crates/models`
  - `sqlx` entities aligned to the existing SQLite schema.
- `crates/services`
  - promotion and trust logic.
- `crates/server`
  - `axum` REST server for the migrated path.

The recovered write-up also highlighted the main lessons from the migration:

### What went well

- schema compatibility with the existing SQLite/WAL-backed product state;
- rapid bootstrap of a full Rust workspace;
- using adversarial review as a correctness audit loop rather than only as a code generator;
- removal of Python GIL constraints on the hot request path.

### What went wrong

- early iterations tolerated too many convenience defaults;
- some paths initially drifted toward fail-open or best-effort behavior;
- the largest architectural miss was misunderstanding snapshot delta manifests and assuming every snapshot directory stored a complete file tree.

### Claimed follow-up priorities

1. canonical baseline soaks against the Rust path;
2. mixed-source stress and malformed-input pressure;
3. replacement of sync file reads in async handlers with `tokio::fs`.

## Locally verified implementation state

The following items are directly confirmed from the current `aether-rs/` workspace.

### Workspace shape

`aether-rs/Cargo.toml` defines a multi-crate workspace with:

- `crates/crdt`
- `crates/models`
- `crates/services`
- `crates/server`
- `crates/cli`

### Server stack

The Rust server is implemented with:

- `axum` routes;
- `sqlx` database access;
- SQLite WAL mode;
- a default database path aligned to `~/.aether/aether.db`;
- service wiring for trust-aware request handling.

The current server exposes the same major surfaces as the Python control plane:

- `GET/POST /v1/repos`
- `GET /v1/repos/:id`
- `GET/POST /v1/repos/:id/proposals`
- `GET /v1/repos/:id/head`
- `GET/POST /v1/repos/:id/promotions`

### CRDT and structured document layer

The Rust workspace contains:

- an RGA implementation in `crates/crdt/src/rga.rs`;
- a `structured_doc` module in `crates/crdt/src/structured_doc.rs`;
- property-style CRDT tests in `crates/crdt/tests/rga_proptest.rs`.

The current promotion/materialization flow uses `StructuredDocument` during canonical text reconstruction before hashing snapshot content.

### Trust semantics

The Rust trust service is implemented in `crates/services/src/trust_service.rs` and currently includes:

- `alpha0 = 3.0`
- `beta0 = 3.0`
- `lambda_trust = 0.5`
- `logit`
- `sigmoid`
- trust score calculation from alpha/beta metrics
- trust-adjusted confidence composition

This is wired into promotion evaluation through `PromotionEngine::evaluate_proposal`.

### Snapshot delta-manifest semantics

The most important recovered architectural point is confirmed in code:

Rust snapshot resolution does **not** assume every snapshot directory contains a full file tree.

Instead, `resolve_snapshot_file_content` in `crates/server/src/main.rs` walks:

- `storage_path`
- `.aether_snapshot_manifest.json`
- `mode`
- `base_snapshot_id`
- `changed_paths`
- `deleted_paths`

Behavior currently confirmed:

- manifest cycles are treated as errors;
- missing `storage_path` is treated as an error;
- invalid or unreadable manifest JSON is treated as an error;
- delta manifests must provide `changed_paths`;
- delta manifests must provide a non-empty `base_snapshot_id`;
- deleted paths return `None` rather than silently reviving old content.

This is the concrete implementation evidence behind the recovered claim that delta-manifest semantics were a central migration issue.

### Promotion and materialization behavior

The current Rust promotion flow in `crates/server/src/main.rs` verifies and applies proposals with several correctness-oriented constraints:

- proposal lookup is repo-scoped;
- `affected_paths` must parse as valid JSON;
- trust-aware scoring is delegated to the promotion engine;
- pending proposals are deterministically ordered before materialization;
- `unified_diff` patches are parsed and applied with `diffy`;
- `full_file` overwrites are handled explicitly;
- `crdt_ops` currently returns `501 NOT_IMPLEMENTED`;
- unknown `change_type` values return `400 BAD_REQUEST`;
- repository head update uses a CAS-style sequence guard and returns `409 CONFLICT` on race.

## Verified fail-closed behavior

The current Rust path is materially more strict than the earlier best-effort versions described in the sprint notes.

Concrete fail-closed behaviors visible in code today:

- trust update/query failures bubble up as request failure;
- malformed `affected_paths` fails request processing;
- patch parse or patch apply failures return `400`;
- malformed delta manifests fail request processing;
- unknown change types are rejected instead of being treated as implicit full-file replacements;
- canonical head races fail with `409` rather than silently overwriting state.

## Gaps still visible in the recovered state

Even after the recovery pass, several gaps remain obvious:

### 1. The workspace is not yet represented in front-door docs

At the time of writing, the repo documentation index did not explain the `aether-rs/` migration or point to a Rust-specific walkthrough.

### 2. The local workspace is still fragile from a provenance perspective

`aether-rs/` exists locally, but the strongest durable recovery path is still to stage and push the workspace plus this memo to GitHub as soon as practical.

### 3. Sync file I/O remains in async request paths

`crates/server/src/main.rs` still uses `std::fs::read_to_string(...)` inside async route/materialization flow. That matches the recovered note that correctness is ahead of throughput hardening.

### 4. Convenience fallbacks still exist

Some `unwrap_or_default` / convenience fallback patterns are still present in the Rust workspace and should be reviewed with the same adversarial strictness that was applied to the manifest path.

### 5. Rust-path soak evidence is still not packaged

The repo already has soak tooling and heartbeat/status support, but the in-repo proof package for sustained Rust-path operation is not yet assembled.

## Practical continuation sequence

To resume work safely, the next sequence should be:

1. keep this memo as the canonical recovery artifact for the lost sprint context;
2. stage and push the `aether-rs/` workspace so it is no longer only local state;
3. add a dedicated `aether-rs` README with run/test commands and migration scope;
4. run canonical baseline soaks against the Rust server using the existing safe harness/heartbeat flow;
5. capture heartbeat, JSON, and log artifacts for the Rust path;
6. replace sync file reads with `tokio::fs` before the next major scale/soak round;
7. add adversarial fixtures specifically for malformed or deep delta-manifest chains.

## Bottom line

The missing documentation problem was real, but the implementation itself is also real.

This repo now has a durable recovery record for the Rust migration describing:

- what the sprint claimed to achieve;
- what is verifiably implemented today;
- what still blocks production confidence;
- what to do next before the soak program continues.
