# Conditional OCC & Advisory-Locking Safety Argument (Design Reasoning & Explicit Assumptions)

> [!NOTE]
> **Scope & Status of This Document:** This document presents a **conditional design argument** for how Optimistic Concurrency Control (OCC) and PostgreSQL transaction-scoped advisory locks interact in the `aether-rs` PostgreSQL backend (`--features db-postgres`). It is **not** a mechanized or machine-checked formal proof, **not** a system-wide correctness guarantee across all Aether backends, and **not** a proof of code-quality verification. In particular:
> 1. The argument is **conditional on explicit assumptions** (such as the absence of uncovered secondary-table writers and strict application-level checking of `rows_affected() == 1`).
> 2. One of the four writer paths analyzed below (`commit_promotion` at `POST /v1/repos/:id/commit_promotion`) is a **trusted external-commit hook** that advances canonical head under SQL CAS **without** independently executing verification gates.

---

## 1. Enumerated Writer Paths & Assumptions

Within the `aether-rs` PostgreSQL backend (`crates/server`), the design argument considers whether the enumerated database write paths that mutate CAS-fingerprinted fields serialize on `aether_repositories` so that concurrent transactions cannot silently overwrite `head_sequence`.

### 1.1 Fingerprinted Fields
The CAS basis fingerprint consists of:
1. `Repository.head_id` (`aether_repositories.head_id`)
2. `Repository.head_sequence` (`aether_repositories.head_sequence`)
3. `Repository.gate_config` (`aether_repositories.gate_config`)
4. `HeadFrontier` (`aether_head_frontiers.frontier_json`)
5. `Proposal.status` (`aether_proposals.status` - specifically transition to/from `PENDING`)

### 1.2 Writer Paths Enumeration
There are four database write paths in the `aether-rs` server workspace:

| Crate & Path | Operation | Target Table(s) | Transaction Lock/Isolation Scheme |
|---|---|---|---|
| `crdt_promotion.rs` (`request_promotion_crdt_inner`) | Promotion Commit (CRDT Mode, `AETHER_CRDT_MODE=true`) | `aether_repositories`<br>`aether_head_frontiers`<br>`aether_proposals`<br>`aether_heads`<br>`aether_promotions` | **Advisory Lock + SQL CAS**:<br>1. Transaction starts.<br>2. Acquires `pg_advisory_xact_lock(hash(repo_id))`. <br>3. Re-reads repo/frontier under lock.<br>4. Re-validates speculative fingerprints.<br>5. SQL CAS `UPDATE` on `aether_repositories`. |
| `db_postgres.rs` (`request_promotion_inner_scalar`) | Promotion Commit (Scalar Mode, default) | `aether_repositories`<br>`aether_proposals`<br>`aether_heads`<br>`aether_promotions` | **SQL CAS + Postgres Row Lock**:<br>1. Transaction starts.<br>2. Re-reads repo state.<br>3. Re-validates speculative sequence.<br>4. Executes SQL CAS `UPDATE` on `aether_repositories` with row lock. |
| `db_postgres.rs` (`commit_promotion`) | Trusted External Promotion Hook (`POST /v1/repos/:id/commit_promotion`) | `aether_repositories`<br>`aether_proposals`<br>`aether_heads`<br>`aether_promotions`<br>`aether_conflict_sets` | **SQL CAS + Postgres Row Lock (No Gate Execution)**:<br>1. Transaction starts.<br>2. Reads repo state.<br>3. Executes SQL CAS `UPDATE` on `aether_repositories` with row lock using caller-supplied snapshot/score. |
| `db_postgres.rs` / `main.rs` (`submit_proposal`) | Proposal Intake | `aether_proposals` | **Append-Only Write**:<br>1. Inserts new proposal with status `PENDING` (no modifications to repository head or frontier). |

### 1.3 Interaction Between Advisory Locks and Row-Level CAS Updates
For CRDT-enabled repositories (`AETHER_CRDT_MODE=true`), the promotion path (`request_promotion_crdt_inner`) acquires `pg_advisory_xact_lock` using a 64-bit key derived from the repository ID. Under PostgreSQL's `READ COMMITTED` isolation:
- At most one CRDT promotion transaction can be in the advisory-locked critical section (re-read → CAS check → write) for a given repository at a time.
- PostgreSQL row-level write locks on `UPDATE aether_repositories` serialize the final repository-row update between the advisory-locked CRDT path and the non-advisory-locked paths (`commit_promotion`, `request_promotion_inner_scalar`).

Specifically:
- If a CRDT promotion transaction holds the advisory lock and updates the repository row, it holds a row-level write lock on `aether_repositories`. Any concurrent `commit_promotion` or scalar promotion attempting to update the same row blocks until the CRDT transaction commits. Once it commits, the blocked transaction's `UPDATE WHERE head_sequence = old_seq` predicate evaluates to false (because `head_sequence` has advanced), resulting in `0` rows updated and an abort with `409 Conflict`.
- Conversely, if a scalar promotion or `commit_promotion` updates the repository row first and commits, the CRDT path either observes the updated `head_id` / `head_sequence` when re-reading under the advisory lock or affects `0` rows on its own `WHERE head_id = ... AND head_sequence = ...` update, aborting with `409 Conflict`.

This serialization property holds **only** for the repository-row CAS transition and assumes no uncovered writer path mutates secondary tables without bumping `head_sequence` (see Section 3.1).

---

## 2. Case Analysis of the Repository-Row CAS Window

We analyze the interleavings between the under-lock re-read, the SQL CAS `UPDATE` on `aether_repositories`, and transaction commit.

Let:
- \(R\) be a repository.
- \(F\) be the `HeadFrontier` of \(R\).
- \(S_k\) be the speculative state computed outside the lock, including speculative frontier \(F_{spec}\) and speculative repo head \(H_{spec} = (\text{head\_id}, \text{head\_sequence})\).
- \(T_c\) be a transaction executing `request_promotion_crdt_inner` to commit \(S_k\).

### 2.1 The Timeline of \(T_c\)
1. **Time \(t_0\)**: Speculative evaluation phase completes. \(T_c\) starts its transaction and requests `pg_advisory_xact_lock(R)`.
2. **Time \(t_1\)**: Lock acquired. \(T_c\) enters the critical section.
3. **Time \(t_2\)**: Re-reads the current basis from the database: \(F_{curr}\) and \(H_{curr}\).
4. **Time \(t_3\)**: Performs the in-application CAS check:
   \[
   \text{CAS} \equiv (F_{curr} == F_{spec}) \land (H_{curr} == H_{spec}) \land \text{proposals\_pending}
   \]
5. **Time \(t_4\)**: If the check passes, \(T_c\) inserts promotion and head records within the transaction.
6. **Time \(t_{cas}\)**: \(T_c\) executes the SQL CAS update on \(R\)'s repository row:
   \[
   \text{UPDATE } aether\_repositories \dots \text{ WHERE head\_id } = H_{spec}.\text{head\_id} \land \text{head\_sequence } = H_{spec}.\text{head\_sequence}
   \]
   and checks that `rows_affected() == 1`.
7. **Time \(t_5\)**: \(T_c\) commits, releasing both the row-level lock and the advisory lock.

### 2.2 Interleaving Cases
Suppose another transaction \(T_x\) commits a change to \(R\)'s `aether_repositories` row (`head_id` / `head_sequence`) during \(T_c\)'s execution. Consider the commit time \(t_x\) of \(T_x\):

- **Case 1: \(t_x < t_1\)**
  Since \(T_x\) committed before \(T_c\) acquired the advisory lock, the database state was already updated. When \(T_c\) re-reads the basis at \(t_2\), it observes \(F_{curr} \neq F_{spec}\) or \(H_{curr} \neq H_{spec}\). The check at \(t_3\) fails, and \(T_c\) rolls back with `409 Conflict`.

- **Case 2: \(t_1 < t_x < t_{cas}\)**
  - If \(T_x\) is another CRDT promotion transaction, it must acquire the same advisory lock `pg_advisory_xact_lock(R)`. Because transaction-level advisory locks for the same key are mutually exclusive, \(T_x\) cannot enter its critical section until \(T_c\) finishes at \(t_5\).
  - If \(T_x\) is a scalar promotion or out-of-band `commit_promotion` transaction, it does not acquire the advisory lock. If it executes its repository-row update before \(t_{cas}\):
    \[
    \text{UPDATE } aether\_repositories \dots \text{ WHERE head\_id } = H_{spec}.\text{head\_id} \land \text{head\_sequence } = H_{spec}.\text{head\_sequence}
    \]
    it acquires the row lock on \(R\)'s row, advances `head_sequence` to \(H_{new\_x}\), and commits.
    When \(T_c\) subsequently reaches \(t_{cas}\) and attempts to update the repository row, the row holds \(H_{new\_x}.\text{head\_sequence} \neq H_{spec}.\text{head\_sequence}\). The `WHERE` predicate evaluates to false, affecting **0 rows**. Because \(T_c\) checks `rows_affected() == 1`, it rolls back the transaction, cleans up the candidate snapshot, and returns `409 Conflict`.

- **Case 3: \(t_x > t_{cas}\)**
  Since \(T_c\) has already executed its update at \(t_{cas}\) and holds the row-level write lock on `aether_repositories`, any concurrent update on that row from \(T_x\) blocks waiting for the row lock. Once \(T_c\) commits at \(t_5\), \(T_x\) wakes up, re-evaluates its `WHERE` predicate under PostgreSQL `READ COMMITTED` EvalPlanQual rules, sees the advanced `head_sequence`, updates 0 rows, and aborts with `409 Conflict`.

### 2.3 Scope of the Conclusion
Under the assumptions enumerated in Section 3, combining transaction-level advisory locks with a final `WHERE head_id = ... AND head_sequence = ...` CAS update prevents concurrent promotion transactions from committing two divergent heads at the same `head_sequence`. This argument applies only to database state transitions on the Rust PostgreSQL backend and does not establish liveness under high contention (where HTTP `409` abort rates can exceed `90%`; see `docs/sprint4d_topology_baseline.md`) or gate verification safety on the trusted `commit_promotion` hook.

---

## 3. Explicit Assumptions & Required Implementation Invariants

The safety argument above is **conditional** and holds only when the following invariants are maintained:

### 3.1 Conditional Aggregate Root Versioning Assumption (Secondary Table TOCTOU)
*   **The Assumption**: Any administrative script, endpoint, or secondary worker that mutates a field included in the speculative pre-check (such as `gate_config` on `aether_repositories`, or a proposal's `PENDING` status on `aether_proposals`) while a promotion is in flight must also advance `head_sequence` on `aether_repositories` (or acquire the same lock).
*   **Why This Matters**: In Case 2 above, if a non-advisory-locked transaction mutated `aether_proposals` without updating `aether_repositories.head_sequence` between \(t_2\) and \(t_{cas}\), the final SQL CAS predicate on `aether_repositories` would not detect the secondary-table change. This design argument assumes that no uncovered secondary-table writer transitions a `PENDING` proposal out from under an active promotion without advancing `head_sequence`.

### 3.2 Explicit `rows_affected() == 1` Check Requirement
*   **The Requirement**: The application layer must explicitly check `rows_affected()` immediately after executing the `UPDATE aether_repositories` statement:
    ```rust
    if rows_affected == 0 {
        return Err(Conflict); // roll back and return 409 Conflict
    }
    ```
*   **Why This Matters**: Under PostgreSQL's `READ COMMITTED` isolation, an `UPDATE` statement whose `WHERE` clause matches 0 rows after waiting on a row lock completes without raising a SQL error. The safety argument holds if and only if every writer path explicitly checks `rows_affected() == 0` and rolls back the enclosing transaction.

### 3.3 Liveness vs. Safety Under Lock Contention
*   **Limitation**: Serialization via `pg_advisory_xact_lock` and SQL CAS preserves safety by aborting losing transactions with `409 Conflict` or blocking them on the per-repository lock. As documented in [`docs/sprint4d_topology_baseline.md`](../sprint4d_topology_baseline.md) and [`docs/sprint4e_mitigation_experiments.md`](../sprint4e_mitigation_experiments.md), under high single-repository concurrency (`50–100` agents on `1` repository), this serialization causes high `409` retry rates and multi-second tail latency.
