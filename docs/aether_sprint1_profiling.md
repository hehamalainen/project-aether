# Sprint 1: T-Optimization Execution Notes

This document captures the implementation details and strategic shifts established during the Sprint 1 execution for the Aether Rust backend.

## 1. Zero-Drift Instrumentation in PostgreSQL

The PostgreSQL backend (`db_postgres.rs`) is deeply instrumented to measure the entire transaction loop of the promotion engine. The measured phases span the complete lifecycle and are captured natively by `OrchestratedPromotion` and the database handler:
- `t_db_read_pre_orch`
- `t_crdt_eval`
- `t_materialize`
- `t_gates`
- `t_trust_math`
- `t_fs_rename`
- `t_db_tx_start`
- `t_db_revalidate`
- `t_db_insert_snapshot_gates`
- `t_db_update_proposals_trust`
- `t_db_insert_promotions_heads_cas`
- `t_db_commit`
- `t_total_inner`

## 2. Evidence Bag Persistence

The timing map is natively serialized and merged into `evidence["_timings_ms"]`. It is cleanly persisted into the `aether_promotions` database table. 

**Note on Persisted Timings:** 
The DB-persisted timings are inherently partial (capturing only the phases up to the CAS insert, thus omitting `t_db_insert_promotions_heads_cas` entirely, its internal sub-stages `t_db_insert_promotion`, `t_db_insert_head`, `t_db_cas_update`, as well as `t_db_commit` and the final `t_total_inner`), while the API response carries the fuller map. This ensures long-term telemetry analysis is possible across millions of stored promotions without creating a physical paradox during insertion.

## 3. End-to-End Verification

The E2E test suite (`scripts/test_pg_e2e.sh`) has been hardened to aggressively validate the telemetry payload. It correctly asserts that:
- `_timings_ms` exists.
- All 13 legacy composite keys are present, alongside the newly introduced diagnostic sub-stages.
- Every key contains valid, numeric floating-point data.
- `t_total_inner` is strictly > 0.
- `_timings_ms` correctly cascades to the evidence payload in rejection responses when a policy violation occurs.

## 4. Strategic Shift: SQLite Deprecation

> [!WARNING]
> SQLite is no longer a parity target. It will intentionally drift from the PostgreSQL control-plane and will act only as transition scaffolding until full deprecation. We will **not** invest further effort in maintaining SQLite parity for new features going forward.
