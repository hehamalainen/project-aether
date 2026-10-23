# Advisory Lock Scoping: Strict Optimistic Concurrency Refactoring

## Goal

Investigate and narrow the scope of `pg_advisory_xact_lock` in `crdt_promotion.rs` by moving heavy FS/materialization logic outside the lock using a strict Optimistic Concurrency Control (OCC) model.

## Correctness & Safety Analysis

A naive "try_append" approach for OCC is insufficient and dangerous due to:
1. **Meet-Frontier Vacuuming**: `orchestrate_promotion_bounded` uses the `meet_frontier_clock` to aggressively vacuum CRDT tombstones. If a concurrent branch commits before we do, the actual meet frontier shrinks. Using a speculatively calculated (larger) meet frontier would lead to vacuuming tombstones that are still needed by the new branch, causing CRDT corruption.
2. **Stale Merging**: If the frontier changes, any speculative snapshot generated outside the lock is based on a stale subset of base snapshots. Appending this stale result as a new branch is semantically incorrect and loses context.
3. **Early Rejection Bypass**: Speculatively rejected proposals (e.g. failing a gate, resulting in `policy_score == 0.0`) must not write status updates (`REJECTED`) without the transaction lock and CAS validation. Doing so allows stale speculative state to overwrite concurrent promotions or edits. Rejection must be gated behind the same strict OCC validation as the success path.

## Proposed Architectural Change: Strict Fingerprint OCC

To safely move orchestration out of the lock, we must treat it as a true OCC with a strict compare-and-swap (CAS) on the exact frontier state and repository basis.

### Execution Flow Change

1. **Pre-Lock Phase (Speculative execution)**:
   - Read the current `aether_repositories` state (including `gate_config`, `head_sequence`, `head_id`).
   - Read the pending `aether_proposals`.
   - Read the current `HeadFrontier` (serialize to JSON as a **basis fingerprint**).
   - Compute the expected `parent_context`, `new_dot_early`, and `meet_frontier_clock`.
   - Call `orchestrate_promotion_bounded` **speculatively**. This performs all disk I/O, file merging, and gate validation, producing a valid candidate snapshot on disk.

2. **Commit Phase (Under `pg_advisory_xact_lock`)**:
   - Begin transaction and acquire `pg_advisory_xact_lock`.
   - Re-read `aether_repositories`.
   - Re-read the current `HeadFrontier`.
   - **Strict OCC Validation**: Re-validate the full basis against the speculative fingerprints.
   - If *anything* has changed in the frontier or repo basis, the speculative snapshot was built on a stale meet-frontier or stale base snapshots. **ABORT and rollback with 409 Conflict**. Do NOT attempt to append or reuse the snapshot.
   - **Rejection Gating**: If the speculative evaluation resulted in a rejection (`policy_score == 0.0`), perform the update to `'REJECTED'` *only after* the CAS validation succeeds, and commit.
   - If the basis completely matches and it is a success, proceed to append the event using `frontier.try_append_checked()`, write the snapshot metadata, gate results, promotion row, update proposals to `'PROMOTED'`, update the repository, and commit.

### Expected Impact
- **Throughput**: Heavy operations (100ms - 500ms) occur entirely outside the lock. The locked section shrinks to just metadata updates, drastically reducing `t_lock_hold` and allowing swarm agents to run speculatively in parallel.
- **Correctness**: Strict frontier fingerprinting guarantees that `orchestrate_promotion_bounded` is never committed if the underlying causal context or meet frontier has shifted, preserving the CRDT hardening fixes implemented in Ticket 3.
- **Rejection Integrity**: Moving the rejection path under the lock and CAS validation ensures stale speculative rejections never overwrite concurrent promotions.

## Proposed Changes

### [MODIFY] [crdt_promotion.rs](aether-rs/crates/server/src/crdt_promotion.rs)
- Split `process_promotion` into two clear phases: `Speculative Pre-Lock` and `Transaction Commit`.
- Add fingerprint capture in the speculative phase for `repo.head_id`, `repo.head_sequence`, `repo.gate_config`, and the serialized `HeadFrontier`.
- Move `load_head_frontier`, `resolve_head_context`, and `orchestrate_promotion_bounded` into the speculative phase.
- Move the `policy_score == 0.0` check into the Commit Phase, under the advisory lock and after the CAS validation check, committing status to `'REJECTED'` only if the repository basis has not changed.
- In the Commit Phase, add an explicit CAS check covering the complete basis.

### [MODIFY] [document_store.rs](aether-rs/crates/crdt/src/document_store.rs)
- In `apply_token_smart_edit_bounded`, change the initial `self.ensure_writable(file_path)` to `self.get_or_create_document(file_path)` to prevent unwrapping non-existent paths on new files.

### [MODIFY] [test_semantic_merge.rs](aether-rs/crates/server/tests/test_semantic_merge.rs)
- Update the client-side test assertions and runner to retry on 409 Conflict status codes and assert that timing metric profiles align with the OCC contract (`t_lock_hold < 150ms` instead of `> 450ms` as gates run outside the lock).

## Verification Plan

### Automated Tests
- Run SQLite unit tests: `cargo test --workspace`
- Run Postgres feature profile tests: `DATABASE_URL=postgres://localhost/postgres cargo test --workspace --no-default-features --features db-postgres`
- Ensure all test suites, including `test_semantic_merge.rs` and `promotion_orchestrator::tests` pass cleanly.

