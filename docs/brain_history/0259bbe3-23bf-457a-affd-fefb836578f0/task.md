# Tasks

- [x] Strict OCC Refactoring of `crdt_promotion.rs`
  - [x] Read and understand the full `process_promotion` function
  - [x] Identify the exact split point between speculative and commit phases
  - [x] Implement fingerprint capture in the speculative phase
  - [x] Move orchestration logic outside the lock
  - [x] Implement strict CAS validation in the commit phase
  - [x] Implement `all_proposals_still_pending` helper
  - [x] Wire up 409 Conflict abort on basis mismatch
  - [x] Preserve all existing telemetry timings
  - [x] Move early rejection check under the transaction lock + CAS validation to protect against stale writes
  - [x] Fix document_store ensure_writable panic on new paths (use get_or_create_document)
  - [x] Update test_semantic_merge.rs to handle 409 retries and assert correct OCC timing limits (< 150ms lock hold)
  - [x] Build and verify SQLite profile: `cargo test --workspace`
  - [x] Build and verify Postgres profile: `DATABASE_URL=... cargo test --workspace --no-default-features --features db-postgres`

