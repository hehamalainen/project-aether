# OCC Race-Window Correctness Proof

This document provides the correctness reasoning for review, analyzing whether Aether's Optimistic Concurrency Control (OCC) and advisory transaction locking design is robust under sustained concurrency, preventing all double-promotion, stale-basis, and race-window violations.

---

## 1. Complete Writer Coverage

To establish strict OCC safety, we must prove that **every** execution path mutating any CAS-fingerprinted field is serialized or gated such that no concurrent transaction can commit on a stale basis.

### 1.1 Fingerprinted Fields
The CAS basis fingerprint consists of:
1. `Repository.head_id` (`aether_repositories.head_id`)
2. `Repository.head_sequence` (`aether_repositories.head_sequence`)
3. `Repository.gate_config` (`aether_repositories.gate_config`)
4. `HeadFrontier` (`aether_head_frontiers.frontier_json`)
5. `Proposal.status` (`aether_proposals.status` - specifically transition to/from `PENDING`)

### 1.2 Writer Paths Enumeration
There are exactly four database write paths in the Aether-RS server workspace:

| Crate & Path | Operation | Target Table(s) | Transaction Lock/Isolation Scheme |
|---|---|---|---|
| `crdt_promotion.rs` (`request_promotion_crdt_inner`) | Promotion Commit (CRDT Mode) | `aether_repositories`<br>`aether_head_frontiers`<br>`aether_proposals`<br>`aether_heads`<br>`aether_promotions` | **Pessimistic Advisory Lock + SQL CAS**:<br>1. Transaction starts.<br>2. Acquires `pg_advisory_xact_lock(hash(repo_id))`. <br>3. Re-reads repo/frontier under lock.<br>4. Re-validates speculative fingerprints.<br>5. SQL CAS `UPDATE` on `aether_repositories`. |
| `db_postgres.rs` (`request_promotion_inner_scalar`) | Promotion Commit (Scalar Mode) | `aether_repositories`<br>`aether_proposals`<br>`aether_heads`<br>`aether_promotions` | **SQL CAS + Postgres Row Lock**:<br>1. Transaction starts.<br>2. Re-reads repo state.<br>3. Re-validates speculative sequence.<br>4. Executes SQL CAS `UPDATE` on `aether_repositories` with row lock. |
| `db_postgres.rs` (`commit_promotion`) | External Promotion Hook | `aether_repositories`<br>`aether_proposals`<br>`aether_heads`<br>`aether_promotions`<br>`aether_conflict_sets` | **SQL CAS + Postgres Row Lock**:<br>1. Transaction starts.<br>2. Reads repo state.<br>3. Executes SQL CAS `UPDATE` on `aether_repositories` with row lock. |
| `db_postgres.rs` / `main.rs` (`submit_proposal`) | Proposal Intake | `aether_proposals` | **Append-Only Write**:<br>1. Inserts new proposal with status `PENDING` (no modifications to repository head or frontier). |

### 1.3 Advisory Lock vs Row Lock Equivalence
For CRDT-enabled repositories, the promotion path (`request_promotion_crdt_inner`) is strictly synchronized via `pg_advisory_xact_lock` using a 64-bit key generated from the repository ID. This guarantees that:
- **Only one** transaction can be in the critical section (re-read → CAS check → write) for a given repository at any time.
- Standard Postgres row-level locks on `UPDATE` guarantee serialization between the advisory-locked CRDT path and the row-locked non-CRDT paths (`commit_promotion`, `request_promotion_inner_scalar`).

Specifically:
- If a CRDT promotion transaction holds the advisory lock and updates the repository row, it holds a row-level write lock. Any concurrent `commit_promotion` or scalar promotion attempting to update the same row will block until the CRDT transaction commits. Once it commits, the blocked transaction's `UPDATE WHERE head_sequence = old_seq` condition evaluates to false (as sequence has advanced), resulting in 0 rows updated and an abort with `409 Conflict`.
- Conversely, if a scalar promotion or `commit_promotion` updates the row first and commits, the CRDT path (upon acquiring the advisory lock) reads the updated `head_id` and `head_sequence` from the database, detects the mismatch against the speculative basis, and aborts immediately prior to performing any writes.

Thus, **universal coverage** is established under the contract: no writer path can bypass the concurrency controls.

---

## 2. CAS-to-Commit Atomicity Proof

We prove that between the under-lock re-read, the CAS check, and the commit, no other transaction can change the basis.

Let:
- \(R\) be a repository.
- \(F\) be the HeadFrontier of \(R\).
- \(S_k\) be the speculative state computed outside the lock, including speculative frontier \(F_{spec}\) and speculative repo head \(H_{spec} = (\text{head\_id}, \text{head\_sequence})\).
- \(T_c\) be a transaction executing `request_promotion_crdt_inner` to commit \(S_k\).

### 2.1 The Timeline of \(T_c\)
1. **Time \(t_0\)**: Speculative evaluation phase completes. \(T_c\) starts its transaction and requests `pg_advisory_xact_lock(R)`.
2. **Time \(t_1\)**: Lock acquired. \(T_c\) now enters the critical section.
3. **Time \(t_2\)**: Re-reads the current basis from database: \(F_{curr}\) and \(H_{curr}\).
4. **Time \(t_3\)**: Performs CAS check:
   \[
   \text{CAS} \equiv (F_{curr} == F_{spec}) \land (H_{curr} == H_{spec}) \land \text{proposals\_pending}
   \]
5. **Time \(t_4\)**: If CAS check passes, \(T_c\) writes promotions and heads to database.
6. **Time \(t_{cas}\)**: \(T_c\) executes a strict SQL CAS update on \(R\)'s repository row:
   \[
   \text{UPDATE } aether\_repositories \dots \text{ WHERE head\_id } = H_{spec}.\text{head\_id} \land \text{head\_sequence } = H_{spec}.\text{head\_sequence}
   \]
   and verifies that `rows_affected() == 1`.
7. **Time \(t_5\)**: \(T_c\) commits, releasing both the row-level lock and the advisory lock.

### 2.2 Proof of Mutual Exclusion
Suppose another transaction \(T_x\) commits a change to \(R\)'s head or frontier during \(T_c\)'s execution. We analyze all possible commit times \(t_x\) of \(T_x\):

- **Case 1: \(t_x < t_1\)**
  Since \(T_x\) committed before \(T_c\) acquired the advisory lock, the database state was updated. When \(T_c\) re-reads the basis at \(t_2\), it will observe \(F_{curr} \neq F_{spec}\) or \(H_{curr} \neq H_{spec}\). The CAS check at \(t_3\) will fail, and \(T_c\) will rollback with `409 Conflict`. No race occurs.

- **Case 2: \(t_1 < t_x < t_{cas}\)**
  - If \(T_x\) is another CRDT promotion transaction, it must acquire the same advisory lock `pg_advisory_xact_lock(R)`. Because advisory locks are mutually exclusive, \(T_x\) cannot enter its critical section or write until \(T_c\) commits at \(t_5\). This is a contradiction.
  - If \(T_x\) is a scalar promotion or out-of-band `commit_promotion` transaction, it does not acquire the advisory lock. Since it executes concurrently, it can execute its update first:
    \[
    \text{UPDATE } aether\_repositories \dots \text{ WHERE head\_id } = H_{spec}.\text{head\_id} \land \text{head\_sequence } = H_{spec}.\text{head\_sequence}
    \]
    It acquires the row lock on \(R\)'s row, updates the row to \(H_{new\_x}\), and commits.
    When \(T_c\) subsequently reaches \(t_{cas}\) and attempts to update the repository row:
    \[
    \text{UPDATE } aether\_repositories \dots \text{ WHERE head\_id } = H_{spec}.\text{head\_id} \land \text{head\_sequence } = H_{spec}.\text{head\_sequence}
    \]
    The database row now has \(H_{new\_x}\). Because \(H_{new\_x}.\text{head\_sequence} \neq H_{spec}.\text{head\_sequence}\), the `WHERE` condition of \(T_c\)'s update evaluates to false.
    The update query affects **0 rows**. \(T_c\) detects this immediately, rolls back the transaction, deletes the candidate snapshot, and returns a `409 Conflict`.
    Thus, no race occurs and stale overrides are prevented.

- **Case 3: \(t_x > t_{cas}\)**
  Since \(T_c\) has already executed its update at \(t_{cas}\) and holds the row-level write lock, any concurrent update from \(T_x\) will block waiting for the row lock. Once \(T_c\) commits at \(t_5\), \(T_x\) wakes up, evaluates its `WHERE` clause, finds that the sequence has advanced to \(H_{new}\), updates 0 rows, and aborts. No race occurs.

### 2.3 Conclusion
Since all cases are exhaustive and mutually exclusive, the combination of transaction-level advisory locks and final query-level CAS updates provides robust atomicity for the CAS-to-commit window. Stale updates and double-promotions are prevented under this design, assuming the writer-coverage audit remains complete and no uncovered paths are introduced.

---

## 3. Implementation Invariants & Hardening Rules

To translate this correctness proof safely into a production-grade system, the implementation must adhere strictly to the following three invariants.

### 3.1 The Conditional Aggregate Root Versioning Invariant (Secondary Table TOCTOU)
*   **The Constraint**: Any out-of-band administrative script, endpoint, or secondary worker that mutates any field included in the CAS fingerprint (e.g. `gate_config` on `aether_repositories`, or a proposal's status on `aether_proposals`) must forcefully execute `UPDATE aether_repositories SET head_sequence = head_sequence + 1, updated_at = NOW() WHERE id = :id`.
*   **Rationale**: The speculative CAS validation evaluates multi-table state (such as verifying that all target proposals are still in a `PENDING` state). If an out-of-band action mutates a proposal's status to `REJECTED` or changes a repository's `gate_config` without bumping the aggregate root's sequence number, a concurrent promotion's final CAS check on `head_sequence` would succeed. This would result in committing a promotion built on stale configuration or rejected proposals. 
*   **Verdict**: This invariant is conditional: it is required only if an uncovered secondary-table writer exists or is introduced. The proposed safety argument assumes that all active server paths mutate these entities either under the same lock/sequence-bump discipline or within the same transaction scope, meaning no uncovered writers are expected. This assumption remains subject to final review and sign-off, and this rule remains a strict requirement for future API extensions or administrative tools.

### 3.2 The Explicit "Rows Affected" Catch Invariant
*   **The Constraint**: The application layer must manually check the rows affected immediately after executing the repository update statement. For example, in Rust SQLx:
    ```rust
    if rows_affected == 0 {
        return Err(Conflict); // roll back and return 409 Conflict
    }
    ```
*   **Rationale**: Under PostgreSQL's default `READ COMMITTED` transaction isolation, an `UPDATE` statement that affects 0 rows due to an EPQ (EvalPlanQual) filter mismatch (i.e. the `head_sequence` advanced concurrently) does not throw a SQL error. The database query completes successfully from the DBMS perspective. Therefore, the proof holds if and only if the application explicitly validates the rows affected to force a transaction rollback. Both `request_promotion_crdt_inner` and `request_promotion_inner_scalar` strictly enforce this check.

### 3.3 Deadlock & Liveness Audit Recommendation
*   **The Recommendation**: Ensure that all concurrent database transaction paths acquire table locks in a consistent order (e.g. updating the aggregate root `aether_repositories` first) to mitigate lock-inversion deadlocks.
*   **Rationale**: This is a liveness concern rather than a correctness/safety axiom, as safety is maintained in deadlock abort scenarios (PostgreSQL rolls back one of the deadlocked transactions, raising a `40P01` error). To prevent performance degradation and aborted transactions under heavy concurrent load, a liveness audit should confirm that multi-table writes follow a consistent order. Under the proposed design for review, both active promotion paths execute repository-row CAS updates near the end of the transaction after pre-validations, which is expected to prevent lock-inversion deadlocks.
