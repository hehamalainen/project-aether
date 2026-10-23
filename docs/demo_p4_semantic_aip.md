# Demo P4 Semantic/AIP Gate

## Goal

Lock semantic conflict improvements and AIP v0 ingestion before scalability runs.

## Command

```bash
./scripts/run_demo_p4_gate.sh
```

## What It Verifies

1. AIP schema validation and parser adapter regression tests pass.
2. Two consecutive strict harness runs pass.
3. Semantic fallback rate stays under target (`--max-fallback-rate`, default `0.05`).
4. Fallback telemetry is visible in `/v1/system/physics` by language and path.

## Artifacts

- Gate report: `.pytest-artifacts/demo/p4-semantic-aip-<timestamp>.json`
- Strict pass 1: `.pytest-artifacts/demo/p4-strict-pass1-<timestamp>.json`
- Strict pass 2: `.pytest-artifacts/demo/p4-strict-pass2-<timestamp>.json`

## Oracle Sufficiency Fail-Open Defect & Mitigation

### Defect Context
During validation, an issue was discovered via inspection of `promoted_proposals`. Specifically, when candidate proposals violated the `must_not_contain` golden assertion criteria of a scenario (resulting in `FAIL_ORACLE_SUFFICIENCY`), the promotion engine would fail to intercept the violation correctly, allowing the adversarial proposals to be promoted instead of being quarantined (the "fail-open" defect).

### Mitigation Details
1. **Engine-Level Verification**: We now enforce the `must_not_contain` golden assertions check directly inside the core promotion logic within `PromotionEngine._promote_internal`. If any target code symbol violates these negative assertions, we raise a dedicated `OracleSufficiencyError`.
2. **Quarantine Logic**: The outer `PromotionEngine.promote` method wraps this logic. When `OracleSufficiencyError` is caught (even when wrapped inside `PromotionPhaseError`), the active transaction is rolled back, and all violating candidate proposals are transitioned to the terminal `QUARANTINED` status with a reason code of `oracle_insufficiency_escalated` to prevent any silent proceed.
3. **Advisory Locking for PG Concurrency**: Transaction-scoped advisory locking via `pg_try_advisory_xact_lock` has been implemented in the core engine. Keys are derived deterministically from conflict sets, affected paths, or proposal IDs, preventing concurrent co-promotions. A try-lock retry loop with exponential backoff and random jitter is used, aborting with `lock_wait_timeout` on exhaustion. Before calling slow external LLM worker synthesis, the active transaction/lock is committed/rolled back, and then re-acquired with a fresh head-verification after worker returns.
4. **Regression Tests**: 
   - SQLite-native fail-closed and quarantine behaviors are validated via regression tests in `tests/aether/test_fail_closed_regression.py` (e.g. validating `class2_subtle_reject` and `oracle_false_green` scenarios).
   - PostgreSQL concurrency and serialization constraints are validated via parallelized tests in `tests/aether/test_postgres_concurrency.py`.
   - The full SQLite battery (`scripts/phase4/run_full_battery.sh`) and PostgreSQL Wave 2B battery (`scripts/phase4/run_wave2b_postgres.sh`) confirm that all scenarios match target classifications.

## Evidence Tracking standing rule

All verification evidence (failing and passing logs) referenced in walkthoughs, plans, or PR reviews must be committed directly to the repository under the [docs/evidence/phase4/](docs/evidence/phase4/) directory. These files are whitelisted in `.gitignore` to ensure they are persistently version-controlled.

## Battery-Blindness findings & mitigation

During adversarial verification, we discovered a "battery blindness" defect class: the original `run_full_battery.sh` verified only scenario outcome classification (e.g., matching `FAIL_ORACLE_SUFFICIENCY`), but was blind to physical database state leaks. Specifically, when a quarantine/rejection error was raised, conflict-set sibling proposals were left stranded in the temporary `EVALUATING` state, and bypassed quarantine routing could go undetected as long as the exception was raised.

To mitigate this:
1. **Row-State Vector Validation**: We upgraded `run_semantic_cascade_load.py` and `run_full_battery.sh` to enforce the full terminal database state vector mapping (checking exact count match for `PROMOTED`, `REJECTED`, `QUARANTINED`, and `SUPERSEDED`, and failing hard on any stranded non-terminal states like `EVALUATING`).
2. **Sibling Proposal Terminalization**: We modified `PromotionEngine.promote` (on quarantine) and `_process_conflict_sets` (on gate rejection) to identify all sibling proposals in the active conflict set and transition them to `QUARANTINED` or `REJECTED` states cleanly.

### Conflict Siblings Mapping (`SUPERSEDED_SEMANTIC_CONFLICT`)
The blueprint design referenced a status named `SUPERSEDED_SEMANTIC_CONFLICT` to represent sibling proposals that were excluded or superseded due to semantic failure of the winning candidate in their conflict set. 
In the shipped implementation:
- **Oracle / Golden Assertion Failure**: Excluded sibling proposals are mapped to `QUARANTINED` status with the reason code `conflict_sibling_failed_oracle_sufficiency`.
- **Gate Failure / Non-Positive Score**: Excluded sibling proposals are mapped to `REJECTED` status with the reason code `conflict_sibling_failed_gates`.

## Blueprint-to-Shipped Terminology Map

The following map translates design concepts from initial blueprints to the concrete implementation shipped in the codebase:

| Blueprint Design Concept | Shipped Codebase Implementation |
|--------------------------|---------------------------------|
| "Orphaned evaluating status" | "Stranded `EVALUATING` proposals" |
| "Ad ad-hoc provenance table" | "SystemMetricEvent database join (no-migration)" |
| "Scenario database row count verification" | "Row-state vector validation" |
| "Fail-closed check bypass" | "Simulated fail-open battery red proof" |
| `SUPERSEDED_SEMANTIC_CONFLICT` | Sibling `QUARANTINED`/`REJECTED` + reason codes |


