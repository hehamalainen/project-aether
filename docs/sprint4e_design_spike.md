# Sprint 4E Causal Frontiers Access-Pattern Design Spike

> [!NOTE]
> **Hypothesis-Driven Design Spike Status:**
> This document compiles the findings of a strict, hypothesis-driven feasibility study under a code freeze. It evaluates alternative database schemas and concurrency invariants for `aether_head_frontiers`. This does **not** constitute an approved redesign or a chosen cure. All implementation remains frozen, and any future development is strictly gated on long-run baseline confirmation.

---

## 1. Schema Design Options & Rust Modeling

To address the single-row `UPDATE` hotspot, we evaluated three relational layout candidates: two append-only candidates and one sharded mutable comparison candidate.

### Candidate 1: Revision-Incremental Generations (Append-Only State Log)
Every promotion appends the entire new version vector frontier JSON as a new record with a monotonic `revision_id` per repository:
```sql
CREATE TABLE aether_head_frontiers_generations (
    id BIGSERIAL PRIMARY KEY,
    repo_id VARCHAR(255) NOT NULL,
    revision_id BIGINT NOT NULL,
    frontier_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_head_frontiers_generations_repo_rev 
    ON aether_head_frontiers_generations(repo_id, revision_id DESC);
```

#### Proposed Rust/SQLx Modeling:
```rust
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct HeadFrontierGeneration {
    pub id: i64,
    pub repo_id: String,
    pub revision_id: i64,
    pub frontier_json: String,
    pub created_at: chrono::DateTime<chrono::Utc>,
}

// Proposed SQLx Read-Path query
pub async fn fetch_latest_frontier(
    executor: &mut sqlx::PgConnection,
    repo_id: &str,
) -> Result<Option<String>, sqlx::Error> {
    sqlx::query_scalar!(
        r#"
        SELECT frontier_json 
        FROM aether_head_frontiers_generations 
        WHERE repo_id = $1 
        ORDER BY revision_id DESC 
        LIMIT 1;
        "#,
        repo_id
    )
    .fetch_optional(executor)
    .await
}
```

---

### Candidate 2: Event-Sourced Causal Log (Atomic Delta Log with Monotonic Sequence)
Every promotion appends individual head insertions or pruning events. To ensure deterministic replay, this option introduces a monotonic per-repository sequence key (`repo_revision_id` BIGINT) rather than relying on timestamp sorting, which is insufficient for resolving concurrent events at high parallel frequencies:
```sql
CREATE TABLE aether_frontier_events (
    id BIGSERIAL PRIMARY KEY,
    repo_id VARCHAR(255) NOT NULL,
    repo_revision_id BIGINT NOT NULL, -- Explicit sequence key for deterministic ordering
    head_id VARCHAR(255) NOT NULL,
    event_type VARCHAR(20) NOT NULL, -- 'ADD_HEAD', 'PRUNE_HEAD'
    clock BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_frontier_events_repo_rev 
    ON aether_frontier_events(repo_id, repo_revision_id DESC);
```

---

### Candidate 3: Sharded Version-Vector Cells (Sharded Mutable Comparison Candidate)
> [!IMPORTANT]
> **Comparison Candidate (Not Append-Only):**
> This candidate stores individual version-vector clock cells as independent rows. Because advancing a clock still updates the cell in-place, this is **not** an append-only design. It is analyzed strictly to evaluate if sharding row keys reduces row-level write contention compared to the single-row version-vector baseline.
```sql
CREATE TABLE aether_frontier_vector_cells (
    repo_id VARCHAR(255) NOT NULL,
    head_id VARCHAR(255) NOT NULL,
    clock BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (repo_id, head_id)
);
```

---

## 2. Concurrency Linearization & Replay Invariants

To maintain transactional consistency across parallel agent promotions under an append-only architecture, we analyzed three critical invariants:

1.  **Monotonic Revision Growth**:
    Reads on `/head` must never observe a stale frontier revision after a promotion transaction commits. Under Candidate 1, we must evaluate using an advisory transaction lock or PostgreSQL `SERIALIZABLE` isolation to guarantee gap-free, monotonic sequence generation and prevent race conditions between parallel insert statements.
2.  **Read Concurrency and Write-Path Serialization**:
    Rather than assuming lock-free reads by definition, this spike analyzes how layout changes affect write-path serialization and transaction isolation. Note that the current `t_lock_wait` telemetry is a write-path promotion metric, not a read-path `get_head` metric. The spike will analyze if append-only schemas simplify transaction isolation under high write concurrency.
3.  **Deterministic Reconstruction**:
    Dynamic reconstruction from event logs (Candidate 2) or sharded cells (Candidate 3) must be mathematically equivalent to RGA-merged version vectors. Reconstructing the frontier from a historical delta log requires processing events in strict sequence. If concurrent transactions write events out of order, the reconstruction query must use the monotonic sequence key `repo_revision_id` to establish a stable, deterministic replay order.

---

## 3. Standalone Database Footprint Simulation Results

To back up our operational analysis with empirical measurements, we built and executed a standalone Python microbenchmark (`scratch/simulate_append_only.py`) against a local PostgreSQL instance. The script executed concurrent write threads simulating parallel agent workloads.

> [!WARNING]
> **Microbenchmark Scope Warning:**
> This scratch simulation measures physical storage footprints and write-transaction latencies only. It does **not** validate, simulate, or prove CRDT convergence, state replication correctness, read-path projected materialization, or full Aether server runtime logic.

### 3.1. Benchmarking Matrix ($N=80$ and $N=160$ Concurrency Profiles)

The table below compiles the raw metrics compiled across the two isolation sweeps:

| Metric | Sweep 1: UPDATE-in-place ($N=80$) | Sweep 2: INSERT-append ($N=80$) | Sweep 1: UPDATE-in-place ($N=160$) | Sweep 2: INSERT-append ($N=160$) |
| :--- | :---: | :---: | :---: | :---: |
| **Committed Transactions** | 400 | 400 | 480 | 480 |
| **Total Duration (s)** | 2.24 s | 2.17 s | 3.44 s | 3.57 s |
| **Base Table Size** | **0.133 MB** | 0.391 MB | **0.156 MB** | 0.352 MB |
| **TOAST Relation Size** | 0.039 MB | 0.039 MB | 0.039 MB | 0.039 MB |
| **Total Index Size** | **0.016 MB** | 0.055 MB | **0.016 MB** | 0.070 MB |
| **Total Relation Size** | **0.188 MB** | 0.484 MB | **0.211 MB** | 0.461 MB |
| **Base Live / Dead Tuples** | 1 / 17 | **401 / 0** | 1 / 29 | **481 / 0** |
| **TOAST Live / Dead Tuples**| 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| **Write Latency P50** | **374.04 ms** | 399.44 ms | **889.39 ms** | 930.34 ms |
| **Write Latency P95** | **463.08 ms** | 484.56 ms | **1144.04 ms** | 1181.65 ms |
| **Write Latency P99** | 588.22 ms | **528.24 ms** | 1568.27 ms | **1548.55 ms** |

---

## 4. Key Operational Tradeoffs & Findings

### 4.1. Sizing Footprint Tradeoff: Dead Tuples vs. Live Generation Growth
*   **Base-Table Dead Tuples Diverged; TOAST Behavior Remained Inconclusive**: The simulation confirms that the base-table dead tuples diverged: the `UPDATE` table accumulated **17 dead tuples** ($N=80$) and **29 dead tuples** ($N=160$), while the `INSERT` table maintained exactly **0 dead tuples** on its base relation. However, because both tables maintained a flat **0.039 MB** TOAST size and reported **0 / 0** live/dead TOAST tuples across all runs, the physical TOAST behavior remained inconclusive in this short-run microbenchmark.
*   **Base Table Sizing Expansion**: Because the append-only layout physically retains all historical generations as live rows, its base table size grew significantly larger (**0.391 MB** vs **0.133 MB** under $N=80$).
*   **Operational Tradeoff**: This confirms that **append-only base-table dead tuples diverged, but TOAST behavior remained inconclusive in this microbenchmark**. It demonstrates that append-only eliminates base-table dead tuples but accelerates live-row generation growth. Without an active background garbage collection and history pruning daemon, the append-only layout will continuously expand.

### 4.2. Index Bloat and Query Complexity
*   **Index Size Overhead**: Because the append-only table contains hundreds of generations, its index size is larger (**0.055 MB** vs **0.016 MB** under $N=80$) to track the historical records.
*   **Read Query Cost**: The `UPDATE` table can be read via a simple primary-key lookup. The append-only table requires sorting by a descending index (`ORDER BY revision_id DESC LIMIT 1`) or querying a sub-select to find the latest active revision, which increases CPU read cost.

### 4.3. Transactional Latency Performance
*   **Execution Parity**: The end-to-end subprocess timings were similar (e.g., P95 latency of **463.08 ms** for UPDATE vs. **484.56 ms** for INSERT under $N=80$), but this method does not isolate DB write-path cost due to process startup and connection spawning overhead on every command. It indicates raw procedural execution parity under this specific simulation environment.

---

## 5. Design Spike Conclusion & Next Decision Gates

This design spike has successfully validated the physical characteristics of both access patterns:
*   The append-only model is consistent with base-table dead tuple divergence, but TOAST behavior remained inconclusive in this microbenchmark. It introduces a trade-off of **accelerated live-row generation growth** and index bloat.
*   The sharded mutable model remains a viable comparison candidate to evaluate if splitting keys reduces lock-wait times.

### Next Decision Gate
These directional short-run simulation metrics **do not authorize** active production changes or migrations. 

Prior to proposing any production implementation, the following subsequent gates are recommended:
1.  **Full-Length baseline soak sweeps (5 minutes per cell)** must be executed under the Ticket 0.5 harness to establish an authoritative, long-run baseline.
2.  **Compaction and Vacuum audits** must be drafted to outline how older append-only generation rows will be pruned or vacuumed out-of-band to prevent infinite disk expansion.
